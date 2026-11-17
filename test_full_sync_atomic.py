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


# ── 6. Обычный и вечерний импорт не изменились ───────────────────────────────

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
