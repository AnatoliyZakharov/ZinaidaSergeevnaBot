"""
Тесты issue #32: классификация ошибок Google, честные тексты отказа Google Tasks,
проверка выданных scopes при OAuth.  Без сети: HTTP-ответы Google — реальные
JSON-тела из документации/логов (номер проекта и email — фиктивные), БД — временная.
Запуск: python test_google_errors.py  (также входит в python test_smoke.py)
"""
import asyncio
import base64
import hashlib
import html
import importlib.util
import json
import logging
import os
import socket
import sqlite3
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import test_my_tasks as _tm
from test_my_tasks import (ME, MY_TITLE, PROTO_HDR, REPO_ROOT, _Book, _FakeGT, _gt,  # noqa: F401
                           _load_main_async, _load_ms, _load_my_tasks, _sync, _tmp_db, _user)

SCOPE_CAL = "https://www.googleapis.com/auth/calendar"
SCOPE_TASKS = "https://www.googleapis.com/auth/tasks"
SCOPE_EMAIL = "https://www.googleapis.com/auth/userinfo.email"

# Секреты/PII, которые не должны попасть в логи (фиктивные значения)
LEAK_MARKERS = ("project=123", "projects/123", "extendedHelp", "has not been used",
                "FAKE-ACCESS-TOKEN-LEAKED", "leak.user@example.com", "console.developers.google.com",
                "Request had", "Insufficient Permission")

# ── Реальные тела ответов Google (структура как в production-логах / docs) ──
BODY_DISABLED_FULL = {"error": {
    "code": 403,
    "message": "Google Tasks API has not been used in project 123 before or it is disabled. "
               "Enable it by visiting https://console.developers.google.com/apis/api/"
               "tasks.googleapis.com/overview?project=123 then retry.",
    "errors": [{
        "message": "Google Tasks API has not been used in project 123 before or it is disabled.",
        "domain": "usageLimits", "reason": "accessNotConfigured",
        "extendedHelp": "https://console.developers.google.com"}],
    "status": "PERMISSION_DENIED",
    "details": [
        {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED",
         "domain": "googleapis.com",
         "metadata": {"service": "tasks.googleapis.com", "consumer": "projects/123",
                      "activationUrl": "https://console.developers.google.com/apis/api/"
                                       "tasks.googleapis.com/overview?project=123"}},
        {"@type": "type.googleapis.com/google.rpc.Help", "links": [{
            "description": "Google developers console API activation",
            "url": "https://console.developers.google.com/apis/api/tasks.googleapis.com/"
                   "overview?project=123"}]}],
}}
BODY_DISABLED_ERRORS_ONLY = {"error": {
    "code": 403, "message": "Access Not Configured. project=123",
    "errors": [{"message": "Access Not Configured", "domain": "usageLimits",
                "reason": "accessNotConfigured", "extendedHelp": "https://console.developers.google.com"}]}}
BODY_DISABLED_DETAILS_ONLY = {"error": {
    "code": 403, "message": "API disabled for projects/123", "status": "PERMISSION_DENIED",
    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                 "reason": "SERVICE_DISABLED", "domain": "googleapis.com"}]}}
BODY_SCOPE = {"error": {
    "code": 403, "message": "Request had insufficient authentication scopes.",
    "errors": [{"message": "Insufficient Permission", "domain": "global",
                "reason": "insufficientPermissions"}],
    "status": "PERMISSION_DENIED",
    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                 "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "domain": "googleapis.com",
                 "metadata": {"service": "tasks.googleapis.com",
                              "method": "google.tasks.v1.TasksService.ListTaskLists"}}]}}
BODY_SCOPE_DETAILS_ONLY = {"error": {
    "code": 403, "message": "Request had insufficient authentication scopes.",
    "status": "PERMISSION_DENIED",
    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                 "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}
BODY_401 = {"error": {
    "code": 401,
    "message": "Request had invalid authentication credentials. Expected OAuth 2 access token, "
               "login cookie or other valid authentication credential. FAKE-ACCESS-TOKEN-LEAKED",
    "errors": [{"message": "Invalid Credentials", "domain": "global", "reason": "authError",
                "location": "Authorization", "locationType": "header"}],
    "status": "UNAUTHENTICATED"}}
BODY_RATE = {"error": {"code": 403, "message": "Rate Limit Exceeded",
                       "errors": [{"message": "Rate Limit Exceeded", "domain": "usageLimits",
                                   "reason": "rateLimitExceeded"}],
                       "status": "PERMISSION_DENIED"}}
BODY_USER_RATE = {"error": {"code": 403, "message": "User Rate Limit Exceeded",
                            "errors": [{"message": "User Rate Limit Exceeded",
                                        "domain": "usageLimits",
                                        "reason": "userRateLimitExceeded"}]}}
BODY_429 = {"error": {"code": 429, "message": "Quota exceeded for quota metric",
                      "status": "RESOURCE_EXHAUSTED",
                      "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                   "reason": "RATE_LIMIT_EXCEEDED"}]}}
BODY_500 = {"error": {"code": 500, "message": "Backend Error",
                      "errors": [{"message": "Backend Error", "domain": "global",
                                  "reason": "backendError"}]}}
BODY_403_UNKNOWN = {"error": {"code": 403, "message": "The caller does not have permission",
                              "errors": [{"message": "Forbidden", "domain": "global",
                                          "reason": "forbidden"}],
                              "status": "PERMISSION_DENIED"}}
HTML_503 = b"<html><head><title>503 Service Unavailable</title></head><body>Error</body></html>"


class FakeHttpError(Exception):
    """Как googleapiclient.errors.HttpError: resp.status + content (bytes).
    str() содержит URL с id задачи, email и токен — их не должно быть в логах."""

    def __init__(self, status, content=b""):
        if isinstance(content, dict):
            content = json.dumps(content).encode()
        self.resp = NS(status=status, reason="Forbidden")
        self.content = content
        super().__init__(f"<HttpError {status} when requesting https://tasks.googleapis.com/"
                         f"tasks/v1/lists/L1/tasks/T1?alt=json for leak.user@example.com "
                         f"FAKE-ACCESS-TOKEN-LEAKED project=123>")


# ── Загрузка настоящего auth.py (без сети, с временной БД) ───────────────

_TEST_ENV = {"GOOGLE_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
             "GOOGLE_CLIENT_SECRET": "test-client-secret",
             "OAUTH_REDIRECT_URI": "https://example.invalid/oauth/callback"}


def load_auth(db_mod=None):
    """Настоящий auth.py под отдельным именем; модуль db подменяется временной БД."""
    for k, v in _TEST_ENV.items():
        os.environ.setdefault(k, v)
    if db_mod is None:
        db_mod = types.ModuleType("db")
    saved = sys.modules.get("db")
    sys.modules["db"] = db_mod
    try:
        spec = importlib.util.spec_from_file_location("auth_test", REPO_ROOT / "auth.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved is None:
            sys.modules.pop("db", None)
        else:
            sys.modules["db"] = saved
    return mod


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def __enter__(self):
        root = logging.getLogger()
        self._old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self)
        return self

    def __exit__(self, *exc):
        root = logging.getLogger()
        root.removeHandler(self)
        root.setLevel(self._old_level)

    def text(self):
        return "\n".join(self.lines)


def _assert_no_leaks(text):
    for m in LEAK_MARKERS:
        assert m not in text, f"в логе утечка «{m}»: {text}"


# ══ 1. Классификатор ═════════════════════════════════════════════════════

def test_classify_http_errors():
    auth = load_auth()
    c = lambda st, body=b"": auth.classify_google_error(FakeHttpError(st, body))  # noqa: E731
    # API выключен в GCP-проекте — полный ответ, только errors, только details
    assert c(403, BODY_DISABLED_FULL) == ("api_disabled", 403, "accessNotConfigured")
    assert c(403, BODY_DISABLED_ERRORS_ONLY).category == "api_disabled"
    assert c(403, BODY_DISABLED_DETAILS_ONLY) == ("api_disabled", 403, "SERVICE_DISABLED")
    # не выдан scope
    assert c(403, BODY_SCOPE) == ("insufficient_scope", 403, "insufficientPermissions")
    assert c(403, BODY_SCOPE_DETAILS_ONLY).reason == "ACCESS_TOKEN_SCOPE_INSUFFICIENT"
    assert c(403, BODY_SCOPE_DETAILS_ONLY).category == "insufficient_scope"
    # токен
    assert c(401, BODY_401) == ("token_invalid", 401, "authError")
    assert c(401).category == "token_invalid"
    # временные: 403 rate limit — не «нет доступа»
    assert c(403, BODY_RATE) == ("transient", 403, "rateLimitExceeded")
    assert c(403, BODY_USER_RATE).category == "transient"
    assert c(429, BODY_429).category == "transient"
    assert c(500, BODY_500) == ("transient", 500, "backendError")
    for st in (500, 502, 503, 504, 429):
        assert c(st, HTML_503) == ("transient", st, None)       # HTML-тело — без падения
        assert c(st, b"") == ("transient", st, None)            # пустое тело
    # прочие 4xx (включая 403 без известного reason) — api_error, не «переподключите»
    assert c(403, BODY_403_UNKNOWN) == ("api_error", 403, "forbidden")
    assert c(403, b"").category == "api_error"
    assert c(403, HTML_503).category == "api_error"
    assert c(404, {"error": {"code": 404, "errors": [{"reason": "notFound"}]}}).category == "api_error"
    assert c(400, b"\xff\xfe not json").category == "api_error"
    assert c(403, b"[1, 2]").category == "api_error"             # JSON, но не объект
    assert c(403, {"error": "string"}).category == "api_error"
    # reason с мусором/длинный не проходит фильтр
    weird = {"error": {"errors": [{"reason": "project=123 x"}, {"reason": "A" * 65},
                                  {"reason": 42}], "status": "PERMISSION_DENIED"}}
    assert c(403, weird) == ("api_error", 403, "PERMISSION_DENIED")
    # str-content (не bytes)
    e = FakeHttpError(403)
    e.content = json.dumps(BODY_DISABLED_FULL)
    assert auth.classify_google_error(e).category == "api_disabled"
    # настоящий googleapiclient.errors.HttpError (если установлен локально)
    try:
        from googleapiclient.errors import HttpError
        import httplib2
    except ImportError:
        HttpError = None
    if HttpError is not None:
        resp = httplib2.Response({"status": "403"})
        real = HttpError(resp, json.dumps(BODY_DISABLED_FULL).encode(),
                         uri="https://tasks.googleapis.com/tasks/v1/users/@me/lists")
        assert auth.classify_google_error(real).category == "api_disabled"


def test_classify_auth_and_network_errors():
    auth = load_auth()
    from google.auth import exceptions as gex
    c = auth.classify_google_error
    inv = gex.RefreshError("invalid_grant: Token has been expired or revoked.",
                           {"error": "invalid_grant",
                            "error_description": "Token has been expired or revoked."})
    assert c(inv) == ("token_invalid", None, "invalid_grant")
    assert c(gex.RefreshError("x", {"error": "internal_failure"}, retryable=True)).category \
        == "transient"
    assert c(gex.RefreshError("x", "<html>502</html>")).category == "transient"
    assert c(gex.RefreshError("no args")).category == "transient"
    assert c(gex.RefreshError("x", {"error": "invalid_client"})) == ("api_error", None,
                                                                    "invalid_client")
    assert c(gex.RefreshError("x", {"error": "invalid_scope"})).category == "api_error"
    assert c(gex.TransportError("connection reset")).category == "transient"
    assert c(OSError("net down")).category == "transient"
    assert c(socket.timeout("timed out")).category == "transient"
    assert c(ConnectionResetError()).category == "transient"
    httplib2_err = type("ServerNotFoundError", (Exception,), {"__module__": "httplib2"})
    assert c(httplib2_err("dns")).category == "transient"
    assert c(ValueError("boom")).category == "api_error"
    assert c(KeyError("x")) == ("api_error", None, None)


def test_log_google_error_no_leaks():
    auth = load_auth()
    with _LogCapture() as cap:
        for st, body in ((403, BODY_DISABLED_FULL), (403, BODY_SCOPE), (401, BODY_401),
                         (403, BODY_RATE), (503, HTML_503)):
            err = auth.classify_google_error(FakeHttpError(st, body))
            auth.log_google_error("tasks.list", 1, err)
    t = cap.text()
    _assert_no_leaks(t)
    assert "category=api_disabled" in t and "reason=accessNotConfigured" in t
    assert "status=403" in t and "category=insufficient_scope" in t


# ══ 2. Credentials и refresh ═════════════════════════════════════════════

def _auth_db(tmp, refresh_token="rt-old", access="at-old", expiry=None):
    db = _tmp_db(tmp)
    db.upsert_user(1, "u")
    db.save_google_tokens(1, access, refresh_token,
                          time.time() - 10 if expiry is None else expiry)
    return db


def _tokens(db, tid=1):
    grant = db.get_google_grant(tid)
    return grant["access_token"], grant["refresh_token"], grant["expiry"]


def test_refresh_errors_classified_tokens_kept():
    from google.auth import exceptions as gex
    from google.oauth2.credentials import Credentials as RealCreds
    cases = [
        (gex.RefreshError("invalid_grant: revoked", {"error": "invalid_grant"}), "token_invalid"),
        (gex.RefreshError("x", {"error": "internal_failure"}, retryable=True), "transient"),
        (gex.TransportError("conn reset"), "transient"),
        (gex.RefreshError("x", {"error": "invalid_client"}), "api_error"),
    ]
    for exc, cat in cases:
        with tempfile.TemporaryDirectory() as tmp:
            db = _auth_db(tmp)
            auth = load_auth(db)
            made = []

            class _Creds(RealCreds):
                def __init__(self, *a, **kw):
                    made.append(kw)
                    super().__init__(*a, **kw)

                def refresh(self, request):
                    raise exc
            auth.Credentials = _Creds
            auth.GoogleRequest = lambda: None
            before = _tokens(db)
            with _LogCapture() as cap:
                creds, reason = auth.get_credentials_checked(1)
                assert auth.get_credentials(1) is None            # совместимость
            assert creds is None and reason == cat, (exc, reason)
            assert _tokens(db) == before                           # токены в БД не стёрты
            assert made and all("scopes" not in kw for kw in made), made
            _assert_no_leaks(cap.text())
            assert f"category={cat}" in cap.text()


def test_credentials_no_auth_and_success():
    from google.oauth2.credentials import Credentials as RealCreds
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        auth = load_auth(db)
        assert auth.get_credentials_checked(1) == (None, "no_auth")          # нет пользователя
        db.upsert_user(1, "u")
        assert auth.get_credentials_checked(1) == (None, "no_auth")          # нет токенов
        db.save_google_tokens(1, "at", "", 0)
        assert auth.get_credentials_checked(1) == (None, "no_auth")          # пустой refresh
        assert auth.get_credentials(1) is None
        # действующий токен — без refresh, scopes не задаются
        db.save_google_tokens(1, "at-ok", "rt", time.time() + 3600)
        creds, reason = auth.get_credentials_checked(1)
        assert reason is None and creds.token == "at-ok" and creds.scopes is None
        assert auth.get_credentials(1).token == "at-ok"
        # истёк — refresh успешен, access token обновлён, refresh_token тот же

        class _Creds(RealCreds):
            def refresh(self, request):
                self.token = "at-new"
                # naive UTC, как у google-auth; +24 ч — не зависит от локального пояса
                self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=24)
        auth.Credentials = _Creds
        auth.GoogleRequest = lambda: None
        db.save_google_tokens(1, "at-old", "rt", time.time() - 10)
        creds, reason = auth.get_credentials_checked(1)
        assert reason is None and creds.token == "at-new"
        at, rt, exp = _tokens(db)
        assert at == "at-new" and rt == "rt" and exp > time.time()


# ══ 3. gtasks: категории вместо no_scope ═════════════════════════════════

def _load_gtasks_real_auth(creds_result=(None, "no_auth")):
    auth = load_auth()
    gt, _ = _tm._load_gtasks()
    gt.auth = NS(classify_google_error=auth.classify_google_error,
                 log_google_error=auth.log_google_error,
                 get_credentials_checked=lambda tid: creds_result)
    return gt


class _RaisingService:
    """tasklists().list() бросает заданное исключение; tasks() не должен вызываться."""

    def __init__(self, exc, inner_exc=None):
        self.exc, self.inner_exc = exc, inner_exc

    def tasklists(self):
        svc = self

        class _TL:
            def list(self, **kw):
                if svc.inner_exc is not None:
                    return _tm._ts._Exec(lambda: {"items": [{"id": "L1", "title": "A"}]})
                return _tm._ts._Exec(lambda: (_ for _ in ()).throw(svc.exc))
        return _TL()

    def tasks(self):
        svc = self

        class _T:
            def list(self, **kw):
                return _tm._ts._Exec(lambda: (_ for _ in ()).throw(svc.inner_exc))
        return _T()


def test_gtasks_list_returns_categories():
    from google.auth import exceptions as gex
    gt = _load_gtasks_real_auth()
    cases = [(FakeHttpError(403, BODY_DISABLED_FULL), "api_disabled"),
             (FakeHttpError(403, BODY_SCOPE), "insufficient_scope"),
             (FakeHttpError(401, BODY_401), "token_invalid"),
             (FakeHttpError(403, BODY_RATE), "transient"),
             (FakeHttpError(503, HTML_503), "transient"),
             (FakeHttpError(403, b""), "api_error"),
             (gex.RefreshError("x", {"error": "invalid_grant"}), "token_invalid"),
             (gex.TransportError("reset"), "transient"),
             (ValueError("boom"), "api_error")]
    with _LogCapture() as cap:
        for exc, cat in cases:
            items, err, complete, ids = gt.list_all_tasks_full(1, service=_RaisingService(exc))
            assert (items, err, complete, ids) == ([], cat, False, []), (exc, err)
    _assert_no_leaks(cap.text())
    assert "no_scope" not in cap.text()
    # ошибка внутри цикла по спискам (не только HttpError) — неполный ответ, не падение
    with _LogCapture() as cap:
        items, err, complete, ids = gt.list_all_tasks_full(
            1, service=_RaisingService(None, inner_exc=gex.RefreshError("x", {"error": "invalid_grant"})))
        assert err is None and not complete and ids == ["L1"]
        items, err, complete, ids = gt.list_all_tasks_full(
            1, service=_RaisingService(None, inner_exc=FakeHttpError(403, BODY_DISABLED_FULL)))
        assert err is None and not complete
    _assert_no_leaks(cap.text())
    # нет авторизации / refresh упал — причина из get_credentials_checked
    for reason in ("no_auth", "token_invalid", "transient"):
        g = _load_gtasks_real_auth((None, reason))
        assert g.list_all_tasks_full(1) == ([], reason, False, [])
        assert g.list_all_tasks(1) == ([], reason)
        assert g.get_task_state(1, "L1", "t") == "error"        # не «удалена»
        assert g.delete_task(1, "L1", "t") == "error"
        assert g.set_task_status(1, "L1", "t", "completed") is False
    # list_all_tasks (/tasks)
    with _LogCapture() as cap:
        for exc, cat in cases:
            g = _load_gtasks_real_auth((object(), None))
            g.build = lambda *a, _e=exc, **k: _RaisingService(_e)
            assert g.list_all_tasks(1) == ([], cat)
    _assert_no_leaks(cap.text())
    # 401/403 у адресных операций — 'error', не 'deleted'/'absent'
    gt2, Http = _tm._load_gtasks()
    svc = lambda **k: _tm._GTService({0: {"items": []}}, {}, Http, **k)  # noqa: E731
    assert gt2.get_task_state(1, "L1", "1", service=svc(op_errors=[401])) == "error"
    assert gt2.get_task_state(1, "L1", "1", service=svc(op_errors=[403])) == "error"
    assert gt2.delete_task(1, "L1", "1", service=svc(op_errors=[401])) == "error"
    assert gt2.set_task_status(1, "L1", "1", "completed", service=svc(op_errors=[403])) is False


# ══ 4. run_sync: текст и итог по каждой категории, без внешних операций ══

def test_run_sync_each_category_no_external_ops():
    ms, mt = _load_ms(), _load_my_tasks()
    expect = {"no_auth": mt.OUT_CONFIG, "token_invalid": mt.OUT_CONFIG,
              "insufficient_scope": mt.OUT_CONFIG, "api_disabled": mt.OUT_CONFIG,
              "transient": mt.OUT_TRANSIENT, "api_error": mt.OUT_TRANSIENT}
    assert set(expect) == set(mt.GT_ERROR_TEXT)
    assert all(t.startswith("Google Tasks: ") for t in mt.GT_ERROR_TEXT.values())
    assert "/connect" not in mt.GT_ERROR_TEXT["api_disabled"]
    assert "администратор" in mt.GT_ERROR_TEXT["api_disabled"]
    for cat in ("no_auth", "token_invalid", "insufficient_scope"):
        assert "/connect" in mt.GT_ERROR_TEXT[cat]
    assert "/connect" not in mt.GT_ERROR_TEXT["transient"]
    assert "/connect" not in mt.GT_ERROR_TEXT["api_error"]
    with tempfile.TemporaryDirectory() as tmp:
        db = _tmp_db(tmp)
        _user(db)
        book = _Book({"Планёрка": [PROTO_HDR]})
        items = [_gt("g1", title="Удалить меня"), _gt("g2", title="Закрыть")]
        gt = _FakeGT(items)
        assert _sync(mt, ms, db, book, gt)["outcome"] == mt.OUT_OK
        # ручные правки: отмена (ждёт удаления в GT) и «Выполнено» (ждёт отправки статуса)
        book.set_status(MY_TITLE, "Удалить меня", "Отменено")
        book.set_status(MY_TITLE, "Закрыть", "Выполнено")
        _sync(mt, ms, db, book, gt, with_gtasks=False)
        snap = lambda: sorted((r["source_key"], r["status"], r.get("gt_delete_pending"),  # noqa: E731
                               r.get("ext_status"), r.get("ext_synced_status"))
                              for r in db.my_tasks_load(1))
        before = snap()
        assert any(r[1] == "Отменено" for r in before)
        for cat, out in expect.items():
            bad = _FakeGT(items, err=cat)
            res = _sync(mt, ms, db, book, bad)
            assert res["outcome"] == out, (cat, res["outcome"])
            assert res["gt_error"] == cat
            assert mt.GT_ERROR_TEXT[cat] in res["text"], res["text"]
            assert "✅" not in res["text"] and "нет доступа" not in res["text"]
            if cat == "api_disabled":
                assert "/connect" not in res["text"], res["text"]
                assert "администратора" in res["text"].split("\n")[0]
            assert bad.calls == [], (cat, bad.calls)       # ни get, ни delete, ни patch
            assert snap() == before, cat                   # статусы в БД не изменились
            assert book.rows(MY_TITLE)["Удалить меня"][6] == "Отменено"
        # неизвестная строка ошибки (старый код) и исключение — как api_error
        res = _sync(mt, ms, db, book, _FakeGT(items, err="no_scope"))
        assert res["outcome"] == mt.OUT_TRANSIENT and res["gt_error"] == "api_error"

        class _Boom(_FakeGT):
            def list_all_tasks_full(self, telegram_id):
                raise RuntimeError("token=SECRET")
        boom = _Boom(items)
        res = _sync(mt, ms, db, book, boom)
        assert res["outcome"] == mt.OUT_TRANSIENT and "SECRET" not in res["text"]
        assert boom.calls == [] and snap() == before
        # после восстановления доступа — отложенные операции выполняются один раз
        res = _sync(mt, ms, db, book, gt)
        assert res["outcome"] == mt.OUT_OK and res["gt_error"] is None
        assert gt.deletes() == [("delete", "L1", "g1")]
        assert ("status", "L1", "g2", "completed") in gt.calls


def test_cmd_tasks_uses_gt_error_text_plain():
    mt = _load_my_tasks()
    for cat in list(mt.GT_ERROR_TEXT) + ["no_scope"]:
        sent = []

        async def reply_text(text, **kw):
            sent.append((text, kw))
        ns = _load_main_async(["cmd_tasks"], {
            "db": NS(is_google_connected=lambda tid: True),
            "gtasks": NS(list_all_tasks=lambda tid, _c=cat: ([], _c)),
            "my_tasks": mt, "_format_tasks": lambda t: "x"})
        upd = NS(effective_user=NS(id=1), message=NS(reply_text=reply_text))
        asyncio.run(ns["cmd_tasks"](upd, NS(user_data={})))
        assert len(sent) == 1
        text, kw = sent[0]
        assert "parse_mode" not in kw, kw
        assert mt.gt_error_text(cat) in text and "\\" not in text
        if cat == "api_disabled":
            assert "/connect" not in text


def test_no_legacy_no_scope_texts():
    for f in ("main.py", "gtasks.py", "my_tasks.py", "auth.py"):
        src = (REPO_ROOT / f).read_text(encoding="utf-8")
        assert "no_scope" not in src, f
        assert "нет доступа — переподключите" not in src, f
        assert "Нет доступа к Google Tasks" not in src, f
    gsrc = (REPO_ROOT / "gtasks.py").read_text(encoding="utf-8")
    assert ": %s\", tl[" not in gsrc and "%s/%s: %s" not in gsrc   # str(HttpError) не логируется
    msrc = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert "доступ только к твоему Календарю" not in msrc
    assert "Google Календарю и Google Tasks" in msrc


# ══ 5. OAuth callback и exchange_code ════════════════════════════════════

class _Query(dict):
    def getall(self, key, default=()):
        return [self[key]] if key in self else default


def _oauth_main(auth):
    response = lambda text, content_type, status, headers: NS(
        text=text, content_type=content_type, status=status, headers=headers,
    )
    return _load_main_async([
        "cmd_connect", "cmd_confirm_google", "approve_google_link",
        "oauth_callback", "_oauth_browser_response", "_oauth_granted_text",
    ], {
        "auth": auth, "db": auth.db, "logger": logging.getLogger("oauth-test"), "html_lib": html,
        "web": NS(Request=object, Response=response),
        "InlineKeyboardMarkup": lambda rows: rows,
        "InlineKeyboardButton": lambda text, **kw: NS(text=text, **kw),
        "_user_lock": lambda _tid: asyncio.Lock(),
    })


def _connect_url(auth, user_id=1, chat_id=101):
    url = auth.get_auth_url(user_id, chat_id)
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def _callback(ns, state, code="synthetic-google-code", **other):
    query = _Query(code=code, state=state, **other)
    return asyncio.run(ns["oauth_callback"](NS(rel_url=NS(query=query))))


def _signer_and_public_key():
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from google.auth.crypt import RSASigner

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return RSASigner.from_string(private, key_id="fixture-key"), public.decode()


def _fake_oidc(auth, params, *, scope=None, access="candidate-access", refresh="candidate-refresh", claims=None,
               wrong_signature=False, exchange_error=None):
    """Drive a real OAuth Flow and Google ID-token verifier with synthetic transport."""
    from google.auth import jwt

    signer, public = _signer_and_public_key()
    if wrong_signature:
        signer, _ = _signer_and_public_key()
    token_claims = dict(iss="https://accounts.google.com", aud=auth.GOOGLE_CLIENT_ID, sub="verified-subject",
                        email="verified@example.test", email_verified=True, nonce=params["nonce"],
                        iat=int(time.time()) - 1, exp=int(time.time()) + 300)
    token_claims.update(claims or {})
    signed = jwt.encode(signer, token_claims).decode()
    issued_scopes = [SCOPE_CAL, SCOPE_EMAIL, "openid"] if scope is None else scope
    body = {"access_token": access, "token_type": "Bearer", "expires_in": 3599,
            "scope": " ".join(issued_scopes), "id_token": signed}
    if refresh:
        body["refresh_token"] = refresh
    real_from = auth.Flow.from_client_config
    seen = []

    def request(method, url, **kw):
        assert method == "POST" and url == "https://oauth2.googleapis.com/token" and kw["timeout"] == 15
        payload = parse_qs(kw["data"]) if isinstance(kw["data"], str) else {k: [v] for k, v in kw["data"].items()}
        verifier = payload["code_verifier"][0]
        digest = hashlib.sha256(verifier.encode()).digest()
        assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == params["code_challenge"]
        assert params["code_challenge_method"] == "S256"
        seen.append(payload["code"][0])
        if exchange_error:
            raise exchange_error
        return NS(status_code=200, text=json.dumps(body), headers={}, request=NS(url=url, headers={}, body=""))

    def cert_request(url, **kw):
        assert url == "https://www.googleapis.com/oauth2/v1/certs" and kw["timeout"] == 15
        return NS(status=200, data=json.dumps({"fixture-key": public}).encode())

    def from_config(*a, **kw):
        flow = real_from(*a, **kw)
        flow.oauth2session.request = request
        return flow

    auth.Flow = NS(from_client_config=from_config)
    auth.GoogleRequest = lambda: cert_request
    return seen


def test_oauth_callback_then_same_telegram_chat_approves():
    import re

    with tempfile.TemporaryDirectory() as tmp:
        db = _auth_db(tmp, expiry=time.time() + 7200)
        db.set_full_name(1, ME)
        db.update_user_settings(1, timezone="Asia/Vladivostok")
        db.save_mymeet_config(1, "synthetic-mymeet", "workspace", "fixture-sheet")
        auth = load_auth(db)
        old = _tokens(db)
        params = _connect_url(auth)
        assert auth.SCOPE_DRIVE_FILE in params["scope"].split()
        calls = _fake_oidc(auth, params)
        ns = _oauth_main(auth)

        with _LogCapture() as logs:
            browser = _callback(ns, params["state"])
            assert browser.status == 200 and "verified@example.test" in browser.text
            assert browser.headers["Cache-Control"] == "no-store"
            assert browser.headers["Referrer-Policy"] == "no-referrer"
            match = re.search(r"/confirm_google ([A-Za-z0-9_-]{43})", browser.text)
            assert match
            code = match.group(1)
            assert _tokens(db) == old
            assert _callback(ns, params["state"]).status == 400
            assert len(calls) == 1
            assert auth.confirm_google_link(1, 101, code) is None
            assert auth.begin_google_confirmation(2, 101, code) is None
            assert auth.begin_google_confirmation(1, 102, code) is None

            upd = NS(effective_chat=NS(id=101, type="private"), effective_user=NS(id=1),
                     message=NS(reply_text=AsyncMock(), delete=AsyncMock()))
            asyncio.run(ns["cmd_confirm_google"](upd, NS(args=[code])))
            upd.message.delete.assert_awaited_once()
            text, = upd.message.reply_text.await_args.args
            approval = upd.message.reply_text.await_args.kwargs["reply_markup"][0][0]
            assert "verified@example.test" in text and code not in text
            assert _tokens(db) == old
            assert auth.begin_google_confirmation(1, 101, code) is None
            assert auth.confirm_google_link(1, 102, approval.callback_data.split(":")[1]) is None

            upd.callback_query = NS(data=approval.callback_data, answer=AsyncMock(), edit_message_text=AsyncMock())
            asyncio.run(ns["approve_google_link"](upd, None))
            assert "Календарь подключён" in upd.callback_query.edit_message_text.await_args.args[0]
            assert auth.confirm_google_link(1, 101, approval.callback_data.split(":")[1]) is None

        current = db.get_google_grant(1)
        assert current["subject"] == "verified-subject" and "email" not in current
        assert current["granted"] == frozenset({SCOPE_CAL, SCOPE_EMAIL, "openid"})
        assert _tokens(db)[:2] == ("candidate-access", "candidate-refresh")
        user = db.get_user(1)
        assert user["mymeet_full_name"] == ME and user["timezone"] == "Asia/Vladivostok"
        assert user["mymeet_spreadsheet_id"] == "fixture-sheet"
        for private in (code, approval.callback_data, "verified@example.test", *old[:2], *_tokens(db)[:2]):
            assert private not in logs.text()


def test_oauth_unverified_identity_or_missing_refresh_rejected():
    from oauthlib.oauth2.rfc6749 import errors as oauth_errors

    cases = [
        ({"claims": {"nonce": "another-nonce"}}, 502),
        ({"claims": {"aud": "another-client"}}, 502),
        ({"claims": {"azp": "another-client"}}, 502),
        ({"claims": {"iss": "https://example.test"}}, 502),
        ({"claims": {"email_verified": False}}, 502),
        ({"wrong_signature": True}, 502),
        ({"refresh": None}, 502),
        ({"exchange_error": ConnectionError("network")}, 503),
        ({"exchange_error": oauth_errors.InvalidGrantError(description="sensitive-error")}, 400),
    ]
    for fixture, expected_status in cases:
        with tempfile.TemporaryDirectory() as tmp:
            db = _auth_db(tmp)
            auth = load_auth(db)
            old = _tokens(db)
            params = _connect_url(auth)
            _fake_oidc(auth, params, **fixture)
            ns = _oauth_main(auth)
            with _LogCapture() as logs:
                response = _callback(ns, params["state"])
                assert response.status == expected_status, (fixture, response.status, logs.text())
                assert _callback(ns, params["state"]).status == 400
            assert "sensitive-error" not in response.text + logs.text()
            assert _tokens(db) == old


def test_oauth_state_expiry_supersession_denial_and_restart():
    with tempfile.TemporaryDirectory() as tmp:
        db = _auth_db(tmp)
        auth = load_auth(db)
        old = _tokens(db)
        clock = NS(monotonic=Mock(return_value=200), time=time.time)
        auth.time = clock
        ns = _oauth_main(auth)
        expired = _connect_url(auth)
        clock.monotonic.return_value += auth._LOGIN_TTL
        assert _callback(ns, expired["state"]).status == 400

        stale = _connect_url(auth)
        current = _connect_url(auth)
        assert _callback(ns, stale["state"]).status == 400
        denial = _callback(ns, current["state"], error="access_denied", error_description="sensitive-marker")
        assert denial.status == 400 and "sensitive-marker" not in denial.text
        assert _callback(ns, current["state"]).status == 400

        current = _connect_url(auth)
        _fake_oidc(auth, current)
        result = auth.exchange_code("synthetic-google-code", current["state"])
        assert result
        assert load_auth(db).begin_google_confirmation(1, 101, result.confirmation_code) is None
        clock.monotonic.return_value += auth._CONFIRM_TTL
        assert auth.begin_google_confirmation(1, 101, result.confirmation_code) is None
        assert _tokens(db) == old


def test_connect_only_private_and_scopes_explained():
    auth = load_auth()
    auth.db.upsert_user = Mock()
    auth.get_auth_url = Mock(return_value="https://accounts.google.com/example")
    ns = _oauth_main(auth)
    reply = AsyncMock()
    upd = NS(effective_chat=NS(id=101, type="group"), effective_user=NS(id=1, username="fixture"),
             message=NS(reply_text=reply))
    asyncio.run(ns["cmd_connect"](upd, None))
    auth.get_auth_url.assert_not_called()
    reply.assert_not_awaited()
    upd.effective_chat.type = "private"
    asyncio.run(ns["cmd_connect"](upd, None))
    auth.get_auth_url.assert_called_once_with(1, 101)
    assert all(word in reply.await_args.args[0] for word in ("Календарю", "Google Tasks", "Drive"))


TESTS = [
    (test_classify_http_errors, "Google: классификация HttpError по reason/status"),
    (test_classify_auth_and_network_errors, "Google: RefreshError/Transport/сеть"),
    (test_log_google_error_no_leaks, "Google: в логе только category/status/reason"),
    (test_refresh_errors_classified_tokens_kept, "OAuth refresh: категории, токены целы, без scopes"),
    (test_credentials_no_auth_and_success, "OAuth: no_auth, успешный refresh"),
    (test_gtasks_list_returns_categories, "gtasks: категории вместо no_scope"),
    (test_run_sync_each_category_no_external_ops, "run_sync: тексты по причинам, без операций в GT"),
    (test_cmd_tasks_uses_gt_error_text_plain, "/tasks: текст причины без MarkdownV2"),
    (test_no_legacy_no_scope_texts, "Нет ложного «переподключите»"),
    (test_oauth_callback_then_same_telegram_chat_approves, "OAuth: PKCE, подпись ID token и явное подтверждение"),
    (test_oauth_unverified_identity_or_missing_refresh_rejected, "OAuth: неверная личность и неполный грант отклонены"),
    (test_oauth_state_expiry_supersession_denial_and_restart, "OAuth: истечение, смена, отказ, перезапуск"),
    (test_connect_only_private_and_scopes_explained, "/connect: личный чат и запрашиваемые права"),
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
    print("\n✅ Все тесты Google-диагностики прошли")
