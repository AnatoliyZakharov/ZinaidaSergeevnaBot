"""
Тесты issue #32: классификация ошибок Google, честные тексты отказа Google Tasks,
проверка выданных scopes при OAuth.  Без сети: HTTP-ответы Google — реальные
JSON-тела из документации/логов (номер проекта и email — фиктивные), БД — временная.
Запуск: python test_google_errors.py  (также входит в python test_smoke.py)
"""
import asyncio
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
    u = db.get_user(tid)
    return u["google_access_token"], u["google_refresh_token"], u["google_token_expiry"]


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

class _StubFlow:
    """Flow без сети: fetch_token выдаёт заданные credentials или бросает."""
    next_creds = None
    next_exc = None
    calls = []

    @classmethod
    def from_client_config(cls, config, scopes, redirect_uri, state=None):
        cls.calls.append({"scopes": list(scopes), "state": state})
        return cls()

    def fetch_token(self, code, timeout):
        assert timeout == 15
        if _StubFlow.next_exc is not None:
            raise _StubFlow.next_exc
        self.credentials = _StubFlow.next_creds


def _creds(granted, access="at-new", refresh="rt-new"):
    return NS(token=access, refresh_token=refresh,
              expiry=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=24),
              granted_scopes=granted)


def _callback_env(db, auth):
    sent, logs = [], []

    class _Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text, kw))

    class _Logger:
        def error(self, msg, *a, **k):
            logs.append(msg % a if a else msg)
        info = warning = error

    ns = _load_main_async(["oauth_callback", "_oauth_granted_text"], {
        "auth": auth, "_bot": _Bot(), "logger": _Logger(), "html_lib": html,
        "web": NS(Request=object, Response=lambda text, content_type, status=200: NS(
            text=text, content_type=content_type, status=status))})

    def call(code="c0de", state="st", **query):
        req = NS(rel_url=NS(query={"code": code, "state": state, **query}))
        return asyncio.run(ns["oauth_callback"](req))
    return call, sent, logs


def test_oauth_callback_granted_scopes():
    full = [SCOPE_CAL, SCOPE_TASKS, SCOPE_EMAIL, "openid"]
    cases = [
        (full, "Подключены Google Календарь и Google Tasks", None),
        (" ".join(full), "Подключены Google Календарь и Google Tasks", None),   # строкой
        ([SCOPE_CAL, SCOPE_EMAIL, "openid"],
         "Календарь подключён. Доступ к Google Tasks не выдан — /connect и отметьте Google Tasks",
         "Подключены"),
        ([SCOPE_TASKS, "openid"],
         "Google Tasks подключены. Доступ к Календарю не выдан — слоты и встречи работать "
         "не будут; /connect и отметьте Календарь", "Подключены"),
        (None, "Google подключён", "Tasks"),
        ([], "Google подключён", "Tasks"),
    ]
    for granted, expect, absent in cases:
        with tempfile.TemporaryDirectory() as tmp:
            db = _auth_db(tmp, refresh_token="rt-old", access="at-old")
            auth = load_auth(db)
            auth.Flow = _StubFlow
            _StubFlow.next_exc, _StubFlow.next_creds = None, _creds(granted)
            db.save_oauth_state("st", 1)
            call, sent, logs = _callback_env(db, auth)
            resp = call()
            assert resp.status == 200 and expect in html.unescape(resp.text), resp.text
            assert len(sent) == 1 and sent[0][0] == 1 and expect in sent[0][1]
            assert "parse_mode" not in sent[0][2]
            if absent:
                assert absent not in sent[0][1], sent[0][1]
            at, rt, exp = _tokens(db)
            assert (at, rt) == ("at-new", "rt-new") and exp > time.time()   # токены сохранены
            assert _StubFlow.calls[-1]["scopes"] == auth.SCOPES
            # тот же state повторно — уже израсходован
            assert auth.exchange_code("c0de", "st") is None


def test_oauth_callback_exchange_error_status():
    import oauthlib.oauth2.rfc6749.errors as oerr
    for exc, expected_status in (
        (Warning('Scope has changed from "a" to "b".'), 502),
        (oerr.InvalidGrantError(description="Bad Request"), 400),
        (ConnectionError("network"), 503),
        (ValueError("boom FAKE-ACCESS-TOKEN-LEAKED"), 502),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            db = _auth_db(tmp, refresh_token="rt-old", access="at-old")
            auth = load_auth(db)
            auth.Flow = _StubFlow
            _StubFlow.next_exc = exc
            db.save_oauth_state("st", 1)
            before = _tokens(db)
            call, sent, logs = _callback_env(db, auth)
            resp = call()
            assert resp.status == expected_status
            assert "Не удалось завершить авторизацию, запросите ссылку заново: /connect" \
                in resp.text, resp.text
            assert sent == [] and _tokens(db) == before
            assert logs and "category=" in logs[0]
            _assert_no_leaks("\n".join(logs))
    _StubFlow.next_exc = None
    # неизвестный state
    with tempfile.TemporaryDirectory() as tmp:
        db = _auth_db(tmp)
        auth = load_auth(db)
        auth.Flow = _StubFlow
        call, sent, logs = _callback_env(db, auth)
        resp = call(state="unknown")
        assert resp.status == 400 and "unknown" not in resp.text and sent == []


def test_oauth_callback_denial_ignores_sensitive_query():
    from unittest.mock import Mock

    exchange = Mock(side_effect=AssertionError("refused callback must not exchange a code"))
    call, sent, logs = _callback_env(None, NS(exchange_code=exchange))
    private = "sensitive-synthetic-callback-data"
    resp = call(code=private, state=private, error="access_denied", error_description=private)
    assert resp.status == 400 and "/connect" in resp.text and private not in resp.text
    assert private not in "\n".join(logs)
    assert sent == []
    exchange.assert_not_called()
    assert call(code="", state=private).status == 400


def _fake_token_response(body: dict):
    def request(method, url, **kw):
        assert url == "https://oauth2.googleapis.com/token"
        return NS(status_code=200, text=json.dumps(body), headers={},
                  request=NS(url=url, headers={}, body=""))
    return request


def test_real_oauthlib_scope_changed_no_warning():
    """Настоящий oauthlib: Google выдал не все scopes (granular consent)."""
    from oauthlib.oauth2.rfc6749.parameters import parse_token_response
    auth = load_auth()
    assert os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
    partial = {"access_token": "at", "token_type": "Bearer", "expires_in": 3599,
               "refresh_token": "rt",
               "scope": f"{SCOPE_CAL} {SCOPE_EMAIL} openid"}
    tok = parse_token_response(json.dumps(partial), scope=auth.SCOPES)   # не бросает Warning
    assert SCOPE_TASKS not in tok.scopes
    # контроль: без переменной oauthlib действительно бросает «Scope has changed»
    saved = os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE")
    try:
        try:
            parse_token_response(json.dumps(partial), scope=auth.SCOPES)
            raise AssertionError("ожидался Warning без OAUTHLIB_RELAX_TOKEN_SCOPE")
        except Warning as w:
            assert "Scope has changed" in str(w)
    finally:
        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = saved
    # сквозной exchange_code на настоящем Flow (HTTP-запрос подменён)
    with tempfile.TemporaryDirectory() as tmp:
        db = _auth_db(tmp, refresh_token="rt-old", access="at-old")
        auth = load_auth(db)
        real_from = auth.Flow.from_client_config

        class _Flow:
            @staticmethod
            def from_client_config(*a, **kw):
                f = real_from(*a, **kw)
                f.oauth2session.request = _fake_token_response(partial)
                return f
        auth.Flow = _Flow
        db.save_oauth_state("st", 1)
        res = auth.exchange_code("c0de", "st")
        assert res.telegram_id == 1
        assert res.granted == frozenset({SCOPE_CAL, SCOPE_EMAIL, "openid"})
        assert _tokens(db)[:2] == ("at", "rt")
        # все scopes
        full = dict(partial, scope=" ".join(auth.SCOPES), access_token="at2",
                    refresh_token="rt2")

        class _Flow2:
            @staticmethod
            def from_client_config(*a, **kw):
                f = real_from(*a, **kw)
                f.oauth2session.request = _fake_token_response(full)
                return f
        auth.Flow = _Flow2
        db.save_oauth_state("st2", 1)
        res = auth.exchange_code("c0de", "st2")
        assert SCOPE_TASKS in res.granted and SCOPE_CAL in res.granted
        assert _tokens(db)[:2] == ("at2", "rt2")


def test_reconnect_keeps_user_data():
    with tempfile.TemporaryDirectory() as tmp:
        db = _auth_db(tmp, refresh_token="rt-old", access="at-old")
        db.set_full_name(1, ME)
        db.save_mymeet_config(1, "mm-test-token", "ws1", "sheet1", "org@example.com")
        db.update_user_settings(1, timezone="Asia/Vladivostok", work_start=8, work_end=20)
        mt = _load_my_tasks()
        mt.add_forwarded(1, {"title": "Позвонить", "assigner": "Мария",
                             "task_date": "10.11.2026", "deadline": "",
                             "source_key": "tg:1:1"}, dbm=db)

        def snapshot():
            conn = sqlite3.connect(db.DB_PATH)
            conn.row_factory = sqlite3.Row
            u = dict(conn.execute("SELECT * FROM users WHERE telegram_id=1").fetchone())
            rows = [tuple(r) for r in conn.execute("SELECT * FROM my_tasks ORDER BY 1, 2")]
            conn.close()
            for k in ("google_access_token", "google_refresh_token", "google_token_expiry"):
                u.pop(k)
            return u, rows
        before = snapshot()
        assert before[1], "нет строк my_tasks"
        auth = load_auth(db)
        auth.Flow = _StubFlow
        _StubFlow.next_exc = None
        _StubFlow.next_creds = _creds([SCOPE_CAL, SCOPE_TASKS], access="at-1", refresh="rt-1")
        db.save_oauth_state("s1", 1)
        assert auth.exchange_code("c", "s1").telegram_id == 1
        assert _tokens(db)[:2] == ("at-1", "rt-1") and snapshot() == before
        # Google не прислал refresh_token — старый не затирается
        for empty in (None, ""):
            _StubFlow.next_creds = _creds([SCOPE_CAL, SCOPE_TASKS], access="at-2", refresh=empty)
            db.save_oauth_state("s2", 1)
            assert auth.exchange_code("c", "s2").telegram_id == 1
            at, rt, exp = _tokens(db)
            assert at == "at-2" and rt == "rt-1" and exp > time.time()
            assert db.is_google_connected(1) and snapshot() == before
        assert db.get_user(1)["mymeet_full_name"] == ME
        assert db.get_user(1)["timezone"] == "Asia/Vladivostok"


def test_connect_text_mentions_both_scopes():
    sent = []

    async def reply_text(text, **kw):
        sent.append(text)
    ns = _load_main_async(["cmd_connect"], {
        "db": NS(upsert_user=lambda *a: None),
        "auth": NS(get_auth_url=lambda tid: "https://accounts.google.com/x"),
        "InlineKeyboardMarkup": lambda rows: rows,
        "InlineKeyboardButton": lambda text, url: (text, url)})
    upd = NS(effective_user=NS(id=1, username="u"), message=NS(reply_text=reply_text))
    asyncio.run(ns["cmd_connect"](upd, None))
    assert sent == ["Нажми кнопку ниже и авторизуйся через Google. Бот запросит доступ к "
                    "Google Календарю и Google Tasks — отметь обе галочки, иначе часть "
                    "функций не будет работать."]


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
    (test_oauth_callback_granted_scopes, "OAuth callback: сообщение по выданным scopes"),
    (test_oauth_callback_exchange_error_status, "OAuth callback: статусы ошибок обмена"),
    (test_oauth_callback_denial_ignores_sensitive_query, "OAuth callback: отказ и безопасный ответ"),
    (test_real_oauthlib_scope_changed_no_warning, "oauthlib: частичные scopes без Warning"),
    (test_reconnect_keeps_user_data, "Повторный /connect: данные целы, refresh не затирается"),
    (test_connect_text_mentions_both_scopes, "/connect: Календарь и Google Tasks"),
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
