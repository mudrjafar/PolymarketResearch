import json
import os
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

TRADES_FILE = DATA_DIR / "live_trades.jsonl"
FLOW_FILE = DATA_DIR / "flow_state.json"
DIAMONDS_FILE = DATA_DIR / "diamonds.json"
CANDIDATES_FILE = DATA_DIR / "diamond_candidates.json"
RISK_FILE = DATA_DIR / "risk_assessment.json"
FOCUS_STATE_FILE = DATA_DIR / "focus_state.json"
FOCUS_FILE = DATA_DIR / "focused_market.json"
BOOK_FILE = DATA_DIR / "book_assessment.json"
PAPER_FILE = DATA_DIR / "paper_state.json"
RUNTIME_STATUS_FILE = DATA_DIR / "runtime_status.json"

REFRESH_SECONDS = 5
TAIL_LINES = 50000


def load_json(path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def file_age(path):
    try:
        age = max(0, time.time() - path.stat().st_mtime)
        if age < 60:
            return f"{int(age)}s ago"
        if age < 3600:
            return f"{int(age//60)}m ago"
        return f"{int(age//3600)}h ago"
    except Exception:
        return "missing"


def count_recent_trades():
    if not TRADES_FILE.exists():
        return 0, 0

    total = 0
    recent = 0
    cutoff = datetime.now(timezone.utc).timestamp() - 15 * 60

    try:
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            lines = deque(f, maxlen=TAIL_LINES)

        total = len(lines)
        for line in lines:
            try:
                row = json.loads(line)
                ts = row.get("detected_at")
                if not ts:
                    continue
                ts = str(ts).replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt.timestamp() >= cutoff:
                    recent += 1
            except Exception:
                continue
    except Exception:
        pass

    return total, recent


def flow_records(data):
    if isinstance(data, dict):
        return [v for v in data.values() if isinstance(v, dict)]
    if isinstance(data, list):
        return [v for v in data if isinstance(v, dict)]
    return []


def focused_label(focus):
    if not isinstance(focus, dict) or not focus:
        return "NONE"

    if str(focus.get("state") or "").upper() == "IDLE":
        return "NONE"

    nested = focus.get("focus")
    if isinstance(nested, dict):
        focus = nested

    market = focus.get("market")
    if isinstance(market, dict):
        q = market.get("question") or market.get("title")
    else:
        q = focus.get("question") or focus.get("title")
    out = focus.get("outcome") or (market.get("outcome") if isinstance(market, dict) else None)
    if q:
        return f"{q} | {out or '?'}"
    return focus.get("condition_id") or "ACTIVE"


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def main():
    while True:
        flow = load_json(FLOW_FILE, {})
        records = flow_records(flow)
        states = Counter(str(r.get("state", "UNKNOWN")).upper() for r in records)

        diamonds = load_json(DIAMONDS_FILE, [])
        candidates = load_json(CANDIDATES_FILE, [])
        focus = load_json(FOCUS_FILE, {})
        runtime = load_json(RUNTIME_STATUS_FILE, {})
        total_tail, recent15 = count_recent_trades()

        clear()
        print("=" * 72)
        print("POLYMARKET - SYSTEM STATUS")
        print("=" * 72)
        print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        print()
        print(f"Trades in recent log tail : {total_tail:,}")
        print(f"Trades last 15 minutes    : {recent15:,}")
        print(f"Tracked markets           : {len(records):,}")
        print()
        print(f"LOW                       : {states.get('LOW', 0):,}")
        print(f"CANDIDATE                 : {states.get('CANDIDATE', 0):,}")
        print(f"VERIFYING                 : {states.get('VERIFYING', 0):,}")
        print(f"VERIFIED                  : {states.get('VERIFIED', 0):,}")
        print()
        print(f"Watchlist items           : {len(candidates) if isinstance(candidates, list) else 0:,}")
        print(f"Diamonds                  : {len(diamonds) if isinstance(diamonds, list) else 0:,}")
        print(f"Focused market            : {focused_label(focus)}")
        runtime_status = runtime.get("overall_status", "UNKNOWN") if isinstance(runtime, dict) else "UNKNOWN"
        print(f"Runtime supervisor        : {runtime_status}")
        if isinstance(runtime, dict) and isinstance(runtime.get("services"), dict):
            degraded = [
                f"{name}:{service.get('status')}"
                for name, service in runtime["services"].items()
                if isinstance(service, dict)
                and service.get("status") not in {"RUNNING", "DISABLED"}
            ]
            if degraded:
                print(f"Runtime exceptions        : {', '.join(degraded)}")
        print()
        print("FILE HEALTH")
        print(f"live_trades.jsonl         : {file_age(TRADES_FILE)}")
        print(f"flow_state.json           : {file_age(FLOW_FILE)}")
        print(f"diamonds.json             : {file_age(DIAMONDS_FILE)}")
        print(f"risk_assessment.json      : {file_age(RISK_FILE)}")
        print(f"focus_state.json          : {file_age(FOCUS_STATE_FILE)}")
        print(f"focused_market.json       : {file_age(FOCUS_FILE)}")
        print(f"book_assessment.json      : {file_age(BOOK_FILE)}")
        print(f"paper_state.json          : {file_age(PAPER_FILE)}")
        print(f"runtime_status.json       : {file_age(RUNTIME_STATUS_FILE)}")
        print()
        print(f"Refresh: {REFRESH_SECONDS}s | CTRL+C to stop this window")
        print("=" * 72)

        try:
            time.sleep(REFRESH_SECONDS)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
