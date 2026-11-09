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
SKIP_FILES = {"test_smoke.py"}


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
