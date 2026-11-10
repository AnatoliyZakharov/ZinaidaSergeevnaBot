"""
Smoke-тесты безопасности и работоспособности.
Запуск: python test_smoke.py  или  pytest test_smoke.py -v
"""
import ast
import os
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent

# Паттерны, указывающие на секреты в строковых литералах
SECRET_PATTERNS = [
    (r'mm_[A-Za-z0-9_\-]{20,}',  "MYmeet token"),
    (r'AIza[0-9A-Za-z\-_]{35}',  "Google API key"),
    (r'ya[0-9]{20,}',             "Yandex API key"),
    (r'[0-9]+-[A-Za-z0-9_]{20,}\.apps\.googleusercontent\.com', "Google OAuth client"),
]

# Файлы, которые не проверяем (сами тесты)
SKIP_FILES = {"test_smoke.py", "test_mymeet_workspace.py", "test_integration.py"}


def _all_string_literals(tree: ast.AST):
    """Возвращает все строковые литералы с номерами строк."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def test_no_hardcoded_secrets():
    """
    Ни один .py файл не должен содержать строковый литерал,
    похожий на секрет — ни как default в .get(), ни как прямое присваивание.
    """
    violations = []
    for path in sorted(REPO_ROOT.glob("*.py")):
        if path.name in SKIP_FILES:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for lineno, val in _all_string_literals(tree):
            if len(val) < 15:          # короткие строки не могут быть секретом
                continue
            for pattern, label in SECRET_PATTERNS:
                if re.search(pattern, val):
                    violations.append(
                        f"{path.name}:{lineno}: {label}: {val[:40]!r}…"
                    )

    assert not violations, (
        "Найдены захардкоженные секреты в исходном коде:\n" +
        "\n".join(violations)
    )


def _load_mymeet():
    """Загружаем mymeet_sync с минимальными env-переменными."""
    import importlib.util
    env_defaults = {
        "MYMEET_TOKEN": "",
        "MYMEET_WS_ID": "",
        "MYMEET_SPREADSHEET_ID": "",
        "ORGANIZER_EMAIL": "",
        "GOOGLE_SA_KEY": "{}",
    }
    for key, val in env_defaults.items():
        os.environ.setdefault(key, val)

    spec = importlib.util.spec_from_file_location(
        "mymeet_sync_test", REPO_ROOT / "mymeet_sync.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mymeet_sync_imports():
    """mymeet_sync.py должен импортироваться без исключений."""
    try:
        _load_mymeet()
    except Exception as exc:
        raise AssertionError(f"mymeet_sync.py не импортируется: {exc}") from exc


def test_build_user_config_rejects_cyrillic():
    """_build_user_config должен отклонять токен с кириллицей."""
    mod = _load_mymeet()
    try:
        mod._build_user_config({"mymeet_token": "КириллицаВТокене"})
        raise AssertionError("Ожидался ValueError, но исключение не было брошено")
    except ValueError as exc:
        msg = str(exc).lower()
        assert "кириллица" in msg or "недопустим" in msg, (
            f"ValueError с неожиданным сообщением: {exc}"
        )



# ── Formatting / deduplication tests ─────────────────────────────────────────

def test_status_values():
    """STATUS_VALUES содержит ровно 5 ожидаемых статусов."""
    mod = _load_mymeet()
    expected = [
        "Требует верификации",
        "В работе",
        "Выполнено",
        "Просрочено",
        "Отменено",
    ]
    assert mod.STATUS_VALUES == expected, (
        f"STATUS_VALUES = {mod.STATUS_VALUES!r}, ожидалось {expected!r}"
    )


def test_col_widths_px():
    """COL_WIDTHS_PX соответствует ТЗ: [50, 110, 420, 180, 120, 190]."""
    mod = _load_mymeet()
    expected = [50, 110, 420, 180, 120, 190]
    assert mod.COL_WIDTHS_PX == expected, (
        f"COL_WIDTHS_PX = {mod.COL_WIDTHS_PX!r}, ожидалось {expected!r}"
    )


def test_task_key_normal():
    """_task_key возвращает корректный ключ для строки с данными."""
    mod = _load_mymeet()
    row = ["1", "2024-01-15", "Подготовить отчёт", "Иванов И.И.", "2024-01-20", "В работе"]
    key = mod._task_key(row)
    assert key == "2024-01-15|подготовить отчёт|иванов и.и.", (
        f"Неожиданный ключ: {key!r}"
    )


def test_task_key_grouper_row():
    """_task_key возвращает None для строки-разделителя (пустая дата)."""
    mod = _load_mymeet()
    row = ["Плановое совещание  (2024-01-15)", "", "", "", "", ""]
    key = mod._task_key(row)
    assert key is None, f"Ожидался None для строки-разделителя, получено: {key!r}"


def test_task_key_short_row():
    """_task_key возвращает None для строки с менее чем 4 колонками."""
    mod = _load_mymeet()
    assert mod._task_key([]) is None
    assert mod._task_key(["1", "2024-01-15"]) is None


def test_task_key_empty_task():
    """_task_key возвращает None, если задача или дата пустые."""
    mod = _load_mymeet()
    row_no_task = ["1", "2024-01-15", "", "Иванов", "2024-01-20", "В работе"]
    assert mod._task_key(row_no_task) is None
    row_no_date = ["1", "", "Задача", "Иванов", "2024-01-20", "В работе"]
    assert mod._task_key(row_no_date) is None


def test_existing_keys_dedup():
    """_existing_keys правильно извлекает множество ключей из листа."""
    mod = _load_mymeet()
    sheet_data = [
        ["#", "Дата", "Поручение", "Исполнитель", "Срок", "Статус"],  # header — skip
        ["1", "2024-01-15", "Подготовить отчёт", "Иванов И.И.", "2024-01-20", "В работе"],
        ["2", "2024-01-15", "Согласовать план", "Петров П.П.", "2024-01-18", "Выполнено"],
        ["Совещание  (2024-01-16)", "", "", "", "", ""],               # grouper — skip
        ["3", "2024-01-16", "Провести встречу", "Сидоров С.С.", "2024-01-17", "В работе"],
    ]
    keys = mod._existing_keys(sheet_data)
    assert "2024-01-15|подготовить отчёт|иванов и.и." in keys
    assert "2024-01-15|согласовать план|петров п.п." in keys
    assert "2024-01-16|провести встречу|сидоров с.с." in keys
    assert len(keys) == 3, f"Ожидалось 3 ключа, получено {len(keys)}: {keys}"


def test_no_duplicate_on_resync():
    """При повторной синхронизации тех же задач дубликаты не добавляются.
    
    Проверяем логику: если ключ уже есть в ex_keys, строка пропускается.
    """
    mod = _load_mymeet()

    meetings = [
        {
            "date": "2024-01-15",
            "tasks": [
                {"name": "Подготовить отчёт", "assignee": "Иванов И.И.",
                 "deadline": "2024-01-20", "done": False},
                {"name": "Согласовать план", "assignee": "Петров П.П.",
                 "deadline": "2024-01-18", "done": True},
            ],
        }
    ]

    # Симулируем уже существующий лист (после первой синхронизации)
    existing = [
        ["#", "Дата", "Поручение", "Исполнитель", "Срок", "Статус"],
        ["1", "2024-01-15", "Подготовить отчёт", "Иванов И.И.", "2024-01-20", "В работе"],
        ["2", "2024-01-15", "Согласовать план", "Петров П.П.", "2024-01-18", "Выполнено"],
    ]

    ex_keys = mod._existing_keys(existing)

    # Повторяем логику фильтрации из sync_to_sheets (recurring path)
    new_rows = []
    for m in meetings:
        for task in m["tasks"]:
            key = mod._task_key([
                None, m["date"], task["name"], task["assignee"],
                task["deadline"], None,
            ])
            if key and key in ex_keys:
                continue
            new_rows.append(task["name"])

    assert new_rows == [], (
        f"Ожидалось 0 новых строк при повторной синхронизации, "
        f"получено {len(new_rows)}: {new_rows}"
    )


def test_new_task_added_on_resync():
    """Новая задача, которой нет на листе, добавляется при повторной синхронизации."""
    mod = _load_mymeet()

    existing = [
        ["#", "Дата", "Поручение", "Исполнитель", "Срок", "Статус"],
        ["1", "2024-01-15", "Подготовить отчёт", "Иванов И.И.", "2024-01-20", "В работе"],
    ]
    ex_keys = mod._existing_keys(existing)

    meetings = [
        {
            "date": "2024-01-15",
            "tasks": [
                # уже есть
                {"name": "Подготовить отчёт", "assignee": "Иванов И.И.",
                 "deadline": "2024-01-20", "done": False},
                # новая
                {"name": "Провести встречу", "assignee": "Петров П.П.",
                 "deadline": "2024-01-17", "done": False},
            ],
        }
    ]

    new_rows = []
    for m in meetings:
        for task in m["tasks"]:
            key = mod._task_key([
                None, m["date"], task["name"], task["assignee"],
                task["deadline"], None,
            ])
            if key and key in ex_keys:
                continue
            new_rows.append(task["name"])

    assert new_rows == ["Провести встречу"], (
        f"Ожидалась 1 новая строка, получено {len(new_rows)}: {new_rows}"
    )



# ── QA-round-2 tests ──────────────────────────────────────────────────────────

def test_dropdown_strict():
    """Выпадающий список статусов должен быть строгим (strict: True)."""
    import ast, pathlib
    sync_src = (pathlib.Path(__file__).parent / "mymeet_sync.py").read_text(encoding="utf-8")
    tree = ast.parse(sync_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "strict"
                        and isinstance(v, ast.Constant)):
                    assert v.value is True, (
                        f"Ожидался strict=True, найдено strict={v.value!r} "
                        f"(строка {node.lineno})"
                    )


def test_run_key_exists():
    """_run_key должна существовать и включать meeting_id в ключ."""
    mod = _load_mymeet()
    assert hasattr(mod, "_run_key"), "_run_key не найдена в mymeet_sync"
    k1 = mod._run_key("mtg-1", "2024-01-15", "Задача", "Иванов")
    k2 = mod._run_key("mtg-2", "2024-01-15", "Задача", "Иванов")
    assert k1 != k2, (
        "Одинаковое поручение из разных встреч должно давать разные _run_key"
    )


def test_intra_run_dedup():
    """Одно и то же поручение из одной встречи не должно дублироваться внутри запуска."""
    mod = _load_mymeet()
    meetings = [
        {
            "id": "mtg-1",
            "date": "2024-01-15",
            "tasks": [
                {"name": "Задача", "assignee": "Иванов", "deadline": "", "done": False},
                {"name": "Задача", "assignee": "Иванов", "deadline": "", "done": False},  # дубль
            ],
        }
    ]
    seen: set = set()
    rows = []
    for m in meetings:
        for t in m["tasks"]:
            rkey = mod._run_key(m["id"], m["date"], t["name"], t["assignee"])
            if rkey in seen:
                continue
            seen.add(rkey)
            rows.append(t["name"])
    assert len(rows) == 1, f"Ожидался 1 ряд, получено {len(rows)}: {rows}"


def test_different_meetings_not_deduped():
    """Одинаковое поручение из РАЗНЫХ встреч должно попасть дважды."""
    mod = _load_mymeet()
    meetings = [
        {"id": "mtg-1", "date": "2024-01-15", "tasks": [
            {"name": "Задача", "assignee": "Иванов", "deadline": "", "done": False},
        ]},
        {"id": "mtg-2", "date": "2024-01-15", "tasks": [
            {"name": "Задача", "assignee": "Иванов", "deadline": "", "done": False},
        ]},
    ]
    seen: set = set()
    rows = []
    for m in meetings:
        for t in m["tasks"]:
            rkey = mod._run_key(m["id"], m["date"], t["name"], t["assignee"])
            if rkey in seen:
                continue
            seen.add(rkey)
            rows.append(t["name"])
    assert len(rows) == 2, (
        f"Ожидалось 2 ряда (разные встречи), получено {len(rows)}: {rows}"
    )


def test_manual_status_preserved_on_full_overwrite():
    """При полной перезаписи вручную изменённый статус сохраняется (7-col лист с mtg_id)."""
    mod = _load_mymeet()

    # Существующий лист с колонкой G (mtg_id) — пользователь поменял статус на 'В работе'
    existing_full = [
        ["#", "Дата", "Поручение", "Исполнитель", "Срок", "Статус", "mtg_id"],
        ["1", "2024-01-15", "Подготовить отчёт", "Иванов", "2024-01-20", "В работе", "mtg-1"],
    ]
    manual_statuses: dict = {}
    for row in existing_full[1:]:
        k = mod._task_key(row)
        if k and len(row) >= 6 and str(row[5]).strip():
            manual_statuses[k] = str(row[5]).strip()

    meetings = [{"id": "mtg-1", "date": "2024-01-15", "tasks": [
        {"name": "Подготовить отчёт", "assignee": "Иванов",
         "deadline": "2024-01-20", "done": False},
    ]}]

    seen: set = set()
    rows = []
    for m in meetings:
        for t in m["tasks"]:
            rkey = mod._run_key(m["id"], m["date"], t["name"], t["assignee"])
            if rkey in seen:
                continue
            seen.add(rkey)
            # tkey must include meeting_id (col G) so lookup is unambiguous
            tkey = mod._task_key([None, m["date"], t["name"], t["assignee"], t["deadline"], None, m["id"]])
            status = manual_statuses.get(tkey) if tkey else None
            if not status:
                status = "Выполнено" if t["done"] else "Требует верификации"
            rows.append(status)

    assert rows == ["В работе"], (
        f"Ожидался сохранённый статус 'В работе', получено: {rows}"
    )


def test_cross_run_dedup_different_meetings_not_merged():
    """Одинаковая задача из двух разных встреч должна давать два разных _task_key."""
    mod = _load_mymeet()

    # Existing 7-col sheet: same task text from mtg-1 and mtg-2 on same date
    existing_full = [
        ["#", "Дата", "Задача", "Ответственный", "Срок", "Статус", "mtg_id"],
        ["1", "2024-01-15", "Написать отчёт", "Петров", "2024-01-20", "В работе", "mtg-1"],
        ["2", "2024-01-15", "Написать отчёт", "Петров", "2024-01-20", "Требует верификации", "mtg-2"],
    ]
    keys = set()
    for row in existing_full[1:]:
        k = mod._task_key(row)
        assert k is not None, f"_task_key вернул None для строки {row}"
        keys.add(k)

    assert len(keys) == 2, (
        f"Ожидались 2 разных ключа для задач из разных встреч, получено {len(keys)}: {keys}"
    )
    # Also verify the keys include meeting_id
    for k in keys:
        assert "mtg-" in k, f"Ключ не содержит meeting_id: {k}"


def test_fmt_warnings_in_result():
    """Ошибки форматирования попадают в строку результата пользователю."""
    import pathlib
    sync_src = (pathlib.Path(__file__).parent / "mymeet_sync.py").read_text(encoding="utf-8")
    # Result string must include fmt_warn_text
    assert "fmt_warn_text" in sync_src, (
        "Переменная fmt_warn_text не найдена в mymeet_sync.py"
    )
    assert "Ошибки форматирования" in sync_src, (
        "Текст 'Ошибки форматирования' не найден в mymeet_sync.py"
    )
    assert "all_fmt_warnings.extend" in sync_src, (
        "all_fmt_warnings.extend не найден — ошибки форматирования не собираются"
    )


def test_formatting_errors_not_silent():
    """Ошибки форматирования не маскируются под debug, а логируются как warning."""
    import ast, pathlib
    sync_src = (pathlib.Path(__file__).parent / "mymeet_sync.py").read_text(encoding="utf-8")
    # Убеждаемся, что debug-вызовов для ошибок форматирования не осталось
    bad_patterns = [
        'logger.debug("Full format batch failed',
        'logger.debug("Auto-resize rows failed',
        'logger.debug("Grouper batch format failed',
    ]
    for pat in bad_patterns:
        assert pat not in sync_src, (
            f"Ошибки форматирования скрыты под logger.debug: {pat!r}"
        )
    # И что warning-вызовы присутствуют
    assert 'logger.warning("Sheet formatting error' in sync_src, (
        "Не найден logger.warning для ошибок форматирования"
    )


# ── Issue #15: «Мои задачи», цвета статусов, фильтр ──────────────────────────

# Эталон запросов _apply_full_format(ws.id=12345, data_rows=3), снятый с
# origin/main ДО параметризации функции.  Дефолты должны давать то же самое.
_APPLY_FULL_FORMAT_REFERENCE = [{'requests': [{'repeatCell': {'range': {'sheetId': 12345,
                                             'startRowIndex': 0,
                                             'endRowIndex': 1,
                                             'startColumnIndex': 0,
                                             'endColumnIndex': 6},
                                   'cell': {'userEnteredFormat': {'textFormat': {'bold': True,
                                                                                 'fontSize': 10,
                                                                                 'foregroundColor': {'red': 1,
                                                                                                     'green': 1,
                                                                                                     'blue': 1}},
                                                                  'backgroundColor': {'red': 0.204,
                                                                                      'green': 0.396,
                                                                                      'blue': 0.643},
                                                                  'horizontalAlignment': 'CENTER',
                                                                  'wrapStrategy': 'WRAP'}},
                                   'fields': 'userEnteredFormat'}},
                   {'updateSheetProperties': {'properties': {'sheetId': 12345,
                                                             'gridProperties': {'frozenRowCount': 1}},
                                              'fields': 'gridProperties.frozenRowCount'}},
                   {'repeatCell': {'range': {'sheetId': 12345,
                                             'startRowIndex': 1,
                                             'startColumnIndex': 0,
                                             'endColumnIndex': 7},
                                   'cell': {'userEnteredFormat': {'wrapStrategy': 'WRAP',
                                                                  'verticalAlignment': 'TOP'}},
                                   'fields': 'userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment'}},
                   {'setDataValidation': {'range': {'sheetId': 12345,
                                                    'startRowIndex': 1,
                                                    'startColumnIndex': 5,
                                                    'endColumnIndex': 6},
                                          'rule': {'condition': {'type': 'ONE_OF_LIST',
                                                                 'values': [{'userEnteredValue': 'Требует '
                                                                                                 'верификации'},
                                                                            {'userEnteredValue': 'В '
                                                                                                 'работе'},
                                                                            {'userEnteredValue': 'Выполнено'},
                                                                            {'userEnteredValue': 'Просрочено'},
                                                                            {'userEnteredValue': 'Отменено'}]},
                                                   'showCustomUi': True,
                                                   'strict': True}}},
                   {'updateDimensionProperties': {'range': {'sheetId': 12345,
                                                            'dimension': 'COLUMNS',
                                                            'startIndex': 0,
                                                            'endIndex': 1},
                                                  'properties': {'pixelSize': 50},
                                                  'fields': 'pixelSize'}},
                   {'updateDimensionProperties': {'range': {'sheetId': 12345,
                                                            'dimension': 'COLUMNS',
                                                            'startIndex': 1,
                                                            'endIndex': 2},
                                                  'properties': {'pixelSize': 110},
                                                  'fields': 'pixelSize'}},
                   {'updateDimensionProperties': {'range': {'sheetId': 12345,
                                                            'dimension': 'COLUMNS',
                                                            'startIndex': 2,
                                                            'endIndex': 3},
                                                  'properties': {'pixelSize': 420},
                                                  'fields': 'pixelSize'}},
                   {'updateDimensionProperties': {'range': {'sheetId': 12345,
                                                            'dimension': 'COLUMNS',
                                                            'startIndex': 3,
                                                            'endIndex': 4},
                                                  'properties': {'pixelSize': 180},
                                                  'fields': 'pixelSize'}},
                   {'updateDimensionProperties': {'range': {'sheetId': 12345,
                                                            'dimension': 'COLUMNS',
                                                            'startIndex': 4,
                                                            'endIndex': 5},
                                                  'properties': {'pixelSize': 120},
                                                  'fields': 'pixelSize'}},
                   {'updateDimensionProperties': {'range': {'sheetId': 12345,
                                                            'dimension': 'COLUMNS',
                                                            'startIndex': 5,
                                                            'endIndex': 6},
                                                  'properties': {'pixelSize': 190},
                                                  'fields': 'pixelSize'}},
                   {'updateDimensionProperties': {'range': {'sheetId': 12345,
                                                            'dimension': 'COLUMNS',
                                                            'startIndex': 6,
                                                            'endIndex': 7},
                                                  'properties': {'pixelSize': 60, 'hiddenByUser': True},
                                                  'fields': 'pixelSize,hiddenByUser'}}]},
     {'requests': [{'autoResizeDimensions': {'dimensions': {'sheetId': 12345,
                                                            'dimension': 'ROWS',
                                                            'startIndex': 1,
                                                            'endIndex': 4}}}]}]


_PROTO_HDR = ["№", "Дата", "Задача", "Ответственный", "Срок", "Статус", "mtg_id"]


def test_norm_person():
    """_norm_person: регистр, пробелы, NBSP; имя/фамилия по отдельности не совпадают."""
    mod = _load_mymeet()
    n = mod._norm_person
    assert n("Иван Петров") == n("иван петров")
    assert n("  Иван   Петров ") == n("Иван Петров")
    assert n("Иван Петров") == n("Иван Петров")
    assert n("ИВАН ПЕТРОВ") == n("Иван Петров")
    assert n("Иван") != n("Иван Петров")
    assert n("Петров") != n("Иван Петров")
    assert n(None) == ""


def _sample_sheets():
    return {
        "Планёрка": [
            _PROTO_HDR,
            ["1", "10.01.2024", "Отчёт", "Иван Петров", "20.01.2024", "В работе", "m1"],
            ["2", "10.01.2024", "Чужая", "Пётр Сидоров", "", "В работе", "m1"],
            ["3", "17.01.2024", "Только имя", "Иван", "", "В работе", "m2"],
            ["4", "17.01.2024", "Готово", "иван  петров", "15.01.2024", "Выполнено", "m2"],
        ],
        "Разовые встречи": [
            _PROTO_HDR,
            ["Стратсессия  (12.01.2024)"],
            ["1", "12.01.2024", "Отчёт", "Иван Петров", "", "Отменено", "m3"],
            ["Ретро (команда)  (13.01.2024)", "", "", "", "", "", ""],
            ["2", "13.01.2024", "План", "ИВАН ПЕТРОВ", "", "Требует верификации", "m4"],
        ],
        "Заметки": [["что-то своё"], ["Иван Петров"]],
        "Мои задачи": [
            ["№", "Дата", "Совещание", "Задача", "Ответственный", "Срок", "Статус", "mtg_id"],
            ["1", "01.01.2024", "Старое", "Старая", "Иван Петров", "", "В работе", "old"],
        ],
    }


def test_build_my_tasks_rows_filter_and_meeting():
    """Фильтр по ФИО, название совещания для обоих типов листов, Выполнено/Отменено есть."""
    mod = _load_mymeet()
    rows = mod._build_my_tasks_rows(_sample_sheets(), "  иван   ПЕТРОВ ")
    tasks = {(r[2], r[3]) for r in rows}
    assert tasks == {
        ("Планёрка", "Отчёт"), ("Планёрка", "Готово"),
        ("Стратсессия", "Отчёт"), ("Ретро (команда)", "План"),
    }, tasks
    # 8 колонок, нумерация 1..N, сортировка по сроку (15.01 < 20.01 < пусто)
    assert all(len(r) == 8 for r in rows)
    assert [r[0] for r in rows] == list(range(1, len(rows) + 1))
    assert rows[0][3] == "Готово" and rows[1][3] == "Отчёт" and rows[1][2] == "Планёрка"
    statuses = {r[6] for r in rows}
    assert "Выполнено" in statuses and "Отменено" in statuses
    # Лист «Мои задачи» и непротокольные листы не являются источником
    assert all(r[3] != "Старая" for r in rows)
    # Пустое ФИО → ничего
    assert mod._build_my_tasks_rows(_sample_sheets(), "   ") == []
    # Только имя / только фамилия как настройка не совпадают с полным ФИО в D
    assert mod._build_my_tasks_rows(_sample_sheets(), "Петров") == []


def test_build_my_tasks_rows_same_task_two_meetings():
    """Одна задача из двух разных встреч → две строки."""
    mod = _load_mymeet()
    sheets = {"Планёрка": [
        _PROTO_HDR,
        ["1", "10.01.2024", "Отчёт", "Иван Петров", "", "В работе", "m1"],
        ["2", "17.01.2024", "Отчёт", "Иван Петров", "", "В работе", "m2"],
        ["3", "10.01.2024", "Отчёт", "Иван Петров", "", "В работе", "m9"],
    ]}
    rows = mod._build_my_tasks_rows(sheets, "Иван Петров")
    assert len(rows) == 3, rows


def test_build_my_tasks_rows_dedup_prefers_manual_status():
    """Один mtg_id в двух листах → одна строка, выбирается ручной статус."""
    mod = _load_mymeet()
    sheets = {
        "Планёрка": [
            _PROTO_HDR,
            ["1", "10.01.2024", "Отчёт", "Иван Петров", "", "Требует верификации", "m1"],
        ],
        "Разовые встречи": [
            _PROTO_HDR,
            ["Планёрка  (10.01.2024)"],
            ["1", "10.01.2024", "Отчёт", "Иван Петров", "", "Просрочено", "m1"],
        ],
    }
    rows = mod._build_my_tasks_rows(sheets, "Иван Петров")
    assert len(rows) == 1, rows
    assert rows[0][6] == "Просрочено", rows
    # Повтор построения даёт тот же результат (нет дублей при ресинке)
    assert mod._build_my_tasks_rows(sheets, "Иван Петров") == rows


def _our_rule(mod, sid, col, status):
    return {
        "ranges": [{"sheetId": sid, "startRowIndex": 1, "endRowIndex": 10000,
                    "startColumnIndex": col, "endColumnIndex": col + 1}],
        "booleanRule": {"condition": {"type": "TEXT_EQ",
                                      "values": [{"userEnteredValue": status}]},
                        "format": {}},
    }


def _apply_cf_requests(rules_by_sheet, reqs):
    """Симуляция применения delete/addConditionalFormatRule к списку правил."""
    for r in reqs:
        if "deleteConditionalFormatRule" in r:
            d = r["deleteConditionalFormatRule"]
            rules_by_sheet[d["sheetId"]].pop(d["index"])
        elif "addConditionalFormatRule" in r:
            a = r["addConditionalFormatRule"]
            sid = a["rule"]["ranges"][0]["sheetId"]
            rules_by_sheet.setdefault(sid, []).insert(a["index"], a["rule"])
    return rules_by_sheet


def test_status_color_requests():
    """Пусто → 5 add; наши → delete по убыванию + 5 add; чужие не трогаются; идемпотентно."""
    mod = _load_mymeet()
    # 1. Пусто
    reqs = mod._status_color_requests({"sheets": []}, {7: 5})
    assert len(reqs) == 5 and all("addConditionalFormatRule" in r for r in reqs)
    added = [r["addConditionalFormatRule"]["rule"] for r in reqs]
    assert [a["booleanRule"]["condition"]["values"][0]["userEnteredValue"] for a in added] \
        == mod.STATUS_VALUES
    assert all(a["ranges"][0]["startColumnIndex"] == 5 for a in added)
    assert added[4]["booleanRule"]["format"].get("textFormat", {}).get("strikethrough") is True

    # 2. Наши правила + чужие
    foreign_other_col = _our_rule(mod, 7, 2, "В работе")          # другая колонка
    foreign_custom = {"ranges": [{"sheetId": 7, "startColumnIndex": 5, "endColumnIndex": 6}],
                      "booleanRule": {"condition": {"type": "TEXT_CONTAINS",
                                                    "values": [{"userEnteredValue": "x"}]},
                                      "format": {}}}
    foreign_value = _our_rule(mod, 7, 5, "Своё значение")
    rules = [_our_rule(mod, 7, 5, "В работе"), foreign_other_col, foreign_custom,
             _our_rule(mod, 7, 5, "Отменено"), foreign_value]
    meta = {"sheets": [{"properties": {"sheetId": 7, "title": "Планёрка"},
                        "conditionalFormats": rules}]}
    reqs = mod._status_color_requests(meta, {7: 5})
    dels = [r["deleteConditionalFormatRule"]["index"] for r in reqs
            if "deleteConditionalFormatRule" in r]
    assert dels == [3, 0], dels
    assert sum("addConditionalFormatRule" in r for r in reqs) == 5
    # deletes идут раньше add
    first_add = next(i for i, r in enumerate(reqs) if "addConditionalFormatRule" in r)
    assert all("deleteConditionalFormatRule" in r for r in reqs[:first_add])

    # 3. Симуляция двух прогонов
    state = {7: list(rules)}
    _apply_cf_requests(state, reqs)
    after1 = list(state[7])
    assert len(after1) == 3 + 5
    for f in (foreign_other_col, foreign_custom, foreign_value):
        assert f in after1
    meta2 = {"sheets": [{"properties": {"sheetId": 7, "title": "Планёрка"},
                         "conditionalFormats": after1}]}
    _apply_cf_requests(state, mod._status_color_requests(meta2, {7: 5}))
    assert len(state[7]) == len(after1)
    assert state[7] == after1

    # 4. sheetId=0 без явного поля sheetId (как может вернуть API)
    r0 = _our_rule(mod, 0, 5, "Выполнено")
    del r0["ranges"][0]["sheetId"]
    meta0 = {"sheets": [{"properties": {"title": "Первый"}, "conditionalFormats": [r0]}]}
    reqs0 = mod._status_color_requests(meta0, {0: 5})
    assert reqs0[0] == {"deleteConditionalFormatRule": {"sheetId": 0, "index": 0}}


def test_apply_full_format_defaults_match_reference():
    """_apply_full_format с дефолтами формирует ровно те же requests, что до изменений."""
    from unittest.mock import MagicMock
    mod = _load_mymeet()
    ss = MagicMock()
    ws = MagicMock()
    ws.id = 12345
    ws.title = "T"
    mod._apply_full_format(ss, ws, data_rows=3)
    calls = [c.args[0] for c in ss.batch_update.call_args_list]
    assert calls == _APPLY_FULL_FORMAT_REFERENCE, calls


def test_apply_full_format_my_tasks_layout():
    """Раскладка «Мои задачи»: 7 видимых колонок, H скрыта, без dropdown."""
    from unittest.mock import MagicMock
    mod = _load_mymeet()
    ss = MagicMock()
    ws = MagicMock()
    ws.id = 5
    ws.title = "Мои задачи"
    mod._apply_full_format(ss, ws, ncols=7, status_col=None,
                           col_widths=mod.MY_TASKS_COL_WIDTHS_PX, hidden_col=7)
    reqs = ss.batch_update.call_args_list[0].args[0]["requests"]
    assert reqs[0]["repeatCell"]["range"]["endColumnIndex"] == 7
    assert reqs[2]["repeatCell"]["range"]["endColumnIndex"] == 8
    assert not any("setDataValidation" in r for r in reqs)
    hidden = [r for r in reqs if "updateDimensionProperties" in r
              and r["updateDimensionProperties"]["properties"].get("hiddenByUser")]
    assert len(hidden) == 1
    assert hidden[0]["updateDimensionProperties"]["range"]["startIndex"] == 7


def _mock_sync_env(mod, meta=None, values=None, meta_exc=None):
    """Подменяет сеть: MYmeet API и Google Sheets — только моки."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock
    import gspread

    start = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    meeting = {"id": "m1", "name": "Стратсессия", "startTime": start,
               "calendarEventId": "ev1"}
    protocol = {"followup_v2": {"templates": [{"entities": [{
        "name": "tasks",
        "html_text": [{"task": "Отчёт", "assignee": {"speaker": "Иван Петров"},
                       "deadline": "20.01.2024"}],
    }]}]}}
    mod._fetch_meetings = lambda **kw: [meeting]
    mod._fetch_protocol = lambda mid, token=None: protocol

    ss = MagicMock()
    ss.worksheet.side_effect = gspread.WorksheetNotFound("nope")
    created = {}

    def _add_ws(title, rows, cols):
        w = MagicMock()
        w.id = 100 + len(created)
        w.title = title
        w.row_count = rows
        w.col_count = cols
        created[title] = w
        return w
    ss.add_worksheet.side_effect = _add_ws
    if meta_exc is not None:
        ss.fetch_sheet_metadata.side_effect = meta_exc
    else:
        ss.fetch_sheet_metadata.return_value = meta or {"sheets": []}
    ss.values_batch_get.return_value = {"valueRanges": values or []}
    gc = MagicMock()
    gc.open_by_key.return_value = ss
    mod._gs_client = lambda: gc
    return ss, created


_GCAL = {"ids": {"ev1"}, "times": set()}


def test_sync_without_full_name_no_my_tasks_sheet():
    """Без ФИО лист «Мои задачи» не создаётся (оба режима), в ответе подсказка."""
    mod = _load_mymeet()
    for append in (False, True):
        ss, created = _mock_sync_env(mod)
        res = mod.sync_to_sheets(days=1, append=append,
                                 user_config={"gcal_organizer_data": _GCAL,
                                              "mymeet_spreadsheet_id": "x"})
        assert not any(t.startswith("Мои задачи") for t in created), created
        assert "/setname" in res, res
        # не читаем данные листов целиком без ФИО
        for c in ss.values_batch_get.call_args_list:
            assert all(r.endswith("!A1:G1") for r in c.args[0]), c


def test_sync_my_tasks_exception_does_not_break_sync():
    """Исключение шага «Мои задачи» не роняет sync и попадает в предупреждения."""
    mod = _load_mymeet()
    for append in (False, True):
        _mock_sync_env(mod, meta_exc=RuntimeError("boom-meta"))
        res = mod.sync_to_sheets(days=1, append=append,
                                 user_config={"gcal_organizer_data": _GCAL,
                                              "mymeet_spreadsheet_id": "x",
                                              "mymeet_full_name": "Иван Петров"})
        assert "Синхронизировано" in res, res
        assert "boom-meta" in res and "Ошибки форматирования" in res, res


def test_sync_with_full_name_writes_my_tasks():
    """С ФИО: лист создаётся, данные записаны, фильтр и цвета выставлены."""
    mod = _load_mymeet()
    meta = {"sheets": [
        {"properties": {"sheetId": 1, "title": "Разовые встречи"}},
        {"properties": {"sheetId": 2, "title": "Заметки"}},
    ]}
    values = [
        {"values": [_PROTO_HDR, ["Стратсессия  (10.01.2024)"],
                    ["1", "10.01.2024", "Отчёт", "Иван Петров", "20.01.2024",
                     "Выполнено", "m1"]]},
        {"values": [["что-то"]]},
    ]
    ss, created = _mock_sync_env(mod, meta=meta, values=values)
    res = mod.sync_to_sheets(days=1, append=False,
                             user_config={"gcal_organizer_data": _GCAL,
                                          "mymeet_spreadsheet_id": "x",
                                          "mymeet_full_name": "иван петров"})
    assert "Мои задачи — иван петров" in created, created
    ws = created["Мои задачи — иван петров"]
    upd = ws.update.call_args
    data = upd.kwargs["values"]
    assert data[0] == ["№", "Дата", "Совещание", "Задача", "Ответственный",
                       "Срок", "Статус", "mtg_id"]
    assert data[1] == [1, "10.01.2024", "Стратсессия", "Отчёт", "Иван Петров",
                       "20.01.2024", "Выполнено", "m1"], data
    assert upd.kwargs["range_name"] == "A1:H2"
    ranges = ss.values_batch_get.call_args.args[0]
    assert ranges == ["'Разовые встречи'!A:G", "'Заметки'!A:G"], ranges
    all_reqs = [r for c in ss.batch_update.call_args_list for r in c.args[0]["requests"]]
    filt = [r for r in all_reqs if "setBasicFilter" in r]
    assert len(filt) == 1
    spec = filt[0]["setBasicFilter"]["filter"]
    assert spec["range"] == {"sheetId": ws.id}
    assert spec["filterSpecs"] == [{"columnIndex": 6, "filterCriteria":
                                    {"hiddenValues": ["Выполнено", "Отменено"]}}]
    prot = [r for r in all_reqs if "addProtectedRange" in r]
    assert len(prot) == 1 and prot[0]["addProtectedRange"]["protectedRange"]["warningOnly"]
    colors = [r["addConditionalFormatRule"]["rule"]["ranges"][0] for r in all_reqs
              if "addConditionalFormatRule" in r]
    targets = {(g["sheetId"], g["startColumnIndex"]) for g in colors}
    assert targets == {(1, 5), (ws.id, 6)}, targets   # «Заметки» не трогаем
    assert len(colors) == 10
    assert "«Мои задачи — иван петров»: 1 задач" in res, res


def test_sync_my_tasks_name_collision_skipped():
    """Если «Мои задачи — ФИО» — протокол встречи с таким названием, шаг пропускается."""
    mod = _load_mymeet()
    meta = {"sheets": [{"properties": {"sheetId": 3, "title": "Мои задачи — Иван Петров"}}]}
    values = [{"values": [_PROTO_HDR,
                          ["1", "10.01.2024", "X", "Иван Петров", "", "В работе", "m1"]]}]
    ss, created = _mock_sync_env(mod, meta=meta, values=values)
    ss.worksheet.side_effect = None
    proto_ws = ss.worksheet.return_value
    proto_ws.get_all_values.return_value = []
    res = mod.sync_to_sheets(days=1, append=False,
                             user_config={"gcal_organizer_data": _GCAL,
                                          "mymeet_spreadsheet_id": "x",
                                          "mymeet_full_name": "Иван Петров"})
    assert "занят протоколом" in res, res
    assert not any(c.args and c.args[0] == "Мои задачи — Иван Петров"
                   for c in ss.worksheet.call_args_list), ss.worksheet.call_args_list
    assert not any(c.kwargs.get("title", "").startswith("Мои задачи")
                   for c in ss.add_worksheet.call_args_list)


def _load_db(tmpdir):
    import importlib.util
    spec = importlib.util.spec_from_file_location("db_test", REPO_ROOT / "db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DB_PATH = pathlib.Path(tmpdir) / "test_bot.db"
    return mod


def test_db_full_name_migration_and_reset():
    """init_db дважды; set_full_name; reset_mymeet_config не стирает ФИО (временная БД)."""
    import sqlite3
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        db = _load_db(tmp)
        assert str(db.DB_PATH).startswith(tmp)
        db.init_db()
        db.init_db()
        conn = sqlite3.connect(db.DB_PATH)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
        conn.close()
        assert "mymeet_full_name" in cols, cols
        db.upsert_user(42, "u")
        assert db.get_user(42)["mymeet_full_name"] is None
        db.set_full_name(42, "  Иван   Петров ")
        assert db.get_user(42)["mymeet_full_name"] == "Иван Петров"
        db.reset_mymeet_config(42)
        assert db.get_user(42)["mymeet_full_name"] == "Иван Петров"


def _load_main_func(name):
    """Извлекает чистую функцию из main.py без импорта всего бота."""
    import warnings
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)   # старые escape-строки в main.py
        tree = ast.parse(src)
    nodes = [n for n in tree.body
             if (isinstance(n, ast.FunctionDef) and n.name == name)
             or (isinstance(n, ast.Assign) and any(
                 isinstance(t, ast.Name) and t.id == "_FULL_NAME_MAX_LEN" for t in n.targets))]
    ns: dict = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), ns)
    return ns[name]


def test_validate_full_name():
    """/setname: ≥2 слов, ≤100 символов, только буквы/дефис/пробел, пробелы схлопываются."""
    v = _load_main_func("_validate_full_name")
    assert v("  Иван   Петров ") == ("Иван Петров", None)
    assert v("Анна-Мария Салтыкова-Щедрина")[0] == "Анна-Мария Салтыкова-Щедрина"
    assert v("Иван")[0] is None
    assert v("")[0] is None
    assert v("Иван Петров2")[0] is None
    assert v("Иван @Петров")[0] is None
    assert v("Иван -")[0] is None
    assert v("Иван " + "П" * 100)[0] is None


# ── Review fixes (#15): open-ended colour ranges, safe write, per-user sheets, dedup ──

def test_status_color_rules_open_ended_and_legacy_removed():
    """Нет endRowIndex в add-правилах; startRowIndex=1 и колонка статуса;
    старое правило с endRowIndex=10000 распознаётся как наше и удаляется."""
    mod = _load_mymeet()
    assert not hasattr(mod, "STATUS_COLOR_RULES_END_ROW")
    legacy = _our_rule(mod, 7, 5, "В работе")
    assert legacy["ranges"][0]["endRowIndex"] == 10000
    assert mod._is_our_status_rule(legacy, 7, 5)
    meta = {"sheets": [{"properties": {"sheetId": 7, "title": "Планёрка"},
                        "conditionalFormats": [legacy]}]}
    reqs = mod._status_color_requests(meta, {7: 5, 9: 6})
    assert reqs[0] == {"deleteConditionalFormatRule": {"sheetId": 7, "index": 0}}
    adds = [r["addConditionalFormatRule"]["rule"] for r in reqs
            if "addConditionalFormatRule" in r]
    assert len(adds) == 10
    for a in adds:
        g = a["ranges"][0]
        assert "endRowIndex" not in g, g
        assert g["startRowIndex"] == 1
        assert g["endColumnIndex"] == g["startColumnIndex"] + 1
    assert {(a["ranges"][0]["sheetId"], a["ranges"][0]["startColumnIndex"]) for a in adds} \
        == {(7, 5), (9, 6)}
    # Новые правила тоже распознаются как наши (повторный прогон их удалит)
    assert all(mod._is_our_status_rule(a, a["ranges"][0]["sheetId"],
                                       a["ranges"][0]["startColumnIndex"]) for a in adds)
    state = {7: [legacy]}
    _apply_cf_requests(state, reqs)
    assert legacy not in state[7] and len(state[7]) == 5


def _fake_ws(title, rows=1000, cols=10, sid=0):
    from unittest.mock import MagicMock
    w = MagicMock()
    w.id = sid
    w.title = title
    w.row_count = rows
    w.col_count = cols
    return w


def test_write_my_tasks_update_failure_keeps_old_data():
    """(a) Ошибка ws.update: clear/batch_clear не вызываются, sync не падает → warning."""
    from unittest.mock import MagicMock
    mod = _load_mymeet()
    title = "Мои задачи — Иван Петров"
    ss = MagicMock()
    ws = _fake_ws(title, sid=11)
    ws.update.side_effect = RuntimeError("quota-boom")
    ss.worksheet.return_value = ws
    ss.fetch_sheet_metadata.return_value = {"sheets": [
        {"properties": {"sheetId": 1, "title": "Планёрка"}},
        {"properties": {"sheetId": 11, "title": title}},
    ]}
    ss.values_batch_get.return_value = {"valueRanges": [
        {"values": [_PROTO_HDR, ["1", "10.01.2024", "Отчёт", "Иван Петров", "", "В работе", "m1"]]},
        {"values": [mod.MY_TASKS_HEADER_ROW[:7], ["1", "01.01.2024", "Совещ", "Стар", "Иван Петров"]]},
    ]}
    warnings, count, my_title = mod._sync_my_tasks_and_colors(ss, "Иван Петров")
    assert my_title == title
    assert count is None
    assert ws.update.called
    assert not ws.clear.called
    assert not ws.batch_clear.called
    assert any("quota-boom" in w for w in warnings), warnings


def test_write_my_tasks_update_before_tail_clear():
    """(b) update раньше очистки хвоста; хвост начинается со строки n+1.
    (c) повторный прогон с меньшим числом строк чистит хвост, без дублей."""
    from unittest.mock import MagicMock
    mod = _load_mymeet()
    title = "Мои задачи — Иван Петров"
    ss = MagicMock()
    ws = _fake_ws(title, rows=1000, sid=11)
    order = []
    ws.update.side_effect = lambda **kw: order.append(("update", kw["range_name"]))
    ws.batch_clear.side_effect = lambda ranges: order.append(("batch_clear", list(ranges)))
    ss.worksheet.return_value = ws
    rows3 = [[None, "10.01.2024", "П", f"Задача {i}", "Иван Петров", "", "В работе", f"m{i}"]
             for i in range(3)]
    w = []
    mod._write_my_tasks_sheet(ss, title, True, [list(r) for r in rows3], w)
    assert order[0] == ("update", "A1:H4"), order
    assert order[1] == ("batch_clear", ["A5:H1000"]), order
    assert not ws.clear.called
    # (c) повтор с 1 строкой
    order.clear()
    mod._write_my_tasks_sheet(ss, title, True, [list(rows3[0])], w)
    assert order == [("update", "A1:H2"), ("batch_clear", ["A3:H1000"])], order
    data = ws.update.call_args.kwargs["values"]
    assert len(data) == 2 and data[1][3] == "Задача 0"
    # Ошибка очистки хвоста — только предупреждение, данные уже записаны
    ws.batch_clear.side_effect = RuntimeError("clear-boom")
    w2 = []
    mod._write_my_tasks_sheet(ss, title, True, [list(rows3[0])], w2)
    assert any("clear-boom" in x for x in w2), w2
    # Лист, где строк ровно n: хвост не чистится (нет выхода за пределы сетки)
    ws_small = _fake_ws(title, rows=2, sid=12)
    ss.worksheet.return_value = ws_small
    mod._write_my_tasks_sheet(ss, title, True, [list(rows3[0])], [])
    assert ws_small.update.called and not ws_small.batch_clear.called


class _FakeBook:
    """Stateful MagicMock-based spreadsheet: metadata/values reflect writes."""

    def __init__(self, sheets):
        from unittest.mock import MagicMock
        self.data = {}           # title -> rows
        self.ws = {}             # title -> MagicMock worksheet
        self.ss = MagicMock()
        for i, (t, rows) in enumerate(sheets.items(), 1):
            self._new(t, i, rows)
        self.ss.fetch_sheet_metadata.side_effect = lambda params=None: {"sheets": [
            {"properties": {"sheetId": w.id, "title": t}} for t, w in self.ws.items()]}
        self.ss.values_batch_get.side_effect = self._batch_get
        self.ss.worksheet.side_effect = lambda t: self.ws[t]
        self.ss.add_worksheet.side_effect = (
            lambda title, rows, cols: self._new(title, 100 + len(self.ws), []))

    def _new(self, title, sid, rows):
        w = _fake_ws(title, sid=sid)
        self.data[title] = [list(r) for r in rows]

        def _upd(values, range_name):
            self.data[title] = [list(r) for r in values]
        w.update.side_effect = _upd
        self.ws[title] = w
        return w

    def _batch_get(self, ranges):
        out = []
        for r in ranges:
            t = r.rsplit("!", 1)[0][1:-1].replace("''", "'")
            out.append({"values": [row[:7] for row in self.data[t]]})
        return {"valueRanges": out}


def test_my_tasks_two_users_shared_spreadsheet():
    """Два пользователя на одной таблице: у каждого свой лист, чужой не трогается."""
    mod = _load_mymeet()
    book = _FakeBook({
        "Планёрка": [
            _PROTO_HDR,
            ["1", "10.01.2024", "Отчёт", "Иван Петров", "", "В работе", "m1"],
            ["2", "10.01.2024", "Бюджет", "Анна Смирнова", "", "В работе", "m1"],
        ],
        "Разовые встречи": [
            _PROTO_HDR,
            ["Стратсессия  (12.01.2024)"],
            ["1", "12.01.2024", "План", "анна смирнова", "", "Просрочено", "m2"],
        ],
    })
    a_title, b_title = "Мои задачи — Иван Петров", "Мои задачи — Анна Смирнова"
    w, cnt, t = mod._sync_my_tasks_and_colors(book.ss, "Иван Петров")
    assert t == a_title and cnt == 1, (t, cnt, w)
    assert set(book.ws) == {"Планёрка", "Разовые встречи", a_title}
    ws_a = book.ws[a_title]
    assert [r[3] for r in book.data[a_title][1:]] == ["Отчёт"]
    upd_a, clr_a, bclr_a = (ws_a.update.call_count, ws_a.clear.call_count,
                            ws_a.batch_clear.call_count)

    w, cnt, t = mod._sync_my_tasks_and_colors(book.ss, "Анна Смирнова")
    assert t == b_title and cnt == 2, (t, cnt, w)
    assert set(book.ws) == {"Планёрка", "Разовые встречи", a_title, b_title}
    # лист A не перезаписан и не очищен
    assert ws_a.update.call_count == upd_a
    assert ws_a.clear.call_count == clr_a and ws_a.batch_clear.call_count == bclr_a
    assert not any(c.args and c.args[0] == a_title for c in book.ss.worksheet.call_args_list)
    assert [r[3] for r in book.data[a_title][1:]] == ["Отчёт"]
    assert sorted(r[3] for r in book.data[b_title][1:]) == ["Бюджет", "План"]
    assert all(mod._norm_person(r[4]) == mod._norm_person("Анна Смирнова")
               for r in book.data[b_title][1:])
    # цвета ставятся и на чужой лист «Мои задачи — …» (колонка G), и на протоколы
    reqs = book.ss.batch_update.call_args_list[-1].args[0]["requests"]
    targets = {(r["addConditionalFormatRule"]["rule"]["ranges"][0]["sheetId"],
                r["addConditionalFormatRule"]["rule"]["ranges"][0]["startColumnIndex"])
               for r in reqs if "addConditionalFormatRule" in r}
    assert targets == {(book.ws["Планёрка"].id, 5), (book.ws["Разовые встречи"].id, 5),
                       (ws_a.id, 6), (book.ws[b_title].id, 6)}, targets
    # Повторная синхронизация A пишет только лист A
    ws_b = book.ws[b_title]
    upd_b, clr_b, bclr_b = ws_b.update.call_count, ws_b.clear.call_count, \
        ws_b.batch_clear.call_count
    mod._sync_my_tasks_and_colors(book.ss, "Иван Петров")
    assert ws_b.update.call_count == upd_b
    assert ws_b.clear.call_count == clr_b and ws_b.batch_clear.call_count == bclr_b
    assert [r[3] for r in book.data[a_title][1:]] == ["Отчёт"]


def test_my_tasks_same_name_different_case_reuses_sheet():
    """Одно ФИО в разном регистре/пробелах → один и тот же лист."""
    mod = _load_mymeet()
    book = _FakeBook({"Планёрка": [
        _PROTO_HDR, ["1", "10.01.2024", "Отчёт", "Иван Петров", "", "В работе", "m1"]]})
    _, _, t1 = mod._sync_my_tasks_and_colors(book.ss, "Иван Петров")
    _, cnt, t2 = mod._sync_my_tasks_and_colors(book.ss, "  иван   ПЕТРОВ ")
    assert t1 == t2 == "Мои задачи — Иван Петров", (t1, t2)
    assert cnt == 1
    assert sum(1 for t in book.ws if t.startswith("Мои задачи")) == 1, list(book.ws)
    assert book.ss.add_worksheet.call_count == 1
    # Нормализация имени: пробелы схлопнуты, регистр как ввёл пользователь
    assert mod._my_tasks_sheet_title("  иван   ПЕТРОВ ") == "Мои задачи — иван ПЕТРОВ"
    # Выбор среди существующих детерминирован
    r = mod._resolve_my_tasks_title(["Мои задачи — иван петров", "Мои задачи — ИВАН ПЕТРОВ"],
                                    "Иван Петров")
    assert r == "Мои задачи — ИВАН ПЕТРОВ"
    assert mod._resolve_my_tasks_title(["Мои задачи — Иван Петров", "Мои задачи — иван петров"],
                                       "иван петров") == "Мои задачи — иван петров"
    # Другое ФИО не переиспользует чужой лист
    assert mod._resolve_my_tasks_title(["Мои задачи — Иван Петров"], "Иван Петрович") \
        == "Мои задачи — Иван Петрович"


def test_my_tasks_long_name_title_limit():
    """Длинное ФИО → имя листа ≤100 символов, детерминированно."""
    mod = _load_mymeet()
    name = "Анна-Мария " + "Салтыкова-Щедрина " * 6
    t = mod._my_tasks_sheet_title(name)
    assert len(t) <= 100, len(t)
    assert t.startswith("Мои задачи — Анна-Мария")
    assert t == mod._my_tasks_sheet_title(name) == mod._my_tasks_sheet_title("  " + name)
    assert t == t.strip()


def test_build_my_tasks_rows_one_time_dedup_without_mtg_id():
    """Без mtg_id: две разные разовые встречи одного дня → 2 строки; реальный дубль → 1."""
    mod = _load_mymeet()
    sheets = {"Разовые встречи": [
        _PROTO_HDR,
        ["Стратсессия  (12.01.2024)"],
        ["1", "12.01.2024", "Сделать X", "Иван Петров", "", "В работе", ""],
        ["Ретро  (12.01.2024)", "", "", "", "", "", ""],
        ["1", "12.01.2024", "Сделать X", "Иван Петров", "", "В работе", ""],
    ]}
    rows = mod._build_my_tasks_rows(sheets, "Иван Петров")
    assert len(rows) == 2, rows
    assert sorted(r[2] for r in rows) == ["Ретро", "Стратсессия"], rows
    # Дубль внутри одного группировщика
    dup = {"Разовые встречи": [
        _PROTO_HDR,
        ["Стратсессия  (12.01.2024)"],
        ["1", "12.01.2024", "Сделать X", "Иван Петров", "", "Требует верификации", ""],
        ["2", "12.01.2024", "Сделать X", "Иван Петров", "", "В работе", ""],
    ]}
    rows = mod._build_my_tasks_rows(dup, "Иван Петров")
    assert len(rows) == 1 and rows[0][2] == "Стратсессия" and rows[0][6] == "В работе", rows
    # Лист повторяющейся встречи без mtg_id: одна дата = одна встреча → 1 строка
    rec = {"Планёрка": [
        _PROTO_HDR,
        ["1", "10.01.2024", "Сделать X", "Иван Петров", "", "В работе", ""],
        ["2", "10.01.2024", "Сделать X", "Иван Петров", "", "В работе", ""],
        ["3", "17.01.2024", "Сделать X", "Иван Петров", "", "В работе", ""],
    ]}
    assert len(mod._build_my_tasks_rows(rec, "Иван Петров")) == 2


# ── Blocker fix (#15): «Мои задачи» из всех встреч, не только организаторских ──

class _SyncBook(_FakeBook):
    """_FakeBook для полного sync_to_sheets: get_all_values/clear/update по
    диапазону, batch_clear хвоста, чтение A:G / A:H / A1:G1, WorksheetNotFound."""

    def __init__(self, sheets):
        super().__init__(sheets)
        import gspread

        def _get(t):
            if t not in self.ws:
                raise gspread.WorksheetNotFound(t)
            return self.ws[t]
        self.ss.worksheet.side_effect = _get

    def _new(self, title, sid, rows):
        import re
        w = _fake_ws(title, sid=sid)
        self.data[title] = [list(r) for r in rows]

        def _upd(*args, **kw):
            if kw:
                values, rng = kw["values"], kw["range_name"]
            else:
                rng, values = args
            start = int(re.match(r"[A-Z]+(\d+)", rng).group(1)) - 1
            d = self.data[title]
            while len(d) < start:
                d.append([])
            for i, row in enumerate(values):
                if start + i < len(d):
                    d[start + i] = list(row)
                else:
                    d.append(list(row))

        def _bclear(ranges):
            start = int(re.match(r"[A-Z]+(\d+)", ranges[0]).group(1)) - 1
            del self.data[title][start:]

        def _clear():
            self.data[title] = []
        w.update.side_effect = _upd
        w.batch_clear.side_effect = _bclear
        w.clear.side_effect = _clear
        w.get_all_values.side_effect = lambda: [list(r) for r in self.data[title]]
        self.ws[title] = w
        return w

    def _batch_get(self, ranges):
        out = []
        for r in ranges:
            t, rng = r.rsplit("!", 1)
            t = t[1:-1].replace("''", "'")
            rows = self.data[t]
            if rng == "A1:G1":
                rows = rows[:1]
            width = 8 if rng.endswith("H") else 7
            out.append({"values": [list(row[:width]) for row in rows]})
        return {"valueRanges": out}


_ME = "Иван Петров"
_MY_SHEET = "Мои задачи — Иван Петров"


def _proto(*tasks):
    return {"followup_v2": {"templates": [{"entities": [{
        "name": "tasks",
        "html_text": [{"task": t, "assignee": {"speaker": a}, "deadline": d, "done": done}
                      for t, a, d, done in tasks],
    }]}]}}


def _two_meetings():
    from datetime import datetime, timedelta, timezone
    start = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    m_org = {"id": "m_org", "name": "Стратсессия", "startTime": start,
             "calendarEventId": "ev1"}
    m_other = {"id": "m_other", "name": "Планёрка отдела", "startTime": start,
               "calendarEventId": "ev_foreign"}
    return m_org, m_other


def _run_full_sync(mod, book, meetings, protocols, full_name=_ME, append=False):
    """sync_to_sheets на моках; protocols: {mid: dict | Exception}."""
    from unittest.mock import MagicMock

    def _fp(mid, token=None):
        p = protocols.get(mid)
        if isinstance(p, Exception):
            raise p
        return p
    mod._fetch_meetings = lambda **kw: list(meetings)
    mod._fetch_protocol = MagicMock(side_effect=_fp)
    mod._throttle = lambda: None      # моки: лимитер записи Sheets не нужен (без sleep)
    gc = MagicMock()
    gc.open_by_key.return_value = book.ss
    mod._gs_client = lambda: gc
    cfg = {"gcal_organizer_data": {"ids": {"ev1"}, "times": set()},
           "mymeet_spreadsheet_id": "x"}
    if full_name:
        cfg["mymeet_full_name"] = full_name
    return mod.sync_to_sheets(days=1, append=append, user_config=cfg)


def _my_rows(book):
    return [r for r in book.data.get(_MY_SHEET, [])[1:] if any(str(c) for c in r)]


def _default_protocols():
    return {
        "m_org": _proto(("Отчёт", _ME, "20.01.2024", False)),
        "m_other": _proto(("Смета", "иван  ПЕТРОВ", "25.01.2024", False),
                          ("Чужое", "Анна Смирнова", "", False)),
    }


def test_my_tasks_include_non_organizer_meeting():
    """П.6: исполнитель на чужой встрече — задача в «Моих задачах», протокол
    этой встречи не создаётся, add_worksheet/update для неё не вызываются,
    протокол организаторской встречи пишется так же, как без чужой встречи."""
    mod = _load_mymeet()
    m_org, m_other = _two_meetings()
    book = _SyncBook({})
    res = _run_full_sync(mod, book, [m_org, m_other], _default_protocols())

    rows = _my_rows(book)
    by_task = {r[3]: r for r in rows}
    assert set(by_task) == {"Отчёт", "Смета"}, rows
    assert by_task["Отчёт"][2] == "Стратсессия" and by_task["Отчёт"][7] == "m_org"
    sm = by_task["Смета"]
    assert sm[2] == "Планёрка отдела" and sm[7] == "m_other", sm
    assert sm[5] == "25.01.2024" and sm[6] == "Требует верификации", sm
    assert "из них с чужих встреч: 1" in res, res
    assert "организатор 1" in res, res

    # Протокол чужой встречи не создан ни отдельным листом, ни в «Разовых встречах»
    added = [c.kwargs.get("title") for c in book.ss.add_worksheet.call_args_list]
    assert "Планёрка отдела" not in added, added
    assert set(book.ws) == {"Разовые встречи", _MY_SHEET}, list(book.ws)
    for title, w in book.ws.items():
        if title == _MY_SHEET:
            continue
        for c in w.update.call_args_list:
            flat = repr(c)
            assert "m_other" not in flat and "Планёрка отдела" not in flat \
                and "Смета" not in flat, (title, c)
    assert not any("m_other" in repr(r) or "Планёрка отдела" in repr(r)
                   for r in book.data["Разовые встречи"])

    # Протокол организаторской встречи — тот же, что без чужой встречи
    ref = _SyncBook({})
    _run_full_sync(mod, ref, [m_org], _default_protocols())
    assert book.data["Разовые встречи"] == ref.data["Разовые встречи"], \
        (book.data["Разовые встречи"], ref.data["Разовые встречи"])
    ot = book.data["Разовые встречи"]
    assert ot[1][0].startswith("Стратсессия") and ot[2][2] == "Отчёт" and ot[2][6] == "m_org", ot


def test_my_tasks_non_organizer_other_assignee_excluded():
    """На чужой встрече задача на другое ФИО (и частичное совпадение) не попадает."""
    mod = _load_mymeet()
    m_org, m_other = _two_meetings()
    protocols = {
        "m_org": _proto(),
        "m_other": _proto(("Чужое", "Анна Смирнова", "", False),
                          ("Частичное", "Петров", "", False),
                          ("Отчество", "Иван Петрович", "", False)),
    }
    book = _SyncBook({})
    res = _run_full_sync(mod, book, [m_org, m_other], protocols)
    assert _my_rows(book) == [], book.data.get(_MY_SHEET)
    assert "«Мои задачи — Иван Петров»: 0 задач" in res, res
    assert "чужих встреч" not in res


def test_my_tasks_no_full_name_no_foreign_protocol_fetch():
    """Без ФИО протоколы чужих встреч не запрашиваются (оба режима)."""
    mod = _load_mymeet()
    m_org, m_other = _two_meetings()
    for append in (False, True):
        book = _SyncBook({})
        _run_full_sync(mod, book, [m_org, m_other], _default_protocols(),
                       full_name="", append=append)
        called = [c.args[0] for c in mod._fetch_protocol.call_args_list]
        assert called == ["m_org"], called
        assert not any(t.startswith("Мои задачи") for t in book.ws)
    # С ФИО — запрашиваются обе
    book = _SyncBook({})
    _run_full_sync(mod, book, [m_org, m_other], _default_protocols())
    assert sorted(c.args[0] for c in mod._fetch_protocol.call_args_list) == ["m_org", "m_other"]


def test_my_tasks_foreign_history_and_statuses():
    """Накопление: строка чужой встречи вне окна (m_old) сохраняется, строка без
    mtg_id не переносится; ручной статус задачи чужой встречи переживает sync;
    done из MYmeet переводит «Требует верификации» в «Выполнено»."""
    mod = _load_mymeet()
    m_org, m_other = _two_meetings()
    hdr = list(mod.MY_TASKS_HEADER_ROW)
    book = _SyncBook({_MY_SHEET: [
        hdr,
        [1, "01.12.2023", "Старая встреча", "Архив", _ME, "", "В работе", "m_old"],
        [2, "01.12.2023", "Без id", "Потерянная", _ME, "", "В работе", ""],
    ]})
    protocols = _default_protocols()
    protocols["m_other"] = _proto(("Смета", _ME, "25.01.2024", False),
                                  ("Звонок", _ME, "", True))
    for append in (False, True):
        _run_full_sync(mod, book, [m_org, m_other], protocols, append=append)
        by_task = {r[3]: r for r in _my_rows(book)}
        assert set(by_task) == {"Отчёт", "Смета", "Звонок", "Архив"}, by_task
        assert by_task["Архив"][2:] == ["Старая встреча", "Архив", _ME, "", "В работе", "m_old"]
        assert by_task["Звонок"][6] == "Выполнено"

    # Пользователь вручную поставил статус на личном листе
    for r in book.data[_MY_SHEET]:
        if len(r) > 3 and r[3] == "Смета":
            r[6] = "В работе"
        if len(r) > 3 and r[3] == "Звонок":
            r[6] = "Требует верификации"       # done в MYmeet → снова «Выполнено»
    for append in (False, True):
        _run_full_sync(mod, book, [m_org, m_other], protocols, append=append)
        by_task = {r[3]: r for r in _my_rows(book)}
        assert by_task["Смета"][6] == "В работе", by_task["Смета"]
        assert by_task["Звонок"][6] == "Выполнено", by_task["Звонок"]
        assert "Архив" in by_task and "Потерянная" not in by_task

    # Ручной «Отменено» не перетирается даже при done
    for r in book.data[_MY_SHEET]:
        if len(r) > 3 and r[3] == "Звонок":
            r[6] = "Отменено"
    _run_full_sync(mod, book, [m_org, m_other], protocols)
    assert {r[3]: r for r in _my_rows(book)}["Звонок"][6] == "Отменено"

    # Ежедневный sync без встреч в окне: история чужих встреч не пропадает
    _run_full_sync(mod, book, [], {}, append=True)
    assert {r[3] for r in _my_rows(book)} >= {"Смета", "Звонок", "Архив"}


def test_my_tasks_foreign_fetch_error_isolated():
    """Ошибка загрузки протокола чужой встречи не роняет sync; протокол
    организатора записан; старые строки этой встречи на личном листе сохранены."""
    mod = _load_mymeet()
    m_org, m_other = _two_meetings()
    hdr = list(mod.MY_TASKS_HEADER_ROW)
    book = _SyncBook({_MY_SHEET: [
        hdr, [1, "01.01.2024", "Планёрка отдела", "Смета", _ME, "", "В работе", "m_other"]]})
    ref = _SyncBook({})
    _run_full_sync(mod, ref, [m_org], _default_protocols(), full_name="")
    for exc in (RuntimeError("mymeet-boom"), None):      # исключение и 404/None
        protocols = _default_protocols()
        protocols["m_other"] = exc
        res = _run_full_sync(mod, book, [m_org, m_other], protocols)
        assert "Синхронизировано: 1 задач" in res, res
        assert book.data["Разовые встречи"] == ref.data["Разовые встречи"]
        rows = {r[3]: r for r in _my_rows(book)}
        assert set(rows) == {"Отчёт", "Смета"}, rows
        assert rows["Смета"][6] == "В работе"


def test_my_tasks_foreign_resync_no_duplicates():
    """Повторный sync (полный и ежедневный) не создаёт дублей; протокол
    встречи организатора побеждает, если тот же mtg_id есть и среди чужих."""
    mod = _load_mymeet()
    m_org, m_other = _two_meetings()
    book = _SyncBook({})
    for append in (False, False, True, True, False):
        _run_full_sync(mod, book, [m_org, m_other], _default_protocols(), append=append)
        rows = _my_rows(book)
        keys = [(r[7], r[1], r[3], r[4]) for r in rows]
        assert len(keys) == len(set(keys)) == 2, rows
        assert [r[0] for r in rows] == [1, 2]
        assert sum(1 for r in book.data["Разовые встречи"] if len(r) > 2 and r[2] == "Отчёт") == 1
    # Дедуп «протокол побеждает» на уровне чистых функций
    proto_sheets = {"Разовые встречи": [
        _PROTO_HDR, ["Стратсессия  (12.01.2024)"],
        ["1", "12.01.2024", "Отчёт", _ME, "", "В работе", "m_org"]]}
    extra = mod._merge_other_meeting_rows(
        [], [[None, "12.01.2024", "X", "Отчёт", _ME, "", "Требует верификации", "m_org"]],
        {"m_org"}, {"m_org"})
    assert extra == []
    rows = mod._build_my_tasks_rows(
        proto_sheets, _ME,
        extra_rows=[[None, "12.01.2024", "X", "Отчёт", _ME, "", "Выполнено", "m_org"]])
    assert len(rows) == 1 and rows[0][2] == "Стратсессия" and rows[0][6] == "В работе", rows


if __name__ == "__main__":
    tests = [
        (test_no_hardcoded_secrets,              "нет захардкоженных секретов"),
        (test_mymeet_sync_imports,               "mymeet_sync импортируется"),
        (test_build_user_config_rejects_cyrillic,"кириллица в токене отклоняется"),
        (test_status_values,                     "STATUS_VALUES содержит 5 статусов"),
        (test_col_widths_px,                     "COL_WIDTHS_PX = [50,110,420,180,120,190]"),
        (test_task_key_normal,                   "_task_key возвращает корректный ключ"),
        (test_task_key_grouper_row,              "_task_key=None для строки-разделителя"),
        (test_task_key_short_row,                "_task_key=None для короткой строки"),
        (test_task_key_empty_task,               "_task_key=None при пустой задаче/дате"),
        (test_existing_keys_dedup,               "_existing_keys извлекает 3 ключа"),
        (test_no_duplicate_on_resync,            "дубликаты не добавляются при ресинке"),
        (test_new_task_added_on_resync,          "новая задача добавляется при ресинке"),
        (test_dropdown_strict,                   "dropdown strict=True"),
        (test_run_key_exists,                    "_run_key разделяет разные встречи"),
        (test_intra_run_dedup,                   "дубль внутри запуска не добавляется"),
        (test_different_meetings_not_deduped,    "одинак. задача из разных встреч — 2 ряда"),
        (test_manual_status_preserved_on_full_overwrite, "статус сохраняется при перезаписи"),
        (test_cross_run_dedup_different_meetings_not_merged, "задачи из разных встреч не сливаются"),
        (test_fmt_warnings_in_result,            "ошибки форматирования в результате"),
        (test_formatting_errors_not_silent,      "ошибки форматирования видны (warning)"),
        (test_norm_person,                        "_norm_person: регистр/пробелы/NBSP"),
        (test_build_my_tasks_rows_filter_and_meeting, "«Мои задачи»: фильтр ФИО и совещание"),
        (test_build_my_tasks_rows_same_task_two_meetings, "«Мои задачи»: 1 задача из 2 встреч"),
        (test_build_my_tasks_rows_dedup_prefers_manual_status, "«Мои задачи»: дедуп, ручной статус"),
        (test_status_color_requests,              "цвета статусов: delete/add идемпотентно"),
        (test_apply_full_format_defaults_match_reference, "_apply_full_format = эталон"),
        (test_apply_full_format_my_tasks_layout,  "_apply_full_format: раскладка «Мои задачи»"),
        (test_sync_without_full_name_no_my_tasks_sheet, "без ФИО лист не создаётся"),
        (test_sync_my_tasks_exception_does_not_break_sync, "ошибка шага не роняет sync"),
        (test_sync_with_full_name_writes_my_tasks, "с ФИО лист записан, фильтр, цвета"),
        (test_sync_my_tasks_name_collision_skipped, "коллизия имени листа — пропуск"),
        (test_db_full_name_migration_and_reset,   "db: миграция ФИО, reset не стирает"),
        (test_validate_full_name,                 "/setname: валидация ФИО"),
        (test_status_color_rules_open_ended_and_legacy_removed, "цвета: без endRowIndex, старые удаляются"),
        (test_write_my_tasks_update_failure_keeps_old_data, "ошибка update — старые данные целы"),
        (test_write_my_tasks_update_before_tail_clear, "update до очистки хвоста, без дублей"),
        (test_my_tasks_two_users_shared_spreadsheet, "два пользователя — разные листы"),
        (test_my_tasks_same_name_different_case_reuses_sheet, "ФИО в другом регистре — тот же лист"),
        (test_my_tasks_long_name_title_limit,     "длинное ФИО — имя листа ≤100"),
        (test_build_my_tasks_rows_one_time_dedup_without_mtg_id, "дедуп без mtg_id по встрече"),
        (test_my_tasks_include_non_organizer_meeting, "чужая встреча: задача есть, протокола нет"),
        (test_my_tasks_non_organizer_other_assignee_excluded, "чужая встреча: другое ФИО не попадает"),
        (test_my_tasks_no_full_name_no_foreign_protocol_fetch, "без ФИО чужие протоколы не грузятся"),
        (test_my_tasks_foreign_history_and_statuses, "чужие встречи: история и статусы"),
        (test_my_tasks_foreign_fetch_error_isolated, "ошибка протокола чужой встречи изолирована"),
        (test_my_tasks_foreign_resync_no_duplicates, "чужие встречи: повторный sync без дублей"),
    ]
    failed = []
    for fn, label in tests:
        try:
            fn()
            print(f"  ✓ {label}")
        except AssertionError as e:
            print(f"  ✗ {label}: {e}")
            failed.append(label)
    if failed:
        raise SystemExit(f"\n❌ Упали тесты: {failed}")
    print("\n✅ Все smoke-тесты прошли")
