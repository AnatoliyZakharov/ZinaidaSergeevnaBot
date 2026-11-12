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
MYMEET_TOKEN      = os.environ.get("MYMEET_TOKEN", "")
MYMEET_WS_ID      = os.environ.get("MYMEET_WS_ID", "")
MYMEET_BASE       = "https://backend.mymeet.ai"
SPREADSHEET_ID    = os.environ.get("MYMEET_SPREADSHEET_ID", "")
ORGANIZER_EMAIL   = os.environ.get("ORGANIZER_EMAIL", "")

GS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ── Sheet formatting constants ────────────────────────────────────────────────
STATUS_VALUES = [
    "Требует верификации", "В работе", "Выполнено", "Просрочено", "Отменено",
]
COL_WIDTHS_PX = [50, 110, 420, 180, 120, 190]  # A B C D E F (pixels)
MTG_ID_COL_PX = 60                             # G — internal meeting-id column

# Protocol sheets (recurring meetings + «Разовые встречи») share this header.
PROTOCOL_HEADER_ROW = ["№", "Дата", "Задача", "Ответственный", "Срок", "Статус", "mtg_id"]
ONE_TIME_SHEET_NAME = "Разовые встречи"

# Personal «Мои задачи» sheet (read-only aggregate of the protocol sheets).
MY_TASKS_SHEET_NAME = "Мои задачи"
# Per-user sheet title: "Мои задачи — {ФИО}" (one shared spreadsheet may serve
# several users, each gets its own sheet).
MY_TASKS_SHEET_PREFIX = f"{MY_TASKS_SHEET_NAME} — "
MY_TASKS_HEADER_ROW = [
    "№", "Дата", "Совещание", "Задача", "Ответственный", "Срок", "Статус", "mtg_id",
]
# Редактируемый лист из трёх источников (протоколы + Google Tasks + Telegram,
# см. my_tasks.py): та же раскладка, «Совещание» → «Источник», H — ключ строки.
MY_TASKS_HEADER_ROW_V2 = [
    "№", "Дата", "Источник", "Задача", "Ответственный", "Срок", "Статус", "id",
]
MY_TASKS_PROTECTION_PREFIX = "Генерируется ботом"
MY_TASKS_PROTECTION_EDITABLE = (
    "Генерируется ботом из протоколов, Google Tasks и Telegram. "
    "Менять можно только «Статус» — бот учтёт его при синхронизации.")
# Защита от массового удаления: не перезаписываем лист, если строк стало
# меньше половины (при ≥ MY_TASKS_SHRINK_MIN строках в листе).
MY_TASKS_SHRINK_MIN = 10
MY_TASKS_COL_WIDTHS_PX = [50, 110, 220, 420, 180, 120, 190]  # A–G
MY_TASKS_STATUS_COL = 6                                      # G (0-based)
MY_TASKS_MTG_ID_COL = 7                                      # H — hidden
MY_TASKS_HIDDEN_STATUSES = ["Выполнено", "Отменено"]         # hidden by filter

# Conditional-format colours for the status column (light tints).
STATUS_COLORS = {
    "Требует верификации": {"backgroundColor": {"red": 1.0,   "green": 0.949, "blue": 0.8}},
    "В работе":            {"backgroundColor": {"red": 0.812, "green": 0.886, "blue": 0.953}},
    "Выполнено":           {"backgroundColor": {"red": 0.851, "green": 0.918, "blue": 0.827}},
    "Просрочено":          {"backgroundColor": {"red": 0.957, "green": 0.8,   "blue": 0.8}},
    "Отменено":            {"backgroundColor": {"red": 0.851, "green": 0.851, "blue": 0.851},
                            "textFormat": {"strikethrough": True}},
}


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


def _apply_full_format(
    spreadsheet, ws, data_rows: int = 0, *,
    ncols: int = 6,
    status_col: int | None = 5,
    col_widths: list | None = None,
    hidden_col: int | None = 6,
) -> None:
    """Apply unified sheet formatting in one batch_update call:
    header style + freeze, column widths A-F, text wrap, row auto-resize,
    dropdown validation for column F (Статус).

    Defaults reproduce the protocol-sheet layout (A–F visible, F = status,
    G = hidden mtg_id).  Other layouts (e.g. «Мои задачи») pass:
      ncols      — number of visible columns (header style + widths)
      status_col — 0-based status column for the dropdown; None = no dropdown
      col_widths — pixel widths for the visible columns
      hidden_col — 0-based hidden internal column (mtg_id); None = none
    """
    sid = ws.id
    widths = COL_WIDTHS_PX if col_widths is None else col_widths
    wrap_end = max(ncols, hidden_col + 1) if hidden_col is not None else ncols
    # Validation covers up to 10 000 rows so new rows also get the dropdown
    VAL_END = 10_000

    requests: list = [
        # 1. Header: bold/color/freeze
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid,
                    "startRowIndex": 0, "endRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": ncols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {
                            "bold": True, "fontSize": 10,
                            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                        },
                        "backgroundColor": {"red": 0.204, "green": 0.396, "blue": 0.643},
                        "horizontalAlignment": "CENTER",
                        "wrapStrategy": "WRAP",
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
        # 2. Text wrap + top align on all data rows
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid,
                    "startRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": wrap_end,
                },
                "cell": {
                    "userEnteredFormat": {
                        "wrapStrategy": "WRAP",
                        "verticalAlignment": "TOP",
                    }
                },
                "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment",
            }
        },
    ]
    # 3. Dropdown validation for the status column (default: F, index 5)
    if status_col is not None:
        requests.append({
            "setDataValidation": {
                "range": {
                    "sheetId": sid,
                    "startRowIndex": 1,
                    "startColumnIndex": status_col, "endColumnIndex": status_col + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": v} for v in STATUS_VALUES],
                    },
                    "showCustomUi": True,
                    "strict": True,
                }
            }
        })

    # 4. Column widths A–F
    for col_idx, px in enumerate(widths):
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sid,
                    "dimension": "COLUMNS",
                    "startIndex": col_idx,
                    "endIndex": col_idx + 1,
                },
                "properties": {"pixelSize": px},
                "fields": "pixelSize",
            }
        })

    # Column G (mtg_id) — hidden internal column
    if hidden_col is not None:
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS",
                           "startIndex": hidden_col, "endIndex": hidden_col + 1},
                "properties": {"pixelSize": MTG_ID_COL_PX, "hiddenByUser": True},
                "fields": "pixelSize,hiddenByUser",
            }
        })
    fmt_warnings: list[str] = []
    try:
        _gs_call(spreadsheet.batch_update, {"requests": requests})
    except Exception as e:
        msg = f"batch_update: {e}"
        logger.warning("Sheet formatting error (%s): %s", ws.title, msg)
        fmt_warnings.append(msg)

    # 5. Auto-resize row heights (separate call; must run after data is in the sheet)
    if data_rows > 0:
        try:
            _gs_call(spreadsheet.batch_update, {"requests": [{
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": sid,
                        "dimension": "ROWS",
                        "startIndex": 1,
                        "endIndex": data_rows + 1,
                    }
                }
            }]})
        except Exception as e:
            msg = f"auto-resize: {e}"
            logger.warning("Sheet formatting error (%s): %s", ws.title, msg)
            fmt_warnings.append(msg)
    return fmt_warnings


def _task_key(row: list) -> str | None:
    """Return a deduplication key for a data row.

    Key format with meeting_id present in col G (index 6):
        '{mid}|{date}|{task}|{assignee}'
    Fallback for legacy rows without col G:
        '{date}|{task}|{assignee}'

    Returns None for header rows, grouper rows (empty date), or short rows.
    """
    if len(row) < 4:
        return None
    date  = str(row[1]).strip()
    task  = str(row[2]).strip()
    asgn  = str(row[3]).strip()
    if not task or not date:
        return None
    mid = str(row[6]).strip() if len(row) > 6 else ""
    if mid:
        return f"{mid}|{date}|{task.lower()}|{asgn.lower()}"
    return f"{date}|{task.lower()}|{asgn.lower()}"


def _existing_keys(existing_rows: list) -> set:
    """Return set of task keys from already-fetched get_all_values() result."""
    keys = set()
    for row in existing_rows[1:]:   # skip header
        k = _task_key(row)
        if k:
            keys.add(k)
    return keys




def _run_key(meeting_id: str, date: str, task: str, assignee: str) -> str:
    """Intra-run deduplication key that includes meeting identity.
    Prevents adding the same task twice within one sync run when it appears
    in two different meetings (different meeting_id → different run_key).
    """
    return f"{meeting_id}|{date}|{task.lower().strip()}|{assignee.lower().strip()}"


# ── MYmeet API ────────────────────────────────────────────────────────────────

def _mymeet_headers(token: str | None = None) -> dict:
    return {"X-API-KEY": token or MYMEET_TOKEN}


class MymeetTokenError(Exception):
    """Raised when the token is rejected by the MYmeet API."""
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


def verify_token_and_get_workspace(token: str) -> dict:
    """Verify the API token and return the active workspace info.

    Returns a dict with keys:
        id   — always "active" (the API uses this keyword, not a UUID)
        name — workspace display name, or a placeholder if unavailable

    Raises:
        MymeetTokenError  — 401/403 from the API (bad key or free plan)
        requests.RequestException — network / timeout
    """
    if not token or not token.strip():
        raise MymeetTokenError(0, "Токен не может быть пустым")
    url = f"{MYMEET_BASE}/api/workspaces/active/all-meetings"
    try:
        resp = requests.get(
            url,
            headers=_mymeet_headers(token),
            params={"page": 0, "perPage": 1},
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.warning("verify_token_and_get_workspace: network error: %s", exc)
        raise
    if resp.status_code == 401:
        raise MymeetTokenError(401, "Неверный API-токен (401 Unauthorized)")
    if resp.status_code == 403:
        try:
            detail = resp.json().get("message") or resp.text[:120]
        except Exception:
            detail = resp.text[:120]
        raise MymeetTokenError(403, f"Доступ запрещён (403): {detail}")
    if resp.status_code == 404:
        raise MymeetTokenError(404, "Endpoint не найден (404) — проверь версию API")
    if not resp.ok:
        raise MymeetTokenError(resp.status_code,
                               f"Ошибка API: {resp.status_code} {resp.text[:120]}")
    ws_name = None
    try:
        data = resp.json()
        ws_name = (data.get("workspaceName") or data.get("workspace_name")
                   or data.get("name") or None)
    except Exception:
        pass
    return {"id": "active", "name": ws_name or "Ваше пространство"}


def fetch_workspaces(token: str) -> list:
    """Compatibility shim: returns a single-element list with the active workspace.
    Use verify_token_and_get_workspace() for new code."""
    try:
        ws = verify_token_and_get_workspace(token)
        return [ws]
    except MymeetTokenError:
        raise
    except Exception:
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
        # Always use the "active" keyword — the API key already determines
        # which workspace is active.  Stored UUIDs from an older setup are
        # ignored; they would produce 404 on the correct API version.
        url    = f"{MYMEET_BASE}/api/workspaces/active/all-meetings"
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


def _meeting_ts_ms(m: dict) -> int | None:
    """Return meeting start time in UTC milliseconds.
    Priority: startTime -> date -> createdAt -> None.
    Returns None when no parseable date found (meeting excluded from sync)."""
    for field in ("startTime", "date", "createdAt"):
        v = m.get(field)
        if not v:
            continue
        if isinstance(v, (int, float)) and v > 1e10:
            return int(v)
        if isinstance(v, str):
            try:
                ts = int(v)
                if ts > 1e10:
                    return ts
            except ValueError:
                pass
            for fmt in (
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S", "%a, %d %b %Y %H:%M:%S GMT", "%Y-%m-%d",
            ):
                try:
                    dt = datetime.strptime(v.replace(" GMT", ""), fmt.replace(" GMT", ""))
                    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
                except ValueError:
                    pass
    return None


def _extract_gcal_event_id(m: dict) -> str | None:
    """Try to extract a Google Calendar event ID from a MYmeet meeting object.

    Checks field paths MYmeet is known to embed based on existing API patterns
    (same paths used in _is_organizer for organizer look-ups).
    Returns None when no GCal event ID is found -- do not invent field names.
    """
    for field in ("calendarEventId", "googleEventId", "gcalEventId",
                  "calendar_event_id", "google_event_id", "eventId"):
        v = m.get(field)
        if v and isinstance(v, str):
            return v
    for path in (
        ("calendarEvent", "id"),
        ("event", "id"),
        ("calendar", "id"),
        ("meetingData", "eventId"),
        ("meetingData", "id"),
    ):
        obj = m
        for key in path:
            obj = obj.get(key) if isinstance(obj, dict) else None
        if obj and isinstance(obj, str):
            return obj
    return None


def _is_organizer_by_gcal(m: dict, gcal_data: dict) -> bool:
    """Return True when this meeting is confirmed as self-organized via Google Calendar.

    gcal_data: {"ids": set[str], "times": set[int]} built in main.py.
      ids:   GCal event IDs where organizer.self == True
      times: same events start times, UTC, minute-rounded, in ms

    Matching strategy (most reliable first):
      1. GCal event ID found in m AND in gcal_data["ids"] -> organizer
         GCal event ID found in m but NOT in ids -> not organizer
      2. No GCal ID in m -> time-based fallback (+-1 min); logged as approximate.
    Unknown-date meetings always excluded.
    """
    gcal_ids   = gcal_data.get("ids", set())
    gcal_times = gcal_data.get("times", set())

    # 1. ID-based match (reliable)
    event_id = _extract_gcal_event_id(m)
    if event_id is not None:
        result = event_id in gcal_ids
        logger.debug("_is_organizer_by_gcal: id=%s gcal_match=%s meeting=%s",
                     event_id, result, m.get("id", "?"))
        return result

    # 2. Time-based fallback (approximate -- may mis-match concurrent meetings)
    ts = _meeting_ts_ms(m)
    if ts is None:
        return False  # unknown date -> exclude per spec
    ts_min = int(ts / 60_000) * 60_000
    matched = any(ts_min + delta in gcal_times for delta in (0, 60_000, -60_000))
    logger.debug(
        "_is_organizer_by_gcal: no GCal event ID found in MYmeet meeting %s; "
        "time fallback ts_min=%d matched=%s",
        m.get("id", "?"), ts_min, matched,
    )
    return matched


def _deadline_sort_key(deadline_str: str) -> tuple:
    """Sort key: (0, datetime) for valid deadline, (1, max) for missing/invalid.
    Keeps tasks without deadlines at the bottom of the sheet."""
    dl = (deadline_str or "").strip()
    if not dl:
        return (1, datetime.max)
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return (0, datetime.strptime(dl, fmt))
        except ValueError:
            pass
    return (1, datetime.max)


def _build_user_config(user_config: dict | None) -> dict:
    """Merge user_config with module-level defaults."""
    cfg = user_config or {}
    token    = cfg.get("mymeet_token")          or MYMEET_TOKEN
    ws_id    = cfg.get("mymeet_ws_id")          or MYMEET_WS_ID
    sheet_id = cfg.get("mymeet_spreadsheet_id") or SPREADSHEET_ID
    email    = cfg.get("mymeet_organizer_email") or ORGANIZER_EMAIL

    # HTTP headers must be latin-1 encodable. Catch bad token early with
    # a human-readable message instead of a cryptic UnicodeEncodeError.
    if token:
        try:
            token.encode("latin-1")
        except (UnicodeEncodeError, AttributeError):
            raise ValueError(
                "MYmeet-токен содержит недопустимые символы (кириллица или спецсимволы).\n"
                "Пожалуйста, сброси конфигурацию и введи корректный токен: /reset_mymeet"
            )

    return {
        "token":    token,
        "ws_id":    ws_id,
        "sheet_id": sheet_id,
        "email":    email,
        "gcal_organizer_data": cfg.get("gcal_organizer_data"),  # dict{"ids","times"} or None
        "full_name": cfg.get("mymeet_full_name") or "",          # ФИО for «Мои задачи»
        # my_tasks.MyTasksBuilder (или None — прежний режим только протоколов)
        "my_tasks_builder": cfg.get("my_tasks_builder"),
    }


GROUPER_FORMAT = {
    "textFormat": {"bold": True, "fontSize": 10},
    "backgroundColor": {"red": 0.878, "green": 0.878, "blue": 0.878},
}


# ── «Мои задачи» + status colours ─────────────────────────────────────────────

_GROUPER_DATE_TAIL_RE = re.compile(r"\s+\(\d{2}\.\d{2}\.\d{4}\)$")
_UNVERIFIED_STATUS = "Требует верификации"


def _norm_person(s) -> str:
    """Normalise a person name for comparison: case-insensitive, all
    whitespace (incl. NBSP) removed.  'Иван  Петров' == 'иван петров'."""
    return "".join(str(s or "").casefold().split())


def _quote_sheet_title(title: str) -> str:
    """A1-notation sheet reference: wrap in single quotes, double inner quotes."""
    return "'" + str(title).replace("'", "''") + "'"


def _is_protocol_header(row) -> bool:
    """True when a sheet's first row is exactly the protocol header."""
    if not row:
        return False
    cells = [str(c).strip() for c in row]
    while cells and not cells[-1]:
        cells.pop()
    return cells == PROTOCOL_HEADER_ROW


def _my_tasks_sheet_title(full_name: str) -> str:
    """Deterministic per-user sheet title «Мои задачи — {ФИО}» (≤100 chars).
    Whitespace in ФИО is collapsed, case is kept as entered."""
    name = " ".join(str(full_name or "").split())
    return _safe_sheet_name(f"{MY_TASKS_SHEET_PREFIX}{name}")


def _is_my_tasks_title(title: str) -> bool:
    """True for the legacy «Мои задачи» and any «Мои задачи — …» sheet title."""
    t = str(title or "")
    return t == MY_TASKS_SHEET_NAME or t.startswith(MY_TASKS_SHEET_PREFIX)


def _is_my_tasks_header(row) -> bool:
    """True when the first row (read as A:G or wider) is a «Мои задачи» header
    (legacy «Совещание»/mtg_id or V2 «Источник»/id)."""
    if not row:
        return False
    cells = [str(c).strip() for c in row]
    while cells and not cells[-1]:
        cells.pop()
    n = len(cells)
    return n >= len(MY_TASKS_HEADER_ROW) - 1 and (
        cells == MY_TASKS_HEADER_ROW[:n] or cells == MY_TASKS_HEADER_ROW_V2[:n])


def _resolve_my_tasks_title(titles, full_name: str) -> str:
    """Pick the sheet title for this user's «Мои задачи».

    An existing «Мои задачи — …» sheet whose title equals the desired one after
    _norm_person() (case/whitespace-insensitive) is reused, so the same person
    typed in a different case gets the same sheet.  Exact match wins; among
    several case-variants the lexicographically smallest is taken
    (deterministic).  Otherwise the desired title is returned (to be created).
    """
    desired = _my_tasks_sheet_title(full_name)
    titles = list(titles or [])
    if desired in titles:
        return desired
    want = _norm_person(desired)
    same = sorted(t for t in titles
                  if str(t).startswith(MY_TASKS_SHEET_PREFIX) and _norm_person(t) == want)
    return same[0] if same else desired


def _build_my_tasks_rows(sheets: dict, full_name: str, matcher=None) -> list:
    """Build «Мои задачи» data rows from protocol sheets.

    sheets    — {sheet_title: rows} as read from the spreadsheet (A:G)
    full_name — user's ФИО; only rows whose Ответственный matches it exactly
                after _norm_person() are taken (no partial/first-name match).

    Returns rows: [№, Дата, Совещание, Задача, Ответственный, Срок, Статус, mtg_id]
    sorted by deadline (no deadline at the bottom).  Rows with any status are
    kept (Выполнено/Отменено are hidden by the sheet filter, not dropped).

    Dedup key: mtg_id|date|task|assignee.  When mtg_id is empty the meeting
    identity is used instead: on «Разовые встречи» — the row index of the
    current grouper row plus its title (two one-time meetings on the same day
    stay separate); on recurring-meeting sheets — the sheet title (one date =
    one meeting there).
    Among duplicates a row with a manually set status (≠ «Требует верификации»)
    wins, otherwise the first one seen.

    matcher — optional callable(assignee, mtg_id) -> bool replacing the exact
    ФИО comparison (my_tasks: word order, cases, surname + initials).
    """
    target = _norm_person(full_name)
    if not target:
        return []
    if matcher is None:
        def matcher(assignee, _mid):
            return _norm_person(assignee) == target

    picked: dict[str, list] = {}
    for title, rows in sheets.items():
        if _is_my_tasks_title(title):
            continue
        if not rows or not _is_protocol_header(rows[0]):
            continue
        is_one_time = title == ONE_TIME_SHEET_NAME
        current_meeting = "" if is_one_time else title
        grouper_idx = 0                       # row index of the current grouper
        for row_idx, raw in enumerate(rows[1:], 1):
            cells = [str(c) if c is not None else "" for c in (raw or [])]
            cells += [""] * (7 - len(cells))
            cells = [c.strip() for c in cells[:7]]
            if is_one_time and cells[0] and not any(cells[1:7]):
                # Grouper row "{name}  ({dd.mm.yyyy})"
                current_meeting = _GROUPER_DATE_TAIL_RE.sub("", cells[0]).strip()
                grouper_idx = row_idx
                continue
            _num, date, task, assignee, deadline, status, mid = cells
            if not date or not task:
                continue
            if not matcher(assignee, mid):
                continue
            if mid:
                base = mid
            elif is_one_time:
                base = f"{title}#{grouper_idx}|{current_meeting.lower()}"
            else:
                base = title
            key = f"{base}|{date}|{task.lower()}|{assignee.lower()}"
            row = [None, date, current_meeting, task, assignee, deadline, status, mid]
            prev = picked.get(key)
            if prev is None:
                picked[key] = row
            elif (prev[6] in ("", _UNVERIFIED_STATUS)
                  and status not in ("", _UNVERIFIED_STATUS)):
                picked[key] = row

    result = list(picked.values())
    result.sort(key=lambda r: _deadline_sort_key(r[5]))
    for i, r in enumerate(result, 1):
        r[0] = i
    return result


def _is_our_status_rule(rule: dict, sheet_id: int, status_col: int) -> bool:
    """True for a conditional-format rule created by _status_color_requests():
    booleanRule TEXT_EQ with a STATUS_VALUES value on exactly the status column."""
    br = rule.get("booleanRule")
    if not isinstance(br, dict):
        return False
    cond = br.get("condition") or {}
    if cond.get("type") != "TEXT_EQ":
        return False
    vals = cond.get("values") or []
    if len(vals) != 1 or vals[0].get("userEnteredValue") not in STATUS_VALUES:
        return False
    ranges = rule.get("ranges") or []
    if len(ranges) != 1:
        return False
    r = ranges[0]
    return (r.get("sheetId", 0) == sheet_id
            and r.get("startColumnIndex", 0) == status_col
            and r.get("endColumnIndex") == status_col + 1)


def _status_color_requests(metadata: dict, targets: dict) -> list:
    """Build batch_update requests that (re)install status colour rules.

    metadata — spreadsheet metadata with sheets[].properties.sheetId and
               sheets[].conditionalFormats
    targets  — {sheetId: status_col (0-based)}

    Only our own rules (see _is_our_status_rule) are deleted, by index in
    descending order; foreign rules are left untouched.  Then one rule per
    status is added.  Running it twice yields the same final rule set.
    """
    existing: dict[int, list] = {}
    for sh in (metadata or {}).get("sheets") or []:
        props = sh.get("properties") or {}
        existing[props.get("sheetId", 0)] = sh.get("conditionalFormats") or []

    reqs: list = []
    for sid, col in targets.items():
        rules = existing.get(sid, [])
        ours = [i for i, rule in enumerate(rules) if _is_our_status_rule(rule, sid, col)]
        for i in sorted(ours, reverse=True):
            reqs.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": i}})
        for i, status in enumerate(STATUS_VALUES):
            reqs.append({
                "addConditionalFormatRule": {
                    "index": i,
                    "rule": {
                        "ranges": [{
                            "sheetId": sid,
                            "startRowIndex": 1,          # open-ended downwards
                            "startColumnIndex": col,
                            "endColumnIndex": col + 1,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_EQ",
                                "values": [{"userEnteredValue": status}],
                            },
                            "format": STATUS_COLORS[status],
                        },
                    },
                }
            })
    return reqs


def _ensure_status_colors(spreadsheet, metadata: dict, targets: dict) -> None:
    """Install status colour rules on all target sheets in ONE batch_update."""
    reqs = _status_color_requests(metadata, targets)
    if reqs:
        _gs_call(spreadsheet.batch_update, {"requests": reqs})


def _write_my_tasks_sheet(spreadsheet, title: str, exists: bool, rows: list,
                          warnings: list, editable: bool = False):
    """Get-or-add the user's «Мои задачи — ФИО» sheet, overwrite its data,
    format it, set the filter.  Returns the worksheet.

    editable=True (my_tasks builder): V2 header, status dropdown in G, the
    soft protection leaves the status column unprotected.

    The sheet is never cleared before writing: data is written first
    (A1:H{n}), and only after a successful update the tail of old rows below
    is cleared.  If update fails, the old content stays and the exception
    propagates (caller turns it into a warning).
    """
    if exists:
        ws = _gs_call(spreadsheet.worksheet, title)
    else:
        ws = _gs_call(spreadsheet.add_worksheet, title=title, rows=1000, cols=10)
        # One-time soft protection: the sheet is regenerated on every sync.
        prot = {
            "range": {"sheetId": ws.id},
            "description": "Генерируется ботом при синхронизации. "
                           "Статус меняйте в листе протокола.",
            "warningOnly": True,
        }
        if editable:
            prot["description"] = MY_TASKS_PROTECTION_EDITABLE
            prot["unprotectedRanges"] = [_my_tasks_status_range(ws.id)]
        try:
            _gs_call(spreadsheet.batch_update, {"requests": [{
                "addProtectedRange": {"protectedRange": prot}
            }]})
        except Exception as e:
            msg = f"{title} (защита листа): {e}"
            logger.warning("Sheet formatting error (%s): %s", title, msg)
            warnings.append(msg)

    all_data = [MY_TASKS_HEADER_ROW_V2 if editable else MY_TASKS_HEADER_ROW] + rows
    need_rows = len(all_data)
    need_cols = len(MY_TASKS_HEADER_ROW)
    try:
        cur_rows, cur_cols = int(ws.row_count), int(ws.col_count)
    except Exception:
        cur_rows, cur_cols = 1000, 10
    if need_rows > cur_rows or need_cols > cur_cols:
        _gs_call(ws.resize, rows=max(need_rows, cur_rows), cols=max(need_cols, cur_cols))
    _gs_call(ws.update, values=all_data, range_name=f"A1:H{need_rows}")
    # Clear leftovers of a previous (longer) run only after a successful write.
    if cur_rows > need_rows:
        try:
            _gs_call(ws.batch_clear, [f"A{need_rows + 1}:H{cur_rows}"])
        except Exception as e:
            msg = f"{title} (очистка старых строк): {e}"
            logger.warning("Sheet formatting error (%s): %s", title, msg)
            warnings.append(msg)

    warnings.extend(_apply_full_format(
        spreadsheet, ws, data_rows=len(rows),
        ncols=len(MY_TASKS_COL_WIDTHS_PX),
        status_col=MY_TASKS_STATUS_COL if editable else None,  # legacy: read-only
        col_widths=MY_TASKS_COL_WIDTHS_PX,
        hidden_col=MY_TASKS_MTG_ID_COL,
    ))

    try:
        _gs_call(spreadsheet.batch_update, {"requests": [{
            "setBasicFilter": {
                "filter": {
                    "range": {"sheetId": ws.id},
                    "filterSpecs": [{
                        "columnIndex": MY_TASKS_STATUS_COL,
                        "filterCriteria": {"hiddenValues": list(MY_TASKS_HIDDEN_STATUSES)},
                    }],
                }
            }
        }]})
    except Exception as e:
        msg = f"{title} (фильтр): {e}"
        logger.warning("Sheet formatting error (%s): %s", title, msg)
        warnings.append(msg)
    return ws


def _my_tasks_status_range(sheet_id: int) -> dict:
    return {"sheetId": sheet_id, "startRowIndex": 1,
            "startColumnIndex": MY_TASKS_STATUS_COL,
            "endColumnIndex": MY_TASKS_STATUS_COL + 1}


def _my_tasks_protection_update_requests(sheet_meta: dict, sheet_id: int) -> list:
    """Existing sheets created read-only: switch our soft protection to
    «status column editable».  Foreign protections are not touched."""
    reqs = []
    for pr in (sheet_meta or {}).get("protectedRanges") or []:
        desc = str(pr.get("description") or "")
        if not desc.startswith(MY_TASKS_PROTECTION_PREFIX):
            continue
        if desc == MY_TASKS_PROTECTION_EDITABLE and pr.get("unprotectedRanges"):
            continue
        if "protectedRangeId" not in pr:
            continue
        reqs.append({"updateProtectedRange": {
            "protectedRange": {
                "protectedRangeId": pr["protectedRangeId"],
                "description": MY_TASKS_PROTECTION_EDITABLE,
                "unprotectedRanges": [_my_tasks_status_range(sheet_id)],
            },
            "fields": "description,unprotectedRanges",
        }})
    return reqs


def _count_my_task_rows(rows) -> int:
    """Data rows (with a task) in a «Мои задачи» sheet read as A:H."""
    if not rows or not _is_my_tasks_header(rows[0]):
        return 0
    return sum(1 for r in rows[1:] if len(r) > 3 and str(r[3]).strip())


def _my_tasks_sheet_stable(spreadsheet, title: str, before_rows, rows_builder) -> bool:
    """Повторно читает лист «Мои задачи» (A:H) и проверяет, что ни одна ячейка
    не менялась после первого чтения.  Ошибка чтения → False (не рискуем)."""
    try:
        resp = _gs_call(spreadsheet.values_batch_get, [_quote_sheet_title(title) + "!A:H"])
        vranges = (resp.get("valueRanges") or []) if isinstance(resp, dict) else []
        fresh = (vranges[0] or {}).get("values") or [] if vranges else []
    except Exception as e:
        logger.warning("My tasks: re-read failed (%s): %s", title, type(e).__name__)
        return False
    return rows_builder.sheet_unchanged(before_rows, fresh)


def _sync_my_tasks_and_colors(spreadsheet, full_name: str, rows_builder=None) -> tuple:
    """Post-sync step: status colours on protocol sheets and on every
    «Мои задачи — …» sheet; when full_name is set, the user's personal
    «Мои задачи — {ФИО}» sheet is (re)written.  Other users' sheets are never
    written or cleared (only our colour rules on them are refreshed).

    Returns (warnings: list[str], my_tasks_count: int | None, my_title: str | None).
    my_tasks_count is None when the sheet was not written.
    Never raises for sheet-level problems — they are returned as warnings.

    rows_builder (my_tasks.MyTasksBuilder) switches the personal sheet to the
    editable three-source mode: builder.build() gets protocol candidates and
    the current sheet (to accept manual status edits) and returns rows;
    builder.commit() is called only after a successful write.  The sheet is
    not rewritten if it would shrink below half (mass-deletion guard).
    """
    warnings: list[str] = []
    full_name = (full_name or "").strip()

    fields = "sheets(properties(sheetId,title),conditionalFormats,basicFilter)"
    if rows_builder is not None:
        fields = ("sheets(properties(sheetId,title),conditionalFormats,basicFilter,"
                  "protectedRanges(protectedRangeId,description,unprotectedRanges))")
    meta = _gs_call(spreadsheet.fetch_sheet_metadata, params={"fields": fields})
    if not isinstance(meta, dict):
        meta = {}
    sheet_ids: dict[str, int] = {}
    for sh in meta.get("sheets") or []:
        props = sh.get("properties") or {}
        if "title" in props:
            sheet_ids[props["title"]] = props.get("sheetId", 0)
    titles = list(sheet_ids)
    my_title = _resolve_my_tasks_title(titles, full_name) if full_name else None

    values: dict[str, list] = {}
    if titles:
        suffix = "!A:G" if full_name else "!A1:G1"
        resp = _gs_call(spreadsheet.values_batch_get,
                        [_quote_sheet_title(t) + ("!A:H" if t == my_title else suffix)
                         for t in titles])
        vranges = (resp.get("valueRanges") or []) if isinstance(resp, dict) else []
        for t, vr in zip(titles, vranges):
            values[t] = (vr or {}).get("values") or []

    protocol_titles = [t for t in titles
                       if values.get(t) and _is_protocol_header(values[t][0])]
    color_targets = {sheet_ids[t]: 5 for t in protocol_titles}
    # Existing «Мои задачи — …» sheets of any user: colours only, no data writes.
    for t in titles:
        if (_is_my_tasks_title(t) and t not in protocol_titles
                and values.get(t) and _is_my_tasks_header(values[t][0])):
            color_targets[sheet_ids[t]] = MY_TASKS_STATUS_COL

    my_count = None
    if full_name:
        if my_title in protocol_titles:
            msg = (f"Лист «{my_title}» занят протоколом встречи с таким "
                   f"же названием — личный лист не обновлён")
            logger.warning("My tasks: %s", msg)
            warnings.append(msg)
        else:
            try:
                proto = {t: values[t] for t in protocol_titles}
                exists = my_title in sheet_ids
                if rows_builder is None:
                    rows = _build_my_tasks_rows(proto, full_name)
                else:
                    my_rows = values.get(my_title) if exists else None
                    candidates = _build_my_tasks_rows(
                        proto, full_name, matcher=rows_builder.matcher())
                    rows = rows_builder.build(candidates, my_rows)
                    before = _count_my_task_rows(my_rows)
                    if rows is None:
                        pass                    # аномалия builder-а (см. его warnings)
                    elif before >= MY_TASKS_SHRINK_MIN and len(rows) * 2 < before:
                        msg = (f"{my_title}: строк стало бы {len(rows)} вместо "
                               f"{before} — лист не перезаписан (защита от массового "
                               f"удаления)")
                        rows = None
                        rows_builder.blocked("shrink")
                        logger.warning("My tasks: %s", msg)
                        warnings.append(msg)
                    elif exists and not _my_tasks_sheet_stable(
                            spreadsheet, my_title, my_rows, rows_builder):
                        # Пользователь правил лист, пока шла сверка — не
                        # перезаписываем его правку; повторим сверку позже.
                        msg = (f"{my_title}: лист изменён во время синхронизации — "
                               f"запись отложена, повторю")
                        rows = None
                        rows_builder.blocked("conflict")
                        logger.warning("My tasks: %s", msg)
                        warnings.append(msg)
                if rows is not None:
                    # Запись сразу после повторного чтения — окно гонки = 1 запрос
                    ws = _write_my_tasks_sheet(
                        spreadsheet, my_title, exists, rows, warnings,
                        editable=rows_builder is not None)
                    color_targets[ws.id] = MY_TASKS_STATUS_COL
                    my_count = len(rows)
                    if rows_builder is not None:
                        rows_builder.commit()
                        # Внешние операции (Google Tasks) — только после проверки
                        # листа и его успешной записи
                        rows_builder.execute_external()
                        if exists:
                            sheet_meta = next(
                                (sh for sh in meta.get("sheets") or []
                                 if (sh.get("properties") or {}).get("title") == my_title),
                                {})
                            preqs = _my_tasks_protection_update_requests(sheet_meta, ws.id)
                            if preqs:
                                try:
                                    _gs_call(spreadsheet.batch_update, {"requests": preqs})
                                except Exception as e:
                                    warnings.append(f"{my_title} (защита листа): {e}")
            except Exception as e:
                msg = f"{my_title}: {e}"
                logger.warning("Sheet formatting error (%s): %s", my_title, msg)
                warnings.append(msg)

    if color_targets:
        try:
            _ensure_status_colors(spreadsheet, meta, color_targets)
        except Exception as e:
            msg = f"цвета статусов: {e}"
            logger.warning("Sheet formatting error (status colors): %s", msg)
            warnings.append(msg)
    return warnings, my_count, my_title


def refresh_my_tasks_sheet(sheet_id: str, full_name: str, rows_builder) -> tuple:
    """Rewrite only the personal «Мои задачи» sheet (+ status colours) without
    touching MYmeet: used by the daily Google Tasks sync and after a forwarded
    Telegram task.  Returns the same tuple as _sync_my_tasks_and_colors."""
    gc = _gs_client()
    spreadsheet = gc.open_by_key(sheet_id or SPREADSHEET_ID)
    return _sync_my_tasks_and_colors(spreadsheet, full_name, rows_builder=rows_builder)


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
    _fetched_count = len(raw_meetings)
    logger.info("Fetched %d raw meetings from API", _fetched_count)

    # Date filter: use startTime as priority source; exclude meetings with unknown date
    _now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    _from_ms = _now_ms - days * 24 * 3600 * 1000
    raw_meetings = [m for m in raw_meetings
                    if (ts := _meeting_ts_ms(m)) is not None and _from_ms <= ts <= _now_ms]
    _after_date_count = len(raw_meetings)
    logger.info("After date filter (%d days): %d meetings", days, _after_date_count)

    # Organizer filter: use Google Calendar organizer.self (passed via user_config)
    _gcal_data = cfg.get("gcal_organizer_data")
    if _gcal_data is None:
        raise RuntimeError(
            "☹️ Не удалось получить данные Google Calendar для "
            "проверки организатора. "
            "Синхронизация отменена — Google Sheets не изменён."
        )
    raw_meetings = [m for m in raw_meetings if _is_organizer_by_gcal(m, _gcal_data)]
    _after_org_count = len(raw_meetings)
    logger.info("After organizer filter (GCal ids=%d times=%d): %d meetings",
                len(_gcal_data.get("ids", set())), len(_gcal_data.get("times", set())), _after_org_count)

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
    all_fmt_warnings: list[str] = []

    HEADER_ROW = list(PROTOCOL_HEADER_ROW)

    # ── Recurring meetings — one sheet per name ───────────────────────────────
    for meeting_name, meetings in sorted(recurring.items()):
        sheet_name = _safe_sheet_name(meeting_name)
        try:
            ws = spreadsheet.worksheet(sheet_name)
            if not append:
                # Read existing data BEFORE clearing so we can restore manual statuses.
                try:
                    existing_full: list = _gs_call(ws.get_all_values)
                except Exception:
                    existing_full = []
                _gs_call(ws.clear)
        except gspread.WorksheetNotFound:
            ws = _gs_call(spreadsheet.add_worksheet, title=sheet_name, rows=1000, cols=10)
            existing_full = []

        if not append:
            # Full overwrite: preserve manually-set statuses from existing sheet.
            manual_statuses: dict[str, str] = {}
            for _row in (existing_full[1:] if existing_full else []):
                _k = _task_key(_row)
                if _k and len(_row) >= 6 and str(_row[5]).strip():
                    manual_statuses[_k] = str(_row[5]).strip()

            rows = []
            idx  = 1
            seen_in_run: set[str] = set()
            for m in sorted(meetings, key=lambda x: x["date"]):
                for task in m["tasks"]:
                    rkey = _run_key(m["id"], m["date"], task["name"], task["assignee"])
                    if rkey in seen_in_run:
                        continue   # intra-run duplicate from same meeting
                    seen_in_run.add(rkey)
                    tkey = _task_key([
                        None, m["date"], task["name"], task["assignee"],
                        task["deadline"], None, m["id"],
                    ])
                    # Restore manually-set status; fall back to MYmeet status
                    status = manual_statuses.get(tkey) if tkey else None
                    if not status:
                        status = "Выполнено" if task["done"] else "Требует верификации"
                    rows.append([
                        idx, m["date"], task["name"], task["assignee"],
                        task["deadline"], status, m["id"],
                    ])
                    idx += 1
            # Sort by deadline: earliest first, no-deadline at bottom
            rows.sort(key=lambda r: _deadline_sort_key(str(r[4]) if len(r) > 4 else ""))
            for _i, _r in enumerate(rows, 1):
                _r[0] = _i  # re-number after sort
            all_data = [HEADER_ROW] + rows
            _gs_call(ws.update, f"A1:G{len(all_data)}", all_data)
            all_fmt_warnings.extend(_apply_full_format(spreadsheet, ws, data_rows=len(rows)))
        else:
            # Append mode: deduplicate by (date, task, assignee) to avoid re-adding
            # existing rows on repeated daily runs; preserve manually-changed statuses.
            existing = _gs_call(ws.get_all_values)
            ex_keys  = _existing_keys(existing) if existing else set()

            # Determine next sequential №
            last_num = 0
            for row in (existing[1:] if existing else []):
                if row and str(row[0]).strip().isdigit():
                    last_num = max(last_num, int(row[0]))

            start_row = (len(existing) + 1) if existing else 2
            rows = []
            idx  = last_num + 1
            seen_in_run_app: set[str] = set()
            for m in sorted(meetings, key=lambda x: x["date"]):
                for task in m["tasks"]:
                    rkey = _run_key(m["id"], m["date"], task["name"], task["assignee"])
                    if rkey in seen_in_run_app:
                        continue   # intra-run duplicate
                    seen_in_run_app.add(rkey)
                    key = _task_key([
                        None, m["date"], task["name"], task["assignee"],
                        task["deadline"], None, m["id"],
                    ])
                    if key and key in ex_keys:
                        continue   # already present — preserve any manual status edits
                    rows.append([
                        idx, m["date"], task["name"], task["assignee"],
                        task["deadline"],
                        "Выполнено" if task["done"] else "Требует верификации",
                        m["id"],
                    ])
                    idx += 1

            if not existing:
                all_data = [HEADER_ROW] + rows
                _gs_call(ws.update, f"A1:G{len(all_data)}", all_data)
            elif rows:
                _gs_call(ws.update, f"A{start_row}:G{start_row - 1 + len(rows)}", rows)

            # Always (re-)apply formatting so existing sheets get widths/wrap/dropdown too
            total_data_rows = (len(existing) - 1 if existing else 0) + len(rows)
            all_fmt_warnings.extend(_apply_full_format(spreadsheet, ws, data_rows=total_data_rows))
        sheets_written += 1
        # No explicit sleep — rate limiter in _gs_call handles quota

    # ── One-time meetings ─────────────────────────────────────────────────────
    # Format: № | Дата | Задача | Ответственный | Срок | Статус
    # Bold grouper row with meeting name before each meeting's tasks.
    ot_name = ONE_TIME_SHEET_NAME
    try:
        ws = spreadsheet.worksheet(ot_name)
        if not append:
            # Read BEFORE clear to preserve manually-set statuses.
            try:
                existing_ot_before: list = _gs_call(ws.get_all_values)
            except Exception:
                existing_ot_before = []
            _gs_call(ws.clear)
        else:
            existing_ot_before = []
    except gspread.WorksheetNotFound:
        ws = _gs_call(spreadsheet.add_worksheet, title=ot_name, rows=2000, cols=10)
        existing_ot_before = []

    if not append:
        start_idx = 1
        start_row = 2
        new_sheet  = True
        existing_ot: list = []
        # Restore manual statuses from before-clear snapshot
        manual_statuses_ot: dict[str, str] = {}
        for _row in (existing_ot_before[1:] if existing_ot_before else []):
            _k = _task_key(_row)
            if _k and len(_row) >= 6 and str(_row[5]).strip():
                manual_statuses_ot[_k] = str(_row[5]).strip()
    else:
        existing_ot = _gs_call(ws.get_all_values)
        manual_statuses_ot = {}
        if not existing_ot:
            start_idx = 1
            start_row = 2
            new_sheet  = True
        else:
            new_sheet  = False
            start_row = len(existing_ot) + 1
            start_idx = start_row  # will be refined below

    # Dedup keys for one-time sheet (append mode)
    ex_keys_ot = _existing_keys(existing_ot) if (append and existing_ot) else set()

    # Determine next sequential №
    last_num_ot = 0
    for row in (existing_ot[1:] if existing_ot else []):
        if row and str(row[0]).strip().isdigit():
            last_num_ot = max(last_num_ot, int(row[0]))
    idx = last_num_ot + 1 if (append and existing_ot) else start_idx

    # Build rows list; track grouper row positions (0-based relative to start_row)
    all_rows: list = []
    grouper_rel_positions: list[int] = []
    seen_in_run_ot: set[str] = set()

    for m in one_time:
        if not m["tasks"]:
            continue
        # In append mode filter tasks already present; also dedup within this run
        new_tasks = []
        for task in m["tasks"]:
            rkey = _run_key(m["id"], m["date"], task["name"], task["assignee"])
            if rkey in seen_in_run_ot:
                continue   # intra-run duplicate
            seen_in_run_ot.add(rkey)
            if append and existing_ot:
                key = _task_key([None, m["date"], task["name"], task["assignee"], None, None, m["id"]])
                if key and key in ex_keys_ot:
                    continue  # skip — already in sheet, status may have been edited manually
            new_tasks.append(task)

        if not new_tasks:
            continue   # all tasks for this meeting already exist

        # Sort tasks by deadline: earliest first, no-deadline at bottom
        new_tasks.sort(key=lambda t: _deadline_sort_key(t.get("deadline", "")))
        label = f"{m['name']}  ({m['date']})" if m["date"] else m["name"]
        grouper_rel_positions.append(len(all_rows))
        all_rows.append([label, "", "", "", "", "", ""])
        for task in new_tasks:
            tkey_ot = _task_key([None, m["date"], task["name"], task["assignee"],
                                  task["deadline"], None, m["id"]])
            status_ot = (manual_statuses_ot.get(tkey_ot) if not append else None) or (
                "Выполнено" if task["done"] else "Требует верификации"
            )
            all_rows.append([
                idx,
                m["date"],
                task["name"],
                task["assignee"],
                task["deadline"],
                status_ot,
                m["id"],
            ])
            idx += 1

    if new_sheet:
        write_data = [HEADER_ROW] + all_rows
        if write_data:
            _gs_call(ws.update, f"A1:G{len(write_data)}", write_data)
        all_fmt_warnings.extend(_apply_full_format(spreadsheet, ws, data_rows=len(all_rows)))
    elif all_rows:
        _gs_call(ws.update, f"A{start_row}:G{start_row - 1 + len(all_rows)}", all_rows)
        # Always re-apply formatting to pick up widths/wrap/dropdown on existing sheet
        total_ot_rows = (len(existing_ot) - 1 if existing_ot else 0) + len(all_rows)
        all_fmt_warnings.extend(_apply_full_format(spreadsheet, ws, data_rows=total_ot_rows))
    else:
        # Nothing new to write but still (re-)apply formatting to existing sheet
        if existing_ot:
            all_fmt_warnings.extend(_apply_full_format(spreadsheet, ws, data_rows=len(existing_ot) - 1))

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
            logger.warning("Sheet formatting error (grouper rows): %s", e)

    # ── Status colours + personal «Мои задачи» sheet ──────────────────────────
    # Must never break the main sync: any failure becomes a warning.
    full_name = (cfg.get("full_name") or "").strip()
    my_tasks_count = None
    my_tasks_title = None
    try:
        _mt_warnings, my_tasks_count, my_tasks_title = _sync_my_tasks_and_colors(
            spreadsheet, full_name, rows_builder=cfg.get("my_tasks_builder"))
        all_fmt_warnings.extend(_mt_warnings)
    except Exception as e:
        logger.warning("Sheet formatting error (Мои задачи / цвета): %s", e)
        all_fmt_warnings.append(f"Мои задачи / цвета статусов: {e}")
    if not full_name:
        my_tasks_line = (f"\n👤 Лист «{MY_TASKS_SHEET_PREFIX}ФИО» не создан. "
                         f"Укажите ФИО: /setname Имя Фамилия")
    elif my_tasks_count is not None:
        my_tasks_line = f"\n👤 Лист «{my_tasks_title}»: {my_tasks_count} задач"
    else:
        my_tasks_line = ""

    fmt_warn_text = (
        f"\n⚠️ Ошибки форматирования ({len(all_fmt_warnings)}):\n"
        + "\n".join(f"  • {w}" for w in all_fmt_warnings)
        if all_fmt_warnings else ""
    )
    _diag_now = datetime.now(timezone.utc)
    _diag_from = _diag_now - timedelta(days=days)
    result = (
        f"✅ Синхронизировано: {total_tasks} задач\n"
        f"📋 {sheets_written} листов по регулярным встречам\n"
        f"📝 Разовые встречи: {len(one_time)} встреч\n"
        f"📅 Период: {_diag_from.strftime('%d.%m.%Y')} — {_diag_now.strftime('%d.%m.%Y')}\n"
        f"🔍 API {_fetched_count} → дата {_after_date_count} → организатор {_after_org_count}"
        + my_tasks_line
        + fmt_warn_text
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
