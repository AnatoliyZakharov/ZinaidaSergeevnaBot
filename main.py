"""
ZinaidaSergeevnaBot — мультипользовательский голосовой агент для Google Календаря
==================================================================================

Переменные окружения:
  TELEGRAM_TOKEN        — токен бота от BotFather
  YANDEX_API_KEY        — API-ключ Яндекс Cloud (SpeechKit)
  YANDEX_FOLDER_ID      — ID папки в Яндекс Cloud
  YANDEX_TELEMOST_TOKEN — OAuth-токен Телемост (общий, если нет per-user)
  GOOGLE_CLIENT_ID      — OAuth client ID (тип «Веб-приложение»)
  GOOGLE_CLIENT_SECRET  — OAuth client secret
  OAUTH_REDIRECT_URI    — https://your-domain.com/oauth/callback
  TIMEZONE              — часовой пояс по умолчанию (Europe/Moscow)
  WORK_START            — начало рабочего дня по умолчанию (9)
  WORK_END              — конец рабочего дня по умолчанию (19)
  WEBHOOK_HOST          — публичный домен для Telegram webhook (если используется)
  GOOGLE_SA_KEY         — JSON сервисного аккаунта для записи в Google Sheets (MYmeet)
"""

import asyncio
import html as html_lib
import os
import json
import logging
import re
import signal
from datetime import datetime, time, timedelta, timezone
import time as time_module
from typing import Optional

import pytz
import requests
import dateparser
from dateparser.search import search_dates

from aiohttp import web

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler, TypeHandler,
)
from telegram.request import HTTPXRequest

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import db
import gtasks
import auth
import my_tasks
import crypto as _crypto
from crypto import CryptoDecryptError
import mymeet_sync
from mymeet_sync import sync_to_sheets_async, verify_token_and_get_workspace, MymeetTokenError

# ─── Логирование ─────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
logger = logging.getLogger(__name__)

# HTTP-клиенты на INFO пишут полный URL запроса, а в URL Telegram Bot API
# входит токен бота (#33).  Их INFO не нужен — только предупреждения и ошибки.
_QUIET_HTTP_LOGGERS = ("httpx", "httpcore")
# Токен бота: <id бота>:<секрет 35 символов>; отдельно или внутри URL
# («…/bot<id>:<секрет>/…» — перед цифрами буква, поэтому без \b)
_BOT_TOKEN_RE = re.compile(r"(?:(?<=bot)|(?<![A-Za-z0-9_-]))\d{6,}:[A-Za-z0-9_-]{30,}")


class _RedactingFormatter(logging.Formatter):
    """Маскирует секреты в готовой строке лога — в сообщении, аргументах и
    traceback (исключения сети могут содержать URL с токеном)."""

    def __init__(self, fmt: str, secrets=()):
        super().__init__(fmt)
        # Короткие значения не маскируем: слишком велик риск задеть обычный текст
        self._secrets = sorted({s for s in secrets if s and len(s) >= 8}, key=len,
                               reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return _BOT_TOKEN_RE.sub("<redacted>", text)


def _configure_log_security(*secrets) -> None:
    """INFO HTTP-клиентов выключен; секреты маскируются во всех обработчиках
    корневого логгера.  Повторный вызов безопасен."""
    for name in _QUIET_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    formatter = _RedactingFormatter(LOG_FORMAT, secrets)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)

# ─── Конфиг ──────────────────────────────────────────────────
TELEGRAM_TOKEN        = os.environ["TELEGRAM_TOKEN"]
YANDEX_API_KEY        = os.environ["YANDEX_API_KEY"]
YANDEX_FOLDER         = os.environ["YANDEX_FOLDER_ID"]
YANDEX_TELEMOST_TOKEN = os.environ.get("YANDEX_TELEMOST_TOKEN", "")
_configure_log_security(TELEGRAM_TOKEN, YANDEX_API_KEY, YANDEX_TELEMOST_TOKEN,
                        os.environ.get("GOOGLE_CLIENT_SECRET", ""))
DEFAULT_TZ            = os.environ.get("TIMEZONE", "Europe/Moscow")


def _parse_admin_id(raw: str | None) -> int | None:
    """Telegram user ID администратора (только число; username не используется)."""
    try:
        v = int(str(raw or "").strip())
        return v if v > 0 else None
    except ValueError:
        return None


ADMIN_TELEGRAM_ID = _parse_admin_id(os.environ.get("ADMIN_TELEGRAM_ID"))
DEFAULT_WORK_START    = int(os.environ.get("WORK_START", "9"))
DEFAULT_WORK_END      = int(os.environ.get("WORK_END", "19"))
WEBHOOK_HOST          = os.environ.get("WEBHOOK_HOST", "")   # напр. https://bot.example.com
OAUTH_CALLBACK_PORT   = int(os.environ.get("OAUTH_CALLBACK_PORT", "8080"))

SCOPES = ["https://www.googleapis.com/auth/calendar"]

CONFERENCE_PATTERNS = re.compile(
    r"(zoom\.us|teams\.microsoft\.com|webex\.com|whereby\.com|"
    r"gotomeeting\.com|bluejeans\.com|meet\.jit\.si|"
    r"telemost\.yandex\.(?:ru|com|net)|telemost\.360\.yandex\.(?:ru|com))",
    re.IGNORECASE,
)

TELEMOST_TRIGGER = re.compile(
    r"(добав|вставь|поставь|прикреп).{0,20}(телемост|ссылк|конференц)|"
    r"(телемост|ссылк).{0,20}(добав|вставь|поставь)|"
    r"^добавь ссылки$",
    re.IGNORECASE,
)
CREATE_TRIGGER = re.compile(
    r"(поставь|добавь|создай|запланируй|поставить|добавить|создать|запланировать)"
    r".{0,50}(встреч|событи|звонок|созвон|митинг|колл)",
    re.IGNORECASE,
)
TIME_IN_TEXT  = re.compile(r"\d{1,2}[:.]\d{2}|\bв\s+\d{1,2}\s*(час|утра|дня|вечера|ночи)?\b", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
INVITE_SKIP   = re.compile(r"\b(готово|пропустить|skip|нет|не надо|позже|стоп|без приглашений)\b", re.IGNORECASE)

# Глобальный бот (инициализируется в run_bot)
_bot: Optional[Bot] = None


# ─── Helpers ─────────────────────────────────────────────────

def _tz(user_row) -> pytz.timezone:
    return pytz.timezone(user_row["timezone"] or DEFAULT_TZ)


def _work_start(user_row) -> int:
    value = user_row["work_start"]
    return DEFAULT_WORK_START if value is None else value


def _work_end(user_row) -> int:
    return user_row["work_end"] or DEFAULT_WORK_END


def get_calendar_service(telegram_id: int):
    creds = auth.get_credentials(telegram_id)
    if not creds:
        raise RuntimeError(f"Нет Google credentials для пользователя {telegram_id}")
    return build("calendar", "v3", credentials=creds)


def _get_calendar_id(user_row) -> str:
    return user_row["calendar_id"] or "primary"


# ─── Google Calendar ─────────────────────────────────────────

def _local_work_boundary(tz, day, hour, *, end=False):
    if hour == 24:
        day += timedelta(days=1)
        hour = 0
    wall_time = datetime.combine(day, time(hour))
    try:
        return tz.localize(wall_time, is_dst=None)
    except pytz.AmbiguousTimeError:
        choices = [tz.localize(wall_time, is_dst=value) for value in (False, True)]
        return (max if end else min)(choices)
    except pytz.NonExistentTimeError:
        # If the clock skips the boundary, move forward by the offset change.
        return max(tz.normalize(tz.localize(wall_time, is_dst=value))
                   for value in (False, True))


def get_free_slots(telegram_id: int, target_date: datetime) -> list[str]:
    user = db.get_user(telegram_id)
    tz   = _tz(user)
    calendar_id = _get_calendar_id(user)
    service = get_calendar_service(telegram_id)

    day = target_date.astimezone(tz).date() if target_date.tzinfo else target_date.date()
    work_start = _local_work_boundary(tz, day, _work_start(user))
    work_end = _local_work_boundary(tz, day, _work_end(user), end=True)
    if work_end <= work_start:
        return []

    body = {
        "timeMin": work_start.isoformat(),
        "timeMax": work_end.isoformat(),
        "items":   [{"id": calendar_id}],
        "timeZone": tz.zone,
    }
    result = service.freebusy().query(body=body).execute()
    busy_raw = result["calendars"][calendar_id]["busy"]

    busy = []
    for p in busy_raw:
        s = datetime.fromisoformat(p["start"].replace("Z", "+00:00")).astimezone(tz)
        e = datetime.fromisoformat(p["end"].replace("Z", "+00:00")).astimezone(tz)
        if max(s, work_start) < min(e, work_end):
            busy.append((max(s, work_start), min(e, work_end)))

    free, cur = [], work_start
    for b_start, b_end in sorted(busy):
        if cur < b_start:
            mins = int((b_start - cur).total_seconds() / 60)
            if mins >= 30:
                free.append(_fmt_slot(cur, b_start, mins))
        cur = max(cur, b_end)
    if cur < work_end:
        mins = int((work_end - cur).total_seconds() / 60)
        if mins >= 30:
            free.append(_fmt_slot(cur, work_end, mins))
    return free


def _fmt_slot(start, end, mins: int) -> str:
    h, m = divmod(mins, 60)
    dur = f"{h}ч {m}м" if h and m else (f"{h}ч" if h else f"{m}м")
    return f"{start.strftime('%H:%M')} — {end.strftime('%H:%M')}  ({dur})"


def get_events_next_7_days(telegram_id: int) -> list[dict]:
    user = db.get_user(telegram_id)
    tz   = _tz(user)
    calendar_id = _get_calendar_id(user)
    now = datetime.now(tz)
    service = get_calendar_service(telegram_id)
    result = service.events().list(
        calendarId=calendar_id,
        timeMin=now.isoformat(),
        timeMax=(now + timedelta(days=7)).isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=100,
    ).execute()
    return result.get("items", [])


def _telemost_line(link) -> str:
    """Строка описания события со ссылкой Телемоста.

    Calendar API допускает HTML в описании. Явная ссылка снижает риск,
    что клиент распознает номер конференции как телефон. Видимый URL также
    можно скопировать в клиентах без поддержки HTML.
    Только для URL из allowlist Телемоста (https, хост Телемоста, путь /j/цифры);
    иначе — прежний текстовый формат."""
    import html
    import re as _re
    from urllib.parse import urlsplit
    plain = f"🔗 Яндекс Телемост: {link}"
    hosts = ("telemost.yandex.ru", "telemost.yandex.com", "telemost.yandex.net",
             "telemost.360.yandex.ru", "telemost.360.yandex.com")
    url = link if isinstance(link, str) else ""
    try:
        u = urlsplit(url)
    except ValueError:
        return plain
    if (u.scheme != "https" or u.netloc not in hosts or u.query or u.fragment
            or not _re.fullmatch(r"/j/[0-9]+", u.path)
            or url != f"https://{u.netloc}{u.path}"):
        return plain
    safe = html.escape(url, quote=True)
    return f'🔗 Яндекс Телемост: <a href="{safe}">{safe}</a>'


def create_calendar_event(
    telegram_id: int,
    title: str,
    start_dt: datetime,
    duration_minutes: int = 60,
    telemost_link: Optional[str] = None,
) -> dict:
    user = db.get_user(telegram_id)
    tz   = _tz(user)
    calendar_id = _get_calendar_id(user)
    service = get_calendar_service(telegram_id)

    if start_dt.tzinfo is None:
        start_dt = tz.localize(start_dt)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    body = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": tz.zone},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": tz.zone},
    }
    if telemost_link:
        body["description"] = _telemost_line(telemost_link)
    return service.events().insert(calendarId=calendar_id, body=body).execute()


def add_attendees_to_event(telegram_id: int, event_id: str, emails: list[str]) -> None:
    user = db.get_user(telegram_id)
    calendar_id = _get_calendar_id(user)
    service = get_calendar_service(telegram_id)
    event    = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    existing = {a["email"] for a in event.get("attendees", [])}
    merged   = list(event.get("attendees", [])) + [{"email": e} for e in emails if e not in existing]
    service.events().patch(
        calendarId=calendar_id,
        eventId=event_id,
        body={"attendees": merged},
        sendUpdates="all",
    ).execute()


def add_telemost_to_event(telegram_id: int, event: dict, link: str) -> bool:
    """Дописывает ссылку Телемоста в описание события.

    `event` должен быть СВЕЖИМ (только что прочитанным через events().get),
    чтобы не затереть изменения описания, сделанные после events().list.
    Патчится только поле description; приглашения участникам не рассылаются.
    """
    event_id = event.get("id") if isinstance(event, dict) else None
    try:
        user = db.get_user(telegram_id)
        calendar_id = _get_calendar_id(user)
        service = get_calendar_service(telegram_id)
        old_desc = event.get("description") or ""
        line = _telemost_line(link)
        new_desc = f"{old_desc}\n\n{line}" if old_desc else line
        service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body={"description": new_desc},
        ).execute()
        return True
    except Exception as e:
        logger.error("Ошибка обновления события: %s", type(e).__name__)
        return False


# ─── Яндекс SpeechKit ────────────────────────────────────────

def transcribe(audio_bytes: bytes) -> str:
    resp = requests.post(
        "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize",
        headers={"Authorization": f"Api-Key {YANDEX_API_KEY}"},
        params={"lang": "ru-RU", "format": "oggopus", "folderId": YANDEX_FOLDER},
        data=audio_bytes,
        timeout=15,
    )
    if resp.status_code == 200:
        return resp.json().get("result", "")
    logger.error("SpeechKit HTTP %s", resp.status_code)
    return ""


# ─── Яндекс Телемост ─────────────────────────────────────────

def create_telemost_conference(telegram_id: Optional[int] = None) -> Optional[str]:
    """Создаём конференцию. Используем per-user токен если есть, иначе общий."""
    token = None
    if telegram_id:
        user = db.get_user(telegram_id)
        if user:
            token = user["telemost_token"]
    if not token:
        token = YANDEX_TELEMOST_TOKEN
    if not token:
        return None

    try:
        resp = requests.post(
            "https://cloud-api.yandex.net/v1/telemost-api/conferences",
            headers={"Authorization": f"OAuth {token}", "Content-Type": "application/json"},
            json={},
            timeout=15,
        )
    except Exception as e:
        logger.error("Телемост: %s", type(e).__name__)
        return None

    if resp.status_code in (200, 201):
        data = resp.json()
        return data.get("join_url") or data.get("conferenceUri") or data.get("uri")
    logger.error("Телемост API HTTP %s", resp.status_code)
    return None


def has_conference_link(event: dict) -> bool:
    text = " ".join([event.get("description") or "", event.get("location") or ""])
    if CONFERENCE_PATTERNS.search(text):
        return True
    for ep in (event.get("conferenceData") or {}).get("entryPoints") or []:
        if CONFERENCE_PATTERNS.search(ep.get("uri") or ""):
            return True
    return False


def _is_event_organizer(event: dict) -> Optional[bool]:
    """Пользователь — организатор события по данным Google Calendar?

    True  — organizer.self is True;
    False — организатор указан, но это не пользователь;
    None  — организатор не определён (поле отсутствует/пустое/не dict).
    Поле creator намеренно не используется.
    """
    if not isinstance(event, dict):
        return None
    organizer = event.get("organizer")
    if not organizer or not isinstance(organizer, dict):
        return None
    return organizer.get("self") is True


def _fetch_fresh_event(service, calendar_id: str, event_id: str) -> dict:
    """Перечитывает событие непосредственно перед изменением."""
    return service.events().get(calendarId=calendar_id, eventId=event_id).execute()


def process_telemost_links(telegram_id: int) -> dict:
    result = {
        "added": [],
        "skipped": 0,
        "skipped_foreign": 0,
        "skipped_no_organizer": 0,
        "errors": [],
    }
    try:
        events = get_events_next_7_days(telegram_id)
    except Exception as e:
        logger.error("process_telemost_links: не удалось получить события: %s", type(e).__name__)
        result["errors"].append("Не удалось получить события из Google Calendar")
        return result

    service = None
    calendar_id = None
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("status") == "cancelled":
            continue
        if "date" in event.get("start", {}) and "dateTime" not in event.get("start", {}):
            result["skipped"] += 1
            continue

        # Организатор проверяется ДО любых обращений к Телемосту.
        is_org = _is_event_organizer(event)
        if is_org is None:
            result["skipped_no_organizer"] += 1
            continue
        if is_org is False:
            result["skipped_foreign"] += 1
            continue

        if has_conference_link(event):
            result["skipped"] += 1
            continue

        title = event.get("summary") or "(без названия)"
        event_id = event.get("id")
        if not event_id:
            result["skipped"] += 1
            continue

        # Перечитываем событие: между list и patch его могли изменить.
        try:
            if service is None:
                user = db.get_user(telegram_id)
                calendar_id = _get_calendar_id(user)
                service = get_calendar_service(telegram_id)
            fresh = _fetch_fresh_event(service, calendar_id, event_id)
        except Exception as e:
            logger.error("process_telemost_links: не удалось перечитать событие: %s", type(e).__name__)
            result["errors"].append(f"Не удалось перечитать «{title}» из Google Calendar")
            continue

        if (not isinstance(fresh, dict)
                or fresh.get("status") == "cancelled"
                or _is_event_organizer(fresh) is not True
                or has_conference_link(fresh)):
            result["skipped"] += 1
            continue

        link = create_telemost_conference(telegram_id)
        if not link:
            result["errors"].append(f"Телемост не создался для «{title}»")
            continue

        if add_telemost_to_event(telegram_id, fresh, link):
            result["added"].append({"title": fresh.get("summary") or title, "link": link})
        else:
            result["errors"].append(f"Не удалось обновить «{title}»")
    return result


def format_telemost_report(res: dict) -> str:
    lines = []
    if res["added"]:
        lines.append(f"✅ Добавлено ссылок: {len(res['added'])}")
        for item in res["added"]:
            lines.append(f"  • {item['title']}")
    else:
        lines.append("ℹ️ Новых встреч без ссылок не найдено.")
    if res.get("skipped", 0):
        lines.append(f"⏭ Пропущено: {res['skipped']}")
    if res.get("skipped_foreign", 0):
        lines.append(f"⏭ Чужие встречи (не трогаю): {res['skipped_foreign']}")
    if res.get("skipped_no_organizer", 0):
        lines.append(f"⏭ Организатор не определён: {res['skipped_no_organizer']}")
    if res["errors"]:
        lines.append("⚠️ Ошибки:")
        for e in res["errors"]:
            lines.append(f"  • {e}")
    return "\n".join(lines)


# ─── Разбор даты ─────────────────────────────────────────────

def parse_date(text: str, tz_name: str = DEFAULT_TZ) -> Optional[datetime]:
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)
    settings = {
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": now,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": tz_name,
    }
    result = dateparser.parse(text, languages=["ru"], settings=settings)
    if result:
        return result
    found = search_dates(text, languages=["ru"], settings=settings)
    return found[0][1] if found else None


def date_to_ru(dt: datetime) -> str:
    months = ["января","февраля","марта","апреля","мая","июня",
              "июля","августа","сентября","октября","ноября","декабря"]
    days   = ["пн","вт","ср","чт","пт","сб","вс"]
    return f"{dt.day} {months[dt.month - 1]} ({days[dt.weekday()]})"


# ─── Парсинг текста ──────────────────────────────────────────

def extract_emails(text: str) -> list[str]:
    return EMAIL_PATTERN.findall(text)


def parse_duration_minutes(text: str) -> int:
    tl = text.lower()
    for pattern, mult in [
        (r"на\s+(\d+)\s*(минут|мин)\b", 1),
        (r"(\d+)\s*(минут|мин)\b", 1),
        (r"на\s+(\d+)\s*час", 60),
    ]:
        m = re.search(pattern, tl)
        if m:
            return int(m.group(1)) * mult
    if re.search(r"полчаса|пол\s*часа", tl): return 30
    if re.search(r"\bна\s+час\b", tl):        return 60
    return 60


def extract_event_title(text: str) -> str:
    t = text
    t = re.sub(r"(поставь|добавь|создай|запланируй|поставить|добавить|создать|запланировать)\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(встреч[уаи]?|событи[ея]|созвон|звонок|митинг|колл)\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\bна\s+\d+\s*(минут|мин|час[аов]?)\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\d+\s*(минут|мин)\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\bна\s+(один\s+)?час\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"полчаса|пол\s*часа", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\b((в|на)\s+)?(понедельник|вторник|среду?|четверг|пятниц[уа]|субботу?|воскресенье)\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(сегодня|завтра|послезавтра)\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(в|на)\s+\d{1,2}[:.]\d{2}\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(в|на)\s+\d{1,2}\s*(час[аов]?|утра|дня|вечера|ночи)?\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\d+\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip(" ,.—-")
    return t if len(t) > 1 else "Встреча"


# ─── Проверка регистрации ─────────────────────────────────────

async def _require_google(update: Update) -> bool:
    """Проверяем, подключён ли Google. Если нет — предлагаем подключить."""
    telegram_id = update.effective_user.id
    if db.is_google_connected(telegram_id):
        return True
    await update.message.reply_text(
        "⚠️ Google Календарь не подключён.\n\n"
        "Нажми /connect чтобы авторизоваться через Google.",
    )
    return False


# ─── Обработка запросов ──────────────────────────────────────

async def process_query(telegram_id: int, text: str, update: Update):
    user = db.get_user(telegram_id)
    tz_name = user["timezone"] if user else DEFAULT_TZ
    target = parse_date(text, tz_name)
    if not target:
        await update.message.reply_text(
            "Не понял дату 🤔\n\nПопробуй:\n"
            "• «свободные слоты на завтра»\n"
            "• «что свободно в пятницу»"
        )
        return

    try:
        slots = await asyncio.get_running_loop().run_in_executor(
            None, lambda: get_free_slots(telegram_id, target)
        )
    except Exception as e:
        logger.error("Calendar error: %s", type(e).__name__)
        await update.message.reply_text("Ошибка при обращении к календарю 😔")
        return

    date_str = date_to_ru(target)
    if slots:
        await update.message.reply_text(
            f"📅 *{date_str}* — свободное время:\n\n" + "\n".join(f"• {s}" for s in slots),
            parse_mode="Markdown",
        )
    else:
        user = db.get_user(telegram_id)
        await update.message.reply_text(
            f"📅 *{date_str}* — день полностью занят.\n"
            f"Нет окон от {_work_start(user)}:00 до {_work_end(user)}:00.",
            parse_mode="Markdown",
        )


async def handle_create_event(telegram_id: int, text: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("📅 Создаю встречу...")
    if not TIME_IN_TEXT.search(text):
        await msg.edit_text("Не понял время встречи 🕐\n\nПопробуй: «поставь встречу в пятницу в 15:00»")
        return

    user = db.get_user(telegram_id)
    tz_name = user["timezone"] if user else DEFAULT_TZ

    text_for_date = re.sub(r"\bна\s+\d+\s*(минут|мин|час[аов]?)\b", "", text, flags=re.IGNORECASE)
    text_for_date = re.sub(r"полчаса|пол\s*часа", "", text_for_date, flags=re.IGNORECASE)
    text_for_date = re.sub(r"\bна\s+(\d{1,2}[:.]\d{2})\b", r"в \1", text_for_date, flags=re.IGNORECASE)
    text_for_date = re.sub(r"\b(в|на)\s+(\d{1,2})\b(?!\s*[:.]\d)",
                           lambda m: f"в {m.group(2)}:00", text_for_date, flags=re.IGNORECASE)

    dt = parse_date(text_for_date, tz_name)
    if not dt:
        await msg.edit_text("Не понял дату встречи 📅\n\nПопробуй: «запланируй созвон завтра в 10:00»")
        return

    title    = extract_event_title(text)
    duration = parse_duration_minutes(text)

    await msg.edit_text("📅 Создаю встречу и ссылку Телемост...")
    telemost_link = await asyncio.get_running_loop().run_in_executor(
        None, lambda: create_telemost_conference(telegram_id)
    )

    inline_emails = extract_emails(text)
    try:
        created  = await asyncio.get_running_loop().run_in_executor(
            None, lambda: create_calendar_event(telegram_id, title, dt, duration, telemost_link)
        )
        event_id = created["id"]
        _track(telegram_id, "first_value", source="calendar_event")
        tz = pytz.timezone(tz_name)
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        end_dt = dt + timedelta(minutes=duration)

        telemost_line = f"\n🔗 {telemost_link}" if telemost_link else "\n⚠️ Телемост не удалось создать"
        invite_note   = ""
        if inline_emails:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: add_attendees_to_event(telegram_id, event_id, inline_emails)
                )
                invite_note = f"\n📧 Приглашены: {', '.join(inline_emails)}"
            except Exception as e:
                logger.error("Ошибка приглашения: %s", type(e).__name__)

        context.user_data["pending_invite"] = {"event_id": event_id, "title": title}
        await msg.edit_text(
            f"✅ <b>Встреча создана!</b>\n\n"
            f"📌 {html_lib.escape(title)}\n"
            f"🗓 {date_to_ru(dt)}\n"
            f"🕐 {dt.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}"
            f"{html_lib.escape(telemost_line)}{html_lib.escape(invite_note)}\n\n"
            f"📧 Кого пригласить? Напиши имя из контактов или email(ы) через запятую\n"
            f"<i>(или напиши «готово» чтобы пропустить)</i>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Ошибка создания события: %s", type(e).__name__)
        await msg.edit_text("😔 Не удалось создать встречу. Попробуй позже.")


# ─── Handlers ────────────────────────────────────────────────

# ─── Продуктовая аналитика (#46) ─────────────────────────────
# События пишутся только из обработчиков действий пользователя.  Фоновые
# задания (scheduler, автоимпорт, сверка), health check и Deploy Monitor
# функции _track не вызывают.  Ошибка аналитики не ломает команду.

ANALYTICS_EXCLUDED_COMMANDS = ("analytics",)
ANALYTICS_REPORT_TZ = DEFAULT_TZ


def _track(user_id, event_type: str, source: str | None = None) -> None:
    try:
        db.analytics_record(user_id, event_type, source=source)
    except Exception as e:
        logger.warning("analytics: запись события не удалась: %s", type(e).__name__)


def _is_excluded_command(update: Update) -> bool:
    msg = update.effective_message
    text = (getattr(msg, "text", None) or "") if msg is not None else ""
    if not text.startswith("/"):
        return False
    cmd = text[1:].split(maxsplit=1)[0].split("@", 1)[0].lower() if len(text) > 1 else ""
    return cmd in ANALYTICS_EXCLUDED_COMMANDS


async def _track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Группа -1: на каждое сообщение / нажатие кнопки пользователя —
    user_active (не чаще раза в день) и user_started для нового пользователя.
    /analytics не считается.  Никогда не прерывает обработку."""
    try:
        user = update.effective_user
        if user is None or user.is_bot:
            return
        if update.message is None and update.callback_query is None:
            return
        if _is_excluded_command(update):
            return
        if db.get_user(user.id) is None:
            _track(user.id, "user_started")
        _track(user.id, "user_active")
    except Exception as e:
        logger.warning("analytics: учёт активности не удался: %s", type(e).__name__)


def _ru_users(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        w = "пользователь"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        w = "пользователя"
    else:
        w = "пользователей"
    return f"{n} {w}"


def _pct(num: int, den: int, digits: int = 1) -> str:
    if not den:
        return "н/д"
    v = round(num * 100 / den, digits)
    s = f"{v:.{digits}f}" if digits else f"{int(v)}"
    return s.replace(".", ",") + "%"


def _format_analytics(d: dict, tz_name: str = ANALYTICS_REPORT_TZ) -> str:
    """Текстовый отчёт /analytics (HTML; только числа и фиксированные подписи)."""
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone("Europe/Moscow")
    started = d.get("started_at")
    since = (datetime.fromtimestamp(started, tz).strftime("%d.%m.%Y")
             if started else "—")
    mau = d["mau"]
    lines = ["<b>📊 Аналитика бота</b>", "",
             "Период: последние 30 дней",
             "Календарные периоды: UTC",
             f"Аналитика собирается с {since}", "",
             "<b>Аудитория</b>",
             f"• DAU: {d['dau']}", f"• WAU: {d['wau']}", f"• MAU: {mau}",
             f"• DAU/MAU: {_pct(d['dau'], mau)}",
             f"• Новые пользователи за 30 дней: {d['new_users_30d']} "
             f"(по дате регистрации; всего {d['total_users']})", "",
             "<b>Удержание</b>"]
    for n in (1, 7, 30):
        ret, cohort = d["retention"][n]
        val = (f"{_pct(ret, cohort, 0)} ({ret} из {cohort})" if cohort
               else "недостаточно данных")
        lines.append(f"• D{n} retention: {val}")
    f = d["features"]
    lines += ["", "<b>Популярные функции</b> (30 дней, доля от MAU)"]
    for key, label in (("tasks_used", "Задачи"), ("mymeet_used", "MYmeet"),
                       ("connect_completed", "Google Connect")):
        lines.append(f"• {label}: {_ru_users(f[key])} ({_pct(f[key], mau)})")
    steps = d["funnel"]
    labels = ("Начали", "Запустили /connect", "Успешно подключили Google",
              "Совершили первое полезное действие")
    lines += ["", "<b>Воронка подключения</b> (пришли за 30 дней)"]
    for i, (label, n) in enumerate(zip(labels, steps)):
        if i == 0:
            lines.append(f"• {label}: {n}")
        else:
            lines.append(f"• {label}: {n} — от предыдущего {_pct(n, steps[i - 1])}, "
                         f"от начала {_pct(n, steps[0])}")
    if d.get("funnel_recent"):
        lines.append(f"<i>Из них {d['funnel_recent']} пришли менее 7 дней назад — "
                     f"наблюдение неполное</i>")
    return "\n".join(lines)


async def cmd_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/analytics — продуктовая аналитика, только для ADMIN_TELEGRAM_ID."""
    user = update.effective_user
    if ADMIN_TELEGRAM_ID is None or user is None or user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("Команда недоступна")
        return
    try:
        data = await asyncio.to_thread(db.analytics_report_data)
        report = _format_analytics(data)
    except Exception as e:
        logger.error("analytics report failed: %s", type(e).__name__)
        await update.message.reply_text("⚠️ Не удалось построить отчёт. Попробуйте позже.")
        return
    await update.message.reply_text(report, parse_mode="HTML")


async def scheduled_analytics_cleanup():
    """Раз в сутки: удаляем сырые события аналитики старше 13 месяцев."""
    try:
        n = await asyncio.get_running_loop().run_in_executor(None, db.analytics_cleanup)
        logger.info("analytics cleanup: удалено событий %d", n)
    except Exception as e:
        logger.warning("analytics cleanup failed: %s", type(e).__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    username    = update.effective_user.username
    db.upsert_user(telegram_id, username)

    if db.is_google_connected(telegram_id):
        await update.message.reply_text(
            "👋 Привет! Я ZinaidaSergeevnaBot.\n\n"
            "*Что умею:*\n"
            "📅 Показать свободные слоты в Google Календаре\n"
            "📌 Поставить встречу в календарь\n"
            "🔗 Добавить ссылку Яндекс Телемост к встречам\n\n"
            "*Примеры:*\n"
            "🎙 «Какие слоты свободны завтра?»\n"
            "🎙 «Поставь встречу с командой в пятницу в 15:00»\n"
            "🎙 «Добавь ссылки на телемост»\n\n"
            "Ссылки Телемост добавляются автоматически каждые 3 часа.\n\n"
            "📋 Перешли мне любое сообщение — оно станет задачей в «Мои задачи»\n"
            "🔄 Google Tasks ↔ «Мои задачи» — каждый день в 19:00 (сейчас: /sync\\_tasks)\n\n"
            "/settings — настройки  |  /disconnect — отключить Google\n"
            "/setname Имя Фамилия — ФИО для листа «Мои задачи — Имя Фамилия» в таблице MYmeet",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "👋 Привет! Я ZinaidaSergeevnaBot — голосовой помощник для Google Календаря.\n\n"
            "Чтобы начать, нужно подключить твой Google аккаунт.\n\n"
            "Нажми /connect 👇",
        )


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    db.upsert_user(telegram_id, update.effective_user.username)
    _track(telegram_id, "connect_started")

    url = auth.get_auth_url(telegram_id)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Войти через Google", url=url)
    ]])
    await update.message.reply_text(
        "Нажми кнопку ниже и авторизуйся через Google. Бот запросит доступ к "
        "Google Календарю и Google Tasks — отметь обе галочки, иначе часть функций "
        "не будет работать.",
        reply_markup=keyboard,
    )


async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    db.save_google_tokens(telegram_id, "", "", 0)
    await update.message.reply_text(
        "🔌 Google Календарь отключён.\nДля повторного подключения: /connect"
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = db.get_user(telegram_id)
    if not user:
        await update.message.reply_text("Сначала /start")
        return
    await update.message.reply_text(
        f"⚙️ Настройки\n\n"
        f"🕐 Рабочее время: {_work_start(user)}:00 — {_work_end(user)}:00\n"
        f"🌍 Часовой пояс: {user['timezone'] or DEFAULT_TZ}\n"
        f"📅 Календарь: {user['calendar_id'] or 'primary'}\n"
        f"👤 ФИО для листа «Мои задачи — ФИО»: {_user_full_name(user) or 'не указано'}\n\n"
        f"Команды:\n"
        f"/settz Europe/Saratov — изменить часовой пояс\n"
        f"/sethours 9 18 — изменить рабочие часы\n"
        f"/setname Имя Фамилия — ФИО для листа «Мои задачи — Имя Фамилия»",
    )


_FULL_NAME_MAX_LEN = 100


def _user_full_name(user) -> str:
    """ФИО из строки users (колонка может отсутствовать до миграции)."""
    try:
        return (user["mymeet_full_name"] or "").strip()
    except (IndexError, KeyError):
        return ""


def _validate_full_name(raw: str):
    """Проверяет ФИО для «Мои задачи».

    Возвращает (normalized, None) при успехе или (None, error_text).
    Правила: не меньше 2 слов, длина ≤ 100, только буквы, дефис и пробел.
    Пробелы схлопываются.
    """
    words = str(raw or "").split()
    if len(words) < 2:
        return None, "Укажите имя и фамилию полностью (минимум 2 слова)."
    name = " ".join(words)
    if len(name) > _FULL_NAME_MAX_LEN:
        return None, f"Слишком длинное ФИО (максимум {_FULL_NAME_MAX_LEN} символов)."
    for w in words:
        if not any(ch.isalpha() for ch in w) or not all(ch.isalpha() or ch == "-" for ch in w):
            return None, "ФИО может содержать только буквы, дефис и пробелы."
    return name, None


async def cmd_setname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setname Имя Фамилия — ФИО для личного листа «Мои задачи»."""
    telegram_id = update.effective_user.id
    db.upsert_user(telegram_id, update.effective_user.username)
    hint = (
        "Лист «Мои задачи — <ваше ФИО>» (например, «Мои задачи — Иван Петров») "
        "в вашей таблице MYmeet собирает:\n"
        "• задачи из протоколов, где вы ответственный (полное ФИО в любом порядке "
        "и падеже или фамилия с инициалами) — со статусом «Требует верификации», "
        "пока вы сами не поменяете статус;\n"
        "• все задачи Google Tasks (статусы синхронизируются в 19:00);\n"
        "• пересланные боту сообщения.\n"
        "У каждого пользователя общей таблицы — свой лист. "
        "В листе можно менять только «Статус» — бот учтёт это при синхронизации.\n"
        "⚠️ «Отменено» у задачи из Google Tasks удаляет её в Google Tasks "
        "при ближайшей синхронизации (19:00 или /sync_tasks)."
    )
    if not context.args:
        user = db.get_user(telegram_id)
        current = _user_full_name(user) if user else ""
        await update.message.reply_text(
            f"👤 Текущее ФИО: {current or 'не указано'}\n\n"
            f"Использование: /setname Имя Фамилия\n"
            f"Пример: /setname Иван Петров\n\n{hint}"
        )
        return
    name, err = _validate_full_name(" ".join(context.args))
    if err:
        await update.message.reply_text(
            f"⚠️ {err}\nПример: /setname Иван Петров"
        )
        return
    db.set_full_name(telegram_id, name)
    await update.message.reply_text(
        f"✅ ФИО сохранено: {name}\n\n{hint}\n\n"
        "Лист появится после следующей синхронизации (/sync_mymeet)."
    )


async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Использование: `/settz Europe/Moscow`", parse_mode="Markdown")
        return
    tz_name = context.args[0]
    try:
        pytz.timezone(tz_name)
    except pytz.exceptions.UnknownTimeZoneError:
        await update.message.reply_text(f"Неизвестный часовой пояс: {tz_name}")
        return
    db.update_user_settings(telegram_id, timezone=tz_name)
    await update.message.reply_text(f"✅ Часовой пояс установлен: {tz_name}")


async def cmd_sethours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text("Использование: `/sethours 9 18`", parse_mode="Markdown")
        return
    try:
        ws, we = int(context.args[0]), int(context.args[1])
        assert 0 <= ws < we <= 24
    except Exception:
        await update.message.reply_text("Пример: `/sethours 9 18`", parse_mode="Markdown")
        return
    db.update_user_settings(telegram_id, work_start=ws, work_end=we)
    await update.message.reply_text(f"✅ Рабочее время: {ws}:00 — {we}:00")


_active_voice_users: set[int] = set()
_MAX_ACTIVE_VOICES = 4


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_google(update):
        return
    telegram_id = update.effective_user.id
    if not context.application.running:
        return
    if telegram_id in _active_voice_users or len(_active_voice_users) >= _MAX_ACTIVE_VOICES:
        await update.message.reply_text("🎙 Уже обрабатываю голосовые сообщения. Попробуй чуть позже.")
        return
    _active_voice_users.add(telegram_id)
    # PTB processes updates sequentially. Track a separate task so the next
    # update can run while SpeechKit is busy, and Application.stop drains it.
    context.application.create_task(_process_voice(telegram_id, update, context), update=update)


async def _process_voice(telegram_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = await update.message.reply_text("🎙 Слушаю...")
        try:
            tg_file = await context.bot.get_file(update.message.voice.file_id)
            audio = await tg_file.download_as_bytearray()
            text = await asyncio.to_thread(transcribe, bytes(audio))
        except Exception as error:
            logger.warning("Voice recognition failed: %s", type(error).__name__)
            text = ""
        if not text:
            await msg.edit_text("Не удалось распознать голос. Попробуй ещё раз.")
            return
        await msg.edit_text(f"Ты сказал: «{text}»\n\n⏳ Обрабатываю...")

        if TELEMOST_TRIGGER.search(text):
            res = await asyncio.to_thread(process_telemost_links, telegram_id)
            await update.message.reply_text("🔗 Телемост-обход завершён\n\n" + format_telemost_report(res))
        elif CREATE_TRIGGER.search(text):
            await handle_create_event(telegram_id, text, update, context)
        else:
            await process_query(telegram_id, text, update)
    finally:
        _active_voice_users.discard(telegram_id)


# Глагольные префиксы которые пользователь добавляет перед именем («Пригласи Анастасию»)
INVITE_VERB_PREFIX = re.compile(
    r"^\s*(пригласи(ть)?|добавь|invite|пригласите)\s+",
    re.IGNORECASE,
)

TASKS_TRIGGER = re.compile(
    r"(покажи|список|мои|открой).{0,20}(задач|таск)|^/tasks$",
    re.IGNORECASE,
)

SKIP_TASK_RE = re.compile(
    r"^\s*скип\s*(\d+)?\s*$",
    re.IGNORECASE,
)

async def _handle_pending_invite(telegram_id: int, text: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending = context.user_data.get("pending_invite")
    if not pending:
        return False

    # Если текст похож на новую команду — завершаем режим приглашения и пропускаем дальше
    if CREATE_TRIGGER.search(text) or TELEMOST_TRIGGER.search(text):
        context.user_data.pop("pending_invite", None)
        return False

    if INVITE_SKIP.search(text):
        context.user_data.pop("pending_invite", None)
        await update.message.reply_text("👍 Без приглашений — окей!")
        return True

    # Убираем глагольный префикс: «Пригласи Анастасию» → «Анастасию»
    text_clean = INVITE_VERB_PREFIX.sub("", text).strip()

    # Сначала ищем email-адреса напрямую
    emails = extract_emails(text_clean)

    # Если email-ов нет — пробуем резолвить имена через глоссарий
    if not emails:
        found, unknown = db.resolve_names_to_emails(telegram_id, text_clean)
        if found:
            emails = found
            if unknown:
                await update.message.reply_text(
                    f"⚠️ Не нашёл в контактах: {', '.join(unknown)}\n"
                    f"Продолжаю с найденными: {', '.join(emails)}"
                )
        else:
            # Ничего не нашлось — сообщаем и сбрасываем режим, чтобы бот не завис
            context.user_data.pop("pending_invite", None)
            await update.message.reply_text(
                f"🤷 Не нашёл в контактах: «{text_clean}»\n"
                f"Встреча создана без приглашений.\n"
                f"Добавь контакт через /add_contact Имя email@example.com",
            )
            return True

    if emails:
        context.user_data.pop("pending_invite", None)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: add_attendees_to_event(telegram_id, pending["event_id"], emails)
            )
            await update.message.reply_text(f"📧 Приглашения отправлены:\n{', '.join(emails)}")
        except Exception as e:
            logger.error("Ошибка добавления участников: %s", type(e).__name__)
            await update.message.reply_text("😔 Не удалось отправить приглашения.")
        return True
    return False


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    text = update.message.text

    # MYmeet визард перехватывает сообщения до проверки Google
    if update.effective_chat.type == "private":
        if await _handle_mymeet_setup(telegram_id, text, update, context):
            return
    elif text.lstrip().startswith("mm_"):
        await _delete_mymeet_token_message(update.message)
        await update.message.reply_text("Настраивай MYmeet в личном чате с ботом.")
        return

    if not await _require_google(update):
        return

    if await _handle_pending_invite(telegram_id, text, update, context):
        return

    if TELEMOST_TRIGGER.search(text):
        msg = await update.message.reply_text("⏳ Проверяю встречи и добавляю ссылки...")
        try:
            res = await asyncio.get_running_loop().run_in_executor(
                None, lambda: process_telemost_links(telegram_id)
            )
            await msg.edit_text(
                f"🔗 Телемост-обход завершён\n\n{format_telemost_report(res)}",
            )
        except Exception as e:
            logger.error("handle_text telemost error: %s", type(e).__name__)
            await msg.edit_text("😔 Ошибка при обходе встреч. Попробуй позже.")
    elif CREATE_TRIGGER.search(text):
        await handle_create_event(telegram_id, text, update, context)
    else:
        await process_query(telegram_id, text, update)


# ─── APScheduler: per-user Телемост ──────────────────────────


async def send_daily_schedule(bot, *, stop_event=None):
    """Каждый день в 8:00 MSK: отправляем расписание встреч на сегодня."""
    users = await asyncio.get_running_loop().run_in_executor(None, db.get_all_active_users)
    for user in users:
        if stop_event is not None and stop_event.is_set():
            return
        telegram_id = user["telegram_id"]
        try:
            tz = _tz(user)
            now = datetime.now(tz)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            today_end   = now.replace(hour=23, minute=59, second=59, microsecond=0)

            service = get_calendar_service(telegram_id)
            if not service:
                continue

            calendar_id = _get_calendar_id(user)
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: service.events().list(
                    calendarId=calendar_id,
                    timeMin=today_start.isoformat(),
                    timeMax=today_end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=50,
                ).execute()
            )
            events = result.get("items", [])

            if not events:
                text = "📅 Расписание на сегодня\n\nВстреч нет — свободный день! 🎉"
            else:
                lines = ["📅 Расписание на сегодня:\n"]
                for ev in events:
                    start_raw = ev.get("start", {})
                    end_raw   = ev.get("end",   {})
                    if "dateTime" in start_raw:
                        s = datetime.fromisoformat(start_raw["dateTime"]).astimezone(tz)
                        e = datetime.fromisoformat(end_raw["dateTime"]).astimezone(tz)
                        time_str = f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}"
                    else:
                        time_str = "весь день"
                    title = ev.get("summary", "(без названия)").strip()
                    lines.append(f"• {time_str}  {title}")
                text = "\n".join(lines)

            await bot.send_message(chat_id=telegram_id, text=text, disable_notification=True)
        except Exception as e:
            logger.error("Daily schedule failed: %s", type(e).__name__)


async def scheduled_telemost_all_users(bot: Bot, *, stop_event=None):
    """Каждые 3 часа: добавляем ссылки для всех активных пользователей."""
    users = await asyncio.get_running_loop().run_in_executor(None, db.get_all_active_users)
    for user in users:
        if stop_event is not None and stop_event.is_set():
            return
        telegram_id = user["telegram_id"]
        try:
            res = await asyncio.get_running_loop().run_in_executor(
                None, lambda uid=telegram_id: process_telemost_links(uid)
            )
            if res["added"]:
                await bot.send_message(
                    chat_id=telegram_id,
                    text=f"🔗 Авто-обход Телемост\n\n{format_telemost_report(res)}",
                    disable_notification=True,
                )
        except Exception as e:
            logger.error("Scheduled Telemost failed: %s", type(e).__name__)


# ─── MYmeet setup wizard ─────────────────────────────────────

_SETUP_STEP_TOKEN = "mymeet_token"
_SETUP_STEP_SHEET = "mymeet_sheet"
_MYMEET_SETUP_TTL = 30 * 60
_MYMEET_RESTART_TEXT = "Сессия настройки MYmeet завершилась. Начни заново: /setup_mymeet."
_MYMEET_SHEET_ID = re.compile(r"[A-Za-z0-9_-]{20,200}")
_MYMEET_SHEET_URL = re.compile(
    r"https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,200})(?:[/?#][^\s]*)?"
)


async def cmd_setup_mymeet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start a replacement draft for /setup_mymeet and /reset_mymeet."""
    telegram_id = update.effective_user.id
    try:
        db.upsert_user(telegram_id, update.effective_user.username)
        db.set_mymeet_setup_in_progress(telegram_id, True)
        context.user_data["mymeet_setup"] = {
            "step": _SETUP_STEP_TOKEN,
            "expires": time_module.monotonic() + _MYMEET_SETUP_TTL,
        }
        _track(telegram_id, "mymeet_used", source="setup_mymeet")
    except Exception as exc:
        logger.error("MYmeet setup start error: %s", type(exc).__name__)
        await update.message.reply_text("Не удалось начать настройку. Попробуй /setup_mymeet позже.")
        return
    await update.message.reply_text(
        "🔧 Настройка MYmeet\n\n"
        "Действующая настройка продолжит работать, пока новая не сохранена.\n"
        "Шаг 1 из 2: отправь API-токен, начинающийся с mm_.\n"
        "Где взять: mymeet.ai → Профиль → API-ключ.\n"
        "Бот попробует удалить сообщение с токеном. Отмена: /cancel."
    )


async def cmd_cancel_mymeet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the replacement draft only."""
    telegram_id = update.effective_user.id
    setup = context.user_data.pop("mymeet_setup", None)
    try:
        user = db.get_user(telegram_id)
        if setup or (user and user["mymeet_setup_in_progress"]):
            db.set_mymeet_setup_in_progress(telegram_id, False)
            text = "Настройка MYmeet отменена. Действующая настройка сохранена."
        else:
            text = "Активной настройки MYmeet нет."
    except Exception as exc:
        logger.error("MYmeet setup cancel error: %s", type(exc).__name__)
        text = "Не удалось подтвердить отмену. Повтори /cancel позже."
    await update.message.reply_text(text)


def _mymeet_sheet_id(text: str) -> str | None:
    if _MYMEET_SHEET_ID.fullmatch(text):
        return text
    match = _MYMEET_SHEET_URL.fullmatch(text)
    return match.group(1) if match else None


def _check_mymeet_sheet_access(sheet_id: str) -> None:
    """Verify the spreadsheet and edit capability without writing a cell."""
    mymeet_sync.require_spreadsheet_editor(sheet_id)
    client = mymeet_sync._gs_client()
    client.http_client.set_timeout(15)
    with client.http_client.session:
        client.http_client.fetch_sheet_metadata(sheet_id, params={"fields": "spreadsheetId"})


def _is_current_mymeet_draft(context, setup: dict) -> bool:
    return (context.user_data.get("mymeet_setup") is setup
            and time_module.monotonic() < setup.get("expires", 0))


async def _delete_mymeet_token_message(message) -> None:
    try:
        await message.delete()
    except Exception as exc:
        logger.warning("MYmeet setup token message deletion error: %s", type(exc).__name__)
        await message.reply_text("Не удалось удалить сообщение с токеном. Удали его вручную.")


async def _handle_mymeet_setup(
    telegram_id: int,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Handle wizard input in the private chat."""
    text = text.strip()
    setup = context.user_data.get("mymeet_setup")
    if not setup or not _is_current_mymeet_draft(context, setup):
        if setup:
            context.user_data.pop("mymeet_setup", None)
            db.set_mymeet_setup_in_progress(telegram_id, False)
        if text.startswith("mm_"):
            await _delete_mymeet_token_message(update.message)
        elif not (setup or _MYMEET_SHEET_URL.fullmatch(text)):
            if not _MYMEET_SHEET_ID.fullmatch(text):
                return False
            user = db.get_user(telegram_id)
            if not user or not user["mymeet_setup_in_progress"]:
                return False
        await update.message.reply_text(_MYMEET_RESTART_TEXT)
        return True

    if setup["step"] == _SETUP_STEP_TOKEN:
        if not re.fullmatch(r"mm_[A-Za-z0-9_+=./-]+", text):
            if text.startswith("mm_"):
                await _delete_mymeet_token_message(update.message)
            await update.message.reply_text("Проверь API-токен MYmeet и попробуй снова. Отмена: /cancel.")
            return True
        await _delete_mymeet_token_message(update.message)
        msg = await update.message.reply_text("⏳ Проверяю токен...")
        try:
            ws = await asyncio.to_thread(verify_token_and_get_workspace, text)
            candidate = _crypto.encrypt_token(text), ws["id"]
        except MymeetTokenError as exc:
            error = {
                401: "Неверный API-токен. Проверь ключ и попробуй снова.",
                403: "MYmeet запретил доступ к API. Проверь ключ и доступ к API в тарифе.",
            }.get(exc.status_code, "MYmeet не подтвердил токен. Попробуй ещё раз позже.")
            await msg.edit_text(error)
            return True
        except Exception as exc:
            logger.warning("MYmeet setup token verification error: %s", type(exc).__name__)
            await msg.edit_text("Не удалось подготовить токен. Попробуй ещё раз позже.")
            return True
        if not _is_current_mymeet_draft(context, setup) or setup["step"] != _SETUP_STEP_TOKEN:
            await msg.edit_text("Эта проверка токена больше не относится к текущему шагу.")
            return True
        setup.update(step=_SETUP_STEP_SHEET, candidate=candidate)
        await msg.edit_text(
            "✅ Токен принят. Шаг 2 из 2: отправь ссылку или ID Google Таблицы.\n"
            "Дай сервисному аккаунту бота доступ на редактирование. Отмена: /cancel."
        )
        return True

    if text.startswith("mm_"):
        await _delete_mymeet_token_message(update.message)
        await update.message.reply_text("Сейчас нужен ID Google Таблицы. Для замены токена начни /setup_mymeet.")
        return True
    sheet_id = _mymeet_sheet_id(text)
    if not sheet_id:
        await update.message.reply_text("Отправь ID таблицы или ссылку вида https://docs.google.com/spreadsheets/d/ID/edit.")
        return True
    encrypted_token, ws_id = setup["candidate"]
    try:
        await asyncio.to_thread(_check_mymeet_sheet_access, sheet_id)
    except Exception as exc:
        logger.warning("MYmeet setup Sheets access check error: %s", type(exc).__name__)
        await update.message.reply_text(
            "Не удалось подтвердить доступ на редактирование. Проверь ссылку и права "
            "сервисного аккаунта или попробуй позже. Отмена: /cancel."
        )
        return True
    try:
        async with _user_lock(telegram_id):
            current = _is_current_mymeet_draft(context, setup)
            if current:
                db.save_mymeet_config(telegram_id, _crypto.decrypt_token(encrypted_token), ws_id, sheet_id)
                context.user_data.pop("mymeet_setup", None)
    except Exception as exc:
        logger.error("MYmeet setup commit error: %s", type(exc).__name__)
        await update.message.reply_text("Не удалось сохранить настройку. Попробуй ещё раз или введи /cancel.")
        return True
    if not current:
        await update.message.reply_text("Этот черновик больше не активен. Новые настройки не применены.")
        return True
    await update.message.reply_text(
        "🎉 MYmeet настроен!\n\n"
        "Первоначальная загрузка: /sync_mymeet_full 30. Ежедневный импорт: /sync_mymeet."
    )
    return True


async def _handle_mymeet_ws_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Ignore old workspace buttons. The verified token identifies the workspace."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("mymeet_ws:"):
        return False
    await query.answer("Эта кнопка устарела. При необходимости запусти /setup_mymeet.")
    return True


# ─── MYmeet sync ─────────────────────────────────────────────


def _get_gcal_organizer_data(telegram_id: int, days: int) -> dict | None:
    """Fetch Google Calendar events and return organizer data for filtering.

    Returns {"ids": set[str], "times": set[int]} where:
      ids   -- GCal event IDs where organizer.self == True
      times -- same events' start times, UTC, minute-rounded, in ms
    Returns None when Google Calendar is unavailable (caller must abort sync).
    """
    return _get_gcal_organizer_data_checked(telegram_id, days)[0]


# Причина недоступности Calendar → (исход автоимпорта, код причины)
_GCAL_REASONS = {
    "no_auth":            ("config", "google_not_connected"),
    "token_invalid":      ("config", "google_token_invalid"),
    "insufficient_scope": ("config", "google_scope"),
    "api_disabled":       ("config", "google_api_disabled"),
}


def _get_gcal_organizer_data_checked(telegram_id: int, days: int) -> tuple:
    """Как _get_gcal_organizer_data, но (data | None, причина | None).
    Причина — категория auth.classify_google_error / «no_auth»."""
    try:
        creds, reason = auth.get_credentials_checked(telegram_id)
    except Exception as exc:
        reason = auth.classify_google_error(exc).category
        creds = None
    if not creds:
        logger.warning("MYmeet calendar authorization unavailable: %s", reason)
        return None, reason or "no_auth"
    try:
        service = build("calendar", "v3", credentials=creds)
        user = db.get_user(telegram_id)
        cal_id = _get_calendar_id(user)
        now = datetime.now(timezone.utc)
        time_min = (now - timedelta(days=days)).isoformat()
        time_max = now.isoformat()
        result = service.events().list(
            calendarId=cal_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            maxResults=2500,
        ).execute()
        organizer_ids: set[str] = set()
        organizer_times: set[int] = set()
        for event in result.get("items", []):
            if event.get("organizer", {}).get("self"):
                organizer_ids.add(event["id"])
                start = event.get("start", {})
                dt_str = start.get("dateTime") or start.get("date")
                if not dt_str:
                    continue
                try:
                    if "T" in dt_str:
                        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    else:
                        dt = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
                    organizer_times.add(int(dt.timestamp() / 60) * 60 * 1000)
                except Exception:
                    pass
        logger.info("MYmeet calendar: %d organizer events", len(organizer_ids))
        return {"ids": organizer_ids, "times": organizer_times}, None
    except Exception as exc:
        err = auth.classify_google_error(exc)
        logger.warning("MYmeet calendar request failed: %s (%s)", type(exc).__name__, err.category)
        return None, err.category  # GCal unavailable -- caller must abort sync


def _user_row_to_mymeet_config(user) -> dict | None:
    """Преобразует строку users в user_config для sync_to_sheets_async.

    Расшифровывает mymeet_token перед возвратом.
    Если токен не удаётся расшифровать — возвращает None и логирует
    предупреждение (без раскрытия токена).
    """
    if not user or not user["mymeet_setup_done"]:
        return None
    raw_token = user["mymeet_token"]
    if not raw_token:
        return None
    try:
        if _crypto.is_encrypted(raw_token):
            token = _crypto.decrypt_token(raw_token)
        else:
            # Незашифрованный токен (устаревшие данные) — пропускаем
            logger.warning(
                "MYmeet: незашифрованная конфигурация — требуется /setup_mymeet"
            )
            return None
    except CryptoDecryptError:
        logger.error(
            "MYmeet: конфигурация не расшифровывается — требуется /setup_mymeet"
        )
        return None
    cfg = dict(user)
    cfg["mymeet_token"] = token
    # «Мои задачи» в редактируемом режиме (протоколы + Google Tasks + Telegram).
    # Google Tasks здесь не опрашивается — только при ежедневной сверке в 19:00.
    cfg["my_tasks_builder"] = my_tasks.MyTasksBuilder(
        user["telegram_id"], _user_full_name(user), tz_name=user["timezone"])
    return cfg


# Сериализация записей листа «Мои задачи» одного пользователя (MYmeet-синк,
# ежедневная сверка Google Tasks, обновление после пересланного сообщения).
_my_tasks_locks: dict[int, asyncio.Lock] = {}


def _user_lock(telegram_id: int) -> asyncio.Lock:
    lock = _my_tasks_locks.get(telegram_id)
    if lock is None:
        lock = _my_tasks_locks[telegram_id] = asyncio.Lock()
    return lock


def _mymeet_config_matches(telegram_id: int, snapshot) -> bool:
    current = db.get_user(telegram_id)
    fields = ("mymeet_setup_done", "mymeet_token", "mymeet_ws_id", "mymeet_spreadsheet_id")
    return bool(current and all(current[field] == snapshot[field] for field in fields))


_MYMEET_CONFIG_CHANGED = "Настройки MYmeet изменились. Запусти синхронизацию снова."


async def cmd_sync_mymeet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Синхронизация за сегодня (/sync_mymeet) — добавляет сегодняшние задачи."""
    try:
        telegram_id = update.effective_user.id
        db.upsert_user(telegram_id, update.effective_user.username)
        user = db.get_user(telegram_id)
        user_config = _user_row_to_mymeet_config(user)

        if not user_config:
            await update.message.reply_text(
                "⚙️ MYmeet ещё не настроен.\n\n"
                "Запусти: /setup_mymeet"
            )
            return
        _track(telegram_id, "mymeet_used", source="sync_mymeet")

        # Fetch GCal organizer data -- abort sync if GCal unavailable
        gcal_data_1d = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _get_gcal_organizer_data(telegram_id, 1)
        )
        if gcal_data_1d is None:
            await update.message.reply_text(
                "❌ Не удалось получить данные Google Calendar для проверки "
                "организатора. Синхронизация отменена — Google Sheets не изменён."
            )
            return
        user_config["gcal_organizer_data"] = gcal_data_1d
        user_config["import_registry"] = mymeet_sync.ImportRegistry(
            db, telegram_id, user_config.get("mymeet_spreadsheet_id"))
        msg = await update.message.reply_text("⏳ Синхронизирую задачи за сегодня из MYmeet...")
        try:
            async with _user_lock(telegram_id):
                if not _mymeet_config_matches(telegram_id, user):
                    await msg.edit_text(_MYMEET_CONFIG_CHANGED)
                    return
                result = await sync_to_sheets_async(days=1, append=True, user_config=user_config)
            _track(telegram_id, "first_value", source="sync_mymeet")
            await msg.edit_text(result)
        except Exception as e:
            logger.error("MYmeet sync error: %s", type(e).__name__)
            await msg.edit_text("❌ Не удалось синхронизировать MYmeet. Попробуй позже.")
    except Exception as fatal:
        logger.error("cmd_sync_mymeet failed: %s", type(fatal).__name__)
        try:
            await update.message.reply_text("❌ Не удалось выполнить команду. Попробуй позже.")
        except Exception:
            pass


async def cmd_sync_mymeet_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Полная синхронизация MYmeet за N дней (/sync_mymeet_full [дней]).

    Пример: /sync_mymeet_full 30  — синхронизировать за последние 30 дней (полная перезапись).
    По умолчанию: 30 дней.
    """
    try:
        telegram_id = update.effective_user.id
        db.upsert_user(telegram_id, update.effective_user.username)
        user = db.get_user(telegram_id)
        user_config = _user_row_to_mymeet_config(user)

        if not user_config:
            await update.message.reply_text(
                "⚙️ MYmeet ещё не настроен.\n\n"
                "Запусти: /setup_mymeet"
            )
            return
        _track(telegram_id, "mymeet_used", source="sync_mymeet_full")

        # Парсим аргумент — количество дней
        days = 30
        if context.args:
            try:
                days = max(1, min(365, int(context.args[0])))
            except ValueError:
                await update.message.reply_text(
                    "⚠️ Укажи целое число дней. Пример: /sync_mymeet_full 30"
                )
                return

        msg = await update.message.reply_text(
            f"⏳ Полная синхронизация MYmeet за {days} дней (перезапись таблицы)..."
        )
        # Fetch GCal organizer data -- abort sync if GCal unavailable
        gcal_data = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _get_gcal_organizer_data(telegram_id, days)
        )
        if gcal_data is None:
            await msg.edit_text(
                "❌ Не удалось получить данные Google Calendar для проверки "
                "организатора. Синхронизация отменена — Google Sheets не изменён."
            )
            return
        user_config["gcal_organizer_data"] = gcal_data
        try:
            async with _user_lock(telegram_id):
                if not _mymeet_config_matches(telegram_id, user):
                    await msg.edit_text(_MYMEET_CONFIG_CHANGED)
                    return
                result = await sync_to_sheets_async(days=days, append=False,
                                                    user_config=user_config)
            await msg.edit_text(result)
        except Exception as e:
            logger.error("MYmeet sync full error: %s", type(e).__name__)
            await msg.edit_text("❌ Не удалось синхронизировать MYmeet. Попробуй позже.")
    except Exception as fatal:
        logger.error("cmd_sync_mymeet_full failed: %s", type(fatal).__name__)
        try:
            await update.message.reply_text("❌ Не удалось выполнить команду. Попробуй позже.")
        except Exception:
            pass


# Автоимпорт: причина → текст для пользователя (без технических деталей)
MYMEET_AUTO_REASON_TEXT = {
    "mymeet_token": "MYmeet отклоняет токен. Подключите заново: /setup_mymeet.",
    "token_undecryptable": "Сохранённый токен MYmeet не читается. Введите его заново: /setup_mymeet.",
    "mymeet_api": "MYmeet отвечает ошибкой. Повторю автоматически.",
    "mymeet_unavailable": "MYmeet временно недоступен. Повторю автоматически.",
    "protocols": "Часть протоколов MYmeet не загрузилась. Повторю автоматически.",
    "my_tasks_sheet": "Задачи записаны в листы протоколов, но лист «Мои задачи» не "
                      "обновился. Повторю автоматически.",
    "my_tasks_guard": "Лист «Мои задачи» не обновлён: сработала защита от массового "
                      "удаления строк. Нужна проверка таблицы.",
    "my_tasks_name_taken": "Лист «Мои задачи» не обновлён: лист с таким именем занят "
                           "протоколом встречи. Переименуйте тот лист протокола.",
    "my_tasks_anomaly": "Лист «Мои задачи» не обновлён: сработала защита данных "
                        "(например, массовая отмена задач). Нужен администратор.",
    "registry": "Задачи записаны, но не удалось сохранить отметку об импорте. "
                "Повторю автоматически.",
    "google_not_connected": "Google не подключён — он нужен, чтобы отобрать встречи, "
                            "где вы организатор. Подключите: /connect",
    "google_token_invalid": "Доступ к Google истёк или отозван. Переподключите: /connect",
    "google_scope": "У бота нет доступа к Google Calendar. Переподключите /connect "
                    "и отметьте доступ к календарю.",
    "google_api_disabled": "Google Calendar API выключен в проекте бота — нужен администратор.",
    "google_calendar": "Google Calendar временно недоступен. Повторю автоматически.",
    "sheet_access": "Нет доступа к Google Таблице MYmeet. Проверьте, что таблица из "
                    "/setup_mymeet открыта для бота на редактирование.",
    "sheets_api": "Google Sheets временно недоступен. Повторю автоматически.",
    "volume": "Найдено необычно много новых задач — импорт остановлен без изменений "
              "в таблице. Сообщите администратору.",
    "internal": "Внутренняя ошибка. Повторю автоматически.",
}


def _mymeet_auto_notice(reason: str, state: dict | None, tz) -> str:
    text = MYMEET_AUTO_REASON_TEXT.get(reason, MYMEET_AUTO_REASON_TEXT["internal"])
    last = (state or {}).get("last_success_ts")
    when = (datetime.fromtimestamp(float(last), tz).strftime("%d.%m %H:%M")
            if last else "ещё не было")
    return ("Автоимпорт задач MYmeet (19:00)\n\n"
            "⚠️ Новые задачи из протоколов не добавлены в таблицу.\n"
            f"{text}\n\nПоследний успешный импорт: {when}.\n"
            "Вручную за сегодня: /sync_mymeet")


async def _run_mymeet_auto(user, state: dict | None, now_ts: float) -> dict:
    """Один автоимпорт пользователя.  Те же проверки, что у /sync_mymeet:
    токен MYmeet, данные организатора из Google Calendar, append без clear.
    Возвращает {"outcome", "reason", "days", "truncated", "stats"}."""
    telegram_id = user["telegram_id"]
    days, truncated = mymeet_sync.auto_window_days(state, now_ts)
    res = {"outcome": mymeet_sync.AUTO_OK, "reason": None, "days": days,
           "truncated": truncated, "stats": {}}
    user_config = _user_row_to_mymeet_config(user)
    if not user_config:
        res.update(outcome=mymeet_sync.AUTO_CONFIG, reason="token_undecryptable")
        return res
    loop = asyncio.get_running_loop()
    gcal, gerr = await loop.run_in_executor(
        None, lambda: _get_gcal_organizer_data_checked(telegram_id, days))
    if gcal is None:
        outcome, reason = _GCAL_REASONS.get(gerr, ("transient", "google_calendar"))
        res.update(outcome=outcome, reason=reason)
        return res
    user_config["gcal_organizer_data"] = gcal
    user_config["import_registry"] = mymeet_sync.ImportRegistry(
        db, telegram_id, user_config.get("mymeet_spreadsheet_id"))
    user_config["max_new_rows"] = mymeet_sync.AUTO_MAX_NEW_ROWS
    try:
        async with _user_lock(telegram_id):
            if not _mymeet_config_matches(telegram_id, user):
                return dict(res, configuration_changed=True)
            await sync_to_sheets_async(days=days, append=True, user_config=user_config,
                                       stats=res["stats"])
    except Exception as e:
        outcome, reason = mymeet_sync.classify_sync_error(e)
        res.update(outcome=outcome, reason=reason)
        return res
    res.update(mymeet_sync.auto_outcome(res["stats"]))
    return res


async def scheduled_mymeet_sync(bot: Bot, *, stop_event=None):
    """Каждые 15 минут: автоимпорт MYmeet → Google Sheets / «Мои задачи».

    Слот — 19:00 по поясу пользователя.  Пропущенный вечер догоняется на
    ближайшем тике, окно — от последнего успешного импорта плюс 3 дня на
    задержку протоколов (не больше 14).  Неудача — повтор с паузой
    15→30→60→120 мин (до 6 попыток за вечер), уведомление без спама."""
    rows = await asyncio.get_running_loop().run_in_executor(None, db.get_mymeet_user_rows)
    now_utc = datetime.now(timezone.utc)
    now_ts = now_utc.timestamp()
    for user in rows:
        if stop_event is not None and stop_event.is_set():
            return
        telegram_id = user["telegram_id"]
        try:
            tz = _tz(user)
            local = now_utc.astimezone(tz)
        except Exception:
            tz = pytz.timezone(DEFAULT_TZ)
            local = now_utc.astimezone(tz)
        try:
            state = db.mymeet_sync_get(telegram_id)
            slot = mymeet_sync.auto_due(state, local, now_ts)
            if not slot:
                continue
            try:
                res = await _run_mymeet_auto(user, state, now_ts)
            except Exception as e:
                logger.error("MYmeet auto error: %s", type(e).__name__)
                res = {"outcome": mymeet_sync.AUTO_TRANSIENT, "reason": "internal",
                       "days": None, "truncated": False, "stats": {}}
            if res.get("configuration_changed"):
                continue
            fields, notify = mymeet_sync.auto_record(
                state, slot, res["outcome"], res["reason"], time_module.time(), now_ts)
            db.mymeet_sync_save(telegram_id, **fields)
            st = res["stats"]
            logger.info(
                "MYmeet auto window=%sd%s outcome=%s reason=%s attempt=%s "
                "fetched=%s organizer=%s tasks=%s new_rows=%s proto_failed=%s "
                "proto_missing=%s protocols_written=%s registry=%s my_tasks=%s",
                res["days"],
                " (truncated)" if res["truncated"] else "", res["outcome"],
                res["reason"] or "-", fields["slot_attempts"], st.get("fetched", "-"),
                st.get("after_org", "-"), st.get("total_tasks", "-"),
                st.get("new_rows", "-"), st.get("protocols_failed", "-"),
                st.get("protocols_missing", "-"), st.get("protocols_written", "-"),
                st.get("registry", "-"), st.get("my_tasks", "-"))
            if notify:
                await bot.send_message(chat_id=telegram_id,
                                       text=_mymeet_auto_notice(res["reason"], state, tz),
                                       disable_notification=True)
        except Exception as e:
            logger.error("MYmeet auto record/notify error: %s", type(e).__name__)


# ─── «Мои задачи»: Google Tasks + пересланные сообщения ───────

MY_TASKS_REFRESH_DELAY = 20          # сек: пачка пересланных → одна запись листа
_my_tasks_refresh_tasks: dict[int, asyncio.Task] = {}
_my_tasks_refresh_running: dict[int, asyncio.Task] = {}
_my_tasks_refresh_stopping = False


async def _run_my_tasks_sync(telegram_id: int, with_gtasks: bool) -> dict:
    async with _user_lock(telegram_id):
        worker = asyncio.create_task(asyncio.to_thread(
            my_tasks.run_sync, telegram_id, with_gtasks=with_gtasks,
        ))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Cancellation of the waiter does not stop an executor write.
            # Hold the user's lock until the worker has actually finished.
            await asyncio.gather(worker, return_exceptions=True)
            raise


def _schedule_my_tasks_refresh(telegram_id: int) -> None:
    """Объединяет ожидающие обновления без отмены уже начатой записи."""
    if _my_tasks_refresh_stopping:
        return
    prev = _my_tasks_refresh_tasks.get(telegram_id)
    if prev is not None:
        prev.cancel()

    def forget_waiting(task: asyncio.Task) -> None:
        if _my_tasks_refresh_tasks.get(telegram_id) is task:
            _my_tasks_refresh_tasks.pop(telegram_id, None)

    async def _later():
        await asyncio.sleep(MY_TASKS_REFRESH_DELAY)
        active = _my_tasks_refresh_running.get(telegram_id)
        if active is not None:
            # A new update can cancel this wait, without cancelling the write.
            await asyncio.shield(active)
        task = asyncio.current_task()
        forget_waiting(task)
        _my_tasks_refresh_running[telegram_id] = task
        try:
            res = await _run_my_tasks_sync(telegram_id, with_gtasks=False)
            logger.info("My tasks refresh: outcome=%s", res["outcome"])
        except Exception as exc:
            logger.error("My tasks refresh failed: %s", type(exc).__name__)
        finally:
            _my_tasks_refresh_running.pop(telegram_id, None)

    task = asyncio.create_task(_later())
    _my_tasks_refresh_tasks[telegram_id] = task
    task.add_done_callback(forget_waiting)


async def _drain_my_tasks_refresh() -> None:
    waiting = tuple(_my_tasks_refresh_tasks.values())
    for task in waiting:
        task.cancel()
    await asyncio.gather(*waiting, *_my_tasks_refresh_running.values(), return_exceptions=True)


async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Любое сообщение, пересланное в личный чат, → задача в «Мои задачи» без
    подтверждения.  Срабатывает раньше мастера MYmeet: пересылка — всегда задача,
    обычные (не пересланные) сообщения мастер получает как прежде."""
    telegram_id = update.effective_user.id
    message = update.message
    db.upsert_user(telegram_id, update.effective_user.username)
    user = db.get_user(telegram_id)
    info = my_tasks.forward_info(message, (user["timezone"] if user else None) or DEFAULT_TZ)
    if info is None:
        return
    try:
        created = await asyncio.get_running_loop().run_in_executor(
            None, lambda: my_tasks.add_forwarded(telegram_id, info))
    except Exception as e:
        logger.error("handle_forwarded: ошибка сохранения [%s]: %s", telegram_id,
                     type(e).__name__)
        await message.reply_text("😔 Не удалось сохранить задачу. Попробуй ещё раз.")
        return
    db.log_event(telegram_id, "my_task_forward")
    _track(telegram_id, "tasks_used", source="forward")
    if info["album"] and not created:
        return          # другая часть того же альбома — задача уже есть
    title = info["title"] if len(info["title"]) <= 120 else info["title"][:117] + "…"
    if not created:
        await message.reply_text(f"♻️ Уже в «Мои задачи»: {title}")
        return
    lines = [f"📌 Добавлено в «Мои задачи»: {title}"]
    if info["assigner"]:
        lines.append(f"👤 Постановщик: {info['assigner']}")
    lines.append(f"📅 Дата: {info['task_date']}")
    if info["deadline"]:
        lines.append(f"⏰ Срок: {info['deadline']}")
    if not (user and user["mymeet_spreadsheet_id"]):
        lines.append("ℹ️ Таблица не настроена — лист появится после /setup_mymeet")
    elif not _user_full_name(user):
        lines.append("ℹ️ Укажите ФИО (/setname Имя Фамилия), чтобы появился лист")
    else:
        _schedule_my_tasks_refresh(telegram_id)
    if user and user["mymeet_setup_in_progress"]:
        lines.append("ℹ️ Идёт настройка MYmeet: токен и ссылку отправьте обычным "
                     "сообщением, не пересылкой.")
    await message.reply_text("\n".join(lines))


def _record_daily_result(telegram_id: int, slot: str, res: dict, state: dict | None) -> bool:
    """Сохраняет итог сверки. Возвращает True, если пользователя надо уведомить."""
    now = time_module.time()
    state = state or {}
    outcome = res.get("outcome")
    if outcome == my_tasks.OUT_OK:
        db.my_tasks_sync_save(telegram_id, last_success_slot=slot, fail_count=0,
                              next_attempt_ts=0, last_attempt_ts=now, last_outcome=outcome)
        return False
    fails = int(state.get("fail_count") or 0) + 1
    notify = my_tasks.should_notify(outcome, fails, state.get("notified_slot"), slot)
    fields = dict(fail_count=fails, last_attempt_ts=now, last_outcome=outcome,
                  next_attempt_ts=now + my_tasks.next_attempt_delay(fails))
    if notify:
        fields["notified_slot"] = slot
    db.my_tasks_sync_save(telegram_id, **fields)
    return notify


async def cmd_sync_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sync_tasks — сверка Google Tasks ↔ «Мои задачи» сейчас (обычно — в 19:00)."""
    telegram_id = update.effective_user.id
    db.upsert_user(telegram_id, update.effective_user.username)
    user = db.get_user(telegram_id)
    if not db.is_google_connected(telegram_id):
        await update.message.reply_text("🔑 Сначала подключи Google: /connect")
        return
    _track(telegram_id, "tasks_used", source="sync_tasks")
    msg = await update.message.reply_text("⏳ Синхронизирую Google Tasks ↔ «Мои задачи»...")
    try:
        res = await _run_my_tasks_sync(telegram_id, with_gtasks=True)
        if res["outcome"] == my_tasks.OUT_OK:
            _track(telegram_id, "first_value", source="sync_tasks")
            slot = my_tasks.daily_slot(datetime.now(timezone.utc).astimezone(_tz(user)))
            db.my_tasks_sync_save(telegram_id, last_success_slot=slot, fail_count=0,
                                  next_attempt_ts=0, last_outcome=res["outcome"])
        await msg.edit_text(res["text"])
    except Exception as e:
        logger.error("cmd_sync_tasks error: %s", type(e).__name__)
        await msg.edit_text("❌ Синхронизация не выполнена (внутренняя ошибка). Попробуй позже.")


async def scheduled_my_tasks_daily(bot: Bot, *, stop_event=None):
    """Каждые 15 минут.  Для каждого пользователя с Google: если наступил
    слот 19:00 по его поясу и он ещё не выполнен полностью — сверка.
    Неудача (частичная/временная/аномалия) не отмечает слот выполненным:
    повтор с паузой 15→30→60→120 мин, в том числе после 19:59 и ночью,
    пока не будет успеха (дальше — уже следующий слот)."""
    users = await asyncio.get_running_loop().run_in_executor(None, db.get_all_active_users)
    now_utc = datetime.now(timezone.utc)
    for user in users:
        if stop_event is not None and stop_event.is_set():
            return
        telegram_id = user["telegram_id"]
        try:
            local = now_utc.astimezone(_tz(user))
        except Exception:
            local = now_utc.astimezone(pytz.timezone(DEFAULT_TZ))
        state = db.my_tasks_sync_get(telegram_id)
        slot = my_tasks.daily_due(state, local, now_utc.timestamp())
        if not slot:
            continue
        try:
            res = await _run_my_tasks_sync(telegram_id, with_gtasks=True)
        except Exception as e:
            logger.error("My tasks daily error: %s", type(e).__name__)
            res = {"outcome": my_tasks.OUT_TRANSIENT,
                   "text": my_tasks._HEADINGS[my_tasks.OUT_TRANSIENT]}
        logger.info("My tasks daily outcome=%s", res["outcome"])
        try:
            notify = _record_daily_result(telegram_id, slot, res, state)
            if notify:
                await bot.send_message(
                    chat_id=telegram_id,
                    text="Синхронизация «Мои задачи» (19:00)\n\n" + res["text"],
                    disable_notification=True,
                )
        except Exception as e:
            logger.error("My tasks daily record/notify error: %s", type(e).__name__)


# ─── OAuth callback (aiohttp web server) ─────────────────────

async def oauth_callback(request: web.Request) -> web.Response:
    """GET /oauth/callback?code=…&state=…"""
    code  = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")
    error = request.rel_url.query.get("error")

    if error or not code or not state:
        category = "user_denied" if error == "access_denied" else "invalid_request"
        logger.info("OAuth callback: category=%s", category)
        return web.Response(
            text="<h2>Ошибка авторизации</h2><p>Попробуй снова: /connect в боте.</p>",
            content_type="text/html", status=400,
        )

    try:
        result = await asyncio.to_thread(auth.exchange_code, code, state)
    except Exception as e:
        err = auth.classify_google_error(e)
        category = "token_invalid" if getattr(e, "error", None) == "invalid_grant" else err.category
        status = {"transient": 503, "token_invalid": 400}.get(category, 502)
        logger.warning("OAuth exchange failed: category=%s response_status=%s", category, status)
        return web.Response(
            text="<h2>Ошибка авторизации</h2>"
                 "<p>Не удалось завершить авторизацию, запросите ссылку заново: /connect</p>",
            content_type="text/html", status=status,
        )
    if not result:
        return web.Response(
            text="<h2>Ссылка недействительна</h2><p>Запроси ссылку заново: /connect</p>",
            content_type="text/html", status=400,
        )

    telegram_id, granted = result
    _track(telegram_id, "connect_completed")
    text = _oauth_granted_text(granted)
    # Уведомляем пользователя в Telegram (обычный текст, без Markdown)
    if _bot:
        try:
            await _bot.send_message(chat_id=telegram_id, text=text)
        except Exception as e:
            logger.error("Не удалось отправить сообщение после OAuth: %s", type(e).__name__)

    return web.Response(
        text=f"<h2>Готово</h2><p>{html_lib.escape(text)}</p><p>Можешь вернуться в Telegram.</p>",
        content_type="text/html",
    )


def _oauth_granted_text(granted) -> str:
    """Сообщение после OAuth — только по фактически выданным scopes."""
    if granted is None:
        return "✅ Google подключён"
    cal = auth.SCOPE_CALENDAR in granted
    tasks = auth.SCOPE_TASKS in granted
    if cal and tasks:
        return "✅ Подключены Google Календарь и Google Tasks"
    if cal:
        return ("⚠️ Календарь подключён. Доступ к Google Tasks не выдан — "
                "/connect и отметьте Google Tasks")
    if tasks:
        return ("⚠️ Google Tasks подключены. Доступ к Календарю не выдан — слоты и встречи "
                "работать не будут; /connect и отметьте Календарь")
    return ("⚠️ Google подключён, но доступ к Календарю и Google Tasks не выдан — "
            "/connect и отметьте обе галочки")


# ─── Запуск ──────────────────────────────────────────────────

def _format_tasks(tasks: list) -> str:
    """Форматирует список задач для вывода в Telegram."""
    if not tasks:
        return "✅ Нет открытых задач!"
    lines = ["📋 <b>Открытые задачи:</b>\n"]
    for i, t in enumerate(tasks, 1):
        title = html_lib.escape((t.get("title") or "(без названия)").strip())
        due   = html_lib.escape((t.get("due") or "")[:10])
        note  = f" <i>({due})</i>" if due else ""
        lines.append(f"{i}. {title}{note}")
    lines.append("\nНапиши <b>скип</b> — закрыть первую, <b>скип 2</b> — конкретную.")
    return "\n".join(lines)


async def _handle_pending_tasks(
    telegram_id: int,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Перехватывает «скип [N]» когда есть кеш задач. Возвращает True если перехватил."""
    tasks = context.user_data.get("tasks")
    if tasks is None:
        return False

    m = SKIP_TASK_RE.match(text)
    if not m:
        return False

    raw_idx = m.group(1)
    idx = (int(raw_idx) - 1) if raw_idx else 0

    if idx < 0 or idx >= len(tasks):
        await update.message.reply_text(
            f"⚠️ Нет задачи с номером {idx + 1}.",
            parse_mode="HTML",
        )
        return True

    task = tasks[idx]
    ok = await asyncio.get_running_loop().run_in_executor(
        None, lambda: gtasks.complete_task(telegram_id, task["id"], task["_listId"])
    )

    if ok:
        tasks.pop(idx)
        context.user_data["tasks"] = tasks
        title = html_lib.escape((task.get("title") or "(без названия)").strip())
        reply = f"✅ Закрыто: <i>{title}</i>\n\n" + _format_tasks(tasks)
        if not tasks:
            context.user_data.pop("tasks", None)
    else:
        reply = "⚠️ Не удалось закрыть задачу, попробуй ещё раз."

    await update.message.reply_text(reply, parse_mode="HTML")
    return True


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает открытые задачи из Google Tasks."""
    telegram_id = update.effective_user.id
    if not db.is_google_connected(telegram_id):
        await update.message.reply_text("🔑 Сначала подключи Google: /connect")
        return
    _track(telegram_id, "tasks_used", source="tasks")   # как в /sync_tasks: после проверки Google

    tasks, err = await asyncio.get_running_loop().run_in_executor(
        None, lambda: gtasks.list_all_tasks(telegram_id)
    )

    if err:
        # Обычный текст (без MarkdownV2): причина — из общего словаря my_tasks
        await update.message.reply_text("⚠️ " + my_tasks.gt_error_text(err))
        return

    context.user_data["tasks"] = tasks
    await update.message.reply_text(_format_tasks(tasks), parse_mode="HTML")
    _track(telegram_id, "first_value", source="tasks")


async def cmd_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список контактов пользователя."""
    telegram_id = update.effective_user.id
    rows = db.list_contacts(telegram_id)
    if not rows:
        await update.message.reply_text(
            "📒 Список контактов пуст.\n"
            "Добавь через: /add_contact Имя email@example.com",
        )
        return
    lines = ["📒 Контакты для приглашений:\n"]
    for r in rows:
        lines.append(f"👤 {r['display_name']} — {r['email']}")
    lines.append("\n/del_contact Имя — удалить контакт")
    await update.message.reply_text("\n".join(lines))


async def cmd_add_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавляет контакт: /add_contact Анастасия bragina@hsk.ru"""
    telegram_id = update.effective_user.id
    args = context.args  # список аргументов после команды
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Использование: /add_contact Имя email@example.com\n\n"
            "Пример: /add_contact Анастасия bragina@hsk.ru",
        )
        return
    email = args[-1]          # последний аргумент — email
    name  = " ".join(args[:-1])  # остальное — имя (может быть несколько слов)
    if not EMAIL_PATTERN.match(email):
        await update.message.reply_text(f"❌ Похоже, «{email}» — не email-адрес.")
        return
    db.add_contact(telegram_id, name, email)
    await update.message.reply_text(
        f"✅ Сохранено: {name} → {email}\n\n"
        f"Теперь можно приглашать: просто напиши «{name}» когда бот спросит «Кого пригласить?»",
    )


async def cmd_del_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет контакт: /del_contact Анастасия"""
    telegram_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Использование: /del_contact Имя",
        )
        return
    name = " ".join(context.args)
    if db.delete_contact(telegram_id, name):
        await update.message.reply_text(f"🗑 Контакт «{name}» удалён.")
    else:
        await update.message.reply_text(
            f"🤷 Контакт «{name}» не найден.\n"
            f"Посмотри список: /contacts"
        )


async def run_bot():
    # Инициализируем БД
    db.init_db()
    logger.info("База данных инициализирована: %s", db.DB_PATH)

    # Telegram Application
    request = HTTPXRequest(connect_timeout=60, read_timeout=60, write_timeout=60, pool_timeout=60)
    polling_request = HTTPXRequest()
    app = (Application.builder().token(TELEGRAM_TOKEN).request(request)
           .get_updates_request(polling_request).build())

    # Аналитика (#46): группа -1 — раньше всех обработчиков, обработку не прерывает
    app.add_handler(TypeHandler(Update, _track_activity), group=-1)
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("connect",      cmd_connect))
    app.add_handler(CommandHandler("disconnect",   cmd_disconnect))
    app.add_handler(CommandHandler("settings",     cmd_settings))
    app.add_handler(CommandHandler("settz",        cmd_settz))
    app.add_handler(CommandHandler("sethours",     cmd_sethours))
    app.add_handler(CommandHandler("setname",      cmd_setname))
    app.add_handler(CommandHandler("contacts",     cmd_contacts))
    app.add_handler(CommandHandler("add_contact",  cmd_add_contact))
    app.add_handler(CommandHandler("del_contact",  cmd_del_contact))
    app.add_handler(CommandHandler("tasks",         cmd_tasks))
    app.add_handler(CommandHandler("analytics",     cmd_analytics))
    app.add_handler(CommandHandler("sync_tasks",    cmd_sync_tasks))
    app.add_handler(CommandHandler("sync_mymeet",      cmd_sync_mymeet))
    app.add_handler(CommandHandler("sync_mymeet_full", cmd_sync_mymeet_full))
    app.add_handler(CommandHandler("setup_mymeet", cmd_setup_mymeet, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("reset_mymeet", cmd_setup_mymeet, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("cancel", cmd_cancel_mymeet, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: _handle_mymeet_ws_callback(u, c),
        pattern=r"^mymeet_ws:",
    ))
    # Пересланные сообщения → «Мои задачи» (раньше голосовых/текстовых хендлеров)
    app.add_handler(MessageHandler(
        filters.FORWARDED & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_forwarded))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Telegram handler failed: %s", type(context.error).__name__)
    app.add_error_handler(error_handler)

    # APScheduler
    scheduler = AsyncIOScheduler()
    stop_event = asyncio.Event()
    active_jobs: set[asyncio.Task] = set()
    active_callbacks: set[asyncio.Task] = set()

    def scheduled(job, *, stop_between_users=False):
        async def run(*args):
            if stop_event.is_set():
                return
            task = asyncio.current_task()
            active_jobs.add(task)
            try:
                if stop_between_users:
                    await job(*args, stop_event=stop_event)
                else:
                    await job(*args)
            except Exception as exc:
                logger.error("Scheduled job %s failed: %s", job.__name__, type(exc).__name__)
            finally:
                active_jobs.discard(task)
        return run

    scheduler.add_job(
        scheduled(scheduled_telemost_all_users, stop_between_users=True),
        "interval",
        hours=3,
        args=[app.bot],
        id="telemost_auto",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled(send_daily_schedule, stop_between_users=True),
        "cron",
        day_of_week="mon-fri",
        hour=5,
        minute=0,
        args=[app.bot],
        id="daily_schedule",
        replace_existing=True,
        timezone="UTC",
    )
    scheduler.add_job(
        scheduled(scheduled_mymeet_sync, stop_between_users=True),
        "cron",
        minute="*/15",   # 19:00 по поясу пользователя + догон пропущенных вечеров
        args=[app.bot],
        id="mymeet_daily_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        timezone="UTC",
    )
    scheduler.add_job(
        scheduled(scheduled_my_tasks_daily, stop_between_users=True),
        "cron",
        minute="*/15",   # 19:00 по поясу пользователя — проверяется на каждом тике
        args=[app.bot],
        id="my_tasks_daily",
        replace_existing=True,
        max_instances=1,      # тики не накладываются друг на друга
        coalesce=True,
        timezone="UTC",
    )
    scheduler.add_job(
        scheduled(scheduled_analytics_cleanup),
        "cron",
        hour=3,
        minute=30,
        id="analytics_cleanup",
        replace_existing=True,
        timezone="UTC",
    )
    async def tracked_oauth_callback(request: web.Request) -> web.Response:
        if stop_event.is_set():
            return web.Response(status=503)
        task = asyncio.current_task()
        active_callbacks.add(task)
        try:
            return await oauth_callback(request)
        except Exception as exc:
            logger.error("OAuth callback failed: %s", type(exc).__name__)
            return web.Response(status=500, text="Не удалось завершить авторизацию. Попробуй /connect.")
        finally:
            active_callbacks.discard(task)

    web_app = web.Application()
    web_app.router.add_get("/oauth/callback", tracked_oauth_callback)
    web_app.router.add_get("/health", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(web_app, access_log=None)
    await _run_bot_lifecycle(
        app, scheduler, runner, stop_event, active_jobs, active_callbacks,
        (request, polling_request),
    )


async def _run_bot_lifecycle(
    app, scheduler, runner, stop_event, active_jobs, active_callbacks, requests,
):
    global _bot, _my_tasks_refresh_stopping

    loop = asyncio.get_running_loop()
    previous_handlers = {}
    initialized = False
    site = None
    listening = False
    _my_tasks_refresh_stopping = False

    async def drain_http():
        try:
            if listening:
                await site.stop()
            # AppRunner can cancel HTTP handlers on shutdown timeout.
            # Drain accepted OAuth writes while Telegram is still initialized.
            await asyncio.gather(*active_callbacks, return_exceptions=True)
        finally:
            await runner.cleanup()

    async def drain_scheduler():
        try:
            await asyncio.gather(*active_jobs, return_exceptions=True)
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=True)
                # AsyncIOScheduler schedules shutdown on the event loop.
                await asyncio.sleep(0)

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous = signal.getsignal(sig)
            loop.add_signal_handler(sig, stop_event.set)
            previous_handlers[sig] = previous

        await app.initialize()
        initialized = True
        if stop_event.is_set():
            return
        _bot = app.bot
        await app.start()
        if stop_event.is_set():
            return
        await app.updater.start_polling(bootstrap_retries=0)
        if stop_event.is_set():
            return
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", OAUTH_CALLBACK_PORT)
        if stop_event.is_set():
            return
        await site.start()
        listening = True
        if not stop_event.is_set():
            scheduler.start()
            logger.info("ZinaidaSergeevnaBot (multi-user) запущен.")
        await stop_event.wait()
    finally:
        stop_event.set()
        _my_tasks_refresh_stopping = True
        try:
            if scheduler.running:
                scheduler.pause()
        finally:
            try:
                if app.updater.running:
                    await app.updater.stop()
            finally:
                try:
                    if app.running:
                        await app.stop()
                finally:
                    try:
                        # Let accepted operations finish before scheduler and client shutdown.
                        # Fly can still enforce kill_timeout if a remote call stalls.
                        outcomes = await asyncio.gather(
                            drain_http(), drain_scheduler(), _drain_my_tasks_refresh(),
                            return_exceptions=True,
                        )
                        for result in outcomes:
                            if isinstance(result, Exception):
                                logger.error("Bot cleanup failed: %s", type(result).__name__)
                    finally:
                        try:
                            if initialized:
                                await app.shutdown()
                            else:
                                # PTB cannot shut down an Application whose initialize()
                                # failed after opening one of its HTTP clients.
                                await asyncio.gather(
                                    *(client.shutdown() for client in requests),
                                    return_exceptions=True,
                                )
                        finally:
                            _bot = None
                            for sig, previous in previous_handlers.items():
                                loop.remove_signal_handler(sig)
                                signal.signal(sig, previous)


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
