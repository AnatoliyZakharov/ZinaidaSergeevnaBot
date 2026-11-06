"""
MYmeet → Google Sheets sync module.

Env vars required:
  MYMEET_TOKEN   — MYmeet API key
  GOOGLE_SA_KEY  — Google service account JSON (single-line, full content)

Usage:
  from mymeet_sync import sync_to_sheets_async
  result = await sync_to_sheets_async()          # full 30-day overwrite
  result = await sync_to_sheets_async(days=1, append=True)  # daily append
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
MYMEET_TOKEN   = os.environ.get("MYMEET_TOKEN",  "mm_RFt7vMJN1zTLCcZkTx4dfDs-8peN3vb6i7Xs8WFMA1I")
MYMEET_WS_ID   = os.environ.get("MYMEET_WS_ID",  "4e9bb2c4-7382-4c86-903f-15b580dfcf82")
MYMEET_BASE    = "https://backend.mymeet.ai"
SPREADSHEET_ID = os.environ.get("MYMEET_SPREADSHEET_ID", "1uDGE4qpDKML_2ZJernysRN0eZWzSu6MikmRFuqfUocs")

GS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADER_FORMAT = {
    "textFormat": {"bold": True, "fontSize": 10},
    "backgroundColor": {"red": 0.204, "green": 0.396, "blue": 0.643},
    "horizontalAlignment": "CENTER",
}
HEADER_TEXT_FORMAT = {
    "textFormat": {"bold": True, "fontSize": 10, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    "backgroundColor": {"red": 0.204, "green": 0.396, "blue": 0.643},
    "horizontalAlignment": "CENTER",
}


# ── Google Sheets client ──────────────────────────────────────────────────────

def _gs_client() -> gspread.Client:
    creds_json = os.environ.get("GOOGLE_SA_KEY")
    if not creds_json:
        raise RuntimeError("GOOGLE_SA_KEY env var is not set")
    creds_data = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_data, scopes=GS_SCOPES)
    return gspread.authorize(creds)


def _format_header(ws, col_range: str) -> None:
    """Apply bold blue formatting to header row and freeze it."""
    try:
        ws.format(col_range, HEADER_TEXT_FORMAT)
        ws.freeze(rows=1)
    except Exception as e:
        logger.debug("Header formatting failed: %s", e)


# ── MYmeet API ────────────────────────────────────────────────────────────────

def _mymeet_headers() -> dict:
    return {"X-API-KEY": MYMEET_TOKEN}


def _fetch_meetings(days: int = 30) -> list:
    """Return list of raw meeting objects for the past N days."""
    now       = datetime.now(timezone.utc)
    date_to   = int(now.timestamp() * 1000)
    date_from = int((now - timedelta(days=days)).timestamp() * 1000)

    meetings, page = [], 0
    while True:
        url    = f"{MYMEET_BASE}/api/workspaces/{MYMEET_WS_ID}/all-meetings"
        params = {"page": page, "perPage": 50, "dateFrom": date_from, "dateTo": date_to}
        resp   = requests.get(url, headers=_mymeet_headers(), params=params, timeout=30)
        resp.raise_for_status()
        data   = resp.json()

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
                raw   = entity.get("html_text", [])
                items = raw if isinstance(raw, list) else json.loads(raw or "[]")
                for item in items:
                    # assignee is an object {"speaker": "Name", ...}
                    assignee_raw = item.get("assignee") or item.get("responsible") or {}
                    if isinstance(assignee_raw, dict):
                        assignee = assignee_raw.get("speaker", "").strip()
                    else:
                        assignee = str(assignee_raw).strip()

                    tasks.append({
                        "id":       item.get("id", ""),
                        "name":     (item.get("task") or item.get("name") or
                                     item.get("text") or "").strip(),
                        "assignee": assignee,
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
    try:
        ts = int(s)
        if ts > 1e10:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%Y")
    except Exception:
        pass
    return s


def _safe_sheet_name(name: str) -> str:
    s = re.sub(r"[\\/?*\[\]:]", "-", name)
    return s[:100].strip()


# ── Core sync ─────────────────────────────────────────────────────────────────

def sync_to_sheets(days: int = 30, append: bool = False) -> str:
    """
    Fetch MYmeet data and write to Google Sheets.

    days=30, append=False  — full overwrite (manual /sync_mymeet command)
    days=1,  append=True   — append yesterday's new tasks (daily scheduler)
    """
    logger.info("MYmeet sync started (days=%d, append=%s)", days, append)

    raw_meetings = _fetch_meetings(days=days)
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

    name_counts   = Counter(m["name"] for m in all_meetings)
    recurring_set = {n for n, c in name_counts.items() if c > 1}

    recurring: dict[str, list] = {}
    one_time:  list            = []

    for m in all_meetings:
        if not m["tasks"]:
            continue
        if m["name"] in recurring_set:
            recurring.setdefault(m["name"], []).append(m)
        else:
            one_time.append(m)

    gc          = _gs_client()
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    sheets_written = 0

    # ── Recurring meetings — one sheet per name ───────────────────────────────
    for meeting_name, meetings in sorted(recurring.items()):
        sheet_name = _safe_sheet_name(meeting_name)
        try:
            ws = spreadsheet.worksheet(sheet_name)
            if not append:
                ws.clear()
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=10)

        if not append:
            ws.update("A1:F1", [["№", "Дата", "Задача", "Ответственный", "Срок", "Статус"]])
            _format_header(ws, "A1:F1")
            start_idx = 1
            start_row = 2
        else:
            # append: find last filled row
            existing = ws.get_all_values()
            if not existing:
                ws.update("A1:F1", [["№", "Дата", "Задача", "Ответственный", "Срок", "Статус"]])
                _format_header(ws, "A1:F1")
                start_idx = 1
                start_row = 2
            else:
                start_idx = len(existing)  # last № + 1
                start_row = len(existing) + 1

        rows = []
        idx  = start_idx
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
            ws.update(f"A{start_row}:F{start_row - 1 + len(rows)}", rows)
        sheets_written += 1

    # ── One-time meetings ─────────────────────────────────────────────────────
    ot_name = "Разовые встречи"
    try:
        ws = spreadsheet.worksheet(ot_name)
        if not append:
            ws.clear()
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=ot_name, rows=2000, cols=10)

    if not append:
        ws.update("A1:E1", [["№", "Задача", "Ответственный", "Срок", "Статус"]])
        _format_header(ws, "A1:E1")
        start_idx = 1
        start_row = 2
    else:
        existing = ws.get_all_values()
        if not existing:
            ws.update("A1:E1", [["№", "Задача", "Ответственный", "Срок", "Статус"]])
            _format_header(ws, "A1:E1")
            start_idx = 1
            start_row = 2
        else:
            start_idx = len(existing)
            start_row = len(existing) + 1

    rows = []
    idx  = start_idx
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
        ws.update(f"A{start_row}:E{start_row - 1 + len(rows)}", rows)

    result = (
        f"✅ Синхронизировано: {total_tasks} задач\n"
        f"📋 {sheets_written} листов по регулярным встречам\n"
        f"📝 Разовые встречи: {len(one_time)} встреч"
    )
    logger.info("MYmeet sync done: %s", result.replace('\n', ' | '))
    return result


# ── Async wrapper ─────────────────────────────────────────────────────────────

async def sync_to_sheets_async(days: int = 30, append: bool = False) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: sync_to_sheets(days=days, append=append))
