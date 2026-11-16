"""
Тесты продуктовой аналитики /analytics (issue #46).

Без сети: временная SQLite, управляемое время (параметр now), фиктивные
пользователи; функции main.py извлекаются через AST (бот не запускается).
Запуск: python test_analytics.py  (также входит в python test_smoke.py)
"""
import ast
import asyncio
import logging
import sqlite3
import tempfile
import warnings
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import test_smoke as _ts

DAY = 86400


def T(s: str) -> float:
    """'2026-03-02 10:00' (UTC) → epoch."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()


def _db(tmp):
    db = _ts._load_db(tmp)
    db.init_db()
    return db


def _rows(db, sql, args=()):
    c = sqlite3.connect(db.DB_PATH)
    r = c.execute(sql, args).fetchall()
    c.close()
    return r


_MAIN_FUNCS = ["_track", "_is_excluded_command", "_track_activity", "_ru_users", "_pct",
               "_format_analytics", "cmd_analytics", "_parse_admin_id",
               "scheduled_analytics_cleanup"]
_MAIN_ASSIGNS = ("ANALYTICS_EXCLUDED_COMMANDS",)


def _main(db, admin=None, logger=None):
    src = (_ts.REPO_ROOT / "main.py").read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(src)
    fdefs = (ast.FunctionDef, ast.AsyncFunctionDef)
    nodes = [n for n in tree.body
             if (isinstance(n, fdefs) and n.name in _MAIN_FUNCS)
             or (isinstance(n, ast.Assign) and any(
                 isinstance(t, ast.Name) and t.id in _MAIN_ASSIGNS for t in n.targets))]
    assert {n.name for n in nodes if isinstance(n, fdefs)} == set(_MAIN_FUNCS)
    lg = logger or logging.getLogger("analytics_test")
    ns = {"db": db, "logger": lg, "Update": object, "asyncio": asyncio,
          "ContextTypes": NS(DEFAULT_TYPE=object), "datetime": datetime,
          "pytz": NS(timezone=ZoneInfo), "ADMIN_TELEGRAM_ID": admin,
          "ANALYTICS_REPORT_TZ": "Europe/Moscow"}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), ns)
    return ns


class Msg:
    def __init__(self, text=None):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))


def _upd(uid, text=None, callback=False, bot=False):
    msg = Msg(text)
    return NS(effective_user=NS(id=uid, is_bot=bot, username="u"),
              message=None if callback else msg, effective_message=msg,
              callback_query=NS(data="x") if callback else None)


# ── 1. DAU / WAU / MAU ───────────────────────────────────────────────────────

def test_dau_wau_mau_unique_users():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        now = T("2026-03-20 12:00")
        # user 1: много действий сегодня и раньше — один раз в каждой метрике
        for h in range(5):
            db.analytics_record(1, "user_active", now=now - h * 600)
        db.analytics_record(1, "user_active", now=now - 3 * DAY)
        db.analytics_record(2, "user_active", now=now - 6 * DAY)    # граница WAU
        db.analytics_record(3, "user_active", now=now - 7 * DAY)    # вне WAU
        db.analytics_record(4, "user_active", now=now - 29 * DAY)   # граница MAU
        db.analytics_record(5, "user_active", now=now - 30 * DAY)   # вне MAU
        d = db.analytics_report_data(now=now)
        assert (d["dau"], d["wau"], d["mau"]) == (1, 2, 4), d
        assert _rows(db, "SELECT COUNT(*) FROM analytics_events WHERE user_id=1")[0][0] == 2


def test_day_and_month_boundaries():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        assert db.analytics_record(1, "user_active", now=T("2026-02-28 23:59"))
        assert db.analytics_record(1, "user_active", now=T("2026-03-01 00:00"))  # новый день
        assert not db.analytics_record(1, "user_active", now=T("2026-03-01 23:59"))
        days = [r[0] for r in _rows(db, "SELECT day FROM analytics_events ORDER BY day")]
        assert days == ["2026-02-28", "2026-03-01"], days
        # 2026 — не високосный: 7 дней до 06.03 включительно начинаются 28.02
        d = db.analytics_report_data(now=T("2026-03-06 08:00"))
        assert (d["dau"], d["wau"]) == (0, 1), d
        d = db.analytics_report_data(now=T("2026-03-07 08:00"))
        assert d["wau"] == 1 and db._shift_day("2026-03-07", -6) == "2026-03-01"
        assert db._shift_day("2024-03-01", -1) == "2024-02-29"           # високосный
        assert db._shift_day("2026-12-31", 1) == "2027-01-01"


# ── 2. Что не считается активностью ──────────────────────────────────────────

def test_analytics_command_not_counted():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        ns = _main(db, admin=1)
        for text in ("/analytics", "/analytics@ZinaBot", "/ANALYTICS extra"):
            asyncio.run(ns["_track_activity"](_upd(1, text), None))
        assert _rows(db, "SELECT COUNT(*) FROM analytics_events")[0][0] == 0
        asyncio.run(ns["_track_activity"](_upd(1, "/analyticsX"), None))   # другая команда
        asyncio.run(ns["_track_activity"](_upd(2, "привет"), None))
        asyncio.run(ns["_track_activity"](_upd(3, callback=True), None))
        asyncio.run(ns["_track_activity"](_upd(4, "hi", bot=True), None))  # бот — нет
        users = sorted(r[0] for r in _rows(
            db, "SELECT DISTINCT user_id FROM analytics_events WHERE event_type='user_active'"))
        assert users == [1, 2, 3], users


def _called_names(fn_node):
    out = set()
    for n in ast.walk(fn_node):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
        elif isinstance(n, ast.Name):
            out.add(n.id)                 # переданные как аргумент (lambda, executor)
    return out


def test_background_jobs_never_track():
    """Фоновые задания (scheduler) не вызывают _track / analytics_record —
    ни напрямую, ни через функции main.py, которые они вызывают."""
    src = (_ts.REPO_ROOT / "main.py").read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    jobs = ["scheduled_mymeet_sync", "scheduled_my_tasks_daily",
            "scheduled_telemost_all_users", "send_daily_schedule",
            "scheduled_analytics_cleanup"]
    for job in jobs:
        assert job in funcs, job
        seen, stack = set(), [job]
        while stack:
            name = stack.pop()
            if name in seen or name not in funcs:
                continue
            seen.add(name)
            stack.extend(_called_names(funcs[name]) & set(funcs))
        assert "_track" not in seen and "_track_activity" not in seen, (job, seen)
        body = "".join(ast.unparse(funcs[n]) for n in seen)
        assert "analytics_record" not in body, job
    # health check — лямбда, без аналитики
    assert 'add_get("/health", lambda r: web.Response(text="ok"))' in src


def test_hooks_registered_in_handlers():
    src = (_ts.REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert "app.add_handler(TypeHandler(Update, _track_activity), group=-1)" in src
    assert 'CommandHandler("analytics",     cmd_analytics)' in src

    def body(name):
        i = src.index(f"def {name}(")
        j = src.find("\nasync def ", i + 1)
        k = src.find("\ndef ", i + 1)
        return src[i:min(x for x in (j, k) if x > 0)]
    expect = {
        "cmd_connect": ['"connect_started"'],
        "oauth_callback": ['"connect_completed"'],
        "cmd_tasks": ['"tasks_used"', '"first_value", source="tasks"'],
        "cmd_sync_tasks": ['"tasks_used"', '"first_value", source="sync_tasks"'],
        "handle_forwarded": ['"tasks_used", source="forward"'],
        "cmd_sync_mymeet": ['"mymeet_used"', '"first_value", source="sync_mymeet"'],
        "cmd_sync_mymeet_full": ['"mymeet_used"'],
        "cmd_setup_mymeet": ['"mymeet_used"'],
        "handle_create_event": ['"first_value", source="calendar_event"'],
    }
    for fn, needles in expect.items():
        b = body(fn)
        for nd in needles:
            assert nd in b, (fn, nd)
    # first_value в /tasks — только после успешного ответа (не на ошибке)
    b = body("cmd_tasks")
    assert b.index('"first_value"') > b.index("_format_tasks(tasks)")
    assert b.index('"first_value"') > b.index("if err:")
    assert b.index('"tasks_used"') > b.index("is_google_connected")


# ── 3. Retention ─────────────────────────────────────────────────────────────

def test_retention_cohorts_point_in_time():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        D = T("2026-03-01 10:00")
        act = lambda u, d: db.analytics_record(u, "user_active", now=D + d * DAY)
        act(1, 0); act(1, 1); act(1, 7)          # D1 и D7
        act(2, 0); act(2, 8)                     # вернулся на 8-й — не D7 (не rolling)
        act(3, 0); act(3, 30)                    # D30
        act(4, 6)                                # когорта 07.03
        now = D + 9 * DAY                        # 10.03: завершены дни до 09.03
        d = db.analytics_report_data(now=now)
        r = d["retention"]
        # D1: когорты 01.03 (3 польз.) и 07.03 (1) — цель 02.03 / 08.03 завершены
        assert r[1] == (1, 4), r
        # D7: цель 08.03 для когорты 01.03 завершена; для 07.03 (14.03) — нет
        assert r[7] == (1, 3), r
        assert r[30] == (0, 0), r                # незрелые когорты не в знаменателе
        d = db.analytics_report_data(now=D + 32 * DAY)
        assert d["retention"][30] == (1, 3), d["retention"]


def test_immature_cohort_today_not_counted():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        now = T("2026-03-10 12:00")
        db.analytics_record(1, "user_active", now=now - DAY)    # вчера: D1 = сегодня
        db.analytics_record(1, "user_active", now=now)
        d = db.analytics_report_data(now=now)
        assert d["retention"][1] == (0, 0), d["retention"]     # день ещё не завершён


# ── 4. Воронка ───────────────────────────────────────────────────────────────

def test_funnel_order_and_cohort():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        now = T("2026-03-20 12:00")
        rec = lambda u, e, t, **k: db.analytics_record(u, e, now=t, **k)
        base = now - 10 * DAY
        # u1: полный путь по порядку
        rec(1, "user_started", base); rec(1, "connect_started", base + 60)
        rec(1, "connect_completed", base + 120); rec(1, "first_value", base + 600, source="tasks")
        # u2: connect_completed раньше connect_started — шаг 3 не засчитан
        rec(2, "user_started", base); rec(2, "connect_completed", base + 60)
        rec(2, "connect_started", base + 120)
        # u3: только старт, пришёл 2 дня назад — неполное наблюдение
        rec(3, "user_started", now - 2 * DAY)
        # u4: пришёл 40 дней назад — вне когорты
        rec(4, "user_started", now - 40 * DAY); rec(4, "connect_started", now - 40 * DAY + 5)
        # u5: first_value до подключения — шаг 4 не засчитан
        rec(5, "user_started", base); rec(5, "first_value", base + 30, source="calendar_event")
        rec(5, "connect_started", base + 60); rec(5, "connect_completed", base + 90)
        d = db.analytics_report_data(now=now)
        assert d["funnel"] == [4, 3, 2, 1], d["funnel"]
        assert d["funnel_recent"] == 1


# ── 5. Дубли и идемпотентность ───────────────────────────────────────────────

def test_duplicates_do_not_distort():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        t = T("2026-03-05 10:00")
        assert db.analytics_record(1, "connect_completed", now=t)
        assert not db.analytics_record(1, "connect_completed", now=t + 5)   # повтор callback
        assert db.analytics_record(1, "user_started", now=t)
        assert not db.analytics_record(1, "user_started", now=t + 3 * DAY)
        assert db.analytics_record(1, "first_value", now=t, source="tasks")
        assert not db.analytics_record(1, "first_value", now=t + DAY, source="sync_mymeet")
        assert _rows(db, "SELECT COUNT(*) FROM analytics_events")[0][0] == 3


# ── 6. Доступ, отчёт, отказоустойчивость ─────────────────────────────────────

def test_unauthorized_gets_neutral_answer():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        calls = []
        real = db.analytics_report_data
        db.analytics_report_data = lambda *a, **k: calls.append(1) or real(*a, **k)
        for admin, uid in ((42, 7), (None, 42), (None, 7)):
            ns = _main(db, admin=admin)
            u = _upd(uid, "/analytics")
            asyncio.run(ns["cmd_analytics"](u, None))
            assert u.message.replies == [("Команда недоступна", {})], u.message.replies
        assert calls == []                                   # статистика не считалась
        ns = _main(db, admin=42)
        u = _upd(42, "/analytics")
        asyncio.run(ns["cmd_analytics"](u, None))
        text, kw = u.message.replies[0]
        assert kw.get("parse_mode") == "HTML" and "Аналитика бота" in text
        assert ns["_parse_admin_id"]("  42 ") == 42
        assert ns["_parse_admin_id"]("@admin") is None and ns["_parse_admin_id"]("") is None


def test_report_format_and_insufficient_data():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        ns = _main(db, admin=1)
        d = db.analytics_report_data(now=T("2026-03-05 10:00"))
        text = ns["_format_analytics"](d)
        assert "MAU: 0" in text and "DAU/MAU: н/д" in text
        assert text.count("недостаточно данных") == 3, text
        assert "Аналитика собирается с " in text and "Новые пользователи за 30 дней" in text
        d.update(dau=12, mau=91, retention={1: (8, 25), 7: (0, 0), 30: (0, 0)},
                 features={"tasks_used": 21, "mymeet_used": 1, "connect_completed": 3},
                 funnel=[100, 72, 61, 48], funnel_recent=5)
        text = ns["_format_analytics"](d)
        assert "DAU/MAU: 13,2%" in text and "D1 retention: 32% (8 из 25)" in text
        assert "Задачи: 21 пользователь" in text and "MYmeet: 1 пользователь" in text
        assert "Google Connect: 3 пользователя" in text
        assert "Запустили /connect: 72 — от предыдущего 72,0%, от начала 72,0%" in text
        assert "Совершили первое полезное действие: 48 — от предыдущего 78,7%" in text
        assert "5 пришли менее 7 дней назад" in text


def test_analytics_failure_does_not_break():
    class Boom:
        def __getattr__(self, name):
            def f(*a, **k):
                raise sqlite3.OperationalError("database is locked: secret-ish detail")
            return f
    records = []

    class H(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())
    lg = logging.getLogger("analytics_fail_test")
    lg.propagate = False
    lg.addHandler(H())
    ns = _main(Boom(), admin=1, logger=lg)
    ns["_track"](1, "user_active")                                  # не бросает
    asyncio.run(ns["_track_activity"](_upd(1, "привет"), None))    # не бросает
    asyncio.run(ns["scheduled_analytics_cleanup"]())               # не бросает
    u = _upd(1, "/analytics")
    asyncio.run(ns["cmd_analytics"](u, None))
    assert "Не удалось построить отчёт" in u.message.replies[0][0]
    assert records and all("secret-ish" not in r for r in records), records


# ── 7. Приватность, миграция, очистка, удаление ──────────────────────────────

def test_no_content_or_secrets_stored():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        cols = [r[1] for r in _rows(db, "PRAGMA table_info(analytics_events)")]
        assert cols == ["id", "user_id", "event_type", "created_at", "day", "metadata",
                        "dedup_key"], cols
        for bad in (("user_active", {"source": "текст сообщения"}),
                    ("message_text", {})):
            try:
                db.analytics_record(1, bad[0], **bad[1])
                raise AssertionError(f"принято: {bad}")
            except ValueError:
                pass
        db.analytics_record(1, "first_value", source="calendar_event")
        metas = {r[0] for r in _rows(db, "SELECT metadata FROM analytics_events")}
        assert metas == {'{"source": "calendar_event"}'}, metas
        src = (_ts.REPO_ROOT / "db.py").read_text(encoding="utf-8")
        i = src.index("# ─── Продуктовая аналитика (#46)")
        block = src[i:src.index("# ─── Метрики событий")]
        assert "f\"DELETE" not in block and "% (" not in block.replace(
            "'{\"source\": \"%s\"}' % source", "")     # SQL параметризован


def test_migration_preserves_data_and_start_date():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        db.upsert_user(10, "a")
        db.log_event(10, "my_task_forward")
        started = db.analytics_started_at()
        assert started and abs(started - datetime.now(timezone.utc).timestamp()) < 60
        c = sqlite3.connect(db.DB_PATH)
        c.execute("UPDATE analytics_meta SET value='1700000000' WHERE key='started_at'")
        c.commit()
        c.close()
        db.init_db()                                           # повторная миграция
        assert db.analytics_started_at() == 1700000000.0       # дата начала не сброшена
        assert db.get_user(10) is not None
        assert _rows(db, "SELECT COUNT(*) FROM events")[0][0] == 1


def test_cleanup_and_user_deletion_scope():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        now = T("2027-06-01 12:00")
        db.upsert_user(1, "a")
        db.log_event(1, "my_task_forward")
        db.analytics_record(1, "user_active", now=now - 400 * DAY)    # старше 13 мес.
        db.analytics_record(1, "user_active", now=now - 10 * DAY)
        db.analytics_record(2, "user_active", now=now - 10 * DAY)
        assert db.analytics_cleanup(now=now) == 1
        assert _rows(db, "SELECT COUNT(*) FROM analytics_events")[0][0] == 2
        assert db.get_user(1) is not None and _rows(db, "SELECT COUNT(*) FROM events")[0][0] == 1
        assert db.analytics_delete_user(1) == 1
        assert [r[0] for r in _rows(db, "SELECT user_id FROM analytics_events")] == [2]
        assert db.get_user(1) is not None                      # другие подсистемы целы


def test_new_users_from_registration_date():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        c = sqlite3.connect(db.DB_PATH)
        c.executemany("INSERT INTO users (telegram_id, created_at) VALUES (?, ?)",
                      [(1, "2026-01-01 10:00:00"), (2, "2026-02-19 00:00:00"),
                       (3, "2026-03-19 23:00:00")])
        c.commit()
        c.close()
        d = db.analytics_report_data(now=T("2026-03-20 12:00"))
        # 30 дней включая 20.03 → с 19.02
        assert (d["new_users_30d"], d["total_users"]) == (2, 3), d


def test_returning_after_cleanup_not_new():
    """PO #1: впервые активен > 396 дней назад, события очищены, вернулся —
    не новый пользователь (нет user_started) и не новая когорта retention."""
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        ns = _main(db, admin=99)
        t0 = T("2025-01-10 10:00")
        db.analytics_record(1, "user_started", now=t0)
        db.analytics_record(1, "user_active", now=t0)
        db.analytics_record(1, "user_active", now=t0 + DAY)
        back = t0 + 420 * DAY
        assert db.analytics_cleanup(now=back) == 3                    # сырые события удалены
        assert _rows(db, "SELECT first_active_day FROM analytics_users WHERE user_id=1") == [
            ("2025-01-10",)]                                          # истинная дата цела
        # Возвращается: пользователя нет в users (писал только текст) — худший случай
        assert db.get_user(1) is None
        asyncio.run(ns["_track_activity"](_upd(1, "привет"), None))
        assert _rows(db, "SELECT COUNT(*) FROM analytics_events WHERE event_type='user_started'"
                     )[0][0] == 0, "вернувшийся стал «новым»"
        assert not db.analytics_record(1, "user_started", now=back + 60)
        # Новый пользователь в тот же день — для сравнения
        asyncio.run(ns["_track_activity"](_upd(2, "привет"), None))
        later = datetime.now(timezone.utc).timestamp() + 2 * DAY
        db.analytics_record(1, "user_active", now=later)              # «D1» от возвращения
        d = db.analytics_report_data(now=later + DAY)
        assert d["funnel"][0] == 1                                     # только пользователь 2
        # Пользователь 1 не образует новую когорту: в D1 только пользователь 2
        assert d["retention"][1][1] == 1, d["retention"]
        assert _rows(db, "SELECT first_active_day FROM analytics_users WHERE user_id=1") == [
            ("2025-01-10",)]


def test_first_active_is_minimum_even_out_of_order():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        db.analytics_record(1, "user_active", now=T("2026-03-10 10:00"))
        db.analytics_record(1, "user_active", now=T("2026-03-05 10:00"))   # позже записано
        assert _rows(db, "SELECT first_active_day FROM analytics_users") == [("2026-03-05",)]


def test_delete_user_removes_first_active():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        db.analytics_record(1, "user_active", now=T("2026-03-10 10:00"))
        db.analytics_record(2, "user_active", now=T("2026-03-10 10:00"))
        db.analytics_delete_user(1)
        assert [r[0] for r in _rows(db, "SELECT user_id FROM analytics_users")] == [2]
        assert [r[0] for r in _rows(db, "SELECT user_id FROM analytics_events")] == [2]


def test_migration_backfills_first_active():
    """БД с событиями, но без analytics_users (как до исправления) — заполняется
    минимальной датой; повторная миграция ничего не меняет."""
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        db.analytics_record(1, "user_active", now=T("2026-03-10 10:00"))
        db.analytics_record(1, "user_active", now=T("2026-03-12 10:00"))
        db.analytics_record(1, "tasks_used", now=T("2026-03-01 10:00"))   # не активность
        c = sqlite3.connect(db.DB_PATH)
        c.execute("DROP TABLE analytics_users")
        c.commit()
        c.close()
        db.init_db()
        assert _rows(db, "SELECT user_id, first_active_day FROM analytics_users") == [
            (1, "2026-03-10")]
        db.init_db()
        assert _rows(db, "SELECT COUNT(*) FROM analytics_users")[0][0] == 1
        assert _rows(db, "SELECT COUNT(*) FROM analytics_events")[0][0] == 3


def test_new_users_upper_bound_and_utc_label():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        c = sqlite3.connect(db.DB_PATH)
        c.executemany("INSERT INTO users (telegram_id, created_at) VALUES (?, ?)",
                      [(1, "2026-03-20 23:59:59"), (2, "2026-03-21 00:00:00"),
                       (3, "2027-01-01 00:00:00")])                    # будущие даты
        c.commit()
        c.close()
        d = db.analytics_report_data(now=T("2026-03-20 12:00"))
        assert d["new_users_30d"] == 1, d
        text = _main(db)["_format_analytics"](d)
        assert "Календарные периоды: UTC" in text


TESTS = [
    (test_dau_wau_mau_unique_users, "Аналитика: DAU/WAU/MAU — уникальные пользователи"),
    (test_day_and_month_boundaries, "Аналитика: границы суток и месяцев"),
    (test_analytics_command_not_counted, "Аналитика: /analytics и боты не считаются"),
    (test_background_jobs_never_track, "Аналитика: фоновые задания не создают активность"),
    (test_hooks_registered_in_handlers, "Аналитика: события в обработчиках и регистрация"),
    (test_retention_cohorts_point_in_time, "Аналитика: retention D1/D7/D30 по когортам"),
    (test_immature_cohort_today_not_counted, "Аналитика: незрелые когорты вне знаменателя"),
    (test_funnel_order_and_cohort, "Аналитика: воронка — порядок шагов и когорта"),
    (test_duplicates_do_not_distort, "Аналитика: повторные события не искажают"),
    (test_unauthorized_gets_neutral_answer, "Аналитика: доступ только администратору"),
    (test_report_format_and_insufficient_data, "Аналитика: формат отчёта, «недостаточно данных»"),
    (test_analytics_failure_does_not_break, "Аналитика: сбой аналитики не ломает команды"),
    (test_no_content_or_secrets_stored, "Аналитика: нет контента и секретов в БД"),
    (test_migration_preserves_data_and_start_date, "Аналитика: миграция сохраняет данные"),
    (test_cleanup_and_user_deletion_scope, "Аналитика: очистка и удаление — только события"),
    (test_new_users_from_registration_date, "Аналитика: новые пользователи по регистрации"),
    (test_returning_after_cleanup_not_new,
     "Аналитика: вернувшийся после очистки — не новый и не новая когорта"),
    (test_first_active_is_minimum_even_out_of_order, "Аналитика: дата первой активности — минимум"),
    (test_delete_user_removes_first_active, "Аналитика: удаление пользователя — и дата первой активности"),
    (test_migration_backfills_first_active, "Аналитика: миграция заполняет дату первой активности"),
    (test_new_users_upper_bound_and_utc_label,
     "Аналитика: верхняя граница новых пользователей, подпись UTC"),
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
