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

from scripts import general1_e2e2_live_probe as report
from scripts import general1_gamma1_live_probe as wrapper


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
    with patch.object(collector, "lookup_market", side_effect=AssertionError("cached hit re-requested")):
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


def test_baseline_opt_in_flag_stays_disabled(monkeypatch):
    assert collector.GAMMA_BATCH_LOOKUP is False
    env = report.controlled_neartip_environment("https://example.org/rpc")
    assert "COLLECTOR_GAMMA_BATCH_LOOKUP" not in env or env["COLLECTOR_GAMMA_BATCH_LOOKUP"] != "1"
    assert report.timing_diagnostics([{"status":"SUCCESS","gamma_batch_ms":100}])["phases_ms"]["gamma_batch_ms"]["total"] == 100


def test_wrapper_guards_and_reverts_context(monkeypatch):
    seen = []
    def fake_main(argv):
        seen.append((report.EXPECTED_NAME, report.REPORT_NAME, argv))
        return 3
    oldname,oldreport = report.EXPECTED_NAME,report.REPORT_NAME
    monkeypatch.setattr(report,"main",fake_main)
    assert wrapper.main(["--duration","60"]) == 3
    assert seen == [("GENERAL1_GAMMA1_LIVE_SANDBOX",
                     "general1_gamma1_live_report.json",
                     ["--near-tip","--gamma-batch","--duration","60"])]
    assert report.EXPECTED_NAME == oldname
    assert report.REPORT_NAME == oldreport
    assert wrapper.main(["--dashboard"]) == 3


def test_generation_lagged_archived_is_distinguished_from_invalid(tmp_path):
    def put(relative, value):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    token = {"token_id":"123","condition_id":"0x" + "b"*64}
    put("diamond_generation.json", {"generation_id":"new"})
    put("diamond_generations/new/flow_state.json", {"new":token})
    put("diamond_generations/new/diamond_analysis_v3.json", [token])
    put("risk_assessment.json", {"source_generation_id":"old","results":[token]})
    put("diamond_generations/old/flow_state.json", {"old":token})
    put("diamond_generations/old/diamond_analysis_v3.json", [token])
    out = report.pipeline_summary(tmp_path, {"_evidence":[("123",token["condition_id"])]})
    assert out["risk_generation_matches"] is False
    assert out["risk_generation_status"] == "LAGGED_ARCHIVED_GENERATION"
    assert out["risk_source_archive_real_trade_binding_observed"] is True
    (tmp_path / "diamond_generations/old/diamond_analysis_v3.json").unlink()
    out2 = report.pipeline_summary(tmp_path, {"_evidence":[("123",token["condition_id"])]})
    assert out2["risk_generation_status"] == "UNVERIFIED_ARCHIVE_MISSING"
    assert out2["risk_source_archive_real_trade_binding_observed"] is False
