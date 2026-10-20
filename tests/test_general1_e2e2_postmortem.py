"""Redacted postmortem must never echo RPC URLs or raw trade identifiers."""
import json
from datetime import datetime, timezone

from scripts import general1_e2e2_postmortem as pm


def test_postmortem_counts_real_timestamp_shape_and_redacts_logs(tmp_path):
    root = tmp_path / "GENERAL1_E2E2_LIVE_SANDBOX"
    (root / "data").mkdir(parents=True)
    (root / "general1_e2e2_logs").mkdir()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    (root / "general1_e2e2_live_report.json").write_text(json.dumps({
        "task": "GENERAL1-E2E-2",
        "generated_at": now.isoformat(),
        "supervisor_status": {"services": {"collector": {"restart_count": 6}}},
        "pipeline": {"flow_market_count": 0},
    }), encoding="utf-8")
    token = "sensitive_token_data"
    row = {
        "collector_version": 4,
        "condition_id": token,
        "token_id": token,
        "outcome": "YES",
        "transaction_hash": "0x" + "fa"*32,
        "log_index": 0,
        "block": 123,
        "block_timestamp": int(now.timestamp()),
        "side_label": "BUY",
        "trade_usd": 100.0,
    }
    (root / "data" / "live_trades.jsonl").write_text(json.dumps(row)+"\n", encoding="utf-8")
    (root / "general1_e2e2_logs" / "collector.log").write_text(
        "Traceback (most recent call last):\n"
        "requests.exceptions.HTTPError: https://example.org/rpc/SECRET-KEY\n"
        "GammaIdentityConflict: private key: SECRET-KEY\n",
        encoding="utf-8"
    )
    summary = pm.summarize(root)
    assert summary["timestamp_types"]["unix_integer"] == 1
    assert summary["snapshot_counters"]["rows"] == 1
    assert summary["snapshot_counters"]["within_15_min_at_report"] == 1
    assert summary["collector_exception_class_counts"]["HTTPError"] == 1
    assert summary["collector_exception_class_counts"]["GammaIdentityConflict"] == 1
    assert summary["collector_tracebacks"] == 1
    assert summary["collector_restarts_from_report"] == 6
    serialized = json.dumps(summary)
    assert "SECRET-KEY" not in serialized
    assert token not in serialized
    assert "0x" + "fa"*32 not in serialized
