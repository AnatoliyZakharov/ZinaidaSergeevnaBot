"""Offline regression checks for Google Sheets DimensionGroup requests."""
from dataclasses import replace
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
        metadata = {
            "formula": '=IF(A2=1,"Готово","")',
            "note": "Уточнить у участника",
            "threaded_comment": {"text": "Проверить срок", "replies": ["Принято"]},
            "hyperlink": "https://example.test/task/1",
            "dataValidation": {"condition": {"type": "ONE_OF_LIST"}},
            "userEnteredFormat": {"textFormat": {"bold": True}},
        }
        originals = {
            key: (1, "26.11.2026", "", "Тестовое поручение", "", "30.11.2026",
                  "В работе", key, metadata.copy())
            for key in old_keys
        }
        updated = apply_row_plan(requests, ["header", *originals.values()])
        for row in updated[1:]:
            if row[7] in originals:
                self.assertIs(row, originals[row[7]])
        self.assertEqual(set(originals) - {row[7] for row in updated[1:]}, set())
        self.assertEqual(updated[-1][6:], ("В работе", "my-old", metadata))
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

    def test_google_tasks_sorts_roots_and_siblings_without_separating_subtasks(self):
        parent = task("parent", "Google Tasks", YESTERDAY)
        late = replace(task("late-child", "Google Tasks"), parent_key=parent.key)
        early = replace(task("early-child", "Google Tasks", TODAY), parent_key=parent.key)
        standalone = task("standalone", "Google Tasks", TODAY)
        tasks = [late, standalone, early, parent]
        plan = plan_grouped_sheet_update(SHEET, 1, [], [], tasks)
        rows = apply_row_plan(plan.requests, ["header"])
        self.assertEqual([row[7] for row in rows[1:] if not row[7].startswith("my-tasks-group:")],
                         ["parent", "early-child", "late-child", "standalone"])
        with self.assertRaisesRegex(ValueError, "Missing Google Tasks parent"):
            plan_grouped_sheet_update(SHEET, 1, [], [], [early])
        with self.assertRaisesRegex(ValueError, "Cyclic Google Tasks hierarchy"):
            plan_grouped_sheet_update(SHEET, 1, [], [], [early, replace(parent, parent_key=early.key)])

    def test_renamed_meeting_updates_only_managed_header_value(self):
        meeting = replace(self.tasks[3], meeting_name="Уточнённое название")
        keys = [SOURCE + "Telegram", SOURCE + "Google Tasks", SOURCE + "MYmeet",
                MEETING + "old", meeting.key]
        owned = (GroupSpan(SOURCE + "MYmeet", 4, 6, 1), GroupSpan(MEETING + "old", 5, 6, 2))
        groups = [group(4, 6, 1, True), group(5, 6, 2, True)]
        before = "Одинаковое название · 25.11.2026"
        after = "Уточнённое название · 25.11.2026"
        plan = plan_grouped_sheet_update(SHEET, 6, keys, groups, [meeting],
                                         owned_groups=owned,
                                         existing_header_labels={MEETING + "old": before})
        self.assertEqual(plan.requests, [{"updateCells": {
            "start": {"sheetId": SHEET, "rowIndex": 4, "columnIndex": 2},
            "rows": [{"values": [{"userEnteredValue": {"stringValue": after}}]}],
            "fields": "userEnteredValue",
        }}])
        self.assertEqual(plan_grouped_sheet_update(
            SHEET, 6, keys, groups, [meeting], owned_groups=owned,
            existing_header_labels={MEETING + "old": after}).requests, [])
        with self.assertRaisesRegex(ValueError, "non-text meeting header"):
            plan_grouped_sheet_update(SHEET, 6, keys, groups, [meeting], owned_groups=owned,
                                      existing_header_labels={MEETING + "old": "=A1"})

    def test_existing_cell_update_uses_final_key_position(self):
        tasks = self.tasks[:2]
        keys = [SOURCE + "Telegram", "late", "urgent", SOURCE + "Google Tasks", SOURCE + "MYmeet"]
        plan = plan_grouped_sheet_update(
            SHEET, 6, keys, [group(2, 4, 1)], tasks,
            owned_groups=(GroupSpan(SOURCE + "Telegram", 2, 4, 1),),
            existing_cell_updates={"urgent": {3: "Новое название"}},
        )
        self.assertEqual(requests_of(plan.requests, "moveDimension")[0]["destinationIndex"], 2)
        self.assertEqual(requests_of(plan.requests, "updateCells"), [{
            "start": {"sheetId": SHEET, "rowIndex": 2, "columnIndex": 3},
            "rows": [{"values": [{"userEnteredValue": {"stringValue": "Новое название"}}]}],
            "fields": "userEnteredValue",
        }])

    def test_empty_sheets_append_only_and_duplicate_meeting_titles_do_not_merge(self):
        tasks = [task("first", "MYmeet", meeting="instance1", meeting_day=TODAY),
                 task("second", "MYmeet", meeting="instance2", meeting_day=TODAY)]
        requests = plan_grouped_sheet_update(SHEET, 1, [], [], tasks).requests
        self.assertEqual(len(requests_of(requests, "addDimensionGroup")), 3)
        self.assertEqual(len(requests_of(requests, "appendDimension")), 7)
        self.assertEqual(requests_of(requests, "insertDimension"), [])
        self.assertEqual(requests_of(requests, "moveDimension"), [])


TESTS = [
    (GroupingTests("test_existing_cell_update_uses_final_key_position").debug,
     "Мои задачи: разрешённая ячейка обновляется по позиции устойчивого ключа"),
    (GroupingTests("test_renamed_meeting_updates_only_managed_header_value").debug,
     "Мои задачи: обновление заголовка встречи по постоянному ID"),
    (GroupingTests("test_google_tasks_sorts_roots_and_siblings_without_separating_subtasks").debug,
     "Мои задачи: сортировка без разрыва дерева Google Tasks"),
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
