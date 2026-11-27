"""Run the focused synthetic PR 54 stand in a fresh, network-disabled process."""
import os
from pathlib import Path
import sys
import tempfile
import unittest


def reject_network(event, _args):
    if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo",
                 "socket.gethostbyname", "socket.gethostbyaddr"}:
        raise RuntimeError("External network is disabled in the MYmeet stand")


def main():
    sys.addaudithook(reject_network)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix="zinochka-mymeet-stand-") as tmp:
        os.environ["DB_PATH"] = str(Path(tmp) / "stand.sqlite")
        os.environ["GOOGLE_SA_KEY"] = "{}"
        from test_my_tasks_group_journal import TESTS as journal
        from test_my_tasks_grouping import TESTS as grouping
        from test_mymeet_daily import TESTS as daily

        focused = [(run, label) for run, label in daily
                   if run.__name__.startswith(("test_grouped_", "test_foreign_", "test_assigned_"))]
        suite = unittest.TestSuite(unittest.FunctionTestCase(run, description=label)
                                   for run, label in [*journal, *grouping, *focused])
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
