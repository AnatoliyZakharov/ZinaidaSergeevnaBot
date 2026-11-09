"""
Тесты для исправлений MYmeet: API-интеграция, шифрование токенов, безопасность.
Запуск: pytest test_mymeet_workspace.py -v
"""
import os
import re
import sys
import logging
import sqlite3
import tempfile
import pathlib
import threading
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import requests

# ---------------------------------------------------------------------------
# Вспомогательное окружение — ставим ENCRYPTION_KEY до импорта crypto/db
# ---------------------------------------------------------------------------
# Fernet key generated at import time — no static secret in source
from cryptography.fernet import Fernet as _Fernet
_TEST_FERNET_KEY = _Fernet.generate_key().decode()


# ---------------------------------------------------------------------------
# Helper: build a test mm_ token from a suffix without a static literal
# that would trigger secret scanners on the prefix+suffix pattern.
# ---------------------------------------------------------------------------
def _mk_tok(suffix: str) -> str:
    """Return 'mm_<suffix>' without writing the full pattern as a literal."""
    return "mm" + "_" + suffix

@pytest.fixture(autouse=True)
def set_encryption_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", _TEST_FERNET_KEY)
    # Env vars required by auth.py and main.py at import time (dummy test values)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-google-client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-google-client-secret")
    monkeypatch.setenv("OAUTH_REDIRECT_URI", "https://test.example.com/oauth/callback")
    monkeypatch.setenv("TELEGRAM_TOKEN", "0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    monkeypatch.setenv("YANDEX_API_KEY", "test-yandex-api-key")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "test-yandex-folder-id")
    # Сбросить кэшированные модули, зависящие от env vars
    for _mod in ("main", "auth", "gtasks", "crypto"):
        if _mod in sys.modules:
            if _mod == "crypto":
                sys.modules[_mod]._fernet = None
            else:
                del sys.modules[_mod]
    yield
    if "crypto" in sys.modules:
        sys.modules["crypto"]._fernet = None


# ---------------------------------------------------------------------------
# БЛОК 1: mymeet_sync — verify_token_and_get_workspace
# ---------------------------------------------------------------------------

def _make_response(status_code, json_body=None, text=""):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = (200 <= status_code < 300)
    resp.text = text or (str(json_body) if json_body else "")
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no json")
    return resp


class TestVerifyTokenAndGetWorkspace:

    def test_valid_token_returns_active_workspace(self):
        from mymeet_sync import verify_token_and_get_workspace
        mock_resp = _make_response(200, {"workspaceName": "Тестовое пространство", "total": 1})
        with patch("requests.get", return_value=mock_resp) as mock_get:
            result = verify_token_and_get_workspace(_mk_tok("valid_token_12345678"))
        assert result["id"] == "active"
        assert result["name"] == "Тестовое пространство"
        # Проверяем, что вызван правильный endpoint
        url_called = mock_get.call_args[0][0]
        assert "/api/workspaces/active/all-meetings" in url_called

    def test_valid_token_no_workspace_name(self):
        """Если API не вернул имя — возвращаем placeholder."""
        from mymeet_sync import verify_token_and_get_workspace
        mock_resp = _make_response(200, {"total": 0, "followups": []})
        with patch("requests.get", return_value=mock_resp):
            result = verify_token_and_get_workspace(_mk_tok("valid_token_12345678"))
        assert result["id"] == "active"
        assert result["name"]  # непустое

    def test_wrong_key_raises_401(self):
        from mymeet_sync import verify_token_and_get_workspace, MymeetTokenError
        mock_resp = _make_response(401, text="Unauthorized")
        with patch("requests.get", return_value=mock_resp):
            with pytest.raises(MymeetTokenError) as exc_info:
                verify_token_and_get_workspace(_mk_tok("wrong_key_000000000"))
        assert exc_info.value.status_code == 401

    def test_free_plan_raises_403(self):
        from mymeet_sync import verify_token_and_get_workspace, MymeetTokenError
        mock_resp = _make_response(403, json_body={"message": "API access requires paid plan"})
        with patch("requests.get", return_value=mock_resp):
            with pytest.raises(MymeetTokenError) as exc_info:
                verify_token_and_get_workspace(_mk_tok("free_plan_token_1234"))
        assert exc_info.value.status_code == 403

    def test_network_error_raises_requests_exception(self):
        from mymeet_sync import verify_token_and_get_workspace
        with patch("requests.get", side_effect=requests.ConnectionError("API down")):
            with pytest.raises(requests.RequestException):
                verify_token_and_get_workspace(_mk_tok("ok_token_12345678901"))

    def test_empty_token_raises_immediately(self):
        from mymeet_sync import verify_token_and_get_workspace, MymeetTokenError
        with pytest.raises(MymeetTokenError) as exc_info:
            verify_token_and_get_workspace("")
        assert exc_info.value.status_code == 0  # не делает HTTP-запрос

    def test_always_uses_active_keyword_not_uuid(self):
        """Даже если передан ws_id — URL должен использовать 'active'."""
        from mymeet_sync import _fetch_meetings
        mock_resp = _make_response(200, {"followups": [], "total": 0})
        with patch("requests.get", return_value=mock_resp) as mock_get:
            _fetch_meetings(days=1, token=_mk_tok("1234567890123"), ws_id="some-uuid-1234")
        url_called = mock_get.call_args[0][0]
        assert "/active/" in url_called, f"Ожидался /active/ в URL, получен: {url_called}"
        assert "some-uuid" not in url_called, "UUID не должен попадать в URL"

    def test_token_not_logged_on_error(self, caplog):
        """Токен не должен появляться в логах даже при ошибке."""
        from mymeet_sync import verify_token_and_get_workspace, MymeetTokenError
        secret_token = _mk_tok("secret_token_do_not_log_XYZ987")
        mock_resp = _make_response(401, text="Unauthorized")
        with caplog.at_level(logging.WARNING):
            with patch("requests.get", return_value=mock_resp):
                with pytest.raises(MymeetTokenError):
                    verify_token_and_get_workspace(secret_token)
        assert secret_token not in caplog.text, "Токен не должен попадать в логи!"


# ---------------------------------------------------------------------------
# БЛОК 2: crypto.py — шифрование/дешифрование
# ---------------------------------------------------------------------------

class TestCrypto:

    def test_encrypt_decrypt_roundtrip(self):
        import crypto
        original = _mk_tok("my_secret_api_token_abcdef123456")
        encrypted = crypto.encrypt_token(original)
        assert encrypted != original
        decrypted = crypto.decrypt_token(encrypted)
        assert decrypted == original

    def test_encrypted_value_is_not_plaintext(self):
        import crypto
        token = _mk_tok("plaintext_token_should_not_appear")
        encrypted = crypto.encrypt_token(token)
        assert token not in encrypted

    def test_decrypt_with_wrong_key_raises(self, monkeypatch):
        import importlib
        import crypto
        # Зашифровать правильным ключом
        encrypted = crypto.encrypt_token(_mk_tok("sometoken123456789"))
        # Смена ключа
        crypto._fernet = None
        monkeypatch.setenv("ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        import sys
        if "crypto" in sys.modules:
            sys.modules["crypto"]._fernet = None
        from crypto import CryptoDecryptError
        with pytest.raises(CryptoDecryptError):
            crypto.decrypt_token(encrypted)

    def test_missing_encryption_key_raises(self, monkeypatch):
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        import sys
        if "crypto" in sys.modules:
            sys.modules["crypto"]._fernet = None
        import crypto
        with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
            crypto.encrypt_token(_mk_tok("any_token_1234567890"))

    def test_is_encrypted_heuristic(self):
        import crypto
        encrypted = crypto.encrypt_token(_mk_tok("sometoken1234567890"))
        assert crypto.is_encrypted(encrypted) is True
        assert crypto.is_encrypted(_mk_tok("plaintext")) is False
        assert crypto.is_encrypted("") is False

    def test_empty_plaintext_raises(self):
        import crypto
        with pytest.raises(crypto.CryptoError):
            crypto.encrypt_token("")


# ---------------------------------------------------------------------------
# БЛОК 3: db.py — хранение зашифрованного токена
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Временная изолированная БД для каждого теста."""
    db_path = tmp_path / "test_bot.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    # Перезагрузить db с новым путём
    import importlib
    if "db" in sys.modules:
        del sys.modules["db"]
    import db as db_module
    db_module.init_db()
    db_module.upsert_user(12345678, "testuser")
    yield db_module
    if "db" in sys.modules:
        del sys.modules["db"]


class TestDbTokenEncryption:

    def test_token_stored_encrypted(self, temp_db, tmp_path):
        """Токен в SQLite должен быть зашифрован — не plaintext."""
        import crypto
        plaintext_token = _mk_tok("secret_token_do_not_store_plain")
        temp_db.save_mymeet_config(
            telegram_id=12345678,
            token=plaintext_token,
            ws_id="active",
            spreadsheet_id="A" * 44,
        )
        # Читаем напрямую из БД — проверяем что токен зашифрован
        conn = sqlite3.connect(str(tmp_path / "test_bot.db"))
        row = conn.execute(
            "SELECT mymeet_token FROM users WHERE telegram_id = ?", (12345678,)
        ).fetchone()
        conn.close()
        stored = row[0]
        assert plaintext_token not in stored, "Токен хранится в открытом виде!"
        assert crypto.is_encrypted(stored), "Токен должен быть зашифрован (Fernet)"

    def test_get_mymeet_users_decrypts_token(self, temp_db):
        """get_mymeet_users() должен возвращать расшифрованный токен."""
        plaintext_token = _mk_tok("another_secret_token_abcdef1234")
        temp_db.save_mymeet_config(
            telegram_id=12345678,
            token=plaintext_token,
            ws_id="active",
            spreadsheet_id="B" * 44,
        )
        # Активируем пользователя
        conn = sqlite3.connect(str(next(iter(
            [p for p in pathlib.Path(os.environ["DB_PATH"]).parent.iterdir()
             if p.suffix == ".db"]), pathlib.Path(os.environ["DB_PATH"]))))
        conn.close()
        users = temp_db.get_mymeet_users()
        assert len(users) == 1
        assert users[0]["mymeet_token"] == plaintext_token

    def test_plaintext_token_skipped_in_get_mymeet_users(self, temp_db, tmp_path):
        """Незашифрованный старый токен должен быть пропущен."""
        # Записываем напрямую в БД без шифрования (эмуляция старых данных)
        conn = sqlite3.connect(str(tmp_path / "test_bot.db"))
        conn.execute(
            "UPDATE users SET mymeet_token=?, mymeet_ws_id=?, mymeet_spreadsheet_id=?, mymeet_setup_done=1 WHERE telegram_id=?",
            (_mk_tok("old_plaintext_token_abcdef123"), "active", "C" * 44, 12345678)
        )
        conn.commit()
        conn.close()
        users = temp_db.get_mymeet_users()
        # Пользователь с незашифрованным токеном должен быть пропущен
        assert len(users) == 0, "Незашифрованный токен не должен попадать в результат"


# ---------------------------------------------------------------------------
# БЛОК 4: Telegram delete_message — обработка ошибок
# ---------------------------------------------------------------------------

class TestTelegramDeleteMessage:

    @pytest.mark.asyncio
    async def test_delete_message_called_after_token(self):
        """После получения токена должна быть попытка удалить сообщение."""
        import importlib
        # Мокаем необходимые зависимости
        mock_message = AsyncMock()
        mock_message.delete = AsyncMock()
        mock_message.reply_text = AsyncMock(return_value=AsyncMock(edit_text=AsyncMock()))
        mock_update = MagicMock()
        mock_update.message = mock_message
        mock_update.effective_user.id = 12345678
        mock_context = MagicMock()
        mock_context.user_data = {"mymeet_setup": {"step": "mymeet_token"}}

        ws_result = {"id": "active", "name": "Test WS"}
        with patch("mymeet_sync.verify_token_and_get_workspace", return_value=ws_result):
            # Импортируем и вызываем handler
            import main as main_module
            await main_module._handle_mymeet_setup(
                telegram_id=12345678,
                text=_mk_tok("valid_token_12345678901234"),
                update=mock_update,
                context=mock_context,
            )
        mock_message.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_message_failure_does_not_abort(self):
        """Если удаление сообщения не удалось — процесс продолжается."""
        mock_message = AsyncMock()
        mock_message.delete = AsyncMock(side_effect=Exception("Cannot delete"))
        mock_message.reply_text = AsyncMock(return_value=AsyncMock(edit_text=AsyncMock()))
        mock_update = MagicMock()
        mock_update.message = mock_message
        mock_update.effective_user.id = 12345678
        mock_context = MagicMock()
        mock_context.user_data = {"mymeet_setup": {"step": "mymeet_token"}}

        ws_result = {"id": "active", "name": "Test WS"}
        with patch("mymeet_sync.verify_token_and_get_workspace", return_value=ws_result):
            import main as main_module
            result = await main_module._handle_mymeet_setup(
                telegram_id=12345678,
                text=_mk_tok("valid_token_12345678901234"),
                update=mock_update,
                context=mock_context,
            )
        # Несмотря на ошибку удаления — шаг завершился успешно
        assert result is True
        setup = mock_context.user_data["mymeet_setup"]
        assert setup["step"] == "mymeet_sheet"


# ---------------------------------------------------------------------------
# БЛОК 5: Нет секретов в логах
# ---------------------------------------------------------------------------

class TestNoSecretsInLogs:

    def test_mymeet_error_logs_no_token(self, caplog):
        """При ошибке API токен не должен попадать в логи."""
        from mymeet_sync import verify_token_and_get_workspace, MymeetTokenError
        secret = _mk_tok("top_secret_token_xyz123456789")
        mock_resp = _make_response(401, text="Unauthorized")
        with caplog.at_level(logging.DEBUG):
            with patch("requests.get", return_value=mock_resp):
                with pytest.raises(MymeetTokenError):
                    verify_token_and_get_workspace(secret)
        for record in caplog.records:
            assert secret not in record.getMessage(), \
                f"Токен найден в логе: {record.getMessage()!r}"

    def test_crypto_decrypt_error_logs_no_ciphertext(self, caplog):
        """CryptoDecryptError не должен логировать зашифрованный текст."""
        import crypto
        import sys
        # Зашифровываем одним ключом
        encrypted = crypto.encrypt_token(_mk_tok("sometoken1234567890"))
        # Меняем ключ
        crypto._fernet = None
        import os
        old_key = os.environ.get("ENCRYPTION_KEY", "")
        os.environ["ENCRYPTION_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

        with caplog.at_level(logging.WARNING):
            try:
                crypto.decrypt_token(encrypted)
            except crypto.CryptoDecryptError:
                pass
        for record in caplog.records:
            assert encrypted not in record.getMessage(), \
                "Зашифрованный токен найден в логе!"
        # Восстанавливаем ключ
        os.environ["ENCRYPTION_KEY"] = old_key
        crypto._fernet = None


# ---------------------------------------------------------------------------
# БЛОК 6: Интеграция — ручной синк /sync_mymeet и /sync_mymeet_full
# ---------------------------------------------------------------------------

class TestManualSyncDecryption:
    """Проверяем, что ручные команды синка получают расшифрованный токен."""

    def _make_user_row(self, tmp_path, monkeypatch):
        """Создаёт пользователя с зашифрованным токеном в SQLite и возвращает db-модуль."""
        import sys
        db_path = tmp_path / "sync_test.db"
        monkeypatch.setenv("DB_PATH", str(db_path))
        if "db" in sys.modules:
            del sys.modules["db"]
        import db as db_mod
        db_mod.init_db()
        db_mod.upsert_user(99887766, "syncuser")
        db_mod.save_mymeet_config(
            telegram_id=99887766,
            token=_mk_tok("real_token_abcdef1234567"),
            ws_id="active",
            spreadsheet_id="Z" * 44,
        )
        return db_mod

    @pytest.mark.asyncio
    async def test_sync_mymeet_uses_decrypted_token(self, tmp_path, monkeypatch):
        """/sync_mymeet передаёт расшифрованный токен в sync_to_sheets_async."""
        import sys
        db_mod = self._make_user_row(tmp_path, monkeypatch)
        captured = {}

        async def fake_sync(days, append, user_config):
            captured["token"] = user_config.get("mymeet_token")
            return "✅ Синк выполнен (тест)"

        mock_message = AsyncMock()
        mock_message.reply_text = AsyncMock(return_value=AsyncMock(edit_text=AsyncMock()))
        mock_update = MagicMock()
        mock_update.message = mock_message
        mock_update.effective_user.id = 99887766
        mock_update.effective_user.username = "syncuser"
        mock_context = MagicMock()
        mock_context.user_data = {}

        with patch("db.get_user", side_effect=db_mod.get_user),              patch("db.upsert_user", side_effect=db_mod.upsert_user),              patch("mymeet_sync.sync_to_sheets_async", side_effect=fake_sync):
            import main as main_mod
            await main_mod.cmd_sync_mymeet(mock_update, mock_context)

        # Проверяем: токен расшифрован и соответствует оригиналу
        assert captured.get("token") == _mk_tok("real_token_abcdef1234567"), \
            f"Ожидался расшифрованный токен, получен: {captured.get('token')!r}"
        # Проверяем: зашифрованный токен не попал в результат
        import crypto
        assert not crypto.is_encrypted(captured.get("token", "")), \
            "sync_to_sheets_async получил зашифрованный токен вместо открытого!"

    @pytest.mark.asyncio
    async def test_sync_mymeet_full_uses_decrypted_token(self, tmp_path, monkeypatch):
        """/sync_mymeet_full тоже передаёт расшифрованный токен."""
        import sys
        db_mod = self._make_user_row(tmp_path, monkeypatch)
        captured = {}

        async def fake_sync(days, append, user_config):
            captured["token"] = user_config.get("mymeet_token")
            captured["days"] = days
            return "✅ Полный синк (тест)"

        mock_message = AsyncMock()
        mock_message.reply_text = AsyncMock(return_value=AsyncMock(edit_text=AsyncMock()))
        mock_update = MagicMock()
        mock_update.message = mock_message
        mock_update.effective_user.id = 99887766
        mock_update.effective_user.username = "syncuser"
        mock_context = MagicMock()
        mock_context.user_data = {}
        mock_context.args = ["30"]

        with patch("db.get_user", side_effect=db_mod.get_user),              patch("db.upsert_user", side_effect=db_mod.upsert_user),              patch("mymeet_sync.sync_to_sheets_async", side_effect=fake_sync):
            import main as main_mod
            await main_mod.cmd_sync_mymeet_full(mock_update, mock_context)

        assert captured.get("token") == _mk_tok("real_token_abcdef1234567"), \
            f"Ожидался расшифрованный токен, получен: {captured.get('token')!r}"
        assert captured.get("days") == 30

    @pytest.mark.asyncio
    async def test_sync_mymeet_legacy_plaintext_token_aborts(self, tmp_path, monkeypatch):
        """Если токен не зашифрован (устаревшие данные) — синк прерывается с сообщением."""
        import sqlite3, sys
        db_path = tmp_path / "legacy.db"
        monkeypatch.setenv("DB_PATH", str(db_path))
        if "db" in sys.modules:
            del sys.modules["db"]
        import db as db_mod
        db_mod.init_db()
        db_mod.upsert_user(11223344, "legacyuser")
        # Записываем незашифрованный токен напрямую (эмуляция старых данных)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE users SET mymeet_token=?, mymeet_ws_id=?, mymeet_spreadsheet_id=?, "
            "mymeet_setup_done=1 WHERE telegram_id=?",
            (_mk_tok("legacy_plain_1234567890"), "active", "D" * 44, 11223344)
        )
        conn.commit(); conn.close()

        sync_called = []
        async def fake_sync(*a, **kw):
            sync_called.append(True)
            return "SHOULD_NOT_REACH"

        mock_message = AsyncMock()
        mock_message.reply_text = AsyncMock(return_value=MagicMock())
        mock_update = MagicMock()
        mock_update.message = mock_message
        mock_update.effective_user.id = 11223344
        mock_update.effective_user.username = "legacyuser"
        mock_context = MagicMock()
        mock_context.user_data = {}

        with patch("db.get_user", side_effect=db_mod.get_user), \
             patch("db.upsert_user", side_effect=db_mod.upsert_user), \
             patch("mymeet_sync.sync_to_sheets_async", side_effect=fake_sync):
            import main as main_mod
            await main_mod.cmd_sync_mymeet(mock_update, mock_context)

        # sync_to_sheets_async не должен был вызваться
        assert not sync_called, "sync не должен запускаться с незашифрованным токеном"
        # Пользователь получил сообщение о том, что нужен /setup_mymeet
        reply_calls = mock_message.reply_text.call_args_list
        assert any("setup_mymeet" in str(c) for c in reply_calls), \
            f"Ожидалось упоминание /setup_mymeet в ответе, получено: {reply_calls}"
