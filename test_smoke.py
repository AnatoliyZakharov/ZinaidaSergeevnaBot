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
