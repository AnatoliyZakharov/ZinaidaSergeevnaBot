"""
gtasks.py — Google Tasks API.

Позволяет читать задачи, менять их статус и адресно удалять одну задачу
(только по ручной отмене в «Мои задачи», см. my_tasks.py).  Задачи не создаются.
Использует те же Google Credentials, что и calendar (auth.get_credentials_checked).

Ошибки классифицируются auth.classify_google_error: no_auth / token_invalid /
insufficient_scope / api_disabled / transient / api_error.  Переподключение
(/connect) помогает только при no_auth / token_invalid / insufficient_scope;
api_disabled — Google Tasks API не включён в GCP-проекте бота.
В лог пишутся только категория, HTTP-статус и reason.
"""

import logging
import time
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import auth

logger = logging.getLogger(__name__)


def _get_service(telegram_id: int):
    """Возвращает (service, None) или (None, категория ошибки авторизации)."""
    creds, reason = auth.get_credentials_checked(telegram_id)
    if not creds:
        return None, reason or "no_auth"
    return build("tasks", "v1", credentials=creds), None


def _classify(where: str, telegram_id, e) -> str:
    err = auth.classify_google_error(e)
    auth.log_google_error(where, telegram_id, err)
    return err.category


def list_all_tasks(telegram_id: int) -> tuple[list[dict], str | None]:
    """
    Собирает незакрытые задачи из всех списков Google Tasks.

    Возвращает (tasks, error):
      - tasks: список задач (каждая содержит _listId, _listTitle)
      - error: категория auth.classify_google_error / no_auth или None
    """
    service, reason = _get_service(telegram_id)
    if not service:
        return [], reason

    try:
        lists_result = service.tasklists().list(maxResults=10).execute()
    except Exception as e:
        return [], _classify("tasks.tasklists.list", telegram_id, e)

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
        except Exception as e:
            _classify("tasks.tasks.list", telegram_id, e)

    # Сортируем: задачи с дедлайном вперёд, потом по порядку в списке
    all_tasks.sort(key=lambda t: (t.get("due", "z"), t.get("position", "")))
    return all_tasks, None


MAX_IMPORT_TASKS = 2000   # предохранитель: больше — ответ считается неполным


def _status_code(e) -> int:
    resp = getattr(e, "resp", None)
    try:
        return int(getattr(resp, "status", 0) or 0)
    except (TypeError, ValueError):
        return 0


def list_all_tasks_full(telegram_id: int, service=None):
    """Все задачи всех списков, включая выполненные и скрытые (импорт в «Мои задачи»).

    Постранично (nextPageToken).  Удалённые (deleted) не возвращаются.
    Возвращает (tasks, error, complete, list_ids):
      complete=False — какой-то список/страница не загрузились или превышен
      MAX_IMPORT_TASKS: вызывающий НЕ должен считать отсутствующие задачи удалёнными;
      list_ids — id всех полученных списков (задачи «чужих»/исчезнувших
      списков не трактуются как удалённые).
    """
    reason = None
    if service is None:
        service, reason = _get_service(telegram_id)
    if not service:
        return [], reason or "no_auth", False, []

    task_lists: list[dict] = []
    page = None
    try:
        while True:
            res = service.tasklists().list(maxResults=100, pageToken=page).execute()
            task_lists.extend(res.get("items", []))
            page = res.get("nextPageToken")
            if not page:
                break
    except Exception as e:
        return [], _classify("tasks_full.tasklists.list", telegram_id, e), False, []

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
            except Exception as e:
                _classify("tasks_full.tasks.list", telegram_id, e)
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
    return all_tasks, None, complete, [tl["id"] for tl in task_lists if tl.get("id")]


_RETRY_CODES = (429, 500, 502, 503, 504)


def _execute_with_retry(request_fn, attempts: int = 3, base_delay: float = 1.0):
    """Выполняет запрос с повтором на временных ошибках (429/5xx)."""
    for i in range(attempts):
        try:
            return request_fn().execute()
        except HttpError as e:
            if _status_code(e) in _RETRY_CODES and i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
                continue
            raise


def get_task_state(telegram_id: int, list_id: str, task_id: str, service=None) -> str:
    """Адресная проверка задачи: 'exists' | 'deleted' | 'error'.
    'deleted' — только при 404/410 или deleted=true; любые другие ошибки
    (сеть, 401/403, 5xx) — 'error' (не считаем удалённой)."""
    if not list_id or not task_id:
        return "error"
    if service is None:
        service, _ = _get_service(telegram_id)
    if not service:
        return "error"
    try:
        t = _execute_with_retry(lambda: service.tasks().get(tasklist=list_id, task=task_id))
    except HttpError as e:
        return "deleted" if _status_code(e) in (404, 410) else "error"
    except Exception:
        return "error"
    return "deleted" if (t or {}).get("deleted") else "exists"


def delete_task(telegram_id: int, list_id: str, task_id: str, service=None) -> str:
    """Адресное удаление одной задачи по (list_id, task_id).

    Идемпотентно: 'deleted' — удалена; 'absent' — уже отсутствовала (404/410);
    'retry' — временная ошибка после повторов; 'error' — прочие ошибки.
    Вызывается только из my_tasks.MyTasksBuilder.execute_external для записи
    с подтверждённой ручной отменой."""
    if not list_id or not task_id:
        return "error"
    if service is None:
        service, _ = _get_service(telegram_id)
    if not service:
        return "error"
    try:
        _execute_with_retry(lambda: service.tasks().delete(tasklist=list_id, task=task_id))
        logger.info("Tasks: задача удалена по ручной отмене (telegram_id=%s)", telegram_id)
        return "deleted"
    except HttpError as e:
        code = _status_code(e)
        if code in (404, 410):
            return "absent"
        logger.error("Tasks: ошибка удаления: HTTP %s", code)
        return "retry" if code in _RETRY_CODES else "error"
    except Exception as e:
        logger.error("Tasks: ошибка удаления: %s", type(e).__name__)
        return "retry"


def set_task_status(telegram_id: int, list_id: str, task_id: str, gt_status: str,
                    service=None) -> bool:
    """Ставит статус задачи: 'completed' или 'needsAction' (снятие отметки).
    Задачи никогда не удаляются."""
    if gt_status not in ("completed", "needsAction") or not list_id or not task_id:
        return False
    if service is None:
        service, _ = _get_service(telegram_id)
    if not service:
        return False
    body = {"status": gt_status}
    if gt_status == "needsAction":
        body["completed"] = None
    try:
        # An ambiguous response can follow an applied PATCH. Reconcile with a
        # fresh task snapshot before sending another status change.
        service.tasks().patch(tasklist=list_id, task=task_id, body=body).execute()
        return True
    except HttpError as e:
        logger.error("Tasks: ошибка смены статуса: HTTP %s", _status_code(e))
        return False


def complete_task(telegram_id: int, task_id: str, list_id: str) -> bool:
    """Отмечает задачу выполненной. Возвращает True при успехе."""
    service, _ = _get_service(telegram_id)
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
        _classify("tasks.complete", telegram_id, e)
        return False
