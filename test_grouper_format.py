"""
Тесты заголовков встреч на листе «Разовые встречи» (issue #41).

Без сети: Google Sheets — фейк, который хранит значения и объединения ячеек
и применяет mergeCells / unmergeCells; MYmeet и Calendar — фейки из
test_mymeet_daily.
Запуск: python test_grouper_format.py  (также входит в python test_smoke.py)
"""
import tempfile
from datetime import timedelta

import test_mymeet_daily as D
import test_smoke as _ts

PROTO_HDR = _ts._PROTO_HDR
ME = D.ME
SID = 7
GROUPER_FMT = {"textFormat": {"bold": True, "fontSize": 10},
               "backgroundColor": {"red": 0.878, "green": 0.878, "blue": 0.878}}


def _ms():
    return D._ms()


def _kinds(reqs):
    return [next(iter(r)) for r in reqs]


def _rows_of(reqs, kind):
    out = []
    for r in reqs:
        if kind in r:
            rng = r[kind].get("range") or {}
            out.append(rng.get("startRowIndex", rng.get("startIndex")))
    return out


SHEET = [
    PROTO_HDR,
    ["Стратсессия  (10.11.2026)", "", "", "", "", "", ""],
    ["1", "10.11.2026", "Отчёт", ME, "", "В работе", "m1"],
    ["2", "10.11.2026", "Бюджет", "Анна", "20.11.2026", "Выполнено", "m1"],
    ["Совет  (11.11.2026)"],                       # укороченная строка API
    ["3", "11.11.2026", "План", ME, "", "Требует верификации", "m2"],
]


# ── 1. Логика запросов ───────────────────────────────────────────────────────

def test_merge_only_grouper_rows():
    ms = _ms()
    reqs, st = ms._grouper_requests(SID, SHEET, [])
    assert st == {"groupers": 2, "merged": 2, "already": 0, "conflicts": 0, "unmerged": 0}, st
    assert _rows_of(reqs, "mergeCells") == [1, 4]
    assert _rows_of(reqs, "repeatCell") == [1, 4]
    for r in reqs:
        if "mergeCells" in r:
            rng = r["mergeCells"]["range"]
            assert (rng["startColumnIndex"], rng["endColumnIndex"]) == (0, 6), rng
            assert rng["endRowIndex"] - rng["startRowIndex"] == 1
            assert r["mergeCells"]["mergeType"] == "MERGE_ALL"
    # Ничего не трогает шапку и строки задач, значения не пишутся
    touched = set(_rows_of(reqs, "mergeCells") + _rows_of(reqs, "repeatCell"))
    assert not touched & {0, 2, 3, 5}
    assert not any(k in ("updateCells", "pasteData", "deleteRange") for k in _kinds(reqs))


def test_format_mask_keeps_wrap():
    ms = _ms()
    reqs, _ = ms._grouper_requests(SID, SHEET, [])
    rc = [r["repeatCell"] for r in reqs if "repeatCell" in r]
    assert rc and all(c["fields"] == "userEnteredFormat.textFormat,userEnteredFormat.backgroundColor"
                      for c in rc), rc
    assert rc[0]["cell"]["userEnteredFormat"] == GROUPER_FMT


def test_existing_merges_idempotent_and_conflicts():
    ms = _ms()
    exact = {"sheetId": SID, "startRowIndex": 1, "endRowIndex": 2,
             "startColumnIndex": 0, "endColumnIndex": 6}
    user = {"sheetId": SID, "startRowIndex": 4, "endRowIndex": 5,
            "startColumnIndex": 0, "endColumnIndex": 3}     # A:C — пользовательское
    reqs, st = ms._grouper_requests(SID, SHEET, [exact, user])
    assert (st["already"], st["conflicts"], st["merged"], st["unmerged"]) == (1, 1, 0, 0), st
    assert "mergeCells" not in _kinds(reqs) and "unmergeCells" not in _kinds(reqs)
    assert _rows_of(reqs, "repeatCell") == [1, 4]           # формат — всем


def test_stale_merge_hiding_data_is_unmerged():
    """Объединение A:F на строке задачи (съехало после перезаписи) скрывает
    B:F — снимаем, данные не меняются; заголовок объединяется."""
    ms = _ms()
    stale = {"sheetId": SID, "startRowIndex": 2, "endRowIndex": 3,
             "startColumnIndex": 0, "endColumnIndex": 6}
    reqs, st = ms._grouper_requests(SID, SHEET, [stale])
    assert st["unmerged"] == 1 and st["merged"] == 2, st
    assert _kinds(reqs)[0] == "unmergeCells"                # снять раньше, чем объединять
    assert reqs[0]["unmergeCells"]["range"]["startRowIndex"] == 2


def test_header_merge_untouched():
    ms = _ms()
    hdr = {"sheetId": SID, "startRowIndex": 0, "endRowIndex": 1,
           "startColumnIndex": 0, "endColumnIndex": 6}
    reqs, st = ms._grouper_requests(SID, SHEET, [hdr])
    assert st["unmerged"] == 0 and "unmergeCells" not in _kinds(reqs)
    assert 0 not in _rows_of(reqs, "repeatCell") + _rows_of(reqs, "mergeCells")


def test_conflicting_values_not_merged():
    """Текст в A и значение в B:F — не заголовок: не объединяем и не красим."""
    ms = _ms()
    rows = [PROTO_HDR,
            ["Совещание  (12.11.2026)", "", "", "заметка", "", "", ""],
            ["Пустая метка", None, " ", "", "", "", ""]]
    reqs, st = ms._grouper_requests(SID, rows, [])
    assert st["groupers"] == 1 and _rows_of(reqs, "mergeCells") == [2], (st, reqs)
    assert 1 not in _rows_of(reqs, "repeatCell")


def test_long_name_row_height():
    ms = _ms()
    rows = [PROTO_HDR, ["Очень длинное название встречи " * 12 + " (10.11.2026)"]]
    reqs, _ = ms._grouper_requests(SID, rows, [])
    dims = [r["updateDimensionProperties"] for r in reqs if "updateDimensionProperties" in r]
    assert len(dims) == 1 and dims[0]["range"]["startIndex"] == 1
    assert dims[0]["properties"]["pixelSize"] > ms.GROUPER_LINE_PX
    short, _ = ms._grouper_requests(SID, SHEET, [])
    assert "updateDimensionProperties" not in _kinds(short)


def test_no_metadata_no_merge_changes():
    ms = _ms()
    reqs, st = ms._grouper_requests(SID, SHEET, None)
    assert set(_kinds(reqs)) == {"repeatCell"} and st["merged"] == 0


def test_numeric_label_known_new_row():
    """Новый заголовок с «числовым» названием встречи (без даты) — по позиции."""
    ms = _ms()
    rows = [PROTO_HDR, ["2025", "", "", "", "", "", ""]]
    assert ms._grouper_requests(SID, rows, [])[1]["groupers"] == 0
    assert ms._grouper_requests(SID, rows, [], known={1})[1]["merged"] == 1


# ── 2. Сквозные: синк → фейковая таблица с объединениями ─────────────────────

class MergeBook(D.Book):
    """Book + хранение объединений; batch_update применяет merge/unmerge."""

    def __init__(self, sheets=None, merges=None):
        super().__init__(sheets)
        self.merges = {t: [] for t in self.ws}
        for t, ms_ in (merges or {}).items():
            self.merges[t] = [dict(m, sheetId=self.ws[t].id) for m in ms_]
        self.batches = []
        self.ss.fetch_sheet_metadata.side_effect = self._meta
        self.ss.batch_update.side_effect = self._batch
        old_new = self._new

        def _new(title, rows):
            w = old_new(title, rows)
            self.merges.setdefault(title, [])
            return w
        self._new = _new
        self.ss.add_worksheet.side_effect = lambda title, rows, cols: self._new(title, [])

    def _title(self, sid):
        return next(t for t, w in self.ws.items() if w.id == sid)

    def _meta(self, params=None):
        return {"sheets": [{"properties": {"sheetId": w.id, "title": t},
                            "merges": [dict(m) for m in self.merges.get(t, [])]}
                           for t, w in self.ws.items()]}

    def _batch(self, body):
        self.batches.append(body)
        for r in body.get("requests", []):
            if "mergeCells" in r:
                rng = dict(r["mergeCells"]["range"])
                t = self._title(rng["sheetId"])
                for m in self.merges[t]:
                    assert not (m["startRowIndex"] < rng["endRowIndex"]
                                and rng["startRowIndex"] < m["endRowIndex"]
                                and m["startColumnIndex"] < rng["endColumnIndex"]
                                and rng["startColumnIndex"] < m["endColumnIndex"]), \
                        "API 400: пересечение объединений"
                self.merges[t].append(rng)
            elif "unmergeCells" in r:
                rng = r["unmergeCells"]["range"]
                t = self._title(rng["sheetId"])
                if "startRowIndex" not in rng:
                    self.merges[t] = []
                else:
                    self.merges[t] = [m for m in self.merges[t]
                                      if not (m["startRowIndex"] < rng.get("endRowIndex", 1e9)
                                              and rng["startRowIndex"] < m["endRowIndex"])]
        return {}

    def merged_rows(self, title):
        return sorted(m["startRowIndex"] for m in self.merges.get(title, [])
                      if (m["startColumnIndex"], m["endColumnIndex"]) == (0, 6)
                      and m["endRowIndex"] - m["startRowIndex"] == 1)


def _grouper_row_idx(rows):
    return [i for i, r in enumerate(rows) if i and r and r[0] and not str(r[0]).isdigit()
            and all(str(c).strip() == "" for c in r[1:6])]


def test_sync_restores_old_and_new_groupers_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        ms = env.ms
        name = ms.ONE_TIME_SHEET_NAME
        old = [list(r) for r in SHEET]
        book = MergeBook({name: old})
        env.books["sheet-1"] = book
        env.mm.add("n1", "Новая встреча", D._now() - timedelta(hours=2), [("Смета", ME, False)])
        before = [list(r) for r in book.data[name]]
        env.force_due()
        env.tick()
        assert env.db.mymeet_sync_get(1)["last_outcome"] == "ok"
        rows = book.data[name]
        # Данные старых строк не изменились, новая встреча дописана
        assert rows[:len(before)] == before, rows
        assert rows[len(before)][0].startswith("Новая встреча")
        groupers = _grouper_row_idx(rows)
        assert groupers == [1, 4, len(before)], groupers
        assert book.merged_rows(name) == groupers                # старые + новый
        # Повтор: без новых объединений, без изменения данных, без дублей
        snapshot = [list(r) for r in rows]
        merges_before = list(book.merges[name])
        env.force_due()
        env.tick()
        assert book.data[name] == snapshot
        assert book.merges[name] == merges_before
        last = book.batches[-1]["requests"] if book.batches else []
        assert not any("mergeCells" in r for b in book.batches[-3:]
                       for r in b["requests"]), "повтор не объединяет заново"
        assert len([r for r in book.task_rows(name) if r[1] == "Смета"]) == 1
        del last


def test_sync_conflicting_merge_left_untouched_and_warns():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        name = env.ms.ONE_TIME_SHEET_NAME
        user_merge = {"startRowIndex": 4, "endRowIndex": 5, "startColumnIndex": 1,
                      "endColumnIndex": 3}
        book = MergeBook({name: [list(r) for r in SHEET]}, merges={name: [user_merge]})
        env.books["sheet-1"] = book
        stats = {}
        cfg = {"gcal_organizer_data": env.mm.gcal(), "mymeet_spreadsheet_id": "sheet-1",
               "mymeet_token": "t"}
        res = env.ms.sync_to_sheets(days=1, append=True, user_config=cfg, stats=stats)
        assert book.merged_rows(name) == [1], book.merges[name]
        assert any(m["startColumnIndex"] == 1 for m in book.merges[name])   # не снято
        assert "не объединены" in res, res


def test_full_sync_drops_stale_merges_before_write():
    """/sync_mymeet_full: после clear() старые объединения снимаются ДО записи
    данных, затем заголовки объединяются по новым позициям."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        name = env.ms.ONE_TIME_SHEET_NAME
        stale = [{"startRowIndex": 2, "endRowIndex": 3, "startColumnIndex": 0,
                  "endColumnIndex": 6}]
        book = MergeBook({name: [list(r) for r in SHEET]}, merges={name: stale})
        env.books["sheet-1"] = book
        order = []
        ws = book.ws[name]
        ws.clear.side_effect = lambda: (order.append("clear"),
                                        book.data.__setitem__(name, []))
        orig_upd = ws.update.side_effect

        def _upd(*a, **k):
            order.append(("update", len(book.merges[name])))
            return orig_upd(*a, **k)
        ws.update.side_effect = _upd
        env.mm.add("n1", "Встреча А", D._now() - timedelta(hours=3), [("Т1", ME, False)])
        env.mm.add("n2", "Встреча Б", D._now() - timedelta(hours=2), [("Т2", ME, False)])
        cfg = {"gcal_organizer_data": env.mm.gcal(), "mymeet_spreadsheet_id": "sheet-1",
               "mymeet_token": "t"}
        env.ms.sync_to_sheets(days=1, append=False, user_config=cfg)
        assert order[0] == "clear" and order[1] == ("update", 0), order
        rows = book.data[name]
        assert book.merged_rows(name) == _grouper_row_idx(rows) == [1, 3], (rows, book.merges)


def test_sync_survives_metadata_failure():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        name = env.ms.ONE_TIME_SHEET_NAME
        book = MergeBook({name: [list(r) for r in SHEET]})
        env.books["sheet-1"] = book
        calls = {"n": 0}
        real = book._meta

        def _meta(params=None):
            calls["n"] += 1
            if "merges" in str(params):
                raise RuntimeError("meta-503")
            return real(params)
        book.ss.fetch_sheet_metadata.side_effect = _meta
        env.mm.add("n1", "Новая", D._now() - timedelta(hours=2), [("Смета", ME, False)])
        env.force_due()
        env.tick()
        assert env.db.mymeet_sync_get(1)["last_outcome"] == "ok"
        assert book.merges[name] == []                          # без метаданных — не трогаем
        assert [r[1] for r in book.task_rows(name)].count("Смета") == 1


TESTS = [
    (test_merge_only_grouper_rows, "Разовые: объединяются только заголовки встреч A:F"),
    (test_format_mask_keeps_wrap, "Разовые: формат заголовка не сбрасывает перенос"),
    (test_existing_merges_idempotent_and_conflicts,
     "Разовые: существующие/чужие объединения — без повторов и конфликтов"),
    (test_stale_merge_hiding_data_is_unmerged, "Разовые: съехавшее объединение снимается"),
    (test_header_merge_untouched, "Разовые: шапка таблицы не трогается"),
    (test_conflicting_values_not_merged, "Разовые: значения в B:F — не объединяем"),
    (test_long_name_row_height, "Разовые: длинное название — высота строки"),
    (test_no_metadata_no_merge_changes, "Разовые: нет метаданных — объединения не меняются"),
    (test_numeric_label_known_new_row, "Разовые: числовое название новой встречи"),
    (test_sync_restores_old_and_new_groupers_idempotent,
     "Разовые: синк чинит старые и новые заголовки, повтор идемпотентен"),
    (test_sync_conflicting_merge_left_untouched_and_warns,
     "Разовые: пользовательское объединение не трогается, предупреждение"),
    (test_full_sync_drops_stale_merges_before_write,
     "Разовые: полный синк снимает старые объединения до записи"),
    (test_sync_survives_metadata_failure, "Разовые: сбой метаданных не ломает синк"),
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
