"""
Тесты ежедневного автоимпорта MYmeet (issue #39).

Без сети: MYmeet API, Google Calendar и Google Sheets — фейки, БД — временная,
Telegram — фейковый bot.  Проверяется реальный код main.py (функции
извлекаются через AST) и mymeet_sync.py.
Запуск: python test_mymeet_daily.py  (также входит в python test_smoke.py)
"""
import asyncio
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(prefix="mymeet_daily_"),
                                              "never.db"))

from zoneinfo import ZoneInfo  # noqa: E402
import test_smoke as _ts  # noqa: E402

from cryptography.fernet import Fernet  # noqa: E402
os.environ["ENCRYPTION_KEY"] = os.environ.get("ENCRYPTION_KEY") or Fernet.generate_key().decode()
import crypto as _crypto  # noqa: E402
from crypto import CryptoDecryptError  # noqa: E402

import gspread  # noqa: E402
import requests  # noqa: E402

PROTO_HDR = _ts._PROTO_HDR
ME = "Иван Петров"
MSK = ZoneInfo("Europe/Moscow")
UNVERIFIED = "Требует верификации"


def _ms():
    ms = _ts._load_mymeet()
    ms._throttle = lambda: None
    return ms


def _tmp_db(tmp):
    db = _ts._load_db(tmp)
    db.init_db()
    return db


# ── Таблица в памяти: учитывает диапазоны записи ─────────────────────────────

class Book:
    def __init__(self, sheets=None):
        from unittest.mock import MagicMock
        self.data, self.ws, self.writes = {}, {}, []
        self.ss = MagicMock()
        for t, rows in (sheets or {}).items():
            self._new(t, [list(r) for r in rows])
        self.ss.fetch_sheet_metadata.side_effect = lambda params=None: {"sheets": [
            {"properties": {"sheetId": w.id, "title": t}} for t, w in self.ws.items()]}
        self.ss.values_batch_get.side_effect = self._batch_get
        self.ss.worksheet.side_effect = self._get
        self.ss.add_worksheet.side_effect = lambda title, rows, cols: self._new(title, [])
        self.fail_reads = False

    def _get(self, title):
        if title not in self.ws:
            raise gspread.WorksheetNotFound(title)
        return self.ws[title]

    def _new(self, title, rows):
        from unittest.mock import MagicMock
        w = MagicMock()
        w.id = 10 + len(self.ws)
        w.title = title
        w.row_count, w.col_count = 1000, 10
        self.data[title] = rows

        def _upd(*args, **kw):
            rng = kw.get("range_name")
            vals = kw.get("values")
            for a in args:
                if isinstance(a, str):
                    rng = a
                else:
                    vals = a
            start = int("".join(ch for ch in rng.split(":")[0] if ch.isdigit()) or 1)
            cur = self.data[title]
            while len(cur) < start - 1 + len(vals):
                cur.append([])
            for i, v in enumerate(vals):
                cur[start - 1 + i] = ["" if x is None else x for x in v]
            self.writes.append(title)
        w.update.side_effect = _upd
        w.get_all_values.side_effect = lambda: [list(map(str, r)) for r in self.data[title]]
        w.batch_clear.side_effect = lambda ranges: None
        self.ws[title] = w
        return w

    def _batch_get(self, ranges):
        if self.fail_reads:
            raise RuntimeError("read-boom")
        out = []
        for r in ranges:
            t = r.rsplit("!", 1)[0][1:-1].replace("''", "'")
            out.append({"values": [[str(x) for x in row] for row in self.data.get(t, [])]})
        return {"valueRanges": out}

    def task_rows(self, title=None):
        """[(лист, задача, ответственный, статус)] по всем листам протоколов."""
        out = []
        for t, rows in self.data.items():
            if title and t != title:
                continue
            if not rows or rows[0][:3] != PROTO_HDR[:3]:
                continue
            for r in rows[1:]:
                if len(r) >= 6 and str(r[0]).strip().isdigit():
                    out.append((t, r[2], r[3], r[5]))
        return out


# ── MYmeet / Calendar фейки ──────────────────────────────────────────────────

def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Mymeet:
    """meetings: id -> (name, start_dt, tasks|None); None — протокол не готов (404)."""

    def __init__(self):
        self.meetings = {}
        self.fail = None          # исключение для списка встреч
        self.proto_fail = set()   # id протоколов с ошибкой (не 404)
        self.calls = 0

    def add(self, mid, name, start, tasks):
        self.meetings[mid] = (name, start, tasks)

    def install(self, ms):
        def _fetch_meetings(days=30, token=None, ws_id=None):
            self.calls += 1
            if self.fail:
                raise self.fail
            return [{"id": mid, "name": n, "startTime": _iso(st), "calendarEventId": "ev-" + mid}
                    for mid, (n, st, _t) in self.meetings.items()]

        def _fetch_protocol(mid, token=None):
            if mid in self.proto_fail:
                raise ms.ProtocolFetchError("HTTP 502")
            tasks = self.meetings[mid][2]
            if tasks is None:
                return None
            return {"followup_v2": {"templates": [{"entities": [{
                "name": "tasks",
                "html_text": [{"task": t, "assignee": {"speaker": a}, "deadline": "",
                               **({"done": True} if done else {})}
                              for (t, a, done) in tasks],
            }]}]}}
        ms._fetch_meetings = _fetch_meetings
        ms._fetch_protocol = _fetch_protocol

    def gcal(self):
        return {"ids": {"ev-" + mid for mid in self.meetings}, "times": set()}


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def _quiet_logger():
    lg = logging.getLogger("mymeet_daily_test")
    lg.addHandler(logging.NullHandler())
    lg.propagate = False
    return lg


_MAIN_FUNCS = ["_run_mymeet_auto", "scheduled_mymeet_sync", "_mymeet_auto_notice",
               "_user_row_to_mymeet_config", "_user_full_name", "_user_lock", "_tz"]
_MAIN_ASSIGNS = ("MYMEET_AUTO_REASON_TEXT", "_GCAL_REASONS")


def _load_main(names, extra_ns, assigns):
    """Функции (в том числе async) и присваивания из main.py без импорта бота."""
    import ast
    import typing
    import warnings
    src = (_ts.REPO_ROOT / "main.py").read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(src)
    fdefs = (ast.FunctionDef, ast.AsyncFunctionDef)
    nodes = [n for n in tree.body
             if (isinstance(n, fdefs) and n.name in names)
             or (isinstance(n, ast.Assign) and any(
                 isinstance(t, ast.Name) and t.id in assigns for t in n.targets))]
    found = {n.name for n in nodes if isinstance(n, fdefs)}
    assert found == set(names), f"не найдены в main.py: {set(names) - found}"
    ns = {"Optional": typing.Optional}
    ns.update(extra_ns)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), ns)
    return ns


class Env:
    """Реальные функции main.py + реальный mymeet_sync + временная БД."""

    def __init__(self, tmp, users=((1, "Europe/Moscow"),), gcal_reason=None):
        import test_my_tasks as tmt
        self.db = _tmp_db(tmp)
        self.ms = _ms()
        self.mt = tmt._load_my_tasks()
        self.mm = Mymeet()
        self.mm.install(self.ms)
        self.books = {}
        self.gcal_reason = dict(gcal_reason or {})
        self.gcal_calls = []
        enc = _crypto.encrypt_token("tok-plain")
        for tid, tzname in users:
            self.db.upsert_user(tid, f"u{tid}")
            conn = __import__("sqlite3").connect(self.db.DB_PATH)
            conn.execute("UPDATE users SET mymeet_token=?, mymeet_setup_done=1, "
                         "mymeet_spreadsheet_id=?, mymeet_full_name=?, timezone=? "
                         "WHERE telegram_id=?", (enc, f"sheet-{tid}", ME, tzname, tid))
            conn.commit()
            conn.close()
            self.books[f"sheet-{tid}"] = Book()
        gc = NS(open_by_key=lambda key: self.books[key].ss)
        self.ms._gs_client = lambda: gc
        mt = self.mt
        dbm = self.db
        my_tasks_ns = NS(MyTasksBuilder=lambda tid, name, tz_name=None:
                         mt.MyTasksBuilder(tid, name, tz_name=tz_name, dbm=dbm))

        def _gcal_checked(tid, days):
            self.gcal_calls.append((tid, days))
            r = self.gcal_reason.get(tid)
            return (None, r) if r else (self.mm.gcal(), None)

        async def _sync_async(days=30, append=False, user_config=None, stats=None):
            return self.ms.sync_to_sheets(days=days, append=append,
                                          user_config=user_config, stats=stats)
        self.ns = _load_main(_MAIN_FUNCS, extra_ns={
            "asyncio": asyncio, "db": self.db, "mymeet_sync": self.ms,
            "my_tasks": my_tasks_ns, "_crypto": _crypto,
            "CryptoDecryptError": CryptoDecryptError, "logger": _quiet_logger(),
            "_get_gcal_organizer_data_checked": _gcal_checked,
            "sync_to_sheets_async": _sync_async, "datetime": datetime,
            "timezone": timezone, "pytz": NS(timezone=ZoneInfo), "DEFAULT_TZ": "Europe/Moscow",
            "time_module": time, "Bot": object, "_my_tasks_locks": {},
        }, assigns=_MAIN_ASSIGNS)

    def book(self, tid=1):
        return self.books[f"sheet-{tid}"]

    def tick(self, bot=None):
        bot = bot or Bot()
        asyncio.run(self.ns["scheduled_mymeet_sync"](bot))
        return bot

    def force_due(self, tid=1):
        """Сдвигаем состояние так, будто вечер ещё не импортирован."""
        self.db.mymeet_sync_save(tid, last_success_slot="2000-01-01", next_attempt_ts=0,
                                 attempt_slot=None, slot_attempts=0)

    def my_tasks_sheet(self, tid=1):
        return self.book(tid).data.get(f"Мои задачи — {ME}", [])


def _now():
    return datetime.now(timezone.utc)


# ── 1. Расписание: слот, часовые поясы, переход даты ─────────────────────────

def test_slot_and_due_daily():
    ms = _ms()
    d = datetime(2026, 11, 16, 18, 59, tzinfo=MSK)
    assert ms.auto_slot(d) == "2026-11-15"
    assert ms.auto_slot(datetime(2026, 11, 16, 19, 0, tzinfo=MSK)) == "2026-11-16"
    assert ms.auto_slot(datetime(2026, 11, 17, 0, 30, tzinfo=MSK)) == "2026-11-16"
    st = {"last_success_slot": "2026-11-15"}
    ts = d.timestamp()
    assert ms.auto_due(st, d, ts) is None                       # вчера уже было
    d19 = datetime(2026, 11, 16, 19, 0, tzinfo=MSK)
    assert ms.auto_due(st, d19, d19.timestamp()) == "2026-11-16"  # ежедневный запуск
    st2 = {"last_success_slot": "2026-11-16"}
    assert ms.auto_due(st2, d19, d19.timestamp()) is None       # повтор не нужен
    # После полуночи вчерашний неуспешный вечер ещё догоняется
    late = datetime(2026, 11, 17, 1, 0, tzinfo=MSK)
    assert ms.auto_due(st, late, late.timestamp()) == "2026-11-16"


def test_timezones_per_user():
    ms = _ms()
    now = datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc)   # 19:00 MSK, 08:00 NY
    st = {"last_success_slot": "2026-11-15"}
    assert ms.auto_due(st, now.astimezone(MSK), now.timestamp()) == "2026-11-16"
    ny = ZoneInfo("America/New_York")
    assert ms.auto_due(st, now.astimezone(ny), now.timestamp()) is None
    vl = ZoneInfo("Asia/Vladivostok")                       # 02:00 следующего дня
    assert ms.auto_due({"last_success_slot": "2026-11-16"}, now.astimezone(vl),
                       now.timestamp()) is None


def test_missed_evening_and_first_run():
    ms = _ms()
    morning = datetime(2026, 11, 18, 9, 0, tzinfo=MSK)
    st = {"last_success_slot": "2026-11-15",
          "last_success_ts": datetime(2026, 11, 15, 19, 0, tzinfo=MSK).timestamp()}
    assert ms.auto_due(st, morning, morning.timestamp()) == "2026-11-17"
    days, trunc = ms.auto_window_days(st, morning.timestamp())
    assert days == 5 and not trunc, days                      # 2.6 дня → 3 + запас 2
    assert ms.auto_due(None, morning, morning.timestamp()) == "2026-11-17"  # без истории
    assert ms.auto_window_days(None, morning.timestamp()) == (ms.AUTO_INITIAL_DAYS, False)
    # Обычный вечер: окно не меньше 3 дней (задержанные протоколы)
    ts = time.time()
    assert ms.auto_window_days({"last_success_ts": ts - 86400}, ts) == (3, False)
    # Долгий простой: окно ограничено
    days, trunc = ms.auto_window_days({"last_success_ts": ts - 40 * 86400}, ts)
    assert days == ms.AUTO_MAX_DAYS and trunc


def test_backoff_attempts_and_notifications():
    ms = _ms()
    t0 = 1_000_000.0
    st = {"last_success_slot": "2026-11-15", "last_success_ts": t0 - 86400}
    sent = []
    for i in range(ms.AUTO_MAX_SLOT_ATTEMPTS):
        f, notify = ms.auto_record(st, "2026-11-16", ms.AUTO_TRANSIENT, "mymeet_unavailable",
                                   t0 + i, t0)
        st = {**st, **f}
        sent.append(notify)
        assert "last_success_slot" not in f or f["last_success_slot"] == "2026-11-15"
    assert sent == [False, False, True, False, False, False], sent   # без спама
    assert st["last_success_ts"] == t0 - 86400                       # окно расширится
    assert st["next_attempt_ts"] == t0 + 5 + 120 * 60
    d = datetime(2026, 11, 16, 23, 0, tzinfo=MSK)
    assert ms.auto_due(st, d, st["next_attempt_ts"] + 1) is None     # лимит попыток
    # Следующий вечер — снова пробуем
    d2 = datetime(2026, 11, 17, 19, 0, tzinfo=MSK)
    assert ms.auto_due(st, d2, st["next_attempt_ts"] + 1) == "2026-11-17"
    # Настройка — сразу, но та же причина не чаще раза в 3 дня
    f, n1 = ms.auto_record({}, "s1", ms.AUTO_CONFIG, "mymeet_token", t0, t0)
    st2 = {**f}
    f, n2 = ms.auto_record(st2, "s2", ms.AUTO_CONFIG, "mymeet_token", t0 + 86400, t0)
    f, n3 = ms.auto_record({**st2, **f}, "s3", ms.AUTO_CONFIG, "mymeet_token",
                           t0 + 4 * 86400, t0)
    assert (n1, n2, n3) == (True, False, True)
    # Успех сбрасывает признак уведомления и двигает окно
    f, n = ms.auto_record(st2, "s4", ms.AUTO_OK, None, t0 + 10, t0 + 5)
    assert not n and f["last_success_ts"] == t0 + 5 and f["notified_reason"] is None


def test_classify_errors():
    ms = _ms()
    resp = NS(status_code=401)
    assert ms.classify_sync_error(requests.HTTPError(response=resp)) == ("config", "mymeet_token")
    assert ms.classify_sync_error(requests.HTTPError(response=NS(status_code=503)))[0] == "transient"
    assert ms.classify_sync_error(requests.ConnectionError()) == ("transient", "mymeet_unavailable")
    assert ms.classify_sync_error(ms.ImportVolumeError(500, 300)) == ("volume", "volume")
    assert ms.classify_sync_error(gspread.SpreadsheetNotFound()) == ("config", "sheet_access")
    assert ms.classify_sync_error(KeyError("x")) == ("transient", "internal")


# ── 2. Сквозные сценарии: планировщик → импорт → таблица ─────────────────────

def test_daily_run_imports_new_tasks_into_my_tasks():
    """Расшифровка токена, Calendar-организатор, задачи в протоколе и в
    «Мои задачи» со статусом «Требует верификации» (даже если в протоколе done)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        env.mm.add("m1", "Стратсессия", _now() - timedelta(hours=3),
                   [("Отчёт", ME, True), ("Бюджет", "Анна Смирнова", False)])
        env.force_due()
        bot = env.tick()
        assert not bot.sent, bot.sent
        st = env.db.mymeet_sync_get(1)
        assert st["last_outcome"] == "ok" and st["last_success_slot"] != "2000-01-01", st
        # проверка организатора — за то же окно (первый запуск: 7 дней)
        assert env.gcal_calls == [(1, env.ms.AUTO_INITIAL_DAYS)], env.gcal_calls
        rows = env.book().task_rows()
        assert sorted(r[1] for r in rows) == ["Бюджет", "Отчёт"], rows
        mt = env.my_tasks_sheet()
        mine = [r for r in mt[1:] if "Отчёт" in r]
        assert mine and UNVERIFIED in mine[0], mt
        assert not any("Бюджет" in r for r in mt[1:]), mt     # чужая задача не попала


def test_repeat_run_no_duplicates_and_manual_status_kept():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        env.mm.add("m1", "Стратсессия", _now() - timedelta(hours=3), [("Отчёт", ME, False)])
        env.force_due()
        env.tick()
        # Ручная правка статуса в протоколе
        b = env.book()
        title = next(t for t in b.data if t == env.ms.ONE_TIME_SHEET_NAME)
        for r in b.data[title]:
            if len(r) > 2 and r[2] == "Отчёт":
                r[5] = "В работе"
        for _ in range(3):
            env.force_due()
            env.tick()
        rows = b.task_rows()
        assert rows == [(title, "Отчёт", ME, "В работе")], rows
        # Повтор вне слота ничего не делает
        calls = env.mm.calls
        env.tick()
        assert env.mm.calls == calls


def test_delayed_protocol_caught_next_evening():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        env.mm.add("m1", "Планёрка-1", _now() - timedelta(hours=30), None)   # ещё 404
        env.force_due()
        env.tick()
        assert env.book().task_rows() == []
        st = env.db.mymeet_sync_get(1)
        assert st["last_outcome"] == "ok"                     # 404 — не ошибка
        # На следующий вечер протокол появился; встреча старше суток
        env.mm.meetings["m1"] = ("Планёрка-1", _now() - timedelta(hours=30),
                                 [("Смета", ME, False)])
        env.db.mymeet_sync_save(1, last_success_slot="2000-01-01",
                                last_success_ts=time.time() - 86400)
        env.tick()
        assert [r[1] for r in env.book().task_rows()] == ["Смета"]


def test_mymeet_error_then_recovery():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        env.mm.add("m1", "Стратсессия", _now() - timedelta(hours=3), [("Отчёт", ME, False)])
        env.mm.fail = requests.ConnectionError("down")
        env.force_due()
        bot = Bot()
        for _ in range(3):
            env.db.mymeet_sync_save(1, next_attempt_ts=0)
            env.tick(bot)
        st = env.db.mymeet_sync_get(1)
        assert st["last_outcome"] == "transient" and st["fail_count"] == 3, st
        assert st["last_success_slot"] == "2000-01-01"
        assert len(bot.sent) == 1 and "временно недоступен" in bot.sent[0][1], bot.sent
        assert "down" not in bot.sent[0][1]                    # без деталей исключения
        assert env.book().writes == []                         # таблица не тронута
        env.mm.fail = None
        env.db.mymeet_sync_save(1, next_attempt_ts=0)
        env.tick(bot)
        assert env.db.mymeet_sync_get(1)["last_outcome"] == "ok"
        assert [r[1] for r in env.book().task_rows()] == ["Отчёт"]


def test_calendar_unavailable_no_writes():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp, gcal_reason={1: "token_invalid"})
        env.mm.add("m1", "Стратсессия", _now() - timedelta(hours=3), [("Отчёт", ME, False)])
        env.force_due()
        bot = env.tick()
        st = env.db.mymeet_sync_get(1)
        assert (st["last_outcome"], st["last_reason"]) == ("config", "google_token_invalid")
        assert env.mm.calls == 0 and env.book().writes == []
        assert len(bot.sent) == 1 and "/connect" in bot.sent[0][1]


def test_protocol_error_is_partial_and_retried():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        env.mm.add("m1", "Стратсессия", _now() - timedelta(hours=3), [("Отчёт", ME, False)])
        env.mm.add("m2", "Совет", _now() - timedelta(hours=2), [("План", ME, False)])
        env.mm.proto_fail = {"m2"}
        env.force_due()
        env.tick()
        st = env.db.mymeet_sync_get(1)
        assert (st["last_outcome"], st["last_reason"]) == ("partial", "protocols"), st
        assert st["last_success_slot"] == "2000-01-01"
        assert [r[1] for r in env.book().task_rows()] == ["Отчёт"]
        env.mm.proto_fail = set()
        env.db.mymeet_sync_save(1, next_attempt_ts=0)
        env.tick()
        assert sorted(r[1] for r in env.book().task_rows()) == ["Отчёт", "План"]
        assert env.db.mymeet_sync_get(1)["last_outcome"] == "ok"


def test_multiple_users_isolated():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp, users=((1, "Europe/Moscow"), (2, "Europe/Moscow")),
                  gcal_reason={1: "transient"})
        env.mm.add("m1", "Стратсессия", _now() - timedelta(hours=3), [("Отчёт", ME, False)])
        env.force_due(1)
        env.force_due(2)
        env.tick()
        assert env.db.mymeet_sync_get(1)["last_outcome"] == "transient"
        assert env.db.mymeet_sync_get(2)["last_outcome"] == "ok"
        assert env.book(1).writes == [] and env.book(2).task_rows()


def test_undecryptable_token_reported():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        import sqlite3
        conn = sqlite3.connect(env.db.DB_PATH)
        conn.execute("UPDATE users SET mymeet_token='plain-legacy' WHERE telegram_id=1")
        conn.commit()
        conn.close()
        env.force_due()
        bot = env.tick()
        st = env.db.mymeet_sync_get(1)
        assert st["last_reason"] == "token_undecryptable", st
        assert bot.sent and "/setup_mymeet" in bot.sent[0][1]


def test_deleted_or_moved_row_not_resurrected():
    """Удалённую вручную строку не возвращаем; смена листа (разовая → регулярная
    при более широком окне) не создаёт дубль."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        env.mm.add("m1", "Планёрка", _now() - timedelta(hours=3), [("Отчёт", ME, False)])
        env.force_due()
        env.tick()
        b = env.book()
        assert [r[1] for r in b.task_rows()] == ["Отчёт"]
        # Вторая «Планёрка» в окне → встречи становятся регулярными (свой лист)
        env.mm.add("m0", "Планёрка", _now() - timedelta(days=2), [("Старое", ME, False)])
        env.force_due()
        env.tick()
        tasks = sorted(r[1] for r in b.task_rows())
        assert tasks == ["Отчёт", "Старое"], b.task_rows()
        # Пользователь удалил строку «Старое» — повтор её не воскрешает
        for t in b.data:
            b.data[t] = [r for r in b.data[t] if not (len(r) > 2 and r[2] == "Старое")]
        env.force_due()
        env.tick()
        assert sorted(r[1] for r in b.task_rows()) == ["Отчёт"], b.task_rows()


def test_existing_rows_without_registry_not_duplicated():
    """Строки, импортированные до #39 (реестра ещё нет), не дублируются."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        start = _now() - timedelta(days=2)
        env.mm.add("m1", "Совет", start, [("Отчёт", ME, False)])
        date = env.ms._fmt_date(_iso(start))
        env.books["sheet-1"] = Book({"Старый лист": [
            PROTO_HDR, ["1", date, "Отчёт", ME, "", "Выполнено", "m1"]]})
        env.force_due()
        env.tick()
        assert env.book().task_rows() == [("Старый лист", "Отчёт", ME, "Выполнено")]


def test_volume_guard_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        env.ms.AUTO_MAX_NEW_ROWS = 2
        env.mm.add("m1", "Стратсессия", _now() - timedelta(hours=3),
                   [(f"Задача {i}", ME, False) for i in range(3)])
        env.force_due()
        bot = env.tick()
        st = env.db.mymeet_sync_get(1)
        assert (st["last_outcome"], st["last_reason"]) == ("volume", "volume"), st
        assert env.book().writes == [] and env.book().data == {}
        assert bot.sent and "администратору" in bot.sent[0][1]


def test_dedup_read_failure_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        env.books["sheet-1"] = Book({"Лист": [PROTO_HDR]})
        env.book().fail_reads = True
        env.mm.add("m1", "Стратсессия", _now() - timedelta(hours=3), [("Отчёт", ME, False)])
        env.force_due()
        env.tick()
        assert env.db.mymeet_sync_get(1)["last_outcome"] == "transient"
        assert env.book().writes == []


def test_parallel_jobs_serialized_by_user_lock():
    """Автоимпорт и сверка «Мои задачи» одного пользователя не пересекаются."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        env.mm.add("m1", "Стратсессия", _now() - timedelta(hours=3), [("Отчёт", ME, False)])
        env.force_due()
        events = []
        real = env.ns["sync_to_sheets_async"]

        async def _slow_sync(**kw):
            events.append("import-start")
            await asyncio.sleep(0.05)
            r = await real(**kw)
            events.append("import-end")
            return r
        env.ns["sync_to_sheets_async"] = _slow_sync

        async def _other_job():
            await asyncio.sleep(0.01)
            async with env.ns["_user_lock"](1):
                events.append("my_tasks")

        async def _both():
            await asyncio.gather(env.ns["scheduled_mymeet_sync"](Bot()), _other_job())
        asyncio.run(_both())
        assert events == ["import-start", "import-end", "my_tasks"], events


def test_scheduler_registration():
    """Job — каждые 15 мин, без наложения; старый фиксированный 16:00 UTC убран."""
    src = (_ts.REPO_ROOT / "main.py").read_text(encoding="utf-8")
    i = src.index("scheduled_mymeet_sync,\n")
    block = src[i:i + 400]
    assert 'minute="*/15"' in block and "max_instances=1" in block, block
    assert "hour=16" not in block
    assert "get_mymeet_users)" not in src     # двойная расшифровка (#39)


def test_logs_have_no_sheet_title_or_names():
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(tmp)
        env.mm.add("m1", "Секретная встреча", _now() - timedelta(hours=3),
                   [("Секретная задача", ME, False)])
        env.force_due()
        records = []

        class H(logging.Handler):
            def emit(self, r):
                records.append(r.getMessage())
        h = H()
        for lg in (env.ns["logger"], logging.getLogger(env.ms.__name__), logging.getLogger()):
            lg.addHandler(h)
        old = logging.getLogger().level
        logging.getLogger().setLevel(logging.INFO)
        try:
            env.tick()
        finally:
            logging.getLogger().setLevel(old)
            for lg in (env.ns["logger"], logging.getLogger(env.ms.__name__),
                       logging.getLogger()):
                lg.removeHandler(h)
        text = "\n".join(records)
        assert "MYmeet auto [1]" in text and "outcome=ok" in text, text
        for secret in ("Секретная", "Иван", "Петров", "tok-plain"):
            assert secret not in text, (secret, text)


TESTS = [
    (test_slot_and_due_daily, "MYmeet авто: слот 19:00, ежедневный запуск, переход даты"),
    (test_timezones_per_user, "MYmeet авто: часовые поясы пользователей"),
    (test_missed_evening_and_first_run, "MYmeet авто: пропущенный вечер, первый запуск, окно"),
    (test_backoff_attempts_and_notifications, "MYmeet авто: повторы, лимит попыток, без спама"),
    (test_classify_errors, "MYmeet авто: классификация ошибок"),
    (test_daily_run_imports_new_tasks_into_my_tasks,
     "MYmeet авто: импорт → протокол и «Мои задачи» (Требует верификации)"),
    (test_repeat_run_no_duplicates_and_manual_status_kept,
     "MYmeet авто: повтор без дублей, ручной статус сохранён"),
    (test_delayed_protocol_caught_next_evening, "MYmeet авто: задержанный протокол догоняется"),
    (test_mymeet_error_then_recovery, "MYmeet авто: ошибка MYmeet → повтор → успех"),
    (test_calendar_unavailable_no_writes, "MYmeet авто: Calendar недоступен — без записи"),
    (test_protocol_error_is_partial_and_retried, "MYmeet авто: сбой протокола → partial → догон"),
    (test_multiple_users_isolated, "MYmeet авто: несколько пользователей независимы"),
    (test_undecryptable_token_reported, "MYmeet авто: нечитаемый токен → уведомление"),
    (test_deleted_or_moved_row_not_resurrected,
     "MYmeet авто: удалённая/перенесённая строка не возвращается"),
    (test_existing_rows_without_registry_not_duplicated,
     "MYmeet авто: старые строки (до реестра) не дублируются"),
    (test_volume_guard_writes_nothing, "MYmeet авто: лимит объёма — ничего не записано"),
    (test_dedup_read_failure_writes_nothing, "MYmeet авто: нет чтения листов — нет записи"),
    (test_parallel_jobs_serialized_by_user_lock, "MYmeet авто: параллельные задания по очереди"),
    (test_scheduler_registration, "MYmeet авто: регистрация job каждые 15 мин"),
    (test_logs_have_no_sheet_title_or_names, "MYmeet авто: в логах нет ФИО/текста/токена"),
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
