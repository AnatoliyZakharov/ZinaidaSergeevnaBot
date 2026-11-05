"""
MYmeet → Google Sheets sync module.

Env vars required:
  MYMEET_TOKEN   — MYmeet API key
  GOOGLE_SA_KEY  — Google service account JSON (single-line, full content)

Usage:
  from mymeet_sync import sync_to_sheets
  result = await sync_to_sheets()   # returns status string
"""

import asyncio
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
MYMEET_TOKEN  = os.environ.get("MYMEET_TOKEN",  "MYMEET_TOKEN_REMOVED")
MYMEET_WS_ID  = os.environ.get("MYMEET_WS_ID",  "4e9bb2c4-7382-4c86-903f-15b580dfcf82")
MYMEET_BASE   = "https://backend.mymeet.ai"
SPREADSHEET_ID = os.environ.get("MYMEET_SPREADSHEET_ID", "1uDGE4qpDKML_2ZJernysRN0eZWzSu6MikmRFuqfUocs")

GS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ── Google Sheets client ──────────────────────────────────────────────────────

def _gs_client() -> gspread.Client:
    creds_json = os.environ.get("GOOGLE_SA_KEY")
    if creds_json:
        creds_data = json.loads(creds_json)
    else:
        raise RuntimeError("GOOGLE_SA_KEY env var is not set")
    creds = Credentials.from_service_account_info(creds_data, scopes=GS_SCOPES)
    return gspread.authorize(creds)


# ── MYmeet API ────────────────────────────────────────────────────────────────

def _mymeet_headers() -> dict:
    return {"X-API-KEY": MYMEET_TOKEN}


def _fetch_meetings() -> list:
    """Return list of raw meeting objects for the past 30 days."""
    now      = datetime.now(timezone.utc)
    date_to  = int(now.timestamp() * 1000)
    date_from = int((now - timedelta(days=30)).timestamp() * 1000)

    meetings, page = [], 0
    while True:
        url    = f"{MYMEET_BASE}/api/workspaces/{MYMEET_WS_ID}/all-meetings"
        params = {"page": page, "perPage": 50, "dateFrom": date_from, "dateTo": date_to}
        resp   = requests.get(url, headers=_mymeet_headers(), params=params, timeout=30)
        resp.raise_for_status()
        data  = resp.json()

        # API may wrap items in different keys depending on version
        items = (data.get("followups") or data.get("items") or
                 data.get("meetings") or data.get("data") or [])
        if not items:
            break
        meetings.extend(items)

        total = data.get("total", 0) or data.get("totalCount", 0)
        if total and len(meetings) >= total:
            break
        if len(items) < 50:
            break
        page += 1

    return meetings


def _fetch_protocol(meeting_id: str) -> dict | None:
    url    = f"{MYMEET_BASE}/api/storage/download"
    params = {"meeting_id": meeting_id, "format": "json"}
    try:
        resp = requests.get(url, headers=_mymeet_headers(), params=params, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug("Protocol fetch failed for %s: %s", meeting_id, e)
        return None


def _extract_tasks(protocol: dict | None) -> list:
    if not protocol:
        return []
    tasks = []
    try:
        templates = (
            protocol.get("followup_v2", {})
                    .get("templates", [])
        )
        for tmpl in templates:
            for entity in tmpl.get("entities", []):
                if entity.get("name") != "tasks":
                    continue
                raw = entity.get("html_text", [])
                items = raw if isinstance(raw, list) else json.loads(raw or "[]")
                for item in items:
                    tasks.append({
                        "name":     (item.get("task") or item.get("name") or
                                     item.get("text") or "").strip(),
                        "assignee": (item.get("assignee") or
                                     item.get("responsible") or "").strip(),
                        "deadline": (item.get("deadline") or
                                     item.get("due_date") or "").strip(),
                        "done":     bool(item.get("done") or item.get("is_done") or
                                         item.get("status") == "done"),
                    })
    except Exception as e:
        logger.debug("Task extraction error: %s", e)
    return tasks


def _fmt_date(raw) -> str:
    if not raw:
        return ""
    s = str(raw).strip()
    for fmt in (
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s.replace(" GMT", ""), fmt.replace(" GMT", "")).strftime("%d.%m.%Y")
        except ValueError:
            pass
    # epoch ms
    try:
        ts = int(s)
        if ts > 1e10:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%Y")
    except Exception:
        pass
    return s


def _safe_sheet_name(name: str) -> str:
    """Google Sheets tab name: no special chars, max 100 chars."""
    s = re.sub(r"[\\/?*\[\]:]", "-", name)
    return s[:100].strip()


# ── Core sync ─────────────────────────────────────────────────────────────────

def sync_to_sheets() -> str:
    """
    Fetch MYmeet data for the past 30 days and write to Google Sheets.
    Returns a human-readable result string.
    """
    logger.info("MYmeet sync started")

    # 1. Fetch all meetings + protocols
    raw_meetings = _fetch_meetings()
    logger.info("Fetched %d raw meetings", len(raw_meetings))

    all_meetings = []
    for m in raw_meetings:
        mid  = m.get("id") or m.get("meetingId") or m.get("meeting_id") or ""
        name = (m.get("name") or m.get("title") or m.get("meetingName") or "Без названия").strip()
        date = m.get("startTime") or m.get("date") or m.get("createdAt") or ""

        protocol = _fetch_protocol(mid) if mid else None
        tasks    = _extract_tasks(protocol)

        all_meetings.append({
            "id":    mid,
            "name":  name,
            "date":  _fmt_date(date),
            "tasks": tasks,
        })

    total_tasks = sum(len(m["tasks"]) for m in all_meetings)
    logger.info("Total tasks: %d", total_tasks)

    # 2. Identify recurring meeting names
    name_counts   = Counter(m["name"] for m in all_meetings)
    recurring_set = {n for n, c in name_counts.items() if c > 1}

    # 3. Split into recurring / one-time
    recurring: dict[str, list] = {}
    one_time:  list            = []

    for m in all_meetings:
        if not m["tasks"]:
            continue
        if m["name"] in recurring_set:
            recurring.setdefault(m["name"], []).append(m)
        else:
            one_time.append(m)

    # 4. Write to spreadsheet
    gc           = _gs_client()
    spreadsheet  = gc.open_by_key(SPREADSHEET_ID)
    sheets_written = 0

    # Recurring: one sheet per meeting name
    for meeting_name, meetings in sorted(recurring.items()):
        sheet_name = _safe_sheet_name(meeting_name)
        try:
            ws = spreadsheet.worksheet(sheet_name)
            ws.clear()
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=10)

        ws.update("A1:F1", [["№", "Дата", "Задача", "Ответственный", "Срок", "Статус"]])

        rows, idx = [], 1
        for m in sorted(meetings, key=lambda x: x["date"]):
            for task in m["tasks"]:
                rows.append([
                    idx,
                    m["date"],
                    task["name"],
                    task["assignee"],
                    task["deadline"],
                    "Выполнено" if task["done"] else "Требует верификации",
                ])
                idx += 1

        if rows:
            ws.update(f"A2:F{1 + len(rows)}", rows)
        sheets_written += 1

    # One-time: single sheet
    ot_name = "Разовые встречи"
    try:
        ws = spreadsheet.worksheet(ot_name)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=ot_name, rows=2000, cols=10)

    ws.update("A1:E1", [["№", "Задача", "Ответственный", "Срок", "Статус"]])

    rows, idx = [], 1
    for m in one_time:
        for task in m["tasks"]:
            rows.append([
                idx,
                task["name"],
                task["assignee"],
                task["deadline"],
                "Выполнено" if task["done"] else "Требует верификации",
            ])
            idx += 1

    if rows:
        ws.update(f"A2:E{1 + len(rows)}", rows)

    result = (
        f"✅ Синхронизировано: {total_tasks} задач\n"
        f"📋 {sheets_written} листов по регулярным встречам\n"
        f"📝 Разовые встречи: {len(one_time)} встреч"
    )
    logger.info("MYmeet sync done: %s", result.replace('\n', ' | '))
    return result


# ── Async wrapper (for use from python-telegram-bot handlers) ─────────────────

async def sync_to_sheets_async() -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, sync_to_sheets)
