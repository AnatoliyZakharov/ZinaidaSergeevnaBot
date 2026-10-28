"""
ZinaidaSergeevnaBot — голосовой агент для Google Календаря
Функции:
  1. Голосовое / текстовое сообщение → Yandex SpeechKit → свободные слоты
  2. Авто-добавление Яндекс Телемост ссылок к встречам без конференций
     (Google Meet не считается; сканируем ближайшие 7 дней каждые 3 часа
      и по команде «добавь ссылки»)

Переменные окружения (Railway / Fly.io):
  TELEGRAM_TOKEN         — токен бота от BotFather
  YANDEX_API_KEY         — API-ключ Яндекс Cloud (SpeechKit)
  YANDEX_FOLDER_ID       — ID папки в Яндекс Cloud
  YANDEX_TELEMOST_TOKEN  — OAuth-токен для Яндекс Телемост
  GOOGLE_SA_JSON         — JSON сервисного аккаунта Google (одной строкой)
  CALENDAR_ID            — ID календаря (обычно email@domain.com)
  TIMEZONE               — часовой пояс (например Europe/Saratov)
  WORK_START             — начало рабочего дня (по умолчанию 9)
  WORK_END               — конец рабочего дня (по умолчанию 19)
  ADMIN_CHAT_ID          — Telegram chat_id для авто-уведомлений (опционально)
"""

import asyncio
import os
import json
import logging
import re
from datetime import datetime, time, timedelta

import pytz
import requests
import dateparser
from dateparser.search import search_dates

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from telegram.request import HTTPXRequest
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ─── Логирование ────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Конфиг из переменных окружения ─────────────────────────
TELEGRAM_TOKEN         = os.environ["TELEGRAM_TOKEN"]
YANDEX_API_KEY         = os.environ["YANDEX_API_KEY"]
YANDEX_FOLDER          = os.environ["YANDEX_FOLDER_ID"]
YANDEX_TELEMOST_TOKEN  = os.environ.get("YANDEX_TELEMOST_TOKEN", "")
GOOGLE_SA_JSON         = os.environ["GOOGLE_SA_JSON"]
CALENDAR_ID            = os.environ.get("CALENDAR_ID", "primary")
TIMEZONE               = os.environ.get("TIMEZONE", "Europe/Moscow")
WORK_START             = int(os.environ.get("WORK_START", "9"))
WORK_END               = int(os.environ.get("WORK_END", "19"))
ADMIN_CHAT_ID          = os.environ.get("ADMIN_CHAT_ID", "")

# Права чтения И записи — нужны для добавления ссылок в события
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Паттерны конференций, которые мы считаем «уже есть ссылка»
# Google Meet НЕ включаем намеренно
CONFERENCE_PATTERNS = re.compile(
    r"(zoom\.us|teams\.microsoft\.com|webex\.com|whereby\.com|"
    r"gotomeeting\.com|bluejeans\.com|meet\.jit\.si|"
    r"telemost\.yandex\.ru|telemost\.yandex\.net)",
    re.IGNORECASE,
)


# ─── Google Calendar ─────────────────────────────────────────
def get_calendar_service():
    sa_info = json.loads(GOOGLE_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds)


def get_free_slots(target_date: datetime) -> list[str]:
    tz = pytz.timezone(TIMEZONE)
    service = get_calendar_service()

    work_start = tz.localize(
        datetime.combine(target_date.date(), time(WORK_START, 0))
    )
    work_end = tz.localize(
        datetime.combine(target_date.date(), time(WORK_END, 0))
    )

    body = {
        "timeMin": work_start.isoformat(),
        "timeMax": work_end.isoformat(),
        "items": [{"id": CALENDAR_ID}],
        "timeZone": TIMEZONE,
    }
    result = service.freebusy().query(body=body).execute()
    busy_raw = result["calendars"][CALENDAR_ID]["busy"]

    busy = []
    for p in sorted(busy_raw, key=lambda x: x["start"]):
        s = datetime.fromisoformat(p["start"].replace("Z", "+00:00")).astimezone(tz)
        e = datetime.fromisoformat(p["end"].replace("Z", "+00:00")).astimezone(tz)
        busy.append((s, e))

    free = []
    cur = work_start

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
    if h and m:
        dur = f"{h}ч {m}м"
    elif h:
        dur = f"{h}ч"
    else:
        dur = f"{m}м"
    return f"{start.strftime('%H:%M')} — {end.strftime('%H:%M')}  ({dur})"


# ─── Yandex SpeechKit ────────────────────────────────────────
def transcribe(audio_bytes: bytes) -> str:
    url = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
    resp = requests.post(
        url,
        headers={"Authorization": f"Api-Key {YANDEX_API_KEY}"},
        params={"lang": "ru-RU", "format": "oggopus", "folderId": YANDEX_FOLDER},
        data=audio_bytes,
        timeout=15,
    )
    if resp.status_code == 200:
        return resp.json().get("result", "")
    logger.error("SpeechKit %s: %s", resp.status_code, resp.text)
    return ""


# ─── Разбор даты ─────────────────────────────────────────────
def parse_date(text: str) -> datetime | None:
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    settings = {
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": now,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": TIMEZONE,
    }
    result = dateparser.parse(text, languages=["ru"], settings=settings)
    if result:
        return result
    found = search_dates(text, languages=["ru"], settings=settings)
    if found:
        return found[0][1]
    return None


def date_to_ru(dt: datetime) -> str:
    months = [
        "января","февраля","марта","апреля","мая","июня",
        "июля","августа","сентября","октября","ноября","декабря",
    ]
    days = ["пн","вт","ср","чт","пт","сб","вс"]
    return f"{dt.day} {months[dt.month - 1]} ({days[dt.weekday()]})"


# ─── Яндекс Телемост ─────────────────────────────────────────
def create_telemost_conference() -> str | None:
    """Создаём новую конференцию в Яндекс Телемост, возвращаем ссылку."""
    if not YANDEX_TELEMOST_TOKEN:
        logger.error("YANDEX_TELEMOST_TOKEN не задан")
        return None
    url = "https://cloud-api.yandex.net/v1/telemost-api/conferences"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"OAuth {YANDEX_TELEMOST_TOKEN}",
                "Content-Type": "application/json",
            },
            json={},
            timeout=15,
        )
    except requests.exceptions.ConnectionError as e:
        logger.error("Телемост: нет соединения — %s", e)
        return None
    except requests.exceptions.Timeout:
        logger.error("Телемост: таймаут")
        return None
    except Exception as e:
        logger.error("Телемост: неожиданная ошибка — %s", e)
        return None

    if resp.status_code in (200, 201):
        data = resp.json()
        link = data.get("join_url") or data.get("conferenceUri") or data.get("uri")
        if link:
            logger.info("Телемост: создана конференция %s", link)
            return link
        logger.error("Телемост: нет ссылки в ответе %s", data)
    else:
        logger.error("Телемост API %s: %s", resp.status_code, resp.text)
    return None


def has_conference_link(event: dict) -> bool:
    """True — у события уже есть ссылка на конференцию (не Google Meet)."""
    # Проверяем description и location
    text = " ".join([
        event.get("description") or "",
        event.get("location") or "",
    ])
    if CONFERENCE_PATTERNS.search(text):
        return True
    # Проверяем встроенные conferenceData (Zoom/Teams через интеграцию)
    conf = event.get("conferenceData") or {}
    entry_points = conf.get("entryPoints") or []
    for ep in entry_points:
        uri = ep.get("uri") or ""
        if CONFERENCE_PATTERNS.search(uri):
            return True
    return False


def get_events_next_7_days() -> list[dict]:
    """Получаем все события на ближайшие 7 дней из Google Calendar."""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=7)).isoformat()

    service = get_calendar_service()
    result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
        maxResults=100,
    ).execute()
    return result.get("items", [])


def add_telemost_to_event(event: dict, link: str) -> bool:
    """Добавляем ссылку Телемост в описание события. Возвращает True при успехе."""
    service = get_calendar_service()
    event_id = event["id"]
    old_desc = event.get("description") or ""

    telemost_block = f"\n\n🔗 Яндекс Телемост: {link}"
    new_desc = old_desc + telemost_block

    try:
        service.events().patch(
            calendarId=CALENDAR_ID,
            eventId=event_id,
            body={"description": new_desc},
        ).execute()
        logger.info("Добавлена ссылка в событие: %s", event.get("summary", event_id))
        return True
    except Exception as e:
        logger.error("Ошибка обновления события %s: %s", event_id, e)
        return False


def process_telemost_links() -> dict:
    """
    Основная логика:
      - Берём события на 7 дней
      - Отфильтровываем те, у которых нет конференции
      - Создаём Телемост и вшиваем ссылку
    Возвращает словарь {added: [...], skipped: int, errors: [...]}
    """
    result = {"added": [], "skipped": 0, "errors": []}

    try:
        events = get_events_next_7_days()
    except Exception as e:
        logger.error("Ошибка получения событий: %s", e)
        result["errors"].append(f"Не удалось получить события: {e}")
        return result

    for event in events:
        # Пропускаем события без участников (личные заметки) и отменённые
        if event.get("status") == "cancelled":
            continue

        # Пропускаем события без времени (весь день — обычно не митинг)
        start = event.get("start", {})
        if "date" in start and "dateTime" not in start:
            result["skipped"] += 1
            continue

        if has_conference_link(event):
            result["skipped"] += 1
            continue

        # Нужна ссылка — создаём конференцию
        link = create_telemost_conference()
        if not link:
            result["errors"].append(f"Не удалось создать Телемост для «{event.get('summary', '?')}»")
            continue

        if add_telemost_to_event(event, link):
            result["added"].append({
                "title": event.get("summary", "(без названия)"),
                "link": link,
            })
        else:
            result["errors"].append(f"Не удалось обновить событие «{event.get('summary', '?')}»")

    return result


def format_telemost_report(res: dict) -> str:
    """Форматируем отчёт для Telegram."""
    lines = []
    if res["added"]:
        lines.append(f"✅ Добавлено ссылок: {len(res['added'])}")
        for item in res["added"]:
            lines.append(f"  • {item['title']}")
    else:
        lines.append("ℹ️ Новых встреч без ссылок не найдено.")

    if res["skipped"]:
        lines.append(f"⏭ Пропущено (уже есть ссылка или весь день): {res['skipped']}")

    if res["errors"]:
        lines.append("⚠️ Ошибки:")
        for err in res["errors"]:
            lines.append(f"  • {err}")

    return "\n".join(lines)


# ─── Обработка запроса свободных слотов ──────────────────────
async def process_query(text: str, update: Update):
    target = parse_date(text)
    if not target:
        await update.message.reply_text(
            "Не понял дату 🤔\n\n"
            "Попробуй сказать:\n"
            "• «свободные слоты на завтра»\n"
            "• «что свободно в пятницу»\n"
            "• «есть время 3 ноября?»"
        )
        return

    try:
        slots = get_free_slots(target)
    except Exception as e:
        logger.error("Calendar error: %s", e)
        await update.message.reply_text("Ошибка при обращении к календарю 😔 Попробуй позже.")
        return

    date_str = date_to_ru(target)

    if slots:
        lines = "\n".join(f"• {s}" for s in slots)
        await update.message.reply_text(
            f"📅 *{date_str}* — свободное время:\n\n{lines}",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"📅 *{date_str}* — день полностью занят.\n"
            f"Нет окон от {WORK_START}:00 до {WORK_END}:00.",
            parse_mode="Markdown",
        )


# ─── Handlers ────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я ZinaidaSergeevnaBot.\n\n"
        "*Что умею:*\n"
        "📅 Показать свободные слоты в Google Календаре\n"
        "📌 Поставить встречу в календарь\n"
        "🔗 Добавить ссылку Яндекс Телемост к встречам без конференций\n\n"
        "*Примеры команд:*\n"
        "🎙 «Какие слоты свободны завтра?»\n"
        "🎙 «Есть время в четверг?»\n"
        "🎙 «Поставь встречу с командой в пятницу в 15:00»\n"
        "🎙 «Запланируй созвон завтра в 10 на 30 минут»\n"
        "✍️ «Свободно в пятницу»\n"
        "✍️ «Добавь ссылки на телемост»\n\n"
        "Ссылки Телемост также добавляются автоматически каждые 3 часа.",
        parse_mode="Markdown",
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

TIME_IN_TEXT = re.compile(
    r"\d{1,2}[:.]\d{2}|\bв\s+\d{1,2}\s*(час|утра|дня|вечера|ночи)?\b",
    re.IGNORECASE,
)


def parse_duration_minutes(text: str) -> int:
    """Извлекаем продолжительность из текста. По умолчанию 60 мин."""
    tl = text.lower()
    m = re.search(r"на\s+(\d+)\s*(минут|мин)\b", tl)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*(минут|мин)\b", tl)
    if m:
        return int(m.group(1))
    m = re.search(r"на\s+(\d+)\s*час", tl)
    if m:
        return int(m.group(1)) * 60
    if re.search(r"полчаса|пол\s*часа", tl):
        return 30
    if re.search(r"\bна\s+час\b", tl):
        return 60
    return 60


def extract_event_title(text: str) -> str:
    """Извлекаем название встречи, убирая командные слова, дату и время."""
    t = text
    # Убираем командные слова
    t = re.sub(
        r"(поставь|добавь|создай|запланируй|поставить|добавить|создать|запланировать)\s*",
        "", t, flags=re.IGNORECASE,
    )
    # Убираем продолжительность
    t = re.sub(r"на\s+\d+\s*(минут|мин|час[аов]?)\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\d+\s*(минут|мин)\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"на\s+(один\s+)?час\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"полчаса|пол\s*часа", "", t, flags=re.IGNORECASE)
    # Убираем дни недели
    t = re.sub(
        r"\b(в\s+)?(понедельник|вторник|среду?|четверг|пятниц[уа]|субботу?|воскресенье)\b",
        "", t, flags=re.IGNORECASE,
    )
    # Убираем относительные даты
    t = re.sub(r"\b(сегодня|завтра|послезавтра)\b", "", t, flags=re.IGNORECASE)
    # Убираем время
    t = re.sub(r"\bв\s+\d{1,2}[:.]\d{2}\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\bв\s+\d{1,2}\s*(час[аов]?|утра|дня|вечера|ночи)?\b", "", t, flags=re.IGNORECASE)
    # Убираем даты типа «3 ноября»
    t = re.sub(
        r"\d+\s+(января|февраля|марта|апреля|мая|июня|июля|августа"
        r"|сентября|октября|ноября|декабря)\b",
        "", t, flags=re.IGNORECASE,
    )
    t = re.sub(r"\s+", " ", t).strip(" ,.—-")
    return t if len(t) > 1 else "Встреча"


def create_calendar_event(
    title: str,
    start_dt: datetime,
    duration_minutes: int = 60,
    telemost_link: str | None = None,
) -> dict:
    """Создаём событие в Google Calendar. Если есть telemost_link — вшиваем в описание."""
    service = get_calendar_service()
    tz = pytz.timezone(TIMEZONE)
    if start_dt.tzinfo is None:
        start_dt = tz.localize(start_dt)
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    body = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE},
    }
    if telemost_link:
        body["description"] = f"🔗 Яндекс Телемост: {telemost_link}"
    return service.events().insert(calendarId=CALENDAR_ID, body=body).execute()


async def handle_create_event(text: str, update: Update):
    """Обрабатываем команду создания встречи."""
    msg = await update.message.reply_text("📅 Создаю встречу...")

    # Проверяем, что в тексте есть время
    if not TIME_IN_TEXT.search(text):
        await msg.edit_text(
            "Не понял время встречи 🕐\n\n"
            "Попробуй: «поставь встречу с командой в пятницу в 15:00»"
        )
        return

    # Убираем фразы длительности перед парсингом даты,
    # иначе dateparser трактует «на 30 минут» как сдвиг времени
    text_for_date = re.sub(
        r"\bна\s+\d+\s*(минут|мин|час[аов]?)\b", "", text, flags=re.IGNORECASE
    )
    text_for_date = re.sub(r"полчаса|пол\s*часа", "", text_for_date, flags=re.IGNORECASE)
    # Нормализуем «в 10» → «в 10:00» чтобы dateparser не игнорировал время
    text_for_date = re.sub(
        r"\bв\s+(\d{1,2})\b(?!\s*[:.]\d)",
        lambda m: f"в {m.group(1)}:00",
        text_for_date, flags=re.IGNORECASE,
    )

    dt = parse_date(text_for_date)
    if not dt:
        await msg.edit_text(
            "Не понял дату встречи 📅\n\n"
            "Попробуй: «запланируй созвон завтра в 10:00»"
        )
        return

    title = extract_event_title(text)
    duration = parse_duration_minutes(text)

    # Создаём Телемост-конференцию заранее (не блокирует — ошибка не критична)
    await msg.edit_text("📅 Создаю встречу и ссылку Телемост...")
    telemost_link = await asyncio.get_event_loop().run_in_executor(
        None, create_telemost_conference
    )

    try:
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: create_calendar_event(title, dt, duration, telemost_link)
        )
        tz = pytz.timezone(TIMEZONE)
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        end_dt = dt + timedelta(minutes=duration)

        telemost_line = f"\n🔗 {telemost_link}" if telemost_link else "\n⚠️ Телемост не удалось создать"
        await msg.edit_text(
            f"✅ *Встреча создана!*\n\n"
            f"📌 {title}\n"
            f"🗓 {date_to_ru(dt)}\n"
            f"🕐 {dt.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}"
            f"{telemost_line}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("Ошибка создания события: %s", e)
        await msg.edit_text("😔 Не удалось создать встречу. Попробуй позже.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🎙 Слушаю...")
    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)
    audio = await tg_file.download_as_bytearray()

    text = transcribe(bytes(audio))
    if not text:
        await msg.edit_text("Не удалось распознать голос. Попробуй ещё раз.")
        return

    await msg.edit_text(f"Ты сказал: «{text}»\n\n⏳ Обрабатываю...")

    if TELEMOST_TRIGGER.search(text):
        res = await asyncio.get_event_loop().run_in_executor(None, process_telemost_links)
        report = format_telemost_report(res)
        await update.message.reply_text(f"🔗 *Телемост-обход завершён*\n\n{report}", parse_mode="Markdown")
    elif CREATE_TRIGGER.search(text):
        await handle_create_event(text, update)
    else:
        await process_query(text, update)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if TELEMOST_TRIGGER.search(text):
        msg = await update.message.reply_text("⏳ Проверяю встречи и добавляю ссылки...")
        try:
            res = await asyncio.get_event_loop().run_in_executor(None, process_telemost_links)
            report = format_telemost_report(res)
            await msg.edit_text(f"🔗 *Телемост-обход завершён*\n\n{report}", parse_mode="Markdown")
        except Exception as e:
            logger.error("handle_text telemost error: %s", e)
            await msg.edit_text("😔 Произошла ошибка при обходе встреч. Попробуй позже.")
    elif CREATE_TRIGGER.search(text):
        await handle_create_event(text, update)
    else:
        await process_query(text, update)


# ─── Авто-задача (APScheduler) ───────────────────────────────
async def scheduled_telemost_job(bot: Bot):
    """Фоновая задача: добавляем Телемост ссылки каждые 3 часа."""
    logger.info("Scheduled: запускаю обход Телемост...")
    res = await asyncio.get_event_loop().run_in_executor(None, process_telemost_links)

    # Уведомляем администратора, если есть ADMIN_CHAT_ID
    if ADMIN_CHAT_ID and res["added"]:
        report = format_telemost_report(res)
        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🔗 *Авто-обход Телемост*\n\n{report}",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error("Не удалось отправить авто-уведомление: %s", e)

    logger.info(
        "Scheduled: добавлено=%d, пропущено=%d, ошибок=%d",
        len(res["added"]), res["skipped"], len(res["errors"])
    )


# ─── Запуск ──────────────────────────────────────────────────
async def run_bot():
    request = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0,
    )
    app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # APScheduler: каждые 3 часа добавляем Телемост-ссылки
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_telemost_job,
        "interval",
        hours=3,
        args=[app.bot],
        id="telemost_auto",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler запущен: Телемост-обход каждые 3 часа")

    logger.info("ZinaidaSergeevnaBot запущен...")
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()   # работает бесконечно
        await app.updater.stop()
        await app.stop()

    scheduler.shutdown()


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
