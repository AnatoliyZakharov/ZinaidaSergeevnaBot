"""Offline regression checks for Google Sheets DimensionGroup requests."""
from datetime import date
import unittest

from my_tasks_grouping import GroupSpan, TaskRow, plan_grouped_sheet_update


SHEET = 27
TODAY = date(2026, 11, 26)
YESTERDAY = date(2026, 11, 25)
SOURCE = "my-tasks-group:source:"
MEETING = "my-tasks-group:meeting:"


def task(key, source, due=None, *, meeting="", meeting_day=None):
    return TaskRow(key, source, due, (1, "", source, key, "", "", "", key),
                   meeting, "Одинаковое название" if meeting else "", meeting_day)


def group(start, end, depth, collapsed=False):
    return {"range": {"sheetId": SHEET, "dimension": "ROWS",
                      "startIndex": start, "endIndex": end},
            "depth": depth, "collapsed": collapsed}


def requests_of(requests, name):
    return [request[name] for request in requests if name in request]


def apply_row_plan(requests, originals):
    rows = list(originals)
    for request in requests:
        if "moveDimension" in request:
            op = request["moveDimension"]
            source = op["source"]["startIndex"]
            row = rows.pop(source)
            rows.insert(op["destinationIndex"], row)
        elif "insertDimension" in request:
            rows.insert(request["insertDimension"]["range"]["startIndex"], None)
        elif "appendDimension" in request:
            rows.append(None)
        elif "updateCells" in request:
            op = request["updateCells"]
            index = op["start"]["rowIndex"]
            # No write targets an existing row, even after moving it.
            if rows[index] is not None:
                raise AssertionError("Overwrites an existing task row")
            rows[index] = tuple(next(iter(cell["userEnteredValue"].values()))
                                for cell in op["rows"][0]["values"])
    return rows


class GroupingTests(unittest.TestCase):
    def setUp(self):
        self.tasks = [
            task("late", "Telegram"),
            task("urgent", "Telegram", YESTERDAY),
            task("gt", "Google Tasks", TODAY),
            task("my-old", "MYmeet", TODAY, meeting="old", meeting_day=YESTERDAY),
            task("my-none", "MYmeet", meeting="new", meeting_day=TODAY),
            task("my-due", "MYmeet", TODAY, meeting="new", meeting_day=TODAY),
        ]

    def test_moves_whole_rows_and_nests_meetings_in_native_groups(self):
        order = ["my-old", "late", "gt", "my-none", "my-due", "urgent", "legacy"]
        original_rows = [((key,) * 7) + (key, {"unmanaged_metadata": object()}) for key in order]
        plan = plan_grouped_sheet_update(SHEET, 9, order, [], self.tasks)
        requests = plan.requests
        rows = apply_row_plan(requests, ["header", *original_rows])[1:]
        self.assertEqual([r[7] for r in rows], [
            SOURCE + "Telegram", "urgent", "late", SOURCE + "Google Tasks", "gt",
            SOURCE + "MYmeet", MEETING + "new", "my-due", "my-none", MEETING + "old",
            "my-old", "legacy",
        ])
        self.assertIs(rows[1], original_rows[5])
        self.assertIs(rows[-1], original_rows[-1])
        self.assertEqual(rows[6][2], "Одинаковое название · 26.11.2026")
        ranges = [op["range"] for op in requests_of(requests, "addDimensionGroup")]
        self.assertEqual([(r["startIndex"], r["endIndex"]) for r in ranges],
                         [(2, 4), (5, 6), (7, 12), (8, 10), (11, 12)])
        self.assertEqual(requests_of(requests, "deleteDimension"), [])
        self.assertEqual(plan_grouped_sheet_update(
            SHEET, 20, [row[7] for row in rows],
            [group(2, 4, 1), group(5, 6, 1), group(7, 12, 1, True),
             group(8, 10, 2), group(11, 12, 2, True)], self.tasks,
            owned_groups=tuple(plan.owned_groups)).requests, [])

    def test_restores_collapsed_by_meeting_id_after_insertion_and_reorder(self):
        old_keys = [SOURCE + "Telegram", "urgent", "late", SOURCE + "Google Tasks", "gt",
                    SOURCE + "MYmeet", MEETING + "old", "my-old",
                    MEETING + "new", "my-none", "my-due"]
        old_groups = [group(2, 4, 1), group(5, 6, 1), group(7, 12, 1, True),
                      group(8, 9, 2, True), group(10, 12, 2)]
        new_task = task("fresh", "MYmeet", TODAY, meeting="old", meeting_day=YESTERDAY)
        ownership = (GroupSpan(SOURCE + "Telegram", 2, 4, 1),
                     GroupSpan(SOURCE + "Google Tasks", 5, 6, 1),
                     GroupSpan(SOURCE + "MYmeet", 7, 12, 1),
                     GroupSpan(MEETING + "old", 8, 9, 2),
                     GroupSpan(MEETING + "new", 10, 12, 2))
        requests = plan_grouped_sheet_update(
            SHEET, 13, old_keys, old_groups, [*self.tasks, new_task],
            owned_groups=ownership).requests
        restored = [op["dimensionGroup"] for op in requests_of(requests, "updateDimensionGroup")]
        self.assertEqual(restored, [group(11, 13, 2, True), group(7, 13, 1, True)])
        deleted = [op["range"] for op in requests_of(requests, "deleteDimensionGroup")]
        self.assertEqual(deleted[:2], [old_groups[3]["range"], old_groups[4]["range"]])

    def test_rejects_ambiguous_keys_and_unmanaged_groups(self):
        with self.assertRaisesRegex(ValueError, "duplicate row keys"):
            plan_grouped_sheet_update(SHEET, 20, ["late", "late"], [], self.tasks)
        with self.assertRaisesRegex(ValueError, "Unmanaged row group"):
            plan_grouped_sheet_update(SHEET, 20, ["late"], [group(1, 2, 1)], self.tasks)
        with self.assertRaisesRegex(ValueError, "Unmanaged row group"):
            plan_grouped_sheet_update(SHEET, 20, [SOURCE + "Telegram", "late"],
                                      [group(2, 3, 1)], self.tasks)
        with self.assertRaisesRegex(ValueError, "Missing stable"):
            plan_grouped_sheet_update(SHEET, 20, [], [], [task("mine", "MYmeet")])

    def test_empty_sheets_append_only_and_duplicate_meeting_titles_do_not_merge(self):
        tasks = [task("first", "MYmeet", meeting="instance1", meeting_day=TODAY),
                 task("second", "MYmeet", meeting="instance2", meeting_day=TODAY)]
        requests = plan_grouped_sheet_update(SHEET, 1, [], [], tasks).requests
        self.assertEqual(len(requests_of(requests, "addDimensionGroup")), 3)
        self.assertEqual(len(requests_of(requests, "appendDimension")), 7)
        self.assertEqual(requests_of(requests, "insertDimension"), [])
        self.assertEqual(requests_of(requests, "moveDimension"), [])


TESTS = [
    (GroupingTests("test_moves_whole_rows_and_nests_meetings_in_native_groups").debug,
     "Мои задачи: группы источников и встреч, целые строки, повтор"),
    (GroupingTests("test_restores_collapsed_by_meeting_id_after_insertion_and_reorder").debug,
     "Мои задачи: сохранение свёрнутости при перестановке и добавлении"),
    (GroupingTests("test_rejects_ambiguous_keys_and_unmanaged_groups").debug,
     "Мои задачи: отказ при дублях ключей и непроверенных группах"),
    (GroupingTests("test_empty_sheets_append_only_and_duplicate_meeting_titles_do_not_merge").debug,
     "Мои задачи: пустой лист и совпавшие названия встреч"),
]


if __name__ == "__main__":
    unittest.main()
