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


def position_id(module_id, outcome_index, *, arity=0, condition_index=0, chain=0, reserved=0):
    return str(
        (module_id << 248)
        | (123 << 120)
        | (arity << 104)
        | (reserved << 40)
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


def test_v3_binary_and_negrisk_identity_survives_offline_engine_path(monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for module_id in (1, 2):
        market = gamma_market(module_id)
        token = market["positionIds"][0]
        market_entry = live_active_trades.build_position_map([market])[token]
        assert market_entry["asset_type"] == "POSITION"
        assert market_entry["position_id"] == token
        assert market_entry["protocol_version"] == "V2"
        assert market_entry["condition_id"] == market["conditionId"]

        normalized_trades = []
        for index in range(20):
            trade = {
                "token_id": token,
                "asset_id": token,
                "order_hash": f"0x{index + 1:064x}",
                "maker": "0x" + "02" * 20,
                "taker": "0x" + "03" * 20,
                "side": 0,
                "maker_amount": 300_000_000,
                "taker_amount": 600_000_000,
                "fee": 0,
            }
            log = {
                "_exchange_address": live_active_trades.EXCHANGE_V3,
                "blockNumber": hex(100 + index),
                "transactionHash": f"0x{index + 1:064x}",
                "logIndex": hex(index),
                "blockHash": "0x" + "cd" * 32,
            }
            normalized = live_active_trades.normalize_trade(
                log, trade, {**market_entry, "market_id": market["id"]},
                {"block_timestamp": now.isoformat()},
            )
            normalized_trades.append(normalized)

        for normalized in normalized_trades:
            assert normalized["token_id"] == token
            assert normalized["asset_id"] == token
            assert normalized["position_id"] == token
            assert normalized["ctf_token_id"] is None
            assert normalized["asset_type"] == "POSITION"
            assert normalized["protocol_version"] == "V2"
            assert flow_tracker.is_flow_eligible_trade(normalized)

        flow_1m = flow_tracker.analyze_flow(normalized_trades, now, "1m")
        flow_5m = flow_tracker.analyze_flow(normalized_trades, now, "5m")
        flow_15m = flow_tracker.analyze_flow(normalized_trades, now, "15m")
        assert flow_5m["trade_count"] == 20
        assert flow_5m["total_volume"] >= 5_000
        assert flow_5m["largest_trade_ratio"] < 0.70

        # Exercise Flow Tracker's real in-memory state writer; it performs no
        # filesystem writes. Diamond and Risk then consume this same state.
        monkeypatch.setattr(flow_tracker, "flow_states", {})
        latest = normalized_trades[-1]
        evidence_at = latest["block_timestamp"]
        evidence_id = f"{latest['transaction_hash']}:{latest['log_index']}"
        market_state = {
            "question": market["question"],
            "outcome": "Yes",
            "asset_type": "POSITION",
            "protocol_version": "V2",
            "end_date": None,
            "start_date": None,
            "price": 0.5,
            "token_id": token,
            "market_id": market["id"],
            "active": True,
            "closed": False,
            "accepting_orders": True,
            "volume24hr": 100_000,
            "last_trade_at": evidence_at,
        }
        flow_tracker.save_flow_state(
            token_id=token,
            condition_id=market["conditionId"],
            market=market_state,
            flow_1m=flow_1m,
            flow_5m=flow_5m,
            flow_15m=flow_15m,
            interest_score=100,
            data_confidence=0.9,
            resolution_state="ACTIVE",
            remaining_seconds=86_400,
            analysis_profile="NEAR",
            analysis_interval_seconds=60,
            state_data={
                "state": "VERIFIED",
                "confirmations": 3,
                "last_confirmation_evidence": evidence_id,
                "last_confirmation_cursor": [latest["block"], latest["log_index"]],
                "last_confirmation_at": evidence_at,
            },
            candidate=True,
            candidate_reasons=[],
            verified=True,
            verification_reasons=[],
        )
        flow_state = flow_tracker.flow_states
        flow_item = flow_state[token]
        assert flow_item["market"]["asset_type"] == "POSITION"
        assert flow_item["market"]["protocol_version"] == "V2"

        # Use Diamond's production analyzer, not a hand-built Diamond row.
        row = diamond_filter_v3.analyze(flow_item, normalized_trades)
        assert row["classification"] == "DIAMOND"
        assert row["diamond"] is True
        assert row["token_id"] == token
        assert row["asset_type"] == "POSITION"
        assert row["protocol_version"] == "V2"
        assert diamond_filter_v3.same_market(flow_item, normalized_trades[-1])

        assessed = risk_engine.assess(row, flow_state, now=now)
        assert assessed["decision"] == "PASS", assessed
        focus_candidate = risk_engine.focus_candidate_result(row, flow_state, assessed)
        focus_state, events = focus_engine.step(focus_engine.new_state(), [focus_candidate], now)
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

        # A fully populated frozen record must still reject protocol-tag drift.
        mismatched_frozen = {**paper_position, "asset_type": "CTF_TOKEN"}
        ok, status, reason = paper_worker._ensure_settlement_identity(
            mismatched_frozen, identity_loader,
        )
        assert (ok, status, reason) == (
            False,
            paper_settlement.IDENTITY_MISMATCH,
            "POSITION_PROTOCOL_METADATA_MISMATCH",
        )

        # The identity-loader path must validate the complete returned
        # settlement identity, not just whether Yes/No matches its index.
        incomplete_mismatch = {
            "condition_id": market["conditionId"],
            "token_id": token,
            "outcome": "Yes",
            "asset_type": "CTF_TOKEN",
            "protocol_version": "LEGACY_CTF",
        }
        ok, status, reason = paper_worker._ensure_settlement_identity(
            incomplete_mismatch, lambda *_args, **_kwargs: paper_identity,
        )
        assert (ok, status, reason) == (
            False,
            paper_settlement.IDENTITY_MISMATCH,
            "POSITION_PROTOCOL_METADATA_MISMATCH",
        )


@pytest.mark.parametrize("invalid", [
    position_id(3, 0),
    position_id(1, 0, chain=1),
    position_id(2, 0, arity=1),
    position_id(1, 0, reserved=1),
    position_id(2, 0, arity=2, reserved=1),
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
