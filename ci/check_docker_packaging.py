#!/usr/bin/env python3
"""
Проверка полноты Docker-образа: все локальные Python-модули, которые нужны
приложению при запуске, должны попадать в образ через COPY/ADD в Dockerfile.

Алгоритм:
  1. Разбираем Dockerfile: WORKDIR, COPY/ADD (shell- и JSON-форма, многострочные
     инструкции, флаги --chown/--chmod/--link, COPY --from=<stage> пропускаем —
     это не build-context), CMD/ENTRYPOINT.
  2. Определяем entrypoint (`python X.py` или `python -m mod`), сверяем с Procfile.
  3. Обходим AST от entrypoint по транзитивным локальным импортам — включая
     импорты внутри функций/try/if, `from ... import ...`, относительные и
     `importlib.import_module("литерал")` / `__import__("литерал")`.
  4. Каждый требуемый файл должен оказаться в образе по пути, откуда его
     импортирует интерпретатор. Тесты и утилиты, не достижимые из entrypoint,
     не требуются.

Только stdlib. Выход 1 — найдены ошибки упаковки.

Использование:
  python ci/check_docker_packaging.py                 # проверка, отчёт в stdout
  python ci/check_docker_packaging.py --list-modules  # имена модулей через пробел
  python ci/check_docker_packaging.py --print-entrypoint  # путь entrypoint в образе
  python ci/check_docker_packaging.py --repo <dir>
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import posixpath
import shlex
import subprocess
import sys
import warnings as _warnings
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_WORKDIR = "/"


# ─── Dockerfile ──────────────────────────────────────────────────────────────

@dataclass
class CopyInstr:
    sources: list[str]
    dest: str            # абсолютный путь в образе (с учётом WORKDIR)
    dest_is_dir: bool
    lineno: int
    instr: str           # COPY / ADD


@dataclass
class DockerInfo:
    copies: list[CopyInstr] = field(default_factory=list)
    copies_all: bool = False
    workdir: str = DEFAULT_WORKDIR
    cmd: Optional[list[str]] = None
    entrypoint: Optional[list[str]] = None
    warnings: list[str] = field(default_factory=list)


def _logical_lines(text: str):
    """Склейка продолжений `\\`, пропуск комментариев. Возвращает (lineno, line)."""
    buf, start = [], None
    for i, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not buf and (not stripped or stripped.startswith("#")):
            continue
        if buf and stripped.startswith("#"):
            continue  # комментарий внутри многострочной инструкции
        if start is None:
            start = i
        if stripped.endswith("\\"):
            buf.append(stripped[:-1].strip())
            continue
        buf.append(stripped)
        yield start, " ".join(p for p in buf if p)
        buf, start = [], None
    if buf:
        yield start, " ".join(p for p in buf if p)


def _split_args(rest: str) -> tuple[list[str], bool]:
    """JSON-форма ["a","b"] или shell-форма. Возвращает (args, is_json)."""
    rest = rest.strip()
    if rest.startswith("["):
        try:
            val = json.loads(rest)
            if isinstance(val, list) and all(isinstance(x, str) for x in val):
                return val, True
        except ValueError:
            pass
    try:
        return shlex.split(rest), False
    except ValueError:
        return rest.split(), False


def _resolve(workdir: str, path: str) -> str:
    if path.startswith("/"):
        return posixpath.normpath(path)
    return posixpath.normpath(posixpath.join(workdir, path))


def parse_dockerfile(text: str) -> DockerInfo:
    info = DockerInfo()
    for lineno, line in _logical_lines(text):
        parts = line.split(None, 1)
        instr = parts[0].upper()
        rest = parts[1] if len(parts) > 1 else ""
        if instr == "FROM":
            # новая стадия: состояние WORKDIR/CMD сбрасывается к базовому
            info.workdir = DEFAULT_WORKDIR
            info.copies = []
            info.copies_all = False
            info.cmd = None
            info.entrypoint = None
        elif instr == "WORKDIR":
            info.workdir = _resolve(info.workdir, rest.strip().strip('"'))
        elif instr in ("COPY", "ADD"):
            tokens = rest.split()
            flags = []
            while tokens and tokens[0].startswith("--"):
                flags.append(tokens.pop(0))
            body = rest
            for f in flags:
                body = body.replace(f, "", 1)
            if any(f.startswith("--from") for f in flags):
                continue  # копирование из другой стадии/образа — не build-context
            args, _ = _split_args(body)
            if len(args) < 2:
                info.warnings.append(f"Dockerfile:{lineno}: {instr} без источника/назначения")
                continue
            sources, dst = args[:-1], args[-1]
            dest_is_dir = (
                dst.endswith("/") or dst in (".", "..") or len(sources) > 1
            )
            for s in sources:
                if s in (".", "./"):
                    info.copies_all = True
                    info.warnings.append(
                        f"Dockerfile:{lineno}: `{instr} . {dst}` копирует весь контекст — "
                        "состав образа определяется .dockerignore"
                    )
                if instr == "ADD" and ("://" in s or s.endswith((".tar", ".tar.gz", ".tgz"))):
                    info.warnings.append(f"Dockerfile:{lineno}: ADD {s} — содержимое не анализируется")
            info.copies.append(CopyInstr(sources, _resolve(info.workdir, dst),
                                         dest_is_dir, lineno, instr))
        elif instr == "CMD":
            info.cmd = _split_args(rest)[0]
        elif instr == "ENTRYPOINT":
            info.entrypoint = _split_args(rest)[0]
    return info


# ─── .dockerignore ───────────────────────────────────────────────────────────

def _load_dockerignore(repo: str) -> list[tuple[bool, str]]:
    path = os.path.join(repo, ".dockerignore")
    rules: list[tuple[bool, str]] = []
    if not os.path.isfile(path):
        return rules
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            neg = line.startswith("!")
            pat = line[1:] if neg else line
            pat = posixpath.normpath(pat.lstrip("/"))
            rules.append((neg, pat))
    return rules


def _ignored(rel: str, rules: list[tuple[bool, str]]) -> bool:
    ignored = False
    for neg, pat in rules:
        p = pat.replace("**/", "*").replace("/**", "/*")
        hit = fnmatch.fnmatch(rel, p) or any(
            fnmatch.fnmatch(rel[: i], p) for i in range(len(rel)) if rel[i] == "/"
        )
        if hit:
            ignored = not neg
    return ignored


# ─── Файлы репозитория ───────────────────────────────────────────────────────

_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache"}


def repo_files(repo: str) -> list[str]:
    """Файлы репозитория (git ls-files: tracked + untracked не из .gitignore).
    Fallback — обход файловой системы."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        files = sorted({f for f in out.splitlines() if f and os.path.exists(os.path.join(repo, f))})
        if files:
            return files
    except (OSError, subprocess.SubprocessError):
        pass
    files = []
    for root, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for n in names:
            files.append(os.path.relpath(os.path.join(root, n), repo).replace(os.sep, "/"))
    return sorted(files)


def image_files(info: DockerInfo, files: list[str], repo: str) -> dict[str, str]:
    """Отображение: путь в образе -> путь в репозитории (для файлов контекста)."""
    rules = _load_dockerignore(repo)
    context = [f for f in files if not _ignored(f, rules)]
    mapping: dict[str, str] = {}
    for c in info.copies:
        for src in c.sources:
            src_n = posixpath.normpath(src.lstrip("/")) if src not in (".", "./") else "."
            has_glob = any(ch in src_n for ch in "*?[")
            for f in context:
                if src_n == ".":
                    target = posixpath.join(c.dest, f)
                elif has_glob:
                    if not fnmatch.fnmatch(f, src_n):
                        continue
                    target = posixpath.join(c.dest, posixpath.basename(f))  # glob -> каталог
                elif f == src_n:
                    target = posixpath.join(c.dest, posixpath.basename(f)) if c.dest_is_dir else c.dest
                elif f.startswith(src_n + "/"):
                    target = posixpath.join(c.dest, f[len(src_n) + 1:])
                else:
                    continue
                mapping[posixpath.normpath(target)] = f
    return mapping


# ─── Entrypoint ──────────────────────────────────────────────────────────────

@dataclass
class EntryPoint:
    kind: str            # "script" | "module"
    target: str          # путь скрипта или имя модуля
    raw: str


_PY_OPTS_WITH_ARG = {"-W", "-X", "-c"}


def parse_entrypoint(argv: Optional[list[str]]) -> Optional[EntryPoint]:
    if not argv:
        return None
    args = list(argv)
    # shell-обёртки: sh -c "python main.py"
    if len(args) >= 3 and posixpath.basename(args[0]) in ("sh", "bash") and args[1] == "-c":
        args = shlex.split(args[2])
    for i, a in enumerate(args):
        base = posixpath.basename(a)
        if base.startswith("python"):
            j = i + 1
            while j < len(args):
                tok = args[j]
                if tok == "-m" and j + 1 < len(args):
                    return EntryPoint("module", args[j + 1], " ".join(argv))
                if tok in _PY_OPTS_WITH_ARG:
                    if tok == "-c":
                        return None
                    j += 2
                    continue
                if tok.startswith("-"):
                    j += 1
                    continue
                return EntryPoint("script", tok, " ".join(argv))
            return None
    return None


def parse_procfile(text: str) -> dict[str, Optional[EntryPoint]]:
    procs = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        name, cmd = line.split(":", 1)
        try:
            procs[name.strip()] = parse_entrypoint(shlex.split(cmd.strip()))
        except ValueError:
            procs[name.strip()] = None
    return procs


# ─── Импорты ─────────────────────────────────────────────────────────────────

@dataclass
class ImportRef:
    module: str          # абсолютное имя (после разрешения относительных)
    lineno: int
    names: tuple = ()    # для from-импортов: импортируемые имена (могут быть подмодулями)


def _package_of(rel_path: str) -> str:
    """Имя пакета, содержащего файл (для относительных импортов)."""
    parts = rel_path[:-3].split("/")  # без .py
    if parts[-1] == "__init__":
        parts = parts[:-1]
    else:
        parts = parts[:-1]
    return ".".join(parts)


def collect_imports(source: str, rel_path: str) -> tuple[list[ImportRef], list[str]]:
    """Все импорты файла, включая вложенные в функции/try/if."""
    refs: list[ImportRef] = []
    warnings: list[str] = []
    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore", SyntaxWarning)  # невалидные escape в коде приложения
        tree = ast.parse(source, filename=rel_path)
    import_module_aliases = {"import_module"}  # from importlib import import_module [as x]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for a in node.names:
                if a.name == "import_module":
                    import_module_aliases.add(a.asname or a.name)
    pkg = _package_of(rel_path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                refs.append(ImportRef(a.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            names = tuple(a.name for a in node.names if a.name != "*")
            if node.level == 0:
                refs.append(ImportRef(node.module or "", node.lineno, names))
            else:
                base = pkg.split(".") if pkg else []
                if node.level > 1:
                    base = base[: len(base) - (node.level - 1)] if len(base) >= node.level - 1 else []
                full = ".".join([p for p in base if p] + ([node.module] if node.module else []))
                if full:
                    refs.append(ImportRef(full, node.lineno, names))
                else:
                    # `from . import x` в модуле верхнего уровня: x — соседний модуль
                    for n in names:
                        refs.append(ImportRef(n, node.lineno))
        elif isinstance(node, ast.Call):
            fn = node.func
            is_dyn = (
                (isinstance(fn, ast.Attribute) and fn.attr == "import_module"
                 and isinstance(fn.value, ast.Name) and fn.value.id == "importlib")
                or (isinstance(fn, ast.Name) and fn.id in import_module_aliases | {"__import__"})
            )
            if not is_dyn:
                continue
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                name = arg.value
                if name.startswith("."):
                    warnings.append(f"{rel_path}:{node.lineno}: относительный динамический импорт "
                                    f"{name!r} — проверьте вручную")
                    continue
                refs.append(ImportRef(name, node.lineno))
            else:
                warnings.append(f"{rel_path}:{node.lineno}: динамический импорт с нелитеральным "
                                "аргументом — статически не проверяется")
    return refs, warnings


def _module_file(name: str, root: str, fileset: set[str]) -> Optional[str]:
    """Файл локального модуля `name` относительно корня импорта `root`."""
    if not name:
        return None
    rel = name.replace(".", "/")
    for cand in (rel + ".py", rel + "/__init__.py"):
        path = posixpath.normpath(posixpath.join(root, cand)) if root else cand
        if path in fileset:
            return path
    return None


def _resolve_local(ref: ImportRef, root: str, fileset: set[str]) -> list[str]:
    """Локальные файлы, которые требуются для импорта ref (включая родительские пакеты)."""
    parts = ref.module.split(".") if ref.module else []
    out: list[str] = []
    for i in range(1, len(parts) + 1):
        f = _module_file(".".join(parts[:i]), root, fileset)
        if f:
            out.append(f)
        elif i == 1:
            return []  # первый сегмент не локальный — сторонняя библиотека
    for n in ref.names:
        f = _module_file(".".join(parts + [n]), root, fileset)
        if f:
            out.append(f)
    return out


def _file_to_module(rel: str, root: str) -> str:
    r = posixpath.relpath(rel, root) if root else rel
    r = r[:-3]
    if r.endswith("/__init__"):
        r = r[: -len("/__init__")]
    return r.replace("/", ".")


# ─── Проверка ────────────────────────────────────────────────────────────────

@dataclass
class Missing:
    module: str
    file: str
    importer: str
    lineno: int

    def message(self) -> str:
        if self.importer:
            return (f"ERROR: module '{self.module}' (imported by {self.importer}:{self.lineno}) "
                    "is not copied into the image by Dockerfile")
        return f"ERROR: entrypoint '{self.file}' is not copied into the image by Dockerfile"


@dataclass
class Result:
    missing: list[Missing] = field(default_factory=list)
    required: list[str] = field(default_factory=list)     # файлы репозитория
    modules: list[str] = field(default_factory=list)      # имена модулей для import-smoke
    errors: list[str] = field(default_factory=list)       # прочие ошибки (entrypoint/Procfile)
    warnings: list[str] = field(default_factory=list)
    entry_image_path: str = ""
    entry_module: str = ""

    @property
    def ok(self) -> bool:
        return not self.missing and not self.errors


def required_modules(repo: str, entry_file: str, root: str, fileset: set[str]):
    """BFS по локальным импортам. Возвращает (dict файл -> (импортёр, строка), warnings)."""
    found: dict[str, tuple[str, int]] = {entry_file: ("", 0)}
    warnings: list[str] = []
    queue = deque([entry_file])
    while queue:
        cur = queue.popleft()
        try:
            with open(os.path.join(repo, cur), encoding="utf-8") as fh:
                src = fh.read()
            refs, w = collect_imports(src, cur)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            warnings.append(f"{cur}: не удалось разобрать ({type(exc).__name__})")
            continue
        warnings += w
        for ref in refs:
            for f in _resolve_local(ref, root, fileset):
                if f not in found:
                    found[f] = (cur, ref.lineno)
                    queue.append(f)
    return found, warnings


def check(repo: str) -> Result:
    res = Result()
    repo = os.path.abspath(repo)
    df_path = os.path.join(repo, "Dockerfile")
    if not os.path.isfile(df_path):
        res.errors.append("ERROR: Dockerfile not found")
        return res
    with open(df_path, encoding="utf-8") as fh:
        info = parse_dockerfile(fh.read())
    res.warnings += info.warnings

    argv = (info.entrypoint or []) + (info.cmd or []) if info.entrypoint else info.cmd
    entry = parse_entrypoint(argv)
    if entry is None:
        res.errors.append("ERROR: cannot determine python entrypoint from Dockerfile CMD/ENTRYPOINT")
        return res

    proc_path = os.path.join(repo, "Procfile")
    if os.path.isfile(proc_path):
        with open(proc_path, encoding="utf-8") as fh:
            procs = parse_procfile(fh.read())
        for name, pe in procs.items():
            if pe is None or (pe.kind, posixpath.normpath(pe.target)) != (entry.kind, posixpath.normpath(entry.target)):
                res.errors.append(
                    f"ERROR: Procfile process '{name}' ({pe.raw if pe else 'не python'}) "
                    f"does not match Dockerfile entrypoint ({entry.raw})"
                )

    files = repo_files(repo)
    fileset = set(files)
    mapping = image_files(info, files, repo)

    if entry.kind == "script":
        entry_img = _resolve(info.workdir, entry.target)
        img_root = posixpath.dirname(entry_img)
        entry_repo = mapping.get(entry_img)
        if entry_repo is None:
            res.entry_image_path = entry_img
            target = posixpath.normpath(entry.target.lstrip("/"))
            res.missing.append(Missing(posixpath.basename(target)[:-3], target, "", 0))
            return res
    else:
        img_root = info.workdir
        rel = entry.target.replace(".", "/")
        entry_repo = None
        for cand in (rel + ".py", rel + "/__main__.py", rel + "/__init__.py"):
            img = posixpath.join(img_root, cand)
            if img in mapping:
                entry_img, entry_repo = img, mapping[img]
                break
        if entry_repo is None:
            res.entry_image_path = posixpath.join(img_root, rel + ".py")
            res.missing.append(Missing(entry.target, rel + ".py", "", 0))
            return res
    res.entry_image_path = entry_img
    repo_root = posixpath.dirname(entry_repo)
    if entry.kind == "module" and entry_repo.endswith(("/__main__.py", "/__init__.py")):
        repo_root = posixpath.dirname(posixpath.dirname(entry_repo))
    if repo_root == ".":
        repo_root = ""

    found, warns = required_modules(repo, entry_repo, repo_root, fileset)
    res.warnings += warns
    res.entry_module = _file_to_module(entry_repo, repo_root)
    for f, (importer, lineno) in found.items():
        res.required.append(f)
        mod = _file_to_module(f, repo_root)
        if mod.endswith(".__main__"):
            continue
        res.modules.append(mod)
        rel_in_root = posixpath.relpath(f, repo_root) if repo_root else f
        target = posixpath.normpath(posixpath.join(img_root, rel_in_root))
        if mapping.get(target) != f:
            res.missing.append(Missing(mod, f, importer, lineno))
    # entrypoint-модуль импортируем последним (порядок не важен, но так понятнее отчёт)
    res.modules.sort(key=lambda m: (m == res.entry_module, m))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Проверка полноты Docker-образа")
    ap.add_argument("--repo", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    ap.add_argument("--list-modules", action="store_true",
                    help="вывести имена требуемых модулей через пробел")
    ap.add_argument("--print-entrypoint", action="store_true",
                    help="вывести путь entrypoint-файла в образе")
    args = ap.parse_args(argv)
    res = check(args.repo)
    if args.list_modules:
        if not res.ok:
            for m in res.missing:
                print(m.message(), file=sys.stderr)
            for e in res.errors:
                print(e, file=sys.stderr)
            return 1
        print(" ".join(res.modules))
        return 0
    if args.print_entrypoint:
        print(res.entry_image_path)
        return 0 if res.ok else 1
    for w in res.warnings:
        print(f"WARNING: {w}")
    for e in res.errors:
        print(e)
    for m in res.missing:
        print(m.message())
    if res.ok:
        print(f"OK: {len(res.required)} local module(s) required by entrypoint "
              f"{res.entry_image_path}, all copied: {' '.join(res.modules)}")
        return 0
    print(f"FAILED: {len(res.missing)} missing module(s), {len(res.errors)} other error(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
