"""GENERAL1-FRESHNESS-1 offline safety and metric tests. No RPC or workers."""
import json
from datetime import datetime, timezone

from scripts import general1_e2e2_live_probe as observer
from scripts import general1_freshness1_live_probe as freshness


def test_neartip_env_never_inherits_prod_block_controls(monkeypatch):
    monkeypatch.setenv("COLLECTOR_START_BLOCK", "1")
    monkeypatch.setenv("COLLECTOR_CONFIRMATION_BLOCKS", "1")
    monkeypatch.setenv("COLLECTOR_INITIAL_LOOKBACK_BLOCKS", "999999")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "PRIVATE")
    env = observer.controlled_neartip_environment("https://provider.example/private")
    assert env["COLLECTOR_INITIAL_LOOKBACK_BLOCKS"] == "30"
    assert env["COLLECTOR_CONFIRMATION_BLOCKS"] == "20"
    assert "COLLECTOR_START_BLOCK" not in env
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert env["PYTHONIOENCODING"] == "utf-8:backslashreplace"


def test_old_e2e2_env_is_not_modified_by_neartip_mode(monkeypatch):
    monkeypatch.delenv("COLLECTOR_INITIAL_LOOKBACK_BLOCKS", raising=False)
    env = observer.worker_process_environment("https://provider.example/private")
    assert "COLLECTOR_INITIAL_LOOKBACK_BLOCKS" not in env


def test_neartip_wrapper_guards_and_restores_legacy_constants(monkeypatch):
    assert freshness.main(["--dashboard"]) == 3
    assert freshness.main(["--near-tip"]) == 3
    old_name, old_report = observer.EXPECTED_NAME, observer.REPORT_NAME
    calls = []

    def fake_main(args):
        calls.append((observer.EXPECTED_NAME, observer.REPORT_NAME, args))
        return 3

    monkeypatch.setattr(observer, "main", fake_main)
    assert freshness.main(["--duration", "60"]) == 3
    assert calls == [("GENERAL1_FRESHNESS1_LIVE_SANDBOX",
                      "general1_freshness1_live_report.json",
                      ["--near-tip", "--duration", "60"])]
    assert observer.EXPECTED_NAME == old_name
    assert observer.REPORT_NAME == old_report


def test_freshness_metric_chain_age_deduplicates_and_never_uses_detected_at(tmp_path):
    now = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)
    ts = int(now.timestamp())
    def event(char, idx, age, **extra):
        return dict(
            collector_version=4,
            transaction_hash="0x" + char * 64,
            log_index=idx,
            block_timestamp=ts - age,
            detected_at=now.isoformat(),
            token_id="PRIVATE-TOKEN", condition_id="PRIVATE-CONDITION",
            **extra
        )
    rows = [
        event("a", 0, 40),
        event("a", 0, 40),  # duplicate event
        event("b", 1, 250),
        event("c", 2, 700),
        event("d", 3, -120),  # future, never fresh
        {**event("e", 4, 50), "block_timestamp": None},
    ]
    (tmp_path / "live_trades.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )
    (tmp_path / "flow_state.json").write_text(json.dumps({
        "PRIVATE-TOKEN": {
            "verified": False,
            "flow_1m": {"trade_count": 1},
            "flow_5m": {"trade_count": 2},
            "flow_15m": {"trade_count": 3},
        }
    }), encoding="utf-8")
    (tmp_path / "diamond_analysis_v3.json").write_text(
        json.dumps([{"classification":"DATA_RISK"}]), encoding="utf-8"
    )
    (tmp_path / "risk_assessment.json").write_text(
        json.dumps({"results":[{"decision":"BLOCK", "reason_codes":["STALE_TRADE"]}]}),
        encoding="utf-8"
    )
    report = observer.freshness_diagnostics(tmp_path, now=now)
    assert report["snapshot_window_trades"] == {"1m": 1, "5m": 2, "15m": 3}
    assert report["snapshot_unique_v4_trades"] == 3
    assert report["invalid_block_timestamps"] == 1
    assert report["future_block_timestamps"] == 1
    assert report["ages_over_120s"] == 2
    assert report["flow_markets_with_trades"] == {"1m":1, "5m":1, "15m":1}
    assert report["risk_reason_codes_top15"] == [["STALE_TRADE", 1]]
    assert "PRIVATE" not in json.dumps(report)


def test_missing_snapshot_never_claims_fresh_trades(tmp_path):
    report = observer.freshness_diagnostics(tmp_path)
    assert report["snapshot_status"] == "MISSING_OR_UNREADABLE"
    assert report["snapshot_unique_v4_trades"] == 0


def test_existing_timing_phases_are_aggregated_without_double_count_claims():
    records = [
        {"status":"SUCCESS","batch_ms":1000.0, "rpc_ms":800.0,
         "get_logs_ms":400.0,"gamma_lookup_ms":500.0, "backoff_ms":0},
        {"status":"SUCCESS","batch_ms":2000.0, "rpc_ms":1800.0,
         "get_logs_ms":700.0,"gamma_lookup_ms":700.0, "backoff_ms":0},
        {"status":"RETRYABLE_FAILURE","batch_ms":500.0, "backoff_ms":2000},
    ]
    report = observer.timing_diagnostics(records)
    assert report["successful_batches"] == 2
    assert report["phases_ms"]["batch_ms"]["total"] == 3000.0
    assert report["phases_ms"]["rpc_ms"]["p95"] == 1750.0
    assert report["phases_ms"]["backoff_ms"]["total"] == 2000.0
    assert "overlap" in report["note"].lower()
