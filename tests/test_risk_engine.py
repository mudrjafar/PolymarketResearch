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
