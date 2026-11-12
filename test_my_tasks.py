"""
Тесты «Мои задачи»: Google Tasks ↔ лист, пересланные сообщения, распознавание
исполнителей в протоколах.  Без сети: Google Sheets / Tasks — фейки, БД — temp.
Запуск: python test_my_tasks.py  (также входит в python test_smoke.py)
"""
import ast
import importlib.util
import os
import pathlib
import sys
import tempfile
import types
from datetime import date, datetime, timezone
from types import SimpleNamespace as NS

REPO_ROOT = pathlib.Path(__file__).parent

import test_smoke as _ts  # noqa: E402  (helpers: _load_mymeet, _load_db, _FakeBook …)

PROTO_HDR = _ts._PROTO_HDR
ME = "Иван Петров"


def _load_my_tasks():
    spec = importlib.util.spec_from_file_location("my_tasks_test", REPO_ROOT / "my_tasks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ══ 1. Распознавание исполнителя ═════════════════════════════════════════

def test_match_assignee_variants():
    mt = _load_my_tasks()
    m = mt.match_assignee
    # точное
    assert m("Иван Петров", ME) == "exact"
    assert m("  иван   ПЕТРОВ ", ME) == "exact"
    # порядок слов, падежи, ё
    for a in ("Петров Иван", "Петрову Ивану", "Петрова Ивана", "Петровым Иваном",
              "Ивану Петрову"):
        assert m(a, ME) == "fuzzy", a
    assert m("Семёнову", "Пётр Семёнов") == "fuzzy"
    # сокращения/инициалы
    for a in ("Петров И.", "И. Петров", "Петров И.С.", "И.С. Петров", "Петров ИС",
              "Петров", "Петрову", "Иван"):
        assert m(a, "Иван Сергеевич Петров") == "fuzzy", a
    # женские фамилии и имена в падежах
    for a in ("Анне Смирновой", "Смирновой А.", "Смирнова", "Анной Смирновой"):
        assert m(a, "Анна Смирнова") == "fuzzy", a
    # несколько исполнителей
    assert m("Сидоров А., Петров И.", ME) == "fuzzy"
    assert m("Сидоров и Петров", ME) == "fuzzy"
    # не совпадает
    for a in ("Петров А.", "Иванов", "Петренко", "Иван Сидоров", "Анна", "", "П.",
              "Петров Иван Сергеевич Лишний", "Speaker A"):
        assert m(a, ME) is None, a
    assert m("Петров", "") is None
    # Speaker A — только при allow_speaker (встреча пользователя)
    for a in ("Speaker A", "speaker b", "Speaker 1", "Спикер 2", "Участник C"):
        assert m(a, ME, allow_speaker=True) == "speaker", a
    assert m("Speakerphone", ME, allow_speaker=True) is None


# ══ 2. Срок из текста ════════════════════════════════════════════════════

def test_parse_deadline():
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
        "через день": "12.11.2026",
        "через неделю": "18.11.2026",
        "через 2 недели": "25.11.2026",
        "через месяц": "11.12.2026",
        "к пятнице": "13.11.2026",
        "до пятницы": "13.11.2026",
        "в понедельник созвон": "16.11.2026",
        "до среды": "18.11.2026",          # тот же день недели → следующая
        "во вторник": "17.11.2026",
        "до 15.11": "15.11.2026",
        "к 05.01": "05.01.2027",           # прошедшая дата без года → следующий год
        "срок 20.12.2026": "20.12.2026",
        "дедлайн: 1/12": "01.12.2026",
        "к 20 ноября": "20.11.2026",
        "до 3 марта 2027 года": "03.03.2027",
        "до конца недели": "13.11.2026",
        "до конца месяца": "30.11.2026",
        "завтра или через неделю": "12.11.2026",   # первое по тексту
        "Нужно позавтракать": "",
        "встреча прошла 15.11": "",        # дата без предлога — не срок
        "до 31.02": "",                    # несуществующая дата
        "": "",
    }
    for text, want in cases.items():
        assert p(text) == want, (text, p(text), want)
    # Пятница → «к пятнице» = следующая пятница
    assert mt.parse_deadline("к пятнице", date(2026, 11, 13)) == date(2026, 11, 20)


# ══ 3. Пересланные сообщения ═════════════════════════════════════════════

def _origin_user(dt, uid=7, name="Мария Иванова", username="mivanova"):
    return NS(type="user", date=dt, sender_user=NS(id=uid, full_name=name, username=username))


def _msg(origin, text=None, caption=None, media_group_id=None, **kw):
    return NS(forward_origin=origin, text=text, caption=caption,
              media_group_id=media_group_id, date=datetime(2026, 11, 12, 9, tzinfo=timezone.utc),
              **kw)


def test_forward_info_user_origin_and_deadline():
    mt = _load_my_tasks()
    odt = datetime(2026, 11, 9, 21, 30, tzinfo=timezone.utc)    # 10.11 00:30 МСК (вторник)
    info = mt.forward_info(_msg(_origin_user(odt), text="Подготовь КП к пятнице"),
                           "Europe/Moscow")
    assert info["assigner"] == "Мария Иванова (@mivanova)"
    assert info["task_date"] == "10.11.2026", info        # дата оригинала, пояс пользователя
    assert info["deadline"] == "13.11.2026", info         # пятница от даты оригинала
    assert info["title"] == "Подготовь КП к пятнице"
    assert info["source_key"].startswith("tg:")
    # тот же оригинал → тот же ключ (дедуп), другой текст → другой
    again = mt.forward_info(_msg(_origin_user(odt), text="Подготовь КП к пятнице"),
                            "Europe/Moscow")
    assert again["source_key"] == info["source_key"]
    other = mt.forward_info(_msg(_origin_user(odt), text="Другое"), "Europe/Moscow")
    assert other["source_key"] != info["source_key"]


def test_forward_info_other_origins():
    mt = _load_my_tasks()
    dt = datetime(2026, 11, 12, 6, tzinfo=timezone.utc)
    hidden = mt.forward_info(_msg(NS(type="hidden_user", date=dt, sender_user_name="Аноним"),
                                  text="завтра позвонить"), "Europe/Moscow")
    assert hidden["assigner"] == "Аноним" and hidden["deadline"] == "13.11.2026"
    ch = NS(type="channel", date=dt, chat=NS(id=-100, title="Новости"), message_id=55,
            author_signature="Редактор")
    c1 = mt.forward_info(_msg(ch, caption="Фото с подписью"), "Europe/Moscow")
    c2 = mt.forward_info(_msg(ch, caption="Фото с подписью (изменено)"), "Europe/Moscow")
    assert c1["assigner"] == "Новости (Редактор)"
    assert c1["source_key"] == c2["source_key"]        # канал: id сообщения однозначен
    grp = NS(type="chat", date=dt, sender_chat=NS(id=-5, title="Команда"), author_signature=None)
    assert mt.forward_info(_msg(grp, text="x"), "UTC")["assigner"] == "Команда"
    # медиа без подписи → понятный заголовок; альбом без подписи → пропуск
    photo = mt.forward_info(_msg(_origin_user(dt), photo=[1]), "UTC")
    assert photo["title"] == "Пересланное сообщение (photo) от Мария Иванова (@mivanova)"
    assert mt.forward_info(_msg(_origin_user(dt), media_group_id="g1", photo=[1]), "UTC") is None
    # не переслано
    assert mt.forward_info(NS(forward_origin=None, text="x"), "UTC") is None
    # длинный текст обрезается
    long = mt.forward_info(_msg(_origin_user(dt), text="я" * 5000), "UTC")
    assert len(long["title"]) == mt.TITLE_MAX_LEN


def test_forward_info_real_ptb_objects():
    """Реальные объекты python-telegram-bot (если библиотека установлена)."""
    try:
        from telegram import (Chat, Message, MessageOriginChannel, MessageOriginHiddenUser,
                              MessageOriginUser, User)
    except ImportError:
        return
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


# ══ 4. БД ════════════════════════════════════════════════════════════════

def _tmp_db(tmp):
    db = _ts._load_db(tmp)
    db.init_db()
    return db


def test_db_my_tasks_tables_idempotent_and_dedup():
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        conn = sqlite3.connect(db.DB_PATH)
        users_cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
        conn.close()
        db.init_db()                                    # повторно — без ошибок
        conn = sqlite3.connect(db.DB_PATH)
        assert [r[1] for r in conn.execute("PRAGMA table_info(users)")] == users_cols
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert {"my_tasks", "my_meetings", "my_tasks_sync"} <= tables
        rec = {"source": "telegram", "source_key": "tg:abc", "title": "T", "status": "В работе"}
        assert db.my_tasks_insert_new(1, rec) is True
        assert db.my_tasks_insert_new(1, rec) is False          # дубль
        assert db.my_tasks_insert_new(2, rec) is True           # другой пользователь
        assert len(db.my_tasks_load(1)) == 1
        db.my_tasks_upsert(1, [dict(rec, title="T2", status="Выполнено")])
        got = db.my_tasks_load(1)
        assert len(got) == 1 and got[0]["title"] == "T2" and got[0]["status"] == "Выполнено"
        db.my_tasks_mark_written(1, {"tg:abc": "Выполнено"})
        assert db.my_tasks_load(1)[0]["sheet_status"] == "Выполнено"
        assert db.my_tasks_load(2)[0]["sheet_status"] is None
        db.my_meetings_add(1, ["m1", "m1", "m2", ""])
        assert sorted(db.my_meetings_get(1)) == ["m1", "m2"]
        assert db.my_tasks_daily_get(1) is None
        db.my_tasks_daily_set(1, "2026-11-12")
        db.my_tasks_daily_set(1, "2026-11-13")
        assert db.my_tasks_daily_get(1) == "2026-11-13"


# ══ 5. Сверка с Google Tasks (чистая логика) ═════════════════════════════

def _gt(tid, title="Задача", status="needsAction", due=None, list_id="L1"):
    it = {"id": tid, "title": title, "status": status, "_listId": list_id, "_listTitle": "Мои"}
    if due:
        it["due"] = due
    return it


def _reconcile(mt, records, items, complete=True):
    stats, warnings = {}, []
    pushes = mt.reconcile_gtasks(records, items, complete, ME, "12.11.2026", stats, warnings)
    for r in records.values():
        r.pop("_dirty", None)
    return pushes, stats, warnings


def test_reconcile_gtasks_import_and_two_way():
    mt = _load_my_tasks()
    recs = {}
    pushes, stats, _ = _reconcile(mt, recs, [
        _gt("a", due="2026-11-20T00:00:00.000Z"), _gt("b", status="completed"),
        _gt("a"), _gt("c", title="  ")])                       # дубль и пустой заголовок
    assert pushes == [] and stats["gtasks_new"] == 2 and set(recs) == {"gtasks:a", "gtasks:b"}
    assert recs["gtasks:a"]["status"] == "В работе" and recs["gtasks:a"]["deadline"] == "20.11.2026"
    assert recs["gtasks:b"]["status"] == "Выполнено"
    assert recs["gtasks:a"]["origin"] == "Google Tasks — Мои"
    # повтор без изменений — ничего не меняется и не отправляется
    pushes, stats, _ = _reconcile(mt, recs, [_gt("a"), _gt("b", status="completed")])
    assert pushes == [] and not stats.get("gtasks_new") and not stats.get("gtasks_status_in")
    # изменение в GT → в «Мои задачи»
    pushes, stats, _ = _reconcile(mt, recs, [_gt("a", status="completed"),
                                             _gt("b", status="needsAction")])
    assert pushes == [] and stats["gtasks_status_in"] == 2
    assert recs["gtasks:a"]["status"] == "Выполнено" and recs["gtasks:b"]["status"] == "В работе"
    # локальные изменения → в GT
    recs["gtasks:a"]["status"] = "В работе"       # переоткрыта
    recs["gtasks:b"]["status"] = "Отменено"
    pushes, _, _ = _reconcile(mt, recs, [_gt("a", status="completed"), _gt("b")])
    assert sorted(pushes) == [("gtasks:a", "L1", "a", "needsAction"),
                              ("gtasks:b", "L1", "b", "completed")], pushes
    # локальное «Просрочено» при needsAction в GT — отправлять нечего
    recs["gtasks:b"].update(status="Просрочено", ext_status="needsAction",
                            ext_synced_status="В работе")
    pushes, _, _ = _reconcile(mt, recs, [_gt("a", status="needsAction"), _gt("b")])
    assert pushes == []
    # «В работе» → «Просрочено» локально, а в GT задачу закрыли → принимаем GT,
    # НЕ переоткрываем задачу в GT
    a = recs["gtasks:a"]
    a.update(status="Просрочено", ext_status="needsAction", ext_synced_status="В работе")
    pushes, _, _ = _reconcile(mt, recs, [_gt("a", status="completed", title="Новое имя"),
                                         _gt("b")])
    assert pushes == [] and a["status"] == "Выполнено", (pushes, a)
    assert a["title"] == "Новое имя"
    # неудачный push (ext_synced_status=None) → повторная отправка
    a.update(status="В работе", ext_status="completed", ext_synced_status=None)
    pushes, _, _ = _reconcile(mt, recs, [_gt("a", status="completed"), _gt("b")])
    assert pushes == [("gtasks:a", "L1", "a", "needsAction")], pushes


def test_reconcile_gtasks_deletions_guard():
    mt = _load_my_tasks()
    recs = {}
    items = [_gt(str(i)) for i in range(20)]
    _reconcile(mt, recs, items)
    # 2 пропали — помечаем «Отменено»
    pushes, stats, w = _reconcile(mt, recs, items[2:])
    assert stats["gtasks_missing"] == 2 and not w
    assert recs["gtasks:0"]["status"] == "Отменено" and recs["gtasks:0"]["missing_since"]
    assert pushes == []                                  # в GT ничего не удаляем/не меняем
    # вернулась — снимаем отметку
    _reconcile(mt, recs, items)
    assert recs["gtasks:0"]["missing_since"] is None
    # пропали почти все — массовое удаление: ничего не меняем
    before = {k: r["status"] for k, r in recs.items()}
    pushes, stats, w = _reconcile(mt, recs, items[:2])
    assert stats["gtasks_mass_missing"] == 18 and any("массового" in x for x in w), w
    assert {k: r["status"] for k, r in recs.items()} == before
    # пустой ответ API — тоже массовое
    _, stats, _ = _reconcile(mt, recs, [])
    assert stats.get("gtasks_mass_missing") == 20
    # неполный ответ — пропавшие не трогаем
    _, stats, w = _reconcile(mt, recs, items[1:], complete=False)
    assert not stats.get("gtasks_missing") and any("неполный" in x for x in w)
    assert all(r["status"] != "Отменено" or k in ("gtasks:0", "gtasks:1")
               for k, r in recs.items())


# ══ 6. Лист «Мои задачи»: сквозные сценарии с фейковой таблицей и temp-БД ═

class _Book(_ts._FakeBook):
    """_FakeBook, который отдаёт A:H для диапазонов «!A:H» и хранит метаданные защиты."""

    def __init__(self, sheets):
        super().__init__(sheets)
        self.protected = {}

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
            width = 8 if suffix == "A:H" else 7
            out.append({"values": [row[:width] for row in self.data[t]]})
        return {"valueRanges": out}

    def set_status(self, title, task, status):
        for row in self.data[title][1:]:
            if row[3] == task:
                row[6] = status
                return
        raise AssertionError(f"нет строки {task}")

    def rows(self, title):
        return {r[3]: r for r in self.data[title][1:]}

    def requests(self):
        return [r for c in self.ss.batch_update.call_args_list for r in c.args[0]["requests"]]


MY_TITLE = f"Мои задачи — {ME}"


def _proto_book():
    return _Book({
        "Планёрка": [
            PROTO_HDR,
            ["1", "10.11.2026", "Отчёт по продажам", "Петров И.", "20.11.2026", "Требует верификации", "m1"],
            ["2", "10.11.2026", "Бюджет", "Иван Петров", "", "В работе", "m1"],
            ["3", "10.11.2026", "Чужая задача", "Анна Смирнова", "", "Требует верификации", "m1"],
            ["4", "10.11.2026", "Неизвестно чья", "Speaker A", "", "Требует верификации", "m1"],
            ["5", "10.11.2026", "Чужая встреча", "Speaker A", "", "Требует верификации", "m9"],
        ],
    })


class _Pusher:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, telegram_id, list_id, task_id, gt_status):
        self.calls.append((list_id, task_id, gt_status))
        return task_id not in self.fail


def _builder(mt, db, gtasks=None, pusher=None):
    return mt.MyTasksBuilder(1, ME, gtasks=gtasks, pusher=pusher, dbm=db, tz_name="Europe/Moscow")


def test_my_tasks_sheet_three_sources_end_to_end():
    ms, mt = _ts._load_mymeet(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        db.upsert_user(1, "u")
        db.set_full_name(1, ME)
        db.my_meetings_add(1, ["m1"])
        mt.add_forwarded(1, {"title": "Позвонить клиенту", "assigner": "Мария", "task_date":
                             "11.11.2026", "deadline": "12.11.2026", "source_key": "tg:x1"}, dbm=db)
        book = _proto_book()
        gt_items = [_gt("g1", title="Купить билеты"), _gt("g2", title="Старое", status="completed")]
        b = _builder(mt, db, gtasks=(gt_items, True), pusher=_Pusher())
        w, cnt, title = ms._sync_my_tasks_and_colors(book.ss, ME, rows_builder=b)
        assert title == MY_TITLE and cnt == 6, (cnt, w, book.data.get(MY_TITLE))
        data = book.data[MY_TITLE]
        assert data[0] == ms.MY_TASKS_HEADER_ROW_V2
        rows = book.rows(MY_TITLE)
        # протокол: нечёткое совпадение и Speaker A своей встречи → «Требует верификации»
        assert rows["Отчёт по продажам"][6] == "Требует верификации"
        assert rows["Отчёт по продажам"][4] == "Петров И."
        assert rows["Неизвестно чья"][6] == "Требует верификации"
        # статус, уже выставленный в протоколе вручную, сохраняется
        assert rows["Бюджет"][6] == "В работе"
        assert "Чужая задача" not in rows and "Чужая встреча" not in rows
        # Google Tasks и Telegram
        assert rows["Купить билеты"][2] == "Google Tasks — Мои" and rows["Купить билеты"][6] == "В работе"
        assert rows["Купить билеты"][7] == "gtasks:g1"
        assert rows["Старое"][6] == "Выполнено"
        assert rows["Позвонить клиенту"][2] == "Telegram — Мария"
        assert rows["Позвонить клиенту"][1] == "11.11.2026" and rows["Позвонить клиенту"][5] == "12.11.2026"
        assert rows["Позвонить клиенту"][7] == "tg:x1"
        assert rows["Бюджет"][7] == "m1"                     # mtg_id как раньше
        # редактируемый режим: выпадающий список статусов в G, защита без колонки статуса
        reqs = book.requests()
        dv = [r["setDataValidation"]["range"] for r in reqs if "setDataValidation" in r]
        assert any(g["startColumnIndex"] == 6 and g["sheetId"] == book.ws[MY_TITLE].id for g in dv)
        prot = [r["addProtectedRange"]["protectedRange"] for r in reqs if "addProtectedRange" in r]
        assert len(prot) == 1 and prot[0]["unprotectedRanges"][0]["startColumnIndex"] == 6
        assert prot[0]["warningOnly"]
        # № по порядку, без дублей
        assert [r[0] for r in data[1:]] == list(range(1, 7))

        # ── повтор без изменений: тот же результат, без дублей и push ──
        pusher = _Pusher()
        snapshot = [list(r) for r in book.data[MY_TITLE]]
        b = _builder(mt, db, gtasks=(gt_items, True), pusher=pusher)
        ms._sync_my_tasks_and_colors(book.ss, ME, rows_builder=b)
        assert book.data[MY_TITLE] == snapshot and pusher.calls == []
        assert len(db.my_tasks_load(1)) == 6 + 0          # 3 протокольных + 2 GT + 1 TG

        # ── ручная верификация в «Мои задачи» + правка статуса задачи GT ──
        book.set_status(MY_TITLE, "Отчёт по продажам", "В работе")
        book.set_status(MY_TITLE, "Неизвестно чья", "Отменено")      # не моя
        book.set_status(MY_TITLE, "Купить билеты", "Выполнено")
        book.set_status(MY_TITLE, "Позвонить клиенту", "Выполнено")
        pusher = _Pusher()
        b = _builder(mt, db, gtasks=(gt_items, True), pusher=pusher)
        ms._sync_my_tasks_and_colors(book.ss, ME, rows_builder=b)
        rows = book.rows(MY_TITLE)
        assert rows["Отчёт по продажам"][6] == "В работе"
        assert rows["Неизвестно чья"][6] == "Отменено"
        assert rows["Купить билеты"][6] == "Выполнено"
        assert rows["Позвонить клиенту"][6] == "Выполнено"
        assert pusher.calls == [("L1", "g1", "completed")], pusher.calls
        recs = {r["source_key"]: r for r in db.my_tasks_load(1)}
        verified = [r for r in recs.values() if r["title"] == "Отчёт по продажам"]
        assert verified[0]["verified"] == 1

        # ── MYmeet-синк без Google Tasks: правки сохраняются, GT не трогается ──
        b = _builder(mt, db)
        ms._sync_my_tasks_and_colors(book.ss, ME, rows_builder=b)
        rows = book.rows(MY_TITLE)
        assert rows["Купить билеты"][6] == "Выполнено" and rows["Отчёт по продажам"][6] == "В работе"

        # ── статус изменён в листе протокола → принимается в «Мои задачи» ──
        for row in book.data["Планёрка"]:
            if row[2] == "Бюджет":
                row[5] = "Выполнено"
        ms._sync_my_tasks_and_colors(book.ss, ME, rows_builder=_builder(mt, db))
        assert book.rows(MY_TITLE)["Бюджет"][6] == "Выполнено"
        # протокол не перезаписан ботом
        assert book.ws["Планёрка"].update.call_count == 0


def test_my_tasks_sheet_shrink_guard():
    """Лист с ≥10 строками не перезаписывается, если строк стало < половины."""
    ms, mt = _ts._load_mymeet(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        old = [ms.MY_TASKS_HEADER_ROW_V2] + [
            [i, "01.11.2026", "Google Tasks", f"Старая {i}", ME, "", "В работе", f"gtasks:z{i}"]
            for i in range(1, 13)]
        book = _Book({"Планёрка": [PROTO_HDR], MY_TITLE: old})
        w, cnt, _ = ms._sync_my_tasks_and_colors(book.ss, ME, rows_builder=_builder(mt, db))
        assert cnt is None and any("массового удаления" in x for x in w), w
        assert book.ws[MY_TITLE].update.call_count == 0
        assert book.ws[MY_TITLE].batch_clear.call_count == 0
        assert db.my_tasks_load(1) == [] or all(r["sheet_status"] is None
                                                for r in db.my_tasks_load(1))


def test_my_tasks_legacy_sheet_protection_switched_to_editable():
    ms, mt = _ts._load_mymeet(), _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        legacy = [ms.MY_TASKS_HEADER_ROW,
                  [1, "10.11.2026", "Планёрка", "Бюджет", ME, "", "В работе", "m1"]]
        book = _Book({"Планёрка": [PROTO_HDR, ["1", "10.11.2026", "Бюджет", ME, "",
                                              "В работе", "m1"]], MY_TITLE: legacy})
        book.protected[MY_TITLE] = [
            {"protectedRangeId": 77, "description": "Генерируется ботом при синхронизации. "
                                                    "Статус меняйте в листе протокола."},
            {"protectedRangeId": 78, "description": "Чужая защита"}]
        w, cnt, _ = ms._sync_my_tasks_and_colors(book.ss, ME, rows_builder=_builder(mt, db))
        assert cnt == 1, w
        upd = [r["updateProtectedRange"] for r in book.requests() if "updateProtectedRange" in r]
        assert len(upd) == 1 and upd[0]["protectedRange"]["protectedRangeId"] == 77
        assert upd[0]["protectedRange"]["unprotectedRanges"][0]["startColumnIndex"] == 6
        assert book.data[MY_TITLE][0] == ms.MY_TASKS_HEADER_ROW_V2
        # Цвета ставятся на лист с V2-заголовком у других пользователей
        assert ms._is_my_tasks_header(ms.MY_TASKS_HEADER_ROW_V2[:7])


def test_legacy_mode_unchanged_without_builder():
    """Без builder — прежний режим: старый заголовок, без dropdown, ключ H = mtg_id."""
    ms = _ts._load_mymeet()
    book = _Book({"Планёрка": [PROTO_HDR, ["1", "10.11.2026", "Отчёт", "Петров И.", "",
                                          "В работе", "m1"],
                               ["2", "10.11.2026", "Бюджет", ME, "", "В работе", "m1"]]})
    _, cnt, _ = ms._sync_my_tasks_and_colors(book.ss, ME)
    assert cnt == 1                                     # нечёткое совпадение только с builder
    assert book.data[MY_TITLE][0] == ms.MY_TASKS_HEADER_ROW
    assert not [r for r in book.requests() if "setDataValidation" in r]


# ══ 7. Оркестрация run_sync ══════════════════════════════════════════════

class _FakeGT:
    def __init__(self, items=None, err=None, complete=True):
        self.items, self.err, self.complete = items or [], err, complete
        self.pushed = []

    def list_all_tasks_full(self, telegram_id):
        return ([dict(i) for i in self.items], self.err, self.complete)

    def set_task_status(self, telegram_id, list_id, task_id, gt_status):
        self.pushed.append((task_id, gt_status))
        return True


def test_run_sync_without_sheet_and_errors():
    mt = _load_my_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        db.upsert_user(1, "u")
        ms = types.SimpleNamespace(refresh_my_tasks_sheet=lambda *a: (_ for _ in ()).throw(
            AssertionError("лист не настроен — не должен вызываться")))
        res = mt.run_sync(1, with_gtasks=True, dbm=db, gt=_FakeGT([_gt("a")]), ms=ms)
        assert "новых 1" in res["text"] and "/setup_mymeet" in res["text"], res
        assert [r["source_key"] for r in db.my_tasks_load(1)] == ["gtasks:a"]
        # повтор — без дублей
        mt.run_sync(1, with_gtasks=True, dbm=db, gt=_FakeGT([_gt("a")]), ms=ms)
        assert len(db.my_tasks_load(1)) == 1
        # нет scope Tasks — предупреждение, ничего не помечено удалённым
        res = mt.run_sync(1, with_gtasks=True, dbm=db, gt=_FakeGT(err="no_scope"), ms=ms)
        assert "/connect" in res["text"]
        assert db.my_tasks_load(1)[0]["status"] == "В работе"
        # ошибка листа не роняет сверку
        db.set_full_name(1, ME)
        import sqlite3
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("UPDATE users SET mymeet_spreadsheet_id='S' WHERE telegram_id=1")
        conn.commit()
        conn.close()
        boom = types.SimpleNamespace(refresh_my_tasks_sheet=lambda *a: (_ for _ in ()).throw(
            RuntimeError("sheets down")))
        res = mt.run_sync(1, with_gtasks=True, dbm=db,
                          gt=_FakeGT([_gt("a", status="completed")]), ms=boom)
        assert "sheets down" in res["text"]
        assert db.my_tasks_load(1)[0]["status"] == "Выполнено"


# ══ 8. gtasks.py: пагинация, без удалений; auth: без Gmail ═══════════════

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
    return mod, HttpError


class _GTService:
    def __init__(self, lists, pages, fail_list=None, err_cls=None):
        self.lists, self.pages, self.fail_list, self.err_cls = lists, pages, fail_list, err_cls
        self.calls = []

    def tasklists(self):
        svc = self

        class _TL:
            def list(self, maxResults, pageToken=None):
                return _ts._Exec(lambda: svc.lists[pageToken or 0])
        return _TL()

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
                svc.calls.append(("patch", kw))
                return _ts._Exec(lambda: {})

            def delete(self, **kw):           # не должен вызываться
                raise AssertionError("delete called")
        return _T()


def test_gtasks_list_all_full_pagination_and_status():
    gt, HttpError = _load_gtasks()
    lists = {0: {"items": [{"id": "L1", "title": "Работа"}], "nextPageToken": "p2"},
             "p2": {"items": [{"id": "L2", "title": "Дом"}]}}
    pages = {("L1", None): {"items": [{"id": "1", "title": "a"}], "nextPageToken": "n"},
             ("L1", "n"): {"items": [{"id": "2", "title": "b", "status": "completed"},
                                     {"id": "3", "title": "c", "deleted": True}]},
             ("L2", None): {"items": [{"id": "4", "title": "d"}]}}
    svc = _GTService(lists, pages, err_cls=HttpError)
    items, err, complete = gt.list_all_tasks_full(1, service=svc)
    assert err is None and complete and [i["id"] for i in items] == ["1", "2", "4"]
    assert items[2]["_listTitle"] == "Дом"
    kw = [c[3] for c in svc.calls if c[0] == "list"][0]
    assert kw["showCompleted"] and kw["showHidden"] and not kw["showDeleted"]
    # сбой одного списка → complete=False
    svc = _GTService(lists, pages, fail_list="L2", err_cls=HttpError)
    items, err, complete = gt.list_all_tasks_full(1, service=svc)
    assert err is None and not complete and len(items) == 2
    # статусы
    svc = _GTService(lists, pages, err_cls=HttpError)
    assert gt.set_task_status(1, "L1", "1", "completed", service=svc)
    assert gt.set_task_status(1, "L1", "1", "needsAction", service=svc)
    patches = [c[1] for c in svc.calls if c[0] == "patch"]
    assert patches[0]["body"] == {"status": "completed"}
    assert patches[1]["body"] == {"status": "needsAction", "completed": None}
    assert not gt.set_task_status(1, "L1", "1", "deleted", service=svc)
    assert not hasattr(gt, "delete_task")


def test_auth_scopes_no_gmail():
    tree = ast.parse((REPO_ROOT / "auth.py").read_text(encoding="utf-8"))
    scopes = None
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "SCOPES" for t in n.targets):
            scopes = ast.literal_eval(n.value)
    assert scopes and "https://www.googleapis.com/auth/tasks" in scopes
    assert not any("gmail" in s or "mail.google" in s for s in scopes), scopes


# ══ 9. main.py: расписание 19:00 по поясу пользователя ═══════════════════

def test_daily_due_user_timezone():
    from zoneinfo import ZoneInfo
    ns = _ts._load_main_funcs(
        ["_my_tasks_daily_due"],
        extra_ns={"datetime": datetime, "pytz": None, "DEFAULT_TZ": "Europe/Moscow",
                  "_tz": lambda u: ZoneInfo(u["timezone"])},
        assigns=("MY_TASKS_DAILY_HOUR",))
    due = ns["_my_tasks_daily_due"]
    t = datetime(2026, 11, 12, 16, 5, tzinfo=timezone.utc)    # 19:05 МСК, 17:05 Берлин
    assert due({"timezone": "Europe/Moscow"}, t) == "2026-11-12"
    assert due({"timezone": "Europe/Berlin"}, t) is None
    assert due({"timezone": "Europe/Berlin"},
               datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)) == "2026-11-12"
    # Владивосток: 19:00 местного = 09:00 UTC; дата — местная
    assert due({"timezone": "Asia/Vladivostok"},
               datetime(2026, 11, 12, 9, 30, tzinfo=timezone.utc)) == "2026-11-12"
    assert due({"timezone": "Europe/Moscow"},
               datetime(2026, 11, 12, 17, 0, tzinfo=timezone.utc)) is None    # 20:00


TESTS = [
    (test_match_assignee_variants, "Мои задачи: распознавание исполнителя"),
    (test_parse_deadline, "Мои задачи: срок из текста"),
    (test_forward_info_user_origin_and_deadline, "Telegram: постановщик/дата/срок/дедуп"),
    (test_forward_info_other_origins, "Telegram: скрытый/канал/чат/медиа/альбом"),
    (test_forward_info_real_ptb_objects, "Telegram: реальные объекты PTB"),
    (test_db_my_tasks_tables_idempotent_and_dedup, "БД: новые таблицы, дедуп, users не тронут"),
    (test_reconcile_gtasks_import_and_two_way, "Google Tasks: импорт и двусторонние статусы"),
    (test_reconcile_gtasks_deletions_guard, "Google Tasks: защита от массового удаления"),
    (test_my_tasks_sheet_three_sources_end_to_end, "Лист: три источника, верификация, повтор"),
    (test_my_tasks_sheet_shrink_guard, "Лист: защита от массового удаления строк"),
    (test_my_tasks_legacy_sheet_protection_switched_to_editable, "Лист: старая защита → статус редактируем"),
    (test_legacy_mode_unchanged_without_builder, "Лист: без builder — прежний режим"),
    (test_run_sync_without_sheet_and_errors, "run_sync: без листа, ошибки API/листа"),
    (test_gtasks_list_all_full_pagination_and_status, "gtasks: пагинация, статусы, без удаления"),
    (test_auth_scopes_no_gmail, "OAuth: нет Gmail scopes"),
    (test_daily_due_user_timezone, "Расписание: 19:00 по поясу пользователя"),
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
