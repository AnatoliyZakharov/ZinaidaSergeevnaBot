"""Regression tests for Telegram handlers, with synthetic data and mocked services."""

import asyncio
from datetime import datetime, time, timedelta
import html
import io
import logging
import re
import signal
import threading
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytz

from test_my_tasks import _load_main_async


def _message():
    return NS(reply_text=AsyncMock(), edit_text=AsyncMock())


def _calendar_handlers(freebusy_query, user):
    service = NS(freebusy=lambda: NS(query=freebusy_query))
    return _load_main_async(
        ["get_free_slots", "_fmt_slot", "_tz", "_work_start", "_work_end",
         "_get_calendar_id", "_local_work_boundary", "cmd_sethours"],
        {"pytz": pytz, "os": NS(environ={}), "time": time, "timedelta": timedelta,
         "db": NS(get_user=lambda _: user, update_user_settings=Mock()),
         "get_calendar_service": lambda _: service},
        assigns=("DEFAULT_TZ", "DEFAULT_WORK_START", "DEFAULT_WORK_END"),
    )


def test_workday_ends_at_following_midnight():
    query = Mock(return_value=NS(execute=lambda: {"calendars": {"primary": {"busy": []}}}))
    user = {"timezone": "Europe/Berlin", "calendar_id": None, "work_start": 9, "work_end": 24}
    ns = _calendar_handlers(query, user)

    update = NS(effective_user=NS(id=1), message=_message())
    asyncio.run(ns["cmd_sethours"](update, NS(args=["9", "24"])))
    ns["db"].update_user_settings.assert_called_once_with(1, work_start=9, work_end=24)

    assert ns["get_free_slots"](1, datetime(2026, 11, 18)) == ["09:00 — 00:00  (15ч)"]
    assert query.call_args.kwargs["body"]["timeMax"] == "2026-11-19T00:00:00+01:00"
    user["work_end"] = 18
    assert ns["get_free_slots"](1, datetime(2026, 11, 18)) == ["09:00 — 18:00  (9ч)"]
    assert query.call_args.kwargs["body"]["timeMax"] == "2026-11-18T18:00:00+01:00"


def test_workday_dst_duration_and_zero_start():
    query = Mock(return_value=NS(execute=lambda: {"calendars": {"primary": {"busy": []}}}))
    user = {"timezone": "Europe/Berlin", "calendar_id": None, "work_start": 0, "work_end": 24}
    ns = _calendar_handlers(query, user)
    for day, start, end, hours in (
        (datetime(2026, 3, 29), "+01:00", "+02:00", 23),
        (datetime(2026, 10, 25), "+02:00", "+01:00", 25),
    ):
        assert ns["get_free_slots"](1, day) == [f"00:00 — 00:00  ({hours}ч)"]
        body = query.call_args.kwargs["body"]
        assert body["timeMin"] == f"{day.date()}T00:00:00{start}"
        assert body["timeMax"] == f"{(day + timedelta(days=1)).date()}T00:00:00{end}"


def test_html_tasks_and_skip():
    complete = Mock(return_value=True)
    ns = _load_main_async(
        ["_format_tasks", "_handle_pending_tasks"],
        {"html_lib": html, "re": re, "gtasks": NS(complete_task=complete)},
        assigns=("SKIP_TASK_RE",),
    )
    title = r'Проверить _*()-.!\ <b>&>'
    tasks = [{"title": "Первая", "id": "one", "_listId": "inbox", "due": "2026-11-19T10:00:00Z"},
             {"title": title, "id": "two", "_listId": "inbox"}]
    formatted = ns["_format_tasks"](tasks)
    assert html.escape(title) in formatted and "2026-11-19" in formatted
    assert title not in formatted and "<b>" in formatted and "<i>" in formatted
    message = _message()
    context = NS(user_data={"tasks": tasks})
    assert asyncio.run(ns["_handle_pending_tasks"](1, "скип 2", NS(message=message), context))
    complete.assert_called_once_with(1, "two", "inbox")
    assert [task["id"] for task in context.user_data["tasks"]] == ["one"]
    response = message.reply_text.call_args
    assert response.kwargs["parse_mode"] == "HTML"
    assert html.escape(title) in response.args[0] and "Первая" in response.args[0]


def test_plain_contact_names_and_addresses():
    name, email = r'Имя_*()-.!\ <i>&>', "mock_user@example.test"
    database = Mock()
    database.list_contacts.return_value = [{"display_name": name, "email": email}]
    ns = _load_main_async(
        ["cmd_contacts", "cmd_add_contact"],
        {"html_lib": html, "db": database, "re": re},
        assigns=("EMAIL_PATTERN",),
    )
    update = NS(effective_user=NS(id=1), message=_message())
    asyncio.run(ns["cmd_contacts"](update, NS()))
    result = update.message.reply_text.call_args
    assert "parse_mode" not in result.kwargs
    assert name in result.args[0] and email in result.args[0]
    asyncio.run(ns["cmd_add_contact"](update, NS(args=[name, email])))
    database.add_contact.assert_called_once_with(1, name, email)
    result = update.message.reply_text.call_args
    assert "parse_mode" not in result.kwargs
    assert name in result.args[0]


def test_slow_voice_does_not_block_another_user():
    async def scenario():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        all_updates_continued = asyncio.Event()
        release = threading.Event()
        background = []
        query = AsyncMock()

        def transcribe(audio):
            assert audio == b"synthetic audio"
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3)
            return "тестовое сообщение"

        def create_task(coro, *, update):
            background.append(asyncio.create_task(coro))
            return background[-1]

        ns = _load_main_async(
            ["handle_voice", "_process_voice"],
            {"_require_google": AsyncMock(return_value=True), "transcribe": transcribe,
             "TELEMOST_TRIGGER": re.compile(r"(?!)"), "CREATE_TRIGGER": re.compile(r"(?!)"),
             "process_query": query, "logger": Mock()},
            assigns=("_active_voice_users", "_MAX_ACTIVE_VOICES"),
        )
        first_message = _message()
        first_message.reply_text.return_value = first_message
        first_message.voice = NS(file_id="mock-audio-file")
        first = NS(effective_user=NS(id=1), message=first_message)
        file = NS(download_as_bytearray=AsyncMock(return_value=bytearray(b"synthetic audio")))
        context = NS(application=NS(running=True, create_task=create_task),
                     bot=NS(get_file=AsyncMock(return_value=file)))

        async def next_update():
            await started.wait()
            await query(2, "обычный текст", NS(effective_user=NS(id=2)))
            all_updates_continued.set()

        other = asyncio.create_task(next_update())
        try:
            await ns["handle_voice"](first, context)
            await asyncio.wait_for(all_updates_continued.wait(), 1)
            assert len(background) == 1 and not background[0].done()
            assert query.await_args.args[:2] == (2, "обычный текст")
            assert ns["_active_voice_users"] == {1}
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(other, *background), 2)
        assert query.await_args.args == (1, "тестовое сообщение", first)
        assert not ns["_active_voice_users"]

    asyncio.run(scenario())


def test_mymeet_sync_command_errors_do_not_disclose_exception():
    private = "sensitive synthetic request data"
    database = NS(upsert_user=Mock(side_effect=ValueError(private)))
    log_output = io.StringIO()
    logger = logging.Logger("synthetic-mymeet-command")
    logger.addHandler(logging.StreamHandler(log_output))
    ns = _load_main_async(
        ["cmd_sync_mymeet", "cmd_sync_mymeet_full"],
        {"db": database, "logger": logger},
    )
    for handler in ns["cmd_sync_mymeet"], ns["cmd_sync_mymeet_full"]:
        update = NS(effective_user=NS(id=1, username=None), message=_message())
        asyncio.run(handler(update, NS()))
        update.message.reply_text.assert_awaited_once()
        assert private not in update.message.reply_text.await_args.args[0]
    assert private not in log_output.getvalue()
    assert "ValueError" in log_output.getvalue()


def test_incomplete_analytics_returns_generic_failure():
    ns = _load_main_async(
        ["cmd_analytics", "_format_analytics"],
        {"ADMIN_TELEGRAM_ID": 1, "ANALYTICS_REPORT_TZ": "Europe/Moscow",
         "db": NS(analytics_report_data=lambda: {"started_at": None}),
         "logger": Mock(), "pytz": pytz},
    )
    update = NS(effective_user=NS(id=1), message=_message())
    asyncio.run(ns["cmd_analytics"](update, NS()))
    update.message.reply_text.assert_awaited_once()
    response = update.message.reply_text.await_args.args[0]
    assert "Не удалось построить отчёт" in response and "mau" not in response
    ns["logger"].error.assert_called_once_with("analytics report failed: %s", "KeyError")


def _lifecycle_environment(events, initialization_error=None, setup_error=None):
    async def initialize():
        events.append("initialize")
        if initialization_error:
            raise initialization_error

    async def runner_setup():
        events.append("runner.setup")
        if setup_error:
            raise setup_error

    def action(name, owner=None, running=None):
        def perform(*args, **kwargs):
            events.append(name)
            if owner is not None:
                owner.running = running
        return perform

    updater = NS(running=False)
    updater.start_polling = AsyncMock(side_effect=action("polling.start", updater, True))
    updater.stop = AsyncMock(side_effect=action("polling.stop", updater, False))
    app = NS(bot=object(), updater=updater, running=False)
    app.initialize = AsyncMock(side_effect=initialize)
    app.start = AsyncMock(side_effect=action("app.start", app, True))
    app.stop = AsyncMock(side_effect=action("app.stop", app, False))
    app.shutdown = AsyncMock(side_effect=action("app.shutdown"))
    scheduler = NS(running=False)
    scheduler.start = Mock(side_effect=action("scheduler.start", scheduler, True))
    scheduler.pause = Mock(side_effect=action("scheduler.pause"))
    scheduler.shutdown = Mock(side_effect=action("scheduler.shutdown", scheduler, False))
    runner = NS(setup=runner_setup, cleanup=AsyncMock(side_effect=action("runner.cleanup")))
    site = NS(start=AsyncMock(side_effect=action("site.start")),
              stop=AsyncMock(side_effect=action("site.stop")))
    requests = (NS(shutdown=AsyncMock()), NS(shutdown=AsyncMock()))
    signals = NS(SIGTERM=signal.SIGTERM, SIGINT=signal.SIGINT,
                 getsignal=Mock(return_value=signal.SIG_DFL), signal=Mock())
    ns = _load_main_async(
        ["_run_bot_lifecycle"],
        {"signal": signals, "web": NS(TCPSite=Mock(return_value=site)),
         "OAUTH_CALLBACK_PORT": 8080, "logger": Mock(), "_bot": None,
         "_drain_my_tasks_refresh": AsyncMock(side_effect=action("debounce.drain"))},
    )
    return ns, app, scheduler, runner, site, requests


def test_shutdown_signals_drain_active_writes():
    async def scenario(sig):
        from unittest.mock import patch

        events = []
        ns, app, scheduler, runner, site, requests = _lifecycle_environment(events)
        handled_signals = {}
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        may_finish = asyncio.Event()

        async def write(name):
            await may_finish.wait()
            events.append(name)

        jobs = {asyncio.create_task(write("scheduled-write"))}
        callbacks = {asyncio.create_task(write("oauth-write"))}
        with patch.object(loop, "add_signal_handler", side_effect=handled_signals.__setitem__), \
                patch.object(loop, "remove_signal_handler", return_value=True):
            lifecycle = asyncio.create_task(
                ns["_run_bot_lifecycle"](app, scheduler, runner, stop_event, jobs, callbacks, requests)
            )
            for _ in range(10):
                await asyncio.sleep(0)
                if scheduler.running:
                    break
            assert scheduler.running
            handled_signals[sig]()
            try:
                for _ in range(10):
                    await asyncio.sleep(0)
                    if site.stop.await_count:
                        break
                assert site.stop.await_count == 1
                assert not lifecycle.done() and all(not job.cancelled() for job in jobs | callbacks)
                assert events.index("polling.stop") < events.index("app.stop")
                assert "scheduler.shutdown" not in events and "app.shutdown" not in events
            finally:
                may_finish.set()
                await asyncio.wait_for(lifecycle, 2)
        for phase in ("polling.stop", "app.stop", "scheduler.pause", "scheduler.shutdown",
                      "runner.cleanup", "app.shutdown", "debounce.drain"):
            assert events.count(phase) == 1, events
        assert events.index("scheduled-write") < events.index("scheduler.shutdown")
        assert events.index("oauth-write") < events.index("runner.cleanup")
        assert events.index("runner.cleanup") < events.index("app.shutdown")
        assert ns["_bot"] is None

    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.run(scenario(sig))


def test_partial_startup_stops_started_components():
    async def scenario(initialization_error):
        from unittest.mock import patch

        events = []
        failure = RuntimeError("synthetic startup failure")
        ns, app, scheduler, runner, site, requests = _lifecycle_environment(
            events, failure if initialization_error else None,
            None if initialization_error else failure,
        )
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler"), patch.object(loop, "remove_signal_handler"):
            try:
                await ns["_run_bot_lifecycle"](
                    app, scheduler, runner, asyncio.Event(), set(), set(), requests,
                )
            except RuntimeError as exc:
                assert exc is failure
            else:
                raise AssertionError("startup failure was not propagated")
        assert "runner.cleanup" in events and "scheduler.start" not in events
        assert ns["_bot"] is None
        if initialization_error:
            assert all(client.shutdown.await_count == 1 for client in requests)
            assert events[:1] == ["initialize"] and "app.stop" not in events
        else:
            assert events.index("polling.stop") < events.index("app.stop") < events.index("app.shutdown")

    asyncio.run(scenario(initialization_error=True))
    asyncio.run(scenario(initialization_error=False))


def test_debounce_replaces_waiting_and_drains_running_tasks():
    async def scenario():
        writes = []
        release = asyncio.Event()

        async def sync(user_id, with_gtasks):
            writes.append((user_id, with_gtasks))
            await release.wait()
            return {"outcome": "ok"}

        waiting = {}
        running = {}
        ns = _load_main_async(
            ["_schedule_my_tasks_refresh", "_drain_my_tasks_refresh"],
            {"_my_tasks_refresh_tasks": waiting, "_my_tasks_refresh_running": running,
             "_my_tasks_refresh_stopping": False, "MY_TASKS_REFRESH_DELAY": 0,
             "_run_my_tasks_sync": sync, "logger": Mock()},
        )
        schedule = ns["_schedule_my_tasks_refresh"]
        schedule(1)
        old = waiting[1]
        schedule(1)
        new = waiting[1]
        assert old is not new
        await asyncio.gather(old, return_exceptions=True)
        for _ in range(10):
            await asyncio.sleep(0)
            if writes:
                break
        assert writes == [(1, False)] and not waiting and running == {1: new}
        schedule(1)
        replaced = waiting[1]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert waiting[1] is replaced and running[1] is new
        schedule(1)
        follow_up = waiting[1]
        await asyncio.gather(replaced, return_exceptions=True)
        assert replaced.cancelled() and not new.cancelled()
        drain = asyncio.create_task(ns["_drain_my_tasks_refresh"]())
        await asyncio.sleep(0)
        assert not drain.done() and not new.cancelled()
        release.set()
        await asyncio.wait_for(drain, 2)
        assert follow_up.cancelled()
        assert not waiting and not running and writes == [(1, False)]

    asyncio.run(scenario())


TESTS = [
    (test_workday_ends_at_following_midnight, "слоты: /sethours 9 24 и обычное окончание рабочего дня"),
    (test_workday_dst_duration_and_zero_start, "слоты: нулевой час и длительность дня при DST"),
    (test_html_tasks_and_skip, "Telegram: спецсимволы, даты и выбор задачи через «скип»"),
    (test_plain_contact_names_and_addresses, "Telegram: безопасное форматирование контактов"),
    (test_slow_voice_does_not_block_another_user, "SpeechKit: медленный запрос другого пользователя не блокирует"),
    (test_mymeet_sync_command_errors_do_not_disclose_exception, "MYmeet: безопасные ответы и диагностика ошибок"),
    (test_incomplete_analytics_returns_generic_failure, "аналитика: неполный отчёт обрабатывается безопасно"),
    (test_shutdown_signals_drain_active_writes, "остановка: SIGTERM/SIGINT дожидаются записи"),
    (test_partial_startup_stops_started_components, "остановка: ресурсы после частичного запуска"),
    (test_debounce_replaces_waiting_and_drains_running_tasks, "debounce: замена и завершение задач"),
]
