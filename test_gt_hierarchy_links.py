"""
Issue #36: иерархия Google Tasks в «Мои задачи», кликабельные ссылки на письма
(textFormatRuns поверх RAW-текста), явная HTML-ссылка Телемоста в описании
события.  Без сети: Sheets / Tasks / Calendar — фейки, БД — временная.
Все URL, email и id — синтетические.
Запуск: python test_gt_hierarchy_links.py  (также входит в python test_smoke.py)
"""
import importlib.util
import inspect
import json
import pathlib
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace as NS

import test_my_tasks as _tm      # noqa: E402  (env, ключ шифрования, фейки)
import test_smoke as _ts         # noqa: E402

REPO_ROOT = pathlib.Path(__file__).parent
ME = _tm.ME
MY_TITLE = _tm.MY_TITLE
PRE_PR_COMMIT = "8c77086"        # main до issue #36 (db.py без колонок иерархии)
MAIL = "https://mail.google.com/mail/#all/synthetic0001"

_load_my_tasks = _tm._load_my_tasks
_load_ms = _tm._load_ms
_tmp_db = _tm._tmp_db
_user = _tm._user
_gt = _tm._gt
_reconcile = _tm._reconcile
_sync = _tm._sync
_FakeGT = _tm._FakeGT
_Book = _tm._Book
PROTO_HDR = _tm.PROTO_HDR


def _numbers(rows):
    """{title: колонка A}"""
    return {r[3]: r[0] for r in rows}


def _titles(rows):
    return [r[3] for r in rows]


# ══ 1. Иерархия Google Tasks ══════════════════════════════════════════════

def _flat_fixture():
    def gt(tid, title, deadline="", status="В работе", lid="L1", **kw):
        r = {"source": "gtasks", "source_key": f"gtasks:{lid}:{tid}", "title": title,
             "origin": "Google Tasks — Мои", "assignee": "Иван Петров", "task_date": "",
             "deadline": deadline, "status": status, "gtasks_list_id": lid,
             "gtasks_task_id": tid, "link": ""}
        r.update(kw)
        return r
    recs = [
        gt("t1", "Бета", "20.11.2026"),
        gt("t2", "Альфа", "20.11.2026"),
        gt("t3", "Без срока"),
        gt("t4", "Письмо", "01.12.2026", link="✉️ Счёт: https://mail.google.com/mail/#all/x1"),
        gt("t5", "Выполненная", "05.11.2026", status="Выполнено"),
        gt("t6", "Правленая", "", overrides=json.dumps(
            {"title": "Своё имя", "deadline": "03.11.2026"}, ensure_ascii=False)),
        {"source": "telegram", "source_key": "tg:1:5", "title": "Позвонить",
         "origin": "Telegram — Мария", "assignee": "Иван Петров", "task_date": "10.11.2026",
         "deadline": "", "status": "В работе"},
        {"source": "protocol", "source_key": "p:m1|10.11.2026|отчёт|иван петров",
         "title": "Отчёт", "origin": "Планёрка", "assignee": "Иван Петров",
         "task_date": "10.11.2026", "deadline": "15.11.2026", "status": "Требует верификации"},
        {"source": "protocol", "source_key": "p:m1|10.11.2026|старое|иван петров",
         "title": "Старое", "origin": "Планёрка", "assignee": "Иван Петров",
         "task_date": "09.11.2026", "deadline": "", "status": "В работе"},
        gt("t7", "Одинаковый срок", "bad-date"),
        gt("t8", "", "", status=None),
    ]
    return {r["source_key"]: r for r in recs}, ["p:m1|10.11.2026|отчёт|иван петров"]


# Эталон: вывод build_sheet_rows кода main @ 8c77086 (до issue #36) на _flat_fixture
_FLAT_BASELINE = (
    '[[1, "", "Google Tasks — Мои", "Своё имя", "Иван Петров", "03.11.2026", "В работе", '
    '"gtasks:L1:t6", "gtasks:L1:t6"], [2, "", "Google Tasks — Мои", "Выполненная", '
    '"Иван Петров", "05.11.2026", "Выполнено", "gtasks:L1:t5", "gtasks:L1:t5"], '
    '[3, "10.11.2026", "Планёрка", "Отчёт", "Иван Петров", "15.11.2026", '
    '"Требует верификации", "p:m1|10.11.2026|отчёт|иван петров", '
    '"p:m1|10.11.2026|отчёт|иван петров"], [4, "", "Google Tasks — Мои", "Альфа", '
    '"Иван Петров", "20.11.2026", "В работе", "gtasks:L1:t2", "gtasks:L1:t2"], '
    '[5, "", "Google Tasks — Мои", "Бета", "Иван Петров", "20.11.2026", "В работе", '
    '"gtasks:L1:t1", "gtasks:L1:t1"], [6, "", "Google Tasks — Мои", '
    '"Письмо\\n✉️ Счёт: https://mail.google.com/mail/#all/x1", "Иван Петров", '
    '"01.12.2026", "В работе", "gtasks:L1:t4", "gtasks:L1:t4"], [7, "10.11.2026", '
    '"Telegram — Мария", "Позвонить", "Иван Петров", "", "В работе", "tg:1:5", "tg:1:5"], '
    '[8, "", "Google Tasks — Мои", "", "Иван Петров", "", "В работе", "gtasks:L1:t8", '
    '"gtasks:L1:t8"], [9, "", "Google Tasks — Мои", "Без срока", "Иван Петров", "", '
    '"В работе", "gtasks:L1:t3", "gtasks:L1:t3"], [10, "", "Google Tasks — Мои", '
    '"Одинаковый срок", "Иван Петров", "bad-date", "В работе", "gtasks:L1:t7", '
    '"gtasks:L1:t7"]]'
)


def _old_module(name, commit, tmp):
    src = subprocess.run(["git", "show", f"{commit}:{name}"], cwd=REPO_ROOT,
                         capture_output=True, text=True)
    if src.returncode != 0:
        return None
    path = pathlib.Path(tmp) / f"old_{name}"
    path.write_text(src.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"old_{name[:-3]}_i36", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_flat_list_byte_identical():
    """Плоский список без parent: вывод побайтно как в main до изменений."""
    mt = _load_my_tasks()
    recs, active = _flat_fixture()
    got = json.dumps(mt.build_sheet_rows(recs, active), ensure_ascii=False)
    assert got == _FLAT_BASELINE, got
    assert all(type(r[0]) is int for r in mt.build_sheet_rows(recs, active))
    # тот же вывод, что у кода main (если коммит доступен)
    with tempfile.TemporaryDirectory() as tmp:
        old = _old_module("my_tasks.py", PRE_PR_COMMIT, tmp)
        assert old is not None or not _tm.IN_CI, f"нет коммита {PRE_PR_COMMIT}"
        if old is not None:
            recs2, _ = _flat_fixture()
            assert json.dumps(old.build_sheet_rows(recs2, active), ensure_ascii=False) == got
    # parent указывает на отсутствующую задачу — тоже плоско и побайтно
    recs, active = _flat_fixture()
    recs["gtasks:L1:t1"]["gtasks_parent_id"] = "ghost"
    recs["gtasks:L1:t2"]["gtasks_parent_id"] = "t2"            # самоссылка
    assert json.dumps(mt.build_sheet_rows(recs, active), ensure_ascii=False) == _FLAT_BASELINE


def test_hierarchy_three_levels_and_position_order():
    """3 уровня; перемешанный порядок API → дети по position; набор строк прежний."""
    mt = _load_my_tasks()
    items = [
        _gt("c2", title="Ребёнок 2", parent="p", position="00000000000000000002"),
        _gt("g1", title="Внук", parent="c1", position="00000000000000000001"),
        _gt("x", title="Другая", due="2026-11-21T00:00:00.000Z"),
        _gt("c1", title="Ребёнок 1", parent="p", position="00000000000000000001"),
        _gt("p", title="Родитель", due="2026-11-20T00:00:00.000Z"),
        _gt("c3", title="Ребёнок 3", parent="p", position="00000000000000000003",
            due="2026-11-01T00:00:00.000Z"),
    ]
    recs = {}
    _reconcile(mt, recs, items)
    assert recs["gtasks:L1:c1"]["gtasks_parent_id"] == "p"
    assert recs["gtasks:L1:c1"]["gtasks_position"] == "00000000000000000001"
    assert recs["gtasks:L1:p"].get("gtasks_parent_id") is None
    assert recs["gtasks:L1:p"].get("gtasks_position") is None      # пусто → None
    rows = mt.build_sheet_rows(recs, [])
    assert _titles(rows) == ["Родитель", "Ребёнок 1", "Внук", "Ребёнок 2", "Ребёнок 3",
                             "Другая"], _titles(rows)
    assert [r[0] for r in rows] == [1, "↳ 1.1", "↳ 1.1.1", "↳ 1.2", "↳ 1.3", 2]
    # набор строк и ключей = как без иерархии
    flat = {k: dict(r, gtasks_parent_id=None) for k, r in recs.items()}
    assert sorted(r[8] for r in rows) == sorted(r[8] for r in mt.build_sheet_rows(flat, []))
    # колонка A не участвует в ключе строки и правках
    assert all(idx != 0 for _f, idx in mt.EDIT_FIELDS)
    for r in rows:
        a = list(r[:8])
        b = list(r[:8])
        b[0] = 999
        assert mt.row_key(a) == mt.row_key(b) == r[8]
    # другой порядок ответа API — тот же результат
    recs2 = {}
    _reconcile(mt, recs2, list(reversed(items)))
    assert [r[8] for r in mt.build_sheet_rows(recs2, [])] == [r[8] for r in rows]


def test_hierarchy_parent_child_on_different_pages():
    """Родитель и дети на разных страницах ответа (nextPageToken) — list_all_tasks_full."""
    gtm, HttpError = _tm._load_gtasks()
    mt = _load_my_tasks()
    lists = {0: {"items": [{"id": "L1", "title": "Работа"}]}}
    pages = {
        ("L1", None): {"items": [{"id": "k2", "title": "Шаг 2", "parent": "root1",
                                  "position": "00000000000000000002"}],
                       "nextPageToken": "n1"},
        ("L1", "n1"): {"items": [{"id": "z", "title": "Ещё"}], "nextPageToken": "n2"},
        ("L1", "n2"): {"items": [{"id": "root1", "title": "Проект"},
                                 {"id": "k1", "title": "Шаг 1", "parent": "root1",
                                  "position": "00000000000000000001"}]},
    }
    items, err, complete, list_ids = gtm.list_all_tasks_full(
        1, service=_tm._GTService(lists, pages, HttpError))
    assert err is None and complete and len(items) == 4
    recs = {}
    _reconcile(mt, recs, items, list_ids=list_ids)
    rows = mt.build_sheet_rows(recs, [])
    assert _titles(rows) == ["Ещё", "Проект", "Шаг 1", "Шаг 2"], _titles(rows)
    assert [r[0] for r in rows] == [1, 2, "↳ 2.1", "↳ 2.2"]


def test_hierarchy_reparent_and_move_list():
    """Смена родителя и перенос в другой список при повторной сверке."""
    mt = _load_my_tasks()
    recs = {}
    base = [_gt("a", title="A"), _gt("b", title="B"), _gt("c", title="C", parent="a")]
    _reconcile(mt, recs, base)
    assert _numbers(mt.build_sheet_rows(recs, [])) == {"A": 1, "C": "↳ 1.1", "B": 2}
    # родитель сменился a → b
    _reconcile(mt, recs, [_gt("a", title="A"), _gt("b", title="B"),
                          _gt("c", title="C", parent="b")])
    assert _numbers(mt.build_sheet_rows(recs, [])) == {"A": 1, "B": 2, "C": "↳ 2.1"}
    # стал корневым
    _reconcile(mt, recs, [_gt("a", title="A"), _gt("b", title="B"), _gt("c", title="C")])
    assert recs["gtasks:L1:c"].get("gtasks_parent_id") is None
    assert _numbers(mt.build_sheet_rows(recs, [])) == {"A": 1, "B": 2, "C": 3}
    # задача с родителем ушла в другой список (родитель остался в L1) → сирота в L2
    _reconcile(mt, recs, [_gt("a", title="A"), _gt("b", title="B"),
                          _gt("c", title="C", parent="a")])
    _, stats, _ = _reconcile(mt, recs, [_gt("a", title="A"), _gt("b", title="B"),
                                        _gt("c", title="C", parent="a", list_id="L2")])
    assert stats["gtasks_moved"] == 1 and set(recs) == {"gtasks:L1:a", "gtasks:L1:b",
                                                        "gtasks:L2:c"}
    assert _numbers(mt.build_sheet_rows(recs, [])) == {"A": 1, "B": 2, "C": 3}
    # переехал вместе с родителем → снова вложен
    _reconcile(mt, recs, [_gt("a", title="A", list_id="L2"), _gt("b", title="B"),
                          _gt("c", title="C", parent="a", list_id="L2")])
    assert set(recs) == {"gtasks:L2:a", "gtasks:L1:b", "gtasks:L2:c"}
    assert _numbers(mt.build_sheet_rows(recs, [])) == {"A": 1, "C": "↳ 1.1", "B": 2}
    # задачи нет в ответе (неполный) — последние значения parent/position сохраняются
    recs["gtasks:L2:c"]["gtasks_position"] = "00000000000000000007"
    _reconcile(mt, recs, [_gt("a", title="A", list_id="L2"), _gt("b", title="B")],
               complete=False)
    assert recs["gtasks:L2:c"]["gtasks_parent_id"] == "a"
    assert recs["gtasks:L2:c"]["gtasks_position"] == "00000000000000000007"


def test_hierarchy_orphans_cycles_depth():
    """Удалённый / безымянный родитель → сирота в корне; циклы и самоссылка —
    каждая строка ровно один раз; глубина больше лимита — на уровне лимита."""
    mt = _load_my_tasks()
    # родителя нет в ответе / родитель без title (не импортируется)
    recs = {}
    _reconcile(mt, recs, [_gt("p0", title="   "), _gt("o1", title="Сирота1", parent="p0"),
                          _gt("o2", title="Сирота2", parent="gone"), _gt("r", title="R")])
    assert "gtasks:L1:p0" not in recs
    rows = mt.build_sheet_rows(recs, [])
    assert all(type(r[0]) is int for r in rows) and len(rows) == 3
    # цикл A→B→A и самоссылка
    recs = {}
    _reconcile(mt, recs, [_gt("A", title="A", parent="B"), _gt("B", title="B", parent="A"),
                          _gt("S", title="S", parent="S"), _gt("N", title="N")])
    rows = mt.build_sheet_rows(recs, [])
    assert sorted(_titles(rows)) == ["A", "B", "N", "S"], _titles(rows)
    nums = _numbers(rows)
    assert nums["N"] == 1 and nums["S"] == 2           # самоссылка — обычный корень
    assert nums["A"] == 3 and nums["B"] == "↳ 3.1"     # цикл поднят в корень
    # цепочка глубже лимита
    depth = mt.TREE_MAX_DEPTH + 5
    chain = [_gt("n0", title="n0")] + [_gt(f"n{i}", title=f"n{i}", parent=f"n{i - 1}")
                                       for i in range(1, depth + 1)]
    recs = {}
    _reconcile(mt, recs, list(reversed(chain)))
    rows = mt.build_sheet_rows(recs, [])
    assert _titles(rows) == [f"n{i}" for i in range(depth + 1)], _titles(rows)
    labels = [r[0] for r in rows]
    assert len(set(labels)) == len(labels)               # номера уникальны
    levels = [1 if isinstance(x, int) else x.count(".") + 1 for x in labels]
    assert max(levels) == mt.TREE_MAX_DEPTH + 1          # корень + 10 уровней
    assert levels[: mt.TREE_MAX_DEPTH + 1] == list(range(1, mt.TREE_MAX_DEPTH + 2))
    assert set(levels[mt.TREE_MAX_DEPTH:]) == {mt.TREE_MAX_DEPTH + 1}
    # цикл длиннее + ветка из цикла — каждый ровно один раз
    recs = {}
    _reconcile(mt, recs, [_gt("x1", title="x1", parent="x3"), _gt("x2", title="x2", parent="x1"),
                          _gt("x3", title="x3", parent="x2"), _gt("y", title="y", parent="x2")])
    rows = mt.build_sheet_rows(recs, [])
    assert sorted(_titles(rows)) == ["x1", "x2", "x3", "y"]
    assert len(rows) == len(recs)


def test_hierarchy_done_parent_open_child_and_protocol_mix():
    """Родитель «Выполнено», ребёнок открыт: оба в листе, ребёнок под родителем;
    протокольные/telegram строки — корни, неактивные протоколы не попадают."""
    mt = _load_my_tasks()
    recs = {}
    _reconcile(mt, recs, [_gt("p", title="Готово", status="completed"),
                          _gt("c", title="Открыто", parent="p")])
    recs["p:x|1|t|a"] = {"source": "protocol", "source_key": "p:x|1|t|a", "title": "Прот",
                         "status": "В работе"}
    recs["p:old"] = {"source": "protocol", "source_key": "p:old", "title": "Старый",
                     "status": "В работе"}
    rows = mt.build_sheet_rows(recs, ["p:x|1|t|a"])
    st = {r[3]: (r[0], r[6]) for r in rows}
    assert "Старый" not in st and len(rows) == 3
    assert st["Готово"][1] == "Выполнено" and st["Открыто"][1] == "В работе"
    assert _titles(rows).index("Открыто") == _titles(rows).index("Готово") + 1
    assert st["Открыто"][0] == f"↳ {st['Готово'][0]}.1"


def test_hierarchy_sync_preserves_edits_no_duplicates():
    """Сквозной сценарий: плоский лист → появилась иерархия → порядок меняется,
    ручные статусы и правки ячеек сохраняются, повтор без дублей и без изменений."""
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR]})
        gt = _FakeGT([_gt("a", title="Родитель", due="2026-11-30T00:00:00.000Z"),
                      _gt("b", title="Подзадача", due="2026-11-10T00:00:00.000Z"),
                      _gt("c", title="Третья", due="2026-11-20T00:00:00.000Z")])
        assert _sync(mt, ms, db, book, gt)["outcome"] == mt.OUT_OK
        assert [r[0] for r in book.data[MY_TITLE][1:]] == [1, 2, 3]
        keys_before = {r[7] for r in book.data[MY_TITLE][1:]}
        # ручные правки: статус и название
        book.set_status(MY_TITLE, "Третья", "Просрочено")
        for row in book.data[MY_TITLE][1:]:
            if row[3] == "Подзадача":
                row[3] = "Подзадача (моё имя)"
        assert _sync(mt, ms, db, book, gt)["outcome"] == mt.OUT_OK
        # в GT b стала подзадачей a
        gt.items[1]["parent"] = "a"
        gt.items[1]["position"] = "00000000000000000001"
        res = _sync(mt, ms, db, book, gt)
        assert res["outcome"] == mt.OUT_OK, res
        data = book.data[MY_TITLE][1:]
        assert [(r[0], r[3]) for r in data] == [
            (1, "Третья"), (2, "Родитель"), ("↳ 2.1", "Подзадача (моё имя)")], data
        assert {r[7] for r in data} == keys_before
        assert book.rows(MY_TITLE)["Третья"][6] == "Просрочено"
        recs = {r["source_key"]: r for r in db.my_tasks_load(1)}
        assert mt._json(recs["gtasks:L1:b"]["overrides"]) == {"title": "Подзадача (моё имя)"}
        assert not mt._json(recs["gtasks:L1:a"]["overrides"])
        assert not mt._json(recs["gtasks:L1:c"]["overrides"])
        assert recs["gtasks:L1:c"]["status"] == "Просрочено"
        assert recs["gtasks:L1:b"]["gtasks_parent_id"] == "a"
        # повторные прогоны: лист и БД не меняются, дублей нет, операций в GT нет
        snap = [list(r) for r in book.data[MY_TITLE]]
        conn = sqlite3.connect(db.DB_PATH)
        q = ("SELECT source_key, status, sheet_status, gtasks_parent_id, gtasks_position "
             "FROM my_tasks ORDER BY id")
        db_before = conn.execute(q).fetchall()
        gt.calls.clear()
        for _ in range(2):
            assert _sync(mt, ms, db, book, gt)["outcome"] == mt.OUT_OK
        assert book.data[MY_TITLE] == snap
        assert conn.execute(q).fetchall() == db_before
        assert [c for c in gt.calls if c[0] != "get"] == []
        assert conn.execute("SELECT COUNT(*) FROM my_tasks").fetchone()[0] == 3
        conn.close()


def test_hierarchy_guards_unchanged():
    """confirm_missing, массовая защита и защита от сжатия работают как раньше
    при наличии подзадач."""
    mt = _load_my_tasks()
    items = [_gt("p", title="P")] + [_gt(f"c{i}", title=f"c{i}", parent="p") for i in range(19)]
    recs = {}
    _reconcile(mt, recs, items)
    plan, stats, w = _reconcile(mt, recs, items[:-2])
    assert sorted(k for k, *_ in plan.confirm_missing) == ["gtasks:L1:c17", "gtasks:L1:c18"]
    plan, stats, w = _reconcile(mt, recs, items[:2])
    assert stats["gtasks_mass_missing"] == 18 and not plan.confirm_missing
    # строк в листе столько же, сколько записей — порядок не влияет на сжатие
    assert len(mt.build_sheet_rows(recs, [])) == len(recs)
    # сжатие: лист из 12 строк, builder вернул бы 0 → не перезаписан
    ms = _load_ms()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR]})
        gt = _FakeGT(items[:12])
        _sync(mt, ms, db, book, gt)
        assert len(book.data[MY_TITLE]) == 13
        snap = [list(r) for r in book.data[MY_TITLE]]
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("DELETE FROM my_tasks")              # только временная тестовая БД
        conn.commit()
        conn.close()
        res = _sync(mt, ms, db, book, _FakeGT([]))
        assert book.data[MY_TITLE] == snap, res


def test_db_hierarchy_migration_idempotent_and_rollback():
    """Старая БД без колонок → новый код (миграция, идемпотентно) → старый код
    читает и пишет, значения иерархии не теряются."""
    with tempfile.TemporaryDirectory() as tmp:
        old = _old_module("db.py", PRE_PR_COMMIT, tmp)
        assert old is not None or not _tm.IN_CI, f"нет коммита {PRE_PR_COMMIT}"
        if old is None:
            return
        old.DB_PATH = pathlib.Path(tmp) / "test_bot.db"
        old.init_db()
        old.upsert_user(1, "u")
        old.my_tasks_upsert(1, [{"source": "gtasks", "source_key": "gtasks:L1:a",
                                 "title": "Старая", "status": "В работе",
                                 "gtasks_list_id": "L1", "gtasks_task_id": "a"}])
        conn = sqlite3.connect(old.DB_PATH)
        cols0 = [r[1] for r in conn.execute("PRAGMA table_info(my_tasks)")]
        assert "gtasks_parent_id" not in cols0
        conn.close()
        new = _ts._load_db(tmp)
        assert str(new.DB_PATH) == str(old.DB_PATH)
        new.init_db()
        new.init_db()                                     # идемпотентно
        conn = sqlite3.connect(new.DB_PATH)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(my_tasks)")]
        assert cols == cols0 + ["gtasks_parent_id", "gtasks_position"], cols
        conn.close()
        rec = new.my_tasks_get(1, "gtasks:L1:a")
        assert rec["title"] == "Старая" and rec.get("gtasks_parent_id") is None
        new.my_tasks_upsert(1, [dict(rec, gtasks_parent_id="p", gtasks_position="0001"),
                                {"source": "gtasks", "source_key": "gtasks:L1:p",
                                 "title": "Родитель", "status": "В работе"}])
        # откат кода: старый db.py читает и обновляет, колонки иерархии не трогает
        old_recs = {r["source_key"]: r for r in old.my_tasks_load(1)}
        assert set(old_recs) == {"gtasks:L1:a", "gtasks:L1:p"}
        assert old_recs["gtasks:L1:a"]["title"] == "Старая"
        assert "gtasks_parent_id" not in old_recs["gtasks:L1:a"]
        old.my_tasks_upsert(1, [dict(old_recs["gtasks:L1:a"], status="Выполнено")])
        rec = new.my_tasks_get(1, "gtasks:L1:a")
        assert rec["status"] == "Выполнено"
        assert rec["gtasks_parent_id"] == "p" and rec["gtasks_position"] == "0001"
        old.init_db()                                     # старый init на новой схеме
        assert new.my_tasks_get(1, "gtasks:L1:a")["gtasks_parent_id"] == "p"


# ══ 2. Ссылки на письма ═══════════════════════════════════════════════════

def test_email_link_span_utf16_and_safety():
    mt = _load_my_tasks()
    link = f"✉️ Счёт №5: {MAIL}"
    text = f"Оплатить 🧾 счёт\n{link}"
    sp = mt.email_link_span(text, link)
    assert sp is not None
    s, e, url = sp
    assert url == MAIL
    u16 = text.encode("utf-16-le")
    assert u16[2 * s:2 * e].decode("utf-16-le") == MAIL
    assert e == len(u16) // 2 and e - s == len(MAIL)
    # 🧾 — суррогатная пара (+1 к индексу Python); ✉️ = U+2709 + U+FE0F (2 и там, и там)
    assert s == text.index(MAIL) + 1
    assert mt.utf16_len("✉️") == 2 and mt.utf16_len("🧾") == 2 and mt.utf16_len("ё") == 1
    # только ссылка без описания
    assert mt.email_link_span(f"T\n✉️ {MAIL}", f"✉️ {MAIL}")[2] == MAIL
    # недопустимые ссылки → None
    for bad in ("https://mail.google.com.evil.example/x", "http://mail.google.com/mail/x",
                "javascript:alert(1)", "https://evil.example/https://mail.google.com/",
                'https://mail.google.com/mail/"onmouseover="x', "https://mail.google.com/a'b",
                "https://mail.google.com/a<b", "https://mail.google.com/a\x7fb",
                "https://user@mail.google.com/x", "https://mail.google.com:8443/x",
                "https://mail.google.com/", "https://mail.google.com/a\\b"):
        ln = f"✉️ {bad}"
        assert mt.email_link_span(f"T\n{ln}", ln) is None, bad
    # текст ячейки не заканчивается ссылкой (override названия, хвост) → None
    assert mt.email_link_span("Своё название", link) is None
    assert mt.email_link_span(f"T\n{link} хвост", link) is None
    assert mt.email_link_span(f"T\n{link}\n", link) is None
    assert mt.email_link_span(text, "") is None and mt.email_link_span(None, link) is None
    assert mt.email_link_span(text, None) is None


def _updatecells(book):
    return [r["updateCells"] for r in book.requests() if "updateCells" in r]


def test_email_links_written_as_text_format_runs():
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        items = [
            _gt("m", title="Ответить 🙂 клиенту", due="2026-11-10T00:00:00.000Z",
                links=[{"type": "email", "description": "Re: счёт", "link": MAIL}]),
            _gt("f1", title="=IMPORTXML(\"https://example.invalid\",\"//a\")",
                due="2026-11-11T00:00:00.000Z"),
            _gt("f2", title="+1", due="2026-11-12T00:00:00.000Z"),
            _gt("f3", title="@x", due="2026-11-13T00:00:00.000Z"),
            _gt("f4", title="-x", due="2026-11-14T00:00:00.000Z"),
            _gt("bad", title="Чужой", due="2026-11-15T00:00:00.000Z",
                links=[{"type": "email", "link": "https://evil.example/x"}]),
        ]
        book, gt = _Book({"Планёрка": [PROTO_HDR]}), _FakeGT(items)
        assert _sync(mt, ms, db, book, gt)["outcome"] == mt.OUT_OK
        ws = book.ws[MY_TITLE]
        # значения — RAW: update без value_input_option, gspread по умолчанию raw=True
        kw = ws.update.call_args.kwargs
        assert set(kw) == {"values", "range_name"}, kw
        assert inspect.signature(ms.gspread.Worksheet.update).parameters["raw"].default is True
        data = book.data[MY_TITLE]
        titles = [r[3] for r in data[1:]]
        cell = f"Ответить 🙂 клиенту\n✉️ Re: счёт: {MAIL}"
        assert titles == [cell, "=IMPORTXML(\"https://example.invalid\",\"//a\")", "+1", "@x",
                          "-x", "Чужой\n✉️ https://evil.example/x"], titles
        ucs = _updatecells(book)
        assert len(ucs) == 1, ucs
        uc = ucs[0]
        assert uc["fields"] == "textFormatRuns"
        assert uc["range"] == {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 7,
                               "startColumnIndex": 3, "endColumnIndex": 4}
        runs = [row["values"][0]["textFormatRuns"] for row in uc["rows"]]
        s = len(cell.encode("utf-16-le")) // 2 - len(MAIL)
        assert runs[0] == [{"startIndex": 0},
                           {"startIndex": s, "format": {"link": {"uri": MAIL}}}], runs[0]
        assert runs[1:] == [[]] * 5, runs
        # ссылка ровно на URL в UTF-16
        assert cell.encode("utf-16-le")[2 * s:].decode("utf-16-le") == MAIL
        # нигде нет формул/USER_ENTERED
        src = (REPO_ROOT / "mymeet_sync.py").read_text(encoding="utf-8") + \
            (REPO_ROOT / "my_tasks.py").read_text(encoding="utf-8")
        assert "USER_ENTERED" not in src and "HYPERLINK" not in src
        # повторное чтение листа не создаёт overrides; повтор — без изменений
        snap = [list(r) for r in data]
        for _ in range(2):
            assert _sync(mt, ms, db, book, gt)["outcome"] == mt.OUT_OK
        assert book.data[MY_TITLE] == snap
        for r in db.my_tasks_load(1):
            assert not mt._json(r.get("overrides")), r["source_key"]
        # ручная правка названия → ссылки нет (текст пользователя не размечаем)
        book.data[MY_TITLE][1][3] = f"Моё название\n✉️ Re: счёт: {MAIL}"
        n_before = len(_updatecells(book))
        assert _sync(mt, ms, db, book, gt)["outcome"] == mt.OUT_OK
        last = _updatecells(book)[n_before:]
        assert last and all(row["values"][0]["textFormatRuns"] == [] for row in last[-1]["rows"])


def test_link_request_runs_shapes():
    """Форма runs: span в начале (без первого run) и в середине (с хвостом)."""
    ms = _load_ms()
    rows = [[1, "", "", "https://mail.google.com/x", "", "", "", "k1"],
            [2, "", "", "a https://mail.google.com/y b", "", "", "", "k2"],
            [3, "", "", "без ссылки", "", "", "", "k3"],
            [4, "", "", "коротко", "", "", "", "k4"]]
    req = ms._my_tasks_link_request(7, rows, {
        "k1": (0, 25, "https://mail.google.com/x"),
        "k2": (2, 27, "https://mail.google.com/y"),
        "k4": (0, 999, "https://mail.google.com/z"),          # вне текста → без runs
    })["updateCells"]
    runs = [r["values"][0]["textFormatRuns"] for r in req["rows"]]
    assert runs[0] == [{"startIndex": 0, "format": {"link": {"uri": "https://mail.google.com/x"}}}]
    assert runs[1] == [{"startIndex": 0},
                       {"startIndex": 2, "format": {"link": {"uri": "https://mail.google.com/y"}}},
                       {"startIndex": 27}]
    assert runs[2] == [] and runs[3] == []


def test_email_link_batch_error_is_warning_commit_done():
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR]})
        orig = book.ss.batch_update.side_effect

        def _bu(body, *a, **k):
            if any("updateCells" in r for r in body.get("requests", [])):
                raise RuntimeError("links quota")
            return orig(body, *a, **k) if orig else None
        book.ss.batch_update.side_effect = _bu
        gt = _FakeGT([_gt("m", title="Письмо", links=[{"type": "email", "link": MAIL}])])
        builder = mt.MyTasksBuilder(1, ME, gtasks=(gt.items, True, ["L1"]), gt_ops=gt, dbm=db)
        warnings, cnt, title = ms._sync_my_tasks_and_colors(book.ss, ME, rows_builder=builder)
        assert cnt == 1 and title == MY_TITLE
        assert any("ссылки на письма" in w for w in warnings), warnings
        assert builder.committed
        rec = db.my_tasks_get(1, "gtasks:L1:m")
        assert rec["sheet_status"] == "В работе" and mt._json(rec["sheet_fields"])
        assert book.data[MY_TITLE][1][3] == f"Письмо\n✉️ {MAIL}"


def test_legacy_mode_no_link_request():
    """Режим без builder (read-only лист) — updateCells не отправляется."""
    ms = _load_ms()
    book = _Book({"Планёрка": [PROTO_HDR, ["1", "10.11.2026", "Сводка", "Иван Петров", "",
                                          "В работе", "m1"]]})
    ms._sync_my_tasks_and_colors(book.ss, ME)
    assert _updatecells(book) == []


# ══ 3. Телемост ═══════════════════════════════════════════════════════════

TM_URL = "https://telemost.yandex.ru/j/12345678901234"


def _tm_ns():
    return _ts._load_main_funcs(("_telemost_line", "has_conference_link"))


def test_telemost_line_anchor_and_fallback():
    ns = _tm_ns()
    line = ns["_telemost_line"](TM_URL)
    assert line == f'🔗 Яндекс Телемост: <a href="{TM_URL}">{TM_URL}</a>', line
    assert line.endswith("</a>")
    href = line.split('href="', 1)[1].split('"', 1)[0]
    text = line.split(">", 1)[1].rsplit("</a>", 1)[0]
    assert href == text == TM_URL
    for host in ("telemost.yandex.com", "telemost.yandex.net", "telemost.360.yandex.ru",
                 "telemost.360.yandex.com"):
        u = f"https://{host}/j/98765432109876"
        assert ns["_telemost_line"](u) == f'🔗 Яндекс Телемост: <a href="{u}">{u}</a>'
        assert ns["has_conference_link"]({"description": ns["_telemost_line"](u)}), host
    # невалидные → прежний текстовый формат, без HTML
    for bad in ("http://telemost.yandex.ru/j/1", "https://evil.example/j/1",
                "https://telemost.yandex.ru.evil.example/j/1", "https://TELEMOST.yandex.ru/j/1",
                "https://telemost.yandex.ru/j/1?x=1", "https://telemost.yandex.ru/j/1#f",
                "https://telemost.yandex.ru/j/abc", "https://telemost.yandex.ru/x/1",
                'https://telemost.yandex.ru/j/1"><script>x</script>',
                "https://telemost.yandex.ru/j/1&x=<b>", "javascript:alert(1)",
                "https://user@telemost.yandex.ru/j/1", "https://telemost.yandex.ru:8443/j/1",
                " https://telemost.yandex.ru/j/1", "https://telemost.yandex.ru/j/1\n",
                "https://telemost.yandex.ru/j/", ""):
        out = ns["_telemost_line"](bad)
        assert out == f"🔗 Яндекс Телемост: {bad}", (bad, out)
        assert "<a" not in out
    # экранирование применяется к href и тексту
    import html
    assert html.escape(TM_URL, quote=True) == TM_URL
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert "html.escape(url, quote=True)" in src


def test_telemost_new_format_dedup_and_callers():
    ns = _tm_ns()
    line = ns["_telemost_line"](TM_URL)
    assert ns["has_conference_link"]({"description": line})
    assert ns["has_conference_link"]({"description": f"Повестка\n\n{line}"})
    assert not ns["has_conference_link"]({"description": "Повестка"})
    # add_telemost_to_event: новый формат, повтор не дублирует
    cal = _ts._FakeCalendar([_ts._ev("e1", description="Повестка")])
    env, create, _ = _ts._telemost_env(cal, links=[TM_URL])
    res = env["process_telemost_links"](1)
    assert len(res["added"]) == 1
    desc = cal.store["e1"]["description"]
    assert desc == f"Повестка\n\n{line}", desc
    res = env["process_telemost_links"](1)
    assert res["added"] == [] and create.call_count == 1
    # create_calendar_event использует тот же формат
    from datetime import datetime, timedelta
    from unittest.mock import MagicMock
    svc = MagicMock()
    tz = NS(zone="Europe/Moscow", localize=lambda dt: dt)
    ns2 = _ts._load_main_funcs(("create_calendar_event", "_telemost_line"), {
        "db": NS(get_user=lambda tid: {}), "_tz": lambda u: tz,
        "_get_calendar_id": lambda u: "primary",
        "get_calendar_service": lambda tid: svc, "timedelta": timedelta, "datetime": datetime,
    })
    ns2["create_calendar_event"](1, "Встреча", datetime(2026, 11, 20, 10, 0), 30, TM_URL)
    body = svc.events.return_value.insert.call_args.kwargs["body"]
    assert body["description"] == line
    ns2["create_calendar_event"](1, "Встреча", datetime(2026, 11, 20, 10, 0), 30, None)
    assert "description" not in svc.events.return_value.insert.call_args.kwargs["body"]


TESTS = [
    (test_flat_list_byte_identical, "#36 иерархия: плоский список побайтно как раньше"),
    (test_hierarchy_three_levels_and_position_order, "#36 иерархия: 3 уровня, порядок position"),
    (test_hierarchy_parent_child_on_different_pages, "#36 иерархия: родитель и дети на разных страницах"),
    (test_hierarchy_reparent_and_move_list, "#36 иерархия: смена родителя, перенос списка"),
    (test_hierarchy_orphans_cycles_depth, "#36 иерархия: сироты, циклы, лимит глубины"),
    (test_hierarchy_done_parent_open_child_and_protocol_mix, "#36 иерархия: выполненный родитель"),
    (test_hierarchy_sync_preserves_edits_no_duplicates, "#36 иерархия: правки сохранены, без дублей"),
    (test_hierarchy_guards_unchanged, "#36 иерархия: confirm_missing/массовая/сжатие"),
    (test_db_hierarchy_migration_idempotent_and_rollback, "#36 БД: миграция и откат кода"),
    (test_email_link_span_utf16_and_safety, "#36 письма: UTF-16 и allowlist"),
    (test_email_links_written_as_text_format_runs, "#36 письма: updateCells textFormatRuns, RAW"),
    (test_link_request_runs_shapes, "#36 письма: форма runs"),
    (test_email_link_batch_error_is_warning_commit_done, "#36 письма: ошибка → warning, commit"),
    (test_legacy_mode_no_link_request, "#36 письма: legacy-лист без updateCells"),
    (test_telemost_line_anchor_and_fallback, "#36 Телемост: <a href>, allowlist, fallback"),
    (test_telemost_new_format_dedup_and_callers, "#36 Телемост: дедуп и вызывающие"),
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
    if failed:
        raise SystemExit(f"\n❌ Упали тесты: {failed}")
    print(f"\n✅ {len(TESTS)} тестов прошли")
