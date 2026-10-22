"""Read-only, offline recovery of an interrupted GENERAL1-FRESHNESS-1 report.

Reads only the dedicated sandbox's SQLite, JSON snapshots and collector
instrumentation. Does not open RPC, start workers or touch production data.
Recovery ages are as-of the persisted snapshot mtime, not live observations.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import general1_e2e2_live_probe as observer

BASE = Path(r"C:\PolymarketResearch")
SANDBOX_NAME = "GENERAL1_FRESHNESS1_LIVE_SANDBOX"
REPORT_NAME = "general1_freshness1_recovery_report.json"


class RecoveryGateError(RuntimeError):
    pass


def recover(source, destination, *, parent=BASE):
    source, destination, parent = Path(source), Path(destination), Path(parent)
    if (source.name.casefold() != SANDBOX_NAME.casefold()
        or source.is_symlink() or source.resolve().parent != parent.resolve()
        or not (source / ".git").is_dir()):
        raise RecoveryGateError("Not the existing dedicated freshness test sandbox")
    if not (source / "data").is_dir() or (source / "data").is_symlink():
        raise RecoveryGateError("Dedicated test data missing or unsafe")
    if destination.exists() or destination.is_symlink():
        raise RecoveryGateError("Recovery report already exists; refusing overwrite")
    for name in ("collector.sqlite3", "live_trades.jsonl"):
        if (source / "data" / name).is_symlink():
            raise RecoveryGateError("Unexpected symlink in sandbox data")
    data = source / "data"
    snapshot = data / "live_trades.jsonl"
    if not snapshot.is_file():
        raise RecoveryGateError("Live trades snapshot missing")
    snapshot_at = datetime.fromtimestamp(snapshot.stat().st_mtime, timezone.utc)
    db = observer.db_summary(data / "collector.sqlite3")
    records = observer.diagnostic_records(source / "general1_e2e2_logs" / "collector.log")
    freshness = observer.freshness_diagnostics(data, now=snapshot_at)
    freshness["checkpoint_block_age_seconds_at_snapshot"] = (
        observer.checkpoint_block_age_seconds(data / "collector.sqlite3", now=snapshot_at)
    )
    freshness["checkpoint_block_age_seconds_p95"] = None
    freshness["checkpoint_age_sample_count"] = 0
    summary = {
        "task": "GENERAL1-FRESHNESS-1",
        "result": "RECOVERED_UNVERIFIED",
        "recovery_only": True,
        "original_test_status": "UNKNOWN_INTERRUPTED_FINALIZATION",
        "generated_at": observer.utc_now(),
        "as_of_snapshot_mtime_utc": snapshot_at.isoformat(),
        "interpretation": (
            "Snapshot/file mtime is a proxy for data publication, not a sampled "
            "live supervisor interval. Cannot infer original 120s checkpoint-age p95 "
            "or certify full-duration PASS from this recovery."
        ),
        "historical_backfill_complete": False,
        "database": {k: v for k, v in db.items() if not k.startswith("_")},
        "collector": observer.checkpoint_metrics(records, None, None, 0.0),
        "pipeline": observer.pipeline_summary(data, db),
        "freshness": freshness,
        "phase_timings": observer.timing_diagnostics(records),
        "historical_databases_touched": False,
        "worker_logs_not_exported": True,
        "real_orders_placed": False,
    }
    # No absolute paths, addresses, wallets, transaction hashes or secrets.
    destination.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def main():
    source = BASE / SANDBOX_NAME
    output = Path(__file__).resolve().parents[1] / REPORT_NAME
    try:
        result = recover(source, output)
    except RecoveryGateError as exc:
        print(f"[BLOCKED] {exc}")
        return 3
    except (OSError, ValueError, TypeError) as exc:
        print(f"[BLOCKED] Recovery failed ({type(exc).__name__}); no secrets printed")
        return 3
    print(f"[RECOVERY] {result['result']}: report={REPORT_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
