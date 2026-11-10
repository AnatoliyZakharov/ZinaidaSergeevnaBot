"""
db.py — слой базы данных (SQLite).
При переезде на PostgreSQL достаточно заменить connect() на psycopg2/asyncpg
и поменять «?» на «%s» в запросах.
"""

import sqlite3
import time
import threading
from pathlib import Path
from typing import Optional

import os
import re

import crypto as _crypto
from crypto import CryptoDecryptError
DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent / "bot.db")))

# Блокировка для thread-safe операций (sqlite3 не thread-safe по умолчанию)
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Создаём таблицы при первом запуске."""
    with _lock:
        conn = _connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id       INTEGER PRIMARY KEY,
                telegram_username TEXT,
                -- Google OAuth токены
                google_access_token  TEXT,
                google_refresh_token TEXT,
                google_token_expiry  REAL,      -- Unix timestamp
                -- Настройки
                calendar_id  TEXT    DEFAULT 'primary',
                timezone     TEXT    DEFAULT 'Europe/Moscow',
                work_start   INTEGER DEFAULT 9,
                work_end     INTEGER DEFAULT 19,
                -- Яндекс Телемост (личный OAuth-токен, если есть)
                telemost_token TEXT,
                -- Мета
                is_active  INTEGER   DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Хранилище OAuth state (telegram_id → state, TTL 10 минут)
            CREATE TABLE IF NOT EXISTS oauth_states (
                state       TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL,
                created_at  REAL    DEFAULT (unixepoch())
            );

            -- Глоссарий контактов для приглашений
            CREATE TABLE IF NOT EXISTS contacts (
                telegram_id  INTEGER NOT NULL,
                name_lower   TEXT    NOT NULL,
                display_name TEXT    NOT NULL,
                email        TEXT    NOT NULL,
                PRIMARY KEY (telegram_id, name_lower)
            );

            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                event_type  TEXT    NOT NULL,
                created_at  REAL    DEFAULT (unixepoch())
            );
            CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
            CREATE INDEX IF NOT EXISTS idx_events_user ON events(telegram_id, created_at);
        """)
        conn.commit()
        # Миграция: добавляем MYmeet-поля если их ещё нет
        _migrate_mymeet(conn)
        conn.close()


def _migrate_mymeet(conn: sqlite3.Connection) -> None:
    """Добавляет MYmeet-колонки в таблицу users (идемпотентно)."""
    mymeet_cols = [
        ("mymeet_token",          "TEXT"),
        ("mymeet_ws_id",          "TEXT"),
        ("mymeet_spreadsheet_id", "TEXT"),
        ("mymeet_organizer_email","TEXT"),
        ("mymeet_setup_done",     "INTEGER DEFAULT 0"),
        # Флаг "wizard запущен, но ещё не завершён" — сохраняется при перезапуске бота
        ("mymeet_setup_in_progress", "INTEGER DEFAULT 0"),
        # Временные данные шага 1 (зашифрованный токен + ws_id) — для восстановления визарда
        ("mymeet_pending_token", "TEXT"),
        ("mymeet_pending_ws_id",  "TEXT"),
    ]
    for col, ddl in mymeet_cols:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # колонка уже существует


# ─── Пользователи ────────────────────────────────────────────

def get_user(telegram_id: int) -> Optional[sqlite3.Row]:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        conn.close()
    return row


def upsert_user(telegram_id: int, username: Optional[str] = None) -> None:
    """Создаём пользователя если нет, обновляем username если есть."""
    with _lock:
        conn = _connect()
        conn.execute("""
            INSERT INTO users (telegram_id, telegram_username)
            VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                telegram_username = excluded.telegram_username
        """, (telegram_id, username))
        conn.commit()
        conn.close()


def save_google_tokens(
    telegram_id: int,
    access_token: str,
    refresh_token: str,
    expiry: float,          # Unix timestamp
    calendar_id: str = "primary",
) -> None:
    with _lock:
        conn = _connect()
        conn.execute("""
            UPDATE users SET
                google_access_token  = ?,
                google_refresh_token = ?,
                google_token_expiry  = ?,
                calendar_id          = ?
            WHERE telegram_id = ?
        """, (access_token, refresh_token, expiry, calendar_id, telegram_id))
        conn.commit()
        conn.close()


def update_access_token(telegram_id: int, access_token: str, expiry: float) -> None:
    """Обновляем только access token после refresh."""
    with _lock:
        conn = _connect()
        conn.execute("""
            UPDATE users SET
                google_access_token = ?,
                google_token_expiry = ?
            WHERE telegram_id = ?
        """, (access_token, expiry, telegram_id))
        conn.commit()
        conn.close()


def save_telemost_token(telegram_id: int, token: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE users SET telemost_token = ? WHERE telegram_id = ?",
            (token, telegram_id)
        )
        conn.commit()
        conn.close()


def update_user_settings(
    telegram_id: int,
    timezone: Optional[str] = None,
    work_start: Optional[int] = None,
    work_end: Optional[int] = None,
) -> None:
    fields, values = [], []
    if timezone   is not None: fields.append("timezone = ?");   values.append(timezone)
    if work_start is not None: fields.append("work_start = ?"); values.append(work_start)
    if work_end   is not None: fields.append("work_end = ?");   values.append(work_end)
    if not fields:
        return
    values.append(telegram_id)
    with _lock:
        conn = _connect()
        conn.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE telegram_id = ?",
            values
        )
        conn.commit()
        conn.close()


def get_all_active_users() -> list[sqlite3.Row]:
    """Все пользователи с подключённым Google Calendar — для планировщика."""
    with _lock:
        conn = _connect()
        rows = conn.execute("""
            SELECT * FROM users
            WHERE is_active = 1
              AND google_refresh_token IS NOT NULL
        """).fetchall()
        conn.close()
    return rows


def is_google_connected(telegram_id: int) -> bool:
    user = get_user(telegram_id)
    return bool(user and user["google_refresh_token"])


# ─── OAuth state store ───────────────────────────────────────

def save_oauth_state(state: str, telegram_id: int) -> None:
    with _lock:
        conn = _connect()
        # Чистим устаревшие state (старше 10 минут)
        conn.execute(
            "DELETE FROM oauth_states WHERE created_at < ?",
            (time.time() - 600,)
        )
        conn.execute(
            "INSERT OR REPLACE INTO oauth_states (state, telegram_id) VALUES (?, ?)",
            (state, telegram_id)
        )
        conn.commit()
        conn.close()


def pop_oauth_state(state: str) -> Optional[int]:
    """Достаём telegram_id по state и удаляем запись."""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT telegram_id FROM oauth_states WHERE state = ?", (state,)
        ).fetchone()
        if row:
            conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
            conn.commit()
        conn.close()
    return row["telegram_id"] if row else None


# ─── Контакты (глоссарий) ────────────────────────────────────

def add_contact(telegram_id: int, display_name: str, email: str) -> None:
    """Добавляем или обновляем контакт."""
    name_lower = display_name.strip().lower()
    with _lock:
        conn = _connect()
        conn.execute("""
            INSERT INTO contacts (telegram_id, name_lower, display_name, email)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id, name_lower) DO UPDATE SET
                display_name = excluded.display_name,
                email        = excluded.email
        """, (telegram_id, name_lower, display_name.strip(), email.strip().lower()))
        conn.commit()
        conn.close()


def get_contact(telegram_id: int, name: str) -> Optional[sqlite3.Row]:
    """Ищем контакт: сначала точное, потом по началу строки."""
    name_lower = name.strip().lower()
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM contacts WHERE telegram_id = ? AND name_lower = ?",
            (telegram_id, name_lower)
        ).fetchone()
        if not row:
            # Ищем вхождение в любом месте: «Алексей» найдёт «Барышников Алексей»
            row = conn.execute(
                "SELECT * FROM contacts WHERE telegram_id = ? AND name_lower LIKE ?",
                (telegram_id, "%" + name_lower + "%")
            ).fetchone()
        if not row and len(name_lower) > 4:
            # Падеж: «Алексея» → «Алексе» → находит «Барышников Алексей»
            row = conn.execute(
                "SELECT * FROM contacts WHERE telegram_id = ? AND name_lower LIKE ?",
                (telegram_id, "%" + name_lower[:-1] + "%")
            ).fetchone()
        conn.close()
    return row


def list_contacts(telegram_id: int) -> list:
    """Все контакты пользователя, отсортированные по имени."""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM contacts WHERE telegram_id = ? ORDER BY display_name COLLATE NOCASE",
            (telegram_id,)
        ).fetchall()
        conn.close()
    return rows


def delete_contact(telegram_id: int, name: str) -> bool:
    """Удаляем контакт. Возвращает True если нашёл и удалил."""
    name_lower = name.strip().lower()
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "DELETE FROM contacts WHERE telegram_id = ? AND name_lower = ?",
            (telegram_id, name_lower)
        )
        conn.commit()
        deleted = cur.rowcount > 0
        conn.close()
    return deleted


def resolve_names_to_emails(telegram_id: int, text: str) -> tuple:
    """
    Разбивает text по запятой и слову «и», ищет каждую часть в контактах.
    Возвращает (found_emails: list[str], unknown_names: list[str]).
    """
    parts = re.split(r"\s*,\s*|\s+и\s+", text, flags=re.IGNORECASE)
    found, unknown = [], []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        contact = get_contact(telegram_id, part)
        if contact:
            found.append(contact["email"])
        else:
            unknown.append(part)
    return found, unknown


# ─── MYmeet конфигурация ─────────────────────────────────────

def save_mymeet_config(
    telegram_id: int,
    token: str,
    ws_id: str,
    spreadsheet_id: str,
    organizer_email: Optional[str] = None,
) -> None:
    """Сохраняем MYmeet-конфиг пользователя и помечаем setup_done.
    Токен шифруется перед записью в базу (Fernet/AES).

    Raises:
        crypto.CryptoError  — шифрование токена не удалось.
        RuntimeError        — строка пользователя не обновилась (внутренняя ошибка БД).
    """
    encrypted_token = _crypto.encrypt_token(token)
    with _lock:
        conn = _connect()
        cur = conn.execute("""
            UPDATE users SET
                mymeet_token           = ?,
                mymeet_ws_id           = ?,
                mymeet_spreadsheet_id  = ?,
                mymeet_organizer_email = ?,
                mymeet_setup_done          = 1,
                mymeet_setup_in_progress   = 0,
                mymeet_pending_token       = NULL,
                mymeet_pending_ws_id       = NULL
            WHERE telegram_id = ?
        """, (encrypted_token, ws_id, spreadsheet_id, organizer_email, telegram_id))
        if cur.rowcount == 0:
            # Страховка: UPDATE не нашёл строку (редкий edge-case — гонка или
            # cmd_setup_mymeet не вызвал upsert_user перед этим).
            # INSERT ... ON CONFLICT DO UPDATE задаёт ТОЛЬКО mymeet_*-поля
            # и никогда не трогает google_*, telemost_token и другие данные
            # пользователя. Если строка существует (гонка) — сработает
            # ON CONFLICT DO UPDATE и затронет только mymeet_* колонки.
            conn.execute("""
                INSERT INTO users (
                    telegram_id, mymeet_token, mymeet_ws_id,
                    mymeet_spreadsheet_id, mymeet_organizer_email, mymeet_setup_done
                ) VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    mymeet_token           = excluded.mymeet_token,
                    mymeet_ws_id           = excluded.mymeet_ws_id,
                    mymeet_spreadsheet_id  = excluded.mymeet_spreadsheet_id,
                    mymeet_organizer_email = excluded.mymeet_organizer_email,
                    mymeet_setup_done      = 1,
                    mymeet_pending_token   = NULL,
                    mymeet_pending_ws_id   = NULL
            """, (telegram_id, encrypted_token, ws_id, spreadsheet_id, organizer_email))
        conn.commit()
        # Верифицируем: читаем обратно и убеждаемся, что setup_done = 1
        verify = conn.execute(
            "SELECT mymeet_setup_done FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        conn.close()
    if not verify or not verify[0]:
        raise RuntimeError(
            f"save_mymeet_config: данные не сохранены для telegram_id={telegram_id}"
        )


def get_mymeet_users() -> list:
    """Все пользователи с настроенным MYmeet (для ежедневного синка).
    Токены расшифровываются перед возвратом; пользователи с нерасшифруемым
    токеном пропускаются (требуется пересетап через /setup_mymeet).
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    with _lock:
        conn = _connect()
        rows = conn.execute("""
            SELECT * FROM users
            WHERE mymeet_setup_done = 1
              AND mymeet_token IS NOT NULL
              AND is_active = 1
        """).fetchall()
        conn.close()

    result = []
    for row in rows:
        raw_token = row["mymeet_token"]
        try:
            if _crypto.is_encrypted(raw_token):
                decrypted = _crypto.decrypt_token(raw_token)
            else:
                # Старый незашифрованный токен — помечаем как устаревший и пропускаем
                _log.warning(
                    "get_mymeet_users: telegram_id=%s имеет незашифрованный токен — "
                    "требуется /setup_mymeet", row["telegram_id"]
                )
                continue
            # Возвращаем row с расшифрованным токеном через обёртку
            result.append(_DecryptedRow(row, decrypted))
        except CryptoDecryptError:
            _log.error(
                "get_mymeet_users: не удалось расшифровать токен для telegram_id=%s — "
                "пропускаем (нужен /setup_mymeet)", row["telegram_id"]
            )
    return result


class _DecryptedRow:
    """Обёртка над sqlite3.Row с подменённым токеном (расшифрованным)."""
    def __init__(self, row: sqlite3.Row, decrypted_token: str):
        self._row = row
        self._token = decrypted_token

    def __getitem__(self, key):
        if key == "mymeet_token":
            return self._token
        return self._row[key]

    def keys(self):
        return self._row.keys()

    def __iter__(self):
        return iter(self._row)


def reset_mymeet_config(telegram_id: int) -> None:
    """Сбрасываем MYmeet-конфиг (пересетап)."""
    with _lock:
        conn = _connect()
        conn.execute("""
            UPDATE users SET
                mymeet_token          = NULL,
                mymeet_ws_id          = NULL,
                mymeet_spreadsheet_id = NULL,
                mymeet_organizer_email = NULL,
                mymeet_setup_done        = 0,
                mymeet_setup_in_progress = 0,
                mymeet_pending_token     = NULL,
                mymeet_pending_ws_id     = NULL
            WHERE telegram_id = ?
        """, (telegram_id,))
        conn.commit()
        conn.close()


def set_mymeet_setup_in_progress(telegram_id: int, active: bool) -> None:
    """Помечаем пользователя как «wizard запущен» (active=True) или «завершён/отменён» (False).
    При active=False очищаем также pending-поля (временные данные шага 1).
    Персистируется в БД — переживает перезапуск бота."""
    with _lock:
        conn = _connect()
        if active:
            conn.execute(
                "UPDATE users SET mymeet_setup_in_progress = 1 WHERE telegram_id = ?",
                (telegram_id,),
            )
        else:
            conn.execute(
                "UPDATE users SET mymeet_setup_in_progress = 0, "
                "mymeet_pending_token = NULL, mymeet_pending_ws_id = NULL "
                "WHERE telegram_id = ?",
                (telegram_id,),
            )
        conn.commit()
        conn.close()


def save_mymeet_pending(telegram_id: int, token: str, ws_id: str) -> None:
    """Сохраняем pending-состояние визарда (зашифрованный токен + ws_id) в БД.
    Данные переживают перезапуск бота и позволяют восстановить шаг 2 без повторного
    ввода токена пользователем.

    Raises:
        crypto.CryptoError — шифрование не удалось.
    """
    encrypted_token = _crypto.encrypt_token(token)
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE users SET mymeet_pending_token = ?, mymeet_pending_ws_id = ? "
            "WHERE telegram_id = ?",
            (encrypted_token, ws_id, telegram_id),
        )
        conn.commit()
        conn.close()


def get_mymeet_pending(telegram_id: int) -> Optional[tuple]:
    """Возвращает (decrypted_token, ws_id) если есть pending-данные шага 1, иначе None.
    Пропускает нерасшифруемые токены — они будут пропущены при логировании.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    user = get_user(telegram_id)
    if not user:
        return None
    raw_token = user["mymeet_pending_token"]
    ws_id = user["mymeet_pending_ws_id"]
    if not raw_token or not ws_id:
        return None
    try:
        if _crypto.is_encrypted(raw_token):
            decrypted = _crypto.decrypt_token(raw_token)
        else:
            _log.warning(
                "get_mymeet_pending: незашифрованный pending-токен для telegram_id=%s",
                telegram_id,
            )
            return None
        return (decrypted, ws_id)
    except CryptoDecryptError:
        _log.error(
            "get_mymeet_pending: не удалось расшифровать pending-токен для telegram_id=%s",
            telegram_id,
        )
        return None

# ─── Метрики событий ─────────────────────────────────────────

def log_event(telegram_id: int, event_type: str) -> None:
    """Записываем событие в лог."""
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO events (telegram_id, event_type) VALUES (?, ?)",
            (telegram_id, event_type)
        )
        conn.commit()
        conn.close()


def get_stats() -> dict:
    """Сводная статистика для /stats."""
    with _lock:
        conn = _connect()

        total_users = conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        google_connected = conn.execute(
            "SELECT COUNT(*) FROM users WHERE google_refresh_token IS NOT NULL"
        ).fetchone()[0]

        new_users_7d = conn.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= datetime('now', '-7 days')"
        ).fetchone()[0]

        active_7d = conn.execute(
            "SELECT COUNT(DISTINCT telegram_id) FROM events WHERE created_at >= unixepoch('now', '-7 days')"
        ).fetchone()[0]

        active_30d = conn.execute(
            "SELECT COUNT(DISTINCT telegram_id) FROM events WHERE created_at >= unixepoch('now', '-30 days')"
        ).fetchone()[0]

        top_events = conn.execute("""
            SELECT event_type, COUNT(*) as cnt
            FROM events
            WHERE created_at >= unixepoch('now', '-30 days')
            GROUP BY event_type
            ORDER BY cnt DESC
            LIMIT 10
        """).fetchall()

        total_events_today = conn.execute(
            "SELECT COUNT(*) FROM events WHERE created_at >= unixepoch('now', 'start of day')"
        ).fetchone()[0]

        conn.close()

    return {
        "total_users": total_users,
        "google_connected": google_connected,
        "new_users_7d": new_users_7d,
        "active_7d": active_7d,
        "active_30d": active_30d,
        "top_events": [(row["event_type"], row["cnt"]) for row in top_events],
        "total_events_today": total_events_today,
    }
