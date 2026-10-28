"""
auth.py — Google OAuth 2.0 flow для мультипользовательского режима.

Схема:
  1. bot вызывает get_auth_url(telegram_id) → возвращает ссылку для пользователя
  2. Пользователь переходит → Google → редирект на /oauth/callback?code=…&state=…
  3. handle_oauth_callback() обменивает code на токены, сохраняет в DB
  4. Присылает пользователю в Telegram сообщение «Подключено!»

Переменные окружения:
  GOOGLE_CLIENT_ID      — OAuth client ID (тип «Веб-приложение»)
  GOOGLE_CLIENT_SECRET  — client secret
  OAUTH_REDIRECT_URI    — https://your-domain.com/oauth/callback
"""

import os
import secrets
import time
import logging
from typing import Optional

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest

import db

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID     = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
OAUTH_REDIRECT_URI   = os.environ["OAUTH_REDIRECT_URI"]

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

CLIENT_CONFIG = {
    "web": {
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uris": [OAUTH_REDIRECT_URI],
        "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
        "token_uri":     "https://oauth2.googleapis.com/token",
    }
}


def get_auth_url(telegram_id: int) -> str:
    """Генерируем ссылку для авторизации Google. Сохраняем state в DB."""
    state = secrets.token_urlsafe(32)
    db.save_oauth_state(state, telegram_id)

    flow = Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
    )
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",          # всегда запрашиваем refresh_token
        state=state,
        include_granted_scopes="true",
    )
    return url


def exchange_code(code: str, state: str) -> Optional[int]:
    """
    Обмениваем auth code на токены.
    Возвращает telegram_id при успехе, None если state невалиден.
    """
    telegram_id = db.pop_oauth_state(state)
    if not telegram_id:
        logger.warning("OAuth: неизвестный или просроченный state=%s", state)
        return None

    flow = Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
        state=state,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials

    expiry_ts = creds.expiry.timestamp() if creds.expiry else (time.time() + 3600)

    db.save_google_tokens(
        telegram_id=telegram_id,
        access_token=creds.token,
        refresh_token=creds.refresh_token,
        expiry=expiry_ts,
    )
    logger.info("OAuth: токены сохранены для telegram_id=%s", telegram_id)
    return telegram_id


def get_credentials(telegram_id: int) -> Optional[Credentials]:
    """
    Возвращаем актуальные Google Credentials для пользователя.
    Автоматически обновляем access token если он истёк.
    """
    user = db.get_user(telegram_id)
    if not user or not user["google_refresh_token"]:
        return None

    creds = Credentials(
        token=user["google_access_token"],
        refresh_token=user["google_refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )

    # Обновляем если истёк (или истекает через < 5 минут)
    if user["google_token_expiry"] and time.time() > user["google_token_expiry"] - 300:
        try:
            creds.refresh(GoogleRequest())
            new_expiry = creds.expiry.timestamp() if creds.expiry else (time.time() + 3600)
            db.update_access_token(telegram_id, creds.token, new_expiry)
            logger.info("OAuth: токен обновлён для telegram_id=%s", telegram_id)
        except Exception as e:
            logger.error("OAuth: ошибка обновления токена telegram_id=%s: %s", telegram_id, e)
            return None

    return creds
