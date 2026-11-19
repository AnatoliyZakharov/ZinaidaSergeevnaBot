"""
db.py — слой базы данных (SQLite).
При переезде на PostgreSQL достаточно заменить connect() на psycopg2/asyncpg
и поменять «?» на «%s» в запросах.
"""

from contextlib import closing
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import threading
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

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

            -- «Мои задачи»: задачи из Google Tasks / Telegram и учёт статусов
            -- протокольных задач (см. my_tasks.py). Строки никогда не удаляются
            -- ботом; UNIQUE защищает от дублей. Текстовые поля (title, origin,
            -- assignee, link) хранятся зашифрованными (enc:v1:<Fernet>).
            CREATE TABLE IF NOT EXISTS my_tasks (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id       INTEGER NOT NULL,
                source            TEXT    NOT NULL,   -- protocol | gtasks | telegram
                source_key        TEXT    NOT NULL,   -- p:… | gtasks:{list}:{id} | tg:{hash}
                title             TEXT    NOT NULL DEFAULT '',
                origin            TEXT    DEFAULT '', -- совещание / список GT / постановщик
                assignee          TEXT    DEFAULT '',
                task_date         TEXT    DEFAULT '', -- dd.mm.yyyy
                deadline          TEXT    DEFAULT '', -- dd.mm.yyyy
                status            TEXT    NOT NULL,
                verified          INTEGER DEFAULT 0,
                sheet_status      TEXT,               -- последний записанный в лист
                ext_status        TEXT,               -- последний увиденный во внешнем источнике
                ext_synced_status TEXT,               -- статус на момент сверки с GT
                gtasks_list_id    TEXT,
                gtasks_task_id    TEXT,
                missing_since     REAL,               -- пропала / удалена в Google Tasks
                created_at        REAL    DEFAULT (unixepoch()),
                updated_at        REAL    DEFAULT (unixepoch()),
                UNIQUE (telegram_id, source_key)
            );
            CREATE INDEX IF NOT EXISTS idx_my_tasks_user ON my_tasks(telegram_id);

            -- Ежедневная сверка Google Tasks (19:00 по поясу пользователя)
            CREATE TABLE IF NOT EXISTS my_tasks_sync (
                telegram_id     INTEGER PRIMARY KEY,
                last_daily_date TEXT
            );

            -- Ежедневный автоимпорт MYmeet (19:00 по поясу пользователя)
            CREATE TABLE IF NOT EXISTS mymeet_sync_state (
                telegram_id       INTEGER PRIMARY KEY,
                last_success_slot TEXT,     -- дата вечера, импортированного полностью
                last_success_ts   REAL,     -- момент, до которого окно покрыто
                attempt_slot      TEXT,
                slot_attempts     INTEGER DEFAULT 0,
                fail_count        INTEGER DEFAULT 0,
                next_attempt_ts   REAL,
                last_attempt_ts   REAL,
                last_outcome      TEXT,
                last_reason       TEXT,     -- код причины, без деталей
                notified_reason   TEXT,
                notified_ts       REAL
            );

            -- Продуктовая аналитика (#46): только id пользователя, тип события,
            -- время и разрешённый source — без текстов, токенов и названий.
            CREATE TABLE IF NOT EXISTS analytics_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,          -- = users.telegram_id
                event_type  TEXT    NOT NULL,
                created_at  REAL    NOT NULL,          -- UTC, секунды
                day         TEXT    NOT NULL,          -- UTC-дата YYYY-MM-DD
                metadata    TEXT,                      -- JSON из разрешённого списка
                dedup_key   TEXT    NOT NULL,          -- дата (ежедневные) / «once»
                UNIQUE (user_id, event_type, dedup_key)
            );
            CREATE INDEX IF NOT EXISTS idx_analytics_type_day
                ON analytics_events(event_type, day);
            CREATE INDEX IF NOT EXISTS idx_analytics_created
                ON analytics_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_analytics_user
                ON analytics_events(user_id, event_type);
            -- Истинная дата первой активности: не очищается вместе с сырыми
            -- событиями (иначе после очистки пользователь стал бы «новым»)
            CREATE TABLE IF NOT EXISTS analytics_users (
                user_id          INTEGER PRIMARY KEY,   -- = users.telegram_id
                first_active_day TEXT    NOT NULL,      -- UTC-дата YYYY-MM-DD
                first_active_ts  REAL    NOT NULL
            );
            INSERT OR IGNORE INTO analytics_users (user_id, first_active_day, first_active_ts)
                SELECT user_id, MIN(day), MIN(created_at) FROM analytics_events
                WHERE event_type = 'user_active' GROUP BY user_id;
            CREATE TABLE IF NOT EXISTS analytics_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO analytics_meta (key, value)
                VALUES ('started_at', CAST(unixepoch() AS TEXT));

            -- Задачи MYmeet, уже импортированные в таблицу: только sha256 ключа
            -- (таблица|встреча|дата|задача|ответственный) — без содержимого.
            CREATE TABLE IF NOT EXISTS mymeet_imported (
                telegram_id   INTEGER NOT NULL,
                key_hash      TEXT    NOT NULL,
                first_seen_ts REAL    DEFAULT (unixepoch()),
                PRIMARY KEY (telegram_id, key_hash)
            );
        """)
        conn.commit()
        # Миграция: добавляем MYmeet-поля если их ещё нет
        _migrate_mymeet(conn)
        _migrate_add_columns(conn, "users", _CREDENTIAL_COLUMNS)
        _migrate_add_columns(conn, "my_tasks", _MY_TASKS_NEW_COLS)
        _migrate_add_columns(conn, "my_tasks_sync", _MY_TASKS_SYNC_NEW_COLS)
        conn.close()


_CREDENTIAL_COLUMNS = [
    ("google_subject", "TEXT"),
    ("google_granted_scopes", "TEXT"),
    ("google_grant_version", "TEXT"),
    ("mymeet_sheet_grant_version", "TEXT"),
]


# Колонки, добавленные после первой версии таблиц «Мои задачи» (идемпотентно)
_MY_TASKS_NEW_COLS = [
    ("link",              "TEXT"),               # ссылка на письмо (Tasks API links)
    ("gt_delete_pending", "INTEGER DEFAULT 0"),  # ручная отмена → удалить в GT
    ("prev_status",       "TEXT"),               # статус до удаления в GT
    ("sheet_fields",      "TEXT"),               # что бот записал в ячейки (enc JSON)
    ("overrides",         "TEXT"),               # ручные правки ячеек (enc JSON)
    ("gtasks_parent_id",  "TEXT"),               # Tasks API parent (подзадача)
    ("gtasks_position",   "TEXT"),               # Tasks API position (порядок)
]
_MY_TASKS_SYNC_NEW_COLS = [
    ("last_success_slot", "TEXT"),     # дата слота 19:00, выполненного полностью
    ("fail_count",        "INTEGER DEFAULT 0"),
    ("next_attempt_ts",   "REAL"),
    ("last_attempt_ts",   "REAL"),
    ("last_outcome",      "TEXT"),
    ("notified_slot",     "TEXT"),
]


def _migrate_add_columns(conn: sqlite3.Connection, table: str, cols: list) -> None:
    """ALTER TABLE ADD COLUMN для отсутствующих колонок. Ничего не удаляет."""
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for col, ddl in cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn.commit()


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
        # ФИО пользователя для личного листа «Мои задачи» (не сбрасывается /reset_mymeet)
        ("mymeet_full_name",      "TEXT"),
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


_TOKEN_FIELDS = ("google_access_token", "google_refresh_token", "telemost_token")
_GOOGLE_TOKEN_FIELDS = _TOKEN_FIELDS[:2]
_CREDENTIAL_PREFIX = "credential:v1:"
_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"


def _seal_credential(telegram_id: int, field: str, value, *, encrypt=None):
    if value is None or value == "":
        return value
    body = json.dumps([telegram_id, field, value], ensure_ascii=False)
    return _CREDENTIAL_PREFIX + (encrypt or _crypto.encrypt_token)(body)


def _read_credential(telegram_id: int, field: str, value, *, decrypt=None):
    if value is None or value == "":
        return value
    if not isinstance(value, str):
        raise CryptoDecryptError("Invalid stored credential type")
    if not value.startswith("credential:"):
        return value  # Legacy plaintext.
    if not value.startswith(_CREDENTIAL_PREFIX):
        raise CryptoDecryptError("Unsupported stored credential format")
    try:
        user, stored_field, token = json.loads(
            (decrypt or _crypto.decrypt_token)(value[len(_CREDENTIAL_PREFIX):])
        )
    except (TypeError, ValueError) as error:
        raise CryptoDecryptError("Invalid stored credential") from error
    if user != telegram_id or stored_field != field or not isinstance(token, str) or not token:
        raise CryptoDecryptError("Invalid stored credential context")
    return token


def _validate_key_witness(conn, telegram_id) -> bool:
    row = conn.execute(
        "SELECT mymeet_token, mymeet_pending_token FROM users WHERE telegram_id = ?",
        (telegram_id,),
    ).fetchone()
    for value in row or ():
        if _crypto.is_encrypted(value):
            _crypto.decrypt_token(value)
            return True
    for field in _TOKEN_FIELDS:
        witness = conn.execute(
            f"SELECT telegram_id, {field} FROM users WHERE {field} LIKE 'credential:v1:%' LIMIT 1"
        ).fetchone()
        if witness:
            _read_credential(witness["telegram_id"], field, witness[field])
            return True
    for witness in conn.execute(
        "SELECT mymeet_token, mymeet_pending_token FROM users "
        "WHERE mymeet_token IS NOT NULL OR mymeet_pending_token IS NOT NULL"
    ):
        for value in witness:
            if _crypto.is_encrypted(value):
                _crypto.decrypt_token(value)
                return True
    return False


def _validate_existing_credentials(conn, telegram_id, fields):
    row = conn.execute(
        f"SELECT {', '.join(fields)} FROM users WHERE telegram_id = ?", (telegram_id,),
    ).fetchone()
    if row:
        tokens = [_read_credential(telegram_id, field, row[field]) for field in fields]
        if any(token != row[field] for field, token in zip(fields, tokens)):
            return  # An existing envelope was authenticated.
        if not _validate_key_witness(conn, telegram_id) and any(tokens):
            raise CryptoDecryptError("No trusted encrypted witness for updating legacy credentials")


def _serialize_scopes(granted):
    return json.dumps(sorted(set(granted))) if granted is not None else None


def get_google_grant(telegram_id: int) -> Optional[dict]:
    """Read OAuth credentials explicitly; normal user lookups keep ciphertext."""
    with _lock, closing(_connect()) as conn:
        row = conn.execute(
            "SELECT google_access_token, google_refresh_token, google_token_expiry, "
            "calendar_id, google_subject, google_granted_scopes, google_grant_version "
            "FROM users WHERE telegram_id = ?", (telegram_id,),
        ).fetchone()
    if not row or not row["google_refresh_token"]:
        return None
    granted = row["google_granted_scopes"]
    granted = frozenset(json.loads(granted)) if granted is not None else None
    return {
        "access_token": _read_credential(telegram_id, "google_access_token", row["google_access_token"]),
        "refresh_token": _read_credential(telegram_id, "google_refresh_token", row["google_refresh_token"]),
        "expiry": row["google_token_expiry"],
        "calendar_id": row["calendar_id"],
        "subject": row["google_subject"],
        "granted": granted,
        "version": row["google_grant_version"],
        "drive_file_authorized": _DRIVE_FILE_SCOPE in (granted or ()),
    }


def save_google_grant(telegram_id: int, *, access_token: str, refresh_token: str,
                      expiry: float, subject: str, granted) -> str:
    """Atomically promote the whole verified OAuth grant while preserving settings."""
    if not all((access_token, refresh_token, subject)):
        raise ValueError("Incomplete verified Google grant")
    version = secrets.token_urlsafe(24)
    with _lock, closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _validate_existing_credentials(conn, telegram_id, _GOOGLE_TOKEN_FIELDS)
        cur = conn.execute(
            "UPDATE users SET google_access_token = ?, google_refresh_token = ?, "
            "google_token_expiry = ?, google_subject = ?, "
            "google_granted_scopes = ?, google_grant_version = ? WHERE telegram_id = ?",
            (_seal_credential(telegram_id, "google_access_token", access_token),
             _seal_credential(telegram_id, "google_refresh_token", refresh_token),
             expiry, subject, _serialize_scopes(granted), version, telegram_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("No user found for confirmed Google grant")
    return version


def refresh_google_grant(telegram_id: int, expected_version: str | None, *,
                         access_token: str, expiry: float, refresh_token: str | None = None,
                         granted=None, expected_refresh_token: str | None = None) -> bool:
    """Commit a refresh only while the credentials it used are still current."""
    with _lock, closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT google_grant_version, google_refresh_token, google_access_token "
            "FROM users WHERE telegram_id = ?", (telegram_id,),
        ).fetchone()
        if not row or row["google_grant_version"] != expected_version or not row["google_refresh_token"]:
            return False
        current_refresh = _read_credential(telegram_id, "google_refresh_token", row["google_refresh_token"])
        if expected_refresh_token is not None and current_refresh != expected_refresh_token:
            return False
        _validate_existing_credentials(conn, telegram_id, _GOOGLE_TOKEN_FIELDS)
        conn.execute(
            "UPDATE users SET google_access_token = ?, google_token_expiry = ?, "
            "google_refresh_token = ?, google_granted_scopes = COALESCE(?, google_granted_scopes) "
            "WHERE telegram_id = ?",
            (_seal_credential(telegram_id, "google_access_token", access_token), expiry,
             (_seal_credential(telegram_id, "google_refresh_token", refresh_token)
              if refresh_token else row["google_refresh_token"]),
             _serialize_scopes(granted), telegram_id),
        )
        return True


def disconnect_google_grant(telegram_id: int) -> None:
    """Remove an explicit user's grant without changing calendar or Sheet settings."""
    with _lock, closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _validate_existing_credentials(conn, telegram_id, _GOOGLE_TOKEN_FIELDS)
        conn.execute(
            "UPDATE users SET google_access_token = '', google_refresh_token = '', "
            "google_token_expiry = 0, google_subject = NULL, "
            "google_granted_scopes = NULL, google_grant_version = NULL WHERE telegram_id = ?",
            (telegram_id,),
        )


def save_google_tokens(telegram_id: int, access_token: str, refresh_token: str,
                       expiry: float, calendar_id: str = "primary") -> None:
    """Compatibility setter for integrations without verified grant metadata."""
    with _lock, closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _validate_existing_credentials(conn, telegram_id, _GOOGLE_TOKEN_FIELDS)
        conn.execute(
            "UPDATE users SET google_access_token = ?, google_refresh_token = ?, "
            "google_token_expiry = ?, calendar_id = ?, google_subject = NULL, "
            "google_granted_scopes = NULL, google_grant_version = NULL WHERE telegram_id = ?",
            (_seal_credential(telegram_id, "google_access_token", access_token),
             _seal_credential(telegram_id, "google_refresh_token", refresh_token),
             expiry, calendar_id, telegram_id),
        )


def update_access_token(telegram_id: int, access_token: str, expiry: float) -> None:
    """Compatibility setter for a legacy access-token refresh."""
    with _lock, closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _validate_existing_credentials(conn, telegram_id, _GOOGLE_TOKEN_FIELDS)
        conn.execute(
            "UPDATE users SET google_access_token = ?, google_token_expiry = ? WHERE telegram_id = ?",
            (_seal_credential(telegram_id, "google_access_token", access_token), expiry, telegram_id),
        )


def get_telemost_token(telegram_id: int) -> Optional[str]:
    with _lock, closing(_connect()) as conn:
        row = conn.execute(
            "SELECT telemost_token FROM users WHERE telegram_id = ?", (telegram_id,),
        ).fetchone()
    return _read_credential(telegram_id, "telemost_token", row[0]) if row else None


def save_telemost_token(telegram_id: int, token: str) -> None:
    with _lock, closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _validate_existing_credentials(conn, telegram_id, ("telemost_token",))
        conn.execute(
            "UPDATE users SET telemost_token = ? WHERE telegram_id = ?",
            (_seal_credential(telegram_id, "telemost_token", token), telegram_id),
        )


_PRODUCTION_DB_PATH = Path("/data/bot.db")


def migrate_credential_envelopes(database_path: Path, *,
                                 expected_key_fingerprint: str | None = None) -> int:
    """Explicit offline conversion for a database copy. Never called by app startup."""
    path = Path(database_path).resolve(strict=True)
    for active in (DB_PATH, _PRODUCTION_DB_PATH):
        protected_path = active.resolve()
        if path == protected_path or (protected_path.exists() and path.samefile(protected_path)):
            raise ValueError("Credential conversion requires a separate database copy")
    key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("Credential conversion requires ENCRYPTION_KEY")
    fingerprint_verified = expected_key_fingerprint is not None and hmac.compare_digest(
        hashlib.sha256(key.encode()).hexdigest(), expected_key_fingerprint,
    )
    if expected_key_fingerprint is not None and not fingerprint_verified:
        raise CryptoDecryptError("Credential conversion key fingerprint did not match")
    try:
        fernet = Fernet(key.encode())
    except (TypeError, ValueError):
        raise CryptoDecryptError("Invalid credential conversion key") from None

    def encrypt(value):
        return fernet.encrypt(value.encode()).decode("ascii")

    def decrypt(value):
        try:
            return fernet.decrypt(value.encode("ascii")).decode()
        except (InvalidToken, UnicodeError):
            raise CryptoDecryptError("Could not authenticate a stored credential") from None

    with _lock, closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        legacy_fields = tuple(field for field in ("mymeet_token", "mymeet_pending_token") if field in columns)
        fields = ", ".join(("telegram_id", *_TOKEN_FIELDS, *legacy_fields))
        witnessed = False
        updates = []
        for row in conn.execute(f"SELECT {fields} FROM users"):
            telegram_id = row["telegram_id"]
            for field in legacy_fields:
                if _crypto.is_encrypted(row[field]):
                    decrypt(row[field])
                    witnessed = True
            converted = []
            for field in _TOKEN_FIELDS:
                previous = row[field]
                token = _read_credential(telegram_id, field, previous, decrypt=decrypt)
                if token != previous:
                    witnessed = True
                    converted.append(previous)
                else:
                    converted.append(_seal_credential(telegram_id, field, token, encrypt=encrypt))
            if any(value != row[field] for field, value in zip(_TOKEN_FIELDS, converted)):
                updates.append((*converted, telegram_id))
        if updates and not (witnessed or fingerprint_verified):
            raise CryptoDecryptError("No trusted encrypted witness or key fingerprint for conversion")
        conn.executemany(
            "UPDATE users SET google_access_token = ?, google_refresh_token = ?, "
            "telemost_token = ? WHERE telegram_id = ?", updates,
        )
        return len(updates)


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
    *,
    expected_google_grant_version: str | None = None,
) -> None:
    """A single transaction promotes the validated configuration and clears its draft."""
    with _lock, closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _validate_key_witness(conn, telegram_id)
        encrypted_token = _crypto.encrypt_token(token)
        if expected_google_grant_version is not None:
            grant = conn.execute(
                "SELECT google_grant_version, google_granted_scopes FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            if (not grant or grant["google_grant_version"] != expected_google_grant_version
                    or _DRIVE_FILE_SCOPE not in json.loads(grant["google_granted_scopes"] or "[]")):
                raise PermissionError("Google grant changed before Sheet confirmation")
        conn.execute("""
            INSERT INTO users (
                telegram_id, mymeet_token, mymeet_ws_id, mymeet_spreadsheet_id,
                mymeet_organizer_email, mymeet_setup_done, mymeet_setup_in_progress, mymeet_sheet_grant_version
            ) VALUES (?, ?, ?, ?, ?, 1, 0, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                mymeet_token = excluded.mymeet_token,
                mymeet_ws_id = excluded.mymeet_ws_id,
                mymeet_spreadsheet_id = excluded.mymeet_spreadsheet_id,
                mymeet_organizer_email = excluded.mymeet_organizer_email,
                mymeet_setup_done = 1,
                mymeet_setup_in_progress = 0,
                mymeet_pending_token = NULL,
                mymeet_pending_ws_id = NULL,
                mymeet_sheet_grant_version = excluded.mymeet_sheet_grant_version
        """, (telegram_id, encrypted_token, ws_id, spreadsheet_id, organizer_email,
              expected_google_grant_version))


def get_sheet_grant_binding(telegram_id: int, sheet_id: str) -> Optional[str]:
    """A verified binding is valid only for its exact target and active grant."""
    with _lock, closing(_connect()) as conn:
        row = conn.execute(
            "SELECT mymeet_sheet_grant_version, google_granted_scopes FROM users "
            "WHERE telegram_id = ? AND mymeet_spreadsheet_id = ? AND mymeet_setup_done = 1 "
            "AND mymeet_sheet_grant_version = google_grant_version "
            "AND google_refresh_token IS NOT NULL AND google_refresh_token != ''",
            (telegram_id, sheet_id),
        ).fetchone()
    if row and _DRIVE_FILE_SCOPE in json.loads(row["google_granted_scopes"] or "[]"):
        return row["mymeet_sheet_grant_version"]
    return None


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
                mymeet_pending_ws_id     = NULL,
                mymeet_sheet_grant_version = NULL
            WHERE telegram_id = ?
        """, (telegram_id,))
        conn.commit()
        conn.close()


def set_full_name(telegram_id: int, name: str) -> None:
    """Сохраняем ФИО для листа «Мои задачи» (пробелы схлопываются)."""
    value = " ".join(str(name or "").split())
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE users SET mymeet_full_name = ? WHERE telegram_id = ?",
            (value, telegram_id),
        )
        conn.commit()
        conn.close()


def set_mymeet_setup_in_progress(telegram_id: int, active: bool) -> None:
    """Start or cancel a draft. Active MYmeet and Google settings remain available."""
    with _lock, closing(_connect()) as conn, conn:
        conn.execute(
            """UPDATE users SET mymeet_setup_in_progress = ?,
                mymeet_pending_token = NULL, mymeet_pending_ws_id = NULL
                WHERE telegram_id = ?""",
            (int(active), telegram_id),
        )


def save_mymeet_pending(telegram_id: int, token: str, ws_id: str) -> None:
    """Атомарно сохраняем pending-состояние визарда: зашифрованный токен, ws_id и
    mymeet_setup_in_progress=1 в одной транзакции с верификацией успешной записи.
    Данные переживают перезапуск бота и позволяют восстановить шаг 2 без повторного
    ввода токена пользователем.

    Raises:
        crypto.CryptoError — шифрование не удалось.
        RuntimeError — данные не подтвердились после коммита (диск, БД).
    """
    encrypted_token = _crypto.encrypt_token(token)
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE users "
            "SET mymeet_pending_token = ?, mymeet_pending_ws_id = ?, "
            "mymeet_setup_in_progress = 1 "
            "WHERE telegram_id = ?",
            (encrypted_token, ws_id, telegram_id),
        )
        conn.commit()
        verify = conn.execute(
            "SELECT mymeet_pending_token, mymeet_pending_ws_id, mymeet_setup_in_progress "
            "FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        conn.close()
    if not verify or not verify[0] or not verify[1] or not verify[2]:
        raise RuntimeError(
            "save_mymeet_pending: данные не подтвердились после записи"
        )


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

# ─── «Мои задачи» (my_tasks.py) ──────────────────────────────

_MY_TASK_FIELDS = (
    "source", "source_key", "title", "origin", "assignee", "task_date", "deadline",
    "status", "verified", "sheet_status", "ext_status", "ext_synced_status",
    "gtasks_list_id", "gtasks_task_id", "missing_since",
    "link", "gt_delete_pending", "prev_status", "sheet_fields", "overrides",
    "gtasks_parent_id", "gtasks_position",
)
# Пользовательский текст — шифруется (Fernet, тот же ENCRYPTION_KEY, что у
# MYmeet-токенов).  Префикс отличает шифротекст от старого открытого текста.
_MY_TASK_ENCRYPTED = ("title", "origin", "assignee", "link", "sheet_fields", "overrides")
_ENC_PREFIX = "enc:v1:"


def _enc(value):
    if value is None or value == "":
        return value
    return _ENC_PREFIX + _crypto.encrypt_token(str(value))


def _dec_record(row) -> dict:
    """Строка БД → dict с расшифрованными полями.
    Ошибка расшифровки: поле = None, _decrypt_error = True (данные в БД не
    трогаются; вызывающий не должен перезаписывать лист/запись)."""
    r = dict(row)
    for f in _MY_TASK_ENCRYPTED:
        v = r.get(f)
        if isinstance(v, str) and v.startswith(_ENC_PREFIX):
            try:
                r[f] = _crypto.decrypt_token(v[len(_ENC_PREFIX):])
            except Exception:
                r[f] = None
                r["_decrypt_error"] = True
        elif v:
            r["_plaintext"] = True      # старая незашифрованная запись
    return r


def my_tasks_load(telegram_id: int) -> list[dict]:
    """Все записи «Мои задачи» пользователя (dict по полям _MY_TASK_FIELDS)."""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            f"SELECT {', '.join(_MY_TASK_FIELDS)} FROM my_tasks WHERE telegram_id = ? "
            "ORDER BY id",
            (telegram_id,),
        ).fetchall()
        conn.close()
    return [_dec_record(r) for r in rows]


def my_tasks_get(telegram_id: int, source_key: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        row = conn.execute(
            f"SELECT {', '.join(_MY_TASK_FIELDS)} FROM my_tasks "
            "WHERE telegram_id = ? AND source_key = ?",
            (telegram_id, source_key),
        ).fetchone()
        conn.close()
    return _dec_record(row) if row else None


def _record_params(telegram_id: int, r: dict, cols: list) -> list:
    vals = []
    for c in cols:
        v = r.get(c)
        if c == "status":
            v = v or "В работе"
        elif c == "title":
            v = v or ""
        elif c == "gt_delete_pending":
            v = int(v or 0)
        if c in _MY_TASK_ENCRYPTED:
            v = _enc(v)
        vals.append(v)
    return [telegram_id] + vals


def my_tasks_upsert(telegram_id: int, records: list[dict]) -> None:
    """Вставка/обновление записей по (telegram_id, source_key). Ничего не удаляет.
    Записи с ошибкой расшифровки не сохраняются (защита от затирания)."""
    records = [r for r in records if not r.get("_decrypt_error")]
    if not records:
        return
    cols = list(_MY_TASK_FIELDS)
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "source_key")
    sql = (
        f"INSERT INTO my_tasks (telegram_id, {', '.join(cols)}) VALUES (?, {placeholders}) "
        f"ON CONFLICT(telegram_id, source_key) DO UPDATE SET {updates}, "
        "updated_at = unixepoch()"
    )
    params = [_record_params(telegram_id, r, cols) for r in records]
    with _lock:
        conn = _connect()
        conn.executemany(sql, params)
        conn.commit()
        conn.close()


def my_tasks_insert_new(telegram_id: int, record: dict) -> bool:
    """Вставляет запись, если её ещё нет. True — создана, False — уже была (дубль)."""
    cols = [c for c in _MY_TASK_FIELDS if c in record]
    with _lock:
        conn = _connect()
        cur = conn.execute(
            f"INSERT OR IGNORE INTO my_tasks (telegram_id, {', '.join(cols)}) "
            f"VALUES (?, {', '.join('?' for _ in cols)})",
            _record_params(telegram_id, record, cols),
        )
        conn.commit()
        created = cur.rowcount > 0
        conn.close()
    return created


def my_tasks_rekey(telegram_id: int, old_key: str, new_key: str) -> bool:
    """Переименовывает ключ записи (миграция ключа Google Tasks, перенос задачи
    между списками). Не выполняется, если новый ключ уже занят (без дублей)."""
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "UPDATE my_tasks SET source_key = ? WHERE telegram_id = ? AND source_key = ? "
            "AND NOT EXISTS (SELECT 1 FROM my_tasks WHERE telegram_id = ? AND source_key = ?)",
            (new_key, telegram_id, old_key, telegram_id, new_key),
        )
        conn.commit()
        ok = cur.rowcount > 0
        conn.close()
    return ok


def my_tasks_mark_written(telegram_id: int, statuses: dict) -> None:
    """После успешной записи листа: sheet_status (и sheet_fields — записанные
    значения ячеек) = то, что бот записал.  Значение — статус или
    (статус, JSON записанных ячеек)."""
    if not statuses:
        return
    params = []
    for key, val in statuses.items():
        st, fields = val if isinstance(val, tuple) else (val, None)
        params.append((st, _enc(fields), telegram_id, key))
    with _lock:
        conn = _connect()
        conn.executemany(
            "UPDATE my_tasks SET sheet_status = ?, sheet_fields = COALESCE(?, sheet_fields) "
            "WHERE telegram_id = ? AND source_key = ?",
            params,
        )
        conn.commit()
        conn.close()


_SYNC_FIELDS = ("last_success_slot", "fail_count", "next_attempt_ts", "last_attempt_ts",
                "last_outcome", "notified_slot")


def my_tasks_sync_get(telegram_id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        row = conn.execute(
            f"SELECT {', '.join(_SYNC_FIELDS)} FROM my_tasks_sync WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        conn.close()
    return dict(row) if row else None


def my_tasks_sync_save(telegram_id: int, **fields) -> None:
    """Обновляет состояние ежедневной сверки (только переданные поля)."""
    fields = {k: v for k, v in fields.items() if k in _SYNC_FIELDS}
    if not fields:
        return
    cols = list(fields)
    with _lock:
        conn = _connect()
        conn.execute(
            f"INSERT INTO my_tasks_sync (telegram_id, {', '.join(cols)}) "
            f"VALUES (?, {', '.join('?' for _ in cols)}) "
            f"ON CONFLICT(telegram_id) DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in cols),
            [telegram_id] + [fields[c] for c in cols],
        )
        conn.commit()
        conn.close()


# ─── Автоимпорт MYmeet: состояние и реестр ───────────────────

_MYMEET_STATE_FIELDS = ("last_success_slot", "last_success_ts", "attempt_slot",
                        "slot_attempts", "fail_count", "next_attempt_ts",
                        "last_attempt_ts", "last_outcome", "last_reason",
                        "notified_reason", "notified_ts")


def get_mymeet_user_rows() -> list:
    """Пользователи с настроенным MYmeet — строки как есть (токен зашифрован;
    расшифровка — там же, где у ручной /sync_mymeet)."""
    with _lock:
        conn = _connect()
        rows = conn.execute("""
            SELECT * FROM users
            WHERE mymeet_setup_done = 1
              AND mymeet_token IS NOT NULL
              AND is_active = 1
        """).fetchall()
        conn.close()
    return rows


def mymeet_sync_get(telegram_id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        row = conn.execute(
            f"SELECT {', '.join(_MYMEET_STATE_FIELDS)} FROM mymeet_sync_state "
            f"WHERE telegram_id = ?", (telegram_id,),
        ).fetchone()
        conn.close()
    return dict(row) if row else None


def mymeet_sync_save(telegram_id: int, **fields) -> None:
    """Обновляет состояние автоимпорта (только переданные известные поля)."""
    fields = {k: v for k, v in fields.items() if k in _MYMEET_STATE_FIELDS}
    if not fields:
        return
    cols = list(fields)
    with _lock:
        conn = _connect()
        conn.execute(
            f"INSERT INTO mymeet_sync_state (telegram_id, {', '.join(cols)}) "
            f"VALUES (?, {', '.join('?' for _ in cols)}) "
            f"ON CONFLICT(telegram_id) DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in cols),
            [telegram_id] + [fields[c] for c in cols],
        )
        conn.commit()
        conn.close()


def mymeet_imported_known(telegram_id: int, hashes: list) -> set:
    """Какие из хэшей уже есть в реестре импорта."""
    found: set = set()
    hashes = list(hashes)
    with _lock:
        conn = _connect()
        for i in range(0, len(hashes), 500):
            chunk = hashes[i:i + 500]
            rows = conn.execute(
                f"SELECT key_hash FROM mymeet_imported WHERE telegram_id = ? "
                f"AND key_hash IN ({', '.join('?' for _ in chunk)})",
                [telegram_id] + chunk,
            ).fetchall()
            found.update(r[0] for r in rows)
        conn.close()
    return found


def mymeet_imported_add(telegram_id: int, hashes: list) -> None:
    """Добавляет хэши в реестр (повтор — без изменений)."""
    if not hashes:
        return
    with _lock:
        conn = _connect()
        conn.executemany(
            "INSERT OR IGNORE INTO mymeet_imported (telegram_id, key_hash) VALUES (?, ?)",
            [(telegram_id, h) for h in hashes],
        )
        conn.commit()
        conn.close()


# ─── Продуктовая аналитика (#46) ─────────────────────────────

ANALYTICS_DAILY = ("user_active", "connect_started", "connect_completed",
                   "tasks_used", "mymeet_used")
ANALYTICS_ONCE = ("user_started", "first_value")
ANALYTICS_EVENTS = ANALYTICS_DAILY + ANALYTICS_ONCE
# metadata: единственное разрешённое поле — source из фиксированного списка
ANALYTICS_SOURCES = ("calendar_event", "tasks", "sync_tasks", "sync_mymeet",
                     "sync_mymeet_full", "setup_mymeet", "forward")
ANALYTICS_RETENTION_DAYS = 396          # ≈ 13 месяцев


def _utc_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def analytics_record(user_id: int, event_type: str, *, source: Optional[str] = None,
                     now: Optional[float] = None) -> bool:
    """Записывает событие.  True — записано, False — дубль (уже есть за этот
    день для ежедневных событий / вообще для разовых).  Неизвестный тип или
    source — ValueError (в базу попадает только разрешённое)."""
    if event_type not in ANALYTICS_EVENTS:
        raise ValueError("unknown analytics event")
    if source is not None and source not in ANALYTICS_SOURCES:
        raise ValueError("unknown analytics source")
    ts = time.time() if now is None else float(now)
    day = _utc_day(ts)
    dedup = "once" if event_type in ANALYTICS_ONCE else day
    meta = '{"source": "%s"}' % source if source else None
    with _lock:
        conn = _connect()
        if event_type == "user_started" and conn.execute(
                "SELECT 1 FROM analytics_users WHERE user_id = ?", (int(user_id),)).fetchone():
            # уже был активен (возможно, его ранние события удалены очисткой) —
            # не «новый» пользователь
            conn.close()
            return False
        cur = conn.execute(
            "INSERT OR IGNORE INTO analytics_events "
            "(user_id, event_type, created_at, day, metadata, dedup_key) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(user_id), event_type, ts, day, meta, dedup),
        )
        if event_type == "user_active":
            conn.execute(
                "INSERT INTO analytics_users (user_id, first_active_day, first_active_ts) "
                "VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "first_active_day = MIN(first_active_day, excluded.first_active_day), "
                "first_active_ts = MIN(first_active_ts, excluded.first_active_ts)",
                (int(user_id), day, ts))
        conn.commit()
        added = cur.rowcount == 1
        conn.close()
    return added


def analytics_started_at() -> Optional[float]:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT value FROM analytics_meta WHERE key = 'started_at'").fetchone()
        conn.close()
    try:
        return float(row[0]) if row else None
    except (TypeError, ValueError):
        return None


def analytics_cleanup(now: Optional[float] = None,
                      days: int = ANALYTICS_RETENTION_DAYS) -> int:
    """Удаляет сырые события старше days.  Только analytics_events:
    analytics_users (дата первой активности) сохраняется."""
    cutoff = (time.time() if now is None else float(now)) - days * 86400
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM analytics_events WHERE created_at < ?", (cutoff,))
        conn.commit()
        n = cur.rowcount
        conn.close()
    return n


def analytics_delete_user(user_id: int) -> int:
    """Удаляет все аналитические данные пользователя (при удалении его данных):
    события и дату первой активности.  Возвращает число удалённых событий."""
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM analytics_events WHERE user_id = ?", (int(user_id),))
        n = cur.rowcount
        conn.execute("DELETE FROM analytics_users WHERE user_id = ?", (int(user_id),))
        conn.commit()
        conn.close()
    return n


def _shift_day(day: str, n: int) -> str:
    """Календарный сдвиг даты YYYY-MM-DD на n дней (без часовых поясов)."""
    from datetime import date, timedelta
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def analytics_report_data(now: Optional[float] = None) -> dict:
    """Метрики по определениям #46 (всё в UTC).

    DAU/WAU/MAU — уникальные user_active за сегодня / 7 / 30 календарных дней
    (включая сегодня).  Новые — users.created_at за 30 дней.  Функции —
    уникальные пользователи событий за 30 дней.  Retention Dn — когорта по
    дню первой user_active; в знаменателе только когорты, у которых день
    когорта+n уже завершён; в числителе — активные ровно в этот день.
    Воронка — когорта user_started за 30 дней; шаг засчитывается, если его
    первое событие не раньше первого события предыдущего шага."""
    ts = time.time() if now is None else float(now)
    today = _utc_day(ts)
    d7, d30 = _shift_day(today, -6), _shift_day(today, -29)
    with _lock:
        conn = _connect()
        q = conn.execute

        def uniq(event_type, since):
            return q("SELECT COUNT(DISTINCT user_id) FROM analytics_events "
                     "WHERE event_type = ? AND day >= ? AND day <= ?",
                     (event_type, since, today)).fetchone()[0]

        dau, wau, mau = uniq("user_active", today), uniq("user_active", d7), uniq("user_active", d30)
        features = {e: uniq(e, d30) for e in ("tasks_used", "mymeet_used", "connect_completed")}
        new_users = q("SELECT COUNT(*) FROM users WHERE created_at >= ? AND created_at < ?",
                      (d30 + " 00:00:00", _shift_day(today, 1) + " 00:00:00")).fetchone()[0]
        total_users = q("SELECT COUNT(*) FROM users").fetchone()[0]
        active_days = q("SELECT user_id, day FROM analytics_events "
                        "WHERE event_type = 'user_active'").fetchall()
        first_active = dict(q("SELECT user_id, first_active_day FROM analytics_users"))
        firsts = {}
        for et in ("user_started", "connect_started", "connect_completed", "first_value"):
            firsts[et] = {}
            for uid, cat in q("SELECT user_id, created_at FROM analytics_events "
                              "WHERE event_type = ? ORDER BY created_at", (et,)):
                firsts[et].setdefault(uid, []).append(cat)
        conn.close()

    # ── Retention ──
    days_by_user: dict = {}
    for uid, day in active_days:
        days_by_user.setdefault(uid, set()).add(day)
    # Когорта — истинная дата первой активности (analytics_users).  Когорты,
    # чьи события уже могли быть удалены очисткой, в расчёт не входят.
    oldest_kept = _shift_day(today, -ANALYTICS_RETENTION_DAYS + 1)
    retention = {}
    for n in (1, 7, 30):
        cohort = returned = 0
        for uid, first in first_active.items():
            if first < oldest_kept:
                continue
            days = days_by_user.get(uid, set())
            target = _shift_day(first, n)
            if target < today:                 # день когорта+n полностью завершён
                cohort += 1
                returned += target in days
        retention[n] = (returned, cohort)

    # ── Воронка ──
    cohort_start = {u: t[0] for u, t in firsts["user_started"].items()
                    if _utc_day(t[0]) >= d30}

    def next_step(prev: dict, et: str) -> dict:
        out = {}
        for u, t_prev in prev.items():
            later = [t for t in firsts[et].get(u, []) if t >= t_prev]
            if later:
                out[u] = min(later)
        return out
    s2 = next_step(cohort_start, "connect_started")
    s3 = next_step(s2, "connect_completed")
    s4 = next_step(s3, "first_value")
    recent = sum(1 for t in cohort_start.values() if t >= ts - 7 * 86400)

    return {
        "today": today, "dau": dau, "wau": wau, "mau": mau,
        "new_users_30d": new_users, "total_users": total_users,
        "features": features, "retention": retention,
        "funnel": [len(cohort_start), len(s2), len(s3), len(s4)],
        "funnel_recent": recent, "started_at": analytics_started_at(),
    }


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
