"""
Тесты #33: токен Telegram-бота не попадает в штатные логи.

Без сети и без реальных секретов: токен — случайный фиктивный, запросы идут
через httpx.MockTransport.  Конфигурация логирования извлекается из main.py
через AST (бот не импортируется).
Запуск: python test_log_security.py  (также входит в python test_smoke.py)
"""
import ast
import io
import logging
import re
import secrets
import warnings

import test_smoke as _ts


def _fake_token() -> str:
    """Фиктивный токен формата BotFather: <id>:<35 символов>."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    return (str(secrets.randbelow(9 * 10 ** 9) + 10 ** 9) + ":"
            + "".join(secrets.choice(alphabet) for _ in range(35)))


def _load_log_config():
    src = (_ts.REPO_ROOT / "main.py").read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(src)
    names = {"_RedactingFormatter", "_configure_log_security"}
    assigns = {"LOG_FORMAT", "_QUIET_HTTP_LOGGERS", "_BOT_TOKEN_RE"}
    nodes = [n for n in tree.body
             if (isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names)
             or (isinstance(n, ast.Assign) and any(
                 isinstance(t, ast.Name) and t.id in assigns for t in n.targets))]
    found = {n.name for n in nodes if not isinstance(n, ast.Assign)}
    assert found == names, f"не найдены в main.py: {names - found}"
    ns = {"logging": logging, "re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), ns)
    return ns


class _Capture:
    """Временный корневой обработчик в StringIO; уровни логгеров — как были."""

    def __enter__(self):
        self.root = logging.getLogger()
        self.saved = (list(self.root.handlers), self.root.level,
                      {n: logging.getLogger(n).level for n in ("httpx", "httpcore")})
        self.buf = io.StringIO()
        self.root.handlers = [logging.StreamHandler(self.buf)]
        self.root.setLevel(logging.INFO)
        for n in ("httpx", "httpcore"):
            logging.getLogger(n).setLevel(logging.NOTSET)
        return self

    def __exit__(self, *exc):
        handlers, level, lv = self.saved
        self.root.handlers = handlers
        self.root.setLevel(level)
        for n, v in lv.items():
            logging.getLogger(n).setLevel(v)
        return False

    @property
    def text(self):
        return self.buf.getvalue()


def _httpx_get(url):
    import httpx
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
    with httpx.Client(transport=transport) as client:
        return client.get(url).status_code


def test_httpx_info_leaks_without_fix():
    """Контроль: без настройки httpx действительно пишет URL с токеном на INFO
    (иначе следующий тест ничего бы не доказывал)."""
    token = _fake_token()
    with _Capture() as cap:
        assert _httpx_get(f"https://api.telegram.org/bot{token}/getMe") == 200
    assert token in cap.text, "httpx перестал логировать URL — тест надо пересмотреть"


def test_httpx_info_has_no_token():
    ns = _load_log_config()
    token = _fake_token()
    with _Capture() as cap:
        ns["_configure_log_security"](token)
        assert _httpx_get(f"https://api.telegram.org/bot{token}/getMe") == 200
        assert _httpx_get(f"https://api.telegram.org/file/bot{token}/voice/f.oga") == 200
        logging.getLogger("telegram.ext").info("Application started")
    assert token not in cap.text, cap.text
    assert token.split(":")[1] not in cap.text
    assert "HTTP Request" not in cap.text                 # INFO httpx выключен
    assert "Application started" in cap.text              # прочие INFO на месте
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING


def test_warnings_and_tracebacks_redacted():
    """Предупреждения httpx и traceback ошибок сети (error_handler с exc_info)
    проходят, но без токена."""
    ns = _load_log_config()
    token = _fake_token()
    with _Capture() as cap:
        ns["_configure_log_security"](token)
        logging.getLogger("httpx").warning("retry %s", f"https://api.telegram.org/bot{token}/x")
        try:
            raise ConnectionError(f"failed for url https://api.telegram.org/bot{token}/getFile")
        except ConnectionError:
            logging.getLogger("__main__").error("Необработанное исключение в хендлере:",
                                                exc_info=True)
    out = cap.text
    assert token not in out and token.split(":")[1] not in out, out
    assert "<redacted>" in out and "ConnectionError" in out and "retry" in out, out


def test_other_secrets_and_unknown_tokens_masked():
    ns = _load_log_config()
    token, other, other2 = _fake_token(), _fake_token(), _fake_token()
    api_key = "AQVN" + secrets.token_urlsafe(24)
    with _Capture() as cap:
        ns["_configure_log_security"](token, api_key, "", "short")
        lg = logging.getLogger("t33")
        lg.info("key=%s", api_key)
        lg.info("чужой токен %s", other)                     # не передан — по шаблону
        lg.info("url https://api.telegram.org/bot%s/getMe", other2)   # внутри URL
        lg.info("file https://api.telegram.org/file/bot%s/v.oga", other2)
        lg.info("Встреча в 10:30, id 12345, short слово")    # обычный текст не трогаем
    out = cap.text
    assert api_key not in out and other not in out, out
    assert other2.split(":")[1] not in out and out.count("bot<redacted>") == 2, out
    assert "Встреча в 10:30, id 12345, short слово" in out, out


def test_configured_at_startup():
    """main.py: настройка вызывается сразу после чтения секретов, до бота."""
    src = (_ts.REPO_ROOT / "main.py").read_text(encoding="utf-8")
    call = src.index("_configure_log_security(TELEGRAM_TOKEN")
    assert src.index('TELEGRAM_TOKEN        = os.environ["TELEGRAM_TOKEN"]') < call
    assert call < src.index("Application.builder()")
    assert '_QUIET_HTTP_LOGGERS = ("httpx", "httpcore")' in src


TESTS = [
    (test_httpx_info_leaks_without_fix, "Логи #33: контроль — httpx без настройки пишет URL"),
    (test_httpx_info_has_no_token, "Логи #33: INFO httpx/httpcore без токена"),
    (test_warnings_and_tracebacks_redacted, "Логи #33: предупреждения и traceback без токена"),
    (test_other_secrets_and_unknown_tokens_masked,
     "Логи #33: прочие секреты и токены по шаблону, обычный текст цел"),
    (test_configured_at_startup, "Логи #33: настройка при старте до создания бота"),
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
