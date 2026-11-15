"""
Тесты ci/deploy_monitor.py — Deploy Monitor и безопасный rollback.
Fly.io, git, GitHub и часы — заглушки: ни сети, ни subprocess к flyctl,
ни реальных деплоев/откатов. Время виртуальное (10 минут наблюдения — мгновенно).
Запуск: python test_deploy_monitor.py  (также входит в python test_smoke.py)
"""
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

REPO_ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(REPO_ROOT / "ci"))
import deploy_monitor as dm  # noqa: E402

APP = "calendar-bot-anatolii"
T_START = 1_800_000_000.0
PREV_SHA, NEW_SHA, NEWER_SHA = "a" * 40, "b" * 40, "c" * 40
PREV_DIG, NEW_DIG, NEWER_DIG = ("sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64)
PREV_IMG = f"registry.fly.io/{APP}:deployment-PREV01"
NEW_IMG = f"registry.fly.io/{APP}:deployment-NEW001"
NEWER_IMG = f"registry.fly.io/{APP}:deployment-NEWER1"
MID, MID2 = "8151d6b9732418", "9a2b3c4d5e6f70"
T0 = T_START - 20  # новая версия стартовала за 20 с до запуска монитора


# ─── Заглушки ────────────────────────────────────────────────────────────────

class FakeClock:
    def __init__(self, t=T_START):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


def machine(sha, digest, image, release, state="started", checks="passing",
            starts=(), exits=(), mid=MID, group="app"):
    ev = [{"type": "start", "status": "started", "source": "flyd", "timestamp": int(t * 1000)}
          for t in starts]
    ev += [{"type": "exit", "status": "stopped", "source": "flyd", "timestamp": int(t * 1000)}
           for t in exits]
    return {
        "id": mid, "state": state, "instance_id": "01ABC",
        "image_ref": {"registry": "registry.fly.io", "repository": APP, "tag": "x",
                      "digest": digest, "labels": {"GH_SHA": sha, "GH_REPO": "o/r"}},
        "config": {"image": image, "metadata": {"fly_process_group": group,
                                                 "fly_release_version": str(release),
                                                 "fly_release_id": "rel_x"},
                   "mounts": [{"path": "/data", "name": "bot_data"}]},
        "events": ev,
        "checks": [{"name": "servicecheck-00-http-8080", "status": checks, "output": "OK"}]
        if checks else [],
    }


def status(version, *machines):
    return {"Version": version, "Status": "deployed", "Machines": list(machines)}


def new_ok(now, **kw):
    return status(100, machine(NEW_SHA, NEW_DIG, NEW_IMG, 100, starts=[T0], **kw))


def restored_ok(now, rolled_at):
    return status(101, machine(PREV_SHA, PREV_DIG, PREV_IMG, 101, starts=[T0, rolled_at + 30]))


def rfc(ts):
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".%09dZ" % int(round((ts - int(ts)) * 1e9))


def log_entry(ts, msg, inst=MID):
    return {"level": "info", "instance": inst, "message": msg, "region": "arn",
            "timestamp": rfc(ts), "meta": {"Instance": inst, "Region": "arn"}}


RELEASES = [
    {"ID": "r100", "Version": 100, "Stable": False, "Status": "complete", "ImageRef": NEW_IMG,
     "User": {"Email": "owner@example.com", "Name": "Owner"}},
    {"ID": "r99", "Version": 99, "Stable": False, "Status": "complete", "ImageRef": PREV_IMG,
     "User": {"Email": "owner@example.com", "Name": "Owner"}},
]


class FakeFly:
    def __init__(self, clock, status_fn=new_ok, logs=(), exec_result=(True, ""),
                 releases=RELEASES, rollback_ok=True, after_rollback=restored_ok):
        self.clock, self.status_fn, self.log_entries = clock, status_fn, list(logs)
        self.exec_result, self._releases = exec_result, releases
        self.rollback_ok, self.after_rollback = rollback_ok, after_rollback
        self.rolled_at = None
        self.deploy_calls, self.exec_calls, self.status_calls = [], [], 0

    def status(self):
        self.status_calls += 1
        now = self.clock.now()
        r = (self.after_rollback(now, self.rolled_at) if self.rolled_at is not None
             else self.status_fn(now))
        if isinstance(r, Exception):
            raise r
        return r

    def releases(self):
        if isinstance(self._releases, Exception):
            raise self._releases
        return self._releases

    def logs(self):
        now = self.clock.now()
        visible = [e for e in self.log_entries if dm.parse_ts(e["timestamp"]) <= now]
        return visible[-100:]

    def exec_check(self, mid, modules):
        self.exec_calls.append((mid, tuple(modules)))
        if isinstance(self.exec_result, Exception):
            raise self.exec_result
        return self.exec_result

    def deploy_image(self, ref):
        self.deploy_calls.append(ref)
        if self.rollback_ok:
            self.rolled_at = self.clock.now()
        self.clock.sleep(60)
        return self.rollback_ok


class FakeGit:
    def __init__(self, changed=("main.py",), has=True, subject="feat: something (#42)"):
        self.changed, self.has, self._subject = changed, has, subject

    def has_commit(self, sha):
        return self.has

    def changed_files(self, a, b):
        return None if self.changed is None else list(self.changed)

    def subject(self, sha):
        return self._subject


class FakeNotifier:
    def __init__(self):
        self.summaries, self.artifacts, self.incidents, self.pr_comments = [], [], [], []

    def summary(self, md):
        self.summaries.append(md)

    def artifact(self, rep):
        self.artifacts.append(rep)

    def incident(self, title, body):
        self.incidents.append((title, body))

    def pr_comment(self, pr, body):
        self.pr_comments.append((pr, body))


def ctx(**kw):
    base = dict(app=APP, mode="deploy", expected_sha=NEW_SHA, prev_image=PREV_IMG,
                prev_digest=PREV_DIG, prev_sha=PREV_SHA, prev_release=99, prev_healthy=True,
                deploy_outcome="success", rollback_enabled=True, kill_switch=False,
                modules=["db", "my_tasks", "main"], run_url="https://example.invalid/run/1")
    base.update(kw)
    return dm.Context(**base)


def run(c, fly, git=None, cfg=None):
    n = FakeNotifier()
    mon = dm.Monitor(c, fly, git or FakeGit(), n, fly.clock, cfg or dm.Config())
    rep = mon.run()
    return rep, mon, n


# ─── Сценарии issue #29 (4–14) ───────────────────────────────────────────────

def test_dm_healthy_10_minutes():
    """4. Деплой успешен, стабильно 10 минут от T0 → HEALTHY, без отката, отчёт и PR-комментарий."""
    clock = FakeClock()
    logs = [log_entry(T0 + 5 + i * 20, "[INFO] apscheduler: job ok") for i in range(40)]
    fly = FakeFly(clock, logs=logs)
    rep, mon, n = run(ctx(), fly)
    assert rep["status"] == dm.HEALTHY, rep["reasons"]
    assert clock.now() >= T0 + 600, "наблюдение должно длиться 10 минут от T0"
    assert rep["t0"] == dm.iso(T0)
    assert rep["observation"]["module_check"] == "ok" and fly.exec_calls
    assert not fly.deploy_calls and not n.incidents
    assert n.pr_comments and n.pr_comments[0][0] == 42
    assert n.artifacts and n.artifacts[0]["status"] == dm.HEALTHY


def test_dm_machine_never_starts_failed():
    """5. Машина не запускается → FAILED (таймаут), при выключенном откате — без deploy."""
    clock = FakeClock()
    fly = FakeFly(clock, status_fn=lambda now: status(
        100, machine(NEW_SHA, NEW_DIG, NEW_IMG, 100, state="stopped", checks="critical")))
    rep, mon, n = run(ctx(rollback_enabled=False), fly)
    assert rep["status"] == dm.FAILED, rep
    assert mon.obs.timed_out and not mon.obs.reached_started
    assert not fly.deploy_calls
    assert any("AUTO_ROLLBACK_ENABLED" in r for r in rep["rollback"]["reasons"])
    assert n.incidents and n.incidents[0][0] == f"Deploy incident: FAILED {NEW_SHA[:7]}"
    # с разрешённым откатом — откат и проверка восстановленной версии
    clock2 = FakeClock()
    fly2 = FakeFly(clock2, status_fn=fly.status_fn)
    rep2, _, _ = run(ctx(), fly2)
    assert rep2["status"] == dm.ROLLED_BACK and fly2.deploy_calls == [PREV_IMG], rep2


def test_dm_restart_loop_failed():
    """6. Постоянные рестарты после T0 → FAILED, наблюдение остановлено досрочно."""
    clock = FakeClock()

    def st(now):
        starts = [T0] + [t for t in (T0 + 70, T0 + 130, T0 + 190) if t <= now]
        exits = [t - 5 for t in starts[1:]]
        return status(100, machine(NEW_SHA, NEW_DIG, NEW_IMG, 100, starts=starts, exits=exits))
    fly = FakeFly(clock, status_fn=st)
    rep, mon, _ = run(ctx(rollback_enabled=False), fly)
    assert rep["status"] == dm.FAILED, rep
    assert mon.obs.restarts >= 2 and mon.obs.early_stop == "restarts"
    # один рестарт — не FAILED и не HEALTHY
    clock2 = FakeClock()
    fly2 = FakeFly(clock2, status_fn=lambda now: status(100, machine(
        NEW_SHA, NEW_DIG, NEW_IMG, 100, starts=[T0] + ([T0 + 100] if now >= T0 + 100 else []))))
    rep2, _, _ = run(ctx(), fly2)
    assert rep2["status"] == dm.DEGRADED, rep2["reasons"]


def test_dm_intermittent_health_checks_not_healthy():
    """7. Health check периодически падает → DEGRADED (не ложный HEALTHY); 3 подряд → FAILED."""
    clock = FakeClock()
    fly = FakeFly(clock, status_fn=lambda now: new_ok(
        now, checks="critical" if int((now - T_START) / 30) % 2 else "passing"))
    rep, mon, _ = run(ctx(), fly)
    assert rep["status"] == dm.DEGRADED, rep["reasons"]
    assert mon.obs.failing_check_ticks > 0 and mon.obs.max_consecutive_failing < 3
    assert not fly.deploy_calls
    clock2 = FakeClock()
    fly2 = FakeFly(clock2, status_fn=lambda now: new_ok(
        now, checks="critical" if T_START + 60 <= now <= T_START + 200 else "passing"))
    rep2, mon2, _ = run(ctx(rollback_enabled=False), fly2)
    assert rep2["status"] == dm.FAILED and mon2.obs.early_stop == "health_checks", rep2


def test_dm_fatal_log_after_t0_rollback_before_t0_ignored():
    """8. ModuleNotFoundError после T0 → FAILED → откат → ROLLED_BACK; до T0 — игнорируется."""
    clock = FakeClock()
    logs = [log_entry(T0 + 40, "Traceback (most recent call last):"),
            log_entry(T0 + 41, "ModuleNotFoundError: No module named 'my_tasks'")]
    fly = FakeFly(clock, logs=logs)
    rep, mon, n = run(ctx(), fly)
    assert "ModuleNotFoundError" in rep["observation"]["fatal_categories"], rep
    assert fly.deploy_calls == [PREV_IMG], fly.deploy_calls
    assert rep["status"] == dm.ROLLED_BACK and rep["rollback"]["result"] == "restored"
    assert n.incidents and "ROLLED_BACK" in n.incidents[0][0]
    # те же ошибки, но от старой версии (до T0) — не влияют
    clock2 = FakeClock()
    old = [log_entry(T0 - 300, "ModuleNotFoundError: No module named 'my_tasks'"),
           log_entry(T0 - 1, "[ERROR] old version failure")]
    fly2 = FakeFly(clock2, logs=old)
    rep2, _, _ = run(ctx(), fly2)
    assert rep2["status"] == dm.HEALTHY, rep2["reasons"]
    assert not fly2.deploy_calls
    # [ERROR] без FATAL после T0 → DEGRADED, без отката
    clock3 = FakeClock()
    fly3 = FakeFly(clock3, logs=[log_entry(T0 + 50, "2026 [ERROR] main: something")])
    rep3, _, _ = run(ctx(), fly3)
    assert rep3["status"] == dm.DEGRADED and not fly3.deploy_calls, rep3


def test_dm_prev_unknown_rollback_forbidden():
    """9. Предыдущая версия неизвестна → автооткат запрещён, FAILED."""
    for kw in (dict(prev_healthy=False), dict(prev_image=""), dict(prev_sha=""),
               dict(prev_release=None), dict(prev_digest=NEW_DIG)):
        clock = FakeClock()
        fly = FakeFly(clock, logs=[log_entry(T0 + 30, "ImportError: cannot import name x")])
        rep, _, _ = run(ctx(**kw), fly)
        assert rep["status"] == dm.FAILED, (kw, rep["status"])
        assert not fly.deploy_calls, kw
        assert rep["rollback"]["considered"] and not rep["rollback"]["allowed"]
    # релиз предыдущей версии не complete → запрещено
    clock = FakeClock()
    rels = [dict(RELEASES[0]), dict(RELEASES[1], Status="failed")]
    fly = FakeFly(clock, releases=rels, logs=[log_entry(T0 + 30, "SyntaxError: bad")])
    rep, _, _ = run(ctx(), fly)
    assert rep["status"] == dm.FAILED and not fly.deploy_calls


def test_dm_db_migration_rollback_forbidden():
    """10. db.py/crypto.py/fly.toml изменены между версиями → откат запрещён."""
    for changed in (["main.py", "db.py"], ["crypto.py"], ["fly.toml"]):
        clock = FakeClock()
        fly = FakeFly(clock, logs=[log_entry(T0 + 30, "ModuleNotFoundError: x")])
        rep, _, _ = run(ctx(), fly, git=FakeGit(changed=changed))
        assert rep["status"] == dm.FAILED and not fly.deploy_calls, changed
        assert any(changed[-1] in r for r in rep["rollback"]["reasons"]), rep["rollback"]
    # коммита нет в истории / diff недоступен → запрещено
    for git in (FakeGit(has=False), FakeGit(changed=None)):
        clock = FakeClock()
        fly = FakeFly(clock, logs=[log_entry(T0 + 30, "ModuleNotFoundError: x")])
        rep, _, _ = run(ctx(), fly, git=git)
        assert rep["status"] == dm.FAILED and not fly.deploy_calls


def test_dm_rollback_verified_and_restore_failure():
    """11. Откат выполнен → проверяется восстановленная версия; не подтверждена/упал → FAILED."""
    fatal = [log_entry(T0 + 30, "ModuleNotFoundError: x")]
    clock = FakeClock()
    fly = FakeFly(clock, logs=fatal)
    rep, mon, _ = run(ctx(), fly)
    assert rep["status"] == dm.ROLLED_BACK
    assert any("восстановленная версия подтверждена" in e["event"] for e in rep["timeline"])
    # восстановленная машина не стартует
    clock2 = FakeClock()
    fly2 = FakeFly(clock2, logs=fatal, after_rollback=lambda now, ra: status(
        101, machine(PREV_SHA, PREV_DIG, PREV_IMG, 101, state="stopped", checks="critical")))
    rep2, _, _ = run(ctx(), fly2)
    assert rep2["status"] == dm.FAILED and rep2["rollback"]["result"] == "restore_not_confirmed"
    assert len(fly2.deploy_calls) == 1, "без повторных попыток"
    # Fly остался на новой версии после «успешного» отката
    clock3 = FakeClock()
    fly3 = FakeFly(clock3, logs=fatal, after_rollback=lambda now, ra: new_ok(now))
    rep3, _, _ = run(ctx(), fly3)
    assert rep3["status"] == dm.FAILED and len(fly3.deploy_calls) == 1
    # сам deploy отката упал
    clock4 = FakeClock()
    fly4 = FakeFly(clock4, logs=fatal, rollback_ok=False)
    rep4, _, _ = run(ctx(), fly4)
    assert rep4["status"] == dm.FAILED and rep4["rollback"]["result"] == "deploy_failed"
    assert len(fly4.deploy_calls) == 1


def test_dm_fly_unavailable_unknown():
    """12. Потерян доступ к Fly.io → UNKNOWN, не успех; ssh недоступен → UNKNOWN."""
    clock = FakeClock()
    fly = FakeFly(clock, status_fn=lambda now: dm.FlyUnavailable("x") if now >= T_START + 120
                  else new_ok(now))
    rep, mon, n = run(ctx(), fly)
    assert rep["status"] == dm.UNKNOWN, rep
    assert mon.obs.fly_unavailable and not fly.deploy_calls
    assert n.incidents and "UNKNOWN" in n.incidents[0][0]
    # недоступен с самого начала
    clock2 = FakeClock()
    fly2 = FakeFly(clock2, status_fn=lambda now: dm.FlyUnavailable("x"))
    rep2, _, _ = run(ctx(), fly2)
    assert rep2["status"] == dm.UNKNOWN and not fly2.deploy_calls
    # отдельные сбои, покрытие < 90% → UNKNOWN
    clock3 = FakeClock()
    fly3 = FakeFly(clock3, status_fn=lambda now: dm.FlyUnavailable("x")
                   if int((now - T_START) / 30) % 4 == 1 else new_ok(now))
    rep3, _, _ = run(ctx(), fly3)
    assert rep3["status"] == dm.UNKNOWN, rep3["reasons"]
    # ssh недоступен → проверка модулей UNKNOWN
    clock4 = FakeClock()
    fly4 = FakeFly(clock4, exec_result=dm.FlyUnavailable("ssh"))
    rep4, _, _ = run(ctx(), fly4)
    assert rep4["status"] == dm.UNKNOWN and rep4["observation"]["module_check"] == "unavailable"


def test_dm_repeated_run_no_double_rollback():
    """13. Повторный запуск: Fly уже на prev или на более новом релизе → deploy_image не вызывается."""
    # повтор после успешного отката: машины на предыдущем образе
    clock = FakeClock()
    fly = FakeFly(clock, status_fn=lambda now: restored_ok(now, T_START - 500))
    rep, _, _ = run(ctx(), fly)
    assert not fly.deploy_calls and rep["status"] != dm.HEALTHY
    # вышел более новый релиз
    clock2 = FakeClock()
    fly2 = FakeFly(clock2, status_fn=lambda now: status(
        102, machine(NEWER_SHA, NEWER_DIG, NEWER_IMG, 102, starts=[T0])))
    rep2, _, _ = run(ctx(), fly2)
    assert not fly2.deploy_calls and rep2["status"] != dm.HEALTHY
    # Fly на новом образе, но версия приложения уже больше (была попытка отката) → запрещено
    clock3 = FakeClock()
    fly3 = FakeFly(clock3, logs=[log_entry(T0 + 30, "ModuleNotFoundError: x")])
    c3 = ctx()
    mon = dm.Monitor(c3, fly3, FakeGit(), FakeNotifier(), clock3)
    assert mon.wait_for_expected_version()
    fly3.status_fn = lambda now: status(101, machine(NEW_SHA, NEW_DIG, NEW_IMG, 100, starts=[T0]))
    allowed, reasons = mon.decide_rollback()
    assert not allowed and any("более новый релиз" in r for r in reasons), reasons
    # к моменту решения машина уже на предыдущем образе (откат сделан параллельно) → запрещено
    fly3.status_fn = lambda now: status(100, machine(PREV_SHA, PREV_DIG, PREV_IMG, 100, starts=[T0]))
    allowed, reasons = mon.decide_rollback()
    assert not allowed and any("не на наблюдаемом образе" in r for r in reasons), reasons
    assert not fly3.deploy_calls
    # в одном запуске — не более одной попытки
    clock4 = FakeClock()
    fly4 = FakeFly(clock4, logs=[log_entry(T0 + 30, "ModuleNotFoundError: x")], rollback_ok=False)
    rep4, mon4, _ = run(ctx(), fly4)
    assert len(fly4.deploy_calls) == 1
    allowed, reasons = mon4.decide_rollback()
    assert not allowed and len(fly4.deploy_calls) == 1


def test_dm_observes_new_release_not_old_machine():
    """14. Старая машина не засчитывается: T0 — старт новой версии; чужие instance игнорируются."""
    clock = FakeClock()
    t_new = T_START + 85

    def st(now):
        if now < T_START + 90:
            return status(99, machine(PREV_SHA, PREV_DIG, PREV_IMG, 99, starts=[T_START - 86400]))
        return status(100, machine(NEW_SHA, NEW_DIG, NEW_IMG, 100, starts=[T_START - 86400, t_new]))
    logs = [log_entry(T_START + 300, "ModuleNotFoundError: other app", inst="ffffffffffffff")]
    fly = FakeFly(clock, status_fn=st, logs=logs)
    rep, mon, _ = run(ctx(), fly)
    assert mon.ctx.t0 == t_new, (mon.ctx.t0, t_new)
    assert rep["status"] == dm.HEALTHY, rep["reasons"]
    assert clock.now() >= t_new + 600
    # две машины: новая started + старая started → версия ещё не выкачена полностью
    clock2 = FakeClock()
    fly2 = FakeFly(clock2, status_fn=lambda now: status(
        100, machine(NEW_SHA, NEW_DIG, NEW_IMG, 100, starts=[T0]),
        machine(PREV_SHA, PREV_DIG, PREV_IMG, 99, starts=[T0 - 9999], mid=MID2)))
    rep2, mon2, _ = run(ctx(rollback_enabled=False), fly2)
    assert not mon2.obs.reached_started and rep2["status"] == dm.FAILED


# ─── Дополнительно ───────────────────────────────────────────────────────────

def test_dm_deploy_failed_production_unchanged():
    """deploy упал, машины на предыдущем образе → FAILED «production не изменён», без отката."""
    clock = FakeClock()
    fly = FakeFly(clock, status_fn=lambda now: restored_ok(now, T_START - 500))
    rep, _, _ = run(ctx(deploy_outcome="failure"), fly)
    assert rep["status"] == dm.FAILED and not fly.deploy_calls
    assert any("откат не требуется" in r for r in rep["reasons"]), rep["reasons"]
    assert not rep["rollback"]["considered"]
    # урок v98: deploy упал, но машина на новом сломанном образе → мониторинг и откат
    clock2 = FakeClock()
    fly2 = FakeFly(clock2, logs=[log_entry(T0 + 30, "ModuleNotFoundError: x")])
    rep2, _, _ = run(ctx(deploy_outcome="failure"), fly2)
    assert rep2["status"] == dm.ROLLED_BACK and fly2.deploy_calls == [PREV_IMG]


def test_dm_kill_switch_and_monitor_only():
    """kill-switch и monitor-only: откат не выполняется никогда."""
    fatal = [log_entry(T0 + 30, "ModuleNotFoundError: x")]
    clock = FakeClock()
    fly = FakeFly(clock, logs=fatal)
    rep, _, _ = run(ctx(kill_switch=True), fly)
    assert rep["status"] == dm.FAILED and not fly.deploy_calls
    clock2 = FakeClock()
    fly2 = FakeFly(clock2, logs=fatal)
    rep2, mon2, n2 = run(ctx(mode="monitor-only", expected_sha=""), fly2)
    assert mon2.ctx.expected_sha == NEW_SHA
    assert rep2["status"] == dm.FAILED and not fly2.deploy_calls
    assert not n2.pr_comments, "monitor-only не комментирует старый PR"
    clock3 = FakeClock()
    fly3 = FakeFly(clock3)
    rep3, _, _ = run(ctx(mode="monitor-only", expected_sha=""), fly3)
    assert rep3["status"] == dm.HEALTHY, rep3["reasons"]


def test_dm_flyctl_allowlist():
    """Allowlist: только фиксированные команды; volumes/secrets/произвольный ssh — запрещены."""
    seen = []

    def runner(argv, **kw):
        seen.append(argv[1:])
        out = "[]" if argv[1] == "releases" else ("{\"Machines\": []}" if argv[1] == "status" else "")
        if argv[1] == "ssh":
            out = dm.MODULE_CHECK_OK
        return subprocess.CompletedProcess(argv, 0, out, "")
    f = dm.FlyCtl(APP, binary="flyctl", runner=runner)
    f.status(); f.releases(); f.logs()
    assert f.exec_check(MID, ["db", "main"]) == (True, "")
    assert f.deploy_image(PREV_IMG) is True
    assert seen[-1] == ["deploy", "--app", APP, "--image", PREV_IMG, "--config", "fly.toml",
                        "--wait-timeout", "5m"], seen[-1]
    forbidden = [
        ["volumes", "destroy", "vol_x", "--app", APP],
        ["volumes", "list", "--app", APP],
        ["secrets", "set", "X=1", "--app", APP],
        ["secrets", "list", "--app", APP],
        ["apps", "destroy", APP],
        ["machines", "destroy", MID, "--app", APP],
        ["ssh", "console", "--app", APP, "--machine", MID, "-C", "rm -rf /data/bot.db"],
        ["ssh", "console", "--app", APP, "--machine", MID, "-C", "sqlite3 /data/bot.db .dump"],
        ["ssh", "console", "--app", APP, "--machine", "x;rm", "-C",
         dm.build_module_check_command(["db"])],
        ["deploy", "--app", APP, "--image", PREV_IMG],
        ["deploy", "--app", APP, "--image", "docker.io/evil:latest", "--config", "fly.toml",
         "--wait-timeout", "5m"],
        ["deploy", "--app", "other-app", "--image", PREV_IMG, "--config", "fly.toml",
         "--wait-timeout", "5m"],
        ["status", "--app", "other-app", "--json"],
        [],
    ]
    for args in forbidden:
        try:
            dm.validate_flyctl_args(args, APP)
        except dm.ForbiddenCommand:
            continue
        raise AssertionError(f"команда должна быть запрещена: {args}")
    try:
        dm.build_module_check_command(["db; rm -rf /"])
        raise AssertionError("невалидное имя модуля должно отклоняться")
    except dm.ForbiddenCommand:
        pass
    # ошибки flyctl → FlyUnavailable
    bad = dm.FlyCtl(APP, runner=lambda a, **k: subprocess.CompletedProcess(a, 0, "not json", ""))
    for fn in (bad.status, bad.releases):
        try:
            fn()
            raise AssertionError("invalid JSON → FlyUnavailable")
        except dm.FlyUnavailable:
            pass

    def boom(a, **k):
        raise subprocess.TimeoutExpired(a, 60)
    try:
        dm.FlyCtl(APP, runner=boom).status()
        raise AssertionError("timeout → FlyUnavailable")
    except dm.FlyUnavailable:
        pass
    # разбор результата проверки импорта
    tb = "Traceback (most recent call last):\n  File x\nModuleNotFoundError: No module named 'my_tasks'\n"
    f2 = dm.FlyCtl(APP, runner=lambda a, **k: subprocess.CompletedProcess(a, 1, "", tb))
    assert f2.exec_check(MID, ["my_tasks"]) == (False, "ModuleNotFoundError")
    f3 = dm.FlyCtl(APP, runner=lambda a, **k: subprocess.CompletedProcess(a, 1, "", "Error: ssh: unauthorized"))
    try:
        f3.exec_check(MID, ["db"])
        raise AssertionError("ssh недоступен → FlyUnavailable")
    except dm.FlyUnavailable:
        pass


def test_dm_report_has_no_log_text_or_email():
    """Отчёт/issue/summary: без текста логов, токенов и email (репозиторий публичный)."""
    clock = FakeClock()
    logs = [
        log_entry(T0 + 10, "HTTP Request: POST https://api.telegram.org/botSECRET123:ABCDEF/getUpdates"),
        log_entry(T0 + 20, "[ERROR] main: user private text Иванов SECRETPAYLOAD"),
        log_entry(T0 + 30, "ModuleNotFoundError: No module named 'SECRETMOD'"),
    ]
    fly = FakeFly(clock, logs=logs)
    rep, mon, n = run(ctx(), fly)
    blob = json.dumps(rep, ensure_ascii=False) + "".join(n.summaries) + "".join(
        b for _, b in n.incidents) + "".join(b for _, b in n.pr_comments)
    for needle in ("SECRET", "owner@example.com", "Иванов", "No module named", "api.telegram.org",
                   "getUpdates", "\"User\""):
        assert needle not in blob, needle
    assert "ModuleNotFoundError" in blob and "log_error" in blob


def test_dm_parse_multiline_log_stream():
    """`flyctl logs --json` — поток многострочных JSON-объектов, не NDJSON."""
    a = json.dumps(log_entry(T0, "one"), indent=4)
    b = json.dumps(log_entry(T0 + 1, "two {brace}"), indent=4)
    text = a + "\n" + b + "\ngarbage line\n" + json.dumps(log_entry(T0 + 2, "three"))
    out = dm.parse_log_stream(text)
    assert [e["message"] for e in out] == ["one", "two {brace}", "three"], out
    ts = dm.parse_ts("2026-11-15T13:23:31.293135522Z")
    assert abs(ts - datetime(2026, 11, 15, 13, 23, 31, 293135, tzinfo=timezone.utc).timestamp()) < 1e-3
    assert dm.parse_ts(1794493674416) == 1794493674.416


def test_dm_t0_from_new_version_start_and_snapshot():
    """T0 = последний start новой версии (а не время merge/запуска монитора)."""
    clock = FakeClock()
    fly = FakeFly(clock, status_fn=lambda now: status(100, machine(
        NEW_SHA, NEW_DIG, NEW_IMG, 100, starts=[T_START - 5000, T_START - 45])))
    mon = dm.Monitor(ctx(), fly, FakeGit(), FakeNotifier(), clock)
    assert mon.wait_for_expected_version()
    assert mon.ctx.t0 == T_START - 45 and mon.ctx.new_release == 100
    assert mon.ctx.new_digest == NEW_DIG and mon.ctx.new_image == NEW_IMG
    snap = dm.snapshot_from_status(new_ok(0))
    assert snap.version == 100 and snap.app_machines[0].gh_sha == NEW_SHA


def test_dm_capture_prev():
    """capture-prev: однозначная здоровая версия; иначе prev_healthy=false."""
    clock = FakeClock()
    out = dm.capture_prev(FakeFly(clock, status_fn=lambda now: restored_ok(now, 0),
                                  releases=[dict(RELEASES[1], Version=101)]))
    assert out["prev_healthy"] == "true" and out["prev_sha"] == PREV_SHA, out
    assert out["prev_image"] == PREV_IMG and out["prev_digest"] == PREV_DIG and out["prev_release"] == "101"
    two = lambda now: status(99, machine(PREV_SHA, PREV_DIG, PREV_IMG, 99),  # noqa: E731
                             machine(PREV_SHA, PREV_DIG, PREV_IMG, 99, mid=MID2))
    assert dm.capture_prev(FakeFly(clock, status_fn=two))["prev_healthy"] == "false"
    failing = lambda now: status(99, machine(PREV_SHA, PREV_DIG, PREV_IMG, 99, checks="critical"))  # noqa: E731
    assert dm.capture_prev(FakeFly(clock, status_fn=failing))["prev_healthy"] == "false"
    ok99 = lambda now: status(99, machine(PREV_SHA, PREV_DIG, PREV_IMG, 99))  # noqa: E731
    rel_failed = [dict(RELEASES[1], Status="failed")]
    assert dm.capture_prev(FakeFly(clock, status_fn=ok99, releases=rel_failed))["prev_healthy"] == "false"
    assert dm.capture_prev(FakeFly(clock, status_fn=ok99))["prev_healthy"] == "true"
    down = FakeFly(clock, status_fn=lambda now: dm.FlyUnavailable("x"))
    assert dm.capture_prev(down)["prev_healthy"] == "false"


def test_dm_notifier_incident_dedup():
    """Issue инцидента: существующий с тем же заголовком → комментарий, без дубля."""
    calls = []
    title = f"Deploy incident: FAILED {NEW_SHA[:7]}"

    def runner(argv, **kw):
        calls.append(argv)
        if argv[1:3] == ["issue", "list"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"number": 77, "title": title}]), "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    n = dm.Notifier(use_gh=True, runner=runner)
    n.use_gh = True
    n.incident(title, "body")
    assert any(c[1:4] == ["issue", "comment", "77"] for c in calls), calls
    assert not any(c[1:3] == ["issue", "create"] for c in calls)
    calls.clear()

    def runner2(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "[]", "")
    n2 = dm.Notifier(use_gh=True, runner=runner2)
    n2.use_gh = True
    n2.incident(title, "body")
    assert any(c[1:3] == ["issue", "create"] for c in calls), calls
    assert dm.find_pr_number("feat: x (#21) (#22)") == 22 and dm.find_pr_number("fix: y") is None


TESTS = [
    (test_dm_healthy_10_minutes, "Monitor: стабильно 10 мин → HEALTHY"),
    (test_dm_machine_never_starts_failed, "Monitor: машина не стартует → FAILED"),
    (test_dm_restart_loop_failed, "Monitor: рестарты → FAILED, 1 рестарт → DEGRADED"),
    (test_dm_intermittent_health_checks_not_healthy, "Monitor: мигающий health check ≠ HEALTHY"),
    (test_dm_fatal_log_after_t0_rollback_before_t0_ignored, "Monitor: FATAL после T0 → откат, до T0 — нет"),
    (test_dm_prev_unknown_rollback_forbidden, "Monitor: prev неизвестна → откат запрещён"),
    (test_dm_db_migration_rollback_forbidden, "Monitor: db.py/crypto.py/fly.toml → откат запрещён"),
    (test_dm_rollback_verified_and_restore_failure, "Monitor: откат проверяется, сбой → FAILED"),
    (test_dm_fly_unavailable_unknown, "Monitor: Fly/ssh недоступен → UNKNOWN"),
    (test_dm_repeated_run_no_double_rollback, "Monitor: повторный запуск без двойного отката"),
    (test_dm_observes_new_release_not_old_machine, "Monitor: наблюдается новый релиз, не старая машина"),
    (test_dm_deploy_failed_production_unchanged, "Monitor: deploy упал — production не изменён / v98"),
    (test_dm_kill_switch_and_monitor_only, "Monitor: kill-switch и monitor-only"),
    (test_dm_flyctl_allowlist, "Monitor: allowlist команд flyctl"),
    (test_dm_report_has_no_log_text_or_email, "Monitor: отчёт без текста логов и email"),
    (test_dm_parse_multiline_log_stream, "Monitor: разбор многострочного потока логов"),
    (test_dm_t0_from_new_version_start_and_snapshot, "Monitor: T0 от старта новой версии"),
    (test_dm_capture_prev, "Monitor: capture-prev"),
    (test_dm_notifier_incident_dedup, "Monitor: issue инцидента без дублей"),
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
    print("\n✅ Все тесты Deploy Monitor прошли")
