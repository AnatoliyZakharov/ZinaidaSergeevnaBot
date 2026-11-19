"""Credential storage and restore regressions. All databases and keys are synthetic."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

import crypto
import db


class CredentialStorageTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "fixture.sqlite"
        self.key = Fernet.generate_key().decode()
        self.enterContext(patch.dict(os.environ, {"ENCRYPTION_KEY": self.key}))
        self.enterContext(patch.object(crypto, "_fernet", None))
        self.enterContext(patch.object(db, "DB_PATH", self.path))
        db.init_db()
        db.upsert_user(1, "fixture")
        db.upsert_user(2, "other-fixture")

    def _seed_plaintext(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "UPDATE users SET google_access_token=?, google_refresh_token=?, "
                "google_token_expiry=?, telemost_token=?, calendar_id=?, timezone=? WHERE telegram_id=1",
                ("access-fixture", "refresh-fixture", 1234, "telemost-fixture", "local-calendar", "UTC"),
            )

    def _backup(self, source, name):
        target = self.path.with_name(name)
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            self.assertEqual(dst.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        return target

    def _rows(self, path):
        with closing(sqlite3.connect(path)) as conn:
            return conn.execute("SELECT * FROM users ORDER BY telegram_id").fetchall()

    def _promote(self, refresh="refresh-fixture"):
        return db.save_google_grant(
            1, access_token="access-fixture", refresh_token=refresh, expiry=3456,
            subject="google-subject-fixture",
            granted={"https://www.googleapis.com/auth/calendar", db._DRIVE_FILE_SCOPE},
        )

    def test_legacy_read_and_authenticated_envelope(self):
        self._seed_plaintext()
        rows = self._rows(self.path)
        self.assertEqual(db.get_google_grant(1)["refresh_token"], "refresh-fixture")
        self.assertEqual(db.get_telemost_token(1), "telemost-fixture")
        self.assertEqual(self._rows(self.path), rows)
        with self.assertRaises(crypto.CryptoDecryptError):
            self._promote()
        self.assertEqual(self._rows(self.path), rows)
        db.save_mymeet_config(1, "mymeet-fixture", "workspace-fixture", "sheet-fixture")
        self._promote()
        db.save_telemost_token(1, "telemost-fixture")
        row = db.get_user(1)
        for field in db._TOKEN_FIELDS:
            self.assertTrue(row[field].startswith(db._CREDENTIAL_PREFIX))
        self.assertEqual(db.get_google_grant(1)["calendar_id"], "local-calendar")
        self.assertEqual(db.get_google_grant(1)["access_token"], "access-fixture")
        self.assertEqual(db.get_telemost_token(1), "telemost-fixture")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE users SET telemost_token=? WHERE telegram_id=2", (row["telemost_token"],))
        with self.assertRaises(crypto.CryptoDecryptError):
            db.get_telemost_token(2)

    def test_first_credentials_require_the_key_of_existing_witness(self):
        db.save_mymeet_config(2, "mymeet-fixture", "workspace-fixture", "sheet-fixture")
        before = self._rows(self.path)
        with patch.dict(os.environ, {"ENCRYPTION_KEY": Fernet.generate_key().decode()}), \
                patch.object(crypto, "_fernet", None):
            with self.assertRaises(crypto.CryptoDecryptError):
                self._promote()
            with self.assertRaises(crypto.CryptoDecryptError):
                db.save_telemost_token(1, "first-telemost-fixture")
        self.assertEqual(self._rows(self.path), before)
        self._promote()
        db.save_telemost_token(1, "first-telemost-fixture")
        self.assertEqual(db.get_telemost_token(1), "first-telemost-fixture")

    def test_wrong_key_preserves_existing_mymeet_configuration(self):
        db.save_mymeet_config(1, "old-mymeet-fixture", "workspace-fixture", "sheet-fixture")
        before = self._rows(self.path)
        with patch.dict(os.environ, {"ENCRYPTION_KEY": Fernet.generate_key().decode()}), \
                patch.object(crypto, "_fernet", None):
            with self.assertRaises(crypto.CryptoDecryptError):
                db.save_mymeet_config(1, "new-mymeet-fixture", "other-workspace", "other-sheet")
        self.assertEqual(self._rows(self.path), before)

    def test_migration_rejects_a_known_production_path_and_hardlink(self):
        production_fixture = self._backup(self.path, "production-fixture.sqlite")
        same_inode = production_fixture.with_name("hardlink.sqlite")
        same_inode.hardlink_to(production_fixture)
        with patch.object(db, "_PRODUCTION_DB_PATH", production_fixture):
            for path in (production_fixture, same_inode):
                with self.assertRaises(ValueError):
                    db.migrate_credential_envelopes(path)

    def test_wrong_or_missing_key_preserves_existing_grant(self):
        self._promote()
        rows = self._rows(self.path)
        for replacement in ("", Fernet.generate_key().decode()):
            with patch.dict(os.environ, {"ENCRYPTION_KEY": replacement}), patch.object(crypto, "_fernet", None):
                with self.assertRaises((RuntimeError, crypto.CryptoDecryptError)):
                    self._promote("replacement-fixture")
                with self.assertRaises((RuntimeError, crypto.CryptoDecryptError)):
                    db.disconnect_google_grant(1)
                self.assertEqual(self._rows(self.path), rows)

    def test_migration_backup_restore_and_repeat(self):
        self._seed_plaintext()
        original = self._rows(self.path)
        backup = self._backup(self.path, "before.sqlite")
        copy = self._backup(backup, "copy.sqlite")
        fingerprint = hashlib.sha256(self.key.encode()).hexdigest()
        with self.assertRaises(crypto.CryptoDecryptError):
            db.migrate_credential_envelopes(copy)
        self.assertEqual(self._rows(copy), original)
        for replacement in ("", Fernet.generate_key().decode()):
            with patch.dict(os.environ, {"ENCRYPTION_KEY": replacement}), patch.object(crypto, "_fernet", None):
                with self.assertRaises((RuntimeError, crypto.CryptoDecryptError)):
                    db.migrate_credential_envelopes(copy, expected_key_fingerprint=fingerprint)
                self.assertEqual(self._rows(copy), original)
        alias = self.path.with_name("alias.sqlite")
        os.link(self.path, alias)
        for live in (self.path, alias):
            with self.assertRaises(ValueError):
                db.migrate_credential_envelopes(live, expected_key_fingerprint=fingerprint)
        self.assertEqual(db.migrate_credential_envelopes(copy, expected_key_fingerprint=fingerprint), 1)
        encrypted = self._rows(copy)
        self.assertEqual(db.migrate_credential_envelopes(copy), 0)
        self.assertEqual(self._rows(copy), encrypted)
        with patch.object(db, "DB_PATH", copy):
            self.assertEqual(db.get_google_grant(1)["access_token"], "access-fixture")
            self.assertEqual(db.get_telemost_token(1), "telemost-fixture")
        recovered = self._backup(backup, "recovered.sqlite")
        self.assertEqual(self._rows(recovered), original)
        self.assertEqual(self._rows(self.path), original)

    def test_legacy_writes_reject_wrong_key_when_mymeet_exists(self):
        self._seed_plaintext()
        db.save_mymeet_config(1, "mymeet-fixture", "workspace-fixture", "sheet-fixture")
        rows = self._rows(self.path)
        wrong = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"ENCRYPTION_KEY": wrong}), patch.object(crypto, "_fernet", None):
            with self.assertRaises(crypto.CryptoDecryptError):
                self._promote()
            with self.assertRaises(crypto.CryptoDecryptError):
                db.save_telemost_token(1, "replacement-telemost")
        self.assertEqual(self._rows(self.path), rows)

    def test_conversion_rolls_back_when_second_update_fails(self):
        self._seed_plaintext()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE users SET telemost_token = 'second-fixture' WHERE telegram_id = 2")
        copy = self._backup(self.path, "interrupted.sqlite")
        with closing(sqlite3.connect(copy)) as conn, conn:
            conn.execute(
                "CREATE TRIGGER interrupt_conversion BEFORE UPDATE ON users "
                "WHEN old.telegram_id = 2 BEGIN SELECT RAISE(ABORT, 'fixture stop'); END;"
            )
        rows = self._rows(copy)
        fingerprint = hashlib.sha256(self.key.encode()).hexdigest()
        with self.assertRaises(sqlite3.IntegrityError):
            db.migrate_credential_envelopes(copy, expected_key_fingerprint=fingerprint)
        self.assertEqual(self._rows(copy), rows)

    def test_stale_refresh_and_sheet_binding_cannot_replace_current_grant(self):
        version = self._promote()
        db.save_mymeet_config(1, "mymeet-fixture", "workspace-fixture", "sheet-fixture",
                              expected_google_grant_version=version)
        self.assertEqual(db.get_sheet_grant_binding(1, "sheet-fixture"), version)
        self.assertIsNone(db.get_sheet_grant_binding(1, "other-sheet"))
        updated = self._promote("new-refresh-fixture")
        self.assertNotEqual(version, updated)
        self.assertIsNone(db.get_sheet_grant_binding(1, "sheet-fixture"))
        self.assertFalse(db.refresh_google_grant(
            1, version, access_token="stale-access-fixture", expiry=7890,
        ))
        with self.assertRaises(PermissionError):
            db.save_mymeet_config(1, "stale-mymeet", "stale-workspace", "stale-sheet",
                                  expected_google_grant_version=version)
        self.assertEqual(db.get_user(1)["mymeet_spreadsheet_id"], "sheet-fixture")
        self.assertEqual(db.get_google_grant(1)["refresh_token"], "new-refresh-fixture")


if __name__ == "__main__":
    unittest.main()
