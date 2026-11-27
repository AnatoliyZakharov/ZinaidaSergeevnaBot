"""Durable intent ledger for a trusted grouped Sheets provider.

The provider must authenticate the destination and fence external editors
independently. Use a dedicated, access-controlled SQLite file on local durable
storage. The ledger stores shape and receipts, never task or protocol contents.
"""
from contextlib import closing
import json
import sqlite3

from my_tasks_grouping import GroupSpan
from mymeet_sync import PendingGroupedWrite


def _encode(groups):
    return json.dumps([[g.key, g.start, g.end, g.depth] for g in groups])


def _decode(raw):
    return tuple(GroupSpan(*group) for group in json.loads(raw))


class SQLiteGroupedWriteJournal:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS grouped_writes (
                    workbook TEXT NOT NULL,
                    sheet INTEGER NOT NULL,
                    owner INTEGER NOT NULL,
                    owned TEXT NOT NULL DEFAULT '[]',
                    receipt TEXT,
                    planned TEXT,
                    PRIMARY KEY (workbook, sheet),
                    CHECK ((receipt IS NULL) = (planned IS NULL))
                )
            """)

    @staticmethod
    def _key(destination):
        return destination.spreadsheet_id, destination.sheet_id

    def _load(self, conn, destination):
        row = conn.execute(
            "SELECT owner, owned, receipt, planned FROM grouped_writes WHERE workbook = ? AND sheet = ?",
            self._key(destination),
        ).fetchone()
        if row is not None and row[0] != destination.telegram_id:
            raise ValueError("Grouped journal belongs to a different user")
        return row

    def load_groups(self, destination):
        with closing(sqlite3.connect(self.path)) as conn:
            row = self._load(conn, destination)
            return _decode(row[1]) if row else ()

    def load_pending_grouped_write(self, destination):
        with closing(sqlite3.connect(self.path)) as conn:
            row = self._load(conn, destination)
            if row and row[2] is not None:
                return PendingGroupedWrite(row[2], _decode(row[3]))
            return None

    def begin_grouped_write(self, destination, pending):
        if not pending.receipt:
            raise ValueError("Missing grouped write receipt")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                INSERT INTO grouped_writes (workbook, sheet, owner) VALUES (?, ?, ?)
                ON CONFLICT(workbook, sheet) DO NOTHING
            """, (*self._key(destination), destination.telegram_id))
            row = self._load(conn, destination)
            if row[2] is not None:
                raise ValueError("Another grouped write is still pending")
            conn.execute("""
                UPDATE grouped_writes SET receipt = ?, planned = ?
                WHERE workbook = ? AND sheet = ?
            """, (pending.receipt, _encode(pending.owned_groups), *self._key(destination)))

    def finalize_grouped_write(self, destination, pending):
        encoded = _encode(pending.owned_groups)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            result = conn.execute("""
                UPDATE grouped_writes SET owned = planned, receipt = NULL, planned = NULL
                WHERE workbook = ? AND sheet = ? AND owner = ? AND receipt = ? AND planned = ?
            """, (*self._key(destination), destination.telegram_id, pending.receipt, encoded))
            if result.rowcount != 1:
                raise ValueError("Grouped write confirmation does not match pending journal")
