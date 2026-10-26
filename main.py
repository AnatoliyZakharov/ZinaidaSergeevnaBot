"""
Голосовой агент: свободные слоты в календаре
Голосовое сообщение → Yandex SpeechKit → Google Calendar → текст

Требуемые переменные окружения (Railway):
  TELEGRAM_TOKEN      — токен бота от BotFather
  YANDEX_API_KEY      — API-ключ Яндекс Cloud
  YANDEX_FOLDER_ID    — ID папки в Яндекс Cloud
  GOOGLE_SA_JSON      — JSON сервисного аккаунта Google (одной строкой)
  CALENDAR_ID         — ID календаря (обычно email@gmail.com)
  TIMEZONE            — часовой пояс (например Europe/Saratov)
  WORK_START          — начало рабочего дня (по умолчанию 9)
  WORK_END            — конец рабочего дня (по умолчанию 19)
"""

import asyncio
import os
import json
import logging
from datetime import datetime, time
import pytz
import requests
import dateparser

from telegram import Update
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
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
YANDEX_API_KEY  = os.environ["YANDEX_API_KEY"]
YANDEX_FOLDER   = os.environ["YANDEX_FOLDER_ID"]
GOOGLE_SA_JSON  = os.environ["GOOGLE_SA_JSON"]   # JSON одной строкой
CALENDAR_ID     = os.environ.get("CALENDAR_ID", "primary")
TIMEZONE        = os.environ.get("TIMEZONE", "Europe/Moscow")
WORK_START      = int(os.environ.get("WORK_START", "9"))
WORK_END        = int(os.environ.get("WORK_END", "19"))

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


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

    # Сортируем занятые периоды
    busy = []
    for p in sorted(busy_raw, key=lambda x: x["start"]):
        s = datetime.fromisoformat(p["start"].replace("Z", "+00:00")).astimezone(tz)
        e = datetime.fromisoformat(p["end"].replace("Z", "+00:00")).astimezone(tz)
        busy.append((s, e))

    # Считаем свободные окна (минимум 30 минут)
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
    return dateparser.parse(
        text,
        languages=["ru"],
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": TIMEZONE,
        },
    )


def date_to_ru(dt: datetime) -> str:
    months = [
        "января","февраля","марта","апреля","мая","июня",
        "июля","августа","сентября","октября","ноября","декабря",
    ]
    days = ["пн","вт","ср","чт","пт","сб","вс"]
    return f"{dt.day} {months[dt.month - 1]} ({days[dt.weekday()]})"


# ─── Обработка запроса ───────────────────────────────────────
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
        "👋 Привет! Я проверю твой Google Календарь и скажу, когда ты свободен.\n\n"
        "Отправь *голосовое* или *текстовое* сообщение:\n"
        "🎙 «Какие слоты свободны завтра?»\n"
        "🎙 «Есть время в четверг?»\n"
        "✍️ «Свободно в пятницу»",
        parse_mode="Markdown",
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🎙 Слушаю...")
    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)
    audio = await tg_file.download_as_bytearray()

    text = transcribe(bytes(audio))
    if not text:
        await msg.edit_text("Не удалось распознать голос. Попробуй ещё раз.")
        return

    await msg.edit_text(f"Ты сказал: «{text}»\n\n⏳ Смотрю календарь...")
    await process_query(text, update)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_query(update.message.text, update)


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
    logger.info("Bot is running...")
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()  # работает бесконечно
        await app.updater.stop()
        await app.stop()


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
