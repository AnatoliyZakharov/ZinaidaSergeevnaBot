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
"""

import asyncio
import os
import json
import logging
import re
from datetime import datetime, time, timedelta
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
    filters, ContextTypes, CallbackQueryHandler,
)
from telegram.request import HTTPXRequest

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import db
import auth

# ─── Логирование ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Конфиг ──────────────────────────────────────────────────
TELEGRAM_TOKEN        = os.environ["TELEGRAM_TOKEN"]
YANDEX_API_KEY        = os.environ["YANDEX_API_KEY"]
YANDEX_FOLDER         = os.environ["YANDEX_FOLDER_ID"]
YANDEX_TELEMOST_TOKEN = os.environ.get("YANDEX_TELEMOST_TOKEN", "")
DEFAULT_TZ            = os.environ.get("TIMEZONE", "Europe/Moscow")
DEFAULT_WORK_START    = int(os.environ.get("WORK_START", "9"))
DEFAULT_WORK_END      = int(os.environ.get("WORK_END", "19"))
WEBHOOK_HOST          = os.environ.get("WEBHOOK_HOST", "")   # напр. https://bot.example.com
OAUTH_CALLBACK_PORT   = int(os.environ.get("OAUTH_CALLBACK_PORT", "8080"))

SCOPES = ["https://www.googleapis.com/auth/calendar"]

CONFERENCE_PATTERNS = re.compile(
    r"(zoom\.us|teams\.microsoft\.com|webex\.com|whereby\.com|"
    r"gotomeeting\.com|bluejeans\.com|meet\.jit\.si|"
    r"telemost\.yandex\.ru|telemost\.yandex\.net)",
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
    return user_row["work_start"] or DEFAULT_WORK_START


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

def get_free_slots(telegram_id: int, target_date: datetime) -> list[str]:
    user = db.get_user(telegram_id)
    tz   = _tz(user)
    calendar_id = _get_calendar_id(user)
    service = get_calendar_service(telegram_id)

    work_start = tz.localize(datetime.combine(target_date.date(), time(_work_start(user), 0)))
    work_end   = tz.localize(datetime.combine(target_date.date(), time(_work_end(user), 0)))

    body = {
        "timeMin": work_start.isoformat(),
        "timeMax": work_end.isoformat(),
        "items":   [{"id": calendar_id}],
        "timeZone": tz.zone,
    }
    result = service.freebusy().query(body=body).execute()
    busy_raw = result["calendars"][calendar_id]["busy"]

    busy = []
    for p in sorted(busy_raw, key=lambda x: x["start"]):
        s = datetime.fromisoformat(p["start"].replace("Z", "+00:00")).astimezone(tz)
        e = datetime.fromisoformat(p["end"].replace("Z", "+00:00")).astimezone(tz)
        busy.append((s, e))

    free, cur = [], work_start
    for b_start, b_end in busy:
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
        body["description"] = f"🔗 Яндекс Телемост: {telemost_link}"
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
    user = db.get_user(telegram_id)
    calendar_id = _get_calendar_id(user)
    service = get_calendar_service(telegram_id)
    old_desc = event.get("description") or ""
    try:
        service.events().patch(
            calendarId=calendar_id,
            eventId=event["id"],
            body={"description": old_desc + f"\n\n🔗 Яндекс Телемост: {link}"},
        ).execute()
        return True
    except Exception as e:
        logger.error("Ошибка обновления события %s: %s", event["id"], e)
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
    logger.error("SpeechKit %s: %s", resp.status_code, resp.text)
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
        logger.error("Телемост: %s", e)
        return None

    if resp.status_code in (200, 201):
        data = resp.json()
        return data.get("join_url") or data.get("conferenceUri") or data.get("uri")
    logger.error("Телемост API %s: %s", resp.status_code, resp.text)
    return None


def has_conference_link(event: dict) -> bool:
    text = " ".join([event.get("description") or "", event.get("location") or ""])
    if CONFERENCE_PATTERNS.search(text):
        return True
    for ep in (event.get("conferenceData") or {}).get("entryPoints") or []:
        if CONFERENCE_PATTERNS.search(ep.get("uri") or ""):
            return True
    return False


def process_telemost_links(telegram_id: int) -> dict:
    result = {"added": [], "skipped": 0, "errors": []}
    try:
        events = get_events_next_7_days(telegram_id)
    except Exception as e:
        result["errors"].append(f"Не удалось получить события: {e}")
        return result

    for event in events:
        if event.get("status") == "cancelled":
            continue
        if "date" in event.get("start", {}) and "dateTime" not in event.get("start", {}):
            result["skipped"] += 1
            continue
        if has_conference_link(event):
            result["skipped"] += 1
            continue

        link = create_telemost_conference(telegram_id)
        if not link:
            result["errors"].append(f"Телемост не создался для «{event.get('summary', '?')}»")
            continue

        if add_telemost_to_event(telegram_id, event, link):
            result["added"].append({"title": event.get("summary", "(без названия)"), "link": link})
        else:
            result["errors"].append(f"Не удалось обновить «{event.get('summary', '?')}»")
    return result


def format_telemost_report(res: dict) -> str:
    lines = []
    if res["added"]:
        lines.append(f"✅ Добавлено ссылок: {len(res['added'])}")
        for item in res["added"]:
            lines.append(f"  • {item['title']}")
    else:
        lines.append("ℹ️ Новых встреч без ссылок не найдено.")
    if res["skipped"]:
        lines.append(f"⏭ Пропущено: {res['skipped']}")
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
        slots = await asyncio.get_event_loop().run_in_executor(
            None, lambda: get_free_slots(telegram_id, target)
        )
    except Exception as e:
        logger.error("Calendar error: %s", e)
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
    telemost_link = await asyncio.get_event_loop().run_in_executor(
        None, lambda: create_telemost_conference(telegram_id)
    )

    inline_emails = extract_emails(text)
    try:
        created  = await asyncio.get_event_loop().run_in_executor(
            None, lambda: create_calendar_event(telegram_id, title, dt, duration, telemost_link)
        )
        event_id = created["id"]
        tz = pytz.timezone(tz_name)
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        end_dt = dt + timedelta(minutes=duration)

        telemost_line = f"\n🔗 {telemost_link}" if telemost_link else "\n⚠️ Телемост не удалось создать"
        invite_note   = ""
        if inline_emails:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: add_attendees_to_event(telegram_id, event_id, inline_emails)
                )
                invite_note = f"\n📧 Приглашены: {', '.join(inline_emails)}"
            except Exception as e:
                logger.error("Ошибка приглашения: %s", e)

        context.user_data["pending_invite"] = {"event_id": event_id, "title": title}
        await msg.edit_text(
            f"✅ *Встреча создана!*\n\n"
            f"📌 {title}\n"
            f"🗓 {date_to_ru(dt)}\n"
            f"🕐 {dt.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}"
            f"{telemost_line}{invite_note}\n\n"
            f"📧 Кого пригласить? Пришли email(ы) через запятую или напиши «готово»",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("Ошибка создания события: %s", e)
        await msg.edit_text("😔 Не удалось создать встречу. Попробуй позже.")


# ─── Handlers ────────────────────────────────────────────────

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
            "/settings — настройки  |  /disconnect — отключить Google",
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

    url = auth.get_auth_url(telegram_id)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Войти через Google", url=url)
    ]])
    await update.message.reply_text(
        "Нажми кнопку ниже и авторизуйся через Google.\n"
        "После этого бот получит доступ только к твоему Календарю.",
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
        f"⚙️ *Настройки*\n\n"
        f"🕐 Рабочее время: {_work_start(user)}:00 — {_work_end(user)}:00\n"
        f"🌍 Часовой пояс: {user['timezone'] or DEFAULT_TZ}\n"
        f"📅 Календарь: {user['calendar_id'] or 'primary'}\n\n"
        f"Команды:\n"
        f"`/settz Europe/Saratov` — изменить часовой пояс\n"
        f"`/sethours 9 18` — изменить рабочие часы",
        parse_mode="Markdown",
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
        await update.message.reply_text(f"Неизвестный часовой пояс: `{tz_name}`", parse_mode="Markdown")
        return
    db.update_user_settings(telegram_id, timezone=tz_name)
    await update.message.reply_text(f"✅ Часовой пояс установлен: `{tz_name}`", parse_mode="Markdown")


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


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_google(update):
        return
    telegram_id = update.effective_user.id
    msg = await update.message.reply_text("🎙 Слушаю...")
    tg_file = await context.bot.get_file(update.message.voice.file_id)
    audio   = await tg_file.download_as_bytearray()
    text    = transcribe(bytes(audio))
    if not text:
        await msg.edit_text("Не удалось распознать голос. Попробуй ещё раз.")
        return
    await msg.edit_text(f"Ты сказал: «{text}»\n\n⏳ Обрабатываю...")

    if TELEMOST_TRIGGER.search(text):
        res = await asyncio.get_event_loop().run_in_executor(
            None, lambda: process_telemost_links(telegram_id)
        )
        await update.message.reply_text(
            f"🔗 *Телемост-обход завершён*\n\n{format_telemost_report(res)}",
            parse_mode="Markdown",
        )
    elif CREATE_TRIGGER.search(text):
        await handle_create_event(telegram_id, text, update, context)
    else:
        await process_query(telegram_id, text, update)


async def _handle_pending_invite(telegram_id: int, text: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending = context.user_data.get("pending_invite")
    if not pending:
        return False
    if INVITE_SKIP.search(text):
        context.user_data.pop("pending_invite", None)
        await update.message.reply_text("👍 Без приглашений — окей!")
        return True
    emails = extract_emails(text)
    if emails:
        context.user_data.pop("pending_invite", None)
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: add_attendees_to_event(telegram_id, pending["event_id"], emails)
            )
            await update.message.reply_text(f"📧 Приглашения отправлены:\n{', '.join(emails)}")
        except Exception as e:
            logger.error("Ошибка добавления участников: %s", e)
            await update.message.reply_text("😔 Не удалось отправить приглашения.")
        return True
    return False


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_google(update):
        return
    telegram_id = update.effective_user.id
    text = update.message.text

    if await _handle_pending_invite(telegram_id, text, update, context):
        return

    if TELEMOST_TRIGGER.search(text):
        msg = await update.message.reply_text("⏳ Проверяю встречи и добавляю ссылки...")
        try:
            res = await asyncio.get_event_loop().run_in_executor(
                None, lambda: process_telemost_links(telegram_id)
            )
            await msg.edit_text(
                f"🔗 *Телемост-обход завершён*\n\n{format_telemost_report(res)}",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error("handle_text telemost error: %s", e)
            await msg.edit_text("😔 Ошибка при обходе встреч. Попробуй позже.")
    elif CREATE_TRIGGER.search(text):
        await handle_create_event(telegram_id, text, update, context)
    else:
        await process_query(telegram_id, text, update)


# ─── APScheduler: per-user Телемост ──────────────────────────

async def scheduled_telemost_all_users(bot: Bot):
    """Каждые 3 часа: добавляем ссылки для всех активных пользователей."""
    users = await asyncio.get_event_loop().run_in_executor(None, db.get_all_active_users)
    for user in users:
        telegram_id = user["telegram_id"]
        try:
            res = await asyncio.get_event_loop().run_in_executor(
                None, lambda uid=telegram_id: process_telemost_links(uid)
            )
            if res["added"]:
                await bot.send_message(
                    chat_id=telegram_id,
                    text=f"🔗 *Авто-обход Телемост*\n\n{format_telemost_report(res)}",
                    parse_mode="Markdown",
                )
        except Exception as e:
            logger.error("Ошибка scheduled для user %s: %s", telegram_id, e)


# ─── OAuth callback (aiohttp web server) ─────────────────────

async def oauth_callback(request: web.Request) -> web.Response:
    """GET /oauth/callback?code=…&state=…"""
    code  = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")
    error = request.rel_url.query.get("error")

    if error or not code or not state:
        return web.Response(
            text="<h2>Ошибка авторизации</h2><p>Попробуй снова: /connect в боте.</p>",
            content_type="text/html",
        )

    telegram_id = await asyncio.get_event_loop().run_in_executor(
        None, lambda: auth.exchange_code(code, state)
    )
    if not telegram_id:
        return web.Response(
            text="<h2>Неверный или просроченный state</h2><p>Запроси ссылку заново: /connect</p>",
            content_type="text/html",
        )

    # Уведомляем пользователя в Telegram
    if _bot:
        try:
            await _bot.send_message(
                chat_id=telegram_id,
                text="✅ *Google Календарь подключён!*\n\nТеперь можешь спрашивать про свободные слоты голосом или текстом.",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error("Не удалось отправить сообщение после OAuth: %s", e)

    return web.Response(
        text="<h2>✅ Готово!</h2><p>Google Календарь подключён. Можешь вернуться в Telegram.</p>",
        content_type="text/html",
    )


# ─── Запуск ──────────────────────────────────────────────────

async def run_bot():
    global _bot

    # Инициализируем БД
    db.init_db()
    logger.info("База данных инициализирована: %s", db.DB_PATH)

    # Telegram Application
    request = HTTPXRequest(connect_timeout=60, read_timeout=60, write_timeout=60, pool_timeout=60)
    app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()
    _bot = app.bot

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("connect",    cmd_connect))
    app.add_handler(CommandHandler("disconnect", cmd_disconnect))
    app.add_handler(CommandHandler("settings",   cmd_settings))
    app.add_handler(CommandHandler("settz",      cmd_settz))
    app.add_handler(CommandHandler("sethours",   cmd_sethours))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Необработанное исключение в хендлере:", exc_info=context.error)
    app.add_error_handler(error_handler)

    # APScheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_telemost_all_users,
        "interval",
        hours=3,
        args=[app.bot],
        id="telemost_auto",
        replace_existing=True,
    )
    scheduler.start()

    # OAuth callback web server
    web_app = web.Application()
    web_app.router.add_get("/oauth/callback", oauth_callback)
    web_app.router.add_get("/health", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", OAUTH_CALLBACK_PORT)
    await site.start()
    logger.info("OAuth callback server: http://0.0.0.0:%s/oauth/callback", OAUTH_CALLBACK_PORT)

    # Запускаем бота
    logger.info("ZinaidaSergeevnaBot (multi-user) запущен...")
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()

    scheduler.shutdown()
    await runner.cleanup()


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
