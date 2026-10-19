"""GENERAL1-E2E-1: isolated REAL worker transitions, deliberately no READY.

This test uses one canonical low-volume V4 blockchain trade fixture, not
fabricated Diamond/Risk approvals. It uses actual Flow, Diamond, Risk,
Focus, Book and Paper modules with tmp_path-only data and zero network calls.
"""
import contextlib
import io
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts import (
    flow_tracker as flow,
    diamond_filter_v3 as diamond,
    risk_engine as risk,
    focus_runner as focus,
    book_worker as book,
    paper_worker as paper,
)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_one_canonical_trade_flows_through_all_downstream_workers_without_ready(
    tmp_path, monkeypatch
):
    data = tmp_path / "isolated_data"
    data.mkdir()
    now = datetime.now(timezone.utc)
    token_id = "1234567"
    condition_id = "0x" + "cd" * 32
    trade = {
        "collector_version": 4,
        "condition_id": condition_id,
        "token_id": token_id,
        "outcome": "Yes",
        "question": "OFFLINE ONE-TRADE SMOKE TEST",
        "market_id": "smoke-market",
        "transaction_hash": "0x" + "ab" * 32,
        "log_index": 3,
        "block": 123456,
        "block_timestamp": (now - timedelta(seconds=10)).isoformat(),
        "detected_at": now.isoformat(),
        "side_label": "BUY",
        "trade_usd": 25.0,
        "fill_price": 0.42,
        "end_date": (now + timedelta(days=3)).isoformat(),
        "active": True,
        "closed": False,
        "accepting_orders": True,
    }
    trade_file = data / "live_trades.jsonl"
    trade_file.write_text(json.dumps(trade) + "\n", encoding="utf-8")
    assert flow.is_flow_eligible_trade(trade) is True

    monkeypatch.setattr(flow, "TRADES_FILE", str(trade_file))
    monkeypatch.setattr(flow, "FLOW_STATE_FILE", str(data / "flow_state.json"))
    monkeypatch.setattr(flow, "VERIFICATION_STATE_FILE", str(data / "verification_state.json"))
    monkeypatch.setattr(flow, "market_states", {})
    monkeypatch.setattr(flow, "flow_states", {})
    monkeypatch.setattr(flow, "analysis_schedule", {})

    # Real flow.main() has a resident loop; interrupt only its sleep after
    # one completed processing iteration. No production worker is launched.
    with patch.object(flow.time, "sleep", side_effect=InterruptedError), contextlib.redirect_stdout(io.StringIO()):
        try:
            flow.main()
        except InterruptedError:
            pass

    flow_state = read(data / "flow_state.json")
    assert token_id in flow_state
    entry = flow_state[token_id]
    assert entry["condition_id"] == condition_id
    assert entry["outcome"] == "Yes"
    assert entry["direction"] == "BUY"
    assert entry["flow_5m"]["trade_count"] == 1
    assert entry["flow_5m"]["total_volume"] == 25.0
    assert entry["verified"] is False
    assert entry["confirmations"] == 0
    assert entry["evidence_id"] is None  # not verified, so no fabricated confirmation

    monkeypatch.setattr(diamond, "FLOW_FILE", data / "flow_state.json")
    monkeypatch.setattr(diamond, "LIVE_TRADES_FILE", trade_file)
    monkeypatch.setattr(diamond, "DIAMONDS_FILE", data / "diamonds.json")
    monkeypatch.setattr(diamond, "CANDIDATES_FILE", data / "diamond_candidates.json")
    monkeypatch.setattr(diamond, "ANALYSIS_FILE", data / "diamond_analysis_v3.json")
    monkeypatch.setattr(diamond, "GENERATIONS_DIR", data / "diamond_generations")
    monkeypatch.setattr(diamond, "GENERATION_MANIFEST_FILE", data / "diamond_generation.json")
    diamonds, _, analyzed = diamond.run_once(verbose=False)
    assert len(analyzed) == 1
    assert analyzed[0]["condition_id"] == condition_id
    assert analyzed[0]["token_id"] == token_id
    assert diamonds == []

    monkeypatch.setattr(risk, "DATA_DIR", data)
    monkeypatch.setattr(risk, "FLOW_FILE", data / "flow_state.json")
    monkeypatch.setattr(risk, "ANALYSIS_FILE", data / "diamond_analysis_v3.json")
    monkeypatch.setattr(risk, "RISK_FILE", data / "risk_assessment.json")
    risk_results = risk.run_once(verbose=False)
    assert len(risk_results) == 1
    assert risk_results[0]["decision"] == "BLOCK"
    assert risk_results[0]["risk_ok"] is False
    risk_state = read(data / "risk_assessment.json")
    manifest = read(data / "diamond_generation.json")
    assert risk_state["source_generation_id"] == manifest["generation_id"]
    assert risk_state["passed"] == 0

    monkeypatch.setattr(focus, "DATA_DIR", data)
    monkeypatch.setattr(focus, "RISK_FILE", data / "risk_assessment.json")
    monkeypatch.setattr(focus, "STATE_FILE", data / "focus_state.json")
    monkeypatch.setattr(focus, "FOCUS_FILE", data / "focused_market.json")
    monkeypatch.setattr(focus, "EVENTS_FILE", data / "focus_events.jsonl")
    focus_state, focus_events, focus_status = focus.run_once(now=datetime.now(timezone.utc))
    assert focus_status == "OK"
    assert focus_events == []
    assert focus_state["focus"] is None
    assert read(data / "focused_market.json")["state"] != "READY"

    monkeypatch.setattr(book, "DATA_DIR", data)
    monkeypatch.setattr(book, "FOCUS_FILE", data / "focused_market.json")
    monkeypatch.setattr(book, "BOOK_FILE", data / "book_assessment.json")

    def forbidden_network(*args, **kwargs):
        raise AssertionError("network fetch on non-READY smoke is forbidden")

    book_result = book.run_once(now=datetime.now(timezone.utc), book_loader=forbidden_network)
    assert book_result["book_ok"] is False
    assert book_result["status"] != "OK"

    monkeypatch.setattr(paper, "DATA_DIR", data)
    monkeypatch.setattr(paper, "FOCUS_FILE", data / "focused_market.json")
    monkeypatch.setattr(paper, "BOOK_FILE", data / "book_assessment.json")
    monkeypatch.setattr(paper, "REQUEST_DIR", data / "paper_requests")
    monkeypatch.setattr(paper, "STATE_FILE", data / "paper_state.json")
    monkeypatch.setattr(paper, "EVENTS_FILE", data / "paper_events.jsonl")
    paper_state, paper_events = paper.run_once(
        now=datetime.now(timezone.utc),
        book_loader=forbidden_network,
        market_info_loader=forbidden_network,
        identity_loader=forbidden_network,
        settlement_checker=forbidden_network,
    )
    assert paper_events == []
    assert paper_state["positions"] == []
    assert read(data / "paper_state.json")["positions"] == []
