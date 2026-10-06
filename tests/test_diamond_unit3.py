from copy import deepcopy
from datetime import datetime, timezone

from scripts import diamond_filter_v3 as diamond


def fixture():
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 4,
        "source_updated_at": now,
        "last_trade_at": now,
        "condition_id": "condition",
        "token_id": "yes-token",
        "market": {
            "question": "TEST",
            "outcome": "Yes",
            "token_id": "yes-token",
            "price": 0.42,
            "end_date": "2027-01-01T00:00:00Z",
            "active": True,
            "closed": False,
            "accepting_orders": True,
            "last_trade_at": now,
        },
        "state": "VERIFIED",
        "verified": True,
        "confirmations": 3,
        "data_confidence": 90,
        "resolution_state": "FAR",
        "remaining_seconds": 7 * 24 * 3600,
        "flow_1m": {"trade_count": 8, "total_volume": 1200, "net_flow": 800, "directional_strength": .67, "largest_trade_ratio": .2},
        "flow_5m": {"trade_count": 22, "total_volume": 6000, "net_flow": 4000, "directional_strength": .67, "largest_trade_ratio": .22, "largest_trade": 1320},
        "flow_15m": {"trade_count": 45, "total_volume": 12000, "net_flow": 8000, "directional_strength": .67, "largest_trade_ratio": .2},
    }


def test_valid_signal_is_diamond():
    assert diamond.analyze(fixture(), [])["diamond"] is True


def test_missing_token_id_blocks():
    x = fixture()
    x.pop("token_id", None)
    x["market"].pop("token_id", None)
    r = diamond.analyze(x, [])
    assert r["diamond"] is False
    assert "token_id missing" in r["why_not_diamond"]


def test_missing_condition_id_blocks():
    x = fixture()
    x.pop("condition_id", None)
    r = diamond.analyze(x, [])
    assert r["diamond"] is False
    assert "condition_id missing" in r["why_not_diamond"]


def test_stale_source_blocks():
    x = fixture()
    x["source_updated_at"] = "2000-01-01T00:00:00Z"
    assert diamond.analyze(x, [])["classification"] == "DATA_RISK"


def test_direction_contradiction_blocks():
    x = fixture()
    x["flow_15m"] = deepcopy(x["flow_15m"])
    x["flow_15m"]["net_flow"] = -8000
    r = diamond.analyze(x, [])
    assert r["diamond"] is False
