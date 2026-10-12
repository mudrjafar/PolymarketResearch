from datetime import datetime, timezone
import os

import pytest

# Import the pure normalization helpers without contacting an RPC provider.
_saved_rpc = os.environ.get("POLYMARKET_RPC_URL")
os.environ["POLYMARKET_RPC_URL"] = "http://127.0.0.1:8545"
from scripts import book_engine, diamond_filter_v3, focus_engine, flow_tracker
from scripts import live_active_trades, paper_settlement, paper_worker, risk_engine
if _saved_rpc is None:
    os.environ.pop("POLYMARKET_RPC_URL", None)
else:
    os.environ["POLYMARKET_RPC_URL"] = _saved_rpc


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def position_id(module_id, outcome_index, *, arity=0, condition_index=0, chain=0):
    return str(
        (module_id << 248)
        | (123 << 120)
        | (arity << 104)
        | (chain << 24)
        | (condition_index << 8)
        | outcome_index
    )


def gamma_market(module_id):
    arity = 2 if module_id == 2 else 0
    yes = position_id(module_id, 0, arity=arity)
    no = position_id(module_id, 1, arity=arity)
    condition_id = f"0x{(int(yes) >> 8):062x}"
    return {
        "id": f"market-{module_id}",
        "conditionId": condition_id,
        "question": "Synthetic V3 market",
        "outcomes": ["Yes", "No"],
        "positionIds": [yes, no],
        "clobTokenIds": [],
        "outcomePrices": ["0.50", "0.50"],
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "volume24hr": "10000",
        "volume": "100000",
    }


def test_v3_binary_and_negrisk_identity_survives_offline_engine_path():
    for module_id in (1, 2):
        market = gamma_market(module_id)
        token = market["positionIds"][0]
        market_entry = live_active_trades.build_position_map([market])[token]
        assert market_entry["asset_type"] == "POSITION"
        assert market_entry["position_id"] == token
        assert market_entry["protocol_version"] == "V2"
        assert market_entry["condition_id"] == market["conditionId"]

        trade = {
            "token_id": token,
            "asset_id": token,
            "order_hash": "0x" + "01" * 32,
            "maker": "0x" + "02" * 20,
            "taker": "0x" + "03" * 20,
            "side": 0,
            "maker_amount": 1_000_000,
            "taker_amount": 2_000_000,
            "fee": 0,
        }
        log = {
            "_exchange_address": live_active_trades.EXCHANGE_V3,
            "blockNumber": "0x64",
            "transactionHash": "0x" + "ab" * 32,
            "logIndex": "0x2",
            "blockHash": "0x" + "cd" * 32,
        }
        normalized = live_active_trades.normalize_trade(
            log, trade, {**market_entry, "market_id": market["id"]},
            {"block_timestamp": NOW.isoformat()},
        )
        assert normalized["token_id"] == token
        assert normalized["asset_id"] == token
        assert normalized["position_id"] == token
        assert normalized["ctf_token_id"] is None
        assert normalized["asset_type"] == "POSITION"
        assert normalized["protocol_version"] == "V2"
        assert flow_tracker.is_flow_eligible_trade(normalized)
        flow = flow_tracker.analyze_flow([normalized], NOW, "5m")
        assert flow["trade_count"] == 1
        assert flow["total_volume"] == 1.0

        diamond_item = {
            "schema_version": 4,
            "condition_id": market["conditionId"],
            "token_id": token,
            "outcome": "Yes",
            "market": {
                "condition_id": market["conditionId"],
                "token_id": token,
                "asset_type": normalized["asset_type"],
                "protocol_version": normalized["protocol_version"],
            },
            "source_updated_at": NOW.isoformat(),
            "state": "VERIFIED",
            "verified": True,
            "confirmations": 3,
            "data_confidence": 0.9,
            "remaining_seconds": 86400,
            "flow_1m": {"trade_count": 1, "total_volume": 1, "net_flow": 1, "directional_strength": 1},
            "flow_5m": {"trade_count": 10, "total_volume": 10000, "net_flow": 10000, "directional_strength": 1, "largest_trade_ratio": 0.1},
            "flow_15m": {"trade_count": 20, "total_volume": 20000, "net_flow": 20000, "directional_strength": 1},
        }
        diamond_market = diamond_filter_v3.normalize(diamond_item, [normalized])
        assert diamond_market["token_id"] == token
        assert diamond_market["asset_type"] == "POSITION"
        assert diamond_market["protocol_version"] == "V2"
        assert diamond_filter_v3.same_market(diamond_item, normalized)

        evidence_at = NOW.isoformat()
        evidence_id = normalized["transaction_hash"] + ":" + str(normalized["log_index"])
        row = {
            "schema_version": 4,
            "source_updated_at": evidence_at,
            "last_trade_at": evidence_at,
            "market_key": f"condition:{market['conditionId']}|outcome:yes",
            "question": market["question"],
            "condition_id": market["conditionId"],
            "token_id": token,
            "asset_type": "POSITION",
            "protocol_version": "V2",
            "outcome": "Yes",
            "direction": "BUY",
            "price": 0.5,
            "classification": "DIAMOND",
            "diamond": True,
            "why_not_diamond": [],
            "metrics": {"verified": True, "confirmations": 3, "largest_trade_ratio": 0.1},
            "resolution": {"remaining_seconds": 86400},
            "cashflow_alert": {"quality": "BROAD"},
        }
        flow_state = {token: {
            "schema_version": 4,
            "token_id": token,
            "condition_id": market["conditionId"],
            "outcome": "Yes",
            "direction": "BUY",
            "verified": True,
            "confirmations": 3,
            "source_updated_at": evidence_at,
            "last_trade_at": evidence_at,
            "remaining_seconds": 86400,
            "evidence_id": evidence_id,
            "evidence_cursor": [100, 2],
            "evidence_at": evidence_at,
            "market": {
                "active": True,
                "closed": False,
                "accepting_orders": True,
                "asset_type": "POSITION",
                "protocol_version": "V2",
            },
        }}
        assessed = risk_engine.assess(row, flow_state, now=NOW)
        assert assessed["decision"] == "PASS"
        focus_candidate = risk_engine.focus_candidate_result(row, flow_state, assessed)
        focus_state, events = focus_engine.step(focus_engine.new_state(), [focus_candidate], NOW)
        assert events[0]["type"] == "LOCKED"
        focus = focus_state["focus"]
        assert focus["token_id"] == token
        assert focus["asset_type"] == "POSITION"
        assert focus["protocol_version"] == "V2"
        order_book = {
            "asset_id": token,
            "market": market["conditionId"],
            "bids": [{"price": "0.49", "size": "1000"}],
            "asks": [{"price": "0.50", "size": "1000"}],
        }
        book = book_engine.analyze_book(order_book, focus)
        assert book["book_ok"] is True
        assert book_engine.analyze_book({**order_book, "asset_id": "12345"}, focus)["book_ok"] is False
        assert paper_settlement.resolve_position_identity(
            market["conditionId"], token,
            asset_type="POSITION", protocol_version="V2", validate_chain=False,
        )["position_module_id"] == module_id
        paper_identity = paper_settlement.resolve_position_identity(
            market["conditionId"], token,
            asset_type="POSITION", protocol_version="V2", validate_chain=False,
        )
        paper_position = {
            "condition_id": market["conditionId"],
            "token_id": token,
            "outcome": "Yes",
            "asset_type": "POSITION",
            "protocol_version": "V2",
        }
        def identity_loader(condition_id, token_id, **identity_type):
            assert condition_id == market["conditionId"]
            assert token_id == token
            assert identity_type == {"asset_type": "POSITION", "protocol_version": "V2"}
            return paper_identity

        assert paper_worker._ensure_settlement_identity(
            paper_position, identity_loader,
        ) == (True, None, None)
        assert "ctf_contract" not in paper_position


@pytest.mark.parametrize("invalid", [
    position_id(3, 0),
    position_id(1, 0, chain=1),
    position_id(2, 0, arity=1),
])
def test_combo_and_invalid_ids_are_removed_before_collector_signal_path(invalid):
    market = gamma_market(1)
    market["positionIds"][0] = invalid
    assert invalid not in live_active_trades.build_position_map([market])


def test_ctf_token_identity_never_replaced_by_v3_position_id():
    market = gamma_market(1)
    market["clobTokenIds"] = ["111", "222"]
    token_map = live_active_trades.build_asset_map([market])
    assert "111" in token_map and "222" in token_map
    assert market["positionIds"][0] not in token_map
    v3_trade = {"token_id": market["positionIds"][0], "condition_id": market["conditionId"]}
    assert not diamond_filter_v3.same_market(
        {"token_id": "111", "condition_id": market["conditionId"], "outcome": "Yes"},
        v3_trade,
    )
