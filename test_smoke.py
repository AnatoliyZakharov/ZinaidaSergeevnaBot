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


if __name__ == "__main__":
    tests = [
        (test_no_hardcoded_secrets,          "нет захардкоженных секретов"),
        (test_mymeet_sync_imports,            "mymeet_sync импортируется"),
        (test_build_user_config_rejects_cyrillic, "кириллица в токене отклоняется"),
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
