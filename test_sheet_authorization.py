"""Synthetic checks for the user/file boundary at every spreadsheet write entry."""
from io import StringIO
import sys
from types import SimpleNamespace as NS
from unittest import TestCase, TextTestRunner, defaultTestLoader
from unittest.mock import Mock, patch

from test_my_tasks import _load_my_tasks
from test_smoke import _load_mymeet


DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
SHEET_ID = "syntheticSheet"
TELEGRAM_ID = 7000123
GRANT_VERSION = "synthetic-version"


class SpreadsheetAuthorizationTests(TestCase):
    def setUp(self):
        self.module = _load_mymeet()
        self.creds = object()
        self.grant = {"version": GRANT_VERSION, "granted": [DRIVE_FILE]}
        self.auth = NS(
            SCOPE_DRIVE_FILE=DRIVE_FILE,
            get_credentials_and_grant_checked=Mock(return_value=(self.creds, self.grant, None)),
        )
        self.db = NS(get_sheet_grant_binding=Mock(return_value=GRANT_VERSION))
        self.enterContext(patch.dict(sys.modules, {"auth": self.auth, "db": self.db}))
        self.session = self.enterContext(patch.object(self.module, "AuthorizedSession"))
        self.authorize = self.enterContext(patch.object(self.module.gspread, "authorize"))
        self.drive = self.session.return_value.__enter__.return_value
        self.drive.get.return_value.status_code = 200
        self.valid_metadata = {
            "id": SHEET_ID,
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "trashed": False,
            "isAppAuthorized": True,
            "capabilities": {"canEdit": True, "canModifyContent": True},
        }
        self.drive.get.return_value.json.return_value = self.valid_metadata

    def test_selection_proves_edit_rights_with_delegated_user(self):
        self.assertEqual(self.module.require_spreadsheet_editor(SHEET_ID, TELEGRAM_ID), GRANT_VERSION)
        self.auth.get_credentials_and_grant_checked.assert_called_once_with(TELEGRAM_ID)
        self.session.assert_called_once_with(self.creds, refresh_timeout=15)
        self.assertEqual(self.drive.get.call_args.args[0], "https://www.googleapis.com/drive/v3/files/" + SHEET_ID)
        self.assertFalse(self.drive.get.call_args.kwargs["allow_redirects"])
        self.authorize.assert_called_once_with(self.creds)
        self.authorize.return_value.open_by_key.assert_called_once_with(SHEET_ID)

    def test_capabilities_and_exact_file_identity_are_required(self):
        for overrides in (
            {"id": "different"},
            {"mimeType": "application/json"},
            {"trashed": True},
            {"isAppAuthorized": False},
            {"capabilities": {"canEdit": True}},
            {"capabilities": {"canEdit": False, "canModifyContent": True}},
        ):
            with self.subTest(overrides=overrides):
                self.drive.get.return_value.json.return_value = {**self.valid_metadata, **overrides}
                with self.assertRaises(self.module.SpreadsheetAuthorizationError):
                    self.module.require_spreadsheet_editor(SHEET_ID, TELEGRAM_ID)
        self.authorize.assert_not_called()

    def test_revocation_rejects_full_append_and_personal_sheet_before_writes(self):
        self.drive.get.return_value.status_code = 403
        with patch.object(self.module, "_fetch_meetings") as fetch:
            config = {"mymeet_spreadsheet_id": SHEET_ID, "telegram_id": TELEGRAM_ID}
            for append in (True, False):
                with self.subTest(append=append), self.assertRaises(self.module.SpreadsheetAuthorizationError):
                    self.module.sync_to_sheets(user_config=config, append=append)
            fetch.assert_not_called()
        with self.assertRaises(self.module.SpreadsheetAuthorizationError):
            self.module.refresh_my_tasks_sheet(SHEET_ID, "Test Owner", object(), telegram_id=TELEGRAM_ID)
        self.authorize.assert_not_called()

    def test_personal_tasks_denial_skips_local_and_external_task_writes(self):
        self.db.get_sheet_grant_binding.return_value = None
        mt = _load_my_tasks()
        dbm = Mock()
        dbm.get_user.return_value = {"mymeet_spreadsheet_id": SHEET_ID,
                                     "mymeet_full_name": "Test Owner"}
        result = mt.run_sync(TELEGRAM_ID, with_gtasks=False, dbm=dbm, ms=self.module)
        self.assertEqual(result["outcome"], mt.OUT_CONFIG)
        self.assertEqual(dbm.method_calls[0].args, (TELEGRAM_ID,))
        self.assertEqual(len(dbm.method_calls), 1)
        self.authorize.assert_not_called()

    def test_existing_configuration_needs_user_selection_and_same_version(self):
        for binding in (None, "previous-version"):
            with self.subTest(binding=binding):
                self.db.get_sheet_grant_binding.return_value = binding
                with self.assertRaises(self.module.SpreadsheetAuthorizationError):
                    self.module._open_user_spreadsheet(TELEGRAM_ID, SHEET_ID)
        self.drive.get.assert_not_called()
        self.authorize.assert_not_called()

    def test_read_only_or_unscoped_grant_has_no_sheets_access(self):
        self.grant["granted"] = []
        with self.assertRaises(self.module.SpreadsheetAuthorizationError):
            self.module.require_spreadsheet_editor(SHEET_ID, TELEGRAM_ID)
        self.drive.get.assert_not_called()
        self.authorize.assert_not_called()


def run_sheet_authorization_tests():
    stream = StringIO()
    suite = defaultTestLoader.loadTestsFromTestCase(SpreadsheetAuthorizationTests)
    result = TextTestRunner(stream=stream).run(suite)
    assert result.wasSuccessful(), stream.getvalue()
