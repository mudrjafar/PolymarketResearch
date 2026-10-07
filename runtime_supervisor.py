"""Process supervision for the Diamond Intelligence runtime.

Core workers are restartable but critical: repeated rapid failure eventually
becomes FATAL. Interface consumers are restartable and may remain DEGRADED
without stopping the core pipeline.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import time

from machine_common import save_json_atomic


RESTART_BACKOFF_SECONDS = (1, 2, 5, 10, 30)
STABLE_RESET_SECONDS = 120
CRITICAL_FATAL_FAILURES = 8
POLL_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    cmd: tuple
    critical: bool = True
    enabled: bool = True


@dataclass
class ServiceState:
    spec: ServiceSpec
    process: object = None
    status: str = "STOPPED"
    consecutive_failures: int = 0
    restart_count: int = 0
    last_started_at: float = None
    last_exit_code: int = None
    next_restart_at: float = None
    last_error: str = None


class RuntimeSupervisor:
    def __init__(
        self,
        specs,
        *,
        cwd,
        status_file,
        popen_factory=subprocess.Popen,
        clock=time.monotonic,
        sleep=time.sleep,
        startup_stagger_seconds=0.7,
    ):
        self.cwd = Path(cwd)
        self.status_file = Path(status_file)
        self.popen_factory = popen_factory
        self.clock = clock
        self.sleep = sleep
        self.startup_stagger_seconds = float(startup_stagger_seconds)
        self.states = {
            spec.name: ServiceState(spec=spec)
            for spec in specs
        }
        self._dirty = True

    def _set_dirty(self):
        self._dirty = True

    def _start(self, state, now):
        spec = state.spec
        if not spec.enabled:
            state.status = "DISABLED"
            state.next_restart_at = None
            self._set_dirty()
            return

        print(f"[START] {spec.name}")
        try:
            process = self.popen_factory(list(spec.cmd), cwd=self.cwd)
        except Exception as exc:
            state.process = None
            state.last_started_at = now
            state.last_exit_code = None
            state.last_error = f"{type(exc).__name__}: {exc}"
            self._register_failure(state, now, spawned=False)
            return

        state.process = process
        state.status = "RUNNING"
        state.last_started_at = now
        state.last_exit_code = None
        state.next_restart_at = None
        state.last_error = None
        self._set_dirty()

    def start_all(self):
        now = self.clock()
        enabled_started = 0
        for state in self.states.values():
            self._start(state, now)
            if state.spec.enabled:
                enabled_started += 1
                if self.startup_stagger_seconds > 0:
                    self.sleep(self.startup_stagger_seconds)
                now = self.clock()
        self._persist(now)
        return enabled_started

    def _register_failure(self, state, now, *, spawned=True, exit_code=None):
        if (
            spawned
            and state.last_started_at is not None
            and now - state.last_started_at >= STABLE_RESET_SECONDS
        ):
            state.consecutive_failures = 0

        state.process = None
        state.last_exit_code = exit_code
        state.consecutive_failures += 1
        state.restart_count += 1

        if state.spec.critical and state.consecutive_failures >= CRITICAL_FATAL_FAILURES:
            state.status = "FATAL"
            state.next_restart_at = None
            print(
                f"[SUPERVISOR] {state.spec.name} crash-loop: FATAL after "
                f"{state.consecutive_failures} consecutive failures"
            )
            self._set_dirty()
            return

        delay = RESTART_BACKOFF_SECONDS[
            min(state.consecutive_failures - 1, len(RESTART_BACKOFF_SECONDS) - 1)
        ]
        state.status = "DEGRADED"
        state.next_restart_at = now + delay

        kind = "core" if state.spec.critical else "consumer"
        rc_text = "spawn error" if exit_code is None else f"exit code {exit_code}"
        print(
            f"[SUPERVISOR] {state.spec.name} ({kind}) {rc_text}; "
            f"DEGRADED, restart in {delay}s"
        )
        self._set_dirty()

    def poll_once(self):
        now = self.clock()
        fatal_service = None

        for state in self.states.values():
            if not state.spec.enabled:
                continue

            if state.process is not None:
                rc = state.process.poll()
                if rc is None:
                    if (
                        state.consecutive_failures
                        and state.last_started_at is not None
                        and now - state.last_started_at >= STABLE_RESET_SECONDS
                    ):
                        state.consecutive_failures = 0
                        self._set_dirty()
                else:
                    self._register_failure(
                        state,
                        now,
                        spawned=True,
                        exit_code=rc,
                    )

            if (
                state.process is None
                and state.status == "DEGRADED"
                and state.next_restart_at is not None
                and now >= state.next_restart_at
            ):
                print(f"[SUPERVISOR] retrying {state.spec.name}")
                self._start(state, now)

            if state.status == "FATAL" and state.spec.critical:
                fatal_service = state.spec.name
                break

        self._persist(now)
        return fatal_service

    def overall_status(self):
        if any(
            state.spec.critical and state.status == "FATAL"
            for state in self.states.values()
        ):
            return "FATAL"
        if any(
            state.status in {"DEGRADED", "FATAL"}
            for state in self.states.values()
            if state.spec.enabled
        ):
            return "DEGRADED"
        return "OK"

    def snapshot(self, now=None):
        now = self.clock() if now is None else now
        services = {}

        for name, state in self.states.items():
            process = state.process
            pid = getattr(process, "pid", None) if process is not None else None
            restart_in = None
            if state.next_restart_at is not None:
                restart_in = max(0.0, state.next_restart_at - now)

            services[name] = {
                "critical": state.spec.critical,
                "enabled": state.spec.enabled,
                "status": state.status,
                "pid": pid,
                "consecutive_failures": state.consecutive_failures,
                "restart_count": state.restart_count,
                "last_exit_code": state.last_exit_code,
                "last_error": state.last_error,
                "restart_in_seconds": (
                    round(restart_in, 3) if restart_in is not None else None
                ),
            }

        return {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "overall_status": self.overall_status(),
            "services": services,
        }

    def _persist(self, now=None):
        if not self._dirty:
            return
        try:
            save_json_atomic(self.status_file, self.snapshot(now))
        except Exception as exc:
            print(
                "[SUPERVISOR] runtime status write failed "
                f"({type(exc).__name__}); supervision continues"
            )
            return
        self._dirty = False

    def shutdown(self):
        active = []
        for state in self.states.values():
            process = state.process
            if process is None:
                continue
            try:
                if process.poll() is None:
                    process.terminate()
                    active.append(process)
            except Exception:
                pass

        for process in active:
            try:
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        for state in self.states.values():
            state.process = None
            if state.status != "FATAL":
                state.status = "STOPPED"
            state.next_restart_at = None
        self._set_dirty()
        self._persist(self.clock())
