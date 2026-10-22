"""Read-only and secret-safe diagnosis of the first GENERAL1-E2E-2 sandbox.

No network. No data writes. No RPC URLs, markets, wallets, log lines,
transaction hashes or credentials are output. Prints diagnostic COUNTS only.
"""
import collections
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import flow_tracker as flow

SENSITIVE_LOG_TYPES = re.compile(
    r"^\s*(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*(?:Error|Exception|Conflict|Timeout|Failure))(?::|$)"
)


def summarize(root=ROOT):
    root = Path(root)
    report_path = root / "general1_e2e2_live_report.json"
    if root.name.casefold() != "general1_e2e2_live_sandbox":
        raise RuntimeError("Expected original E2E-2 sandbox only")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("task") != "GENERAL1-E2E-2":
        raise RuntimeError("Missing or invalid local report")
    report_time = datetime.fromisoformat(report["generated_at"])
    if report_time.tzinfo is None:
        raise RuntimeError("Report timestamp must be timezone-aware")

    stats = collections.Counter()
    timestamp_types = collections.Counter()
    max_age = None
    min_age = None
    file = root / "data" / "live_trades.jsonl"
    try:
        with file.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    stats["bad_json_lines"] += 1
                    continue
                if not isinstance(row, dict):
                    stats["non_object_lines"] += 1
                    continue
                stats["rows"] += 1
                raw_time = row.get("block_timestamp")
                if isinstance(raw_time, int) and not isinstance(raw_time, bool):
                    timestamp_types["unix_integer"] += 1
                    try:
                        dt = datetime.fromtimestamp(raw_time, timezone.utc)
                    except (OverflowError, ValueError, OSError):
                        stats["invalid_block_time"] += 1
                        continue
                elif isinstance(raw_time, str):
                    timestamp_types["string"] += 1
                    try:
                        dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            raise ValueError
                    except ValueError:
                        stats["invalid_block_time"] += 1
                        continue
                else:
                    timestamp_types["missing_or_other"] += 1
                    stats["invalid_block_time"] += 1
                    continue
                age = (report_time - dt).total_seconds()
                min_age = age if min_age is None else min(min_age, age)
                max_age = age if max_age is None else max(max_age, age)
                if age < 0:
                    stats["future_at_report"] += 1
                elif age <= 15 * 60:
                    stats["within_15_min_at_report"] += 1
                elif age <= 20 * 60:
                    stats["between_15_and_20_min_at_report"] += 1
                else:
                    stats["older_than_20_min_at_report"] += 1
                if flow.is_flow_eligible_trade(row):
                    stats["accepted_by_original_flow_contract"] += 1
                if row.get("collector_version") == 4:
                    stats["v4_rows"] += 1
                if not row.get("condition_id") or not row.get("token_id") or not row.get("outcome"):
                    stats["missing_market_identity"] += 1
                if flow.evidence_identity(row) is None:
                    stats["missing_canonical_evidence_id"] += 1
                if flow.get_trade_side(row) not in {"BUY", "SELL"}:
                    stats["missing_direction"] += 1
                if flow.get_trade_size(row) is None:
                    stats["missing_valid_usd"] += 1
    except FileNotFoundError:
        stats["missing_live_snapshot"] += 1

    types = collections.Counter()
    crash_markers = 0
    log = root / "general1_e2e2_logs" / "collector.log"
    try:
        with log.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("Traceback (most recent call last)"):
                    crash_markers += 1
                match = SENSITIVE_LOG_TYPES.match(line)
                if match:
                    types[match.group(1)] += 1
    except FileNotFoundError:
        pass

    return {
        "task": "GENERAL1-E2E-2 POSTMORTEM",
        "read_only": True, "network_used": False, "secrets_redacted": True,
        "collector_restarts_from_report": report.get("supervisor_status", {}).get(
            "services", {}).get("collector", {}).get("restart_count"),
        "flow_markets_at_end": report.get("pipeline", {}).get("flow_market_count"),
        "timestamp_types": dict(timestamp_types),
        "snapshot_counters": dict(stats),
        "min_block_age_seconds_at_end": round(min_age) if min_age is not None else None,
        "max_block_age_seconds_at_end": round(max_age) if max_age is not None else None,
        "collector_tracebacks": crash_markers,
        "collector_exception_class_counts": dict(types),
    }


if __name__ == "__main__":
    try:
        print(json.dumps(summarize(), indent=2, sort_keys=True))
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
        # Never echo exception message because it might include filesystem or log content.
        print(json.dumps({"result": "POSTMORTEM_UNAVAILABLE", "error_type": type(exc).__name__}))
        raise SystemExit(2)
