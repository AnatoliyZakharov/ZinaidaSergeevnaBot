"""
Integration tests for mymeet_sync.sync_to_sheets using unittest.mock.

These tests mock the Google Sheets API (gspread) at the boundary so we can
verify the full sync logic — including dedup, status restoration, column G
write ranges, hidden column, and formatting calls — without a real network.

Run with:
    python test_integration.py
"""
import importlib.util
import os
import sys
import types
import unittest
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

# ── Minimal stubs so mymeet_sync can be imported without real credentials ─────

def _make_gspread_stub():
    gs = types.ModuleType("gspread")
    gs.WorksheetNotFound = type("WorksheetNotFound", (Exception,), {})
    gs.exceptions = types.SimpleNamespace(APIError=Exception)
    gs.Client = object
    gs.authorize = MagicMock(return_value=MagicMock())
    return gs

def _make_requests_stub():
    req = types.ModuleType("requests")
    req.get  = MagicMock(side_effect=RuntimeError("no network in tests"))
    req.post = MagicMock(side_effect=RuntimeError("no network in tests"))
    return req

def _load_module():
    stubs = {
        "gspread": _make_gspread_stub(),
        "requests": _make_requests_stub(),
        "google": types.ModuleType("google"),
        "google.oauth2": types.ModuleType("google.oauth2"),
        "google.oauth2.service_account": types.ModuleType("google.oauth2.service_account"),
    }
    stubs["google"].oauth2 = stubs["google.oauth2"]
    stubs["google.oauth2"].service_account = stubs["google.oauth2.service_account"]
    stubs["google.oauth2.service_account"].Credentials = object
    for name, stub in stubs.items():
        sys.modules.setdefault(name, stub)

    spec = importlib.util.spec_from_file_location(
        "mymeet_sync",
        os.path.join(os.path.dirname(__file__), "mymeet_sync.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()

# ── Helpers ───────────────────────────────────────────────────────────────────

HEADER = ["№", "Дата", "Задача", "Ответственный", "Срок", "Статус", "mtg_id"]


def _make_ws(name="Еженедельная", existing_rows=None):
    """Return a MagicMock worksheet with preconfigured get_all_values."""
    ws = MagicMock()
    ws.id = 42
    ws.title = name
    ws.get_all_values = MagicMock(return_value=list(existing_rows or []))
    ws.update = MagicMock()
    ws.clear = MagicMock()
    return ws


def _make_spreadsheet(ws_map: dict):
    """
    ws_map: {sheet_name: ws_mock} — worksheets that already 'exist'.
    WorksheetNotFound is raised for any name not in ws_map.
    add_worksheet always returns a fresh blank ws.
    """
    spreadsheet = MagicMock()
    spreadsheet.batch_update = MagicMock(return_value={})

    gspread_stub = sys.modules["gspread"]

    def worksheet(name):
        if name in ws_map:
            return ws_map[name]
        raise gspread_stub.WorksheetNotFound(name)

    spreadsheet.worksheet = MagicMock(side_effect=worksheet)
    spreadsheet.add_worksheet = MagicMock(side_effect=lambda title, rows, cols: _make_ws(title))
    return spreadsheet


def _run_sync(spreadsheet, all_meetings, append=False):
    """
    Call sync_to_sheets with mocked Google Sheets client and MYmeet API.

    all_meetings — list of dicts shaped like _fetch_one() returns:
        {"id": str, "name": str, "date": "YYYY-MM-DD", "tasks": [...]}
    Recurring detection: 2+ meetings with the same name → recurring sheet.
    One meeting per name → one-time sheet ("Разовые встречи").
    """
    cfg = {
        "token":    "fake-token",
        "ws_id":    "fake-ws-id",
        "sheet_id": "FAKE-SHEET-ID",
        "email":    "organizer@example.com",
    }
    gc_mock = MagicMock()
    gc_mock.open_by_key.return_value = spreadsheet

    # _fetch_one is a closure inside sync_to_sheets that hits the network.
    # We bypass it by patching ThreadPoolExecutor so each submit() just wraps
    # the meeting dict in a resolved Future, and as_completed yields all of them.
    class _FakePool:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def submit(self, fn, m):
            f = Future()
            f.set_result(m)   # pass the pre-built meeting through unchanged
            return f

    def _fake_as_completed(futures):
        return list(futures.keys())

    with patch.object(mod, "_gs_client", return_value=gc_mock), \
         patch.object(mod, "_build_user_config", return_value=cfg), \
         patch.object(mod, "_fetch_meetings", return_value=all_meetings), \
         patch.object(mod, "_is_organizer", return_value=True), \
         patch.object(mod, "ThreadPoolExecutor", _FakePool), \
         patch.object(mod, "as_completed", _fake_as_completed), \
         patch.object(mod, "_throttle", lambda: None):
        return mod.sync_to_sheets(append=append)


def _meeting(mid, name, date, tasks, done=False):
    """Build a meeting dict as _fetch_one() would produce."""
    return {
        "id":    mid,
        "name":  name,
        "date":  date,
        "tasks": [
            {"name": t, "assignee": a, "deadline": dl, "done": done}
            for t, a, dl in tasks
        ],
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestFullOverwriteReadBeforeClear(unittest.TestCase):
    """get_all_values must be called BEFORE clear in full-overwrite mode."""

    def test_read_order(self):
        existing = [
            HEADER,
            ["1", "2024-01-15", "Написать отчёт", "Иванов", "2024-01-20", "В работе", "mtg-1"],
        ]
        ws = _make_ws("Еженедельная", existing)
        spreadsheet = _make_spreadsheet({"Еженедельная": ws})

        # Two meetings with same name → recurring
        meetings = [
            _meeting("mtg-1", "Еженедельная", "2024-01-15",
                     [("Написать отчёт", "Иванов", "2024-01-20")]),
            _meeting("mtg-1b", "Еженедельная", "2024-01-22",
                     [("Другая задача", "Иванов", "")]),
        ]
        _run_sync(spreadsheet, meetings)

        calls = ws.method_calls
        names = [c[0] for c in calls]
        get_idx   = next((i for i, n in enumerate(names) if n == "get_all_values"), None)
        clear_idx = next((i for i, n in enumerate(names) if n == "clear"), None)
        self.assertIsNotNone(get_idx,   "get_all_values was never called")
        self.assertIsNotNone(clear_idx, "clear was never called")
        self.assertLess(get_idx, clear_idx,
                        "get_all_values must be called before clear")


class TestFullOverwriteStatusPreservation(unittest.TestCase):
    """Status set manually in the sheet must survive a full sync."""

    def test_manual_status_preserved(self):
        existing = [
            HEADER,
            ["1", "2024-01-15", "Написать отчёт", "Иванов", "2024-01-20", "В работе", "mtg-1"],
            ["2", "2024-01-22", "Другая задача",   "Иванов", "",           "Выполнено", "mtg-1b"],
        ]
        ws = _make_ws("Еженедельная", existing)
        spreadsheet = _make_spreadsheet({"Еженедельная": ws})

        meetings = [
            _meeting("mtg-1", "Еженедельная", "2024-01-15",
                     [("Написать отчёт", "Иванов", "2024-01-20")]),
            _meeting("mtg-1b", "Еженедельная", "2024-01-22",
                     [("Другая задача", "Иванов", "")]),
        ]
        _run_sync(spreadsheet, meetings)

        data = ws.update.call_args_list[0][0][1]
        statuses = {r[2]: r[5] for r in data if r[0] != "№"}
        self.assertEqual(statuses.get("Написать отчёт"), "В работе",
                         f"Expected 'В работе' preserved, got: {statuses}")
        self.assertEqual(statuses.get("Другая задача"), "Выполнено",
                         f"Expected 'Выполнено' preserved, got: {statuses}")

    def test_cross_meeting_statuses_independent(self):
        """Same task from mtg-1 and mtg-2 must keep independent statuses."""
        ws_a = _make_ws("Встреча А", [
            HEADER,
            ["1", "2024-01-15", "Задача", "Петров", "", "В работе", "mtg-1"],
            ["2", "2024-01-22", "Задача2","Петров", "", "В работе", "mtg-1b"],
        ])
        ws_b = _make_ws("Встреча Б", [
            HEADER,
            ["1", "2024-01-15", "Задача", "Петров", "", "Выполнено", "mtg-2"],
            ["2", "2024-01-22", "Задача2","Петров", "", "Выполнено", "mtg-2b"],
        ])
        spreadsheet = _make_spreadsheet({"Встреча А": ws_a, "Встреча Б": ws_b})

        meetings = [
            _meeting("mtg-1",  "Встреча А", "2024-01-15", [("Задача",  "Петров", "")]),
            _meeting("mtg-1b", "Встреча А", "2024-01-22", [("Задача2", "Петров", "")]),
            _meeting("mtg-2",  "Встреча Б", "2024-01-15", [("Задача",  "Петров", "")]),
            _meeting("mtg-2b", "Встреча Б", "2024-01-22", [("Задача2", "Петров", "")]),
        ]
        _run_sync(spreadsheet, meetings)

        def _get_status(ws, task_name):
            data = ws.update.call_args_list[0][0][1]
            return next((r[5] for r in data if r[0] != "№" and r[2] == task_name), None)

        self.assertEqual(_get_status(ws_a, "Задача"), "В работе",
                         "Встреча А: expected 'В работе'")
        self.assertEqual(_get_status(ws_b, "Задача"), "Выполнено",
                         "Встреча Б: expected 'Выполнено'")


class TestAppendDedupIncludesMeetingId(unittest.TestCase):
    """In append mode, same task from a NEW meeting must be added, not skipped."""

    def test_new_meeting_task_not_skipped(self):
        existing = [
            HEADER,
            ["1", "2024-01-15", "Задача", "Иванов", "", "В работе", "mtg-1"],
            ["2", "2024-01-22", "Задача", "Иванов", "", "В работе", "mtg-1b"],
        ]
        ws = _make_ws("Еженедельная", existing)
        spreadsheet = _make_spreadsheet({"Еженедельная": ws})

        meetings = [
            # mtg-1 task already in sheet → skip
            _meeting("mtg-1",  "Еженедельная", "2024-01-15", [("Задача", "Иванов", "")]),
            # mtg-2 same task text but different meeting_id → must be appended
            _meeting("mtg-2",  "Еженедельная", "2024-01-15", [("Задача", "Иванов", "")]),
            _meeting("mtg-1b", "Еженедельная", "2024-01-22", [("Задача", "Иванов", "")]),
        ]
        _run_sync(spreadsheet, meetings, append=True)

        self.assertTrue(ws.update.called, "Expected ws.update for new mtg-2 task")
        new_rows = ws.update.call_args_list[0][0][1]
        mtg_ids = [r[6] for r in new_rows]
        self.assertIn("mtg-2", mtg_ids,
                      f"mtg-2 task must be appended; got rows: {new_rows}")
        self.assertNotIn("mtg-1", mtg_ids,
                         "mtg-1 task already exists, must not be re-appended")


class TestAppendIntraRunDedup(unittest.TestCase):
    """Same task appearing twice in one sync run (same meeting_id) is added once."""

    def test_intra_run_duplicate_skipped(self):
        ws = _make_ws("Еженедельная")
        spreadsheet = _make_spreadsheet({})
        spreadsheet.add_worksheet = MagicMock(return_value=ws)

        # Two meetings with same name → recurring; both contain duplicate task
        meetings = [
            {"id": "mtg-1", "name": "Еженедельная", "date": "2024-01-15",
             "tasks": [
                 {"name": "Задача", "assignee": "Иванов", "deadline": "", "done": False},
                 {"name": "Задача", "assignee": "Иванов", "deadline": "", "done": False},
             ]},
            {"id": "mtg-1b", "name": "Еженедельная", "date": "2024-01-22",
             "tasks": [{"name": "Другая", "assignee": "Иванов", "deadline": "", "done": False}]},
        ]
        _run_sync(spreadsheet, meetings)

        data = ws.update.call_args_list[0][0][1]
        task_rows = [r for r in data if r[0] != "№"]
        task_names = [r[2] for r in task_rows]
        self.assertEqual(task_names.count("Задача"), 1,
                         f"Duplicate 'Задача' should appear once, got: {task_names}")


class TestSevenColumnWrite(unittest.TestCase):
    """Every ws.update call must write 7-column ranges (A:G)."""

    def test_full_overwrite_uses_G_range(self):
        ws = _make_ws("Еженедельная")
        spreadsheet = _make_spreadsheet({"Еженедельная": ws})

        meetings = [
            _meeting("m1", "Еженедельная", "2024-01-15", [("Задача", "Иванов", "")]),
            _meeting("m2", "Еженедельная", "2024-01-22", [("Задача2", "Иванов", "")]),
        ]
        _run_sync(spreadsheet, meetings)

        range_arg = ws.update.call_args_list[0][0][0]
        self.assertIn(":G", range_arg,
                      f"Full overwrite must use A:G range, got {range_arg!r}")

    def test_new_one_time_sheet_uses_G_range(self):
        ws = _make_ws("Разовые встречи")
        spreadsheet = _make_spreadsheet({})
        spreadsheet.add_worksheet = MagicMock(return_value=ws)

        # Only 1 meeting with this name → one-time
        meetings = [
            _meeting("ot-1", "Разовая встреча", "2024-01-15", [("Задача", "Петров", "")]),
        ]
        _run_sync(spreadsheet, meetings)

        self.assertTrue(ws.update.called, "Expected ws.update call for one-time sheet")
        range_arg = ws.update.call_args_list[0][0][0]
        self.assertIn(":G", range_arg,
                      f"One-time new_sheet must use A:G range, got {range_arg!r}")

    def test_data_rows_contain_meeting_id_in_col7(self):
        ws = _make_ws("Еженедельная")
        spreadsheet = _make_spreadsheet({"Еженедельная": ws})

        meetings = [
            _meeting("m1", "Еженедельная", "2024-01-15", [("Задача", "Иванов", "")]),
            _meeting("m2", "Еженедельная", "2024-01-22", [("Задача2", "Иванов", "")]),
        ]
        _run_sync(spreadsheet, meetings)

        data = ws.update.call_args_list[0][0][1]
        data_rows = [r for r in data if r[0] != "№"]
        for row in data_rows:
            self.assertEqual(len(row), 7, f"Row must have 7 elements: {row}")
            self.assertIn(row[6], ("m1", "m2"),
                          f"Col G must be meeting_id, got {row[6]!r}")


class TestColumnGHidden(unittest.TestCase):
    """batch_update request for column G must set hiddenByUser=True."""

    def test_column_g_hidden_in_format_request(self):
        ws = _make_ws("Еженедельная")
        spreadsheet = _make_spreadsheet({"Еженедельная": ws})

        meetings = [
            _meeting("m1", "Еженедельная", "2024-01-15", [("Задача", "Иванов", "")]),
            _meeting("m2", "Еженедельная", "2024-01-22", [("Задача2", "Иванов", "")]),
        ]
        _run_sync(spreadsheet, meetings)

        all_requests = []
        for c in spreadsheet.batch_update.call_args_list:
            body = c[0][0] if c[0] else c[1].get("body", {})
            all_requests.extend(body.get("requests", []))

        col_g_reqs = [
            r for r in all_requests
            if "updateDimensionProperties" in r
            and r["updateDimensionProperties"]["range"].get("startIndex") == 6
        ]
        self.assertTrue(col_g_reqs, "No updateDimensionProperties for column G found")
        props = col_g_reqs[0]["updateDimensionProperties"]["properties"]
        self.assertTrue(props.get("hiddenByUser"),
                        f"Column G must have hiddenByUser=True, got: {props}")
        fields = col_g_reqs[0]["updateDimensionProperties"]["fields"]
        self.assertIn("hiddenByUser", fields,
                      f"fields must include 'hiddenByUser', got: {fields!r}")


class TestFormattingWarningsInResult(unittest.TestCase):
    """When batch_update raises, the error appears in the result string."""

    def test_format_error_in_result(self):
        ws = _make_ws("Еженедельная")
        spreadsheet = _make_spreadsheet({"Еженедельная": ws})
        spreadsheet.batch_update.side_effect = Exception("quota exceeded")

        meetings = [
            _meeting("m1", "Еженедельная", "2024-01-15", [("Задача", "Иванов", "")]),
            _meeting("m2", "Еженедельная", "2024-01-22", [("Задача2", "Иванов", "")]),
        ]
        result = _run_sync(spreadsheet, meetings)

        self.assertIn("Ошибки форматирования", result,
                      f"Expected 'Ошибки форматирования' in result:\n{result}")
        self.assertIn("quota exceeded", result,
                      f"Expected error text in result:\n{result}")


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        TestFullOverwriteReadBeforeClear,
        TestFullOverwriteStatusPreservation,
        TestAppendDedupIncludesMeetingId,
        TestAppendIntraRunDedup,
        TestSevenColumnWrite,
        TestColumnGHidden,
        TestFormattingWarningsInResult,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if result.failures or result.errors:
        sys.exit(1)
