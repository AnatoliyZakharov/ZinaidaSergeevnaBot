"""
#49 B-1: /sync_mymeet_full без промежуточной очистки листов.

Полная перезапись сначала читает и рассчитывает все листы протоколов, затем
пишет их ОДНИМ spreadsheets.batchUpdate (updateCells, fields=userEnteredValue).
Google применяет такой запрос целиком или не применяет вовсе — фейк таблицы
(test_mymeet_daily.Book) моделирует это: при отказе ничего не меняется,
выход за сетку отклоняет весь запрос.

Сценарии: отказ записи, таймаут после применения, неподтверждённый результат,
обрыв процесса до и после применения, повтор, рост и сокращение листа,
формат не трогается, обычный импорт не изменился.

Без сети, без реальных данных.
Запуск: python test_full_sync_atomic.py  (также входит в python test_smoke.py)
"""
import tempfile
from datetime import timedelta

import test_mymeet_daily as D
import test_smoke as _ts
from test_sync_audit import _all_task_keys, _assert_no_dupes, _full_cfg

ME = D.ME
PROTO_HDR = _ts._PROTO_HDR
REG = "Планёрка"


def _scenario(env):
    """Таблица со строками вне окна и ручными статусами + новые встречи окна."""
    ot = env.ms.ONE_TIME_SHEET_NAME
    book = D.Book({
        ot: [PROTO_HDR, ["Старая встреча  (01.08.2026)", "", "", "", "", "", ""],
             ["1", "01.08.2026", "Старая задача", ME, "", "Выполнено", "old1"]],
        REG: [PROTO_HDR,
              ["1", "02.08.2026", "Старая регулярная", ME, "", "В работе", "old2"]],
    })
    for t in list(book.ws):        # clear() реально очищает (как в Google)
        book.ws[t].clear.side_effect = (lambda tt: (lambda: book.data.__setitem__(tt, [])))(t)
    env.books["sheet-1"] = book
    n = D._now()
    env.mm.add("n1", "Новая встреча", n - timedelta(hours=2), [("Новая", ME, False)])
    env.mm.add("p1", REG, n - timedelta(hours=3), [("Свежая", ME, False)])
    env.mm.add("p2", REG, n - timedelta(hours=1), [("Свежая 2", ME, False)])
    env.mm.add("r1", "Совет", n - timedelta(hours=5), [("Р1", ME, False)])
    env.mm.add("r2", "Совет", n - timedelta(hours=4), [("Р2", ME, False)])
    return book


def _snapshot(book):
    return {t: [list(map(str, r)) for r in rows] for t, rows in book.data.items()}


def _content(snap):
    """Содержимое листов протоколов без учёта порядка строк и нумерации:
    задачи одной даты MYmeet отдаёт в произвольном порядке."""
    return {t: sorted(r[1:] for r in rows) for t, rows in snap.items()
            if not t.startswith("Мои задачи")}


def _protocols(book):
    return {t: rows for t, rows in _snapshot(book).items() if not t.startswith("Мои задачи")}


def _reference(_unused=None):
    """Итог той же синхронизации без сбоев — эталон для сравнения."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        book = _scenario(env)
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        return _snapshot(book)


def _value_writes(book):
    """Вызовы batch_update, которые пишут значения (updateCells/userEnteredValue)."""
    out = []
    for c in book.ss.batch_update.call_args_list:
        reqs = c.args[0]["requests"]
        if any((r.get("updateCells") or {}).get("fields") == "userEnteredValue" for r in reqs):
            out.append(reqs)
    return out


def _expect_ok_state(book, statuses=True):
    keys = {k[1] for k in _all_task_keys(book)}
    assert {"Старая задача", "Старая регулярная", "Новая", "Свежая", "Свежая 2",
            "Р1", "Р2"} <= keys, keys
    _assert_no_dupes(book)
    if statuses:
        st = {r[2]: r[5] for rows in book.data.values() for r in rows[1:] if len(r) > 5}
        assert st["Старая задача"] == "Выполнено" and st["Старая регулярная"] == "В работе", st


def _full_sync_error(env):
    """Запустить полный синк, который должен завершиться ошибкой; вернуть её."""
    try:
        res = env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
    except SystemExit:
        raise
    except Exception as e:      # noqa: BLE001 — тип проверяет вызывающий
        return e
    raise AssertionError(f"ошибка записи не должна выглядеть как успех: {res[:80]}")


def _my_tasks(env):
    return sorted(r[3] for r in env.my_tasks_sheet()[1:])


ALL_MINE = sorted(["Старая задача", "Старая регулярная", "Новая", "Свежая", "Свежая 2",
                   "Р1", "Р2"])


# ── 1. Механизм: без clear, одна атомарная запись ────────────────────────────

def test_no_clear_single_atomic_write():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        book = _scenario(env)
        res = env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        assert res.startswith("✅"), res
        assert not any(w.clear.called for w in book.ws.values())
        assert not any(w.batch_clear.called for t, w in book.ws.items()
                       if not t.startswith("Мои задачи"))
        writes = _value_writes(book)
        assert len(writes) == 1 and book.commits == 1, writes
        reqs = writes[0]
        cells = [r["updateCells"] for r in reqs if "updateCells" in r]
        ids = {c["range"]["sheetId"] for c in cells}
        protocol_ids = {w.id for t, w in book.ws.items() if not t.startswith("Мои задачи")}
        assert ids == protocol_ids, (ids, protocol_ids)       # все листы — в одном запросе
        for c in cells:
            assert c["fields"] == "userEnteredValue"            # формат не трогаем
            # без заметок правее G — ровно A:G
            assert (c["range"]["startColumnIndex"], c["range"]["endColumnIndex"]) == (0, 7)
        assert {next(iter(r)) for r in reqs} <= {"updateCells", "updateSheetProperties"}
        # протоколы не меняли значения до этой записи (только чтения)
        _expect_ok_state(book)
        assert _my_tasks(env) == ALL_MINE


# ── 2. Отказ записи: ничего не изменено, не «успех», повтор восстанавливает ──

def test_write_rejected_nothing_changed_then_retry():
    with tempfile.TemporaryDirectory() as tmp:
        ref = _reference()
        env = D.Env(tmp)
        book = _scenario(env)
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        env.mm.add("n2", "Ещё встреча", D._now() - timedelta(minutes=30), [("Ещё", ME, False)])
        before = _snapshot(book)
        mine_before = _my_tasks(env)
        book.commit_fail = (RuntimeError("HTTP 400"), False)
        e = _full_sync_error(env)
        assert _snapshot(book) == before                  # ни одна ячейка не изменилась
        assert _my_tasks(env) == mine_before              # «Мои задачи» не тронуты
        msg = str(e)
        assert type(e).__name__ == "FullSyncWriteError", repr(e)
        assert "не изменены" in msg and "✅" not in msg, msg
        assert "Ещё" not in msg and ME not in msg and REG not in msg, msg   # без данных
        book.commit_fail = None                           # повтор
        res = env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        assert res.startswith("✅"), res
        _expect_ok_state(book)
        assert _my_tasks(env) == sorted(ALL_MINE + ["Ещё"])
        assert _content(_snapshot(book))[REG] == _content(ref)[REG]


def test_rejected_first_write_on_new_sheets():
    """Отказ записи при первом полном синке (листы создаются): данные не теряются,
    созданные листы пусты, повтор не создаёт дублей и вторых листов."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        book = _scenario(env)
        before = _snapshot(book)
        book.commit_fail = (ConnectionError("reset"), False)
        e = _full_sync_error(env)
        snap = _snapshot(book)
        for t, rows in before.items():
            assert snap[t] == rows, t
        assert all(snap[t] == [] for t in snap if t not in before), snap   # только пустые
        assert type(e).__name__ == "FullSyncWriteError", repr(e)
        book.commit_fail = None
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        _expect_ok_state(book)
        assert sorted(book.data) == sorted(set(book.data))
        assert book.ss.add_worksheet.call_count == len(
            [t for t in book.data if t not in before]), book.ss.add_worksheet.call_args_list


# ── 3. Неопределённый результат (таймаут) ────────────────────────────────────

def test_timeout_after_apply_confirmed_by_readback():
    """Google применил запрос, ответ потерян: проверка чтением подтверждает
    запись — успех без повторной записи и без дублей."""
    with tempfile.TemporaryDirectory() as tmp:
        ref = _reference()
        env = D.Env(tmp)
        book = _scenario(env)
        book.commit_fail = (TimeoutError("read timeout"), True)
        res = env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        assert res.startswith("✅"), res
        assert _content(_snapshot(book)) == _content(ref)
        assert _my_tasks(env) == ALL_MINE
        book.commit_fail = None
        snap = _snapshot(book)
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        assert _content(_snapshot(book)) == _content(snap)   # повтор идемпотентен
        _expect_ok_state(book)


def test_unconfirmed_result_is_not_success():
    """Запись упала и проверить результат не удалось — не «успех», «Мои
    задачи» не перестраиваются; данные целы в обоих исходах."""
    for applied in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            ref = _reference()
            env = D.Env(tmp)
            book = _scenario(env)
            before = _snapshot(book)
            real = book._batch_update

            def _bu(body, real=real, book=book, applied=applied):
                if any((r.get("updateCells") or {}).get("fields") == "userEnteredValue"
                       for r in body.get("requests", [])):
                    if applied:
                        book.apply_requests(body["requests"])
                    book.fail_reads = True                # проверочное чтение тоже падает
                    raise TimeoutError("timeout")
                return real(body)
            book.ss.batch_update.side_effect = _bu
            e = _full_sync_error(env)
            assert "не подтверждён" in str(e) and "✅" not in str(e), repr(e)
            assert env.my_tasks_sheet() == []             # личный лист не строился
            prot = _protocols(book)
            if applied:
                assert _content(prot) == _content(ref), prot
            else:
                for t, rows in before.items():
                    assert prot[t] == rows, t
            book.fail_reads = False                       # повтор
            book.ss.batch_update.side_effect = real
            res = env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
            assert res.startswith("✅"), res
            _expect_ok_state(book)
            assert _my_tasks(env) == ALL_MINE


# ── 4. Обрыв процесса во время записи ────────────────────────────────────────

def test_process_killed_during_write_then_restart():
    for applied in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            ref = _reference()
            env = D.Env(tmp)
            book = _scenario(env)
            before = _snapshot(book)
            book.commit_fail = (SystemExit("kill -9"), applied)
            try:
                env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
                raise AssertionError("процесс должен был «умереть»")
            except SystemExit:
                pass
            prot = _protocols(book)
            # Ни одного пустого или частично записанного листа протокола
            if applied:
                assert _content(prot) == _content(ref), prot
            else:
                for t, rows in before.items():
                    assert prot[t] == rows, t
            book.commit_fail = None                       # перезапуск и повтор
            env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
            assert _content(_snapshot(book)) == _content(ref)
            _expect_ok_state(book)
            assert _my_tasks(env) == ALL_MINE


# ── 5. Размер листа: рост и сокращение в том же запросе ──────────────────────

def test_sheet_grows_beyond_grid_in_same_request():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        book = _scenario(env)
        book.ws[REG].row_count = 2                       # сетка меньше новых данных
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        reqs = _value_writes(book)[0]
        sid = book.ws[REG].id
        kinds = [(next(iter(r)), (r.get("updateSheetProperties") or r.get("updateCells")
                                  or {}).get("properties", {}).get("sheetId")
                  or (r.get("updateCells") or {}).get("range", {}).get("sheetId"))
                 for r in reqs]
        i_props = kinds.index(("updateSheetProperties", sid))
        i_cells = kinds.index(("updateCells", sid))
        assert i_props < i_cells, kinds
        assert len(book.data[REG]) == 4, book.data[REG]   # шапка + 3 задачи
        _expect_ok_state(book)


def test_sheet_shrinks_tail_cleared_atomically():
    """Протокол встречи окна сократился: лишние прежние строки очищаются в
    том же запросе — без устаревших строк и дублей."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        n = D._now()
        book = D.Book({REG: [PROTO_HDR,
                             ["1", "x", "a", ME, "", "В работе", "p1"],
                             ["2", "x", "b", ME, "", "В работе", "p1"],
                             ["3", "x", "c", ME, "", "В работе", "p1"],
                             ["4", "x", "d", ME, "", "В работе", "p1"]]})
        env.books["sheet-1"] = book
        env.mm.add("p1", REG, n - timedelta(hours=3), [("Одна", ME, False)])
        env.mm.add("p2", REG, n - timedelta(hours=1), [("Две", ME, False)])
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        assert book.commits == 1
        assert sorted(r[2] for r in book.data[REG][1:]) == ["Две", "Одна"], book.data[REG]
        cells = [r["updateCells"] for r in _value_writes(book)[0] if "updateCells" in r
                 and r["updateCells"]["range"]["sheetId"] == book.ws[REG].id][0]
        assert cells["range"]["endRowIndex"] == 5         # высота прежнего листа
        assert len(cells["rows"]) == 3                    # хвост очищается тем же запросом


# ── 6. Структура: колонки правее G, совпадение имени листа ───────────────────

def test_user_columns_follow_their_row():
    """Заметка в H остаётся напротив своей задачи после пересортировки;
    заметка строки, которой больше нет, очищается (как раньше clear)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        book = _scenario(env)
        book.data[REG][1] = book.data[REG][1] + ["заметка-старой"]
        book.data[REG].append(["2", "x", "удалённая", ME, "", "В работе", "p1", "сирота"])
        ot = env.ms.ONE_TIME_SHEET_NAME
        book.data[ot][1] = book.data[ot][1] + ["к заголовку"]
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        notes = {r[2]: r[7] for r in book.data[REG][1:] if len(r) > 7 and r[7]}
        assert notes == {"Старая регулярная": "заметка-старой"}, book.data[REG]
        assert [r[7] for r in book.data[ot] if len(r) > 7 and r[7]] == ["к заголовку"]
        assert next(r for r in book.data[ot] if len(r) > 7 and r[7])[0] == \
            "Старая встреча  (01.08.2026)"
        _expect_ok_state(book)
        snap = _snapshot(book)                            # повтор — те же заметки
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        assert {r[2]: r[7] for r in book.data[REG][1:] if len(r) > 7 and r[7]} == notes
        assert _content(_snapshot(book)) == _content(snap)


def _user_cells(rows):
    """{(задача или заголовок, колонка): значение} для всех непустых ячеек
    правее G — заметки и к какой строке они относятся."""
    out = {}
    for r in rows[1:]:
        owner = r[2] if len(r) > 2 and str(r[2]).strip() else r[0]
        for j in range(7, len(r)):
            if str(r[j]).strip():
                out[(owner, j)] = r[j]
    return out


def _b2_book(env, rows):
    book = D.Book({REG: [PROTO_HDR] + rows})
    env.books["sheet-1"] = book
    return book


def test_b2_removed_task_note_not_orphaned():
    """B-2: у исключённой задачи заметка в H, у оставшихся заметок нет —
    после синка заметка не остаётся напротив другой задачи."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        n = D._now()
        book = _b2_book(env, [
            ["1", "x", "Удалённая", ME, "", "В работе", "p1", "заметка H"],
            ["2", "x", "Удалённая 2", ME, "", "В работе", "p1", "", "заметка I", "", "заметка K"],
        ])
        env.mm.add("p1", REG, n - timedelta(hours=3), [("Новая А", ME, False)])
        env.mm.add("p2", REG, n - timedelta(hours=1), [("Новая Б", ME, False)])
        for _ in range(2):                                # повтор — тот же итог
            env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
            assert _user_cells(book.data[REG]) == {}, book.data[REG]
            assert sorted(r[2] for r in book.data[REG][1:]) == ["Новая А", "Новая Б"]


def test_b2_notes_in_several_columns_follow_task():
    """Заметки в H, I и K остаются при своей задаче после пересортировки;
    заметка исключённой задачи в дальней колонке (M) очищается, хотя у
    оставшихся строк такой колонки нет."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        n = D._now()
        book = _b2_book(env, [
            ["1", "01.08.2026", "Старая", ME, "", "Выполнено", "old", "h1", "i1", "", "k1"],
            ["2", "x", "Удалённая", ME, "", "В работе", "p1", "", "", "", "", "", "m-сирота"],
            ["3", "02.08.2026", "Вторая старая", ME, "01.01.2020", "В работе", "old2", "h2"],
        ])
        env.mm.add("p1", REG, n - timedelta(hours=3), [("Новая А", ME, False)])
        env.mm.add("p2", REG, n - timedelta(hours=1), [("Новая Б", ME, False)])
        want = {("Старая", 7): "h1", ("Старая", 8): "i1", ("Старая", 10): "k1",
                ("Вторая старая", 7): "h2"}
        for _ in range(2):
            env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
            assert _user_cells(book.data[REG]) == want, book.data[REG]
            # пересортировка действительно была: «Вторая старая» (срок 2020) — первая
            assert book.data[REG][1][2] == "Вторая старая", book.data[REG]
        cells = [r["updateCells"] for r in _value_writes(book)[-1] if "updateCells" in r]
        assert cells[0]["range"]["endColumnIndex"] == 13    # A:M — ширина старых данных
        assert cells[0]["fields"] == "userEnteredValue"


def test_b2_shrink_with_notes_in_tail():
    """Лист сократился: заметки в хвостовых строках очищаются вместе с ними,
    заметка оставшейся задачи переезжает на её новую строку."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        n = D._now()
        book = _b2_book(env, [
            ["1", "x", "a", ME, "", "В работе", "p1", "na"],
            ["2", "x", "b", ME, "", "В работе", "p1", "nb", "ib"],
            ["3", "x", "c", ME, "", "В работе", "p1", "", "", "jc"],
            ["4", "01.08.2026", "Старая", ME, "", "Выполнено", "old", "keep"],
        ])
        env.mm.add("p1", REG, n - timedelta(hours=3), [("Одна", ME, False)])
        env.mm.add("p2", REG, n - timedelta(hours=1), [("Две", ME, False)])
        for _ in range(2):
            env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
            assert len(book.data[REG]) == 4, book.data[REG]   # шапка + 3 задачи
            assert _user_cells(book.data[REG]) == {("Старая", 7): "keep"}, book.data[REG]
            st = {r[2]: r[5] for r in book.data[REG][1:]}
            assert st["Старая"] == "Выполнено", st
        assert book.commits == 2


def test_b2_notes_with_write_failures():
    """Заметки и сбои записи: отказ — заметки на прежних местах; таймаут после
    применения подтверждается чтением на полную ширину; повтор — без сирот."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        n = D._now()
        book = _b2_book(env, [
            ["1", "x", "Удалённая", ME, "", "В работе", "p1", "", "сирота-I"],
            ["2", "01.08.2026", "Старая", ME, "", "Выполнено", "old", "h-старой"],
        ])
        env.mm.add("p1", REG, n - timedelta(hours=3), [("Новая", ME, False)])
        env.mm.add("p2", REG, n - timedelta(hours=1), [("Новая 2", ME, False)])
        before = _snapshot(book)
        book.commit_fail = (RuntimeError("HTTP 400"), False)
        e = _full_sync_error(env)
        assert type(e).__name__ == "FullSyncWriteError" and "не изменены" in str(e), repr(e)
        assert _snapshot(book)[REG] == before[REG]
        book.commit_fail = (TimeoutError("timeout"), True)
        res = env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        assert res.startswith("✅"), res
        assert _user_cells(book.data[REG]) == {("Старая", 7): "h-старой"}, book.data[REG]
        book.commit_fail = None
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        assert _user_cells(book.data[REG]) == {("Старая", 7): "h-старой"}, book.data[REG]
        _assert_no_dupes(book)


def test_notes_duplicate_groupers_and_keys():
    """Одинаковые подписи заголовков и одинаковые ключи задач: каждая заметка
    остаётся при своей строке (n-я — при n-й), ничего не теряется."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        ot = env.ms.ONE_TIME_SHEET_NAME
        book = D.Book({ot: [
            PROTO_HDR,
            ["Созвон  (01.08.2026)", "", "", "", "", "", "", "note-A"],
            ["1", "01.08.2026", "Дубль", ME, "", "В работе", "s1", "n1"],
            ["2", "01.08.2026", "Дубль", ME, "", "Выполнено", "s1", "n2"],
            ["Созвон  (01.08.2026)", "", "", "", "", "", "", "note-B"],
            ["3", "01.08.2026", "Другая", ME, "", "В работе", "s2", "", "i-другой"],
        ]})
        env.books["sheet-1"] = book
        env.mm.add("n1", "Новая", D._now() - timedelta(hours=2), [("Н", ME, False)])
        for _ in range(2):
            env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
            rows = book.data[ot]
            got = [(r[0] if not str(r[1]).strip() else (r[2], r[5]), r[7] if len(r) > 7 else "",
                    r[8] if len(r) > 8 else "") for r in rows[1:]]
            assert got[:5] == [
                ("Созвон  (01.08.2026)", "note-A", ""), (("Дубль", "В работе"), "n1", ""),
                (("Дубль", "Выполнено"), "n2", ""), ("Созвон  (01.08.2026)", "note-B", ""),
                (("Другая", "В работе"), "", "i-другой")], got
            assert all(not (len(r) > 7 and any(str(c).strip() for c in r[7:]))
                       for r in rows[6:]), rows


def test_notes_window_meeting_vs_carried_same_label():
    """Заголовок встречи окна (пересоздаётся из MYmeet) и перенесённый блок
    с той же подписью: заметки не меняются местами."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        ot = env.ms.ONE_TIME_SHEET_NAME
        t = D._now() - timedelta(hours=2)
        env.mm.add("w1", "Совещание", t, [("Задача окна", ME, False)])
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        book = env.book()
        label = next(r[0] for r in book.data[ot][1:] if not str(r[1]).strip())
        rows = book.data[ot]
        # Над ним — старый блок с той же подписью вне окна (другая встреча)
        old_block = [[label, "", "", "", "", "", "", "note-old"],
                     ["1", "01.08.2026", "Старая", ME, "", "Выполнено", "o1", "t-old"]]
        rows[1] = list(rows[1]) + [""] * (7 - len(rows[1])) + ["note-win"]
        book.data[ot] = [rows[0]] + old_block + [list(r) for r in rows[1:]]
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        vals = sorted(c for r in book.data[ot][1:] for c in r[7:] if str(c).strip())
        assert vals == ["note-old", "note-win", "t-old"], book.data[ot]
        r_old = next(i for i, r in enumerate(book.data[ot]) if len(r) > 2 and r[2] == "Старая")
        r_win = next(i for i, r in enumerate(book.data[ot]) if len(r) > 2 and r[2] == "Задача окна")
        assert book.data[ot][r_old - 1][7] == "note-old", book.data[ot]
        assert book.data[ot][r_win - 1][7] == "note-win", book.data[ot]


def test_legacy_row_note_kept_when_task_in_window():
    """Legacy-строка без mtg_id, чья задача снова пришла из MYmeet: заметка
    переходит к строке задачи вместе со статусом."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        n = D._now()
        env.mm.add("p1", REG, n - timedelta(hours=3), [("Legacy", ME, False)])
        env.mm.add("p2", REG, n - timedelta(hours=1), [("Другая", ME, False)])
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        book = env.book()
        date = next(r[1] for r in book.data[REG][1:] if r[2] == "Legacy")
        book.data[REG] = [PROTO_HDR,
                          ["1", date, "Legacy", ME, "", "В работе", "", "note-L"],
                          ["2", date, "Другая", ME, "", "Требует верификации", "p2"]]
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        assert _user_cells(book.data[REG]) == {("Legacy", 7): "note-L"}, book.data[REG]
        assert {r[2]: r[5] for r in book.data[REG][1:]}["Legacy"] == "В работе"
        _assert_no_dupes(book)


def test_grid_columns_grow_in_same_request():
    """Заметки дальше текущей ширины сетки: columnCount растёт тем же запросом."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        n = D._now()
        book = _b2_book(env, [["1", "01.08.2026", "Старая", ME, "", "Выполнено", "old",
                               "", "", "", "", "", "m"]])
        book.ws[REG].col_count = 7                        # фейк отклонит выход за сетку
        env.mm.add("p1", REG, n - timedelta(hours=3), [("А", ME, False)])
        env.mm.add("p2", REG, n - timedelta(hours=1), [("Б", ME, False)])
        res = env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        assert res.startswith("✅"), res
        reqs = _value_writes(book)[0]
        props = [r["updateSheetProperties"] for r in reqs if "updateSheetProperties" in r
                 and r["updateSheetProperties"]["properties"]["sheetId"] == book.ws[REG].id]
        assert props and props[0]["properties"]["gridProperties"]["columnCount"] == 13, props
        assert _user_cells(book.data[REG]) == {("Старая", 12): "m"}, book.data[REG]


def test_unknown_grid_size_never_shrinks():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        book = _scenario(env)
        class _Broken:
            def __int__(self):
                raise RuntimeError("no props")
        book.ws[REG].row_count = _Broken()
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        sid = book.ws[REG].id
        assert not any("updateSheetProperties" in r and
                       r["updateSheetProperties"]["properties"]["sheetId"] == sid
                       for r in _value_writes(book)[0])
        _expect_ok_state(book)


def test_unknown_grid_size_data_too_big_rejected_safely():
    """Размер сетки неизвестен, данных больше сетки: Google отклоняет весь
    запрос — «не изменены», данные целы, не «успех»."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        book = _scenario(env)

        class _Broken:
            def __int__(self):
                raise RuntimeError("no props")
        book.ws[REG].row_count = _Broken()
        book.true_grid[(REG, "row_count")] = 2            # фактическая сетка — 2 строки
        before = _snapshot(book)
        e = _full_sync_error(env)
        assert type(e).__name__ == "FullSyncWriteError" and "не изменены" in str(e), repr(e)
        snap = _snapshot(book)
        assert {t: snap[t] for t in before} == before
        assert env.my_tasks_sheet() == []


def test_note_follows_task_to_another_sheet():
    """Встреча стала регулярной: её задача переезжает из «Разовых встреч» в
    лист регулярной встречи вместе со статусом и заметками правее G; в
    «Разовых» заметка не остаётся."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        ot = env.ms.ONE_TIME_SHEET_NAME
        n = D._now()
        env.mm.add("p1", REG, n - timedelta(hours=3), [("Переезд", ME, False)])
        env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
        book = env.book()
        rows = book.data[ot]
        i = next(j for j, r in enumerate(rows) if len(r) > 2 and r[2] == "Переезд")
        rows[i] = (list(rows[i]) + [""] * 7)[:7] + ["note-H", "", "note-J"]
        rows[i][5] = "В работе"
        rows[i - 1] = (list(rows[i - 1]) + [""] * 7)[:7] + ["note-заголовка"]
        env.mm.add("p2", REG, n - timedelta(hours=1), [("Вторая", ME, False)])  # стала регулярной
        for _ in range(2):
            env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
            assert _user_cells(book.data[REG]) == {("Переезд", 7): "note-H",
                                                   ("Переезд", 9): "note-J"}, book.data[REG]
            assert {r[2]: r[5] for r in book.data[REG][1:]}["Переезд"] == "В работе"
            assert not any(len(r) > 7 and any(str(c).strip() for c in r[7:])
                           for r in book.data[ot]), book.data[ot]
            _assert_no_dupes(book)


def test_recurring_named_like_one_time_sheet_atomic():
    """Регулярная встреча с именем листа «Разовые встречи»: один лист в плане,
    отказ записи не меняет его, повтор — без дублей."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        book = _scenario(env)
        ot = env.ms.ONE_TIME_SHEET_NAME
        n = D._now()
        env.mm.add("c1", ot, n - timedelta(hours=7), [("К1", ME, False)])
        env.mm.add("c2", ot, n - timedelta(hours=6), [("К2", ME, False)])
        before = _snapshot(book)
        book.commit_fail = (RuntimeError("HTTP 400"), False)
        e = _full_sync_error(env)
        assert type(e).__name__ == "FullSyncWriteError", repr(e)
        snap = _snapshot(book)
        assert {t: snap[t] for t in before} == before     # прежние листы не тронуты
        assert all(snap[t] == [] for t in snap if t not in before)   # новые — пустые
        book.commit_fail = None
        for _ in range(2):
            env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
            names = sorted(r[1] for r in book.task_rows(ot))
            assert names == sorted(["Новая", "Старая задача", "К1", "К2"]), names
            _expect_ok_state(book)
        cells = [r["updateCells"]["range"]["sheetId"] for r in _value_writes(book)[-1]
                 if "updateCells" in r]
        assert len(cells) == len(set(cells)), cells       # лист один раз в запросе


# ── 7. Обычный и вечерний импорт не изменились ───────────────────────────────

def test_append_and_auto_import_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        book = _scenario(env)
        env.force_due()
        env.tick()                                        # вечерний автоимпорт
        env.ms.sync_to_sheets(days=30, append=True, user_config=_full_cfg(env))   # /sync_mymeet
        assert book.commits == 0 and not _value_writes(book)
        assert not any(w.clear.called for w in book.ws.values())
        _expect_ok_state(book)


TESTS = [
    (test_no_clear_single_atomic_write, "Полный синк B-1: без clear, одна атомарная запись"),
    (test_write_rejected_nothing_changed_then_retry,
     "Полный синк B-1: отказ записи — ничего не изменено, повтор без потерь"),
    (test_rejected_first_write_on_new_sheets,
     "Полный синк B-1: отказ при создании листов — данные целы, без дублей"),
    (test_timeout_after_apply_confirmed_by_readback,
     "Полный синк B-1: таймаут после применения — подтверждено чтением"),
    (test_unconfirmed_result_is_not_success,
     "Полный синк B-1: неподтверждённый результат — не успех, данные целы"),
    (test_process_killed_during_write_then_restart,
     "Полный синк B-1: обрыв процесса во время записи — без пустых листов"),
    (test_sheet_grows_beyond_grid_in_same_request,
     "Полный синк B-1: рост листа за сетку — в том же запросе"),
    (test_sheet_shrinks_tail_cleared_atomically,
     "Полный синк B-1: сокращение листа — хвост очищен атомарно"),
    (test_user_columns_follow_their_row,
     "Полный синк B-1: колонки правее G остаются при своей строке"),
    (test_b2_removed_task_note_not_orphaned,
     "Полный синк B-2: заметка исключённой задачи не остаётся у другой"),
    (test_b2_notes_in_several_columns_follow_task,
     "Полный синк B-2: заметки в H, I, K — при задаче, дальняя сирота очищена"),
    (test_b2_shrink_with_notes_in_tail, "Полный синк B-2: сокращение листа с заметками"),
    (test_b2_notes_with_write_failures, "Полный синк B-2: заметки при отказе и таймауте записи"),
    (test_notes_duplicate_groupers_and_keys,
     "Полный синк: заметки при одинаковых заголовках и ключах — каждая при своей"),
    (test_notes_window_meeting_vs_carried_same_label,
     "Полный синк: заголовок окна и перенесённый с той же подписью"),
    (test_legacy_row_note_kept_when_task_in_window, "Полный синк: заметка legacy-строки"),
    (test_grid_columns_grow_in_same_request, "Полный синк: рост колонок сетки в том же запросе"),
    (test_unknown_grid_size_never_shrinks, "Полный синк: неизвестный размер сетки не уменьшается"),
    (test_unknown_grid_size_data_too_big_rejected_safely,
     "Полный синк: неизвестная сетка и отказ Google — данные целы"),
    (test_note_follows_task_to_another_sheet,
     "Полный синк: заметка переезжает с задачей в другой лист"),
    (test_recurring_named_like_one_time_sheet_atomic,
     "Полный синк B-1: регулярная встреча с именем «Разовые встречи»"),
    (test_append_and_auto_import_unchanged,
     "Полный синк B-1: обычный и вечерний импорт без изменений"),
]


if __name__ == "__main__":
    failed = []
    for fn, label in TESTS:
        try:
            fn()
            print(f"  ✓ {label}")
        except AssertionError as e:
            print(f"  ✗ {label}: {str(e)[:800]}")
            failed.append(label)
    raise SystemExit(1 if failed else 0)
