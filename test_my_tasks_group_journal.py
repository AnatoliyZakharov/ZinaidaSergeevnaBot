"""Isolated SQLite coverage for grouped write intent across connection lifetimes."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from my_tasks_group_journal import SQLiteGroupedWriteJournal
from my_tasks_grouping import GroupSpan
from mymeet_sync import PendingGroupedWrite, VerifiedGroupedDestination


class GroupJournalTests(unittest.TestCase):
    def test_pending_groups_survive_restart_and_require_exact_confirmation(self):
        target = VerifiedGroupedDestination(1, "test-book", 27)
        pending = PendingGroupedWrite("receipt-one", (GroupSpan("section", 2, 5, 1),))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.sqlite"
            SQLiteGroupedWriteJournal(path).begin_grouped_write(target, pending)
            reopened = SQLiteGroupedWriteJournal(path)
            self.assertEqual(reopened.load_pending_grouped_write(target), pending)
            self.assertEqual(reopened.load_groups(target), ())
            with self.assertRaisesRegex(ValueError, "does not match"):
                reopened.finalize_grouped_write(target, PendingGroupedWrite(pending.receipt, ()))
            with self.assertRaisesRegex(ValueError, "different user"):
                reopened.load_pending_grouped_write(VerifiedGroupedDestination(2, "test-book", 27))
            reopened.finalize_grouped_write(target, pending)
            completed = SQLiteGroupedWriteJournal(path)
            self.assertIsNone(completed.load_pending_grouped_write(target))
            self.assertEqual(completed.load_groups(target), pending.owned_groups)

    def test_concurrent_begin_permits_only_one_pending_operation(self):
        target = VerifiedGroupedDestination(1, "test-book", 27)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.sqlite"
            first, second = SQLiteGroupedWriteJournal(path), SQLiteGroupedWriteJournal(path)
            start = Barrier(2)

            def begin(journal, receipt):
                start.wait(timeout=5)
                try:
                    journal.begin_grouped_write(target, PendingGroupedWrite(receipt, ()))
                except ValueError:
                    return None
                return receipt

            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(begin, first, "first")
                b = pool.submit(begin, second, "second")
                winners = [value for value in (a.result(), b.result()) if value is not None]
            self.assertEqual(len(winners), 1)
            self.assertEqual(first.load_pending_grouped_write(target).receipt, winners[0])


TESTS = [
    (GroupJournalTests("test_pending_groups_survive_restart_and_require_exact_confirmation").debug,
     "Мои задачи: устойчивый журнал точной квитанции"),
    (GroupJournalTests("test_concurrent_begin_permits_only_one_pending_operation").debug,
     "Мои задачи: единственное устойчивое намерение для книги и листа"),
]
