"""Prepare Google Sheets structural updates for a verified personal task sheet.

The caller supplies the authenticated sheet snapshot and stable task/meeting
IDs. This module never reads or edits a spreadsheet by itself.
"""
from dataclasses import dataclass
from datetime import date


_SOURCES = ("Telegram", "Google Tasks", "MYmeet")
GROUP_HEADER_PREFIX = "my-tasks-group:"


@dataclass(frozen=True)
class TaskRow:
    key: str
    source: str
    deadline: date | None = None
    values: tuple = ()  # Columns A:H, required only when inserting a new task
    meeting_id: str = ""
    meeting_name: str = ""
    meeting_date: date | None = None
    parent_key: str = ""


@dataclass(frozen=True)
class GroupSpan:
    key: str
    start: int
    end: int
    depth: int


def _date_order(task: TaskRow):
    return task.deadline or date.max, task.key


def _google_task_order(tasks: list[TaskRow]) -> list[str]:
    by_key = {task.key: task for task in tasks}
    children: dict[str, list[TaskRow]] = {}
    for task in sorted(tasks, key=_date_order):
        if task.parent_key and task.parent_key not in by_key:
            raise ValueError("Missing Google Tasks parent")
        children.setdefault(task.parent_key, []).append(task)

    ordered = []
    pending = list(reversed(children.get("", [])))
    while pending:
        task = pending.pop()
        ordered.append(task.key)
        pending.extend(reversed(children.get(task.key, [])))
    if len(ordered) != len(tasks):
        raise ValueError("Cyclic Google Tasks hierarchy")
    return ordered


def _layout(tasks: list[TaskRow]):
    if any(task.source not in _SOURCES or not task.key or task.key.startswith(GROUP_HEADER_PREFIX)
           for task in tasks):
        raise ValueError("Unknown source or invalid stable task key")
    task_rows = {task.key: task.values for task in tasks}
    if len(task_rows) != len(tasks):
        raise ValueError("Duplicate stable task key")

    # One fixed column header precedes these rows. All group spans are zero-based.
    keys = []
    groups = []

    def add_header(key, label):
        keys.append(key)
        task_rows[key] = ("", "", label, "", "", "", "", key)
        return len(keys) + 1

    for source in _SOURCES:
        source_key = f"{GROUP_HEADER_PREFIX}source:{source}"
        start = add_header(source_key, source)
        if source == "Telegram":
            keys.extend(task.key for task in sorted(tasks, key=_date_order)
                        if task.source == source)
        elif source == "Google Tasks":
            keys.extend(_google_task_order([task for task in tasks if task.source == source]))
        else:
            meetings = {}
            meeting_labels = {}
            for task in tasks:
                if task.source != source:
                    continue
                if not task.meeting_id or not task.meeting_name or task.meeting_date is None:
                    raise ValueError("Missing stable MYmeet meeting ID, title or date")
                label = (task.meeting_date, task.meeting_name)
                if meeting_labels.setdefault(task.meeting_id, label) != label:
                    raise ValueError("Inconsistent metadata for the same MYmeet meeting")
                meetings.setdefault(task.meeting_id, []).append(task)
            order = sorted(meetings, key=lambda key: (-meeting_labels[key][0].toordinal(), key))
            for meeting_id in order:
                meeting_key = f"{GROUP_HEADER_PREFIX}meeting:{meeting_id}"
                meeting_date, title = meeting_labels[meeting_id]
                meeting_start = add_header(meeting_key, f"{title} · {meeting_date:%d.%m.%Y}")
                keys.extend(task.key for task in sorted(meetings[meeting_id], key=_date_order))
                groups.append(GroupSpan(meeting_key, meeting_start, len(keys) + 1, 2))
        if start < len(keys) + 1:
            groups.append(GroupSpan(source_key, start, len(keys) + 1, 1))
    return keys, task_rows, groups


def _range(sheet_id, start, end):
    return {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": start, "endIndex": end}


def _value(value):
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    if isinstance(value, str):
        return {"userEnteredValue": {"stringValue": value}}
    raise ValueError("New row must contain only plain cell values")


@dataclass(frozen=True)
class GroupedSheetPlan:
    requests: list[dict]
    owned_groups: list[GroupSpan]


def plan_grouped_sheet_update(
    sheet_id: int,
    row_count: int,
    existing_keys: list[str | None],
    row_groups: list[dict],
    tasks: list[TaskRow],
    *,
    owned_groups: tuple[GroupSpan, ...] = (),
    existing_header_labels: dict[str, str] | None = None,
) -> GroupedSheetPlan:
    """Plan one batchUpdate for rows after the fixed column header.

    existing_keys covers the used rows in physical order. Non-task rows with
    absent keys can be represented by None. Unlisted rows keep their contents and
    relative order. Existing task cells move as whole rows. Only managed meeting labels may change.
    Only ranges previously recorded as owned by the bot for the same spreadsheet
    and sheet may be removed. A header key from the sheet alone is insufficient.
    Persist plan.owned_groups only after confirming the batch was applied.
    Call with a fresh snapshot after an uncertain write. Never replay indexes
    from an earlier snapshot.
    """
    keys = list(existing_keys)
    known = [key for key in keys if key]
    if row_count < len(keys) + 1 or len(known) != len(set(known)):
        raise ValueError("Invalid sheet snapshot or duplicate row keys")

    desired, values, expected_groups = _layout(tasks)
    old_groups = []
    collapsed = {}
    owned_group_set = set(owned_groups)
    for group in row_groups:
        bounds = group["range"]
        start, end = bounds["startIndex"], bounds["endIndex"]
        depth = group["depth"]
        if (bounds["sheetId"] != sheet_id or bounds["dimension"] != "ROWS"
                or not 0 <= start < end <= row_count):
            raise ValueError("Invalid row group in sheet snapshot")
        key = keys[start - 2] if 2 <= start <= len(keys) + 1 else None
        old_group = GroupSpan(key, start, end, depth)
        if old_group not in owned_group_set or key in collapsed:
            raise ValueError("Unmanaged row group in task sheet")
        old_groups.append(old_group)
        collapsed[key] = bool(group.get("collapsed", False))

    old_group_set = set(old_groups)
    labels = existing_header_labels or {}
    managed_meetings = {group.key for group in old_groups if group.depth == 2}
    header_updates = []
    for index, key in enumerate(desired, start=1):
        if key not in managed_meetings or key not in labels or labels[key] == values[key][2]:
            continue
        if not isinstance(labels[key], str) or labels[key].startswith("="):
            raise ValueError("Cannot replace a non-text meeting header")
        header_updates.append({"updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": index, "columnIndex": 2},
            "rows": [{"values": [_value(values[key][2])]}],
            "fields": "userEnteredValue",
        }})
    if keys[:len(desired)] == desired and old_group_set == set(expected_groups):
        return GroupedSheetPlan(header_updates, expected_groups)

    requests = []
    for group in sorted(old_groups, key=lambda group: (-group.depth, group.start)):
        requests.append({"deleteDimensionGroup": {"range": _range(sheet_id, group.start, group.end)}})

    for index, key in enumerate(desired, start=1):
        try:
            old_index = keys.index(key, index - 1) + 1
        except ValueError:
            old_index = None
        if old_index == index:
            continue
        if old_index is not None:
            requests.append({"moveDimension": {"source": _range(sheet_id, old_index, old_index + 1),
                                               "destinationIndex": index}})
            keys.pop(old_index - 1)
        else:
            row = values[key]
            if len(row) != 8 or row[7] != key:
                raise ValueError("New task row is missing values or its stable key")
            if index == row_count:
                requests.append({"appendDimension": {"sheetId": sheet_id, "dimension": "ROWS",
                                                      "length": 1}})
            else:
                requests.append({"insertDimension": {"range": _range(sheet_id, index, index + 1),
                                                      "inheritFromBefore": False}})
            row_count += 1
            requests.append({"updateCells": {
                "start": {"sheetId": sheet_id, "rowIndex": index, "columnIndex": 0},
                "rows": [{"values": [_value(value) for value in row]}],
                "fields": "userEnteredValue",
            }})
        keys.insert(index - 1, key)

    for group in sorted(expected_groups, key=lambda group: (group.depth, group.start)):
        requests.append({"addDimensionGroup": {"range": _range(sheet_id, group.start, group.end)}})
    for group in sorted(expected_groups, key=lambda group: (-group.depth, group.start)):
        if collapsed.get(group.key):
            requests.append({"updateDimensionGroup": {
                "dimensionGroup": {"range": _range(sheet_id, group.start, group.end),
                                   "depth": group.depth, "collapsed": True},
                "fields": "collapsed",
            }})
    return GroupedSheetPlan([*requests, *header_updates], expected_groups)
