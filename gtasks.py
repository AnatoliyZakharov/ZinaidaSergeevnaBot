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
