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


def test_checkpoint_age_is_none_when_no_db(tmp_path):
    assert observer.checkpoint_block_age_seconds(tmp_path / "absent.sqlite3") is None


def test_checkpoint_age_uses_canonical_block_time(tmp_path):
    import sqlite3
    db = tmp_path / "fresh.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE blocks(chain_id INTEGER, block_number INTEGER, block_timestamp INTEGER)")
        con.execute("INSERT INTO blocks VALUES (137, 100, 1700000000)")
        con.execute("INSERT INTO blocks VALUES (137, 101, 1700000040)")
    now = datetime.fromtimestamp(1700000100, timezone.utc)
    assert observer.checkpoint_block_age_seconds(db, now=now) == 60.0


def test_neartip_requires_real_freshness_for_pass(tmp_path, monkeypatch):
    """Positive core linkage cannot produce PASS if block evidence is stale."""
    p = tmp_path / observer.EXPECTED_NAME
    p.mkdir()
    (p / ".git").mkdir()
    monkeypatch.setattr(observer, "ROOT", p)
    monkeypatch.setattr(observer, "isolation_gate", lambda *_: True)
    monkeypatch.setattr(observer.subprocess, "run", lambda *args, **kwargs: None)
    ticks = iter((0, 1, 2, 90, 100, 110))
    monkeypatch.setattr(observer.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(observer.time, "sleep", lambda *_: None)
    heads = iter((1000, 1010))
    monkeypatch.setattr(observer, "rpc_number", lambda rpc, method:
                        137 if method == "eth_chainId" else next(heads))
    monkeypatch.setattr(observer, "diagnostic_records", lambda *_: [
        {"status":"SUCCESS", "blocks":100, "lag_before":100, "lag_after":0}])
    monkeypatch.setattr(observer, "db_summary", lambda *_: {
        "quick_check_ok":True, "checkpoint":990, "_evidence":[("t","c")]})
    monkeypatch.setattr(observer, "pipeline_summary", lambda *_: {
        "real_trade_to_risk_binding_observed":True, "paper_positions":0})
    monkeypatch.setattr(observer, "freshness_diagnostics", lambda *_: {
        "snapshot_window_trades":{"1m":0, "5m":0, "15m":3},
        "flow_markets_with_trades":{"1m":0, "5m":0, "15m":1}})
    monkeypatch.setattr(observer, "checkpoint_block_age_seconds", lambda *_: 600)
    monkeypatch.setattr(observer.run_machine, "build_services", lambda: [
        observer.ServiceSpec(name, (name,), critical=True)
        for name in sorted(observer.CORE)])
    class Supervisor:
        def __init__(self, specs, **kwargs):
            pass
        def start_all(self):
            pass
        def poll_once(self):
            return None
        def snapshot(self):
            return {"overall_status":"RUNNING",
                    "services":{name:{
                        "status":"RUNNING","restart_count":0,"consecutive_failures":0,
                        "critical":True,"enabled":True}
                        for name in observer.CORE}}
        def shutdown(self):
            pass
    monkeypatch.setattr(observer, "RuntimeSupervisor", Supervisor)
    assert observer.run_live(60, "https://provider.example/no-real-rpc", near_tip=True) == 2
    report = json.loads((p / observer.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["result"] == "PARTIAL"
    assert report["experiment"]["historical_backfill_complete"] is False
    assert report["freshness"]["checkpoint_block_age_seconds_p95"] == 600.0
    assert report["freshness"]["snapshot_window_trades"]["5m"] == 0


def _canonical_v4_event(now, seconds_old, block, hex_byte, side="BUY"):
    return {
        "collector_version": 4,
        "transaction_hash": "0x" + format(hex_byte, "02x") * 32,
        "log_index": 0,
        "block": block,
        "block_timestamp": int(now.timestamp()) - seconds_old,
        "detected_at": now.isoformat(),
        "condition_id": "condition-a",
        "token_id": "token-a",
        "outcome": "Yes",
        "side_label": side,
        "trade_usd": 120.0,
    }


def test_old_chain_evidence_cannot_create_1m_or_5m_signal():
    from scripts import flow_tracker as flow
    from scripts import risk_engine as risk
    from scripts import focus_engine as focus
    now = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)
    old = _canonical_v4_event(now, 700, 100, 17)
    assert flow.is_flow_eligible_trade(old)
    assert flow.analyze_flow([old], now, "1m")["trade_count"] == 0
    assert flow.analyze_flow([old], now, "5m")["trade_count"] == 0
    assert flow.analyze_flow([old], now, "15m")["trade_count"] == 1
    assert flow.confirmation_evidence_id(
        old, flow.evidence_identity(old), True, "NEUTRAL"
    ) is None
    rejected = risk.assess(
        {"classification": "DATA_RISK", "diamond": False, "direction": "NEUTRAL"},
        {},
        now=now,
    )
    assert rejected["decision"] == "BLOCK"
    assert focus._eligible(rejected, focus.new_state()) is False


def test_fresh_chain_evidence_populates_windows_and_preserves_evidence_triple():
    from scripts import flow_tracker as flow
    from scripts import diamond_filter_v3 as diamond
    now = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)
    trades = [
        _canonical_v4_event(now, 200, 100, 18),
        _canonical_v4_event(now, 45, 101, 19),
        _canonical_v4_event(now, 5, 102, 20),
    ]
    for row in trades:
        assert flow.is_flow_eligible_trade(row)
    assert flow.analyze_flow(trades, now, "1m")["trade_count"] == 2
    assert flow.analyze_flow(trades, now, "5m")["trade_count"] == 3
    assert flow.analyze_flow(trades, now, "15m")["trade_count"] == 3
    assert flow.analyze_flow(trades, now, "5m")["direction"] == "BUY"
    latest = max(trades, key=flow.trade_order_key)
    identity = flow.evidence_identity(latest)
    confirmed = flow.confirmation_evidence_id(latest, identity, True, "BUY")
    evidence = flow.confirmation_evidence_triple(latest, confirmed)
    assert evidence["evidence_id"] == identity
    assert evidence["evidence_cursor"] == [102, 0]
    assert evidence["evidence_at"] == datetime.fromtimestamp(
        latest["block_timestamp"], timezone.utc
    ).isoformat()
    assert diamond._canonical_block_time(latest["block_timestamp"]) == evidence["evidence_at"]


def _recovery_fixture(tmp_path):
    import sqlite3
    source = tmp_path / "GENERAL1_FRESHNESS1_LIVE_SANDBOX"
    (source / ".git").mkdir(parents=True)
    data = source / "data"
    data.mkdir()
    (source / "general1_e2e2_logs").mkdir()
    db = data / "collector.sqlite3"
    payload = {
        "collector_version": 4,
        "condition_id": "PRIVATE-CONDITION",
        "token_id": "PRIVATE-TOKEN",
        "transaction_hash": "0x" + "a" * 64,
        "log_index": 1,
        "block_timestamp": 1700000000,
    }
    with sqlite3.connect(db) as c:
        c.executescript("""
        CREATE TABLE checkpoint(chain_id INTEGER PRIMARY KEY, block_number INTEGER, block_hash TEXT);
        CREATE TABLE blocks(chain_id INTEGER, block_number INTEGER, block_timestamp INTEGER);
        CREATE TABLE trades(chain_id INTEGER, block_number INTEGER, log_index INTEGER,
            payload_json TEXT);
        CREATE INDEX idx_trades_block ON trades(chain_id,block_number,log_index);
        """)
        c.execute("INSERT INTO checkpoint VALUES (137,101,'hash')")
        c.execute("INSERT INTO blocks VALUES (137,101,1700000000)")
        c.execute("INSERT INTO trades VALUES (137,101,1,?)", (json.dumps(payload),))
    (data / "live_trades.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    (source / "general1_e2e2_logs" / "collector.log").write_text(
        '[LIVE-1C] {"status":"SUCCESS","blocks":5,"lag_before":30,"lag_after":25,"batch_ms":400}\n',
        encoding="utf-8")
    return source


def test_db_postflight_chain_index_and_correct_readonly_evidence(tmp_path):
    import sqlite3
    source = _recovery_fixture(tmp_path)
    db = source / "data" / "collector.sqlite3"
    info = observer.db_summary(db)
    assert info["quick_check_ok"] is True
    assert info["checkpoint"] == 101
    assert info["blocks"] == 1
    assert info["trades"] == 1
    assert info["_evidence"] == [("PRIVATE-TOKEN", "PRIVATE-CONDITION")]
    with sqlite3.connect(db) as c:
        plan = " ".join(str(row) for row in c.execute(
            "EXPLAIN QUERY PLAN SELECT payload_json FROM trades WHERE chain_id=137 "
            "ORDER BY block_number DESC, log_index DESC LIMIT 500"))
    assert "idx_trades_block" in plan
    assert "USE TEMP B-TREE" not in plan


def test_recovery_offline_preserves_data_and_no_fake_live_pass(tmp_path):
    from scripts import general1_freshness1_recover as recovery
    source = _recovery_fixture(tmp_path)
    report = tmp_path / "recovered.json"
    before = (source / "data" / "collector.sqlite3").read_bytes()
    result = recovery.recover(source, report, parent=tmp_path)
    assert result["result"] == "RECOVERED_UNVERIFIED"
    assert result["original_test_status"] == "UNKNOWN_INTERRUPTED_FINALIZATION"
    assert result["collector"]["collector_blocks_per_second"] is None
    assert result["collector"]["blocks_processed"] == 5
    assert result["database"]["quick_check_ok"] is True
    assert result["historical_backfill_complete"] is False
    assert report.is_file()
    assert (source / "data" / "collector.sqlite3").read_bytes() == before
    assert "PRIVATE-TOKEN" not in report.read_text(encoding="utf-8")
    assert "PRIVATE-CONDITION" not in report.read_text(encoding="utf-8")
    try:
        recovery.recover(source, report, parent=tmp_path)
    except recovery.RecoveryGateError:
        pass
    else:
        raise AssertionError("Recovery must refuse overwriting the report")


def test_db_postflight_timeout_does_not_raise(tmp_path, monkeypatch):
    source = _recovery_fixture(tmp_path)
    # The timeout guard fails closed instead of blocking a final report forever.
    report = observer.db_summary(source / "data" / "collector.sqlite3", budget_seconds=-1)
    assert report["exists"] is True
    assert report["quick_check_ok"] is False
    assert report["error_type"] == "OperationalError"


def test_recovery_script_imports_from_other_cwd_without_rpc(tmp_path):
    """CLI smoke test: no installed project, network, production data or workers."""
    import subprocess
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "scripts" / "general1_freshness1_recover.py"
    completed = subprocess.run(
        [sys.executable, "-B", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 3
    assert "[BLOCKED]" in completed.stdout
    assert "ModuleNotFoundError" not in completed.stderr
