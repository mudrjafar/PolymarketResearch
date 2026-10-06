import os
import subprocess
import sys
import time
from pathlib import Path

BASE=Path(__file__).resolve().parent
PY=sys.executable

SERVICES=[
    ("collector", [PY,"-u",str(BASE/"scripts"/"live_active_trades.py")]),
    ("flow", [PY,"-u",str(BASE/"scripts"/"flow_tracker.py")]),
    ("diamond", [PY,"-u",str(BASE/"scripts"/"diamond_filter_v3.py"),"--watch","--interval","10"]),
    ("risk", [PY,"-u",str(BASE/"scripts"/"risk_worker.py"),"--interval","5"]),
    ("focus", [PY,"-u",str(BASE/"scripts"/"focus_runner.py"),"--interval","5"]),
    ("telegram", [PY,"-u",str(BASE/"telegram_bot.py")]),
    ("dashboard", [PY,"-m","streamlit","run",str(BASE/"dashboard.py"),"--server.headless=true","--server.port=8501"]),
]


def validate():
    missing=[]
    for var in ["POLYMARKET_RPC_URL","TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID"]:
        if not os.getenv(var,"").strip(): missing.append(var)
    files=[BASE/"scripts"/"live_active_trades.py",BASE/"scripts"/"flow_tracker.py",BASE/"scripts"/"diamond_filter_v3.py",BASE/"scripts"/"risk_engine.py",BASE/"scripts"/"risk_worker.py",BASE/"scripts"/"focus_engine.py",BASE/"scripts"/"focus_runner.py",BASE/"telegram_bot.py",BASE/"dashboard.py",BASE/"collector_storage_v4"/"storage.py",BASE/"collector_storage_v4"/"bridge.py"]
    for f in files:
        if not f.exists(): missing.append(str(f.relative_to(BASE)))
    if missing:
        print("[STARTUP] Missing: "+", ".join(missing)); return False
    return True


def self_test():
    print("="*70); print("DIAMOND INTELLIGENCE V3 - SELF TEST"); print("="*70)
    for test_path in [BASE/"tests"/"test_machine_v3.py", BASE/"tests"/"test_regressions.py",
                      BASE/"collector_storage_v4"/"test_storage.py", BASE/"tests"/"test_collector_sqlite.py"]:
        t=subprocess.run([PY,"-B",str(test_path)],cwd=BASE)
        if t.returncode: return t.returncode
    for cmd in [
        [PY,"-B",str(BASE/"scripts"/"risk_engine.py"),"--self-test"],
        [PY,"-B",str(BASE/"scripts"/"focus_engine.py")],
        [PY,"-B",str(BASE/"scripts"/"focus_runner.py"),"--self-test"],
    ]:
        t=subprocess.run(cmd,cwd=BASE)
        if t.returncode: return t.returncode
    return 0


def main():
    if "--self-test" in sys.argv: return self_test()
    if not validate(): return 2
    subprocess.run([PY,str(BASE/"scripts"/"prepare_v3_data.py")],cwd=BASE,check=True)
    children=[]
    print("="*70); print("DIAMOND INTELLIGENCE V3 - ONE START MACHINE"); print("="*70)
    print("Pipeline: LIVE V2 SCAN -> FLOW -> DIAMOND V3 -> RISK -> FOCUS -> TELEGRAM + DASHBOARD")
    print("Dashboard: http://localhost:8501")
    try:
        for name,cmd in SERVICES:
            print(f"[START] {name}")
            p=subprocess.Popen(cmd,cwd=BASE)
            children.append((name,p)); time.sleep(.7)
        while True:
            for name,p in children:
                rc=p.poll()
                if rc is not None: raise RuntimeError(f"{name} stopped with exit code {rc}")
            time.sleep(2)
    except KeyboardInterrupt: print("\n[STOP] Shutting down...")
    except Exception as exc: print(f"[FATAL] {exc}"); return 1
    finally:
        for _,p in children:
            if p.poll() is None: p.terminate()
        for _,p in children:
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill()
    return 0

if __name__=="__main__": raise SystemExit(main())
