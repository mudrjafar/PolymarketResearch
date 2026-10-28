"""GENERAL1-GAMMA-1 offline tests: no network, orders, RPC, or workers."""
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

os.environ.setdefault("POLYMARKET_RPC_URL", "http://127.0.0.1:1")
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gamma1_collector", ROOT / "scripts" / "live_active_trades.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)



def gamma_market(token, *, closed=False, condition=None):
    return {
        "id": token, "conditionId": condition or "0x" + "f" * 64,
        "clobTokenIds": [token], "outcomes": ["Yes"], "outcomePrices": ["0.5"],
        "question": "Fixture", "closed": closed, "active": not closed,
        "acceptingOrders": not closed
    }


def response(markets):
    r = Mock()
    r.json.return_value = markets
    r.raise_for_status.return_value = None
    return r


def test_batch_open_one_request_many_identities(monkeypatch):
    record = {"gamma_batch_ms": 0}
    monkeypatch.setattr(collector, "_LIVE1C_CURRENT", record)
    with patch.object(collector.requests, "get", return_value=response([
        gamma_market("123"), gamma_market("456"), gamma_market("789")
    ])) as get:
        found = collector._gamma_batch_legacy_lookup(["123", "456", "789"])
    assert set(found) == {"123", "456", "789"}
    assert get.call_count == 1
    assert get.call_args.kwargs["params"]["clob_token_ids"] == ["123", "456", "789"]
    assert record["gamma_batch_tokens_resolved"] == 3
    assert record["gamma_batch_http_requests"] == 1


def test_batch_closed_stage_only_queries_missing_and_retains_closed_state():
    with patch.object(collector.requests, "get", side_effect=[
        response([gamma_market("123")]),
        response([gamma_market("456", closed=True)]),
    ]) as get:
        resolved = collector._gamma_batch_legacy_lookup(["123", "456"])
    assert len(resolved) == 2
    assert resolved["456"]["closed"] is True
    assert get.call_args_list[1].kwargs["params"]["clob_token_ids"] == ["456"]
    assert get.call_args_list[1].kwargs["params"]["closed"] is True


def test_bad_shape_truncation_and_identity_conflicts_fail_closed():
    with patch.object(collector.requests, "get", return_value=response({})):
        with pytest.raises(collector.StorageError):
            collector._gamma_batch_legacy_lookup(["123","456"])
    with patch.object(collector.requests, "get", return_value=response([
        gamma_market(str(i)) for i in range(100)
    ])):
        with pytest.raises(collector.RetryableCollectorError):
            collector._gamma_batch_legacy_lookup(["123", "456"])
    with patch.object(collector.requests, "get", return_value=response([
        {**gamma_market("123"), "conditionId":"invalid"},
        gamma_market("456"),
    ])):
        with pytest.raises(collector.StorageError):
            collector._gamma_batch_legacy_lookup(["123", "456"])


def test_network_failure_does_not_modify_cached_identities():
    ledger = collector.GammaTokenMap({})
    logs = [
        {"blockNumber":"0x65", "blockHash":"0x" + "a" * 64,
         "_exchange_address":collector.EXCHANGES[0], "asset":token}
        for token in ("123","456")
    ]
    header = {"block_hash":"0x" + "a" * 64}
    with patch.object(collector, "decode_order_filled", side_effect=lambda log: {
        "taker":log["_exchange_address"],"asset_id":log["asset"]
    }), patch.object(collector.requests, "get", side_effect=collector.requests.Timeout("unavailable")):
        with pytest.raises(collector.requests.Timeout):
            collector._prefetch_legacy_assets(logs, {101: header}, ledger)
    assert not ledger
    assert not ledger.known_identities


def test_prefetch_deduplicates_and_caches_exact_legacy_ids(monkeypatch):
    ledger = collector.GammaTokenMap({})
    logs = [
        {"blockNumber":"0x65", "blockHash":"0x" + "a" * 64,
         "_exchange_address":collector.EXCHANGES[0], "asset":token}
        for token in ("123","123","456","456")
    ]
    record = {}
    monkeypatch.setattr(collector, "_LIVE1C_CURRENT", record)
    with patch.object(collector, "decode_order_filled", side_effect=lambda log: {
        "taker":log["_exchange_address"], "asset_id":log["asset"]
    }), patch.object(collector.requests, "get", return_value=response([
        gamma_market("123"), gamma_market("456")
    ])) as get:
        collector._prefetch_legacy_assets(logs, {101:{"block_hash":"0x" + "a"*64}}, ledger)
    assert set(ledger) == {"123","456"}
    assert get.call_count == 1
    assert record["gamma_cache_miss_unique_legacy"] == 2
    assert record["gamma_batch_tokens_resolved"] == 2
    with patch.object(collector, "decode_order_filled", side_effect=lambda log: {
        "taker":log["_exchange_address"], "asset_id":log["asset"]
    }), patch.object(collector, "lookup_market", side_effect=AssertionError("cached hit re-requested")):
        collector._prefetch_legacy_assets(logs, {101:{"block_hash":"0x" + "a"*64}}, ledger)
    assert record["gamma_cache_hit_events"] == 4


def test_v3_not_converted_to_ctf_by_prefetch(monkeypatch):
    ledger = collector.GammaTokenMap({})
    event = {"blockNumber":"0x65","blockHash":"0x" + "a"*64,
             "_exchange_address":collector.EXCHANGE_V3,"asset":"123"}
    monkeypatch.setattr(collector, "_LIVE1C_CURRENT", {})
    with patch.object(collector, "decode_order_filled", return_value={
        "taker":collector.EXCHANGE_V3,"asset_id":"123"
    }), patch.object(collector.requests,"get",side_effect=AssertionError("V3 batched unexpectedly")):
        collector._prefetch_legacy_assets([event], {101:{"block_hash":"0x" + "a"*64}}, ledger)
    assert not ledger



def test_batching_is_opt_in():
    assert collector.GAMMA_BATCH_LOOKUP is False
