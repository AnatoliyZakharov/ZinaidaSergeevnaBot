"""
Тесты «Мои задачи»: Google Tasks ↔ лист, пересланные сообщения, распознавание
исполнителей в протоколах.  Без сети: Google Sheets / Tasks — фейки (никаких
реальных удалений), БД — временная.
Запуск: python test_my_tasks.py  (также входит в python test_smoke.py)
"""
import ast
import asyncio
import importlib.util
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import types
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS

REPO_ROOT = pathlib.Path(__file__).parent
IN_CI = bool(os.environ.get("CI"))
# Страховка: реальный модуль db (если где-то импортируется) не трогает рабочую БД
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="mytasks_test_"), "never.db")

import test_smoke as _ts  # noqa: E402  (helpers: _load_mymeet, _load_db, _FakeBook …)

# Ключ шифрования для тестов (реальный ENCRYPTION_KEY не используется)
from cryptography.fernet import Fernet  # noqa: E402
_TEST_KEY = Fernet.generate_key().decode()
os.environ["ENCRYPTION_KEY"] = _TEST_KEY
import crypto as _crypto  # noqa: E402
_crypto._fernet = None

PROTO_HDR = _ts._PROTO_HDR
ME = "Иван Петров"
MY_TITLE = f"Мои задачи — {ME}"
PRE_PR_COMMIT = "5fb90f9"     # main до PR #25 (схема БД production)


def _load_my_tasks():
    spec = importlib.util.spec_from_file_location("my_tasks_test", REPO_ROOT / "my_tasks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_ms():
    """mymeet_sync без лимитера записей (тесты делают много прогонов подряд)."""
    ms = _ts._load_mymeet()
    ms._throttle = lambda: None
    return ms


def _tmp_db(tmp):
    db = _ts._load_db(tmp)
    db.init_db()
    return db


def _user(db, sheet=True, name=ME):
    db.upsert_user(1, "u")
    if name:
        db.set_full_name(1, name)
    if sheet:
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("UPDATE users SET mymeet_spreadsheet_id='S' WHERE telegram_id=1")
        conn.commit()
        conn.close()


# ══ 1. Распознавание исполнителя ═════════════════════════════════════════

def test_match_assignee_reliable_only():
    mt = _load_my_tasks()
    m = mt.match_assignee
    assert m("Иван Петров", ME) == "exact"
    assert m("  иван   ПЕТРОВ ", ME) == "exact"
    # перестановки, падежи, фамилия + инициалы
    for a in ("Петров Иван", "Петрову Ивану", "Петрова Ивана", "Петровым Иваном",
              "Ивану Петрову", "Петров И.", "И. Петров", "Петров И"):
        assert m(a, ME) == "fuzzy", a
    full3 = "Иван Сергеевич Петров"
    for a in ("Петров И.С.", "И.С. Петров", "Петров ИС", "Петров Иван Сергеевич",
              "Петрову Ивану Сергеевичу", "Иван Петров", "Петров И."):
        assert m(a, full3) == "fuzzy", a
    for a in ("Анна Смирнова", "Смирнова А.", "Анне Смирновой", "Анной Смирновой"):
        assert m(a, "Анна Смирнова") is not None, a
    assert m("Семёнову Петру", "Пётр Семёнов") == "fuzzy"
    assert m("Сидоров А., Петров И.", ME) == "fuzzy"            # несколько исполнителей
    # недостоверно → нет
    negatives = (
        "Петров",            # одна фамилия — однофамильцы
        "Иван",              # одно имя
        "Ивану",
        "Петров А.",         # другое имя
        "Иван П.",           # фамилия инициалом
        "Петрова И.",        # может быть Ирина Петрова
        "Ирина Петрова",     # однофамилица
        "Иван Сидоров",      # тёзка
        "Иванов", "Петренко", "", "П.", "И. П.",
        "Петров Иван Сергеевич Лишний",
        "Speaker A", "Speaker B", "speaker 1", "Спикер 2", "Участник C",
        "Speaker A, Speaker B",
    )
    for a in negatives:
        assert m(a, ME) is None, a
    assert m("Иван Сергеевич", full3) is None              # без фамилии
    assert m("Петров С.", full3) is None                    # инициал отчества вместо имени
    assert m("Смирновой А.", "Анна Смирнова") is None       # падеж + инициал — неоднозначно
    assert m("Петров", "") is None
    # ФИО из одного слова — только точное совпадение
    assert m("Петров", "Петров") == "exact" and m("Петрову", "Петров") is None


def test_user_name_parts():
    mt = _load_my_tasks()
    p = mt.user_name_parts
    assert p("Иван Петров") == {"surname": "петров", "first": "иван", "patronymic": None}
    assert p("Петров Иван")["surname"] == "петров"
    assert p("Петров Иван Сергеевич") == {"surname": "петров", "first": "иван",
                                         "patronymic": "сергеевич"}
    assert p("Иван Сергеевич Петров")["surname"] == "петров"
    assert p("Иван Сергеевич") is None and p("Петров") is None


# ══ 2. Срок из текста ════════════════════════════════════════════════════

def test_parse_deadline_unambiguous_only():
    mt = _load_my_tasks()
    base = date(2026, 11, 11)          # среда
    p = lambda t: mt.fmt_date(mt.parse_deadline(t, base))   # noqa: E731
    cases = {
        "Сделай завтра": "12.11.2026",
        "послезавтра пришли отчёт": "13.11.2026",
        "сегодня до вечера": "11.11.2026",
        "через 3 дня": "14.11.2026",
        "через два дня": "13.11.2026",
        "через пару дней": "13.11.2026",
        "через неделю": "18.11.2026",
        "через 2 недели": "25.11.2026",
        "через месяц": "11.12.2026",
        "к пятнице": "13.11.2026",
        "до пятницы": "13.11.2026",
        "в понедельник созвон": "16.11.2026",
        "до среды": "18.11.2026",
        "до 15.11": "15.11.2026",
        "к 05.01": "05.01.2027",
        "срок 20.12.2026": "20.12.2026",
        "к 20 ноября": "20.11.2026",
        "до 3 марта 2027 года": "03.03.2027",
        "до конца недели": "13.11.2026",
        "до конца месяца": "30.11.2026",
        # одна и та же дата разными словами — однозначно
        "завтра, т.е. к 12.11": "12.11.2026",
        "к пятнице (через 2 дня)": "13.11.2026",
        # неоднозначно — не угадываем
        "завтра или через неделю": "",
        "к пятнице, крайний срок — в понедельник": "",
        "до 15.11, а лучше до 13.11": "",
        # нет срока
        "Нужно позавтракать": "",
        "встреча прошла 15.11": "",
        "до 31.02": "",
        "": "",
    }
    for text, want in cases.items():
        assert p(text) == want, (text, p(text), want)
    assert mt.parse_deadline("к пятнице", date(2026, 11, 13)) == date(2026, 11, 20)


# ══ 3. Пересланные сообщения ═════════════════════════════════════════════

def _origin_user(dt, uid=7, name="Мария Иванова", username="mivanova"):
    return NS(type="user", date=dt, sender_user=NS(id=uid, full_name=name, username=username))


def _msg(origin, text=None, caption=None, media_group_id=None, message_id=1, **kw):
    return NS(forward_origin=origin, text=text, caption=caption, chat=NS(id=1),
              media_group_id=media_group_id, message_id=message_id,
              date=datetime(2026, 11, 12, 9, tzinfo=timezone.utc), **kw)


def test_forward_info_user_origin_and_deadline():
    mt = _load_my_tasks()
    odt = datetime(2026, 11, 9, 21, 30, tzinfo=timezone.utc)    # 10.11 00:30 МСК (вт)
    info = mt.forward_info(_msg(_origin_user(odt), text="Подготовь КП к пятнице"),
                           "Europe/Moscow")
    assert info["assigner"] == "Мария Иванова (@mivanova)"
    assert info["task_date"] == "10.11.2026", info        # дата оригинала, пояс пользователя
    assert info["deadline"] == "13.11.2026", info         # от даты оригинала, не пересылки
    # в UTC это ещё 09.11 → пятница та же, дата задачи другая
    assert mt.forward_info(_msg(_origin_user(odt), text="x"), "UTC")["task_date"] == "09.11.2026"
    again = mt.forward_info(_msg(_origin_user(odt), text="Подготовь КП к пятнице",
                                 message_id=99), "Europe/Moscow")
    assert again["source_key"] == info["source_key"]       # повторная пересылка — тот же ключ
    other = mt.forward_info(_msg(_origin_user(odt), text="Другое"), "Europe/Moscow")
    assert other["source_key"] != info["source_key"]
    later = mt.forward_info(_msg(_origin_user(odt + timedelta(seconds=5)),
                                 text="Подготовь КП к пятнице"), "Europe/Moscow")
    assert later["source_key"] != info["source_key"]       # тот же текст позже — другое сообщение


def test_forward_info_other_origins_and_dedup_safety():
    mt = _load_my_tasks()
    dt = datetime(2026, 11, 12, 6, tzinfo=timezone.utc)
    hidden = mt.forward_info(_msg(NS(type="hidden_user", date=dt, sender_user_name="Аноним"),
                                  text="завтра позвонить"), "Europe/Moscow")
    assert hidden["assigner"] == "Аноним" and hidden["deadline"] == "13.11.2026"
    ch = NS(type="channel", date=dt, chat=NS(id=-100, title="Новости"), message_id=55,
            author_signature="Редактор")
    c1 = mt.forward_info(_msg(ch, caption="Фото с подписью"), "Europe/Moscow")
    c2 = mt.forward_info(_msg(ch, caption="Фото (изменено)"), "Europe/Moscow")
    assert c1["assigner"] == "Новости (Редактор)" and c1["source_key"] == c2["source_key"]
    grp = NS(type="chat", date=dt, sender_chat=NS(id=-5, title="Команда"), author_signature=None)
    assert mt.forward_info(_msg(grp, text="x"), "UTC")["assigner"] == "Команда"
    # голос/медиа без текста: задача создаётся; разные файлы — разные ключи
    v1 = mt.forward_info(_msg(_origin_user(dt), voice=NS(file_unique_id="A")), "UTC")
    v2 = mt.forward_info(_msg(_origin_user(dt), voice=NS(file_unique_id="B")), "UTC")
    assert v1["title"] == "Пересланное сообщение (voice) от Мария Иванова (@mivanova)"
    assert v1["source_key"] != v2["source_key"]
    p1 = mt.forward_info(_msg(_origin_user(dt), photo=[NS(file_unique_id="s"),
                                                       NS(file_unique_id="P1")]), "UTC")
    p2 = mt.forward_info(_msg(_origin_user(dt), photo=[NS(file_unique_id="s"),
                                                       NS(file_unique_id="P2")]), "UTC")
    assert p1["source_key"] != p2["source_key"]
    # альбом: все части → один ключ, заглушка у частей без подписи
    a1 = mt.forward_info(_msg(_origin_user(dt), media_group_id="g", photo=[NS(file_unique_id="1")]),
                         "UTC")
    a2 = mt.forward_info(_msg(_origin_user(dt), media_group_id="g", caption="Смета",
                              photo=[NS(file_unique_id="2")]), "UTC")
    assert a1["source_key"] == a2["source_key"] and a1["placeholder"] and not a2["placeholder"]
    # без даты оригинала — ключ уникален на каждую пересылку (не склеиваем)
    nd = NS(type="hidden_user", date=None, sender_user_name="X")
    k1 = mt.forward_info(_msg(nd, text="одно и то же", message_id=10), "UTC")["source_key"]
    k2 = mt.forward_info(_msg(nd, text="одно и то же", message_id=11), "UTC")["source_key"]
    assert k1 != k2
    assert mt.forward_info(NS(forward_origin=None, text="x"), "UTC") is None
    long = mt.forward_info(_msg(_origin_user(dt), text="я" * 5000), "UTC")
    assert len(long["title"]) == mt.TITLE_MAX_LEN


def _ptb():
    try:
        import telegram  # noqa: F401
        return True
    except ImportError:
        if IN_CI:
            raise AssertionError("python-telegram-bot не установлен в CI — тест обязателен")
        print("    (python-telegram-bot не установлен — локально пропущено)")
        return False


def test_forward_info_real_ptb_objects():
    """Реальные объекты python-telegram-bot (в CI обязательно)."""
    if not _ptb():
        return
    from telegram import (Chat, Message, MessageOriginChannel, MessageOriginHiddenUser,
                          MessageOriginUser, User, Voice)
    mt = _load_my_tasks()
    dt = datetime(2026, 11, 12, 6, tzinfo=timezone.utc)
    chat = Chat(id=1, type="private")
    u = User(id=9, first_name="Олег", last_name="Сидоров", is_bot=False, username="osid")
    for origin, who in ((MessageOriginUser(date=dt, sender_user=u), "Олег Сидоров (@osid)"),
                        (MessageOriginHiddenUser(date=dt, sender_user_name="Скрыт"), "Скрыт"),
                        (MessageOriginChannel(date=dt, chat=Chat(id=-1, type="channel",
                                                                 title="Канал"),
                                              message_id=3), "Канал")):
        msg = Message(message_id=1, date=dt, chat=chat, text="через 2 дня отчёт",
                      forward_origin=origin)
        info = mt.forward_info(msg, "Europe/Moscow")
        assert info["assigner"] == who, (info, who)
        assert info["deadline"] == "14.11.2026", info
    voice = Message(message_id=2, date=dt, chat=chat, forward_origin=MessageOriginUser(
        date=dt, sender_user=u), voice=Voice(file_id="f", file_unique_id="uv", duration=3))
    vi = mt.forward_info(voice, "UTC")
    assert vi["media"] == "voice" and "voice" in vi["title"]


def test_forward_handler_filter_and_order():
    """Фильтр хендлера ловит пересылки (текст/голос) в личке и не ловит обычные
    сообщения; хендлер пересылок зарегистрирован раньше текста/голоса."""
    if not _ptb():
        return
    from telegram import Chat, Message, MessageOriginUser, Update, User, Voice
    from telegram.ext import filters
    flt = filters.FORWARDED & filters.ChatType.PRIVATE & ~filters.COMMAND
    dt = datetime(2026, 11, 12, 6, tzinfo=timezone.utc)
    u = User(id=9, first_name="О", is_bot=False)
    priv, grp = Chat(id=1, type="private"), Chat(id=-2, type="group")
    org = MessageOriginUser(date=dt, sender_user=u)

    def upd(**kw):
        return Update(update_id=1, message=Message(message_id=1, date=dt, **kw))
    assert flt.check_update(upd(chat=priv, text="задача", forward_origin=org))
    assert flt.check_update(upd(chat=priv, forward_origin=org,
                                voice=Voice(file_id="f", file_unique_id="u", duration=1)))
    assert not flt.check_update(upd(chat=priv, text="обычный текст"))
    assert not flt.check_update(upd(chat=grp, text="задача", forward_origin=org))
    # порядок регистрации в main.py
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    i_fwd = src.index("handle_forwarded))")
    assert i_fwd < src.index("MessageHandler(filters.VOICE, handle_voice)")
    assert i_fwd < src.index("filters.TEXT & ~filters.COMMAND, handle_text")
    # в коде хендлера нет перенаправления пересылки в мастер MYmeet
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "handle_forwarded")
    called = {c.func.id for c in ast.walk(fn) if isinstance(c, ast.Call)
              and isinstance(c.func, ast.Name)}
    assert "handle_text" not in called and "_handle_mymeet_setup" not in called


def _load_main_async(names, ns_extra, assigns=()):
    import warnings
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(src)
    nodes = [n for n in tree.body
             if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names)
             or (isinstance(n, (ast.Assign, ast.AnnAssign)) and any(
                 isinstance(t, ast.Name) and t.id in assigns
                 for t in (n.targets if isinstance(n, ast.Assign) else [n.target])))]
    assert {n.name for n in nodes if hasattr(n, "name")} == set(names)
    import typing
    ns = {"asyncio": asyncio, "Optional": typing.Optional, "datetime": datetime,
          "timezone": timezone, "Update": object, "Bot": object,
          "ContextTypes": NS(DEFAULT_TYPE=object)}
    ns.update(ns_extra)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), ns)
    return ns


def test_forward_during_mymeet_wizard_creates_task():
    """Пересылка во время мастера MYmeet → задача; мастер её не получает."""
    mt = _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db, sheet=False)
        db.set_mymeet_setup_in_progress(1, True)
        replies, wizard_calls, refresh = [], [], []

        async def handle_text(*a, **k):
            wizard_calls.append(a)
        mt_bound = NS(forward_info=mt.forward_info,
                      add_forwarded=lambda tid, info: mt.add_forwarded(tid, info, dbm=db))
        ns = _load_main_async(["handle_forwarded"], {
            "db": db, "my_tasks": mt_bound, "DEFAULT_TZ": "Europe/Moscow", "logger": NS(
                error=lambda *a, **k: None), "handle_text": handle_text,
            "_user_full_name": lambda u: (u["mymeet_full_name"] or ""),
            "_schedule_my_tasks_refresh": refresh.append})

        async def reply(t):
            replies.append(t)
        dt = datetime(2026, 11, 12, 6, tzinfo=timezone.utc)
        upd = NS(effective_user=NS(id=1, username="u"),
                 message=_msg(_origin_user(dt), text="Счёт оплатить завтра", reply_text=reply))
        asyncio.run(ns["handle_forwarded"](upd, None))
        asyncio.run(ns["handle_forwarded"](upd, None))            # повторная пересылка
        assert not wizard_calls
        recs = db.my_tasks_load(1)
        assert len(recs) == 1 and recs[0]["title"] == "Счёт оплатить завтра"
        assert recs[0]["deadline"] == "13.11.2026"
        assert "Добавлено" in replies[0] and "настройка MYmeet" in replies[0]
        assert replies[1].startswith("♻️ Уже")


# ══ 4. БД: схема, миграции, шифрование ═══════════════════════════════════

def _main_db_module(tmp):
    """db.py production-версии до PR (main @ 5fb90f9) — проверка обновления
    существующей БД.  Коммит зафиксирован: после merge main уже содержит новый db.py."""
    src = subprocess.run(["git", "show", f"{PRE_PR_COMMIT}:db.py"], cwd=REPO_ROOT,
                         capture_output=True, text=True)
    if src.returncode != 0:
        return None
    path = pathlib.Path(tmp) / "db_main.py"
    path.write_text(src.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("db_main_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DB_PATH = pathlib.Path(tmp) / "test_bot.db"
    return mod


def test_db_upgrade_existing_database_preserves_data():
    with tempfile.TemporaryDirectory() as tmp:
        old = _main_db_module(tmp)
        assert old is not None or not IN_CI, f"нет коммита {PRE_PR_COMMIT} в CI (fetch-depth)"
        if old is None:
            return
        old.init_db()
        old.upsert_user(5, "old")
        old.set_full_name(5, "Анна Смирнова")
        old.save_google_tokens(5, "at", "rt", 123.0)
        old.add_contact(5, "Коля", "k@example.com")
        conn = sqlite3.connect(old.DB_PATH)
        before_users = conn.execute("SELECT * FROM users").fetchall()
        before_cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
        conn.close()
        # первая версия таблицы my_tasks (из прошлой ревизии PR) с данными
        conn = sqlite3.connect(old.DB_PATH)
        conn.execute("""CREATE TABLE my_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL, source TEXT NOT NULL, source_key TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '', origin TEXT DEFAULT '', assignee TEXT DEFAULT '',
            task_date TEXT DEFAULT '', deadline TEXT DEFAULT '', status TEXT NOT NULL,
            verified INTEGER DEFAULT 0, sheet_status TEXT, ext_status TEXT,
            ext_synced_status TEXT, gtasks_list_id TEXT, gtasks_task_id TEXT,
            missing_since REAL, created_at REAL, updated_at REAL,
            UNIQUE (telegram_id, source_key))""")
        conn.execute("""INSERT INTO my_tasks (telegram_id, source, source_key, title, status,
            gtasks_list_id, gtasks_task_id, sheet_status, ext_status, ext_synced_status)
            VALUES (5,'gtasks','gtasks:t1','Старая открытая задача','В работе','L1','t1',
            'В работе','needsAction','В работе')""")
        conn.execute("CREATE TABLE my_tasks_sync (telegram_id INTEGER PRIMARY KEY, "
                     "last_daily_date TEXT)")
        conn.commit()
        conn.close()

        db = _ts._load_db(tmp)
        db.init_db()
        db.init_db()                                       # идемпотентно
        conn = sqlite3.connect(db.DB_PATH)
        assert conn.execute("SELECT * FROM users").fetchall() == before_users
        assert [r[1] for r in conn.execute("PRAGMA table_info(users)")] == before_cols
        assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1
        cols = {r[1] for r in conn.execute("PRAGMA table_info(my_tasks)")}
        assert {"link", "gt_delete_pending", "prev_status"} <= cols
        scols = {r[1] for r in conn.execute("PRAGMA table_info(my_tasks_sync)")}
        assert {"last_success_slot", "fail_count", "next_attempt_ts", "notified_slot"} <= scols
        conn.close()
        recs = db.my_tasks_load(5)
        assert len(recs) == 1 and recs[0]["title"] == "Старая открытая задача"
        assert recs[0]["_plaintext"] and recs[0]["gt_delete_pending"] == 0


def test_db_encryption_dedup_and_rekey():
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        rec = {"source": "telegram", "source_key": "tg:abc", "title": "Секретный текст",
               "origin": "Telegram — Мария", "status": "В работе"}
        assert db.my_tasks_insert_new(1, rec) is True
        assert db.my_tasks_insert_new(1, rec) is False          # дубль
        assert db.my_tasks_insert_new(2, rec) is True           # другой пользователь
        raw = sqlite3.connect(db.DB_PATH).execute(
            "SELECT title, origin FROM my_tasks WHERE telegram_id=1").fetchone()
        assert raw[0].startswith("enc:v1:") and "Секретный" not in raw[0], raw
        assert raw[1].startswith("enc:v1:") and "Мария" not in raw[1]
        got = db.my_tasks_load(1)[0]
        assert got["title"] == "Секретный текст" and not got.get("_decrypt_error")
        db.my_tasks_upsert(1, [dict(got, title="Новый", status="Выполнено")])
        got = db.my_tasks_get(1, "tg:abc")
        assert got["title"] == "Новый" and got["status"] == "Выполнено"
        db.my_tasks_mark_written(1, {"tg:abc": "Выполнено"})
        assert db.my_tasks_load(1)[0]["sheet_status"] == "Выполнено"
        # rekey без дублей
        db.my_tasks_insert_new(1, {"source": "telegram", "source_key": "tg:other",
                                   "title": "x", "status": "В работе"})
        assert db.my_tasks_rekey(1, "tg:abc", "tg:other") is False
        assert db.my_tasks_rekey(1, "tg:abc", "tg:new") is True
        assert {r["source_key"] for r in db.my_tasks_load(1)} == {"tg:new", "tg:other"}
        # состояние ежедневной сверки
        assert db.my_tasks_sync_get(1) is None
        db.my_tasks_sync_save(1, fail_count=2, last_outcome="partial")
        db.my_tasks_sync_save(1, last_success_slot="2026-11-12", fail_count=0)
        st = db.my_tasks_sync_get(1)
        assert st["last_success_slot"] == "2026-11-12" and st["fail_count"] == 0
        assert st["last_outcome"] == "partial"


def test_wrong_key_no_data_loss():
    """Другой ENCRYPTION_KEY: записи не расшифровываются → лист и GT не трогаем,
    БД не затираем; с верным ключом всё на месте."""
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        mt.add_forwarded(1, {"title": "Важное", "assigner": "", "task_date": "11.11.2026",
                             "deadline": "", "source_key": "tg:k"}, dbm=db)
        raw_before = sqlite3.connect(db.DB_PATH).execute("SELECT * FROM my_tasks").fetchall()
        book = _Book({"Планёрка": [PROTO_HDR]})
        try:
            os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
            _crypto._fernet = None
            gt = _FakeGT([_gt("a")])
            res = mt.run_sync(1, with_gtasks=True, dbm=db, gt=gt,
                              ms=_ms_with_book(ms, book))
            assert res["outcome"] == mt.OUT_ANOMALY, res
            assert "расшифровать" in res["text"]
            assert MY_TITLE not in book.data                     # лист не создан/не тронут
            assert gt.calls == []                                # GT не трогали
            assert sqlite3.connect(db.DB_PATH).execute(
                "SELECT * FROM my_tasks").fetchall() == raw_before
        finally:
            os.environ["ENCRYPTION_KEY"] = _TEST_KEY
            _crypto._fernet = None
        assert db.my_tasks_load(1)[0]["title"] == "Важное"


# ══ 5. Google Tasks: чистая сверка ═══════════════════════════════════════

def _gt(tid, title="Задача", status="needsAction", due=None, list_id="L1", **kw):
    it = {"id": tid, "title": title, "status": status, "_listId": list_id, "_listTitle": "Мои"}
    if due:
        it["due"] = due
    it.update(kw)
    return it


def _reconcile(mt, records, items, complete=True, list_ids=("L1", "L2"), sheet_vals=None):
    stats, warnings = {}, []
    plan = mt.reconcile_gtasks(records, items, complete, list_ids, ME,
                               stats, warnings, sheet_vals)
    for r in records.values():
        r.pop("_dirty", None)
        r.pop("_old_keys", None)
    return plan, stats, warnings


def test_reconcile_gtasks_import_two_way_no_loops():
    mt = _load_my_tasks()
    recs = {}
    plan, stats, _ = _reconcile(mt, recs, [
        _gt("a", due="2026-11-20T00:00:00.000Z"), _gt("b", status="completed"),
        _gt("a"), _gt("c", title="  "),
        _gt("m", links=[{"type": "email", "description": "Счёт №5",
                         "link": "https://mail.google.com/mail/#all/abc"}])])
    assert not plan.pushes and stats["gtasks_new"] == 3
    a, b = recs["gtasks:L1:a"], recs["gtasks:L1:b"]
    assert a["status"] == "В работе" and a["deadline"] == "20.11.2026"
    assert b["status"] == "Выполнено"
    assert recs["gtasks:L1:m"]["link"] == "✉️ Счёт №5: https://mail.google.com/mail/#all/abc"
    assert a["link"] == ""                           # письма нет — ничего не выдумываем
    base = [_gt("a"), _gt("b", status="completed"), _gt("m")]
    plan, stats, _ = _reconcile(mt, recs, base)
    assert not plan.pushes and not stats.get("gtasks_status_in")
    # GT → лист, в т.ч. повторное открытие
    plan, stats, _ = _reconcile(mt, recs, [_gt("a", status="completed"), _gt("b"), _gt("m")])
    assert not plan.pushes and stats["gtasks_status_in"] == 2
    assert a["status"] == "Выполнено" and b["status"] == "В работе"
    # лист → GT (переоткрытие и закрытие)
    a["status"], b["status"] = "В работе", "Выполнено"
    plan, _, _ = _reconcile(mt, recs, [_gt("a", status="completed"), _gt("b"), _gt("m")])
    assert sorted(plan.pushes) == [("gtasks:L1:a", "L1", "a", "needsAction"),
                                   ("gtasks:L1:b", "L1", "b", "completed")]
    # после успешного push — повтор без операций (нет цикла)
    a["ext_status"], b["ext_status"] = "needsAction", "completed"
    plan, stats, _ = _reconcile(mt, recs, [_gt("a"), _gt("b", status="completed"), _gt("m")])
    assert not plan.pushes and not stats.get("gtasks_status_in")
    # «Просрочено» локально, а в GT закрыли → принимаем GT, не переоткрываем
    a.update(status="Просрочено")
    plan, _, _ = _reconcile(mt, recs, [_gt("a", status="completed", title="Новое имя"),
                                       _gt("b", status="completed"), _gt("m")])
    assert not plan.pushes and a["status"] == "Выполнено" and a["title"] == "Новое имя"
    assert mt.gt_class("Отменено") is None and mt.gt_class("Выполнено") == "completed"
    assert mt.gt_class("Просрочено") == mt.gt_class("Требует верификации") == "needsAction"
    # «Отменено» не превращается в «Выполнено» и не отправляется как completed
    b.update(status="Отменено")
    plan, _, _ = _reconcile(mt, recs, [_gt("a", status="completed"),
                                       _gt("b", status="completed"), _gt("m")])
    assert b["status"] == "Отменено" and not plan.pushes and not plan.deletes


def test_reconcile_gtasks_composite_key_move_and_legacy():
    mt = _load_my_tasks()
    recs = {}
    _reconcile(mt, recs, [_gt("same", list_id="L1"), _gt("same", list_id="L2", title="Другая")])
    assert set(recs) == {"gtasks:L1:same", "gtasks:L2:same"}      # одинаковый id в двух списках
    # перенос между списками: тот же task_id в другом списке → перенос ключа, не дубль
    recs = {}
    _reconcile(mt, recs, [_gt("t", list_id="L1")])
    recs["gtasks:L1:t"]["status"] = "Просрочено"
    plan, stats, _ = _reconcile(mt, recs, [_gt("t", list_id="L2")])
    assert set(recs) == {"gtasks:L2:t"} and stats["gtasks_moved"] == 1
    assert recs["gtasks:L2:t"]["status"] == "Просрочено" and not plan.confirm_missing
    # ключ первой версии PR → составной
    legacy = {"gtasks:t9": {"source": "gtasks", "source_key": "gtasks:t9", "status": "Выполнено",
                            "gtasks_list_id": "L1", "gtasks_task_id": "t9"}}
    assert mt.migrate_legacy_gt_keys(legacy) == 1 and set(legacy) == {"gtasks:L1:t9"}
    assert legacy["gtasks:L1:t9"]["_old_keys"] == ["gtasks:t9"]


def test_reconcile_gtasks_missing_requires_confirmation():
    mt = _load_my_tasks()
    recs = {}
    items = [_gt(str(i)) for i in range(20)]
    _reconcile(mt, recs, items)
    # 2 пропали — НЕ отменяем сразу, а планируем адресную проверку
    plan, stats, w = _reconcile(mt, recs, items[2:])
    assert sorted(k for k, *_ in plan.confirm_missing) == ["gtasks:L1:0", "gtasks:L1:1"]
    assert recs["gtasks:L1:0"]["status"] == "В работе" and not w
    # массово пропали — аномалия, без проверок и изменений
    plan, stats, w = _reconcile(mt, recs, items[:2])
    assert stats["gtasks_mass_missing"] == 18 and not plan.confirm_missing
    assert any("массового" in x for x in w)
    plan, stats, _ = _reconcile(mt, recs, [])                  # пустой ответ
    assert stats["gtasks_mass_missing"] == 20 and not plan.confirm_missing
    # неполный ответ — ничего
    plan, stats, w = _reconcile(mt, recs, items[1:], complete=False)
    assert not plan.confirm_missing and any("неполный" in x for x in w)
    # список недоступен (смена аккаунта / удалён список) — ничего
    plan, stats, w = _reconcile(mt, recs, items[1:], list_ids=("L2",))
    assert not plan.confirm_missing and stats["gtasks_list_missing"] == 1
    assert all(r["status"] == "В работе" for r in recs.values())


# ══ 6. Лист «Мои задачи»: сквозные сценарии ══════════════════════════════

class _Book(_ts._FakeBook):
    """_FakeBook: «!A:H» отдаёт 8 колонок; on_reread — правка между чтениями."""

    def __init__(self, sheets):
        super().__init__(sheets)
        self.protected = {}
        self.on_reread = None
        self._my_reads = 0

        def _meta(params=None):
            out = []
            for t, w in self.ws.items():
                sh = {"properties": {"sheetId": w.id, "title": t}}
                if t in self.protected:
                    sh["protectedRanges"] = self.protected[t]
                out.append(sh)
            return {"sheets": out}
        self.ss.fetch_sheet_metadata.side_effect = _meta

    def _batch_get(self, ranges):
        out = []
        for r in ranges:
            t, suffix = r.rsplit("!", 1)
            t = t[1:-1].replace("''", "'")
            if suffix == "A:H":
                self._my_reads += 1
                if self._my_reads >= 2 and self.on_reread:
                    self.on_reread(self)
            width = 8 if suffix == "A:H" else 7
            out.append({"values": [list(row[:width]) for row in self.data[t]]})
        return {"valueRanges": out}

    def set_status(self, title, task, status):
        for row in self.data[title][1:]:
            if row[3].split("\n")[0] == task:
                row[6] = status
                return
        raise AssertionError(f"нет строки {task}")

    def rows(self, title):
        return {r[3].split("\n")[0]: r for r in self.data[title][1:]}

    def requests(self):
        return [r for c in self.ss.batch_update.call_args_list for r in c.args[0]["requests"]]


def _proto_book():
    return _Book({
        "Планёрка": [
            PROTO_HDR,
            ["1", "10.11.2026", "Отчёт по продажам", "Петров И.", "20.11.2026", "Требует верификации", "m1"],
            ["2", "10.11.2026", "Бюджет", "Иван Петров", "", "В работе", "m1"],
            ["3", "10.11.2026", "Чужая задача", "Анна Смирнова", "", "Требует верификации", "m1"],
            ["4", "10.11.2026", "Неизвестно чья", "Speaker A", "", "Требует верификации", "m1"],
            ["5", "10.11.2026", "Однофамилец", "Петров", "", "Требует верификации", "m1"],
            ["6", "10.11.2026", "Тёзка", "Иван Сидоров", "", "Требует верификации", "m1"],
        ],
    })


class _FakeGT:
    """Фейковый Google Tasks API: никакой сети, все операции записываются."""

    def __init__(self, items=None, err=None, complete=True, list_ids=("L1", "L2"),
                 delete_results=None, get_results=None, push_ok=True):
        self.items, self.err, self.complete = items or [], err, complete
        self.list_ids = list(list_ids)
        self.delete_results = list(delete_results or [])
        self.get_results = dict(get_results or {})
        self.push_ok = push_ok
        self.calls = []

    def list_all_tasks_full(self, telegram_id):
        return ([dict(i) for i in self.items], self.err, self.complete, list(self.list_ids))

    def set_task_status(self, telegram_id, list_id, task_id, gt_status):
        self.calls.append(("status", list_id, task_id, gt_status))
        return self.push_ok

    def delete_task(self, telegram_id, list_id, task_id):
        self.calls.append(("delete", list_id, task_id))
        res = self.delete_results.pop(0) if self.delete_results else "deleted"
        if res == "deleted":
            self.items = [i for i in self.items
                          if not (i["id"] == task_id and i["_listId"] == list_id)]
        return res

    def get_task_state(self, telegram_id, list_id, task_id):
        self.calls.append(("get", list_id, task_id))
        return self.get_results.get(task_id, "deleted")

    def deletes(self):
        return [c for c in self.calls if c[0] == "delete"]


def _ms_with_book(ms, book):
    def _refresh(sheet_id, full_name, builder):
        return ms._sync_my_tasks_and_colors(book.ss, full_name, rows_builder=builder)
    return NS(refresh_my_tasks_sheet=_refresh)


def _sync(mt, ms, db, book, gt=None, with_gtasks=True):
    return mt.run_sync(1, with_gtasks=with_gtasks, dbm=db, gt=gt or _FakeGT(),
                       ms=_ms_with_book(ms, book))


def test_sheet_three_sources_verification_end_to_end():
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        mt.add_forwarded(1, {"title": "Позвонить клиенту", "assigner": "Мария", "task_date":
                             "11.11.2026", "deadline": "12.11.2026", "source_key": "tg:x1"}, dbm=db)
        book = _proto_book()
        gt = _FakeGT([_gt("g1", title="Купить билеты"), _gt("g2", title="Старое",
                                                            status="completed")])
        res = _sync(mt, ms, db, book, gt)
        assert res["outcome"] == mt.OUT_OK, res
        rows = book.rows(MY_TITLE)
        assert book.data[MY_TITLE][0] == ms.MY_TASKS_HEADER_ROW_V2
        assert set(rows) == {"Отчёт по продажам", "Бюджет", "Купить билеты", "Старое",
                             "Позвонить клиенту"}, set(rows)
        # Speaker A, однофамилец, тёзка, чужой — не попали
        assert rows["Отчёт по продажам"][6] == "Требует верификации"
        # статус протокола («В работе») НЕ переносится: новая задача не подтверждена
        assert rows["Бюджет"][6] == "Требует верификации"
        assert rows["Купить билеты"][7] == "gtasks:L1:g1" and rows["Старое"][6] == "Выполнено"
        assert rows["Позвонить клиенту"][2] == "Telegram — Мария"
        reqs = book.requests()
        assert any(r["setDataValidation"]["range"]["startColumnIndex"] == 6
                   for r in reqs if "setDataValidation" in r)
        prot = [r["addProtectedRange"]["protectedRange"] for r in reqs if "addProtectedRange" in r]
        assert prot[0]["unprotectedRanges"][0]["startColumnIndex"] == 6
        # повтор без изменений: тот же лист, без операций в GT, без дублей
        snap = [list(r) for r in book.data[MY_TITLE]]
        gt.calls.clear()
        assert _sync(mt, ms, db, book, gt)["outcome"] == mt.OUT_OK
        assert book.data[MY_TITLE] == snap and [c for c in gt.calls if c[0] != "get"] == []
        assert len(db.my_tasks_load(1)) == 5
        # бот сам «Требует верификации» не снимает (даже через много прогонов)
        for _ in range(2):
            _sync(mt, ms, db, book, gt)
        assert book.rows(MY_TITLE)["Отчёт по продажам"][6] == "Требует верификации"
        # ручная верификация в листе
        book.set_status(MY_TITLE, "Отчёт по продажам", "В работе")
        book.set_status(MY_TITLE, "Купить билеты", "Выполнено")
        assert _sync(mt, ms, db, book, gt)["outcome"] == mt.OUT_OK
        assert book.rows(MY_TITLE)["Отчёт по продажам"][6] == "В работе"
        assert ("status", "L1", "g1", "completed") in gt.calls
        # изменения статуса в протоколе личную задачу не меняют (ни подтверждённую,
        # ни неподтверждённую)
        for row in book.data["Планёрка"]:
            if row[2] == "Отчёт по продажам":
                row[5] = "Требует верификации"
            if row[2] == "Бюджет":
                row[5] = "Выполнено"
        _sync(mt, ms, db, book, gt, with_gtasks=False)
        rows = book.rows(MY_TITLE)
        assert rows["Отчёт по продажам"][6] == "В работе"
        assert rows["Бюджет"][6] == "Требует верификации"
        assert book.ws["Планёрка"].update.call_count == 0     # протокол не перезаписан


def test_cancel_in_sheet_deletes_gt_task_once():
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR]})
        gt = _FakeGT([_gt("g1", title="Удалить меня"), _gt("g2", title="Оставить")])
        _sync(mt, ms, db, book, gt)
        assert gt.deletes() == []                             # импорт не удаляет
        book.set_status(MY_TITLE, "Удалить меня", "Отменено")
        # сеанс без Google Tasks (MYmeet/пересылка): удаления нет, но правка сохранена
        _sync(mt, ms, db, book, gt, with_gtasks=False)
        assert gt.deletes() == []
        assert book.rows(MY_TITLE)["Удалить меня"][6] == "Отменено"
        res = _sync(mt, ms, db, book, gt)
        assert gt.deletes() == [("delete", "L1", "g1")], gt.calls
        assert res["outcome"] == mt.OUT_OK and "удалено в GT 1" in res["text"]
        rows = book.rows(MY_TITLE)
        assert rows["Удалить меня"][6] == "Отменено"         # строка и статус сохранены
        assert rows["Оставить"][6] == "В работе"
        # повторы — без повторного удаления
        for _ in range(3):
            _sync(mt, ms, db, book, gt)
        assert len(gt.deletes()) == 1
        rec = db.my_tasks_get(1, "gtasks:L1:g1")
        assert rec["status"] == "Отменено" and rec["ext_status"] == "deleted"


def test_cancel_deletion_safety_cases():
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        # (1) старое «Отменено» в листе при первом импорте → не удаляем
        pre = [ms.MY_TASKS_HEADER_ROW_V2,
               [1, "01.11.2026", "Google Tasks", "Старая", ME, "", "Отменено", "gtasks:L1:o"]]
        book = _Book({"Планёрка": [PROTO_HDR], MY_TITLE: pre})
        gt = _FakeGT([_gt("o", title="Старая")])
        _sync(mt, ms, db, book, gt)
        _sync(mt, ms, db, book, gt)
        assert gt.deletes() == [] and not [c for c in gt.calls if c[0] == "status"]
        assert book.rows(MY_TITLE)["Старая"][6] == "Отменено"     # пометка не затёрта
        # (1б) запись есть в БД, но в лист ещё не писалась, а в листе «Отменено»
        db.my_tasks_upsert(1, [{"source": "gtasks", "source_key": "gtasks:L1:n",
                                "title": "Новая", "status": "В работе",
                                "ext_status": "needsAction", "ext_synced_status": "В работе",
                                "gtasks_list_id": "L1", "gtasks_task_id": "n"}])
        gt.items.append(_gt("n", title="Новая"))
        book.data[MY_TITLE].append([9, "01.11.2026", "Google Tasks", "Новая", ME, "",
                                    "Отменено", "gtasks:L1:n"])
        _sync(mt, ms, db, book, gt)
        _sync(mt, ms, db, book, gt)
        assert gt.deletes() == [] and book.rows(MY_TITLE)["Новая"][6] == "Отменено"
        # (2) запись с «Отменено» из БД (миграция/восстановление) → не удаляем
        db.my_tasks_upsert(1, [{"source": "gtasks", "source_key": "gtasks:L1:z",
                                "title": "Z", "status": "Отменено", "sheet_status": "Отменено",
                                "ext_status": "needsAction", "ext_synced_status": "Отменено",
                                "gtasks_list_id": "L1", "gtasks_task_id": "z"}])
        gt.items.append(_gt("z", title="Z"))
        _sync(mt, ms, db, book, gt)
        assert gt.deletes() == []

    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR]})
        gt = _FakeGT([_gt("a", title="A"), _gt("b", title="B")])
        _sync(mt, ms, db, book, gt)
        # (3) неверная привязка (id списка в записи не совпадает с ключом) → не удаляем
        book.set_status(MY_TITLE, "A", "Отменено")
        b = mt.MyTasksBuilder(1, ME, gtasks=(gt.items, True, ["L1"]), gt_ops=gt, dbm=db)
        ms._sync_my_tasks_and_colors(book.ss, ME, rows_builder=b)
        assert gt.deletes() == [("delete", "L1", "a")]
        gt.calls.clear()
        plan = mt.GTPlan()
        plan.deletes = [("gtasks:L1:b", "L2", "b"), ("gtasks:L1:b", None, "b")]
        b2 = mt.MyTasksBuilder(1, ME, gt_ops=gt, dbm=db)
        b2._records = {r["source_key"]: r for r in db.my_tasks_load(1)}
        b2._plan = plan
        b2.execute_external()
        assert gt.deletes() == [] and b2.stats["gtasks_delete_failed"] == 2
        # (4) временная ошибка удаления → pending сохраняется, повтор один раз
        book.set_status(MY_TITLE, "B", "Отменено")
        gt.delete_results = ["retry"]
        res = _sync(mt, ms, db, book, gt)
        assert res["outcome"] == mt.OUT_PARTIAL and db.my_tasks_get(1, "gtasks:L1:b")[
            "gt_delete_pending"] == 1
        _sync(mt, ms, db, book, gt)
        assert gt.deletes() == [("delete", "L1", "b"), ("delete", "L1", "b")]
        _sync(mt, ms, db, book, gt)
        assert len(gt.deletes()) == 2                       # после успеха — больше нет
        assert db.my_tasks_get(1, "gtasks:L1:b")["gt_delete_pending"] == 0

    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR]})
        gt = _FakeGT([_gt("a", title="A"), _gt("b", title="B")])
        _sync(mt, ms, db, book, gt)
        # (4б) отмена отмечена, но строка пропала из листа (удалена/сбой
        #      чтения) до сверки с GT → не удаляем
        book.set_status(MY_TITLE, "A", "Отменено")
        _sync(mt, ms, db, book, gt, with_gtasks=False)
        assert db.my_tasks_get(1, "gtasks:L1:a")["gt_delete_pending"] == 1
        book.data[MY_TITLE] = [r for r in book.data[MY_TITLE] if not str(r[3]).startswith("A")]
        _sync(mt, ms, db, book, gt)
        assert gt.deletes() == []
        assert db.my_tasks_get(1, "gtasks:L1:a")["gt_delete_pending"] == 1   # ждёт листа
        _sync(mt, ms, db, book, gt)          # строка вернулась (бот её записал) → удаление
        assert gt.deletes() == [("delete", "L1", "a")]
        gt.calls.clear()
        gt.items.append(_gt("a", title="A"))   # для следующих шагов — новая задача A
        db.my_tasks_upsert(1, [dict(db.my_tasks_get(1, "gtasks:L1:a"), ext_status="needsAction",
                                    status="В работе", ext_synced_status="В работе",
                                    missing_since=None)])
        _sync(mt, ms, db, book, gt)
        # (5) пользователь передумал до сверки → не удаляем
        book.set_status(MY_TITLE, "A", "Отменено")
        _sync(mt, ms, db, book, gt, with_gtasks=False)
        book.set_status(MY_TITLE, "A", "В работе")
        _sync(mt, ms, db, book, gt)
        assert gt.deletes() == []
        # (6) неполный ответ API → удаление отложено
        book.set_status(MY_TITLE, "A", "Отменено")
        gt.complete = False
        res = _sync(mt, ms, db, book, gt)
        assert gt.deletes() == [] and res["outcome"] == mt.OUT_PARTIAL
        # (7) задача уже удалена в GT → «absent», без вызова delete
        gt.complete = True
        gt.items = [i for i in gt.items if i["id"] != "a"]
        _sync(mt, ms, db, book, gt)
        assert gt.deletes() == [] and db.my_tasks_get(1, "gtasks:L1:a")["gt_delete_pending"] == 0
        # (8) ошибка API при листинге → никаких операций
        book.set_status(MY_TITLE, "B", "Отменено")
        gt.err = "api_error"
        res = _sync(mt, ms, db, book, gt)
        assert gt.deletes() == [] and res["outcome"] == mt.OUT_TRANSIENT


def test_mass_cancel_in_sheet_no_deletes():
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR]})
        gt = _FakeGT([_gt(str(i), title=f"T{i}") for i in range(10)])
        _sync(mt, ms, db, book, gt)
        for i in range(6):
            book.set_status(MY_TITLE, f"T{i}", "Отменено")
        res = _sync(mt, ms, db, book, gt)
        assert gt.deletes() == [] and res["outcome"] == mt.OUT_ANOMALY
        assert "массового" in res["text"]
        assert all(book.rows(MY_TITLE)[f"T{i}"][6] == "Отменено" for i in range(6))
        _sync(mt, ms, db, book, gt)
        assert gt.deletes() == []                          # и потом тоже


def test_deleted_in_gt_becomes_cancelled_after_confirmation():
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR]})
        items = [_gt(str(i), title=f"T{i}", status="completed" if i == 1 else "needsAction")
                 for i in range(10)]
        gt = _FakeGT(items)
        _sync(mt, ms, db, book, gt)
        gt.items = items[2:]
        gt.get_results = {"0": "deleted", "1": "deleted"}
        res = _sync(mt, ms, db, book, gt)
        rows = book.rows(MY_TITLE)
        assert rows["T0"][6] == "Отменено" and rows["T1"][6] == "Отменено"   # «Выполнено» → тоже
        assert db.my_tasks_get(1, "gtasks:L1:1")["prev_status"] == "Выполнено"
        assert res["outcome"] == mt.OUT_OK and len(rows) == 10            # строки сохранены
        assert gt.deletes() == []
        # GET говорит «существует» (сбой листинга) → ничего не меняем
        gt.items = items[3:]
        gt.get_results = {"2": "exists"}
        _sync(mt, ms, db, book, gt)
        assert book.rows(MY_TITLE)["T2"][6] == "В работе"
        # GET — ошибка → не трогаем, итог частичный
        gt.get_results = {"2": "error"}
        res = _sync(mt, ms, db, book, gt)
        assert book.rows(MY_TITLE)["T2"][6] == "В работе" and res["outcome"] == mt.OUT_PARTIAL
        # массово пропали → аномалия, ни одного GET/изменения
        gt.calls.clear()
        gt.items = []
        res = _sync(mt, ms, db, book, gt)
        assert res["outcome"] == mt.OUT_ANOMALY and not [c for c in gt.calls if c[0] == "get"]
        assert book.rows(MY_TITLE)["T5"][6] == "В работе"


def test_concurrent_manual_edit_not_overwritten():
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR]})
        gt = _FakeGT([_gt("a", title="A"), _gt("b", title="B")])
        _sync(mt, ms, db, book, gt)
        book.set_status(MY_TITLE, "A", "Отменено")          # правка до сверки
        # пользователь меняет B между чтением и записью
        book.on_reread = lambda bk: bk.set_status(MY_TITLE, "B", "Выполнено")
        upd_before = book.ws[MY_TITLE].update.call_count
        res = _sync(mt, ms, db, book, gt)
        assert res["outcome"] == mt.OUT_TRANSIENT and "отложена" in res["text"]
        assert book.ws[MY_TITLE].update.call_count == upd_before     # не перезаписан
        assert book.rows(MY_TITLE)["B"][6] == "Выполнено"           # правка цела
        assert gt.calls == [c for c in gt.calls if c[0] == "get"]   # ни push, ни delete
        # следующий прогон — обе правки приняты
        book.on_reread = None
        res = _sync(mt, ms, db, book, gt)
        assert res["outcome"] == mt.OUT_OK
        assert ("delete", "L1", "a") in gt.calls and ("status", "L1", "b", "completed") in gt.calls
        assert book.rows(MY_TITLE)["B"][6] == "Выполнено"


def test_sheet_write_failure_and_shrink_keep_data():
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        old = [ms.MY_TASKS_HEADER_ROW_V2] + [
            [i, "01.11.2026", "Google Tasks", f"Старая {i}", ME, "", "В работе", f"gtasks:L1:z{i}"]
            for i in range(1, 13)]
        book = _Book({"Планёрка": [PROTO_HDR], MY_TITLE: [list(r) for r in old]})
        res = _sync(mt, ms, db, book, _FakeGT(), with_gtasks=False)
        assert res["outcome"] == mt.OUT_ANOMALY and "массового удаления" in res["text"]
        assert book.data[MY_TITLE] == old and book.ws[MY_TITLE].batch_clear.call_count == 0
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR]})
        gt = _FakeGT([_gt("a", title="A")])
        _sync(mt, ms, db, book, gt)
        snap = [list(r) for r in book.data[MY_TITLE]]
        book.set_status(MY_TITLE, "A", "Выполнено")
        book.ws[MY_TITLE].update.side_effect = RuntimeError("quota")
        res = _sync(mt, ms, db, book, gt, with_gtasks=False)
        assert res["outcome"] == mt.OUT_PARTIAL
        assert book.data[MY_TITLE][1][6] == "Выполнено" and len(book.data[MY_TITLE]) == len(snap)
        rec = db.my_tasks_get(1, "gtasks:L1:a")
        assert rec["status"] == "Выполнено" and rec["sheet_status"] == "В работе"


def test_legacy_sheet_migration_and_legacy_mode():
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        legacy = [ms.MY_TASKS_HEADER_ROW,
                  [1, "10.11.2026", "Планёрка", "Бюджет", ME, "", "В работе", "m1"]]
        book = _Book({"Планёрка": [PROTO_HDR, ["1", "10.11.2026", "Бюджет", ME, "",
                                              "В работе", "m1"]], MY_TITLE: legacy})
        book.protected[MY_TITLE] = [
            {"protectedRangeId": 77, "description": "Генерируется ботом при синхронизации. "
                                                    "Статус меняйте в листе протокола."},
            {"protectedRangeId": 78, "description": "Чужая защита"}]
        res = _sync(mt, ms, db, book, with_gtasks=False)
        assert res["outcome"] == mt.OUT_OK and res["sheet_count"] == 1
        upd = [r["updateProtectedRange"] for r in book.requests() if "updateProtectedRange" in r]
        assert len(upd) == 1 and upd[0]["protectedRange"]["protectedRangeId"] == 77
        assert book.data[MY_TITLE][0] == ms.MY_TASKS_HEADER_ROW_V2
        # статус старого листа — копия протокола, а не подтверждение пользователя
        assert book.rows(MY_TITLE)["Бюджет"][6] == "Требует верификации"
    # без builder — прежний режим
    book = _Book({"Планёрка": [PROTO_HDR, ["1", "10.11.2026", "Отчёт", "Петров И.", "",
                                          "В работе", "m1"],
                               ["2", "10.11.2026", "Бюджет", ME, "", "В работе", "m1"]]})
    _, cnt, _ = ms._sync_my_tasks_and_colors(book.ss, ME)
    assert cnt == 1 and book.data[MY_TITLE][0] == ms.MY_TASKS_HEADER_ROW
    assert not [r for r in book.requests() if "setDataValidation" in r]


# ══ 7. run_sync: итоги ═══════════════════════════════════════════════════

def test_run_sync_outcomes_and_retry_no_duplicates():
    ms, mt = _load_ms(), _load_my_tasks()
    never = NS(refresh_my_tasks_sheet=lambda *a: (_ for _ in ()).throw(
        AssertionError("лист не настроен — не должен вызываться")))
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db, sheet=False)
        res = mt.run_sync(1, dbm=db, gt=_FakeGT([_gt("a")]), ms=never)
        assert res["outcome"] == mt.OUT_OK and "/setup_mymeet" in res["text"]
        assert res["outcome"] == mt.run_sync(1, dbm=db, gt=_FakeGT([_gt("a")]), ms=never)["outcome"]
        assert len(db.my_tasks_load(1)) == 1
        r = mt.run_sync(1, dbm=db, gt=_FakeGT(err="no_scope"), ms=never)
        assert r["outcome"] == mt.OUT_CONFIG and "/connect" in r["text"] and "✅" not in r["text"]
        r = mt.run_sync(1, dbm=db, gt=_FakeGT(err="api_error"), ms=never)
        assert r["outcome"] == mt.OUT_TRANSIENT and "✅" not in r["text"]
        r = mt.run_sync(1, dbm=db, gt=_FakeGT([_gt("a")], complete=False), ms=never)
        assert r["outcome"] == mt.OUT_PARTIAL
        assert db.my_tasks_load(1)[0]["status"] == "В работе"
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        boom = NS(refresh_my_tasks_sheet=lambda *a: (_ for _ in ()).throw(
            RuntimeError("sheets down token=SECRET")))
        r = mt.run_sync(1, dbm=db, gt=_FakeGT([_gt("a", title="A"), _gt("b", title="B")]),
                        ms=boom)
        assert r["outcome"] == mt.OUT_PARTIAL and "SECRET" not in r["text"]
        # повтор после сбоя — без дублей
        book = _Book({"Планёрка": [PROTO_HDR]})
        r = _sync(mt, ms, db, book, _FakeGT([_gt("a", title="A"), _gt("b", title="B")]))
        assert r["outcome"] == mt.OUT_OK and len(db.my_tasks_load(1)) == 2
        assert len(book.rows(MY_TITLE)) == 2


# ══ 8. Расписание 19:00 ══════════════════════════════════════════════════

def test_daily_schedule_slots_and_retries():
    from zoneinfo import ZoneInfo
    mt = _load_my_tasks()
    msk = ZoneInfo("Europe/Moscow")
    t = lambda d, h, m=0: datetime(2026, 11, d, h, m, tzinfo=msk)   # noqa: E731
    due = mt.daily_due
    assert mt.daily_slot(t(12, 19, 5)) == "2026-11-12"
    assert mt.daily_slot(t(13, 8)) == "2026-11-12"
    assert due(None, t(12, 10), 0) is None                  # нет истории, утро — ждём 19:00
    assert due(None, t(12, 19, 1), 0) == "2026-11-12"
    ok = {"last_success_slot": "2026-11-12", "fail_count": 0}
    assert due(ok, t(12, 21), 0) is None                   # уже выполнено сегодня
    assert due(ok, t(13, 18, 59), 0) is None
    assert due(ok, t(13, 19, 0), 0) == "2026-11-13"
    # сбой в 19:00 — повтор после паузы, в т.ч. после 19:59 и ночью
    failed = {"last_success_slot": "2026-11-11", "fail_count": 2,
              "next_attempt_ts": t(12, 20, 30).timestamp()}
    assert due(failed, t(12, 20, 0), t(12, 20, 0).timestamp()) is None
    assert due(failed, t(12, 20, 45), t(12, 20, 45).timestamp()) == "2026-11-12"
    assert due(failed, t(13, 3), t(13, 3).timestamp()) == "2026-11-12"   # ночью — тот же слот
    first_fail = {"fail_count": 1, "next_attempt_ts": 0}
    assert due(first_fail, t(13, 10), t(13, 10).timestamp()) == "2026-11-12"
    assert [mt.next_attempt_delay(n) // 60 for n in (1, 2, 3, 4, 9)] == [15, 30, 60, 120, 120]
    sn = mt.should_notify
    assert not sn("ok", 0, None, "s")
    assert sn("anomaly", 1, None, "s") and not sn("anomaly", 1, "s", "s")
    assert sn("config", 1, None, "s")
    assert not sn("partial", 2, None, "s") and sn("partial", 3, None, "s")
    assert not sn("transient", 5, "s", "s")
    # пояса: 19:00 по поясу пользователя
    vl = ZoneInfo("Asia/Vladivostok")
    now = datetime(2026, 11, 12, 9, 30, tzinfo=timezone.utc)
    assert due(None, now.astimezone(vl), now.timestamp()) == "2026-11-12"
    assert due(None, now.astimezone(msk), now.timestamp()) is None


def test_record_daily_result_marks_only_success():
    mt = _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        import time as _time
        ns = _load_main_async(["_record_daily_result"], {"db": db, "my_tasks": mt,
                                                         "time_module": _time})
        rec = ns["_record_daily_result"]
        for i, out in enumerate(("partial", "transient", "partial"), 1):
            notify = rec(1, "2026-11-12", {"outcome": out}, db.my_tasks_sync_get(1))
            st = db.my_tasks_sync_get(1)
            assert st["last_success_slot"] is None and st["fail_count"] == i
            assert st["next_attempt_ts"] > _time.time()
            assert notify == (i == 3)
        assert not rec(1, "2026-11-12", {"outcome": "partial"}, db.my_tasks_sync_get(1))
        assert rec(1, "2026-11-13", {"outcome": "anomaly"}, db.my_tasks_sync_get(1))
        assert not rec(1, "2026-11-13", {"outcome": "ok"}, db.my_tasks_sync_get(1))
        st = db.my_tasks_sync_get(1)
        assert st["last_success_slot"] == "2026-11-13" and st["fail_count"] == 0


def test_user_lock_serializes_runs():
    """Операции над листом одного пользователя не выполняются одновременно."""
    import threading
    import time as _time
    active, peak, lock = [0], [0], threading.Lock()

    def run_sync(tid, with_gtasks):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        _time.sleep(0.05)
        with lock:
            active[0] -= 1
        return {"outcome": "ok"}
    ns = _load_main_async(["_user_lock", "_run_my_tasks_sync"],
                          {"my_tasks": NS(run_sync=run_sync)}, assigns=("_my_tasks_locks",))

    async def main():
        await asyncio.gather(*(ns["_run_my_tasks_sync"](1, True) for _ in range(4)))
        a = peak[0]
        peak[0] = 0
        await asyncio.gather(ns["_run_my_tasks_sync"](1, True), ns["_run_my_tasks_sync"](2, True))
        return a, peak[0]
    same_user, two_users = asyncio.run(main())
    assert same_user == 1 and two_users == 2


# ══ 9. gtasks.py и OAuth ═════════════════════════════════════════════════

def _load_gtasks():
    class HttpError(Exception):
        def __init__(self, status):
            self.resp = NS(status=status)
    fake = {
        "googleapiclient": types.ModuleType("googleapiclient"),
        "googleapiclient.discovery": types.ModuleType("googleapiclient.discovery"),
        "googleapiclient.errors": types.ModuleType("googleapiclient.errors"),
        "auth": types.ModuleType("auth"),
    }
    fake["googleapiclient.discovery"].build = lambda *a, **k: None
    fake["googleapiclient.errors"].HttpError = HttpError
    fake["auth"].get_credentials = lambda tid: None
    saved = {k: sys.modules.get(k) for k in fake}
    sys.modules.update(fake)
    try:
        spec = importlib.util.spec_from_file_location("gtasks_test", REPO_ROOT / "gtasks.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    mod.time = NS(sleep=lambda s: None)        # без реальных пауз в тестах
    return mod, HttpError


class _GTService:
    def __init__(self, lists, pages, err_cls, fail_list=None, op_errors=None, get_result=None):
        self.lists, self.pages, self.err_cls = lists, pages, err_cls
        self.fail_list = fail_list
        self.op_errors = list(op_errors or [])      # коды ошибок для delete/patch/get по очереди
        self.get_result = get_result or {}
        self.calls = []

    def tasklists(self):
        svc = self

        class _TL:
            def list(self, maxResults, pageToken=None):
                return _ts._Exec(lambda: svc.lists[pageToken or 0])
        return _TL()

    def _op(self, name, kw, result):
        def run():
            self.calls.append((name, kw))
            if self.op_errors:
                code = self.op_errors.pop(0)
                if code:
                    raise self.err_cls(code)
            return result
        return _ts._Exec(run)

    def tasks(self):
        svc = self

        class _T:
            def list(self, tasklist, pageToken=None, **kw):
                svc.calls.append(("list", tasklist, pageToken, kw))

                def run():
                    if tasklist == svc.fail_list:
                        raise svc.err_cls(500)
                    return svc.pages[(tasklist, pageToken)]
                return _ts._Exec(run)

            def patch(self, **kw):
                return svc._op("patch", kw, {})

            def delete(self, **kw):
                return svc._op("delete", kw, "")

            def get(self, **kw):
                return svc._op("get", kw, svc.get_result)
        return _T()


def test_gtasks_api_wrappers():
    gt, HttpError = _load_gtasks()
    lists = {0: {"items": [{"id": "L1", "title": "Работа"}], "nextPageToken": "p2"},
             "p2": {"items": [{"id": "L2", "title": "Дом"}]}}
    pages = {("L1", None): {"items": [{"id": "1", "title": "a"}], "nextPageToken": "n"},
             ("L1", "n"): {"items": [{"id": "2", "title": "b", "status": "completed"},
                                     {"id": "3", "title": "c", "deleted": True}]},
             ("L2", None): {"items": []}}
    svc = _GTService(lists, pages, HttpError)
    items, err, complete, list_ids = gt.list_all_tasks_full(1, service=svc)
    assert err is None and complete and [i["id"] for i in items] == ["1", "2"]
    assert list_ids == ["L1", "L2"]                    # пустой список тоже учтён
    kw = [c[3] for c in svc.calls if c[0] == "list"][0]
    assert kw["showCompleted"] and kw["showHidden"] and not kw["showDeleted"]
    _, _, complete, _ = gt.list_all_tasks_full(1, service=_GTService(lists, pages, HttpError,
                                                                     fail_list="L2"))
    assert not complete
    # delete: успех / уже нет / временная ошибка с повторами / прочие
    s = _GTService(lists, pages, HttpError)
    assert gt.delete_task(1, "L1", "1", service=s) == "deleted"
    assert s.calls[-1] == ("delete", {"tasklist": "L1", "task": "1"})
    assert gt.delete_task(1, "L1", "1", service=_GTService(lists, pages, HttpError,
                                                           op_errors=[404])) == "absent"
    s = _GTService(lists, pages, HttpError, op_errors=[503, 503, 503])
    assert gt.delete_task(1, "L1", "1", service=s) == "retry" and len(s.calls) == 3
    s = _GTService(lists, pages, HttpError, op_errors=[503, 0])
    assert gt.delete_task(1, "L1", "1", service=s) == "deleted" and len(s.calls) == 2
    assert gt.delete_task(1, "L1", "1", service=_GTService(lists, pages, HttpError,
                                                           op_errors=[403])) == "error"
    assert gt.delete_task(1, "", "1", service=s) == "error"
    assert gt.delete_task(1, "L1", None, service=s) == "error"
    # get_task_state: только 404/410/deleted — «удалена»
    st = lambda **k: gt.get_task_state(1, "L1", "1", service=_GTService(  # noqa: E731
        lists, pages, HttpError, **k))
    assert st(get_result={"id": "1"}) == "exists"
    assert st(get_result={"id": "1", "deleted": True}) == "deleted"
    assert st(op_errors=[404]) == "deleted" and st(op_errors=[410]) == "deleted"
    assert st(op_errors=[403]) == "error" and st(op_errors=[500, 500, 500]) == "error"
    # статусы
    s = _GTService(lists, pages, HttpError)
    assert gt.set_task_status(1, "L1", "1", "completed", service=s)
    assert gt.set_task_status(1, "L1", "1", "needsAction", service=s)
    patches = [c[1] for c in s.calls if c[0] == "patch"]
    assert patches[0]["body"] == {"status": "completed"}
    assert patches[1]["body"] == {"status": "needsAction", "completed": None}
    assert not gt.set_task_status(1, "L1", "1", "deleted", service=s)


def test_auth_scopes_no_gmail_and_no_gt_insert():
    tree = ast.parse((REPO_ROOT / "auth.py").read_text(encoding="utf-8"))
    scopes = None
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "SCOPES" for t in n.targets):
            scopes = ast.literal_eval(n.value)
    assert scopes and "https://www.googleapis.com/auth/tasks" in scopes
    assert not any("gmail" in s or "mail.google" in s for s in scopes), scopes
    # задачи в Google Tasks из Sheets не создаются; удаление — только в delete_task
    for f in ("gtasks.py", "my_tasks.py"):
        src = (REPO_ROOT / f).read_text(encoding="utf-8")
        assert ".insert(" not in src, f
    gsrc = (REPO_ROOT / "gtasks.py").read_text(encoding="utf-8")
    assert gsrc.count(".delete(") == 1
    assert "delete_task(" not in (REPO_ROOT / "main.py").read_text(encoding="utf-8")


# ══ 10. Финальные исправления аудита: верификация, дата GT, ручные правки ══

def _proto(status="Выполнено", task="Сводка", assignee="Петров Иван"):
    return ["1", "10.11.2026", task, assignee, "25.11.2026", status, "m7"]


def _set_proto_status(book, task, status):
    for row in book.data["Планёрка"]:
        if row[2] == task:
            row[5] = status


def test_protocol_verification_only_by_manual_change():
    """1–4: новая задача протокола «Выполнено» → «Требует верификации», verified=0;
    изменения протокола не подтверждают и не перетирают; подтверждает только
    ручная смена статуса в «Мои задачи»."""
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR, _proto("Выполнено")]})
        _sync(mt, ms, db, book, with_gtasks=False)
        key = book.rows(MY_TITLE)["Сводка"][7]
        assert key.startswith("p:") and book.rows(MY_TITLE)["Сводка"][6] == "Требует верификации"
        rec = db.my_tasks_get(1, key)
        assert rec["status"] == "Требует верификации" and rec["verified"] == 0
        # протокол меняется туда-сюда — личная задача остаётся неподтверждённой
        for st in ("В работе", "Отменено", "Выполнено", "Просрочено"):
            _set_proto_status(book, "Сводка", st)
            _sync(mt, ms, db, book, with_gtasks=False)
            assert book.rows(MY_TITLE)["Сводка"][6] == "Требует верификации", st
            assert db.my_tasks_get(1, key)["verified"] == 0
        # ручное изменение в «Мои задачи» → подтверждение
        book.set_status(MY_TITLE, "Сводка", "В работе")
        _sync(mt, ms, db, book, with_gtasks=False)
        rec = db.my_tasks_get(1, key)
        assert rec["status"] == "В работе" and rec["verified"] == 1
        # после подтверждения протокол статус пользователя не перетирает
        for st in ("Выполнено", "Требует верификации", "Отменено"):
            _set_proto_status(book, "Сводка", st)
            _sync(mt, ms, db, book, with_gtasks=False)
            assert book.rows(MY_TITLE)["Сводка"][6] == "В работе", st
        # явный ручной возврат к «Требует верификации» — снова не подтверждена
        book.set_status(MY_TITLE, "Сводка", "Требует верификации")
        _sync(mt, ms, db, book, with_gtasks=False)
        assert db.my_tasks_get(1, key)["verified"] == 0
        assert book.ws["Планёрка"].update.call_count == 0          # протокол не трогаем


def test_protocol_verification_db_restore_and_migration():
    """Восстановление БД: статус пользователя из листа V2 сохраняется;
    старый лист V1 (копия протокола) подтверждением не считается."""
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR, _proto("Требует верификации"),
                                   _proto("Требует верификации", task="Вторая")]})
        _sync(mt, ms, db, book, with_gtasks=False)
        book.set_status(MY_TITLE, "Сводка", "В работе")           # подтвердил
        _sync(mt, ms, db, book, with_gtasks=False)
    with tempfile.TemporaryDirectory() as tmp2:                    # «потеряли» БД
        db2 = _tmp_db(tmp2)
        _user(db2)
        res = _sync(mt, ms, db2, book, with_gtasks=False)
        assert res["outcome"] == mt.OUT_OK
        rows = book.rows(MY_TITLE)
        assert rows["Сводка"][6] == "В работе" and rows["Вторая"][6] == "Требует верификации"
        key = rows["Сводка"][7]
        assert db2.my_tasks_get(1, key)["verified"] == 1
        assert db2.my_tasks_get(1, rows["Вторая"][7])["verified"] == 0
    # V1: статусы — копия протокола → «Требует верификации»
    with tempfile.TemporaryDirectory() as tmp3:
        db3 = _tmp_db(tmp3)
        _user(db3)
        legacy = [ms.MY_TASKS_HEADER_ROW,
                  [1, "10.11.2026", "Планёрка", "Сводка", "Петров Иван", "25.11.2026",
                   "Выполнено", "m7"]]
        book = _Book({"Планёрка": [PROTO_HDR, _proto("Выполнено")], MY_TITLE: legacy})
        _sync(mt, ms, db3, book, with_gtasks=False)
        assert book.rows(MY_TITLE)["Сводка"][6] == "Требует верификации"
        assert db3.my_tasks_load(1)[0]["verified"] == 0


def test_gtasks_task_date_not_invented():
    """5–6: дата постановки GT не подставляется (ни импорт, ни updated);
    сохранённая дата не стирается."""
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR]})
        gt = _FakeGT([_gt("old", title="Старая", updated="2024-01-05T10:00:00.000Z",
                          links=[{"type": "email", "description": "Письмо",
                                  "link": "https://mail.google.com/mail/#all/x"}])])
        _sync(mt, ms, db, book, gt)
        assert book.rows(MY_TITLE)["Старая"][1] == ""
        assert db.my_tasks_get(1, "gtasks:L1:old")["task_date"] == ""
        # запись прошлой версии с датой → повторная синхронизация её не стирает
        db.my_tasks_upsert(1, [dict(db.my_tasks_get(1, "gtasks:L1:old"), task_date="01.11.2026")])
        _sync(mt, ms, db, book, gt)
        _sync(mt, ms, db, book, gt)
        assert db.my_tasks_get(1, "gtasks:L1:old")["task_date"] == "01.11.2026"
        # дату, введённую пользователем в листе, тоже не стираем
        for row in book.data[MY_TITLE][1:]:
            if row[3].startswith("Старая"):
                row[1] = "03.11.2026"
        _sync(mt, ms, db, book, gt)
        _sync(mt, ms, db, book, gt)
        assert book.rows(MY_TITLE)["Старая"][1] == "03.11.2026"


def test_manual_field_edits_preserved():
    """Правки названия, срока, ответственного, даты в листе не перезаписываются
    ни следующей синхронизацией, ни изменениями источника."""
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR, _proto("Требует верификации")]})
        gt = _FakeGT([_gt("a", title="Купить билеты", due="2026-11-20T00:00:00.000Z")])
        mt.add_forwarded(1, {"title": "Позвонить", "assigner": "Мария", "task_date":
                             "11.11.2026", "deadline": "", "source_key": "tg:p"}, dbm=db)
        _sync(mt, ms, db, book, gt)
        for row in book.data[MY_TITLE][1:]:
            if row[3] == "Купить билеты":
                row[3], row[5] = "Купить билеты в Казань", "18.11.2026"
            elif row[3] == "Сводка":
                row[3], row[4] = "Сводка за ноябрь", "Петров И. и Сидоров"
            elif row[3] == "Позвонить":
                row[5] = "13.11.2026"
        res = _sync(mt, ms, db, book, gt)
        assert res["outcome"] == mt.OUT_OK and res["stats"]["manual_field_edits"] == 5
        rows = book.rows(MY_TITLE)
        assert rows["Купить билеты в Казань"][5] == "18.11.2026"
        assert rows["Сводка за ноябрь"][4] == "Петров И. и Сидоров"
        assert rows["Позвонить"][5] == "13.11.2026"
        # источник меняется — правка пользователя важнее; строка не теряется
        gt.items[0]["title"] = "Билеты (переименовано в GT)"
        for row in book.data["Планёрка"]:
            row[4] = "30.11.2026" if row[2] == "Сводка" else row[4]
        for _ in range(2):
            _sync(mt, ms, db, book, gt)
        rows = book.rows(MY_TITLE)
        assert set(rows) == {"Купить билеты в Казань", "Сводка за ноябрь", "Позвонить"}, set(rows)
        assert rows["Купить билеты в Казань"][5] == "18.11.2026"
        assert rows["Сводка за ноябрь"][5] == "30.11.2026"        # срок не правили → из протокола
        assert db.my_tasks_get(1, "gtasks:L1:a")["title"] == "Билеты (переименовано в GT)"
        raw = sqlite3.connect(db.DB_PATH).execute(
            "SELECT overrides, sheet_fields FROM my_tasks WHERE source_key='gtasks:L1:a'").fetchone()
        assert all(v.startswith("enc:v1:") and "Казань" not in v for v in raw)


def test_concurrent_edit_any_field_conflict():
    """7–8: правка названия/срока/ответственного/статуса между чтением и записью →
    конфликт, правка сохранена, запись и удаление в GT не выполняются."""
    ms, mt = _load_ms(), _load_my_tasks()
    for col, val in ((3, "Новое название"), (5, "01.12.2026"), (4, "Сидоров А."),
                     (6, "Выполнено"), (1, "02.11.2026")):
        with tempfile.TemporaryDirectory() as tmp:
            db = _tmp_db(tmp)
            _user(db)
            book = _Book({"Планёрка": [PROTO_HDR]})
            gt = _FakeGT([_gt("a", title="A"), _gt("b", title="B")])
            _sync(mt, ms, db, book, gt)
            book.set_status(MY_TITLE, "A", "Отменено")           # запланировано удаление

            def edit(bk, col=col, val=val):
                for row in bk.data[MY_TITLE][1:]:
                    if row[3] == "B":
                        row[col] = val
            book.on_reread = edit
            upd = book.ws[MY_TITLE].update.call_count
            res = _sync(mt, ms, db, book, gt)
            assert res["outcome"] == mt.OUT_TRANSIENT, (col, res["text"])
            assert book.ws[MY_TITLE].update.call_count == upd, col
            assert gt.deletes() == [] and not [c for c in gt.calls if c[0] == "status"], col
            row_b = next(r for r in book.data[MY_TITLE][1:] if r[7] == "gtasks:L1:b")
            assert row_b[col] == val, col
            # следующая сверка: правка принята и сохранена, удаление выполнено
            book.on_reread = None
            res = _sync(mt, ms, db, book, gt)
            assert res["outcome"] == mt.OUT_OK, (col, res["text"])
            row_b = next(r for r in book.data[MY_TITLE][1:] if r[7] == "gtasks:L1:b")
            assert row_b[col] == val, (col, row_b)
            assert gt.deletes() == [("delete", "L1", "a")], col


def test_db_restore_keeps_sheet_edits_without_destructive_ops():
    """9: БД потеряна/восстановлена из старого снапшота — правки и статусы листа
    сохраняются, удалений в GT нет, записей не меньше, чем строк."""
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR, _proto("Требует верификации")]})
        gt = _FakeGT([_gt("a", title="A"), _gt("b", title="B"), _gt("c", title="C")])
        _sync(mt, ms, db, book, gt)
        book.set_status(MY_TITLE, "A", "Отменено")
        book.set_status(MY_TITLE, "B", "Выполнено")
        for row in book.data[MY_TITLE][1:]:
            if row[3] == "C":
                row[3] = "C — уточнено"
    with tempfile.TemporaryDirectory() as tmp2:
        db2 = _tmp_db(tmp2)
        _user(db2)
        gt.calls.clear()
        for _ in range(3):
            _sync(mt, ms, db2, book, gt)
        rows = book.rows(MY_TITLE)
        assert rows["A"][6] == "Отменено" and rows["B"][6] == "Выполнено"
        assert "C — уточнено" in rows and len(rows) == 4
        assert gt.deletes() == [] and not [c for c in gt.calls if c[0] == "status"]
        assert len(db2.my_tasks_load(1)) == 4


def test_no_phantom_override_after_gt_push():
    """После записи листа и push в GT записанные значения не откатываются:
    следующие переименования в GT отображаются (нет «фантомной» правки)."""
    ms, mt = _load_ms(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR]})
        gt = _FakeGT([_gt("a", title="A1")])
        _sync(mt, ms, db, book, gt)
        book.set_status(MY_TITLE, "A1", "Выполнено")        # → push в GT
        gt.items[0]["title"] = "A2"                         # и переименование в GT
        _sync(mt, ms, db, book, gt)
        assert ("status", "L1", "a", "completed") in gt.calls
        gt.items[0].update(title="A3", status="completed")
        _sync(mt, ms, db, book, gt)
        rows = book.rows(MY_TITLE)
        assert set(rows) == {"A3"}, set(rows)
        rec = db.my_tasks_get(1, "gtasks:L1:a")
        assert not mt._json(rec.get("overrides")) and rec["sheet_status"] == "Выполнено"


TESTS = [
    (test_match_assignee_reliable_only, "Протоколы: только достоверное распознавание"),
    (test_user_name_parts, "Протоколы: разбор ФИО пользователя"),
    (test_parse_deadline_unambiguous_only, "Срок: только однозначный"),
    (test_forward_info_user_origin_and_deadline, "Telegram: постановщик/дата/срок/дедуп"),
    (test_forward_info_other_origins_and_dedup_safety, "Telegram: источники, медиа, альбом, без даты"),
    (test_forward_info_real_ptb_objects, "Telegram: реальные объекты PTB"),
    (test_forward_handler_filter_and_order, "Telegram: фильтр и порядок хендлеров"),
    (test_forward_during_mymeet_wizard_creates_task, "Telegram: пересылка во время мастера"),
    (test_db_upgrade_existing_database_preserves_data, "БД: обновление существующей базы"),
    (test_db_encryption_dedup_and_rekey, "БД: шифрование, дедуп, rekey, состояние"),
    (test_wrong_key_no_data_loss, "БД: неверный ключ — без потери данных"),
    (test_reconcile_gtasks_import_two_way_no_loops, "GT: импорт, двусторонние статусы, без циклов"),
    (test_reconcile_gtasks_composite_key_move_and_legacy, "GT: составной ключ, перенос, миграция"),
    (test_reconcile_gtasks_missing_requires_confirmation, "GT: пропавшие — только с подтверждением"),
    (test_sheet_three_sources_verification_end_to_end, "Лист: три источника и верификация"),
    (test_cancel_in_sheet_deletes_gt_task_once, "Отмена → адресное удаление в GT один раз"),
    (test_cancel_deletion_safety_cases, "Отмена: импорт/миграция/привязка/повтор/сбой"),
    (test_mass_cancel_in_sheet_no_deletes, "Отмена: массовая — без удалений"),
    (test_deleted_in_gt_becomes_cancelled_after_confirmation, "Удаление в GT → «Отменено»"),
    (test_concurrent_manual_edit_not_overwritten, "Гонка: ручная правка не затирается"),
    (test_sheet_write_failure_and_shrink_keep_data, "Лист: ошибка записи / сжатие"),
    (test_legacy_sheet_migration_and_legacy_mode, "Лист: миграция старого формата"),
    (test_run_sync_outcomes_and_retry_no_duplicates, "run_sync: итоги, повтор без дублей"),
    (test_daily_schedule_slots_and_retries, "19:00: слоты, повторы, уведомления"),
    (test_record_daily_result_marks_only_success, "19:00: успех отмечается только при ok"),
    (test_user_lock_serializes_runs, "Блокировка: один пользователь — по очереди"),
    (test_gtasks_api_wrappers, "gtasks: листинг, delete/get/patch, повторы"),
    (test_auth_scopes_no_gmail_and_no_gt_insert, "OAuth без Gmail; задачи в GT не создаются"),
    (test_protocol_verification_only_by_manual_change, "Протоколы: подтверждение только вручную"),
    (test_protocol_verification_db_restore_and_migration, "Протоколы: восстановление БД, лист V1"),
    (test_gtasks_task_date_not_invented, "GT: дата постановки не выдумывается"),
    (test_manual_field_edits_preserved, "Лист: ручные правки полей сохраняются"),
    (test_concurrent_edit_any_field_conflict, "Гонка: правка любого поля → конфликт"),
    (test_db_restore_keeps_sheet_edits_without_destructive_ops, "Восстановление БД: правки целы"),
    (test_no_phantom_override_after_gt_push, "Лист: нет фантомных правок после push"),
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
    print("\n✅ Все тесты «Мои задачи» прошли")
