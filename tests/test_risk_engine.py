"""Risk Engine regression tests — Unit 4 pre-freeze."""
from datetime import datetime, timedelta, timezone

from scripts.risk_engine import assess

NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
FRESH_TS = (NOW - timedelta(seconds=30)).isoformat()


def make_flow(**overrides):
    flow = {
        "schema_version": 4,
        "token_id": "yes-token",
        "condition_id": "condition",
        "outcome": "Yes",
        "direction": "BUY",
        "verified": True,
        "confirmations": 3,
        "source_updated_at": FRESH_TS,
        "last_trade_at": FRESH_TS,
        "remaining_seconds": 86400,
        "evidence_id": "0xabababababababababababababababababababababababababababababababab:4",
        "evidence_cursor": [123, 4],
        "evidence_at": FRESH_TS,
        "market": {"active": True, "closed": False, "accepting_orders": True},
    }
    flow.update(overrides)
    return flow


def make_row(**overrides):
    row = {
        "schema_version": 4,
        "source_updated_at": FRESH_TS,
        "last_trade_at": FRESH_TS,
        "market_key": "condition:condition|outcome:yes",
        "question": "TEST MARKET",
        "condition_id": "condition",
        "token_id": "yes-token",
        "outcome": "Yes",
        "direction": "BUY",
        "price": 0.42,
        "classification": "DIAMOND",
        "diamond": True,
        "why_not_diamond": [],
        "metrics": {"verified": True, "confirmations": 3, "largest_trade_ratio": 0.20},
        "resolution": {"remaining_seconds": 86400},
        "cashflow_alert": {"quality": "BROAD"},
    }
    row.update(overrides)
    return row


def codes(result):
    return set(result["reason_codes"])


def test_valid_all_around_pass():
    assert assess(make_row(), {"yes-token": make_flow()}, now=NOW)["decision"] == "PASS"


def test_replay_clock_pass():
    assert assess(make_row(), {"yes-token": make_flow()}, now=NOW)["decision"] == "PASS"


def test_replay_clock_with_live_clock_blocked():
    r = assess(make_row(), {"yes-token": make_flow()})
    assert r["decision"] == "BLOCK"
    assert "STALE_DIAMOND" in codes(r)


def test_non_diamond_classification():
    r = assess(make_row(classification="VERIFYING"), {"yes-token": make_flow()}, now=NOW)
    assert r["decision"] == "BLOCK" and "NOT_DIAMOND" in codes(r)


def test_diamond_confirmations_low():
    r = assess(make_row(metrics={"verified": True, "confirmations": 2, "largest_trade_ratio": 0.2}), {"yes-token": make_flow()}, now=NOW)
    assert r["decision"] == "BLOCK" and "CONFIRMATIONS_LOW" in codes(r)


def test_whale_concentration_blocked():
    r = assess(make_row(metrics={"verified": True, "confirmations": 3, "largest_trade_ratio": 0.85}), {"yes-token": make_flow()}, now=NOW)
    assert r["decision"] == "BLOCK" and "WHALE_CONCENTRATION" in codes(r)


def test_sell_direction_blocked():
    assert assess(make_row(direction="SELL"), {"yes-token": make_flow()}, now=NOW)["decision"] == "BLOCK"


def test_expired_market_blocked():
    r = assess(make_row(resolution={"remaining_seconds": -1}), {"yes-token": make_flow()}, now=NOW)
    assert r["decision"] == "BLOCK" and "MARKET_EXPIRED" in codes(r)


def test_flow_confirmations_low_blocked():
    r = assess(make_row(), {"yes-token": make_flow(confirmations=2)}, now=NOW)
    assert r["decision"] == "BLOCK" and "FLOW_CONFIRMATIONS_LOW" in codes(r)


def test_flow_direction_divergent_blocked():
    r = assess(make_row(), {"yes-token": make_flow(direction="SELL")}, now=NOW)
    assert r["decision"] == "BLOCK" and "FLOW_DIRECTION_DIVERGENT" in codes(r)


def test_flow_unverified_blocked():
    r = assess(make_row(), {"yes-token": make_flow(verified=False)}, now=NOW)
    assert r["decision"] == "BLOCK" and "FLOW_NOT_VERIFIED" in codes(r)


def test_flow_direction_missing_blocked():
    r = assess(make_row(), {"yes-token": make_flow(direction="")}, now=NOW)
    assert r["decision"] == "BLOCK" and "FLOW_DIRECTION_UNKNOWN" in codes(r)


def test_identity_mismatch_blocked():
    r = assess(make_row(), {"yes-token": make_flow(condition_id="other-condition")}, now=NOW)
    assert r["decision"] == "BLOCK" and "IDENTITY_MISMATCH" in codes(r)


def test_missing_flow_state_blocked():
    r = assess(make_row(), {}, now=NOW)
    assert r["decision"] == "BLOCK" and "FLOW_MISSING" in codes(r)


def test_flow_stale_blocked():
    old_ts = (NOW - timedelta(seconds=500)).isoformat()
    r = assess(make_row(), {"yes-token": make_flow(source_updated_at=old_ts)}, now=NOW)
    assert r["decision"] == "BLOCK" and "STALE_FLOW" in codes(r)


def test_market_not_accepting_blocked():
    flow = make_flow()
    flow["market"] = {"active": True, "closed": False, "accepting_orders": False}
    r = assess(make_row(), {"yes-token": flow}, now=NOW)
    assert r["decision"] == "BLOCK" and "MARKET_NOT_ACCEPTING" in codes(r)


def test_schema_mismatch_blocked():
    r = assess(make_row(schema_version=3), {"yes-token": make_flow()}, now=NOW)
    assert r["decision"] == "BLOCK" and "SCHEMA_MISMATCH" in codes(r)


def test_evidence_id_missing_blocks():
    flow = make_flow()
    flow.pop("evidence_id")
    r = assess(make_row(), {"yes-token": flow}, now=NOW)
    assert r["decision"] == "BLOCK"
    assert "EVIDENCE_MISSING" in codes(r)


def test_evidence_cursor_missing_blocks():
    flow = make_flow()
    flow.pop("evidence_cursor")
    r = assess(make_row(), {"yes-token": flow}, now=NOW)
    assert r["decision"] == "BLOCK"
    assert "EVIDENCE_MISSING" in codes(r)


def test_malformed_evidence_cursor_blocks():
    malformed = [
        [123],
        [123, 4, 5],
        [True, 4],
        [123, False],
        [-1, 4],
        [123, -1],
        ["123", 4],
        [123, "4"],
    ]
    for cursor in malformed:
        r = assess(
            make_row(),
            {"yes-token": make_flow(evidence_cursor=cursor)},
            now=NOW,
        )
        assert r["decision"] == "BLOCK"
        assert "EVIDENCE_INVALID" in codes(r)


def test_evidence_at_missing_blocks():
    flow = make_flow()
    flow.pop("evidence_at")
    r = assess(make_row(), {"yes-token": flow}, now=NOW)
    assert r["decision"] == "BLOCK"
    assert "EVIDENCE_MISSING" in codes(r)


def test_naive_or_invalid_evidence_timestamp_blocks():
    for value in ("2025-01-15T11:59:30", "not-a-time"):
        r = assess(
            make_row(),
            {"yes-token": make_flow(evidence_at=value)},
            now=NOW,
        )
        assert r["decision"] == "BLOCK"
        assert "EVIDENCE_INVALID" in codes(r)


def test_evidence_id_and_cursor_must_identify_same_event():
    r = assess(
        make_row(),
        {"yes-token": make_flow(evidence_id="0xabababababababababababababababababababababababababababababababab:9", evidence_cursor=[123, 4])},
        now=NOW,
    )
    assert r["decision"] == "BLOCK"
    assert "EVIDENCE_INVALID" in codes(r)


def test_canonical_evidence_id_requires_exact_32_byte_evm_hash():
    valid_hash = "0x" + "ab" * 32
    valid = assess(
        make_row(),
        {"yes-token": make_flow(evidence_id=f"{valid_hash}:4")},
        now=NOW,
    )
    assert valid["decision"] == "PASS"

    invalid_ids = [
        "hello:4",
        "garbage:4",
        "not-a-transaction-hash:4",
        "0xabc:4",
        "0x" + "ab" * 31 + ":4",
        "0x" + "ab" * 33 + ":4",
        "ab" * 32 + ":4",
        "0x" + "ag" * 32 + ":4",
        valid_hash,
        valid_hash + ":-1",
        valid_hash + ":four",
        True,
        4,
        None,
    ]
    for evidence_id in invalid_ids:
        result = assess(
            make_row(),
            {"yes-token": make_flow(evidence_id=evidence_id)},
            now=NOW,
        )
        assert result["decision"] == "BLOCK"
        assert "EVIDENCE_INVALID" in codes(result) or "EVIDENCE_MISSING" in codes(result)


def test_canonical_evidence_id_accepts_hex_case_but_preserves_cursor_coherence():
    upper_hash = "0x" + "AB" * 32
    result = assess(
        make_row(),
        {"yes-token": make_flow(evidence_id=f"{upper_hash}:4", evidence_cursor=[123, 4])},
        now=NOW,
    )
    assert result["decision"] == "PASS"
