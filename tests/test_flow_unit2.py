import contextlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import flow_tracker as flow



def test_trade_size_never_treats_shares_as_usd():
    assert flow.get_trade_size({"token_amount": 100}) is None
    assert flow.get_trade_size({"token_amount": 100, "fill_price": 0.42}) == 42.0
    assert flow.get_trade_size({"trade_usd": 12.5, "token_amount": 100, "fill_price": 0.42}) == 12.5


def _canonical_v4(now=None, **overrides):
    now = now or datetime.now(timezone.utc)
    row = {
        "collector_version": 4,
        "condition_id": "condition",
        "token_id": "yes-token",
        "outcome": "Yes",
        "transaction_hash": "0x" + "ab" * 32,
        "log_index": 4,
        "block": 123,
        "block_timestamp": now.isoformat(),
        "side_label": "BUY",
        "trade_usd": 100.0,
    }
    row.update(overrides)
    return row


def test_collector_version_contract_fails_closed_for_unknown_future_versions():
    now = datetime.now(timezone.utc)

    assert flow.is_flow_eligible_trade(_canonical_v4(now)) is True

    for version in (5, 999, 2, "4", "future", True):
        row = _canonical_v4(now, collector_version=version)
        assert flow.is_flow_eligible_trade(row) is False

    legacy_v3 = _canonical_v4(now, collector_version=3)
    legacy_v3.pop("block")
    assert flow.is_flow_eligible_trade(legacy_v3) is True

    missing_version = _canonical_v4(now)
    missing_version.pop("collector_version")
    missing_version.pop("block")
    assert flow.is_flow_eligible_trade(missing_version) is True


def test_v4_missing_block_is_rejected():
    now = datetime.now(timezone.utc)
    row = _canonical_v4(now)
    row.pop("block")
    assert flow.is_flow_eligible_trade(row) is False
    result = flow.analyze_flow([row], now, "5m")
    assert result["trade_count"] == 0
    assert result["total_volume"] == 0.0


def test_v4_missing_transaction_hash_does_not_affect_flow():
    now = datetime.now(timezone.utc)
    row = _canonical_v4(now)
    row.pop("transaction_hash")
    result = flow.analyze_flow([row], now, "5m")
    assert result["trade_count"] == 0
    assert result["total_volume"] == 0.0
    assert result["net_flow"] == 0.0


def test_v4_missing_log_index_does_not_affect_flow():
    now = datetime.now(timezone.utc)
    row = _canonical_v4(now)
    row.pop("log_index")
    result = flow.analyze_flow([row], now, "5m")
    assert result["trade_count"] == 0
    assert result["total_volume"] == 0.0


def test_v4_noncanonical_transaction_hash_is_rejected_before_flow():
    now = datetime.now(timezone.utc)
    invalid_hashes = (
        "0xa",
        "0xabc",
        "ab" * 32,
        "0x" + "ab" * 31,
        "0x" + "ab" * 33,
        "0x" + "ag" * 32,
    )
    for transaction_hash in invalid_hashes:
        row = _canonical_v4(now, transaction_hash=transaction_hash)
        assert flow.evidence_identity(row) is None
        assert flow.is_flow_eligible_trade(row) is False
        result = flow.analyze_flow([row], now, "5m")
        assert result["trade_count"] == 0
        assert result["total_volume"] == 0.0


def test_v4_missing_block_timestamp_does_not_use_detected_at():
    now = datetime.now(timezone.utc)
    row = _canonical_v4(now, detected_at=now.isoformat())
    row.pop("block_timestamp")
    assert flow.is_flow_eligible_trade(row) is False
    result = flow.analyze_flow([row], now, "5m")
    assert result["trade_count"] == 0
    assert result["total_volume"] == 0.0


def test_v4_unknown_usd_size_is_none_and_does_not_affect_flow():
    now = datetime.now(timezone.utc)
    row = _canonical_v4(now)
    row.pop("trade_usd")
    row["token_amount"] = 100
    row["fill_price"] = 0.42
    assert flow.get_trade_size(row) is None
    result = flow.analyze_flow([row], now, "5m")
    assert result["trade_count"] == 0
    assert result["total_volume"] == 0.0
    assert result["net_flow"] == 0.0


def test_v4_legitimate_zero_usd_is_known_not_unknown():
    now = datetime.now(timezone.utc)
    row = _canonical_v4(now, trade_usd=0)
    assert flow.get_trade_size(row) == 0.0
    assert flow.is_flow_eligible_trade(row) is True


def test_v4_future_timestamp_does_not_enter_window():
    now = datetime.now(timezone.utc)
    row = _canonical_v4(now, block_timestamp=(now + timedelta(seconds=1)).isoformat())
    result = flow.analyze_flow([row], now, "5m")
    assert result["trade_count"] == 0
    assert result["total_volume"] == 0.0


def test_canonical_v4_rows_keep_existing_flow_math():
    now = datetime.now(timezone.utc)
    rows = [
        _canonical_v4(now, transaction_hash="0x" + "aa" * 32, log_index=1, block=101, trade_usd=100.0, side_label="BUY"),
        _canonical_v4(now, transaction_hash="0x" + "bb" * 32, log_index=2, block=102, trade_usd=40.0, side_label="SELL"),
    ]
    result = flow.analyze_flow(rows, now, "5m")
    assert result["trade_count"] == 2
    assert result["buy_volume"] == 100.0
    assert result["sell_volume"] == 40.0
    assert result["total_volume"] == 140.0
    assert result["net_flow"] == 60.0
    assert result["direction"] == "BUY"

def test_event_time_prefers_block_timestamp():
    now = datetime.now(timezone.utc)
    trade = {
        "block_timestamp": (now - timedelta(minutes=10)).isoformat(),
        "detected_at": now.isoformat(),
    }
    assert flow.event_time(trade) < now - timedelta(minutes=9)


def test_confirmation_evidence_only_when_canonical_and_supporting():
    trade = {"side_label": "SELL", "transaction_hash": "0x" + "ab" * 32, "log_index": 4}
    evidence = flow.market_evidence_id(trade)
    assert flow.confirmation_evidence_id(trade, evidence, True, "BUY") is None
    assert flow.confirmation_evidence_id(trade, evidence, True, "SELL") == evidence
    assert flow.confirmation_evidence_id(trade, evidence, False, "SELL") is None

    legacy = {
        "side_label": "BUY",
        "token_id": "yes",
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }
    legacy_evidence = flow.market_evidence_id(legacy)
    assert flow.confirmation_evidence_id(legacy, legacy_evidence, True, "BUY") is None


def test_update_market_state_separates_tokens_and_direction():
    old = flow.market_states
    try:
        flow.market_states = {}
        for e in ("a", "b", "c"):
            flow.update_market_state("yes-token", True, True, e, "BUY")
        flow.update_market_state("no-token", True, True, "n1", "SELL")
        assert flow.market_states["yes-token"]["state"] == "VERIFIED"
        assert flow.market_states["yes-token"]["confirmations"] == 3
        assert flow.market_states["no-token"]["confirmations"] == 1
        assert flow.market_states["yes-token"]["direction"] == "BUY"
        assert flow.market_states["no-token"]["direction"] == "SELL"
    finally:
        flow.market_states = old


def test_main_persists_token_keys_and_last_trade_at(tmp_path):
    now = datetime.now(timezone.utc)
    rows = []
    for token, outcome in (("yes-token", "Yes"), ("no-token", "No")):
        rows.append({
            "condition_id": "same-condition",
            "token_id": token,
            "outcome": outcome,
            "question": "TEST",
            "side_label": "BUY",
            "trade_usd": 100.0,
            "fill_price": 0.4,
            "block_timestamp": now.isoformat(),
            "detected_at": now.isoformat(),
            "transaction_hash": f"0x{token}",
            "log_index": 1,
            "end_date": (now + timedelta(days=10)).isoformat(),
            "active": True,
            "closed": False,
            "accepting_orders": True,
        })

    trades = tmp_path / "trades.jsonl"
    flow_file = tmp_path / "flow.json"
    verify_file = tmp_path / "verify.json"
    trades.write_text("\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8")

    old = (flow.TRADES_FILE, flow.FLOW_STATE_FILE, flow.VERIFICATION_STATE_FILE,
           flow.market_states, flow.flow_states, flow.analysis_schedule)
    try:
        flow.TRADES_FILE = trades
        flow.FLOW_STATE_FILE = flow_file
        flow.VERIFICATION_STATE_FILE = verify_file
        flow.market_states = {}
        flow.flow_states = {}
        flow.analysis_schedule = {}
        with patch.object(flow.time, "sleep", side_effect=InterruptedError), contextlib.redirect_stdout(io.StringIO()):
            try:
                flow.main()
            except InterruptedError:
                pass
        state = json.loads(flow_file.read_text(encoding="utf-8"))
        assert set(state) == {"yes-token", "no-token"}
        assert set(flow.market_states) == {"yes-token", "no-token"}
        assert all(v["market"]["last_trade_at"] == now.isoformat() for v in state.values())
    finally:
        (flow.TRADES_FILE, flow.FLOW_STATE_FILE, flow.VERIFICATION_STATE_FILE,
         flow.market_states, flow.flow_states, flow.analysis_schedule) = old


def test_focus_evidence_metadata_is_exported_only_for_counted_confirmation():
    old_states = flow.market_states
    old_flow_states = flow.flow_states
    try:
        flow.market_states = {}
        flow.flow_states = {}
        evidence_at = "2026-10-05T08:00:00+00:00"
        state = flow.update_market_state(
            "yes-token", True, True, "0xabc:4", "BUY",
            evidence_cursor=[123, 4], evidence_at=evidence_at,
        )
        assert state["last_confirmation_evidence"] == "0xabc:4"
        assert state["last_confirmation_cursor"] == [123, 4]
        assert state["last_confirmation_at"] == evidence_at

        state = flow.update_market_state(
            "yes-token", True, True, "0xabc:4", "BUY",
            evidence_cursor=[124, 1], evidence_at="2026-10-05T08:01:00+00:00",
        )
        assert state["last_confirmation_cursor"] == [123, 4]
        assert state["last_confirmation_at"] == evidence_at

        state = flow.update_market_state(
            "yes-token", True, True, None, "BUY",
            evidence_cursor=[125, 2], evidence_at="2026-10-05T08:02:00+00:00",
        )
        assert state["last_confirmation_evidence"] == "0xabc:4"

        flow.save_flow_state(
            token_id="yes-token",
            condition_id="condition-1",
            market={"outcome": "Yes", "last_trade_at": evidence_at},
            flow_1m={},
            flow_5m={"direction": "BUY"},
            flow_15m={},
            interest_score=90,
            data_confidence=95,
            resolution_state="ACTIVE",
            remaining_seconds=3600,
            analysis_profile="NEAR",
            analysis_interval_seconds=5,
            state_data=state,
            candidate=True,
            candidate_reasons=[],
            verified=True,
            verification_reasons=[],
        )
        saved = flow.flow_states["yes-token"]
        assert saved["evidence_id"] == "0xabc:4"
        assert saved["evidence_cursor"] == [123, 4]
        assert saved["evidence_at"] == evidence_at
    finally:
        flow.market_states = old_states
        flow.flow_states = old_flow_states

def test_live_collector_v4_integer_epoch_timestamp_is_flow_eligible():
    """Real collector encodes Polygon block_timestamp as integer Unix seconds."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    row = _canonical_v4(now, block_timestamp=int(now.timestamp()))
    assert flow.is_flow_eligible_trade(row) is True
    assert flow.event_time(row) == now
    assert flow.analyze_flow([row], now, "5m")["trade_count"] == 1


def test_live_v4_epoch_timestamp_remains_fail_closed_for_invalid_or_old_values():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for raw in (True, -1, 10**100, None):
        assert flow.is_flow_eligible_trade(_canonical_v4(now, block_timestamp=raw)) is False
    old = _canonical_v4(now, block_timestamp=int((now - timedelta(minutes=25)).timestamp()),
                        detected_at=now.isoformat())
    assert flow.is_flow_eligible_trade(old) is True
    assert flow.analyze_flow([old], now, "15m")["trade_count"] == 0


def test_real_collector_epoch_is_normalized_for_diamond_freshness(tmp_path):
    from machine_common import fresh
    now = datetime.now(timezone.utc).replace(microsecond=0)
    row = _canonical_v4(now,
        block_timestamp=int(now.timestamp()),
        detected_at=now.isoformat(),
        question="LIVE BLOCK TIME CONTRACT",
        end_date=(now + timedelta(days=10)).isoformat(),
        active=True, closed=False, accepting_orders=True,
        fill_price=.44,
    )
    trade_file = tmp_path / "real_collector.jsonl"
    flow_file = tmp_path / "flow_state.json"
    verify_file = tmp_path / "verification_state.json"
    trade_file.write_text(json.dumps(row) + "\n", encoding="utf-8")
    old = (flow.TRADES_FILE, flow.FLOW_STATE_FILE, flow.VERIFICATION_STATE_FILE,
           flow.market_states, flow.flow_states, flow.analysis_schedule)
    try:
        flow.TRADES_FILE = trade_file
        flow.FLOW_STATE_FILE = flow_file
        flow.VERIFICATION_STATE_FILE = verify_file
        flow.market_states = {}
        flow.flow_states = {}
        flow.analysis_schedule = {}
        with patch.object(flow.time, "sleep", side_effect=InterruptedError), contextlib.redirect_stdout(io.StringIO()):
            try:
                flow.main()
            except InterruptedError:
                pass
        state = json.loads(flow_file.read_text(encoding="utf-8"))
        assert "yes-token" in state
        entry = state["yes-token"]
        assert entry["schema_version"] == 4
        assert entry["market"]["last_trade_at"] == now.isoformat()
        assert entry["last_trade_at"] == now.isoformat()
        assert fresh(entry["market"]["last_trade_at"]) is True
        assert entry["condition_id"] == row["condition_id"]
        assert entry["token_id"] == row["token_id"]
    finally:
        (flow.TRADES_FILE, flow.FLOW_STATE_FILE, flow.VERIFICATION_STATE_FILE,
         flow.market_states, flow.flow_states, flow.analysis_schedule) = old


def test_collector_epoch_jsonl_is_ingested_without_using_detection_time(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    valid = _canonical_v4(now, block_timestamp=int(now.timestamp()), detected_at=now.isoformat())
    stale = _canonical_v4(now, transaction_hash="0x" + "cd" * 32, log_index=5,
        block_timestamp=int((now - timedelta(minutes=35)).timestamp()), detected_at=now.isoformat())
    file = tmp_path / "trades.jsonl"
    file.write_text(json.dumps(valid) + "\n" + json.dumps(stale) + "\n", encoding="utf-8")
    old_file = flow.TRADES_FILE
    try:
        flow.TRADES_FILE = file
        seen = flow.load_trades()
        assert len(seen) == 1
        assert seen[0]["transaction_hash"] == valid["transaction_hash"]
    finally:
        flow.TRADES_FILE = old_file
