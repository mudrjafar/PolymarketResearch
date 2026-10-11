from pathlib import Path

from runtime_supervisor import (
    CRITICAL_FATAL_FAILURES,
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


def test_consumer_crash_is_degraded_and_core_keeps_running(tmp_path):
    specs = [
        ServiceSpec("core", ("core",), critical=True),
        ServiceSpec("telegram", ("telegram",), critical=False),
    ]
    supervisor, clock, popen = make_supervisor(tmp_path, specs)
    supervisor.start_all()

    core = supervisor.states["core"].process
    telegram = supervisor.states["telegram"].process
    telegram.fail(1)

    assert supervisor.poll_once() is None
    assert supervisor.states["telegram"].status == "DEGRADED"
    assert supervisor.states["core"].status == "RUNNING"
    assert supervisor.states["core"].process is core
    assert core.poll() is None

    clock.advance(1)
    assert supervisor.poll_once() is None
    assert supervisor.states["telegram"].status == "RUNNING"
    assert supervisor.states["telegram"].process is not telegram


def test_consumer_can_crash_repeatedly_without_becoming_fatal(tmp_path):
    specs = [
        ServiceSpec("core", ("core",), critical=True),
        ServiceSpec("telegram", ("telegram",), critical=False),
    ]
    supervisor, clock, _ = make_supervisor(tmp_path, specs)
    supervisor.start_all()

    for _ in range(CRITICAL_FATAL_FAILURES + 3):
        process = supervisor.states["telegram"].process
        process.fail(1)
        assert supervisor.poll_once() is None
        state = supervisor.states["telegram"]
        assert state.status == "DEGRADED"
        clock.advance(max(1, state.next_restart_at - clock()))
        assert supervisor.poll_once() is None
        assert supervisor.states["telegram"].status == "RUNNING"

    assert supervisor.states["core"].status == "RUNNING"
    assert supervisor.overall_status() == "OK"


def test_critical_crash_loop_eventually_becomes_fatal(tmp_path):
    specs = [ServiceSpec("collector", ("collector",), critical=True)]
    supervisor, clock, _ = make_supervisor(tmp_path, specs)
    supervisor.start_all()

    fatal = None
    for _ in range(CRITICAL_FATAL_FAILURES):
        supervisor.states["collector"].process.fail(2)
        fatal = supervisor.poll_once()
        if fatal:
            break
        state = supervisor.states["collector"]
        clock.advance(max(1, state.next_restart_at - clock()))
        fatal = supervisor.poll_once()

    assert fatal == "collector"
    assert supervisor.states["collector"].status == "FATAL"
    assert supervisor.overall_status() == "FATAL"


def test_stable_run_resets_critical_failure_streak(tmp_path):
    specs = [ServiceSpec("risk", ("risk",), critical=True)]
    supervisor, clock, _ = make_supervisor(tmp_path, specs)
    supervisor.start_all()

    supervisor.states["risk"].process.fail(1)
    assert supervisor.poll_once() is None
    clock.advance(1)
    assert supervisor.poll_once() is None
    assert supervisor.states["risk"].consecutive_failures == 1

    clock.advance(STABLE_RESET_SECONDS + 1)
    supervisor.states["risk"].process.fail(1)
    assert supervisor.poll_once() is None

    assert supervisor.states["risk"].consecutive_failures == 1
    assert supervisor.states["risk"].status == "DEGRADED"


def test_disabled_consumer_never_spawns_and_is_not_degraded(tmp_path):
    specs = [
        ServiceSpec("core", ("core",), critical=True),
        ServiceSpec("telegram", ("telegram",), critical=False, enabled=False),
    ]
    supervisor, _, popen = make_supervisor(tmp_path, specs)
    supervisor.start_all()

    assert [process.name for process in popen.started] == ["core"]
    assert supervisor.states["telegram"].status == "DISABLED"
    assert supervisor.overall_status() == "OK"


def test_runtime_status_snapshot_exposes_service_health(tmp_path):
    specs = [
        ServiceSpec("core", ("core",), critical=True),
        ServiceSpec("dashboard", ("dashboard",), critical=False),
    ]
    supervisor, _, _ = make_supervisor(tmp_path, specs)
    supervisor.start_all()
    supervisor.states["dashboard"].process.fail(7)
    supervisor.poll_once()

    snapshot = supervisor.snapshot()

    assert snapshot["schema_version"] == 1
    assert snapshot["overall_status"] == "DEGRADED"
    assert snapshot["services"]["core"]["status"] == "RUNNING"
    assert snapshot["services"]["dashboard"]["status"] == "DEGRADED"
    assert snapshot["services"]["dashboard"]["last_exit_code"] == 7
    assert snapshot["services"]["dashboard"]["restart_in_seconds"] == 1.0


def test_runtime_status_refreshes_while_services_remain_healthy(
    monkeypatch, tmp_path
):
    specs = [ServiceSpec("core", ("core",), critical=True)]
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


def test_shutdown_terminates_only_running_children(tmp_path):
    specs = [
        ServiceSpec("core", ("core",), critical=True),
        ServiceSpec("telegram", ("telegram",), critical=False),
    ]
    supervisor, _, _ = make_supervisor(tmp_path, specs)
    supervisor.start_all()

    core = supervisor.states["core"].process
    telegram = supervisor.states["telegram"].process
    telegram.fail(1)
    supervisor.poll_once()

    supervisor.shutdown()

    assert core.terminated is True
    assert telegram.terminated is False
    assert supervisor.states["core"].status == "STOPPED"
    assert supervisor.states["telegram"].status == "STOPPED"


def test_run_machine_classifies_only_interfaces_as_noncritical(monkeypatch):
    import run_machine

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    specs = {spec.name: spec for spec in run_machine.build_services()}

    assert specs["collector"].critical is True
    assert specs["flow"].critical is True
    assert specs["diamond"].critical is True
    assert specs["risk"].critical is True
    assert specs["learning"].critical is True
    assert specs["focus"].critical is True
    assert specs["book"].critical is True
    assert specs["paper"].critical is True
    assert specs["telegram"].critical is False
    assert specs["dashboard"].critical is False
    assert specs["telegram"].enabled is True


def test_missing_telegram_env_does_not_block_runtime_validation(monkeypatch):
    import run_machine

    monkeypatch.setenv("POLYMARKET_RPC_URL", "configured-for-test")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    assert run_machine.validate() is True


def test_missing_telegram_env_disables_only_telegram(monkeypatch):
    import run_machine

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    specs = {spec.name: spec for spec in run_machine.build_services()}

    assert specs["telegram"].enabled is False
    assert all(
        spec.enabled
        for name, spec in specs.items()
        if name != "telegram"
    )


def test_stable_running_service_resets_failure_streak_without_waiting_for_next_crash(tmp_path):
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


def test_runtime_status_write_failure_never_stops_supervision(monkeypatch, tmp_path):
    specs = [
        ServiceSpec("core", ("core",), critical=True),
        ServiceSpec("telegram", ("telegram",), critical=False),
    ]
    supervisor, clock, _ = make_supervisor(tmp_path, specs)

    calls = {"count": 0}

    def broken_save(*args, **kwargs):
        calls["count"] += 1
        raise PermissionError("locked")

    monkeypatch.setattr("runtime_supervisor.save_json_atomic", broken_save)

    supervisor.start_all()
    assert supervisor.states["core"].status == "RUNNING"
    assert supervisor.states["telegram"].status == "RUNNING"

    supervisor.states["telegram"].process.fail(1)
    clock.advance(30)
    assert supervisor.poll_once() is None
    assert supervisor.states["core"].status == "RUNNING"
    assert supervisor.states["telegram"].status == "DEGRADED"
    assert calls["count"] >= 2


def test_disabled_consumer_remains_disabled_after_shutdown(tmp_path):
    specs = [ServiceSpec("telegram", ("telegram",), critical=False, enabled=False)]
    supervisor, _, popen = make_supervisor(tmp_path, specs)
    supervisor.start_all()

    supervisor.shutdown()

    assert popen.started == []
    assert supervisor.states["telegram"].status == "DISABLED"
