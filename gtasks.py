"""
gtasks.py — Google Tasks API.

Позволяет читать открытые задачи и отмечать их выполненными.
Использует те же Google Credentials, что и calendar (auth.get_credentials).

Важно: для работы нужен scope https://www.googleapis.com/auth/tasks.
Пользователи, подключившиеся до добавления этого scope, должны
переподключиться через /connect.
"""

import logging
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import auth

logger = logging.getLogger(__name__)


def _get_service(telegram_id: int):
    """Возвращает Tasks API service или None если нет авторизации."""
    creds = auth.get_credentials(telegram_id)
    if not creds:
        return None
    return build("tasks", "v1", credentials=creds)


def list_all_tasks(telegram_id: int) -> tuple[list[dict], str | None]:
    """
    Собирает незакрытые задачи из всех списков Google Tasks.

    Возвращает (tasks, error):
      - tasks: список задач (каждая содержит _listId, _listTitle)
      - error: строка с ошибкой или None
    """
    service = _get_service(telegram_id)
    if not service:
        return [], "no_auth"

    try:
        lists_result = service.tasklists().list(maxResults=10).execute()
    except HttpError as e:
        if e.resp.status == 403:
            return [], "no_scope"
        logger.error("Tasks: ошибка получения списков: %s", e)
        return [], "api_error"

    task_lists = lists_result.get("items", [])
    all_tasks: list[dict] = []

    for tl in task_lists:
        try:
            result = service.tasks().list(
                tasklist=tl["id"],
                showCompleted=False,
                showHidden=False,
                maxResults=20,
            ).execute()
            tasks = result.get("items", [])
            for t in tasks:
                t["_listId"]    = tl["id"]
                t["_listTitle"] = tl.get("title", "Мои задачи")
            all_tasks.extend(tasks)
        except HttpError as e:
            logger.error("Tasks: ошибка списка %s: %s", tl["id"], e)

    # Сортируем: задачи с дедлайном вперёд, потом по порядку в списке
    all_tasks.sort(key=lambda t: (t.get("due", "z"), t.get("position", "")))
    return all_tasks, None


MAX_IMPORT_TASKS = 2000   # предохранитель: больше — ответ считается неполным


def list_all_tasks_full(telegram_id: int, service=None) -> tuple[list[dict], str | None, bool]:
    """Все задачи всех списков, включая выполненные и скрытые (импорт в «Мои задачи»).

    Постранично (nextPageToken).  Удалённые (deleted) не возвращаются.
    Возвращает (tasks, error, complete):
      complete=False — какой-то список/страница не загрузились или превышен
      MAX_IMPORT_TASKS: вызывающий НЕ должен считать отсутствующие задачи удалёнными.
    """
    if service is None:
        service = _get_service(telegram_id)
    if not service:
        return [], "no_auth", False

    task_lists: list[dict] = []
    page = None
    try:
        while True:
            res = service.tasklists().list(maxResults=100, pageToken=page).execute()
            task_lists.extend(res.get("items", []))
            page = res.get("nextPageToken")
            if not page:
                break
    except HttpError as e:
        if getattr(e, "resp", None) is not None and e.resp.status == 403:
            return [], "no_scope", False
        logger.error("Tasks: ошибка получения списков: %s", e)
        return [], "api_error", False

    all_tasks: list[dict] = []
    complete = True
    for tl in task_lists:
        page = None
        while True:
            try:
                res = service.tasks().list(
                    tasklist=tl["id"], showCompleted=True, showHidden=True,
                    showDeleted=False, maxResults=100, pageToken=page,
                ).execute()
            except HttpError as e:
                logger.error("Tasks: ошибка списка %s: %s", tl.get("id"), e)
                complete = False
                break
            for t in res.get("items", []):
                if t.get("deleted"):
                    continue
                t["_listId"] = tl["id"]
                t["_listTitle"] = tl.get("title", "Мои задачи")
                all_tasks.append(t)
            page = res.get("nextPageToken")
            if not page:
                break
            if len(all_tasks) >= MAX_IMPORT_TASKS:
                break
        if len(all_tasks) >= MAX_IMPORT_TASKS:
            logger.warning("Tasks: превышен лимит импорта %d для %s", MAX_IMPORT_TASKS,
                           telegram_id)
            complete = False
            all_tasks = all_tasks[:MAX_IMPORT_TASKS]
            break
    return all_tasks, None, complete


def set_task_status(telegram_id: int, list_id: str, task_id: str, gt_status: str,
                    service=None) -> bool:
    """Ставит статус задачи: 'completed' или 'needsAction' (снятие отметки).
    Задачи никогда не удаляются."""
    if gt_status not in ("completed", "needsAction") or not list_id or not task_id:
        return False
    if service is None:
        service = _get_service(telegram_id)
    if not service:
        return False
    body = {"status": gt_status}
    if gt_status == "needsAction":
        body["completed"] = None
    try:
        service.tasks().patch(tasklist=list_id, task=task_id, body=body).execute()
        return True
    except HttpError as e:
        logger.error("Tasks: ошибка статуса %s/%s: %s", list_id, task_id, e)
        return False


def complete_task(telegram_id: int, task_id: str, list_id: str) -> bool:
    """Отмечает задачу выполненной. Возвращает True при успехе."""
    service = _get_service(telegram_id)
    if not service:
        return False
    try:
        service.tasks().patch(
            tasklist=list_id,
            task=task_id,
            body={"status": "completed"},
        ).execute()
        return True
    except HttpError as e:
        logger.error("Tasks: ошибка закрытия %s/%s: %s", list_id, task_id, e)
        return False
