import os
import subprocess
import sys
from pathlib import Path

from runtime_supervisor import POLL_INTERVAL_SECONDS, RuntimeSupervisor, ServiceSpec

BASE=Path(__file__).resolve().parent
PY=sys.executable
RUNTIME_STATUS_FILE=BASE/"data"/"runtime_status.json"

# Backward-compatible public runtime order used by existing integration tests
# and operator tooling. Supervision policy is layered on top in build_services().
SERVICES=[
    ("collector",(PY,"-u",str(BASE/"scripts"/"live_active_trades.py"))),
    ("flow",(PY,"-u",str(BASE/"scripts"/"flow_tracker.py"))),
    ("diamond",(PY,"-u",str(BASE/"scripts"/"diamond_filter_v3.py"),"--watch","--interval","10")),
    ("risk",(PY,"-u",str(BASE/"scripts"/"risk_worker.py"),"--interval","5")),
    ("focus",(PY,"-u",str(BASE/"scripts"/"focus_runner.py"),"--interval","5")),
    ("book",(PY,"-u",str(BASE/"scripts"/"book_worker.py"),"--interval","5")),
    ("paper",(PY,"-u",str(BASE/"scripts"/"paper_worker.py"),"--interval","5")),
    ("telegram",(PY,"-u",str(BASE/"telegram_bot.py"))),
    ("dashboard",(PY,"-m","streamlit","run",str(BASE/"dashboard.py"),"--server.headless=true","--server.port=8501")),
]


def build_services():
    telegram_enabled=bool(
        os.getenv("TELEGRAM_BOT_TOKEN","").strip()
        and os.getenv("TELEGRAM_CHAT_ID","").strip()
    )

    specs=[]
    for name, cmd in SERVICES:
        consumer=name in {"telegram","dashboard"}
        enabled=telegram_enabled if name == "telegram" else True
        specs.append(
            ServiceSpec(
                name,
                tuple(cmd),
                critical=not consumer,
                enabled=enabled,
            )
        )
    return specs


def validate():
    missing=[]
    if not os.getenv("POLYMARKET_RPC_URL","").strip():
        missing.append("POLYMARKET_RPC_URL")

    files=[
        BASE/"scripts"/"live_active_trades.py",
        BASE/"scripts"/"flow_tracker.py",
        BASE/"scripts"/"diamond_filter_v3.py",
        BASE/"scripts"/"risk_engine.py",
        BASE/"scripts"/"risk_worker.py",
        BASE/"scripts"/"focus_engine.py",
        BASE/"scripts"/"focus_runner.py",
        BASE/"scripts"/"book_engine.py",
        BASE/"scripts"/"book_worker.py",
        BASE/"scripts"/"paper_engine.py",
        BASE/"scripts"/"paper_worker.py",
        BASE/"telegram_bot.py",
        BASE/"dashboard.py",
        BASE/"runtime_supervisor.py",
        BASE/"collector_storage_v4"/"storage.py",
        BASE/"collector_storage_v4"/"bridge.py",
    ]
    for file in files:
        if not file.exists():
            missing.append(str(file.relative_to(BASE)))

    if missing:
        print("[STARTUP] Missing: "+", ".join(missing))
        return False

    if not os.getenv("TELEGRAM_BOT_TOKEN","").strip() or not os.getenv("TELEGRAM_CHAT_ID","").strip():
        print("[STARTUP] Telegram disabled: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    return True


def self_test():
    print("="*70)
    print("DIAMOND INTELLIGENCE V3 - SELF TEST")
    print("="*70)

    for test_path in [
        BASE/"tests"/"test_machine_v3.py",
        BASE/"tests"/"test_regressions.py",
        BASE/"collector_storage_v4"/"test_storage.py",
        BASE/"tests"/"test_collector_sqlite.py",
    ]:
        test=subprocess.run([PY,"-B",str(test_path)],cwd=BASE)
        if test.returncode:
            return test.returncode

    for cmd in [
        [PY,"-m","pytest","-q",str(BASE/"tests"/"test_collector_legacy_schema_migration.py")],
        [PY,"-m","pytest","-q",str(BASE/"tests"/"test_runtime_supervisor.py")],
        [PY,"-B",str(BASE/"scripts"/"risk_engine.py"),"--self-test"],
        [PY,"-B",str(BASE/"scripts"/"focus_engine.py")],
        [PY,"-B",str(BASE/"scripts"/"focus_runner.py"),"--self-test"],
        [PY,"-B",str(BASE/"scripts"/"book_engine.py")],
        [PY,"-B",str(BASE/"scripts"/"book_worker.py"),"--self-test"],
        [PY,"-B",str(BASE/"scripts"/"paper_engine.py")],
        [PY,"-B",str(BASE/"scripts"/"paper_worker.py"),"--self-test"],
    ]:
        test=subprocess.run(cmd,cwd=BASE)
        if test.returncode:
            return test.returncode

    return 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    if not validate():
        return 2

    subprocess.run(
        [PY,str(BASE/"scripts"/"prepare_v3_data.py")],
        cwd=BASE,
        check=True,
    )

    services=build_services()
    supervisor=RuntimeSupervisor(
        services,
        cwd=BASE,
        status_file=RUNTIME_STATUS_FILE,
    )

    print("="*70)
    print("DIAMOND INTELLIGENCE V3 - ONE START MACHINE")
    print("="*70)
    print("Pipeline: LIVE V2 SCAN -> FLOW -> DIAMOND V3 -> RISK -> FOCUS -> BOOK -> PAPER + TELEGRAM + DASHBOARD")
    print("Supervisor: core=critical restart | telegram/dashboard=isolated consumers")
    print("Dashboard: http://localhost:8501")

    try:
        supervisor.start_all()

        while True:
            fatal_service=supervisor.poll_once()
            if fatal_service:
                print(
                    f"[FATAL] critical service {fatal_service} exceeded "
                    "the restart safety threshold"
                )
                return 1
            supervisor.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\n[STOP] Shutting down...")
        return 0
    except Exception as exc:
        print(f"[FATAL] supervisor error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        supervisor.shutdown()


if __name__=="__main__":
    raise SystemExit(main())
