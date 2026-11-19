"""Local regression tests for safe replacement of a MYmeet configuration."""
import asyncio
from io import StringIO
import logging
import os
import re
import sqlite3
import tempfile
import time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, defaultTestLoader, TextTestRunner, main
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet
from gspread.http_client import HTTPClient


USER_ID = 700000001
OLD_SHEET = "O" * 33
NEW_SHEET = "N" * 33
OLD_TOKEN = "mm" + "_old_fixture"
NEW_TOKEN = "mm" + "_new_fixture"


class MymeetReconfigurationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(patch.dict(os.environ))
        import crypto
        self.enterContext(patch.object(crypto, "_fernet", None))
        # Reuse the smoke loader so the actual handlers can run with CI's slim dependencies.
        from test_my_tasks import _load_main_async, _load_ms, _load_my_tasks, _ts

        os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        self.db = _ts._load_db(directory.name)
        self.ms = _load_ms()
        self.bot = _load_main_async([
            "cmd_setup_mymeet", "cmd_cancel_mymeet", "_mymeet_sheet_id", "_check_mymeet_sheet_access",
            "_is_current_mymeet_draft", "_delete_mymeet_token_message", "_handle_mymeet_setup",
            "_user_full_name", "_user_row_to_mymeet_config", "_user_lock", "_mymeet_config_matches",
            "cmd_sync_mymeet",
        ], {
            "re": re, "time_module": time, "logger": logging.getLogger("mymeet-reconfiguration"),
            "db": self.db, "_crypto": crypto, "CryptoDecryptError": crypto.CryptoDecryptError,
            "mymeet_sync": self.ms, "MymeetTokenError": self.ms.MymeetTokenError, "my_tasks": _load_my_tasks(),
        }, assigns=(
            "_SETUP_STEP_TOKEN", "_SETUP_STEP_SHEET", "_MYMEET_SETUP_TTL", "_MYMEET_RESTART_TEXT",
            "_MYMEET_SHEET_ID", "_MYMEET_SHEET_URL", "_my_tasks_locks", "_MYMEET_CONFIG_CHANGED",
        ))
        self.db.init_db()
        self.db.upsert_user(USER_ID, "fixture")
        self.db.save_mymeet_config(USER_ID, OLD_TOKEN, "old", OLD_SHEET)
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=USER_ID, username="fixture"),
            effective_chat=SimpleNamespace(type="private"),
            message=AsyncMock(),
        )
        self.context = SimpleNamespace(user_data={}, args=[])

    async def send(self, text):
        return await self.bot["_handle_mymeet_setup"](USER_ID, text, self.update, self.context)

    async def start(self):
        await self.bot["cmd_setup_mymeet"](self.update, self.context)
        with patch.dict(self.bot, verify_token_and_get_workspace=MagicMock(return_value={"id": "active"})):
            self.assertTrue(await self.send(NEW_TOKEN))

    def assert_active(self, token=OLD_TOKEN, workspace="old", sheet=OLD_SHEET):
        user = self.db.get_user(USER_ID)
        self.assertEqual(user["mymeet_setup_done"], 1)
        self.assertEqual(self.db._crypto.decrypt_token(user["mymeet_token"]), token)
        self.assertEqual((user["mymeet_ws_id"], user["mymeet_spreadsheet_id"]), (workspace, sheet))

    async def test_failed_validation_keeps_active_and_cancel_discards_draft(self):
        await self.start()
        self.update.message.delete.assert_awaited_once()
        self.assert_active()
        self.assertNotIn(NEW_TOKEN, repr(self.context.user_data))
        self.assertIsNone(self.db.get_user(USER_ID)["mymeet_pending_token"])
        with patch.dict(self.bot, _check_mymeet_sheet_access=MagicMock(side_effect=ValueError("private-fixture"))):
            with self.assertLogs(self.bot["logger"], level="WARNING") as logs:
                self.assertTrue(await self.send(NEW_SHEET))
        self.assertNotIn("private-fixture", "\n".join(logs.output))
        self.assert_active()
        await self.bot["cmd_cancel_mymeet"](self.update, self.context)
        self.assertNotIn("mymeet_setup", self.context.user_data)
        self.assertEqual(self.db.get_user(USER_ID)["mymeet_setup_in_progress"], 0)
        self.assert_active()

    async def test_rejected_token_preserves_active_configuration(self):
        await self.bot["cmd_setup_mymeet"](self.update, self.context)
        error = self.ms.MymeetTokenError(401, "sensitive-fixture")
        with patch.dict(self.bot, verify_token_and_get_workspace=MagicMock(side_effect=error)):
            self.assertTrue(await self.send(NEW_TOKEN))
        self.assert_active()
        self.assertNotIn("sensitive-fixture", repr(self.update.message.mock_calls))
        self.assertNotIn("candidate", self.context.user_data["mymeet_setup"])

    async def test_atomic_promotion_can_retry_after_rollback(self):
        await self.start()
        with sqlite3.connect(self.db.DB_PATH) as conn:
            conn.execute("""
                CREATE TRIGGER fail_promotion BEFORE UPDATE OF mymeet_token ON users
                BEGIN SELECT RAISE(ABORT, 'fixture failure'); END
            """)
        metadata = MagicMock()
        with patch.dict(self.bot, _check_mymeet_sheet_access=metadata):
            self.assertTrue(await self.send(NEW_SHEET))
            self.assert_active()
            self.assertIn("mymeet_setup", self.context.user_data)
            with sqlite3.connect(self.db.DB_PATH) as conn:
                conn.execute("DROP TRIGGER fail_promotion")
            self.assertTrue(await self.send(NEW_SHEET))
            self.assertEqual(metadata.call_count, 2)
        self.assert_active(NEW_TOKEN, "active", NEW_SHEET)
        self.assertEqual(self.db.get_user(USER_ID)["mymeet_setup_in_progress"], 0)
        self.assertNotIn("mymeet_setup", self.context.user_data)

    async def test_expired_and_restarted_drafts_require_setup(self):
        await self.start()
        self.context.user_data["mymeet_setup"]["expires"] = time.monotonic() - 1
        metadata = MagicMock()
        with patch.dict(self.bot, _check_mymeet_sheet_access=metadata):
            self.assertTrue(await self.send(NEW_SHEET))
            self.assert_active()
            await self.start()
            self.context.user_data.clear()
            self.assertTrue(await self.send(NEW_SHEET))
            metadata.assert_not_called()
        self.assert_active()
        self.assertIn("/setup_mymeet", self.update.message.reply_text.call_args.args[0])

    async def test_concurrent_validation_cannot_promote_replaced_draft(self):
        await self.start()
        validating = asyncio.Event()
        finish = asyncio.Event()

        async def validate(func, sheet_id):
            self.assertEqual(sheet_id, NEW_SHEET)
            validating.set()
            await finish.wait()

        with patch.object(asyncio, "to_thread", side_effect=validate):
            previous = asyncio.create_task(self.send(NEW_SHEET))
            try:
                await asyncio.wait_for(validating.wait(), 2)
                await self.bot["cmd_setup_mymeet"](self.update, self.context)
            finally:
                finish.set()
                await asyncio.wait_for(previous, 2)
        self.assert_active()
        self.assertEqual(self.context.user_data["mymeet_setup"]["step"], self.bot["_SETUP_STEP_TOKEN"])

    async def test_sheet_probe_uses_metadata_get_on_pinned_gspread(self):
        session = MagicMock()
        session.request.return_value.ok = True
        session.request.return_value.json.return_value = {"spreadsheetId": NEW_SHEET}
        client = SimpleNamespace(http_client=HTTPClient(auth=None, session=session))
        with patch.object(self.ms, "_gs_client", return_value=client), \
                patch.object(self.ms, "require_spreadsheet_editor") as require_editor:
            self.bot["_check_mymeet_sheet_access"](NEW_SHEET)
        require_editor.assert_called_once_with(NEW_SHEET)
        session.request.assert_called_once_with(
            method="get", url="https://sheets.googleapis.com/v4/spreadsheets/" + NEW_SHEET,
            json=None, params={"fields": "spreadsheetId"}, data=None, files=None, headers=None, timeout=15,
        )

    async def test_token_in_sheet_step_never_reaches_google(self):
        await self.start()
        token = NEW_TOKEN + "X" * 30
        with patch.dict(self.bot, _check_mymeet_sheet_access=MagicMock()) as ns:
            self.assertTrue(await self.send(token))
            ns["_check_mymeet_sheet_access"].assert_not_called()
        self.assert_active()

    async def test_drive_checks_editor_without_writing_or_replacing_on_denial(self):
        session = MagicMock()
        session.get.return_value.status_code = 200
        authorization = MagicMock()
        authorization.return_value.__enter__.return_value = session
        with patch.object(self.ms, "_service_account_credentials") as creds, \
                patch.object(self.ms, "AuthorizedSession", authorization), \
                patch.object(self.ms, "_gs_client") as sheets:
            for metadata in ({"capabilities": {"canEdit": False}},
                             {"capabilities": {"canEdit": True, "canModifyContent": False}}, {}):
                with self.subTest(metadata=metadata):
                    session.get.return_value.json.return_value = metadata
                    await self.start()
                    self.assertTrue(await self.send(NEW_SHEET))
                    self.assert_active()
            session.get.return_value.status_code = 403
            self.assertTrue(await self.send(NEW_SHEET))
            self.assert_active()
            sheets.assert_not_called()
            session.get.return_value.status_code = 200
            session.get.return_value.json.return_value = {"capabilities": {"canEdit": True, "canModifyContent": True}}
            self.assertTrue(await self.send(NEW_SHEET))
            self.assert_active(NEW_TOKEN, "active", NEW_SHEET)
        creds.assert_called_with(["https://www.googleapis.com/auth/drive.metadata.readonly"])
        session.get.assert_called_with(
            "https://www.googleapis.com/drive/v3/files/" + NEW_SHEET,
            params={"fields": "capabilities(canEdit,canModifyContent)", "supportsAllDrives": "true"},
            timeout=15, allow_redirects=False,
        )

    async def test_sync_checks_snapshot_after_calendar_fetch(self):
        def replace_during_calendar_check(telegram_id, days):
            self.db.save_mymeet_config(USER_ID, NEW_TOKEN, "active", NEW_SHEET)
            return {}

        sync = AsyncMock()
        with patch.dict(self.bot, _get_gcal_organizer_data=replace_during_calendar_check, sync_to_sheets_async=sync):
            await self.bot["cmd_sync_mymeet"](self.update, self.context)
        sync.assert_not_awaited()
        self.assert_active(NEW_TOKEN, "active", NEW_SHEET)


def run_mymeet_reconfiguration_tests():
    stream = StringIO()
    suite = defaultTestLoader.loadTestsFromTestCase(MymeetReconfigurationTests)
    result = TextTestRunner(stream=stream).run(suite)
    assert result.wasSuccessful(), stream.getvalue()


if __name__ == "__main__":
    main()
