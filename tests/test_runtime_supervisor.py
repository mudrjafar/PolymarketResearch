import json

import pytest

from runtime_supervisor import (
    CRITICAL_FATAL_FAILURES,
    RESTART_BACKOFF_SECONDS,
    STATUS_REFRESH_SECONDS,
    STABLE_RESET_SECONDS,
    RuntimeSupervisor,
    ServiceSpec,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class FakeProcess:
    next_pid = 1000

    def __init__(self, name):
        self.name = name
        self.exit_code = None
        self.terminated = False
        self.killed = False
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1

    def poll(self):
        return self.exit_code

    def fail(self, code=1):
        self.exit_code = code

    def terminate(self):
        self.terminated = True
        if self.exit_code is None:
            self.exit_code = 0

    def wait(self, timeout=None):
        return self.exit_code

    def kill(self):
        self.killed = True
        self.exit_code = -9


class FakePopen:
    def __init__(self):
        self.started = []

    def __call__(self, cmd, cwd=None):
        process = FakeProcess(cmd[0])
        self.started.append(process)
        return process


def make_supervisor(tmp_path, specs):
    clock = FakeClock()
    popen = FakePopen()
    supervisor = RuntimeSupervisor(
        specs,
        cwd=tmp_path,
        status_file=tmp_path / "runtime_status.json",
        popen_factory=popen,
        clock=clock,
        sleep=lambda _: None,
        startup_stagger_seconds=0,
    )
    return supervisor, clock, popen


def _assert_noncritical_crash_isolated(tmp_path, name):
    specs = [
        ServiceSpec("collector", ("collector",), critical=True),
        ServiceSpec(name, (name,), critical=False),
    ]
    supervisor, clock, _ = make_supervisor(tmp_path, specs)
    supervisor.start_all()

    collector = supervisor.states["collector"].process
    failed = supervisor.states[name].process
    failed.fail(7)

    assert supervisor.poll_once() is None
    assert supervisor.states[name].status == "DEGRADED"
    assert supervisor.states["collector"].status == "RUNNING"
    assert supervisor.states["collector"].process is collector
    assert collector.poll() is None
    assert supervisor.overall_status() == "DEGRADED"

    clock.advance(1)
    assert supervisor.poll_once() is None
    assert supervisor.states[name].status == "RUNNING"
    assert supervisor.states[name].process is not failed
    assert supervisor.states["collector"].process is collector


def test_telegram_crash_degrades_only_telegram(tmp_path):
    _assert_noncritical_crash_isolated(tmp_path, "telegram")


def test_dashboard_crash_degrades_only_dashboard(tmp_path):
    _assert_noncritical_crash_isolated(tmp_path, "dashboard")


def test_learning_crash_degrades_only_learning(tmp_path):
    _assert_noncritical_crash_isolated(tmp_path, "learning")


def test_noncritical_repeated_crashes_never_become_fatal(tmp_path):
    specs = [
        ServiceSpec("collector", ("collector",), critical=True),
        ServiceSpec("learning", ("learning",), critical=False),
    ]
    supervisor, clock, _ = make_supervisor(tmp_path, specs)
    supervisor.start_all()
    collector = supervisor.states["collector"].process

    for _ in range(CRITICAL_FATAL_FAILURES + 3):
        supervisor.states["learning"].process.fail(1)
        assert supervisor.poll_once() is None
        state = supervisor.states["learning"]
        assert state.status == "DEGRADED"
        delay = state.next_restart_at - clock()
        clock.advance(delay)
        assert supervisor.poll_once() is None
        assert supervisor.states["learning"].status == "RUNNING"

    assert supervisor.states["collector"].process is collector
    assert collector.poll() is None
    assert supervisor.states["collector"].status == "RUNNING"
    assert supervisor.overall_status() == "RUNNING"


def test_critical_worker_uses_required_restart_backoff(tmp_path):
    specs = [ServiceSpec("collector", ("collector",), critical=True)]
    supervisor, clock, _ = make_supervisor(tmp_path, specs)
    supervisor.start_all()

    observed = []
    for expected in RESTART_BACKOFF_SECONDS:
        supervisor.states["collector"].process.fail(2)
        assert supervisor.poll_once() is None
        state = supervisor.states["collector"]
        observed.append(state.next_restart_at - clock())
        assert state.status == "DEGRADED"

        clock.advance(expected)
        assert supervisor.poll_once() is None
        assert supervisor.states["collector"].status == "RUNNING"

    assert observed == list(RESTART_BACKOFF_SECONDS)


def test_eight_fast_critical_failures_are_fatal(tmp_path):
    specs = [ServiceSpec("paper", ("paper",), critical=True)]
    supervisor, clock, _ = make_supervisor(tmp_path, specs)
    supervisor.start_all()

    fatal = None
    for _ in range(CRITICAL_FATAL_FAILURES):
        supervisor.states["paper"].process.fail(3)
        fatal = supervisor.poll_once()
        if fatal:
            break
        state = supervisor.states["paper"]
        clock.advance(state.next_restart_at - clock())
        fatal = supervisor.poll_once()

    assert fatal == "paper"
    assert supervisor.states["paper"].status == "FATAL"
    assert supervisor.states["paper"].consecutive_failures == 8
    assert supervisor.overall_status() == "FATAL"


def test_stable_120_seconds_resets_failure_streak(tmp_path):
    specs = [ServiceSpec("risk", ("risk",), critical=True)]
    supervisor, clock, _ = make_supervisor(tmp_path, specs)
    supervisor.start_all()

    supervisor.states["risk"].process.fail(1)
    assert supervisor.poll_once() is None
    clock.advance(1)
    assert supervisor.poll_once() is None
    assert supervisor.states["risk"].consecutive_failures == 1

    clock.advance(STABLE_RESET_SECONDS)
    assert supervisor.poll_once() is None
    assert supervisor.states["risk"].status == "RUNNING"
    assert supervisor.states["risk"].consecutive_failures == 0

    supervisor.states["risk"].process.fail(1)
    assert supervisor.poll_once() is None
    assert supervisor.states["risk"].consecutive_failures == 1


def test_missing_telegram_credentials_disable_only_telegram(monkeypatch):
    import run_machine

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    specs = {spec.name: spec for spec in run_machine.build_services()}

    assert specs["telegram"].enabled is False
    assert specs["telegram"].critical is False
    assert all(
        spec.enabled
        for name, spec in specs.items()
        if name != "telegram"
    )


def test_missing_telegram_credentials_do_not_block_validation(monkeypatch):
    import run_machine

    monkeypatch.setenv("POLYMARKET_RPC_URL", "configured-for-test")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    assert run_machine.validate() is True


def test_missing_rpc_still_blocks_live_startup(monkeypatch):
    import run_machine

    monkeypatch.delenv("POLYMARKET_RPC_URL", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    assert run_machine.validate() is False


def test_runtime_status_file_contains_required_states(tmp_path):
    specs = [
        ServiceSpec("collector", ("collector",), critical=True),
        ServiceSpec("telegram", ("telegram",), critical=False),
        ServiceSpec("optional", ("optional",), critical=False, enabled=False),
    ]
    supervisor, _, _ = make_supervisor(tmp_path, specs)
    supervisor.start_all()

    supervisor.states["telegram"].process.fail(9)
    assert supervisor.poll_once() is None

    payload = json.loads(
        (tmp_path / "runtime_status.json").read_text(encoding="utf-8")
    )

    assert payload["schema_version"] == 1
    assert payload["generated_at"]
    assert payload["overall_status"] == "DEGRADED"

    collector = payload["services"]["collector"]
    assert collector["name"] == "collector"
    assert collector["critical"] is True
    assert collector["enabled"] is True
    assert collector["status"] == "RUNNING"
    assert isinstance(collector["pid"], int)

    telegram = payload["services"]["telegram"]
    assert telegram["critical"] is False
    assert telegram["status"] == "DEGRADED"
    assert telegram["consecutive_failures"] == 1
    assert telegram["restart_count"] == 1
    assert telegram["last_exit_code"] == 9
    assert telegram["last_error"] == "exit code 9"
    assert telegram["restart_in_seconds"] == 1.0

    optional = payload["services"]["optional"]
    assert optional["enabled"] is False
    assert optional["status"] == "DISABLED"
    assert optional["pid"] is None


def test_runtime_status_refreshes_while_healthy(monkeypatch, tmp_path):
    specs = [ServiceSpec("collector", ("collector",), critical=True)]
    supervisor, clock, _ = make_supervisor(tmp_path, specs)
    writes = []

    monkeypatch.setattr(
        "runtime_supervisor.save_json_atomic",
        lambda path, payload: writes.append(payload),
    )
    supervisor.start_all()
    assert len(writes) == 1

    clock.advance(STATUS_REFRESH_SECONDS - 1)
    supervisor.poll_once()
    assert len(writes) == 1

    clock.advance(1)
    supervisor.poll_once()
    assert len(writes) == 2
    assert writes[-1]["overall_status"] == "RUNNING"


def test_runtime_status_write_failure_does_not_stop_supervision(
    monkeypatch, tmp_path
):
    specs = [
        ServiceSpec("collector", ("collector",), critical=True),
        ServiceSpec("dashboard", ("dashboard",), critical=False),
    ]
    supervisor, clock, _ = make_supervisor(tmp_path, specs)

    calls = {"count": 0}

    def broken_save(*args, **kwargs):
        calls["count"] += 1
        raise PermissionError("locked")

    monkeypatch.setattr("runtime_supervisor.save_json_atomic", broken_save)

    supervisor.start_all()
    assert supervisor.states["collector"].status == "RUNNING"

    supervisor.states["dashboard"].process.fail(1)
    assert supervisor.poll_once() is None
    assert supervisor.states["dashboard"].status == "DEGRADED"
    assert supervisor.states["collector"].status == "RUNNING"

    clock.advance(30)
    assert supervisor.poll_once() is None
    assert supervisor.states["dashboard"].status == "RUNNING"
    assert supervisor.states["collector"].status == "RUNNING"
    assert calls["count"] >= 2


def test_shutdown_terminates_running_children_cleanly(tmp_path):
    specs = [
        ServiceSpec("collector", ("collector",), critical=True),
        ServiceSpec("telegram", ("telegram",), critical=False),
        ServiceSpec("disabled", ("disabled",), critical=False, enabled=False),
    ]
    supervisor, _, popen = make_supervisor(tmp_path, specs)
    supervisor.start_all()

    collector = supervisor.states["collector"].process
    telegram = supervisor.states["telegram"].process

    supervisor.shutdown()

    assert collector.terminated is True
    assert telegram.terminated is True
    assert supervisor.states["collector"].status == "STOPPED"
    assert supervisor.states["telegram"].status == "STOPPED"
    assert supervisor.states["disabled"].status == "DISABLED"
    assert len(popen.started) == 2


def test_services_preserve_current_public_order_and_workers():
    import run_machine

    assert [name for name, _ in run_machine.SERVICES] == [
        "collector",
        "flow",
        "diamond",
        "risk",
        "learning",
        "focus",
        "book",
        "paper",
        "telegram",
        "dashboard",
    ]


def test_learning_telegram_dashboard_are_noncritical(monkeypatch):
    import run_machine

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    specs = {spec.name: spec for spec in run_machine.build_services()}

    assert specs["learning"].critical is False
    assert specs["telegram"].critical is False
    assert specs["dashboard"].critical is False


def test_core_pipeline_services_remain_critical(monkeypatch):
    import run_machine

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    specs = {spec.name: spec for spec in run_machine.build_services()}

    for name in (
        "collector",
        "flow",
        "diamond",
        "risk",
        "focus",
        "book",
        "paper",
    ):
        assert specs[name].critical is True


def test_disabled_service_never_spawns(tmp_path):
    specs = [
        ServiceSpec("collector", ("collector",), critical=True),
        ServiceSpec("telegram", ("telegram",), critical=False, enabled=False),
    ]
    supervisor, _, popen = make_supervisor(tmp_path, specs)
    supervisor.start_all()

    assert [process.name for process in popen.started] == ["collector"]
    assert supervisor.states["telegram"].status == "DISABLED"
    assert supervisor.overall_status() == "RUNNING"
