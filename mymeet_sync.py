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
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
MYMEET_TOKEN      = os.environ.get("MYMEET_TOKEN",  "mm_RFt7vMJN1zTLCcZkTx4dfDs-8peN3vb6i7Xs8WFMA1I")
MYMEET_WS_ID      = os.environ.get("MYMEET_WS_ID",  "4e9bb2c4-7382-4c86-903f-15b580dfcf82")
MYMEET_BASE       = "https://backend.mymeet.ai"
SPREADSHEET_ID    = os.environ.get("MYMEET_SPREADSHEET_ID", "1uDGE4qpDKML_2ZJernysRN0eZWzSu6MikmRFuqfUocs")
ORGANIZER_EMAIL   = os.environ.get("ORGANIZER_EMAIL", "ztstroy@gmail.com")

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


# ── Write-rate limiter ────────────────────────────────────────────────────────
# Google Sheets API: 60 write requests/min/user.  We use a simple sliding-window
# limiter so the sync never issues more than ~50 writes per 60 s.

_WRITE_WINDOW  = 60.0   # seconds
_WRITE_MAX     = 50     # requests per window (keep some head-room)
_write_times: list = [] # timestamps of recent writes (module-level, single-threaded)


def _throttle():
    """Block until another write is allowed by the rate limit."""
    now = time.monotonic()
    # Evict timestamps older than the window
    while _write_times and now - _write_times[0] >= _WRITE_WINDOW:
        _write_times.pop(0)
    if len(_write_times) >= _WRITE_MAX:
        wait = _WRITE_WINDOW - (now - _write_times[0]) + 0.2
        logger.debug("Rate-limit: sleeping %.1fs before next write", wait)
        time.sleep(wait)
        now = time.monotonic()
        while _write_times and now - _write_times[0] >= _WRITE_WINDOW:
            _write_times.pop(0)
    _write_times.append(time.monotonic())


# ── Retry helper ──────────────────────────────────────────────────────────────

def _gs_call(fn, *args, retries: int = 7, **kwargs):
    """Call a gspread method with rate limiting + exponential backoff on 429/503."""
    _throttle()
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(e, "response", None)
            code = status.status_code if status else 0
            if code in (429, 503) and attempt < retries - 1:
                wait = 2 ** (attempt + 1)   # 2 4 8 16 32 64 s
                logger.warning("Sheets quota hit (attempt %d/%d), waiting %ds…",
                               attempt + 1, retries, wait)
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("unreachable")


# ── Google Sheets client ──────────────────────────────────────────────────────

def _gs_client() -> gspread.Client:
    creds_json = os.environ.get("GOOGLE_SA_KEY")
    if not creds_json:
        raise RuntimeError("GOOGLE_SA_KEY env var is not set")
    creds_data = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_data, scopes=GS_SCOPES)
    return gspread.authorize(creds)


def _batch_format_freeze(spreadsheet, ws, num_cols: int = 6) -> None:
    """Apply header format + freeze row 1 in a single Sheets batch_update call (saves 1 API op)."""
    try:
        sid = ws.id
        _gs_call(spreadsheet.batch_update, {"requests": [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sid,
                        "startRowIndex": 0, "endRowIndex": 1,
                        "startColumnIndex": 0, "endColumnIndex": num_cols,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {
                                "bold": True, "fontSize": 10,
                                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                            },
                            "backgroundColor": {"red": 0.204, "green": 0.396, "blue": 0.643},
                            "horizontalAlignment": "CENTER",
                        }
                    },
                    "fields": "userEnteredFormat",
                }
            },
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sid,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            },
        ]})
    except Exception as e:
        logger.debug("Batch format/freeze failed: %s", e)


# ── MYmeet API ────────────────────────────────────────────────────────────────

def _mymeet_headers(token: str | None = None) -> dict:
    return {"X-API-KEY": token or MYMEET_TOKEN}


def fetch_workspaces(token: str) -> list:
    """Запрашиваем список рабочих пространств MYmeet по токену."""
    for endpoint in ("/api/workspaces", "/api/user/workspaces", "/api/workspace"):
        try:
            resp = requests.get(
                f"{MYMEET_BASE}{endpoint}",
                headers=_mymeet_headers(token),
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else (
                    data.get("items") or data.get("workspaces") or data.get("data") or []
                )
                if items:
                    return items
        except Exception:
            pass
    return []


def _fetch_meetings(days: int = 30, token: str | None = None, ws_id: str | None = None) -> list:
    """Return list of raw meeting objects for the past N days."""
    _token = token or MYMEET_TOKEN
    _ws_id = ws_id or MYMEET_WS_ID

    now       = datetime.now(timezone.utc)
    date_to   = int(now.timestamp() * 1000)
    date_from = int((now - timedelta(days=days)).timestamp() * 1000)

    meetings, page = [], 0
    while True:
        url    = f"{MYMEET_BASE}/api/workspaces/{_ws_id}/all-meetings"
        params = {"page": page, "perPage": 50, "dateFrom": date_from, "dateTo": date_to}
        resp   = requests.get(url, headers=_mymeet_headers(_token), params=params, timeout=30)
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


def _fetch_protocol(meeting_id: str, token: str | None = None) -> dict | None:
    url    = f"{MYMEET_BASE}/api/storage/download"
    params = {"meeting_id": meeting_id, "format": "json"}
    try:
        resp = requests.get(url, headers=_mymeet_headers(token), params=params, timeout=30)
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


def _is_organizer(m: dict, organizer_email: str | None = None) -> bool:
    """Return True if the current user is the organizer of this meeting.

    MYmeet embeds Google Calendar event data, where the organizer field looks like:
      {"email": "user@example.com", "self": true}
    We check several possible locations for this structure.
    """
    _email = organizer_email or ORGANIZER_EMAIL

    def _check_organizer_obj(obj) -> bool | None:
        if not isinstance(obj, dict):
            return None
        if obj.get("self"):
            return True
        email = obj.get("email", "")
        if email and email.lower() == _email.lower():
            return True
        if email:          # organizer field exists but it's someone else
            return False
        return None        # organizer field absent/empty — keep looking

    # Try direct and nested organizer fields
    for path in (
        ("organizer",),
        ("calendarEvent", "organizer"),
        ("event", "organizer"),
        ("calendar", "organizer"),
        ("meetingData", "organizer"),
    ):
        obj = m
        for key in path:
            obj = obj.get(key) if isinstance(obj, dict) else None
        result = _check_organizer_obj(obj)
        if result is not None:
            return result

    # Boolean shortcut fields some APIs use
    for field in ("isOrganizer", "is_organizer", "isMy", "isMine", "isOwner"):
        val = m.get(field)
        if val is not None:
            return bool(val)

    # Role-based fields
    role = (m.get("role") or m.get("userRole") or m.get("myRole") or "").lower()
    if role in ("organizer", "owner", "host", "creator"):
        return True
    if role in ("attendee", "guest", "participant"):
        return False

    # Can't determine — include the meeting and log for later inspection
    logger.debug("_is_organizer: could not determine for %s, keys=%s",
                 m.get("id", "?"), list(m.keys()))
    return True


def _build_user_config(user_config: dict | None) -> dict:
    """Merge user_config with module-level defaults."""
    cfg = user_config or {}
    return {
        "token":    cfg.get("mymeet_token")          or MYMEET_TOKEN,
        "ws_id":    cfg.get("mymeet_ws_id")          or MYMEET_WS_ID,
        "sheet_id": cfg.get("mymeet_spreadsheet_id") or SPREADSHEET_ID,
        "email":    cfg.get("mymeet_organizer_email") or ORGANIZER_EMAIL,
    }


GROUPER_FORMAT = {
    "textFormat": {"bold": True, "fontSize": 10},
    "backgroundColor": {"red": 0.878, "green": 0.878, "blue": 0.878},
}


# ── Core sync ─────────────────────────────────────────────────────────────────

def sync_to_sheets(days: int = 30, append: bool = False, user_config: dict | None = None) -> str:
    """
    Fetch MYmeet data and write to Google Sheets.

    days=30, append=False  — full overwrite (manual /sync_mymeet command)
    days=1,  append=True   — append yesterday's new tasks (daily scheduler)
    user_config            — per-user dict with mymeet_token, mymeet_ws_id,
                             mymeet_spreadsheet_id, mymeet_organizer_email
    """
    cfg = _build_user_config(user_config)
    logger.info("MYmeet sync started (days=%d, append=%s, ws=%s)", days, append, cfg["ws_id"])

    raw_meetings = _fetch_meetings(days=days, token=cfg["token"], ws_id=cfg["ws_id"])
    logger.info("Fetched %d raw meetings", len(raw_meetings))

    raw_meetings = [m for m in raw_meetings if _is_organizer(m, cfg["email"])]
    logger.info("After organizer filter: %d meetings", len(raw_meetings))

    # ── Параллельная загрузка протоколов ──────────────────────────────────────
    def _fetch_one(m: dict) -> dict:
        mid  = m.get("id") or m.get("meetingId") or m.get("meeting_id") or ""
        name = (m.get("name") or m.get("title") or m.get("meetingName") or "Без названия").strip()
        date = m.get("startTime") or m.get("date") or m.get("createdAt") or ""
        protocol = _fetch_protocol(mid, token=cfg["token"]) if mid else None
        return {
            "id":    mid,
            "name":  name,
            "date":  _fmt_date(date),
            "tasks": _extract_tasks(protocol),
        }

    all_meetings = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one, m): m for m in raw_meetings}
        for fut in as_completed(futures):
            try:
                all_meetings.append(fut.result())
            except Exception as e:
                logger.debug("Protocol fetch error: %s", e)

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
    spreadsheet = gc.open_by_key(cfg["sheet_id"])
    sheets_written = 0

    HEADER_ROW = ["№", "Дата", "Задача", "Ответственный", "Срок", "Статус"]

    # ── Recurring meetings — one sheet per name ───────────────────────────────
    for meeting_name, meetings in sorted(recurring.items()):
        sheet_name = _safe_sheet_name(meeting_name)
        try:
            ws = spreadsheet.worksheet(sheet_name)
            if not append:
                _gs_call(ws.clear)
        except gspread.WorksheetNotFound:
            ws = _gs_call(spreadsheet.add_worksheet, title=sheet_name, rows=1000, cols=10)

        if not append:
            # Build task rows
            rows = []
            idx  = 1
            for m in sorted(meetings, key=lambda x: x["date"]):
                for task in m["tasks"]:
                    rows.append([
                        idx, m["date"], task["name"], task["assignee"],
                        task["deadline"],
                        "Выполнено" if task["done"] else "Требует верификации",
                    ])
                    idx += 1
            # Single update: header + data together (saves 1 API call vs separate header update)
            all_data = [HEADER_ROW] + rows
            _gs_call(ws.update, f"A1:F{len(all_data)}", all_data)
            # Format header + freeze in one batch call (saves 1 API call vs format+freeze separately)
            _batch_format_freeze(spreadsheet, ws)
        else:
            # append: find last filled row
            existing = _gs_call(ws.get_all_values)
            if not existing:
                rows = []
                idx  = 1
                for m in sorted(meetings, key=lambda x: x["date"]):
                    for task in m["tasks"]:
                        rows.append([
                            idx, m["date"], task["name"], task["assignee"],
                            task["deadline"],
                            "Выполнено" if task["done"] else "Требует верификации",
                        ])
                        idx += 1
                all_data = [HEADER_ROW] + rows
                _gs_call(ws.update, f"A1:F{len(all_data)}", all_data)
                _batch_format_freeze(spreadsheet, ws)
            else:
                start_idx = len(existing)
                start_row = len(existing) + 1
                rows = []
                idx  = start_idx
                for m in sorted(meetings, key=lambda x: x["date"]):
                    for task in m["tasks"]:
                        rows.append([
                            idx, m["date"], task["name"], task["assignee"],
                            task["deadline"],
                            "Выполнено" if task["done"] else "Требует верификации",
                        ])
                        idx += 1
                if rows:
                    _gs_call(ws.update, f"A{start_row}:F{start_row - 1 + len(rows)}", rows)
        sheets_written += 1
        # No explicit sleep — rate limiter in _gs_call handles quota

    # ── One-time meetings ─────────────────────────────────────────────────────
    # Format: № | Дата | Задача | Ответственный | Срок | Статус
    # Bold grouper row with meeting name before each meeting's tasks.
    ot_name = "Разовые встречи"
    try:
        ws = spreadsheet.worksheet(ot_name)
        if not append:
            _gs_call(ws.clear)
    except gspread.WorksheetNotFound:
        ws = _gs_call(spreadsheet.add_worksheet, title=ot_name, rows=2000, cols=10)

    if not append:
        start_idx = 1
        start_row = 2
        new_sheet  = True
    else:
        existing = _gs_call(ws.get_all_values)
        if not existing:
            start_idx = 1
            start_row = 2
            new_sheet  = True
        else:
            start_idx = len(existing)
            start_row = len(existing) + 1
            new_sheet  = False

    # Build rows list; track grouper row positions (0-based relative to start_row)
    all_rows: list = []
    grouper_rel_positions: list[int] = []
    idx = start_idx

    for m in one_time:
        if not m["tasks"]:
            continue
        label = f"{m['name']}  ({m['date']})" if m["date"] else m["name"]
        grouper_rel_positions.append(len(all_rows))
        all_rows.append([label, "", "", "", "", ""])
        for task in m["tasks"]:
            all_rows.append([
                idx,
                m["date"],
                task["name"],
                task["assignee"],
                task["deadline"],
                "Выполнено" if task["done"] else "Требует верификации",
            ])
            idx += 1

    if new_sheet:
        # Combine header + data in single write (saves 1 API call)
        write_data = [HEADER_ROW] + all_rows
        if write_data:
            _gs_call(ws.update, f"A1:F{len(write_data)}", write_data)
        # Format+freeze header in single batch call (saves 1 API call)
        _batch_format_freeze(spreadsheet, ws)
    elif all_rows:
        _gs_call(ws.update, f"A{start_row}:F{start_row - 1 + len(all_rows)}", all_rows)

    # Batch-format all grouper rows in a single API call (saves N-1 API calls)
    if all_rows and grouper_rel_positions:
        sid = ws.id
        grouper_requests = []
        for rel in grouper_rel_positions:
            abs_row = start_row + rel - 1  # 0-based row index
            grouper_requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sid,
                        "startRowIndex": abs_row, "endRowIndex": abs_row + 1,
                        "startColumnIndex": 0, "endColumnIndex": 6,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"bold": True, "fontSize": 10},
                            "backgroundColor": {"red": 0.878, "green": 0.878, "blue": 0.878},
                        }
                    },
                    "fields": "userEnteredFormat",
                }
            })
        try:
            _gs_call(spreadsheet.batch_update, {"requests": grouper_requests})
        except Exception as e:
            logger.debug("Grouper batch format failed: %s", e)

    result = (
        f"✅ Синхронизировано: {total_tasks} задач\n"
        f"📋 {sheets_written} листов по регулярным встречам\n"
        f"📝 Разовые встречи: {len(one_time)} встреч"
    )
    logger.info("MYmeet sync done: %s", result.replace('\n', ' | '))
    return result


# ── Async wrapper ─────────────────────────────────────────────────────────────

async def sync_to_sheets_async(
    days: int = 30,
    append: bool = False,
    user_config: dict | None = None,
) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: sync_to_sheets(days=days, append=append, user_config=user_config),
    )
