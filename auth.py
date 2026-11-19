"""
auth.py — Google OAuth 2.0 flow для мультипользовательского режима.

Google returns an authorization code to the browser callback. A short-lived
candidate is kept in process memory until the originating Telegram user explicitly
confirms it in the originating chat.

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

from datetime import datetime, timezone  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import logging  # noqa: E402
from typing import NamedTuple, Optional  # noqa: E402

from google_auth_oauthlib.flow import Flow  # noqa: E402
from google.oauth2 import id_token  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402
from google.auth.transport.requests import Request as GoogleRequest  # noqa: E402
from google.auth import exceptions as google_auth_exceptions  # noqa: E402

import db  # noqa: E402

logger = logging.getLogger(__name__)
# oauthlib's debug output includes the token exchange request and response.
logging.getLogger("oauthlib").setLevel(logging.WARNING)
logging.getLogger("requests_oauthlib").setLevel(logging.WARNING)

GOOGLE_CLIENT_ID     = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
OAUTH_REDIRECT_URI   = os.environ["OAUTH_REDIRECT_URI"]

SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar"
SCOPE_TASKS = "https://www.googleapis.com/auth/tasks"
SCOPE_DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"

SCOPES = [
    SCOPE_CALENDAR,
    SCOPE_TASKS,
    SCOPE_DRIVE_FILE,
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


_LOGIN_TTL = 10 * 60
_CONFIRM_TTL = 5 * 60
_MAX_LOGIN_ATTEMPTS = 128
_RANDOM_URLSAFE = re.compile(r"[A-Za-z0-9_-]{43}")
_login_lock = threading.Lock()


class OAuthResult(NamedTuple):
    """Only browser-visible data leaves the staging store."""

    email: str
    confirmation_code: str


class _CandidateGrant(NamedTuple):
    access_token: str
    refresh_token: str
    expiry: float
    subject: str
    email: str
    granted: Optional[frozenset]
    confirmation_digest: bytes


class _LoginAttempt(NamedTuple):
    state: str
    verifier: str
    nonce: str
    deadline: float
    exchanged: bool = False
    candidate: Optional[_CandidateGrant] = None
    reviewed_in_telegram: bool = False


_logins: dict[tuple[int, int], _LoginAttempt] = {}


def _prune_logins(now: float) -> None:
    for key, entry in tuple(_logins.items()):
        if entry.deadline <= now:
            del _logins[key]


def get_auth_url(telegram_id: int, chat_id: int) -> str:
    """Start Google consent with a fresh state, PKCE verifier and OpenID nonce."""
    attempt = _LoginAttempt(
        secrets.token_urlsafe(32), secrets.token_urlsafe(48), secrets.token_urlsafe(32),
        time.monotonic() + _LOGIN_TTL,
    )
    flow = Flow.from_client_config(
        CLIENT_CONFIG, scopes=SCOPES, redirect_uri=OAUTH_REDIRECT_URI, code_verifier=attempt.verifier,
        autogenerate_code_verifier=False,
    )
    url, _ = flow.authorization_url(
        access_type="offline", prompt="consent select_account", state=attempt.state, nonce=attempt.nonce,
    )
    key = telegram_id, chat_id
    with _login_lock:
        _prune_logins(time.monotonic())
        _logins.pop(key, None)
        if len(_logins) >= _MAX_LOGIN_ATTEMPTS:
            del _logins[next(iter(_logins))]
        _logins[key] = attempt
    return url


def _consume_state(state: str):
    if not isinstance(state, str) or not _RANDOM_URLSAFE.fullmatch(state):
        return None
    with _login_lock:
        _prune_logins(time.monotonic())
        for key, attempt in _logins.items():
            if not attempt.exchanged and secrets.compare_digest(attempt.state, state):
                _logins[key] = attempt._replace(exchanged=True)
                return key, attempt
    return None


def discard_login_state(state: str) -> None:
    """Make a denied state unusable without affecting already exchanged grants."""
    _consume_state(state)


def cancel_google_link(telegram_id: int, chat_id: int) -> None:
    with _login_lock:
        _logins.pop((telegram_id, chat_id), None)


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


def _expiry_timestamp(creds) -> float:
    expiry = creds.expiry
    if expiry is None:
        return time.time() + 3600
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry.timestamp()


def _verify_identity(token: str, nonce: str) -> tuple[str, str]:
    google_request = GoogleRequest()

    def bounded_request(url, **kwargs):
        return google_request(url, timeout=15, **kwargs)

    claims = id_token.verify_oauth2_token(token, bounded_request, audience=GOOGLE_CLIENT_ID)
    subject = claims.get("sub")
    email = claims.get("email")
    if (claims.get("nonce") != nonce or claims.get("azp", GOOGLE_CLIENT_ID) != GOOGLE_CLIENT_ID
            or not isinstance(subject, str) or not subject or len(subject) > 255
            or claims.get("email_verified") is not True or not isinstance(email, str)
            or not re.fullmatch(r"[^@\s<>]{1,100}@[^@\s<>]{1,150}", email)):
        raise ValueError("Google account identity could not be confirmed")
    return subject, email


def exchange_code(code: str, state: str) -> Optional[OAuthResult]:
    """Consume the state before Google I/O and stage a complete verified grant."""
    if not isinstance(code, str) or not 0 < len(code) <= 8192:
        return None
    consumed = _consume_state(state)
    if not consumed:
        return None
    key, attempt = consumed
    flow = Flow.from_client_config(
        CLIENT_CONFIG, scopes=SCOPES, redirect_uri=OAUTH_REDIRECT_URI, state=state,
        code_verifier=attempt.verifier, autogenerate_code_verifier=False,
    )
    flow.fetch_token(code=code, timeout=15)
    # Read the token response first: Flow.credentials requires a new refresh token.
    token_response = flow.oauth2session.token
    identity_token = token_response.get("id_token")
    if not identity_token or not token_response.get("refresh_token") or not token_response.get("access_token"):
        raise ValueError("Google did not return a complete authorization")
    creds = flow.credentials
    subject, email = _verify_identity(identity_token, attempt.nonce)
    confirmation_code = secrets.token_urlsafe(32)
    candidate = _CandidateGrant(
        creds.token, creds.refresh_token, _expiry_timestamp(creds), subject, email,
        _granted_scopes(creds), hashlib.sha256(confirmation_code.encode("ascii")).digest(),
    )
    with _login_lock:
        now = time.monotonic()
        _prune_logins(now)
        current = _logins.get(key)
        if not current or current.state != attempt.state:
            return None
        _logins[key] = current._replace(deadline=now + _CONFIRM_TTL, candidate=candidate)
    return OAuthResult(email, confirmation_code)


def _confirmation_digest(code: str) -> Optional[bytes]:
    if not isinstance(code, str) or not _RANDOM_URLSAFE.fullmatch(code):
        return None
    return hashlib.sha256(code.encode("ascii")).digest()


def begin_google_confirmation(telegram_id: int, chat_id: int, code: str) -> Optional[OAuthResult]:
    """Spend the browser code and issue a one-time approval token for Telegram."""
    digest = _confirmation_digest(code)
    if digest is None:
        return None
    key = telegram_id, chat_id
    with _login_lock:
        _prune_logins(time.monotonic())
        attempt = _logins.get(key)
        candidate = attempt.candidate if attempt and not attempt.reviewed_in_telegram else None
        if not candidate or not secrets.compare_digest(candidate.confirmation_digest, digest):
            return None
        approval = secrets.token_urlsafe(32)
        candidate = candidate._replace(confirmation_digest=_confirmation_digest(approval))
        _logins[key] = attempt._replace(candidate=candidate, reviewed_in_telegram=True)
        return OAuthResult(candidate.email, approval)


def confirm_google_link(telegram_id: int, chat_id: int, code: str) -> Optional[tuple[str, Optional[frozenset]]]:
    """Promote only after the Telegram user explicitly approves the displayed account."""
    digest = _confirmation_digest(code)
    if digest is None:
        return None
    key = telegram_id, chat_id
    with _login_lock:
        _prune_logins(time.monotonic())
        attempt = _logins.get(key)
        candidate = attempt.candidate if attempt and attempt.reviewed_in_telegram else None
        if not candidate or not secrets.compare_digest(candidate.confirmation_digest, digest):
            return None
        db.save_google_grant(
            telegram_id, access_token=candidate.access_token, refresh_token=candidate.refresh_token,
            expiry=candidate.expiry, subject=candidate.subject, granted=candidate.granted,
        )
        del _logins[key]
        return candidate.email, candidate.granted


# ─── Credentials ─────────────────────────────────────────────

def _credentials_and_grant(telegram_id: int, *, verified: bool):
    try:
        grant = db.get_google_grant(telegram_id)
    except Exception as exc:
        logger.warning("Google grant read failed: %s", type(exc).__name__)
        return None, None, "no_auth"
    if not grant or not grant["refresh_token"] or (verified and (not grant["subject"] or not grant["version"])):
        return None, None, "no_auth"

    creds = Credentials(
        token=grant["access_token"], refresh_token=grant["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID, client_secret=GOOGLE_CLIENT_SECRET,
    )
    if not grant["expiry"] or time.time() > grant["expiry"] - 300:
        try:
            creds.refresh(GoogleRequest())
            scope_report = _granted_scopes(creds)
            granted = grant["granted"]
            if scope_report is not None and granted is not None:
                granted = frozenset(granted) & scope_report
            expiry = _expiry_timestamp(creds)
            refresh = creds.refresh_token or grant["refresh_token"]
            saved = db.refresh_google_grant(
                telegram_id, grant["version"], access_token=creds.token, refresh_token=refresh,
                expiry=expiry, expected_refresh_token=grant["refresh_token"], granted=granted,
            )
            if not saved:
                return None, None, "transient"
            grant = dict(grant, access_token=creds.token, refresh_token=refresh, expiry=expiry, granted=granted)
        except Exception as exc:
            err = classify_google_error(exc)
            log_google_error("refresh", telegram_id, err)
            return None, None, err.category
    return creds, grant, None


def get_credentials_and_grant_checked(telegram_id: int):
    """Return (credentials, confirmed grant, reason) for protected user operations."""
    return _credentials_and_grant(telegram_id, verified=True)


def get_credentials_checked(telegram_id: int):
    """Return credentials and status for existing Calendar and Tasks callers."""
    creds, _, reason = _credentials_and_grant(telegram_id, verified=False)
    return creds, reason


def get_credentials(telegram_id: int) -> Optional[Credentials]:
    return get_credentials_checked(telegram_id)[0]
