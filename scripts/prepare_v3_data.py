import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / "data"
LEGACY = DATA / "legacy"


def archive(path, label):
    if not path.exists() or path.stat().st_size == 0:
        return None
    LEGACY.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = LEGACY / f"{label}_{stamp}{path.suffix}"
    shutil.move(str(path), str(dest))
    print(f"[MIGRATION] Archived {path.name} -> {dest.relative_to(BASE)}")
    return dest


def live_file_is_v3(path):
    if not path.exists() or path.stat().st_size == 0:
        return True
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line=line.strip()
                if not line:
                    continue
                row=json.loads(line)
                return int(row.get("collector_version", 0)) >= 3
    except Exception:
        return False
    return True


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    live = DATA / "live_trades.jsonl"
    if not live_file_is_v3(live):
        print("[MIGRATION] Pre-V3 live trade data detected.")
        print("[MIGRATION] It is preserved, not deleted, because V3 fixes trade-side/USD semantics.")
        archive(live, "live_trades_pre_v3")
        for name in ["flow_state.json", "diamonds.json", "diamond_candidates.json", "diamond_analysis_v3.json", "verification_state.json"]:
            archive(DATA/name, name.rsplit('.',1)[0] + "_pre_v3")
        live.touch()
    else:
        live.touch(exist_ok=True)

    for name, default in [
        ("flow_state.json", {}),
        ("diamonds.json", []),
        ("diamond_candidates.json", []),
        ("diamond_analysis_v3.json", []),
        ("verification_state.json", {}),
        ("paper_trades.json", []),
        ("telegram_state.json", {"chat_ids": [], "active_diamonds": [], "cashflow_levels": {}}),
    ]:
        p=DATA/name
        if not p.exists() or p.stat().st_size == 0:
            p.write_text(json.dumps(default, indent=2), encoding="utf-8")
    print("[MIGRATION] V3 data layout ready.")

if __name__ == "__main__":
    main()
