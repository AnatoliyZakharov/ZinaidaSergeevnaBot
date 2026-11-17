"""
Тесты заголовков встреч на листе «Разовые встречи» (issue #41).

Решение PO: без объединения A:F.  Заголовок встречи «перекрывает соседние
ячейки» (wrapStrategy OVERFLOW_CELL), жирный шрифт и серая заливка A:F —
после общего формата листа (WRAP) на каждом синке.

Без сети: Google Sheets — фейк, который применяет запросы форматирования по
порядку и хранит итоговый wrapStrategy / формат строк; MYmeet и Calendar —
фейки из test_mymeet_daily.
Запуск: python test_grouper_format.py  (также входит в python test_smoke.py)
"""
import tempfile
from datetime import timedelta

import test_mymeet_daily as D
import test_smoke as _ts

PROTO_HDR = _ts._PROTO_HDR
ME = D.ME
SID = 7
GREY = {"red": 0.878, "green": 0.878, "blue": 0.878}
FIELDS = ("userEnteredFormat.textFormat,userEnteredFormat.backgroundColor,"
          "userEnteredFormat.wrapStrategy")
FORBIDDEN = ("mergeCells", "unmergeCells", "updateCells", "pasteData", "deleteRange",
             "deleteDimension", "insertDimension")


def _ms():
    return D._ms()


def _kinds(reqs):
    return [next(iter(r)) for r in reqs]


SHEET = [
    PROTO_HDR,
    ["Стратсессия  (10.11.2026)", "", "", "", "", "", ""],
    ["1", "10.11.2026", "Отчёт", ME, "", "В работе", "m1"],
    ["2", "10.11.2026", "Бюджет", "Анна", "20.11.2026", "Выполнено", "m1"],
    ["Совет  (11.11.2026)"],                       # укороченная строка из API
    ["3", "11.11.2026", "План", ME, "", "Требует верификации", "m2"],
]


# ── 1. Запросы ───────────────────────────────────────────────────────────────

def test_only_grouper_rows_overflow_bold_grey():
    ms = _ms()
    reqs, n = ms._grouper_requests(SID, SHEET)
    assert n == 2
    rc = [r["repeatCell"] for r in reqs if "repeatCell" in r]
    assert [c["range"]["startRowIndex"] for c in rc] == [1, 4]
    for c in rc:
        rng = c["range"]
        assert (rng["startColumnIndex"], rng["endColumnIndex"]) == (0, 6), rng
        assert rng["endRowIndex"] - rng["startRowIndex"] == 1
        fmt = c["cell"]["userEnteredFormat"]
        assert fmt["wrapStrategy"] == "OVERFLOW_CELL"
        assert fmt["textFormat"]["bold"] is True and fmt["backgroundColor"] == GREY
        assert c["fields"] == FIELDS                       # без verticalAlignment и др.
    assert not set(_kinds(reqs)) & set(FORBIDDEN), _kinds(reqs)


def test_no_fixed_height_only_autoresize():
    ms = _ms()
    rows = [PROTO_HDR, ["Очень длинное название встречи " * 12 + " (10.11.2026)"]] + SHEET[2:]
    reqs, _ = ms._grouper_requests(SID, rows)
    assert "updateDimensionProperties" not in _kinds(reqs)
    ar = [r["autoResizeDimensions"]["dimensions"] for r in reqs if "autoResizeDimensions" in r]
    assert [(d["dimension"], d["startIndex"], d["endIndex"]) for d in ar] == [
        ("ROWS", 1, 2), ("ROWS", 4, 5)], ar


def test_conflicting_values_not_grouper():
    """Текст в A и значение (даже пробел) в B:F — не заголовок: не трогаем."""
    ms = _ms()
    rows = [PROTO_HDR,
            ["Совещание  (12.11.2026)", "", "", "заметка", "", "", ""],
            ["С пробелом", None, " ", "", "", "", ""],
            ["Пустая метка", None, "", "", "", "", ""],
            ["", "", "", "", "", "", ""]]
    reqs, n = ms._grouper_requests(SID, rows)
    assert n == 1
    assert [r["repeatCell"]["range"]["startRowIndex"] for r in reqs if "repeatCell" in r] == [3]


def test_numeric_label_known_new_row():
    ms = _ms()
    rows = [PROTO_HDR, ["2025", "", "", "", "", "", ""]]
    assert ms._grouper_requests(SID, rows)[1] == 0
    assert ms._grouper_requests(SID, rows, known={1})[1] == 1


def test_no_merge_code_left():
    src = (_ts.REPO_ROOT / "mymeet_sync.py").read_text(encoding="utf-8")
    for word in ("mergeCells", "unmergeCells", "merges"):
        assert word not in src, word


# ── 2. Сквозные: фейковая таблица с форматом строк ───────────────────────────

class FmtBook(D.Book):
    """Book + применение repeatCell по порядку: итоговый wrap/формат строк."""

    def __init__(self, sheets=None):
        super().__init__(sheets)
        self.wrap = {}           # (title, row) -> wrapStrategy колонки A
        self.bold = {}
        self.batches = []
        self.ss.batch_update.side_effect = self._batch

    def _title(self, sid):
        return next(t for t, w in self.ws.items() if w.id == sid)

    def _batch(self, body):
        self.batches.append(body)
        D.Book._batch_update(self, {"requests": [
            r for r in body.get("requests", []) if "updateCells" in r]})
        for r in body.get("requests", []):
            k = next(iter(r))
            if k == "updateCells" and r[k].get("fields") == "userEnteredValue":
                # значения полного синка
                continue
            assert k not in FORBIDDEN, k
            if k != "repeatCell":
                continue
            rc = r["repeatCell"]
            rng = rc["range"]
            t = self._title(rng["sheetId"])
            if rng.get("startColumnIndex", 0) > 0:
                continue
            end = rng.get("endRowIndex", max(len(self.data[t]), 1))
            fmt = rc["cell"].get("userEnteredFormat", {})
            fields = rc["fields"]
            for row in range(rng.get("startRowIndex", 0), end):
                if fields == "userEnteredFormat" or "wrapStrategy" in fields:
                    self.wrap[(t, row)] = fmt.get("wrapStrategy")
                if fields == "userEnteredFormat" or "textFormat" in fields:
                    self.bold[(t, row)] = (fmt.get("textFormat") or {}).get("bold")
        return {}

    def groupers(self, t):
        return [i for i, r in enumerate(self.data[t])
                if i and r and r[0] and not str(r[0]).isdigit()
                and all(c in ("", None) for c in r[1:6])]


def _check_sheet(book, name):
    rows = book.data[name]
    gs = book.groupers(name)
    for i in range(1, len(rows)):
        if i in gs:
            assert book.wrap.get((name, i)) == "OVERFLOW_CELL", (i, book.wrap.get((name, i)))
            assert book.bold.get((name, i)) is True, i
        else:
            assert book.wrap.get((name, i)) == "WRAP", (i, book.wrap.get((name, i)))
    return gs


def _cfg(env):
    return {"gcal_organizer_data": env.mm.gcal(), "mymeet_spreadsheet_id": "sheet-1",
            "mymeet_token": "t"}


def test_sync_old_and_new_groupers_overflow_repeat_stable():
    """Append-синк: старые и новые заголовки — OVERFLOW_CELL после WRAP;
    задачи — WRAP; данные не меняются; повтор не возвращает WRAP заголовкам."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        name = env.ms.ONE_TIME_SHEET_NAME
        book = FmtBook({name: [list(r) for r in SHEET]})
        env.books["sheet-1"] = book
        env.mm.add("n1", "Новая встреча", D._now() - timedelta(hours=2), [("Смета", ME, False)])
        before = [list(r) for r in book.data[name]]
        env.force_due()
        env.tick()
        assert env.db.mymeet_sync_get(1)["last_outcome"] == "ok"
        rows = book.data[name]
        assert rows[:len(before)] == before
        assert _check_sheet(book, name) == [1, 4, len(before)]
        snapshot = [list(r) for r in rows]
        for _ in range(2):                                  # повторы
            env.force_due()
            env.tick()
            assert book.data[name] == snapshot
            assert _check_sheet(book, name) == [1, 4, len(before)]
        assert [r[1] for r in book.task_rows(name)].count("Смета") == 1


def test_full_sync_same_behaviour():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        name = env.ms.ONE_TIME_SHEET_NAME
        book = FmtBook({name: [list(r) for r in SHEET]})
        env.books["sheet-1"] = book
        book.ws[name].clear.side_effect = lambda: book.data.__setitem__(name, [])
        env.mm.add("n1", "Встреча А", D._now() - timedelta(hours=3), [("Т1", ME, False)])
        env.mm.add("n2", "Встреча Б", D._now() - timedelta(hours=2), [("Т2", ME, False)])
        for _ in range(2):
            env.ms.sync_to_sheets(days=1, append=False, user_config=_cfg(env))
            # #49: встречи вне окна (10–11.11) сохраняются со своими заголовками
            assert _check_sheet(book, name) == [1, 4, 6, 8]
            assert [r[1] for r in book.task_rows(name)] == ["Отчёт", "Бюджет", "План", "Т1", "Т2"]


def test_append_without_new_rows_restores_groupers():
    """Нечего добавлять — заголовки, созданные до исправления, всё равно чинятся."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        name = env.ms.ONE_TIME_SHEET_NAME
        book = FmtBook({name: [list(r) for r in SHEET]})
        env.books["sheet-1"] = book
        env.ms.sync_to_sheets(days=1, append=True, user_config=_cfg(env))
        assert _check_sheet(book, name) == [1, 4]


def test_user_merges_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        name = env.ms.ONE_TIME_SHEET_NAME
        book = FmtBook({name: [list(r) for r in SHEET]})
        env.books["sheet-1"] = book
        env.ms.sync_to_sheets(days=1, append=True, user_config=_cfg(env))
        env.ms.sync_to_sheets(days=1, append=False, user_config=_cfg(env))
        sent = [next(iter(r)) for b in book.batches for r in b["requests"]]
        assert not set(sent) & {"mergeCells", "unmergeCells"}, sent


def test_format_error_is_warning():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        name = env.ms.ONE_TIME_SHEET_NAME
        book = FmtBook({name: [list(r) for r in SHEET]})
        env.books["sheet-1"] = book
        real = book._batch

        def _batch(body):
            if any("OVERFLOW_CELL" in str(r) for r in body.get("requests", [])):
                raise RuntimeError("503")
            return real(body)
        book.ss.batch_update.side_effect = _batch
        env.mm.add("n1", "Новая", D._now() - timedelta(hours=2), [("Смета", ME, False)])
        env.force_due()
        env.tick()
        assert env.db.mymeet_sync_get(1)["last_outcome"] == "ok"
        assert [r[1] for r in book.task_rows(name)].count("Смета") == 1


TESTS = [
    (test_only_grouper_rows_overflow_bold_grey,
     "Разовые: заголовки — OVERFLOW_CELL, жирный, серый A:F"),
    (test_no_fixed_height_only_autoresize, "Разовые: без фиксированной высоты строк"),
    (test_conflicting_values_not_grouper, "Разовые: значения в B:F — не заголовок"),
    (test_numeric_label_known_new_row, "Разовые: числовое название новой встречи"),
    (test_no_merge_code_left, "Разовые: логики объединений в коде нет"),
    (test_sync_old_and_new_groupers_overflow_repeat_stable,
     "Разовые: append — старые и новые заголовки, повтор без WRAP"),
    (test_full_sync_same_behaviour, "Разовые: полный синк — то же поведение"),
    (test_append_without_new_rows_restores_groupers,
     "Разовые: синк без новых строк чинит старые заголовки"),
    (test_user_merges_untouched, "Разовые: пользовательские объединения не трогаются"),
    (test_format_error_is_warning, "Разовые: ошибка формата — предупреждение"),
]


if __name__ == "__main__":
    failed = []
    for fn, label in TESTS:
        try:
            fn()
            print(f"  ✓ {label}")
        except AssertionError as e:
            print(f"  ✗ {label}: {e}")
            failed.append(label)
    raise SystemExit(1 if failed else 0)
