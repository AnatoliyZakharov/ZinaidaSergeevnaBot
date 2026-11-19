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

# Granular consent: пользователь может снять галочку Tasks или Календаря.
# Без этого oauthlib бросает Warning("Scope has changed …") внутри fetch_token,
# и callback падает с 500, а токены не сохраняются.  Выданные scopes мы
# проверяем сами (OAuthResult.granted).  Должно быть выставлено до fetch_token;
# oauthlib читает переменную в момент разбора ответа.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import json  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import socket  # noqa: E402
import time  # noqa: E402
import logging  # noqa: E402
from typing import NamedTuple, Optional  # noqa: E402

from google_auth_oauthlib.flow import Flow  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402
from google.auth.transport.requests import Request as GoogleRequest  # noqa: E402
from google.auth import exceptions as google_auth_exceptions  # noqa: E402

import db  # noqa: E402

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID     = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
OAUTH_REDIRECT_URI   = os.environ["OAUTH_REDIRECT_URI"]

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
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


SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar"
SCOPE_TASKS = "https://www.googleapis.com/auth/tasks"


# ─── Классификация ошибок Google ─────────────────────────────

# Категории (единый словарь для gtasks / my_tasks / main):
#   no_auth            — нет сохранённого refresh_token (не подключён)
#   token_invalid      — токен истёк/отозван (invalid_grant, 401)
#   insufficient_scope — при подключении не выдано нужное разрешение
#   api_disabled       — API не включён в GCP-проекте (переподключение не поможет)
#   transient          — временная ошибка (квоты, 5xx, сеть)
#   api_error          — прочие ошибки
class GoogleErr(NamedTuple):
    category: str
    status: Optional[int]
    reason: Optional[str]


_R_DISABLED = frozenset({"accessNotConfigured", "SERVICE_DISABLED"})
_R_SCOPE = frozenset({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})
_R_TRANSIENT = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED",
                          "quotaExceeded", "backendError"})
_R_AUTH = frozenset({"authError", "UNAUTHENTICATED"})
_S_TRANSIENT = frozenset({429, 500, 502, 503, 504})
_REASON_RE = re.compile(r"[A-Za-z_]{1,64}")


def _safe_reason(v) -> Optional[str]:
    """Только короткие идентификаторы: в лог не попадут message/URL/номер проекта."""
    if isinstance(v, str) and _REASON_RE.fullmatch(v):
        return v
    return None


def _http_reasons(content) -> list:
    """reasons из тела ответа Google: error.errors[].reason ∪ error.details[].reason
    ∪ error.status.  Не JSON / неожиданная структура → []."""
    try:
        if isinstance(content, (bytes, bytearray)):
            content = bytes(content).decode("utf-8", errors="replace")
        data = json.loads(content) if isinstance(content, str) and content else {}
    except Exception:
        data = {}
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return []
    raw = []
    for key in ("errors", "details"):
        items = err.get(key)
        if isinstance(items, list):
            raw.extend(i.get("reason") for i in items if isinstance(i, dict))
    raw.append(err.get("status"))
    out = []
    for v in raw:
        r = _safe_reason(v)
        if r and r not in out:
            out.append(r)
    return out


def _http_status(exc) -> Optional[int]:
    resp = getattr(exc, "resp", None)
    if resp is None:
        return None
    try:
        return int(getattr(resp, "status", None))
    except (TypeError, ValueError):
        return None


def _pick(reasons, group) -> Optional[str]:
    return next((r for r in reasons if r in group), None)


def classify_google_error(exc) -> GoogleErr:
    """Единственный классификатор ошибок Google (HttpError, RefreshError, сеть).

    HttpError распознаётся по атрибутам resp.status / content (без импорта
    googleapiclient).  Решение — по reason, затем по статусу: 403 бывает и
    «API выключен», и «нет scope», и rate limit."""
    if isinstance(exc, google_auth_exceptions.RefreshError):
        args = getattr(exc, "args", ())
        info = args[1] if len(args) > 1 else None
        if isinstance(info, dict):
            code = _safe_reason(info.get("error"))
            if code == "invalid_grant":
                return GoogleErr("token_invalid", None, code)
            if getattr(exc, "retryable", False):
                return GoogleErr("transient", None, code)
            return GoogleErr("api_error", None, code)
        return GoogleErr("transient", None, None)
    if isinstance(exc, google_auth_exceptions.TransportError):
        return GoogleErr("transient", None, None)

    status = _http_status(exc)
    if status is not None:
        reasons = _http_reasons(getattr(exc, "content", None))
        r = _pick(reasons, _R_DISABLED)
        if r:
            return GoogleErr("api_disabled", status, r)
        r = _pick(reasons, _R_SCOPE)
        if r:
            return GoogleErr("insufficient_scope", status, r)
        r = _pick(reasons, _R_TRANSIENT)
        if r or status in _S_TRANSIENT:
            return GoogleErr("transient", status, r or (reasons[0] if reasons else None))
        r = _pick(reasons, _R_AUTH)
        if status == 401 or r:
            return GoogleErr("token_invalid", status, r or (reasons[0] if reasons else None))
        return GoogleErr("api_error", status, reasons[0] if reasons else None)

    if isinstance(exc, (OSError, socket.timeout, TimeoutError)):
        return GoogleErr("transient", None, None)
    if type(exc).__module__.split(".")[0] == "httplib2":
        return GoogleErr("transient", None, None)
    return GoogleErr("api_error", None, None)


def log_google_error(where: str, _telegram_id, err: GoogleErr) -> None:
    """Пишет в лог только категорию, HTTP-статус и reason (без message, URL,
    номера проекта, токенов и email)."""
    logger.warning("Google API error: where=%s category=%s status=%s reason=%s",
                   where, err.category, err.status, err.reason)


# ─── OAuth callback ──────────────────────────────────────────

class OAuthResult(NamedTuple):
    telegram_id: int
    granted: Optional[frozenset]     # выданные scopes; None — Google их не сообщил


def _granted_scopes(creds) -> Optional[frozenset]:
    raw = getattr(creds, "granted_scopes", None)
    if isinstance(raw, str):
        raw = raw.split()
    if not raw:
        return None
    try:
        return frozenset(str(s) for s in raw)
    except TypeError:
        return None


def exchange_code(code: str, state: str) -> Optional[OAuthResult]:
    """
    Обмениваем auth code на токены.
    Возвращает OAuthResult(telegram_id, granted) при успехе, None если state
    невалиден.  Ошибки обмена (fetch_token) пробрасываются вызывающему.
    """
    telegram_id = db.pop_oauth_state(state)
    if not telegram_id:
        logger.warning("OAuth: код отклонён из-за недействительной сессии")
        return None

    flow = Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
        state=state,
    )
    flow.fetch_token(code=code, timeout=15)
    creds = flow.credentials

    expiry_ts = creds.expiry.timestamp() if creds.expiry else (time.time() + 3600)

    if creds.refresh_token:
        db.save_google_tokens(
            telegram_id=telegram_id,
            access_token=creds.token,
            refresh_token=creds.refresh_token,
            expiry=expiry_ts,
        )
    else:
        # Google не прислал refresh_token — сохранённый не затираем (иначе
        # пользователь «отключится»); обновляем только access token.
        db.update_access_token(telegram_id, creds.token, expiry_ts)
    granted = _granted_scopes(creds)
    logger.info("OAuth: токены сохранены (calendar=%s tasks=%s)",
                None if granted is None else SCOPE_CALENDAR in granted,
                None if granted is None else SCOPE_TASKS in granted)
    return OAuthResult(telegram_id, granted)


# ─── Credentials ─────────────────────────────────────────────

def get_credentials_checked(telegram_id: int):
    """
    Возвращает (Credentials | None, reason | None).
    reason: no_auth — нет refresh_token; иначе — категория classify_google_error
    при ошибке обновления access token.  Токены в БД при ошибке не стираются.
    """
    user = db.get_user(telegram_id)
    if not user or not user["google_refresh_token"]:
        return None, "no_auth"

    # Без scopes=: refresh возвращает токен с фактически выданными scopes.
    # Если запросить все SCOPES, а Tasks не выдан, refresh падает — и ломается
    # в том числе Календарь.
    creds = Credentials(
        token=user["google_access_token"],
        refresh_token=user["google_refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
    )

    # Обновляем если истёк (или истекает через < 5 минут)
    if user["google_token_expiry"] and time.time() > user["google_token_expiry"] - 300:
        try:
            creds.refresh(GoogleRequest())
            new_expiry = creds.expiry.timestamp() if creds.expiry else (time.time() + 3600)
            db.update_access_token(telegram_id, creds.token, new_expiry)
            logger.info("OAuth: токен обновлён")
        except Exception as e:
            err = classify_google_error(e)
            log_google_error("refresh", telegram_id, err)
            return None, err.category

    return creds, None


def get_credentials(telegram_id: int) -> Optional[Credentials]:
    """
    Возвращаем актуальные Google Credentials для пользователя (или None).
    Автоматически обновляем access token если он истёк.
    """
    return get_credentials_checked(telegram_id)[0]
