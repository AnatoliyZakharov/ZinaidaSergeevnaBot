"""
Аудит надёжности синхронизации MYmeet → протоколы → реестр → «Мои задачи».

Сценарии, не покрытые test_mymeet_daily / test_grouper_format / test_my_tasks:
перезапуск во время синхронизации, одновременный ручной и автоматический
запуск, неоднозначный исполнитель, две встречи с одинаковым названием и
задачей, полный синк (/sync_mymeet_full) и строки вне окна.

Без сети: фейки MYmeet / Calendar / Sheets, временная БД, реальный код
main.py (AST) и mymeet_sync.py.
Запуск: python test_sync_audit.py  (также входит в python test_smoke.py)
"""
import asyncio
import sqlite3
import tempfile
from datetime import timedelta

import test_mymeet_daily as D
import test_smoke as _ts

ME = D.ME
PROTO_HDR = _ts._PROTO_HDR


def _cfg(env):
    return {"gcal_organizer_data": env.mm.gcal(), "mymeet_spreadsheet_id": "sheet-1",
            "mymeet_token": "t"}


def _all_task_keys(book):
    """[(лист, встреча, задача, ответственный)] по всем листам протоколов."""
    out = []
    for t, rows in book.data.items():
        if not rows or rows[0][:3] != PROTO_HDR[:3]:
            continue
        for r in rows[1:]:
            if len(r) >= 7 and str(r[0]).strip().isdigit():
                out.append((r[6], r[2], r[3]))
    return out


def _assert_no_dupes(book):
    keys = _all_task_keys(book)
    assert len(keys) == len(set(keys)), keys


# ── Перезапуск приложения во время синхронизации ─────────────────────────────

def test_restart_mid_sync_then_recovery():
    """Процесс «умирает» после записи протоколов, до «Мои задачи» и до записи
    состояния слота.  После перезапуска слот снова due, повтор доставляет
    задачу в личный лист, дублей в протоколах нет."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        env.mm.add("m1", "Стратсессия", D._now() - timedelta(hours=3), [("Отчёт", ME, False)])
        env.mm.add("m2", "Совет", D._now() - timedelta(hours=2), [("План", ME, False)])
        real = env.ms._sync_my_tasks_and_colors

        def _die(*a, **k):
            raise SystemExit("restart")            # не Exception — как kill процесса
        env.ms._sync_my_tasks_and_colors = _die
        env.force_due()
        try:
            env.tick()
            raise AssertionError("должно было «упасть»")
        except SystemExit:
            pass
        st = env.db.mymeet_sync_get(1)
        assert st["last_success_slot"] == "2000-01-01" and not st.get("last_outcome"), st
        assert sorted(r[1] for r in env.book().task_rows()) == ["Отчёт", "План"]
        env.ms._sync_my_tasks_and_colors = real    # «перезапуск»
        env.tick()
        st = env.db.mymeet_sync_get(1)
        assert st["last_outcome"] == "ok" and st["last_success_slot"] != "2000-01-01", st
        _assert_no_dupes(env.book())
        assert sorted(D._my_task_names(env) if hasattr(D, "_my_task_names") else
                      [r[3] for r in env.my_tasks_sheet()[1:]]) == ["Отчёт", "План"]


def test_restart_between_protocol_sheets():
    """«Смерть» процесса между записью двух протокольных листов: после
    перезапуска оставшийся лист дописывается, первый не дублируется."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        n = D._now()
        env.mm.add("a1", "Планёрка", n - timedelta(hours=5), [("A1", ME, False)])
        env.mm.add("a2", "Планёрка", n - timedelta(hours=4), [("A2", ME, False)])
        env.mm.add("o1", "Разовая", n - timedelta(hours=2), [("O1", ME, False)])
        book = env.book()
        real_add = book.ss.add_worksheet.side_effect
        state = {"n": 0}

        def _add(title, rows, cols):
            if title == env.ms.ONE_TIME_SHEET_NAME:
                raise SystemExit("restart")
            return real_add(title, rows, cols)
        book.ss.add_worksheet.side_effect = _add
        env.force_due()
        try:
            env.tick()
        except SystemExit:
            state["n"] += 1
        assert state["n"] == 1
        assert sorted(r[1] for r in book.task_rows()) == ["A1", "A2"]
        book.ss.add_worksheet.side_effect = real_add
        env.tick()
        assert env.db.mymeet_sync_get(1)["last_outcome"] == "ok"
        assert sorted(r[1] for r in book.task_rows()) == ["A1", "A2", "O1"]
        _assert_no_dupes(book)


# ── Одновременный ручной и автоматический запуск ─────────────────────────────

def _load_manual(env):
    return D._load_main(["cmd_sync_mymeet", "_user_lock"], {
        **{k: env.ns[k] for k in ("db", "mymeet_sync", "logger", "asyncio",
                                  "_user_row_to_mymeet_config", "_mymeet_config_matches", "_my_tasks_locks")},
        "Update": object, "ContextTypes": D.NS(DEFAULT_TYPE=object),
        "_get_gcal_organizer_data": lambda tid, days: env.mm.gcal(),
        "sync_to_sheets_async": env.ns["sync_to_sheets_async"],
        "_track": lambda *a, **k: None,
    }, assigns=())


class _Msg:
    def __init__(self):
        self.texts = []

    async def reply_text(self, text, **kw):
        self.texts.append(text)
        return self

    async def edit_text(self, text, **kw):
        self.texts.append(text)


def test_manual_and_auto_concurrently():
    """Ручной /sync_mymeet и автоимпорт одновременно: работают по очереди
    (общий _user_lock), дублей нет, оба завершаются успешно."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        env.mm.add("m1", "Стратсессия", D._now() - timedelta(hours=3), [("Отчёт", ME, False)])
        manual = _load_manual(env)
        manual["_my_tasks_locks"] = env.ns["_my_tasks_locks"]       # один и тот же lock
        order = []
        real = env.ns["sync_to_sheets_async"]

        async def _slow(**kw):
            order.append(("start", kw.get("days")))
            await asyncio.sleep(0.03)
            r = await real(**kw)
            order.append(("end", kw.get("days")))
            return r
        env.ns["sync_to_sheets_async"] = _slow
        manual["sync_to_sheets_async"] = _slow
        msg = _Msg()
        upd = D.NS(effective_user=D.NS(id=1, username="u"), message=msg)
        env.force_due()

        async def both():
            await asyncio.gather(env.ns["scheduled_mymeet_sync"](D.Bot()),
                                 manual["cmd_sync_mymeet"](upd, None))
        asyncio.run(both())
        kinds = [k for k, _ in order]
        assert kinds == ["start", "end", "start", "end"], order      # не пересеклись
        assert env.db.mymeet_sync_get(1)["last_outcome"] == "ok"
        assert any("Синхронизировано" in t for t in msg.texts), msg.texts
        _assert_no_dupes(env.book())
        assert [r[1] for r in env.book().task_rows()] == ["Отчёт"]


# ── Исполнитель и одинаковые встречи ─────────────────────────────────────────

def test_ambiguous_assignees_not_assigned():
    """Неоднозначные исполнители не попадают в «Мои задачи» (не приписываем
    чужое), но остаются в листе протокола."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        tasks = [("Точно моя", ME, False), ("Порядок слов", "Петров Иван", False),
                 ("Инициал", "Петров И.", False), ("Только фамилия", "Петров", False),
                 ("Только имя", "Иван", False), ("Метка", "Speaker A", False),
                 ("Падеж+инициал", "Петрова И.", False), ("Двое", f"{ME}, Анна Смирнова", False),
                 ("Чужая", "Анна Смирнова", False)]
        env.mm.add("m1", "Совет", D._now() - timedelta(hours=2), tasks)
        env.force_due()
        env.tick()
        assert len(env.book().task_rows()) == len(tasks)            # всё в протоколе
        mine = sorted(r[3] for r in env.my_tasks_sheet()[1:])
        assert mine == sorted(["Точно моя", "Порядок слов", "Инициал", "Двое"]), mine


def test_two_meetings_same_name_same_task():
    """Две разные встречи с одинаковым названием, датой, задачей и
    исполнителем — две строки (разные meeting id), повтор без дублей."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        t = D._now() - timedelta(hours=3)
        env.mm.add("m1", "Синк", t, [("Отчёт", ME, False)])
        env.mm.add("m2", "Синк", t + timedelta(minutes=30), [("Отчёт", ME, False)])
        for _ in range(2):
            env.force_due()
            env.tick()
        rows = _all_task_keys(env.book())
        assert sorted(rows) == [("m1", "Отчёт", ME), ("m2", "Отчёт", ME)], rows
        assert [r[3] for r in env.my_tasks_sheet()[1:]].count("Отчёт") == 2


# ── Полный синк и строки вне окна ────────────────────────────────────────────

def _book_with_old_rows(env):
    name = env.ms.ONE_TIME_SHEET_NAME
    book = D.Book({
        name: [PROTO_HDR, ["Старая встреча  (01.08.2026)"],
               ["1", "01.08.2026", "Старая задача", ME, "", "Выполнено", "old1"]],
        "Планёрка": [PROTO_HDR,
                     ["1", "02.08.2026", "Старая регулярная", ME, "", "В работе", "old2"]],
    })
    for t in (name, "Планёрка"):
        book.ws[t].clear.side_effect = (lambda tt: (lambda: book.data.__setitem__(tt, [])))(t)
    env.books["sheet-1"] = book
    return book


def test_full_sync_keeps_rows_outside_window():
    """/sync_mymeet_full за N дней не должен терять строки старше окна и их
    ручные статусы (иначе они исчезают и из «Мои задачи»)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        book = _book_with_old_rows(env)
        env.mm.add("n1", "Новая встреча", D._now() - timedelta(hours=2), [("Новая", ME, False)])
        env.mm.add("p1", "Планёрка", D._now() - timedelta(hours=3), [("Свежая", ME, False)])
        env.mm.add("p2", "Планёрка", D._now() - timedelta(hours=1), [("Свежая 2", ME, False)])
        res = env.ms.sync_to_sheets(days=30, append=False, user_config=_cfg(env))
        keys = _all_task_keys(book)
        assert ("old1", "Старая задача", ME) in keys, (res, book.data)
        assert ("old2", "Старая регулярная", ME) in keys, (res, book.data)
        statuses = {r[2]: r[5] for t in book.data for r in book.data[t][1:] if len(r) > 5}
        assert statuses.get("Старая задача") == "Выполнено", statuses
        assert statuses.get("Старая регулярная") == "В работе", statuses
        assert ("n1", "Новая", ME) in keys and ("p1", "Свежая", ME) in keys
        _assert_no_dupes(book)


def _full_cfg(env):
    cfg = env.ns["_user_row_to_mymeet_config"](env.db.get_user(1))
    cfg["gcal_organizer_data"] = env.mm.gcal()
    return cfg


def test_full_sync_old_tasks_stay_in_my_tasks_and_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        book = _book_with_old_rows(env)
        env.mm.add("n1", "Новая встреча", D._now() - timedelta(hours=2), [("Новая", ME, False)])
        for _ in range(2):                                         # повтор — то же самое
            env.ms.sync_to_sheets(days=30, append=False, user_config=_full_cfg(env))
            _assert_no_dupes(book)
            mine = sorted(r[3] for r in env.my_tasks_sheet()[1:])
            assert mine == ["Новая", "Старая задача", "Старая регулярная"], mine
        assert sorted(k[1] for k in _all_task_keys(book)) == [
            "Новая", "Старая задача", "Старая регулярная"]


def test_full_sync_read_failure_does_not_clear():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        book = _book_with_old_rows(env)
        name = env.ms.ONE_TIME_SHEET_NAME
        before = {t: [list(r) for r in rows] for t, rows in book.data.items()}
        book.ws[name].get_all_values.side_effect = RuntimeError("503")
        env.mm.add("n1", "Новая встреча", D._now() - timedelta(hours=2), [("Новая", ME, False)])
        try:
            env.ms.sync_to_sheets(days=30, append=False, user_config=_cfg(env))
            raise AssertionError("должна быть ошибка")
        except RuntimeError as e:
            assert "до записи: Google Sheets не изменён" in str(e), e
        assert not book.ws[name].clear.called
        assert book.commits == 0                              # ни одной записи значений
        assert book.data == before


def test_full_sync_status_read_failure_aborts_before_clear():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        book = _book_with_old_rows(env)
        before = {t: [list(r) for r in rows] for t, rows in book.data.items()}
        book.ss.values_batch_get.side_effect = RuntimeError("503")
        env.mm.add("n1", "Новая встреча", D._now() - timedelta(hours=2), [("Новая", ME, False)])
        try:
            env.ms.sync_to_sheets(days=30, append=False, user_config=_cfg(env))
            raise AssertionError("должна быть ошибка")
        except RuntimeError:
            pass
        assert not any(w.clear.called for w in book.ws.values())
        assert book.data == before and book.writes == []


def test_full_sync_classification_change_no_cross_sheet_dupes():
    """Встреча была разовой (строка в «Разовые встречи»), в новом окне их две
    с тем же названием — уходит в регулярный лист; в «Разовых» не остаётся
    копии (ключ задачи в окне — строка не переносится)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        name = env.ms.ONE_TIME_SHEET_NAME
        t1 = D._now() - timedelta(days=2)
        date1 = env.ms._fmt_date(D._iso(t1))
        book = D.Book({name: [PROTO_HDR, [f"Синк  ({date1})"],
                              ["1", date1, "Отчёт", ME, "", "В работе", "m1"]]})
        book.ws[name].clear.side_effect = lambda: book.data.__setitem__(name, [])
        env.books["sheet-1"] = book
        env.mm.add("m1", "Синк", t1, [("Отчёт", ME, False)])
        env.mm.add("m2", "Синк", D._now() - timedelta(hours=2), [("Итоги", ME, False)])
        env.ms.sync_to_sheets(days=30, append=False, user_config=_cfg(env))
        keys = _all_task_keys(book)
        assert sorted(keys) == [("m1", "Отчёт", ME), ("m2", "Итоги", ME)], book.data
        assert "Синк" in book.data and not book.task_rows(name)
        # ручной статус переехал вместе с задачей в регулярный лист
        st = {r[2]: r[5] for r in book.data["Синк"][1:]}
        assert st["Отчёт"] == "В работе", st


def test_full_sync_legacy_row_without_mid_not_duplicated():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        name = env.ms.ONE_TIME_SHEET_NAME
        t = D._now() - timedelta(hours=3)
        date = env.ms._fmt_date(D._iso(t))
        book = D.Book({name: [PROTO_HDR, [f"Совет  ({date})"],
                              ["1", date, "Отчёт", ME, "", "Выполнено"],          # без G
                              ["2", "01.08.2026", "Древняя", ME, "", "Выполнено"]]})
        book.ws[name].clear.side_effect = lambda: book.data.__setitem__(name, [])
        env.books["sheet-1"] = book
        env.mm.add("m1", "Совет", t, [("Отчёт", ME, False)])
        env.ms.sync_to_sheets(days=30, append=False, user_config=_cfg(env))
        tasks = sorted(r[1] for r in book.task_rows(name))
        assert tasks == ["Древняя", "Отчёт"], book.data[name]
        st = {r[2]: r[5] for r in book.data[name][1:] if len(r) > 5}
        assert st["Отчёт"] == "Выполнено", st                     # статус legacy-строки


def test_full_sync_window_meeting_mymeet_is_source_of_truth():
    """Встреча в окне: задача изменена в MYmeet — устаревшая строка не
    переносится (нет «двойника»).  Встреча в окне с протоколом 404 — её
    строки сохраняются (протокол временно недоступен — данные не теряем)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        name = env.ms.ONE_TIME_SHEET_NAME
        t = D._now() - timedelta(hours=3)
        date = env.ms._fmt_date(D._iso(t))
        book = D.Book({name: [PROTO_HDR, [f"Совет  ({date})"],
                              ["1", date, "Отчёт v1", ME, "", "В работе", "m1"],
                              [f"Планёрка  ({date})"],
                              ["2", date, "Смета", ME, "", "Выполнено", "m2"]]})
        book.ws[name].clear.side_effect = lambda: book.data.__setitem__(name, [])
        env.books["sheet-1"] = book
        env.mm.add("m1", "Совет", t, [("Отчёт v2", ME, False)])     # текст изменён
        env.mm.add("m2", "Планёрка", t, None)                        # протокол 404
        env.ms.sync_to_sheets(days=30, append=False, user_config=_cfg(env))
        tasks = sorted(r[1] for r in book.task_rows(name))
        assert tasks == ["Отчёт v2", "Смета"], book.data[name]
        st = {r[2]: r[5] for r in book.data[name][1:] if len(r) > 5}
        assert st["Смета"] == "Выполнено", st


def test_full_sync_numeric_meeting_title_carried():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        name = env.ms.ONE_TIME_SHEET_NAME
        book = D.Book({name: [PROTO_HDR, ["Старая A  (01.08.2026)"],
                              ["1", "01.08.2026", "A1", ME, "", "Выполнено", "a"],
                              ["2024"],                                   # числовое название
                              ["2", "02.08.2026", "B1", ME, "", "В работе", "b"]]})
        book.ws[name].clear.side_effect = lambda: book.data.__setitem__(name, [])
        env.books["sheet-1"] = book
        env.mm.add("n1", "Новая", D._now() - timedelta(hours=2), [("N1", ME, False)])
        env.ms.sync_to_sheets(days=30, append=False, user_config=_cfg(env))
        labels = [r[0] for r in book.data[name][1:] if len(r) > 1 and not str(r[1]).strip()]
        assert labels[:2] == ["Старая A  (01.08.2026)", "2024"], book.data[name]


def test_full_sync_sheet_name_collisions():
    with tempfile.TemporaryDirectory() as tmp:
        env = D.Env(tmp)
        name = env.ms.ONE_TIME_SHEET_NAME
        n = D._now()
        book = D.Book({"Синк 1-1": [PROTO_HDR, ["1", "01.08.2026", "old", ME, "", "В работе", "x"]]})
        book.ws["Синк 1-1"].clear.side_effect = lambda: book.data.__setitem__("Синк 1-1", [])
        env.books["sheet-1"] = book
        for mid, nm, h, task in (("a1", "Синк 1:1", 5, "A1"), ("a2", "Синк 1:1", 4, "A2"),
                                 ("b1", "Синк 1/1", 3, "B1"), ("b2", "Синк 1/1", 2, "B2"),
                                 ("r1", name, 6, "R1"), ("r2", name, 1, "R2"),
                                 ("o1", "Разовая", 1, "O1")):
            env.mm.add(mid, nm, n - timedelta(hours=h), [(task, ME, False)])
        real_add = book.ss.add_worksheet.side_effect

        def _add(title, rows, cols):
            w = real_add(title, rows, cols)
            w.clear.side_effect = lambda: book.data.__setitem__(title, [])
            return w
        book.ss.add_worksheet.side_effect = _add
        for _ in range(2):
            env.ms.sync_to_sheets(days=30, append=False, user_config=_cfg(env))
            assert sorted(r[1] for r in book.task_rows("Синк 1-1")) == ["A1", "A2", "B1", "B2", "old"]
            assert sorted(r[1] for r in book.task_rows(name)) == ["O1", "R1", "R2"], book.data[name]
            _assert_no_dupes(book)


TESTS = [
    (test_restart_mid_sync_then_recovery, "Аудит: перезапуск во время синка → повтор без дублей"),
    (test_restart_between_protocol_sheets, "Аудит: перезапуск между листами протоколов"),
    (test_manual_and_auto_concurrently, "Аудит: ручной и автоимпорт одновременно — по очереди"),
    (test_ambiguous_assignees_not_assigned, "Аудит: неоднозначный исполнитель не приписан"),
    (test_two_meetings_same_name_same_task, "Аудит: две встречи с одинаковым названием и задачей"),
    (test_full_sync_keeps_rows_outside_window, "Аудит: полный синк не теряет строки вне окна"),
    (test_full_sync_old_tasks_stay_in_my_tasks_and_idempotent,
     "Аудит: полный синк — старые задачи в «Мои задачи», повтор идемпотентен"),
    (test_full_sync_read_failure_does_not_clear, "Аудит: полный синк — сбой чтения, лист не очищен"),
    (test_full_sync_status_read_failure_aborts_before_clear,
     "Аудит: полный синк — сбой чтения статусов, ничего не очищено"),
    (test_full_sync_classification_change_no_cross_sheet_dupes,
     "Аудит: полный синк — смена разовая/регулярная: без дублей, статус сохранён"),
    (test_full_sync_legacy_row_without_mid_not_duplicated,
     "Аудит: полный синк — legacy-строки без дублей, статус сохранён"),
    (test_full_sync_window_meeting_mymeet_is_source_of_truth,
     "Аудит: полный синк — изменённая задача без двойника, протокол 404 — строки целы"),
    (test_full_sync_numeric_meeting_title_carried, "Аудит: полный синк — числовое название встречи"),
    (test_full_sync_sheet_name_collisions, "Аудит: полный синк — одно имя листа у разных встреч"),
]


if __name__ == "__main__":
    failed = []
    for fn, label in TESTS:
        try:
            fn()
            print(f"  ✓ {label}")
        except AssertionError as e:
            print(f"  ✗ {label}: {str(e)[:600]}")
            failed.append(label)
    raise SystemExit(1 if failed else 0)
