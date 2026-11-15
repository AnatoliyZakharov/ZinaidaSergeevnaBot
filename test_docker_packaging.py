"""
Тесты ci/check_docker_packaging.py — полнота Docker-образа.
Каждый тест строит временный «репозиторий» (без git → обход ФС), реальный
Dockerfile проекта проверяется отдельно. Без сети и без docker.
Запуск: python test_docker_packaging.py  (также входит в python test_smoke.py)
"""
import io
import os
import pathlib
import sys
import tempfile
import textwrap
from contextlib import redirect_stdout, redirect_stderr

REPO_ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(REPO_ROOT / "ci"))
import check_docker_packaging as cdp  # noqa: E402

BASE_DOCKERFILE = """\
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY {files} ./
CMD ["python", "main.py"]
"""


def _repo(files: dict, dockerfile: str = None, copy: str = None, procfile="worker: python main.py\n"):
    d = tempfile.mkdtemp(prefix="pkgtest_")
    for name, body in files.items():
        p = os.path.join(d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(body))
    with open(os.path.join(d, "requirements.txt"), "w") as fh:
        fh.write("requests\n")
    if dockerfile is None:
        dockerfile = BASE_DOCKERFILE.format(files=copy or " ".join(
            n for n in files if n.endswith(".py") and "/" not in n and not n.startswith("test_")))
    with open(os.path.join(d, "Dockerfile"), "w") as fh:
        fh.write(dockerfile)
    if procfile is not None:
        with open(os.path.join(d, "Procfile"), "w") as fh:
            fh.write(procfile)
    return d


def _cli(repo, *args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cdp.main(["--repo", repo, *args])
    return code, out.getvalue() + err.getvalue()


def test_pkg_missing_module_fails():
    """1. Новый модуль импортируется, но не в COPY → missing, exit 1, понятное сообщение."""
    repo = _repo({"main.py": "import os\nimport db\nimport my_tasks\n",
                  "db.py": "import sqlite3\n", "my_tasks.py": "x = 1\n"},
                 copy="main.py db.py")
    res = cdp.check(repo)
    assert [m.module for m in res.missing] == ["my_tasks"], res.missing
    code, out = _cli(repo)
    assert code == 1, out
    assert ("ERROR: module 'my_tasks' (imported by main.py:3) is not copied "
            "into the image by Dockerfile") in out, out
    code, _ = _cli(repo, "--list-modules")
    assert code == 1


def test_pkg_all_present_ok():
    """2. Всё скопировано → OK; тесты и утилиты не требуются."""
    repo = _repo({"main.py": "import db\n", "db.py": "import crypto\n", "crypto.py": "",
                  "test_main.py": "import main\n", "tools_local.py": "import main\n"},
                 copy="main.py db.py crypto.py")
    res = cdp.check(repo)
    assert res.ok, (res.missing, res.errors)
    assert sorted(res.required) == ["crypto.py", "db.py", "main.py"], res.required
    assert res.modules[-1] == "main" and set(res.modules) == {"main", "db", "crypto"}
    code, out = _cli(repo, "--list-modules")
    assert code == 0 and set(out.split()) == {"main", "db", "crypto"}, out


def test_pkg_real_repository_dockerfile_ok():
    """2b. Реальный Dockerfile репозитория: все модули приложения (включая my_tasks) в образе."""
    res = cdp.check(str(REPO_ROOT))
    assert res.ok, ([m.message() for m in res.missing], res.errors)
    for mod in ("main", "db", "auth", "gtasks", "crypto", "mymeet_sync", "my_tasks"):
        assert mod in res.modules, (mod, res.modules)
    assert not any(m.startswith("test_") for m in res.modules), res.modules
    assert res.entry_image_path == "/app/main.py"


def test_pkg_transitive_and_nested_import_detected():
    """3. Транзитивный импорт (main → a → b) и импорт внутри функции/try."""
    repo = _repo({
        "main.py": "import a\n",
        "a.py": "def f():\n    try:\n        import b\n    except ImportError:\n        pass\n",
        "b.py": "if True:\n    from c import thing\n",
        "c.py": "thing = 1\n",
    }, copy="main.py a.py b.py")
    res = cdp.check(repo)
    assert [(m.module, m.importer) for m in res.missing] == [("c", "b.py")], res.missing


def test_pkg_relative_and_package_imports():
    """Пакеты и относительные импорты: from . import x, from .sub import y."""
    repo = _repo({
        "main.py": "from pkg import helpers\n",
        "pkg/__init__.py": "from .core import run\n",
        "pkg/core.py": "from . import util\n",
        "pkg/util.py": "",
        "pkg/helpers.py": "",
    }, dockerfile="FROM python:3.11\nWORKDIR /app\nCOPY main.py ./\n"
                  "COPY pkg/__init__.py pkg/core.py pkg/helpers.py ./pkg/\nCMD [\"python\", \"main.py\"]\n")
    res = cdp.check(repo)
    assert [m.file for m in res.missing] == ["pkg/util.py"], res.missing
    # каталог целиком — OK
    repo2 = _repo({
        "main.py": "from pkg import helpers\n", "pkg/__init__.py": "from .core import run\n",
        "pkg/core.py": "from . import util\n", "pkg/util.py": "", "pkg/helpers.py": "",
    }, dockerfile="FROM python:3.11\nWORKDIR /app\nCOPY main.py ./\nCOPY pkg ./pkg\nCMD python main.py\n")
    res2 = cdp.check(repo2)
    assert res2.ok, res2.missing
    assert "pkg.util" in res2.modules


def test_pkg_importlib_literal_and_dynamic_warning():
    """importlib.import_module("lit") и __import__ — учитываются; нелитеральный — warning."""
    repo = _repo({
        "main.py": ("import importlib\nfrom importlib import import_module as im\n"
                    "importlib.import_module('plug_a')\nim('plug_b')\n__import__('plug_c')\n"
                    "name = 'x'\nimportlib.import_module(name)\n"),
        "plug_a.py": "", "plug_b.py": "", "plug_c.py": "",
    }, copy="main.py plug_a.py")
    res = cdp.check(repo)
    assert sorted(m.module for m in res.missing) == ["plug_b", "plug_c"], res.missing
    assert any("нелитеральным" in w for w in res.warnings), res.warnings


def test_pkg_multiline_copy_chown_and_json_form():
    """Многострочный COPY с `\\`, --chown/--chmod, JSON-форма, COPY --from игнорируется."""
    df = ("FROM python:3.11 AS base\nRUN true\nFROM python:3.11-slim\n# comment\nWORKDIR /srv\n"
          "WORKDIR app\n"
          "COPY --from=base /usr/lib/x.py ./\n"
          "COPY --chown=app:app --chmod=644 main.py \\\n    # inline comment\n    db.py \\\n    ./\n"
          "COPY [\"crypto.py\", \"./\"]\n"
          "ENTRYPOINT [\"python\", \"-u\"]\nCMD [\"main.py\"]\n")
    repo = _repo({"main.py": "import db\nimport crypto\nimport x\n", "db.py": "", "crypto.py": "",
                  "x.py": ""}, dockerfile=df, procfile="worker: python -u main.py\n")
    res = cdp.check(repo)
    assert [m.module for m in res.missing] == ["x"], (res.missing, res.errors)
    assert res.entry_image_path == "/srv/app/main.py", res.entry_image_path


def test_pkg_copy_all_context_with_dockerignore():
    """COPY . . — всё в образе (с предупреждением); .dockerignore исключает файл → missing."""
    repo = _repo({"main.py": "import db\n", "db.py": ""},
                 dockerfile="FROM python:3.11\nWORKDIR /app\nCOPY . .\nCMD [\"python\",\"main.py\"]\n")
    res = cdp.check(repo)
    assert res.ok, res.missing
    assert any("копирует весь контекст" in w for w in res.warnings), res.warnings
    with open(os.path.join(repo, ".dockerignore"), "w") as fh:
        fh.write("*.md\ndb.py\n")
    res = cdp.check(repo)
    assert [m.module for m in res.missing] == ["db"], res.missing


def test_pkg_entrypoint_not_copied_and_procfile_mismatch():
    """Entrypoint не скопирован → ошибка; CMD ≠ Procfile → ошибка."""
    repo = _repo({"main.py": "", "bot.py": ""}, copy="bot.py")
    res = cdp.check(repo)
    assert not res.ok and res.missing and res.missing[0].file == "main.py", res.missing
    code, out = _cli(repo)
    assert code == 1 and "entrypoint 'main.py' is not copied" in out, out
    repo2 = _repo({"main.py": "", "bot.py": ""}, copy="main.py bot.py",
                  procfile="worker: python bot.py\n")
    res2 = cdp.check(repo2)
    assert any("Procfile" in e for e in res2.errors), res2.errors
    code, _ = _cli(repo2)
    assert code == 1


def test_pkg_python_m_entrypoint():
    """CMD python -m app: модуль ищется от WORKDIR."""
    repo = _repo({"app.py": "import db\n", "db.py": ""},
                 dockerfile="FROM python:3.11\nWORKDIR /app\nCOPY app.py ./\nCMD [\"python\",\"-m\",\"app\"]\n",
                 procfile=None)
    res = cdp.check(repo)
    assert [m.module for m in res.missing] == ["db"], (res.missing, res.errors)


TESTS = [
    (test_pkg_missing_module_fails, "Docker: модуль не в COPY → ошибка, exit 1"),
    (test_pkg_all_present_ok, "Docker: всё скопировано → OK, тесты не требуются"),
    (test_pkg_real_repository_dockerfile_ok, "Docker: реальный Dockerfile репозитория → OK"),
    (test_pkg_transitive_and_nested_import_detected, "Docker: транзитивный/вложенный импорт"),
    (test_pkg_relative_and_package_imports, "Docker: пакеты и относительные импорты"),
    (test_pkg_importlib_literal_and_dynamic_warning, "Docker: importlib/__import__, warning"),
    (test_pkg_multiline_copy_chown_and_json_form, "Docker: многострочный COPY, --chown, JSON"),
    (test_pkg_copy_all_context_with_dockerignore, "Docker: COPY . . и .dockerignore"),
    (test_pkg_entrypoint_not_copied_and_procfile_mismatch, "Docker: entrypoint/Procfile"),
    (test_pkg_python_m_entrypoint, "Docker: CMD python -m"),
]

if __name__ == "__main__":
    failed = []
    for fn, label in TESTS:
        try:
            fn()
            print(f"  ✓ {label}")
        except AssertionError as e:
            print(f"  ✗ {label}: {e}")
            failed.append(label)
    if failed:
        raise SystemExit(f"\n❌ Упали тесты: {failed}")
    print("\n✅ Все тесты упаковки прошли")
