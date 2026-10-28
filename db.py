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
# На Fly.io используем /data (persistent volume), локально — рядом со скриптом
DB_PATH = Path(os.environ.get("DB_PATH", "/data/bot.db"))

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
        """)
        conn.commit()
        conn.close()


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
