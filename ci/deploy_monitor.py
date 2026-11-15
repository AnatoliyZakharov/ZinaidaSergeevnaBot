#!/usr/bin/env python3
"""
Deploy Monitor: проверка production-релиза на Fly.io после деплоя.

Команды:
  capture-prev  — до деплоя зафиксировать текущую (предыдущую) версию
                  (образ, digest, GH_SHA, release) → $GITHUB_OUTPUT.
  run           — дождаться новой версии (GH_SHA == expected), 10 минут наблюдать
                  от её старта (T0), вынести вердикт, при FAILED — безопасный
                  откат по утверждённой процедуре, отчёт.

Статусы: HEALTHY, DEGRADED, FAILED, ROLLED_BACK, UNKNOWN. Exit 0 только при HEALTHY.

Безопасность (репозиторий публичный):
  * в отчёты попадают только категории ошибок, классы исключений, timestamps и
    счётчики — НИКАКОГО текста логов приложения, сырого JSON flyctl, email, env;
  * flyctl вызывается только через allowlist фиксированных команд: volumes,
    secrets, БД, destroy и т.п. недоступны по построению;
  * откат = `flyctl deploy --image <предыдущий образ>`; данные/volume не трогаются.

Только stdlib.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HEALTHY, DEGRADED, FAILED, ROLLED_BACK, UNKNOWN = (
    "HEALTHY", "DEGRADED", "FAILED", "ROLLED_BACK", "UNKNOWN")
STATUSES = (HEALTHY, DEGRADED, FAILED, ROLLED_BACK, UNKNOWN)

# Файлы, изменение которых между версиями делает автооткат небезопасным
# (схема/миграции БД, шифрование, конфигурация Fly/volume).
ROLLBACK_PROTECTED_FILES = ("db.py", "crypto.py", "fly.toml")

DEFAULT_APP = "calendar-bot-anatolii"
# `flyctl logs --no-tail` отдаёт хвост буфера (~100 записей). Полный буфер, начинающийся
# позже T0, означает, что записи между T0 и первым опросом могли быть вытеснены.
LOG_BUFFER_FULL = 100
MODULE_CHECK_OK = "DEPLOY_MONITOR_IMPORT_OK"


@dataclass
class Config:
    wait_version_timeout: float = 6 * 60
    observe: float = 10 * 60
    poll: float = 30
    max_fly_errors_consecutive: int = 3
    min_coverage: float = 0.9
    fail_check_consecutive: int = 3
    rollback_verify: float = 4 * 60
    module_check_delay: float = 60
    module_check_attempts: int = 2
    restart_fail_threshold: int = 2
    min_live_ticks: int = 3


class FlyUnavailable(Exception):
    """flyctl недоступен / ошибка / timeout / невалидный JSON."""


class ForbiddenCommand(ValueError):
    """Команда flyctl вне allowlist."""


# ─── Allowlist flyctl ────────────────────────────────────────────────────────

_RE_APP = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_RE_MACHINE = re.compile(r"^[0-9a-f]{8,20}$")
_RE_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_RE_SHA = re.compile(r"^[0-9a-f]{7,40}$")


# Переменные, которые модули приложения читают через os.environ[...] при импорте.
# Если ssh-сессия Fly не получает env машины, они подставляются фиктивными
# значениями ТОЛЬКО при отсутствии (setdefault — реальные не перезаписываются),
# чтобы KeyError не маскировал проверку. DB_PATH → /tmp: импорт БД не открывает,
# но даже теоретически не коснётся /data/bot.db. Значения не секретные.
MODULE_CHECK_ENV_PLACEHOLDERS = (
    ("TELEGRAM_TOKEN", "deploy-monitor-placeholder"),
    ("YANDEX_API_KEY", "deploy-monitor-placeholder"),
    ("YANDEX_FOLDER_ID", "deploy-monitor-placeholder"),
    ("GOOGLE_CLIENT_ID", "deploy-monitor-placeholder"),
    ("GOOGLE_CLIENT_SECRET", "deploy-monitor-placeholder"),
    ("OAUTH_REDIRECT_URI", "http://localhost/deploy-monitor"),
    ("DB_PATH", "/tmp/deploy-monitor-import-check.db"),
)
# Только эти классы исключений означают сломанную упаковку → FAILED.
# Всё остальное (KeyError, нет маркера, ошибка ssh, timeout) — нет данных → UNKNOWN.
MODULE_CHECK_FATAL_EXC = ("ModuleNotFoundError", "ImportError", "SyntaxError")


def build_module_check_command(modules: list[str]) -> str:
    """Фиксированная read-only проверка импорта модулей внутри работающей машины.
    Только import: бот не запускается (main.py стартует лишь в __main__), БД не открывается."""
    if not modules or not all(_RE_MODULE.match(m) for m in modules):
        raise ForbiddenCommand("invalid module list")
    env = ";".join(f"os.environ.setdefault(\"{k}\",\"{v}\")" for k, v in MODULE_CHECK_ENV_PLACEHOLDERS)
    code = ("import importlib,os,sys;" + env + ";sys.path.insert(0,\"/app\");"
            "[importlib.import_module(m) for m in sys.argv[1:]];"
            f"print(\"{MODULE_CHECK_OK}\")")
    return f"python -c '{code}' " + " ".join(modules)


def classify_module_check(exc: str) -> str:
    """'failed' только для явных ошибок импорта/синтаксиса, иначе 'inconclusive'."""
    base = (exc or "").rsplit(".", 1)[-1]
    return "failed" if base in MODULE_CHECK_FATAL_EXC else "inconclusive"


def image_ref_ok(ref: str, app: str) -> bool:
    return bool(re.match(
        r"^registry\.fly\.io/" + re.escape(app) + r"(:[A-Za-z0-9._-]{1,128}|@sha256:[0-9a-f]{64})$",
        ref or ""))


def validate_flyctl_args(args: list[str], app: str) -> None:
    """Пропускает только фиксированные шаблоны команд. Иначе ForbiddenCommand."""
    if not _RE_APP.match(app):
        raise ForbiddenCommand("invalid app name")
    a = list(args)
    templates = [
        ["status", "--app", app, "--json"],
        ["releases", "--app", app, "--json", "--image"],
        ["logs", "--app", app, "--no-tail", "--json"],
        ["machines", "list", "--app", app, "--json"],
    ]
    if a in templates:
        return
    if len(a) == 8 and a[:5] == ["ssh", "console", "--app", app, "--machine"] and a[6] == "-C":
        if not _RE_MACHINE.match(a[5] or ""):
            raise ForbiddenCommand("invalid machine id")
        mods = a[7].split("' ", 1)[1].split() if "' " in a[7] else []
        try:
            if a[7] == build_module_check_command(mods):
                return
        except ForbiddenCommand:
            pass
        raise ForbiddenCommand("ssh console: only the fixed import check is allowed")
    if (len(a) == 9 and a[:4] == ["deploy", "--app", app, "--image"] and image_ref_ok(a[4], app)
            and a[5:] == ["--config", "fly.toml", "--wait-timeout", "5m"]):
        return
    raise ForbiddenCommand(f"flyctl subcommand not allowed: {a[0] if a else '<empty>'}")


# ─── Разбор логов ────────────────────────────────────────────────────────────

def parse_log_stream(text: str) -> list[dict]:
    """`flyctl logs --json` — поток многострочных JSON-объектов (не NDJSON)."""
    dec = json.JSONDecoder()
    out, i, n = [], 0, len(text)
    while i < n:
        j = text.find("{", i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(text, j)
        except ValueError:
            i = j + 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        i = end
    return out


def parse_ts(value) -> Optional[float]:
    """RFC3339 (с наносекундами) или epoch-ms → epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e11 else float(value)
    s = str(value).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?$", s)
    if not m:
        return None
    base, frac, tz = m.group(1), m.group(2) or "", m.group(3) or "Z"
    frac = (frac + "000000")[:7] if frac else ""
    tz = "+00:00" if tz == "Z" else tz
    try:
        return datetime.fromisoformat(base + frac + tz).timestamp()
    except ValueError:
        return None


def iso(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_FATAL_RULES = [
    ("ModuleNotFoundError", re.compile(r"\bModuleNotFoundError\b")),
    ("ImportError", re.compile(r"\bImportError\b")),
    ("SyntaxError", re.compile(r"\bSyntaxError\b")),
    ("process_exit_nonzero", re.compile(r"exit(?:ed)?\b[^\n]{0,60}?\bcode:?\s*[1-9]\d*", re.I)),
    ("oom_killed", re.compile(r"\bOOM\b|Out of memory", re.I)),
    ("telegram_conflict", re.compile(r"terminated by other getUpdates|\bConflict\b.{0,80}getUpdates", re.I)),
]
_ERROR_RULES = [
    ("traceback", re.compile(r"\bTraceback \(most recent call last\)")),
    ("log_error", re.compile(r"\[ERROR\]")),
    ("log_critical", re.compile(r"\[CRITICAL\]")),
    ("unhandled_exception", re.compile(r"Необработанное исключение")),
]
_EXC_CLASS = re.compile(r"\b((?:[A-Za-z_]\w*\.)*[A-Z]\w*(?:Error|Exception|Conflict))\b")


def classify_message(msg: str):
    """→ (severity, category, exception_class) или None. Текст сообщения не сохраняется."""
    if not msg:
        return None
    exc = None
    m = _EXC_CLASS.search(msg)
    if m:
        exc = m.group(1)
    for cat, rx in _FATAL_RULES:
        if rx.search(msg):
            return ("FATAL", cat, exc)
    for cat, rx in _ERROR_RULES:
        if rx.search(msg):
            return ("ERROR", cat, exc)
    return None


# ─── Снимок состояния Fly ────────────────────────────────────────────────────

@dataclass
class MachineSnap:
    id: str
    state: str
    group: str
    gh_sha: str
    digest: str
    image: str
    release: Optional[int]
    checks: list          # [(name, status)]
    events: list          # [(type, status, ts_seconds)]

    @property
    def checks_failing(self) -> bool:
        return any(st != "passing" for _, st in self.checks)

    def last_start(self) -> Optional[float]:
        ts = [t for (ty, st, t) in self.events if ty == "start" and st == "started" and t]
        return max(ts) if ts else None


@dataclass
class Snapshot:
    version: Optional[int]
    machines: list

    @property
    def app_machines(self) -> list:
        return [m for m in self.machines if m.group in ("app", "")]


def snapshot_from_status(data: dict) -> Snapshot:
    if not isinstance(data, dict) or not isinstance(data.get("Machines"), list):
        raise FlyUnavailable("unexpected status payload")
    machines = []
    for m in data["Machines"]:
        ir = m.get("image_ref") or {}
        cfg = m.get("config") or {}
        md = cfg.get("metadata") or {}
        labels = ir.get("labels") or {}
        try:
            rel = int(md.get("fly_release_version")) if md.get("fly_release_version") else None
        except (TypeError, ValueError):
            rel = None
        events = []
        for e in m.get("events") or []:
            events.append((e.get("type", ""), e.get("status", ""), parse_ts(e.get("timestamp"))))
        checks = [(c.get("name", ""), c.get("status", "")) for c in (m.get("checks") or [])]
        machines.append(MachineSnap(
            id=str(m.get("id", "")), state=str(m.get("state", "")),
            group=str(md.get("fly_process_group", "")), gh_sha=str(labels.get("GH_SHA", "")),
            digest=str(ir.get("digest", "")), image=str(cfg.get("image", "")),
            release=rel, checks=checks, events=events))
    v = data.get("Version")
    return Snapshot(version=int(v) if isinstance(v, (int, float)) else None, machines=machines)


# ─── Адаптеры ────────────────────────────────────────────────────────────────

class FlyCtl:
    """Реальный клиент: subprocess flyctl, только allowlist."""

    def __init__(self, app: str, binary: Optional[str] = None, timeout: int = 60, runner=None):
        self.app = app
        self.binary = binary or shutil.which("flyctl") or shutil.which("fly") or "flyctl"
        self.timeout = timeout
        self.runner = runner or subprocess.run

    def _run(self, args: list[str], timeout: Optional[int] = None, check: bool = True) -> str:
        validate_flyctl_args(args, self.app)
        try:
            p = self.runner([self.binary] + args, capture_output=True, text=True,
                            timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            raise FlyUnavailable(f"flyctl {args[0]}: timeout")
        except OSError as exc:
            raise FlyUnavailable(f"flyctl {args[0]}: {type(exc).__name__}")
        if check and p.returncode != 0:
            raise FlyUnavailable(f"flyctl {args[0]}: exit {p.returncode}")
        return (p.stdout or "") + ("\n" + p.stderr if not check and p.stderr else "")

    def _json(self, args):
        out = self._run(args)
        try:
            return json.loads(out)
        except ValueError:
            raise FlyUnavailable(f"flyctl {args[0]}: invalid JSON")

    def status(self) -> dict:
        return self._json(["status", "--app", self.app, "--json"])

    def releases(self) -> list:
        data = self._json(["releases", "--app", self.app, "--json", "--image"])
        if not isinstance(data, list):
            raise FlyUnavailable("flyctl releases: unexpected payload")
        return data

    def logs(self) -> list:
        return parse_log_stream(self._run(["logs", "--app", self.app, "--no-tail", "--json"]))

    def exec_check(self, machine_id: str, modules: list[str]) -> tuple[Optional[bool], str]:
        """→ (True, "") — импорт прошёл; (False, ExcClass) — Traceback в выводе (решение о
        FAILED/UNKNOWN — classify_module_check); FlyUnavailable — ssh недоступен/нет маркера."""
        cmd = build_module_check_command(modules)
        out = self._run(["ssh", "console", "--app", self.app, "--machine", machine_id, "-C", cmd],
                        timeout=120, check=False)
        if MODULE_CHECK_OK in out:
            return True, ""
        if "Traceback (most recent call last)" in out:
            exc = ""
            for line in out.splitlines()[::-1]:
                m = re.match(r"^((?:[A-Za-z_]\w*\.)*[A-Z]\w*(?:Error|Exception))\b", line.strip())
                if m:
                    exc = m.group(1)
                    break
            return False, exc or "UnknownError"
        raise FlyUnavailable("ssh console: no result")

    def deploy_image(self, image_ref: str) -> bool:
        args = ["deploy", "--app", self.app, "--image", image_ref,
                "--config", "fly.toml", "--wait-timeout", "5m"]
        try:
            self._run(args, timeout=480)
            return True
        except FlyUnavailable:
            return False


class GitClient:
    def __init__(self, repo: str = "."):
        self.repo = repo

    def _git(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", self.repo, *args], capture_output=True,
                              text=True, timeout=30)

    def has_commit(self, sha: str) -> bool:
        if not _RE_SHA.match(sha or ""):
            return False
        try:
            return self._git("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def changed_files(self, a: str, b: str) -> Optional[list[str]]:
        if not (_RE_SHA.match(a or "") and _RE_SHA.match(b or "")):
            return None
        try:
            p = self._git("diff", "--name-only", a, b)
        except (OSError, subprocess.SubprocessError):
            return None
        if p.returncode != 0:
            return None
        return [f for f in p.stdout.splitlines() if f]

    def subject(self, sha: str) -> str:
        if not _RE_SHA.match(sha or ""):
            return ""
        try:
            p = self._git("log", "-1", "--format=%s", sha)
            return p.stdout.strip() if p.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""


class Clock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


class Notifier:
    """Job summary, артефакт JSON, issue инцидента и комментарий в PR через `gh`."""

    def __init__(self, summary_path: Optional[str] = None, artifact_path: Optional[str] = None,
                 repo: Optional[str] = None, use_gh: bool = True, runner=None):
        self.summary_path = summary_path
        self.artifact_path = artifact_path
        self.repo = repo
        self.use_gh = use_gh and bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
        self.runner = runner or subprocess.run
        self.log: list[str] = []

    def summary(self, markdown: str) -> None:
        if self.summary_path:
            with open(self.summary_path, "a", encoding="utf-8") as fh:
                fh.write(markdown + "\n")
        print(markdown)

    def artifact(self, report: dict) -> None:
        if self.artifact_path:
            with open(self.artifact_path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)

    def _gh(self, args: list[str], body: Optional[str] = None):
        cmd = ["gh"] + args + (["--repo", self.repo] if self.repo else [])
        try:
            return self.runner(cmd, input=body, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log.append(f"gh {args[0]} failed: {type(exc).__name__}")
            return None

    def incident(self, title: str, body: str) -> None:
        if not self.use_gh:
            self.log.append("gh disabled: incident issue not created")
            return
        p = self._gh(["issue", "list", "--state", "open", "--search", f"{title} in:title",
                      "--json", "number,title", "--limit", "50"])
        number = None
        if p is not None and p.returncode == 0:
            try:
                for it in json.loads(p.stdout or "[]"):
                    if it.get("title") == title:
                        number = it.get("number")
                        break
            except ValueError:
                pass
        if number:
            r = self._gh(["issue", "comment", str(number), "--body-file", "-"], body)
            self.log.append(f"incident comment #{number}: {'ok' if r is not None and r.returncode == 0 else 'failed'}")
        else:
            r = self._gh(["issue", "create", "--title", title, "--body-file", "-"], body)
            self.log.append(f"incident issue created: {'ok' if r is not None and r.returncode == 0 else 'failed'}")

    def pr_comment(self, pr: int, body: str) -> None:
        if not self.use_gh:
            return
        r = self._gh(["pr", "comment", str(pr), "--body-file", "-"], body)
        self.log.append(f"pr #{pr} comment: {'ok' if r is not None and r.returncode == 0 else 'failed'}")


# ─── Контекст и наблюдения ───────────────────────────────────────────────────

@dataclass
class Context:
    app: str = DEFAULT_APP
    mode: str = "deploy"                 # deploy | monitor-only
    expected_sha: str = ""
    prev_image: str = ""
    prev_digest: str = ""
    prev_sha: str = ""
    prev_release: Optional[int] = None
    prev_healthy: bool = False
    deploy_outcome: str = "success"      # success | failure | skipped
    rollback_enabled: bool = False
    kill_switch: bool = False
    modules: list = field(default_factory=list)
    run_url: str = ""
    # заполняется по ходу
    new_digest: str = ""
    new_image: str = ""
    new_release: Optional[int] = None
    t0: Optional[float] = None


@dataclass
class Observation:
    reached_started: bool = False
    timed_out: bool = False
    superseded: bool = False
    ticks: int = 0
    expected_ticks: int = 0
    status_ok_ticks: int = 0
    fly_errors_total: int = 0
    fly_errors_max_consecutive: int = 0
    fly_unavailable: bool = False
    logs_ok_ticks: int = 0
    log_gaps: int = 0
    logs_start_missing: bool = False              # записи от T0 до первого опроса вытеснены
    checks_present_ticks: int = 0                 # опросы, где у всех машин есть health checks
    no_checks_ticks: int = 0
    restarts: int = 0
    exits: int = 0
    failing_check_ticks: int = 0
    max_consecutive_failing: int = 0
    last_tick_all_started: Optional[bool] = None
    fatal: dict = field(default_factory=dict)     # category -> count
    errors: dict = field(default_factory=dict)    # category -> count
    exc_classes: dict = field(default_factory=dict)
    module_check: str = "not_run"                 # ok | failed | inconclusive | unavailable | not_run
    module_check_exc: str = ""
    early_stop: str = ""
    unconfirmed_findings: int = 0                 # находки логов, не подтверждённые статусом
    deploy_not_applied: bool = False

    @property
    def coverage(self) -> float:
        return self.status_ok_ticks / self.expected_ticks if self.expected_ticks else 0.0

    @property
    def logs_coverage(self) -> float:
        return self.logs_ok_ticks / self.expected_ticks if self.expected_ticks else 0.0

    @property
    def checks_coverage(self) -> float:
        return self.checks_present_ticks / self.status_ok_ticks if self.status_ok_ticks else 0.0


class Monitor:
    def __init__(self, ctx: Context, fly, git, notifier, clock, cfg: Optional[Config] = None):
        self.ctx, self.fly, self.git, self.notifier, self.clock = ctx, fly, git, notifier, clock
        self.cfg = cfg or Config()
        self.obs = Observation()
        self.timeline: list[tuple[float, str]] = []
        self.rollback_attempted = False
        self.rollback: dict = {"considered": False, "allowed": False, "executed": False,
                               "reasons": [], "result": ""}
        self._seen_logs: set = set()
        self._log_start: Optional[float] = None          # нижняя граница окна логов
        self._pending: list = []                          # находки логов без подтверждения статусом
        self._last_log_ts: Optional[float] = None
        self._restart_events: set = set()
        self._exit_events: set = set()
        self._consec_fly_err = 0
        self._consec_failing = 0
        self._module_attempts = 0
        self.verdict_reasons: list[str] = []

    def event(self, text: str) -> None:
        self.timeline.append((self.clock.now(), text))

    # --- Fly status с учётом ошибок
    def _status(self) -> Optional[Snapshot]:
        try:
            snap = snapshot_from_status(self.fly.status())
        except FlyUnavailable:
            self._consec_fly_err += 1
            self.obs.fly_errors_total += 1
            self.obs.fly_errors_max_consecutive = max(self.obs.fly_errors_max_consecutive,
                                                      self._consec_fly_err)
            if self._consec_fly_err >= self.cfg.max_fly_errors_consecutive:
                self.obs.fly_unavailable = True
            return None
        self._consec_fly_err = 0
        return snap

    # --- Шаг 1: дождаться ожидаемой версии
    def wait_for_expected_version(self) -> bool:
        ctx, cfg = self.ctx, self.cfg
        deadline = self.clock.now() + cfg.wait_version_timeout
        self.event(f"ожидание версии {ctx.expected_sha[:7]} (до {int(cfg.wait_version_timeout)} с)")
        while True:
            snap = self._status()
            if snap is None and self.obs.fly_unavailable:
                self.event("Fly недоступен при ожидании версии")
                return False
            if snap is not None:
                apps = snap.app_machines
                if (ctx.deploy_outcome == "failure" and apps and ctx.prev_digest
                        and all(m.digest == ctx.prev_digest for m in apps)):
                    self.obs.deploy_not_applied = True
                    self.event("deploy упал, машины на предыдущем образе: production не изменён")
                    return False
                new = [m for m in apps if m.gh_sha == ctx.expected_sha]
                if new:
                    ctx.new_digest = new[0].digest
                    ctx.new_image = new[0].image
                    rels = [m.release for m in new if m.release is not None]
                    ctx.new_release = max(rels) if rels else None
                if apps and len(new) == len(apps) and all(m.state == "started" for m in apps):
                    starts = [m.last_start() for m in apps]
                    starts = [s for s in starts if s is not None]
                    ctx.t0 = max(starts) if starts else self.clock.now()
                    self.obs.reached_started = True
                    self.event(f"новая версия started (release {ctx.new_release}), T0={iso(ctx.t0)}")
                    return True
            if self.clock.now() >= deadline:
                self.obs.timed_out = True
                self.event("таймаут: новая версия не достигла started")
                return False
            self.clock.sleep(cfg.poll)

    # --- Шаг 2: наблюдение
    def _record_log(self, sev: str, cat: str, exc: Optional[str]) -> None:
        bucket = self.obs.fatal if sev == "FATAL" else self.obs.errors
        if cat not in bucket:
            self.event(f"лог {sev}: {cat}" + (f" ({exc})" if exc else ""))
        bucket[cat] = bucket.get(cat, 0) + 1
        if exc:
            self.obs.exc_classes[exc] = self.obs.exc_classes.get(exc, 0) + 1

    def _confirm_pending(self) -> None:
        """Статус подтвердил: машины всё ещё на наблюдаемом образе → находки засчитываются."""
        for item in self._pending:
            self._record_log(*item)
        self._pending = []

    def _process_logs(self, ids: set, confirmed: bool = True) -> None:
        """confirmed=False — статус на этом опросе не получен: находки откладываются до
        следующего успешного статуса (машину мог заменить другой релиз)."""
        window = self._log_start if self._log_start is not None else self.ctx.t0
        try:
            entries = self.fly.logs()
        except FlyUnavailable:
            return
        relevant_ts = []
        complete = True
        tss = [parse_ts(e.get("timestamp")) for e in entries]
        tss = [t for t in tss if t is not None]
        if self._last_log_ts is not None and tss and min(tss) > self._last_log_ts:
            self.obs.log_gaps += 1  # буфер переполнился между опросами — возможен пропуск
            complete = False
        elif (self._last_log_ts is None and tss and min(tss) > window
              and len(entries) >= LOG_BUFFER_FULL):
            # первый успешный опрос: буфер полон и начинается позже начала окна — начало потеряно
            self.obs.log_gaps += 1
            self.obs.logs_start_missing = True
            complete = False
            self.event("логи от начала окна до первого опроса вытеснены из буфера — данных нет")
        for e in entries:
            ts = parse_ts(e.get("timestamp"))
            inst = str(e.get("instance", ""))
            if ts is None or ts < window or inst not in ids:
                continue
            msg = str(e.get("message", ""))
            key = (e.get("timestamp"), inst, hashlib.sha256(msg.encode("utf-8", "replace")).hexdigest())
            if key in self._seen_logs:
                continue
            self._seen_logs.add(key)
            relevant_ts.append(ts)
            c = classify_message(msg)
            if not c:
                continue
            if confirmed:
                self._record_log(*c)
            else:
                self._pending.append(c)
        if tss:
            self._last_log_ts = max(tss + ([self._last_log_ts] if self._last_log_ts else []))
        if complete:
            self.obs.logs_ok_ticks += 1

    def _module_check(self, apps: list) -> None:
        if self.obs.module_check in ("ok", "failed") or not self.ctx.modules:
            return
        if self._module_attempts >= self.cfg.module_check_attempts:
            return
        self._module_attempts += 1
        try:
            for m in apps:
                ok, exc = self.fly.exec_check(m.id, list(self.ctx.modules))
                if not ok:
                    verdict = classify_module_check(exc)
                    self.obs.module_check, self.obs.module_check_exc = verdict, exc or ""
                    if verdict == "failed":
                        self.event(f"проверка импорта модулей: FAILED ({exc})")
                    else:
                        self.event(f"проверка импорта модулей: нет вывода ({exc or 'нет маркера'}) — UNKNOWN")
                    return
            self.obs.module_check = "ok"
            self.event("проверка импорта модулей: OK")
        except FlyUnavailable:
            self.obs.module_check = "unavailable"
            self.event("проверка импорта модулей: ssh недоступен")

    def observe(self) -> None:
        ctx, cfg, obs = self.ctx, self.cfg, self.obs
        start = self.clock.now()
        last_ids: set = set()
        end = max(ctx.t0 + cfg.observe, start + cfg.min_live_ticks * cfg.poll)
        if ctx.mode == "monitor-only":
            end = start + cfg.observe
        # окно логов: в deploy — от T0 (старт новой версии); в monitor-only версия могла
        # стартовать дни назад, буфер Fly (~100 записей) столько не хранит — окно от запуска
        self._log_start = ctx.t0 if ctx.mode == "deploy" else max(ctx.t0, start)
        obs.expected_ticks = max(1, int(math.ceil((end - start) / cfg.poll)))
        self.event(f"наблюдение до {iso(end)} ({obs.expected_ticks} опросов)")
        while self.clock.now() < end:
            obs.ticks += 1
            snap = self._status()
            if snap is None:
                if obs.fly_unavailable:
                    obs.early_stop = "fly_unavailable"
                    self.event(f"Fly недоступен {self._consec_fly_err} опроса подряд")
                    break
                if last_ids:
                    # статус не получен, но логи читаем по последним известным машинам;
                    # находки — только после подтверждения следующим успешным статусом
                    self._process_logs(last_ids, confirmed=False)
            else:
                obs.status_ok_ticks += 1
                apps = snap.app_machines
                if apps and all(m.digest == ctx.new_digest for m in apps):
                    self._confirm_pending()
                if not apps or any(m.digest != ctx.new_digest for m in apps):
                    if self._pending:
                        obs.unconfirmed_findings += len(self._pending)
                        self._pending = []  # логи могли принадлежать другому релизу
                    obs.superseded = True
                    obs.early_stop = "superseded"
                    self.event("машины ушли с наблюдаемого образа (новый релиз/откат) — наблюдение остановлено")
                    break
                for m in apps:
                    for (ty, st, t) in m.events:
                        if t is None or t <= ctx.t0:
                            continue
                        if ty == "start" and st == "started":
                            self._restart_events.add((m.id, t))
                        elif ty == "exit":
                            self._exit_events.add((m.id, t))
                obs.restarts = max(len(self._restart_events), len(self._exit_events))
                obs.exits = len(self._exit_events)
                if all(m.checks for m in apps):
                    obs.checks_present_ticks += 1
                else:
                    obs.no_checks_ticks += 1  # нет данных health check — не «passing»
                failing = any(m.checks_failing for m in apps)
                if failing:
                    obs.failing_check_ticks += 1
                    self._consec_failing += 1
                    obs.max_consecutive_failing = max(obs.max_consecutive_failing, self._consec_failing)
                else:
                    self._consec_failing = 0
                obs.last_tick_all_started = all(m.state == "started" for m in apps)
                last_ids = {m.id for m in apps}
                self._process_logs(last_ids)
                if self.clock.now() >= ctx.t0 + cfg.module_check_delay:
                    self._module_check(apps)
                if obs.fatal:
                    obs.early_stop = "fatal_log"
                elif obs.restarts >= cfg.restart_fail_threshold:
                    obs.early_stop = "restarts"
                elif obs.max_consecutive_failing >= cfg.fail_check_consecutive:
                    obs.early_stop = "health_checks"
                elif obs.module_check == "failed":
                    obs.early_stop = "module_check"
                if obs.early_stop:
                    self.event(f"критический сбой: {obs.early_stop} — наблюдение остановлено")
                    break
            self.clock.sleep(cfg.poll)
        if self._pending:
            obs.unconfirmed_findings += len(self._pending)
            self._pending = []
        # модульная проверка не успела (короткое окно) — одна попытка в конце
        if (not obs.early_stop and obs.module_check in ("not_run", "unavailable", "inconclusive")
                and not obs.fly_unavailable):
            snap = self._status()
            if snap is not None and snap.app_machines and all(
                    m.digest == ctx.new_digest for m in snap.app_machines):
                self._module_check(snap.app_machines)
                if obs.module_check == "failed":
                    obs.early_stop = "module_check"

    # --- Вердикт
    def verdict(self) -> str:
        o, cfg, r = self.obs, self.cfg, self.verdict_reasons
        if o.deploy_not_applied:
            r.append("deploy завершился ошибкой, production остался на предыдущем образе — откат не требуется")
            return FAILED
        if o.fly_unavailable and not o.reached_started:
            r.append("Fly.io недоступен — нет достоверных данных")
            return UNKNOWN
        if not o.reached_started:
            r.append("новая версия не достигла состояния started за отведённое время")
            return FAILED
        if o.fatal:
            r.append("критичные ошибки в логах после T0: " + ", ".join(sorted(o.fatal)))
            return FAILED
        if o.restarts >= cfg.restart_fail_threshold:
            r.append(f"рестартов после T0: {o.restarts}")
            return FAILED
        if o.last_tick_all_started is False:
            r.append("на последнем опросе машина не в состоянии started")
            return FAILED
        if o.max_consecutive_failing >= cfg.fail_check_consecutive:
            r.append(f"health check failing {o.max_consecutive_failing} опроса подряд")
            return FAILED
        if o.module_check == "failed":
            r.append(f"импорт модулей в production упал ({o.module_check_exc})")
            return FAILED
        unknown = []
        if o.superseded:
            unknown.append("наблюдение прервано: машины перешли на другой образ")
        if o.fly_unavailable:
            unknown.append("Fly.io недоступен ≥3 опроса подряд")
        if o.coverage < cfg.min_coverage:
            unknown.append(f"покрытие статусов {o.coverage:.0%} < {cfg.min_coverage:.0%}")
        if o.logs_start_missing:
            unknown.append("логи от T0 до первого опроса недоступны (буфер переполнен)")
        if o.checks_coverage < cfg.min_coverage:
            unknown.append(f"health checks присутствуют на {o.checks_coverage:.0%} опросов "
                           f"< {cfg.min_coverage:.0%} — нет данных о здоровье")
        if o.logs_coverage < cfg.min_coverage:
            unknown.append(f"покрытие логов {o.logs_coverage:.0%} < {cfg.min_coverage:.0%}")
        if o.unconfirmed_findings:
            unknown.append(f"находки в логах без подтверждения статусом Fly: {o.unconfirmed_findings}")
        if o.module_check != "ok":
            unknown.append(f"проверка импорта модулей: {o.module_check}")
        if unknown:
            r.extend(unknown)
            return UNKNOWN
        degraded = []
        if o.restarts == 1:
            degraded.append("1 рестарт после T0")
        if o.no_checks_ticks:
            degraded.append(f"health checks отсутствовали на {o.no_checks_ticks} опросах")
        if o.failing_check_ticks:
            degraded.append(f"health check failing на {o.failing_check_ticks} опросах")
        if o.errors:
            degraded.append("ошибки в логах: " + ", ".join(sorted(o.errors)))
        if degraded:
            r.extend(degraded)
            return DEGRADED
        r.append(f"стабильно {int(self.cfg.observe // 60)} мин от T0: started, checks passing, "
                 "без рестартов и критичных ошибок, модули импортируются")
        return HEALTHY

    # --- Откат
    def decide_rollback(self) -> tuple[bool, list[str]]:
        ctx = self.ctx
        reasons: list[str] = []
        ok = True

        def deny(msg):
            nonlocal ok
            ok = False
            reasons.append("НЕТ: " + msg)

        if self.rollback_attempted:
            deny("откат уже выполнялся в этом запуске")
            return False, reasons
        if ctx.mode == "monitor-only":
            deny("режим monitor-only — откат не выполняется")
        if not ctx.rollback_enabled:
            deny("AUTO_ROLLBACK_ENABLED выключен")
        if ctx.kill_switch:
            deny("kill-switch AUTO_ROLLBACK_DISABLED включён")
        if not (ctx.prev_healthy and ctx.prev_image and ctx.prev_digest and ctx.prev_sha
                and ctx.prev_release is not None):
            deny("предыдущая версия не определена однозначно")
        if not ctx.new_digest:
            deny("новая версия не обнаружена на машинах — откатывать нечего")
        elif ctx.new_release is None:
            deny("номер релиза новой версии неизвестен — защита от гонок невозможна")
        if ctx.prev_digest and ctx.new_digest and ctx.prev_digest == ctx.new_digest:
            deny("предыдущий и новый образ совпадают")
        if ctx.prev_image and not image_ref_ok(ctx.prev_image, ctx.app):
            deny("ссылка на предыдущий образ не прошла проверку")
        if not ok:
            return False, reasons
        try:
            rels = self.fly.releases()
            prev = [r for r in rels if r.get("Version") == ctx.prev_release]
            if len(prev) != 1 or prev[0].get("Status") != "complete":
                deny(f"релиз v{ctx.prev_release} не в статусе complete")
            else:
                reasons.append(f"да: релиз v{ctx.prev_release} complete")
        except FlyUnavailable:
            deny("не удалось получить список релизов")
        if not self.git.has_commit(ctx.prev_sha):
            deny("коммит предыдущей версии отсутствует в истории")
        else:
            changed = self.git.changed_files(ctx.prev_sha, ctx.expected_sha)
            if changed is None:
                deny("не удалось получить diff между версиями")
            else:
                hit = sorted(set(changed) & set(ROLLBACK_PROTECTED_FILES))
                if hit:
                    deny("между версиями изменены " + ", ".join(hit) +
                         " — откат может быть несовместим с данными/конфигурацией")
                else:
                    reasons.append("да: db.py, crypto.py, fly.toml не менялись")
        if not ok:
            return False, reasons
        snap = self._status()
        if snap is None:
            deny("свежий статус Fly недоступен")
            return False, reasons
        apps = snap.app_machines
        if not apps or not all(m.digest == ctx.new_digest for m in apps):
            deny("Fly уже не на наблюдаемом образе (откат выполнен или вышел другой релиз)")
        if (snap.version is not None and ctx.new_release is not None
                and snap.version > ctx.new_release):
            deny(f"на Fly более новый релиз v{snap.version} > v{ctx.new_release}")
        if ok:
            reasons.append("да: Fly всё ещё на новой версии, более новых релизов нет")
        return ok, reasons

    def verify_restored(self) -> bool:
        ctx, cfg = self.ctx, self.cfg
        start = self.clock.now()
        deadline = start + cfg.rollback_verify
        restored_seen = False
        last_ok = False
        restarts: set = set()
        consec_err = 0
        while True:
            try:
                snap = snapshot_from_status(self.fly.status())
                consec_err = 0
                apps = snap.app_machines
                on_prev = bool(apps) and all(m.gh_sha == ctx.prev_sha and m.digest == ctx.prev_digest
                                             for m in apps)
                started = on_prev and all(m.state == "started" for m in apps)
                if started and not restored_seen:
                    restored_seen = True
                    self.event("восстановленная версия started")
                if on_prev:
                    for m in apps:
                        for (ty, st, t) in m.events:
                            if ty == "start" and st == "started" and t and t > start:
                                restarts.add((m.id, t))
                last_ok = (started and all(m.checks for m in apps)
                           and not any(m.checks_failing for m in apps))
            except FlyUnavailable:
                consec_err += 1
                last_ok = False
                if consec_err >= cfg.max_fly_errors_consecutive:
                    self.event("Fly недоступен при проверке восстановленной версии")
                    return False
            if self.clock.now() >= deadline:
                break
            self.clock.sleep(cfg.poll)
        # один старт после отката ожидаем (сама замена образа); больше — нестабильно
        return restored_seen and last_ok and len(restarts) <= 1

    # --- Основной сценарий
    def run(self) -> dict:
        ctx = self.ctx
        if ctx.mode == "monitor-only":
            snap = self._status() or self._status() or self._status()
            shas = {m.gh_sha for m in snap.app_machines} if snap else set()
            if snap is None or len(shas) != 1 or not next(iter(shas)):
                self.verdict_reasons.append("monitor-only: не удалось однозначно определить текущую версию")
                return self.finish(UNKNOWN)
            ctx.expected_sha = next(iter(shas))
            self.event(f"monitor-only: текущая версия {ctx.expected_sha[:7]}")
        if not self.wait_for_expected_version():
            status = self.verdict()
            if status == FAILED and not self.obs.deploy_not_applied:
                status = self._handle_failed()
            return self.finish(status)
        self.observe()
        status = self.verdict()
        if status == FAILED:
            status = self._handle_failed()
        return self.finish(status)

    def _handle_failed(self) -> str:
        self.rollback["considered"] = True
        allowed, reasons = self.decide_rollback()
        self.rollback["allowed"], self.rollback["reasons"] = allowed, reasons
        if not allowed:
            self.event("автооткат не разрешён — требуется решение Product Owner")
            self.rollback["result"] = "not_allowed"
            return FAILED
        self.rollback_attempted = True
        self.rollback["executed"] = True
        self.event(f"откат: deploy предыдущего образа (release v{self.ctx.prev_release})")
        if not self.fly.deploy_image(self.ctx.prev_image):
            self.event("откат завершился ошибкой — дальнейшие автодействия остановлены")
            self.rollback["result"] = "deploy_failed"
            return FAILED
        if self.verify_restored():
            self.event("восстановленная версия подтверждена")
            self.rollback["result"] = "restored"
            return ROLLED_BACK
        self.event("восстановленная версия не подтверждена — дальнейшие автодействия остановлены")
        self.rollback["result"] = "restore_not_confirmed"
        return FAILED

    # --- Отчёт
    def build_report(self, status: str) -> dict:
        ctx, o = self.ctx, self.obs
        return {
            "status": status,
            "app": ctx.app,
            "mode": ctx.mode,
            "deploy_outcome": ctx.deploy_outcome,
            "new": {"sha": ctx.expected_sha, "release": ctx.new_release,
                    "digest": ctx.new_digest[:19] if ctx.new_digest else ""},
            "previous": {"sha": ctx.prev_sha, "release": ctx.prev_release,
                         "digest": ctx.prev_digest[:19] if ctx.prev_digest else "",
                         "healthy": ctx.prev_healthy},
            "t0": iso(ctx.t0),
            "reasons": list(self.verdict_reasons),
            "observation": {
                "reached_started": o.reached_started, "ticks": o.ticks,
                "expected_ticks": o.expected_ticks, "status_coverage": round(o.coverage, 3),
                "logs_coverage": round(o.logs_coverage, 3), "log_gaps": o.log_gaps,
                "logs_start_missing": o.logs_start_missing,
                "checks_coverage": round(o.checks_coverage, 3), "no_checks_ticks": o.no_checks_ticks,
                "fly_errors_total": o.fly_errors_total,
                "fly_errors_max_consecutive": o.fly_errors_max_consecutive,
                "restarts_after_t0": o.restarts, "exits_after_t0": o.exits,
                "failing_check_ticks": o.failing_check_ticks,
                "max_consecutive_failing_checks": o.max_consecutive_failing,
                "fatal_categories": dict(o.fatal), "error_categories": dict(o.errors),
                "exception_classes": dict(o.exc_classes),
                "module_check": o.module_check, "module_check_exception": o.module_check_exc,
                "early_stop": o.early_stop, "superseded": o.superseded,
                "unconfirmed_findings": o.unconfirmed_findings,
            },
            "rollback": dict(self.rollback),
            "timeline": [{"t": iso(t), "event": e} for t, e in self.timeline],
            "run_url": ctx.run_url,
        }

    def finish(self, status: str) -> dict:
        report = self.build_report(status)
        md = render_markdown(report)
        self.notifier.summary(md)
        self.notifier.artifact(report)
        short = (self.ctx.expected_sha or "unknown")[:7]
        if status != HEALTHY:
            self.notifier.incident(f"Deploy incident: {status} {short}", md)
        pr = None
        if self.ctx.mode == "deploy" and self.ctx.expected_sha:
            pr = find_pr_number(self.git.subject(self.ctx.expected_sha))
        if pr:
            self.notifier.pr_comment(pr, md)
        return report


def find_pr_number(subject: str) -> Optional[int]:
    found = re.findall(r"\(#(\d+)\)", subject or "")
    return int(found[-1]) if found else None


def render_markdown(r: dict) -> str:
    o, rb = r["observation"], r["rollback"]
    lines = [
        f"## Deploy Monitor: **{r['status']}**",
        "",
        f"- Приложение: `{r['app']}`, режим: `{r['mode']}`, шаг deploy: `{r['deploy_outcome']}`",
        f"- Новая версия: `{(r['new']['sha'] or '-')[:12]}` release `v{r['new']['release']}`",
        f"- Предыдущая версия: `{(r['previous']['sha'] or '-')[:12]}` release "
        f"`v{r['previous']['release']}` (однозначна: {'да' if r['previous']['healthy'] else 'нет'})",
        f"- T0 (старт новой версии): {r['t0']}",
        "",
        "### Основание",
    ] + [f"- {x}" for x in r["reasons"]] + [
        "",
        "### Наблюдение",
        f"- Опросов: {o['ticks']}/{o['expected_ticks']}, покрытие статусов {o['status_coverage']:.0%}, "
        f"логов {o['logs_coverage']:.0%}, health checks {o['checks_coverage']:.0%}, "
        f"ошибок Fly {o['fly_errors_total']}",
        f"- Рестартов после T0: {o['restarts_after_t0']}; failing checks: {o['failing_check_ticks']} опросов "
        f"(подряд максимум {o['max_consecutive_failing_checks']})",
        f"- FATAL: {o['fatal_categories'] or 'нет'}; ERROR: {o['error_categories'] or 'нет'}; "
        f"классы исключений: {o['exception_classes'] or 'нет'}",
        f"- Импорт модулей в production: {o['module_check']}"
        + (f" ({o['module_check_exception']})" if o["module_check_exception"] else ""),
        "",
        "### Откат",
        f"- Рассматривался: {'да' if rb['considered'] else 'нет'}; разрешён: "
        f"{'да' if rb['allowed'] else 'нет'}; выполнен: {'да' if rb['executed'] else 'нет'}"
        + (f"; результат: `{rb['result']}`" if rb["result"] else ""),
    ] + [f"  - {x}" for x in rb["reasons"]] + [
        "",
        "### Таймлайн",
    ] + [f"- {x['t']} — {x['event']}" for x in r["timeline"]] + [
        "",
        f"Run: {r['run_url'] or '-'}",
        "",
        "_Текст логов приложения не публикуется (публичный репозиторий): только категории и счётчики._",
    ]
    return "\n".join(lines)


# ─── capture-prev ────────────────────────────────────────────────────────────

def capture_prev(fly) -> dict:
    out = {"prev_image": "", "prev_digest": "", "prev_sha": "", "prev_release": "",
           "prev_healthy": "false", "prev_reason": ""}
    try:
        snap = snapshot_from_status(fly.status())
    except FlyUnavailable:
        out["prev_reason"] = "fly_unavailable"
        return out
    apps = snap.app_machines
    if len(apps) != 1:
        out["prev_reason"] = f"app_machines={len(apps)}"
        return out
    m = apps[0]
    out.update(prev_image=m.image, prev_digest=m.digest, prev_sha=m.gh_sha,
               prev_release=str(m.release or ""))
    if m.state != "started":
        out["prev_reason"] = "not_started"
    elif not m.checks or m.checks_failing:
        out["prev_reason"] = "checks_not_passing"
    elif not _RE_SHA.match(m.gh_sha or ""):
        out["prev_reason"] = "no_gh_sha"
    elif not m.digest or not m.release:
        out["prev_reason"] = "no_digest_or_release"
    else:
        try:
            rels = [r for r in fly.releases() if r.get("Version") == m.release]
            if len(rels) == 1 and rels[0].get("Status") == "complete":
                out["prev_healthy"] = "true"
            else:
                out["prev_reason"] = "release_not_complete"
        except FlyUnavailable:
            out["prev_reason"] = "releases_unavailable"
    return out


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _default_modules() -> list[str]:
    try:
        import check_docker_packaging as cdp
        res = cdp.check(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        return list(res.modules) if res.ok else []
    except Exception:  # noqa: BLE001 — отсутствие списка модулей → проверка импорта UNKNOWN
        return []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deploy Monitor для Fly.io")
    sub = ap.add_subparsers(dest="cmd", required=True)
    cp = sub.add_parser("capture-prev")
    cp.add_argument("--app", default=os.environ.get("FLY_APP", DEFAULT_APP))
    cp.add_argument("--output", default=os.environ.get("GITHUB_OUTPUT", ""))
    sub.add_parser("module-check-command",
                   help="вывести фиксированную команду проверки импорта (для docker smoke в CI)")
    rp = sub.add_parser("run")
    rp.add_argument("--app", default=os.environ.get("FLY_APP", DEFAULT_APP))
    rp.add_argument("--mode", choices=["deploy", "monitor-only"], default="deploy")
    rp.add_argument("--expected-sha", default="")
    rp.add_argument("--prev-image", default="")
    rp.add_argument("--prev-digest", default="")
    rp.add_argument("--prev-sha", default="")
    rp.add_argument("--prev-release", default="")
    rp.add_argument("--prev-healthy", default="false")
    rp.add_argument("--deploy-outcome", default="success")
    rp.add_argument("--report-json", default="deploy-monitor-report.json")
    rp.add_argument("--run-url", default="")
    rp.add_argument("--modules", default="", help="имена модулей через пробел (по умолчанию — из checker)")
    args = ap.parse_args(argv)

    if args.cmd == "module-check-command":
        mods = _default_modules()
        if not mods:
            print("ERROR: module list unavailable (packaging check failed)", file=sys.stderr)
            return 1
        print(build_module_check_command(mods))
        return 0

    if args.cmd == "capture-prev":
        out = capture_prev(FlyCtl(args.app))
        for k, v in out.items():
            print(f"{k}={v}")
        if args.output:
            with open(args.output, "a", encoding="utf-8") as fh:
                for k, v in out.items():
                    fh.write(f"{k}={v}\n")
        return 0

    try:
        prev_release = int(args.prev_release) if args.prev_release else None
    except ValueError:
        prev_release = None
    ctx = Context(
        app=args.app, mode=args.mode, expected_sha=args.expected_sha.strip(),
        prev_image=args.prev_image.strip(), prev_digest=args.prev_digest.strip(),
        prev_sha=args.prev_sha.strip(), prev_release=prev_release,
        prev_healthy=args.prev_healthy.strip().lower() == "true",
        deploy_outcome=args.deploy_outcome.strip() or "success",
        rollback_enabled=os.environ.get("AUTO_ROLLBACK_ENABLED", "").lower() == "true",
        kill_switch=os.environ.get("AUTO_ROLLBACK_DISABLED", "").lower() == "true",
        modules=args.modules.split() if args.modules else _default_modules(),
        run_url=args.run_url,
    )
    notifier = Notifier(summary_path=os.environ.get("GITHUB_STEP_SUMMARY") or None,
                        artifact_path=args.report_json,
                        repo=os.environ.get("GITHUB_REPOSITORY") or None)
    repo_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    mon = Monitor(ctx, FlyCtl(args.app), GitClient(repo_root), notifier, Clock())
    report = mon.run()
    for line in notifier.log:
        print(f"notifier: {line}")
    return 0 if report["status"] == HEALTHY else 1


if __name__ == "__main__":
    sys.exit(main())
