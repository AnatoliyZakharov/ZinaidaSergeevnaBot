"""
Smoke-тесты: модули импортируются и нет захардкоженных секретов.
Запуск: python test_smoke.py  или  pytest test_smoke.py
"""
import ast, re, pathlib, os

REPO_ROOT = pathlib.Path(__file__).parent

FORBIDDEN_PATTERNS = [
    r'mm_[A-Za-z0-9_\-]{20,}',   # MYmeet token
    r'AIza[0-9A-Za-z\-_]{35}',   # Google API key
]


def test_no_hardcoded_secrets():
    for path in REPO_ROOT.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "get"):
                continue
            if len(node.args) < 2:
                continue
            default = node.args[1]
            if not isinstance(default, ast.Constant) or not isinstance(default.value, str):
                continue
            val = default.value
            for pattern in FORBIDDEN_PATTERNS:
                assert not re.search(pattern, val), (
                    f"{path.name}:{node.lineno}: захардкоженный секрет: {val[:30]!r}"
                )


def _load_mymeet():
    import importlib.util
    for key in ("MYMEET_TOKEN","MYMEET_WS_ID","MYMEET_SPREADSHEET_ID","ORGANIZER_EMAIL","GOOGLE_SA_KEY"):
        os.environ.setdefault(key, "")
    spec = importlib.util.spec_from_file_location("mymeet_sync", REPO_ROOT / "mymeet_sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mymeet_sync_imports():
    try:
        _load_mymeet()
    except Exception as e:
        raise AssertionError(f"mymeet_sync.py не импортируется: {e}") from e


def test_build_user_config_rejects_cyrillic():
    mod = _load_mymeet()
    try:
        mod._build_user_config({"mymeet_token": "КириллицаВТокене"})
        raise AssertionError("Должен был бросить ValueError")
    except ValueError as e:
        assert "кириллица" in str(e).lower() or "недопустим" in str(e).lower(), str(e)


if __name__ == "__main__":
    test_no_hardcoded_secrets()
    print("✓ нет захардкоженных секретов")
    test_mymeet_sync_imports()
    print("✓ mymeet_sync импортируется")
    test_build_user_config_rejects_cyrillic()
    print("✓ кириллица в токене отклоняется")
    print("\n✅ Все проверки прошли")
