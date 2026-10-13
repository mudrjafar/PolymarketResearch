"""Offline protocol-binding contract: Collector labels are never execution authorization.

No live RPC, no requests, no production Paper state.
"""

from datetime import datetime, timezone

import pytest

from scripts import diamond_filter_v3 as diamond
from scripts import flow_tracker as flow
from scripts import focus_engine
from scripts import risk_engine


NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
EVIDENCE = "0x" + "ab" * 32 + ":1"


def _identity(token, kind):
    if kind == "LEGACY":
        return dict(asset_type="CTF_TOKEN", protocol_version="LEGACY_CTF",
                    asset_id=token, ctf_token_id=token, position_id=None,
                    exchange_address="0xE111180000d2663C0091e4f400237545B87B996B")
    return dict(asset_type="POSITION", protocol_version="V2",
                asset_id=token, ctf_token_id=None, position_id=token,
                exchange_address="0xe3333700cA9d93003F00f0F71f8515005F6c00Aa")


def _flow_to_focus(monkeypatch, token, kind):
    monkeypatch.setattr(flow, "flow_states", {})
    identity = _identity(token, kind)
    flow.save_flow_state(
        token_id=token, condition_id="0x" + "ab" * 32,
        market={"token_id": token, "outcome": "Yes", "question": "Fixture",
                "last_trade_at": NOW.isoformat(), "price": 0.42,
                "active": True, "closed": False, "accepting_orders": True,
                **identity},
        flow_1m={}, flow_5m={"direction": "BUY"}, flow_15m={},
        interest_score=1.0, data_confidence=1.0,
        resolution_state="OPEN", remaining_seconds=7200,
        analysis_profile="NORMAL", analysis_interval_seconds=15,
        state_data={"state": "VERIFIED", "confirmations": 3,
                    "last_confirmation_evidence": EVIDENCE,
                    "last_confirmation_cursor": [100, 1],
                    "last_confirmation_at": NOW.isoformat()},
        candidate=True, candidate_reasons=[], verified=True,
        verification_reasons=[],
    )
    frozen_flow = flow.flow_states[token]
    assert frozen_flow["asset_type"] == identity["asset_type"]
    assert frozen_flow["market"]["protocol_version"] == identity["protocol_version"]
    analyzed = diamond.normalize(frozen_flow, [])
    assert analyzed["asset_type"] == identity["asset_type"]
    assert analyzed["protocol_version"] == identity["protocol_version"]
    risk_row = risk_engine.focus_candidate_result(
        analyzed,
        {token: frozen_flow},
        {"token_id": token, "condition_id": analyzed["condition_id"],
         "risk_ok": True, "decision": "PASS", "reason_codes": [],
         "direction": "BUY", "evidence_id": EVIDENCE,
         "evidence_cursor": [100, 1], "evidence_at": NOW.isoformat()},
    )
    return risk_row


@pytest.mark.parametrize("kind", ["LEGACY", "POSITION"])
def test_protocol_identity_reaches_focus_without_reconstructing_it(monkeypatch, kind):
    row = _flow_to_focus(monkeypatch, "108", kind)
    assert row["execution_protocol_eligible"] is (kind == "LEGACY")
    state, events = focus_engine.step(focus_engine.new_state(), [row], NOW)
    if kind == "LEGACY":
        assert state["focus"]["token_id"] == "108"
        assert events[-1]["type"] == "LOCKED"
    else:
        assert state["focus"] is None
        assert events == []


def test_v3_before_legacy_preserves_ranking_but_only_legacy_locks(monkeypatch):
    v3 = _flow_to_focus(monkeypatch, "110", "POSITION")
    legacy = _flow_to_focus(monkeypatch, "109", "LEGACY")
    state, events = focus_engine.step(focus_engine.new_state(), [v3, legacy], NOW)
    assert state["focus"]["token_id"] == "109"
    assert events[-1]["type"] == "LOCKED"


def test_flow_conflicting_trade_provenance_is_explicitly_unknown():
    token = "108"
    a = {"token_id": token, **_identity(token, "LEGACY")}
    b = {"token_id": token, **_identity(token, "POSITION")}
    result = flow.consistent_protocol_identity([a, b])
    assert result["asset_type"] is None
    assert result["protocol_version"] is None
    assert result["ctf_token_id"] is None


def test_diamond_disagreement_with_flow_market_cannot_create_execution_eligibility(monkeypatch):
    row = _flow_to_focus(monkeypatch, "113", "LEGACY")
    assert row["execution_protocol_eligible"] is True

    token = "113"
    original = flow.flow_states[token]
    mismatched = dict(original)
    mismatched["asset_type"] = "POSITION"
    analyzed = diamond.normalize(mismatched, [])
    assert analyzed["asset_type"] is None
    result = risk_engine.focus_candidate_result(
        analyzed, {token: original},
        {"risk_ok": True, "reason_codes": [], "token_id": token}
    )
    assert result["execution_protocol_eligible"] is False
