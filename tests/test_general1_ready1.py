"""GENERAL1-READY-1. Offline integration; no chain, CLOB, credentials or live trades.

Exercises actual Risk assessment, durable Focus runner, Book worker and
explicit Paper OPEN worker in one temp directory with controlled evidence.
Mocks are confined to network boundaries, never to Focus READY authority.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from machine_common import save_json_atomic
from scripts import book_worker, focus_runner, paper_settlement, paper_worker, risk_engine


T0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
TOKEN = "123"
CONDITION = "0x" + "b" * 64


def configure(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    request_dir = data / "paper_requests"
    request_dir.mkdir()
    paths = {
        "data": data,
        "risk": data / "risk_assessment.json",
        "focus_state": data / "focus_state.json",
        "focus": data / "focused_market.json",
        "focus_events": data / "focus_events.jsonl",
        "book": data / "book_assessment.json",
        "requests": request_dir,
        "paper_state": data / "paper_state.json",
        "paper_events": data / "paper_events.jsonl",
    }
    for module, mapping in (
        (focus_runner, {"DATA_DIR": data, "RISK_FILE": paths["risk"],
                        "STATE_FILE": paths["focus_state"], "FOCUS_FILE": paths["focus"],
                        "EVENTS_FILE": paths["focus_events"]}),
        (book_worker, {"DATA_DIR": data, "FOCUS_FILE": paths["focus"],
                       "BOOK_FILE": paths["book"]}),
        (paper_worker, {"DATA_DIR": data, "FOCUS_FILE": paths["focus"],
                        "BOOK_FILE": paths["book"], "REQUEST_DIR": paths["requests"],
                        "STATE_FILE": paths["paper_state"], "EVENTS_FILE": paths["paper_events"]}),
    ):
        for key, value in mapping.items():
            monkeypatch.setattr(module, key, value)
    # Learning is observational; no external learning DB or outbound messaging.
    monkeypatch.setattr(book_worker, "_queue_learning", lambda _: None)
    monkeypatch.setattr(paper_worker, "_queue_learning_open_positions", lambda _state, _now: False)
    return paths


def saved(path):
    return json.loads(path.read_text(encoding="utf-8"))


def risk_candidate(n, at, *, condition=CONDITION, outcome="Yes",
                   token=TOKEN, direction="BUY", active=True):
    evidence_id = "0x" + f"{n:064x}" + ":1"
    flow = {
        "schema_version": 4, "token_id": token, "condition_id": condition,
        "outcome": outcome, "direction": direction, "verified": True,
        "confirmations": 3, "source_updated_at": at.isoformat(),
        "last_trade_at": at.isoformat(), "remaining_seconds": 86400,
        "evidence_id": evidence_id, "evidence_cursor": [100 + n, 1],
        "evidence_at": at.isoformat(),
        "market": {"active": active, "closed": False, "accepting_orders": True},
    }
    diamond = {
        "schema_version": 4, "source_updated_at": at.isoformat(),
        "last_trade_at": at.isoformat(),
        "market_key": f"condition:{condition}|outcome:{outcome.lower()}",
        "question": "Offline READY integration fixture",
        "condition_id": condition, "token_id": token, "outcome": outcome,
        "direction": direction, "price": 0.50, "classification": "DIAMOND",
        "diamond": True, "why_not_diamond": [],
        "metrics": {"verified": True, "confirmations": 3,
                    "largest_trade_ratio": 0.20},
        "resolution": {"remaining_seconds": 86400},
        "cashflow_alert": {"quality": "BROAD"},
    }
    flow_state = {token: flow}
    result = risk_engine.focus_candidate_result(
        diamond, flow_state, risk_engine.assess(diamond, flow_state, now=at)
    )
    return result


def publish_and_tick(paths, n, now, **kwargs):
    row = risk_candidate(n, now, **kwargs)
    save_json_atomic(paths["risk"], {
        "source_generation_id": f"GEN-{n}",
        "source_generated_at": now.isoformat(),
        "generated_at": now.isoformat(),
        "results": [row],
    })
    state, events, status = focus_runner.run_once(now=now)
    assert status == "OK"
    return row, state, events, saved(paths["focus"])


def ready_sequence(paths):
    result = []
    for n, offset, expected in [
        (1, 0, "LOCKED"), (2, 61, "WAIT 1/3"),
        (3, 122, "WAIT 2/3"), (4, 183, "READY"),
    ]:
        item = publish_and_tick(paths, n, T0 + timedelta(seconds=offset))
        assert item[0]["decision"] == "PASS"
        assert item[3]["state"] == expected
        result.append(item)
    return result


def book():
    return {
        "market": CONDITION, "asset_id": TOKEN,
        "timestamp": "123", "hash": "offline-book",
        "bids": [{"price": "0.49", "size": "1000"}],
        "asks": [{"price": "0.50", "size": "1000"}],
        "min_order_size": "5", "tick_size": "0.01",
        "neg_risk": False, "last_trade_price": "0.50",
    }


def market_info(_condition):
    return {"t": [{"t": TOKEN}], "fd": {"r": 0.05, "e": 1}}


def settlement_identity(_condition, _token):
    return {
        "settlement_protocol": paper_settlement.PROTOCOL_LEGACY_CTF,
        "settlement_family": paper_settlement.FAMILY_STANDARD,
        "ctf_contract": paper_settlement.CTF_CONTRACT,
        "position_collateral": paper_settlement.STANDARD_USDCE,
        "outcome_index": 0,
    }


def unresolved(_position):
    return {"status": paper_settlement.UNRESOLVED,
            "reason_code": None, "settlement_read_block": 100,
            "settlement_read_block_hash": "0x" + "a" * 64,
            "settlement_authority": paper_settlement.CTF_CONTRACT,
            "settlement_finality_source": "RPC_FINALIZED"}


def paper_tick(now, *, raw_book=None, settlement=None):
    return paper_worker.run_once(
        now=now,
        book_loader=lambda _token: book() if raw_book is None else raw_book,
        market_info_loader=market_info,
        identity_loader=settlement_identity,
        settlement_checker=unresolved if settlement is None else settlement,
    )


def request_open(paths, at, *, request_id="READY1-OPEN", **changes):
    payload = saved(paths["focus"])
    focus = payload["focus"]
    request = {
        "schema_version": 1, "request_id": request_id,
        "requested_at": at.isoformat(), "action": "OPEN",
        "amount_usd": 25.0, "chat_id": "offline-only",
        "token_id": TOKEN, "condition_id": CONDITION,
        "source_generation_id": payload["source_generation_id"],
        "source_evidence_id": focus["last_evidence_id"],
        "focus_locked_at": focus["locked_at"],
    }
    request.update(changes)
    save_json_atomic(paths["requests"] / (request_id + ".json"), request)
    return request


def test_risk_focus_book_explicit_paper_full_path_is_idempotent(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    sequence = ready_sequence(paths)
    assert [x[2][0]["type"] for x in sequence] == [
        "LOCKED", "WAIT", "WAIT", "READY"
    ]
    assert sequence[-1][1]["focus"]["progress"] == 3
    assert sequence[-1][1]["focus"]["last_evidence_cursor"] == [104, 1]
    book_at = T0 + timedelta(seconds=184)
    loaded = []
    analysis = book_worker.run_once(now=book_at,
                                    book_loader=lambda token: (loaded.append(token), book())[1])
    assert loaded == [TOKEN]
    assert analysis["book_ok"] is True
    assert analysis["status"] == "OK"
    assert analysis["source_generation_id"] == "GEN-4"
    assert analysis["source_evidence_id"] == sequence[-1][1]["focus"]["last_evidence_id"]
    assert paper_worker.read_execution_gate(now=book_at)[0] == "READY_BOOK_PASS"

    # No request means no Paper OPEN even though the signal is READY+Book PASS.
    untouched, empty_events = paper_tick(book_at)
    assert untouched["positions"] == []
    assert empty_events == []

    request_open(paths, book_at)
    state, events = paper_tick(book_at)
    assert any(event["type"] == "OPENED" for event in events)
    assert len(state["positions"]) == 1
    position = state["positions"][0]
    assert position["status"] == "OPEN"
    assert position["source_generation_id"] == "GEN-4"
    assert position["source_evidence_id"] == analysis["source_evidence_id"]
    assert position["investment_usd"] == 25.0
    assert position["entry"]["fee_usd"] > 0
    assert position["tokens"] > 0
    assert position["entry"]["vwap"] == 0.5

    replay, replay_events = paper_tick(book_at + timedelta(seconds=1))
    assert len(replay["positions"]) == 1
    assert not any(event["type"] == "OPENED" for event in replay_events)


@pytest.mark.parametrize("change,expected", [
    ({"source_generation_id": "OLD-GEN"}, "REQUEST_SOURCE_GENERATION_ID_MISMATCH"),
    ({"source_evidence_id": "0x" + "c" * 64 + ":1"},
     "REQUEST_SOURCE_EVIDENCE_ID_MISMATCH"),
    ({"condition_id": "0x" + "f" * 64}, "REQUEST_CONDITION_ID_MISMATCH"),
    ({"token_id": "999"}, "REQUEST_TOKEN_ID_MISMATCH"),
])
def test_paper_rejects_unbound_explicit_request(monkeypatch, tmp_path, change, expected):
    paths = configure(monkeypatch, tmp_path)
    ready_sequence(paths)
    at = T0 + timedelta(seconds=184)
    book_worker.run_once(now=at, book_loader=lambda _: book())
    request_open(paths, at, **change)
    state, events = paper_tick(at)
    assert state["positions"] == []
    assert events[0]["type"] == "REJECTED"
    assert events[0]["reason_code"] == expected


@pytest.mark.parametrize("field", ["asset_id", "market"])
def test_missing_clob_identity_never_allows_book_pass(monkeypatch, tmp_path, field):
    paths = configure(monkeypatch, tmp_path)
    ready_sequence(paths)
    at = T0 + timedelta(seconds=184)
    raw = book()
    raw.pop(field)
    result = book_worker.run_once(now=at, book_loader=lambda _: raw)
    assert result["book_ok"] is False
    assert result["reason_codes"]
    assert paper_worker.read_execution_gate(now=at)[0] == "BOOK_BLOCK"


def test_focus_cannot_confirm_different_condition_under_same_token(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    publish_and_tick(paths, 1, T0)
    next_at = T0 + timedelta(seconds=61)
    contrary, state, events, view = publish_and_tick(
        paths, 2, next_at, condition="0x" + "c" * 64
    )
    # The new candidate is individually Risk PASS, but belongs to a
    # DIFFERENT condition than the existing exclusive locked focus.
    assert contrary["decision"] == "PASS"
    assert state["focus"] is None
    assert events[-1]["type"] == "INVALIDATED"
    assert "IDENTITY_MISMATCH" in events[-1]["reasons"]
    assert view["state"] == "IDLE"


def test_focus_cannot_confirm_different_outcome_under_same_token(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    publish_and_tick(paths, 1, T0)
    _, state, events, _ = publish_and_tick(
        paths, 2, T0 + timedelta(seconds=61), outcome="No"
    )
    assert state["focus"] is None
    assert events[-1]["type"] == "INVALIDATED"
    assert "IDENTITY_MISMATCH" in events[-1]["reasons"]


def test_duplicate_evidence_and_generation_ticking_do_not_advance_focus(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    _, locked, _, _ = publish_and_tick(paths, 1, T0)
    assert locked["focus"]["progress"] == 0
    for i in range(2, 5):
        row = risk_candidate(1, T0 + timedelta(seconds=i * 65))
        save_json_atomic(paths["risk"], {
            "source_generation_id": f"GEN-REPLAY-{i}",
            "source_generated_at": (T0 + timedelta(seconds=i * 65)).isoformat(),
            "generated_at": (T0 + timedelta(seconds=i * 65)).isoformat(),
            "results": [row],
        })
        state, events, _ = focus_runner.run_once(now=T0 + timedelta(seconds=i * 65))
        assert state["focus"]["progress"] == 0
        assert events == []
    assert saved(paths["focus"])["state"] == "LOCKED"


def test_lower_cursor_and_short_spacing_are_not_counted(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    publish_and_tick(paths, 3, T0)
    # Valid but too-close evidence updates last seen cursor, NOT progress.
    _, state, _, _ = publish_and_tick(paths, 4, T0 + timedelta(seconds=30))
    assert state["focus"]["progress"] == 0
    _, state, events, _ = publish_and_tick(paths, 2, T0 + timedelta(seconds=65))
    assert state["focus"]["last_evidence_cursor"] == [104, 1]
    assert state["focus"]["progress"] == 0
    assert events == []


def test_hard_block_invalidates_and_never_triggers_book(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    publish_and_tick(paths, 1, T0)
    row, state, events, _ = publish_and_tick(
        paths, 2, T0 + timedelta(seconds=61), active=False
    )
    assert row["decision"] == "BLOCK"
    assert "MARKET_NOT_ACTIVE" in row["reason_codes"]
    assert state["focus"] is None
    assert events[-1]["type"] == "INVALIDATED"
    result = book_worker.run_once(
        now=T0 + timedelta(seconds=62),
        book_loader=lambda _: (_ for _ in ()).throw(AssertionError("No Book access")),
    )
    assert result["book_ok"] is False
    assert result["status"] == "NOT_READY"


def test_stale_focus_and_book_reject_paper_open(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    ready_sequence(paths)
    at = T0 + timedelta(seconds=184)
    book_worker.run_once(now=at, book_loader=lambda _: book())
    request_open(paths, at)
    later = at + timedelta(minutes=10)
    assert paper_worker.read_execution_gate(now=later)[0] == "INPUT_NOT_OK"
    state, events = paper_tick(later)
    assert state["positions"] == []
    assert events[0]["type"] == "REJECTED"


def test_ready_loss_before_request_rechecks_and_rejects(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    ready_sequence(paths)
    at = T0 + timedelta(seconds=184)
    book_worker.run_once(now=at, book_loader=lambda _: book())
    request_open(paths, at)
    # Atomic Focus publication changes authoritative state before processing.
    latest = saved(paths["focus"])
    latest["state"] = "WAIT 2/3"
    latest["focus"]["status"] = "WAIT"
    latest["focus"]["progress"] = 2
    save_json_atomic(paths["focus"], latest)
    state, events = paper_tick(at)
    assert state["positions"] == []
    assert events[0]["reason_code"] == "FOCUS_NOT_READY"


def test_mismatched_book_generation_and_undersized_depth_fail_closed(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    ready_sequence(paths)
    at = T0 + timedelta(seconds=184)
    book_worker.run_once(now=at, book_loader=lambda _: book())
    request_open(paths, at)
    altered = saved(paths["book"])
    altered["source_generation_id"] = "OTHER"
    save_json_atomic(paths["book"], altered)
    assert paper_worker.read_execution_gate(now=at)[0] == "GENERATION_ID_MISMATCH"
    # Restore and check that the actual depth gate cannot be bypassed.
    altered["source_generation_id"] = "GEN-4"
    save_json_atomic(paths["book"], altered)
    shallow = book()
    shallow["asks"] = [{"price": "0.50", "size": "1"}]
    state, events = paper_tick(at, raw_book=shallow)
    assert state["positions"] == []
    assert events[0]["type"] == "REJECTED"


def test_unknown_settlement_rejects_paper_open(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    ready_sequence(paths)
    at = T0 + timedelta(seconds=184)
    book_worker.run_once(now=at, book_loader=lambda _: book())
    request_open(paths, at)
    state, events = paper_tick(at, settlement=lambda _: {"status": "UNKNOWN"})
    assert state["positions"] == []
    assert events[0]["reason_code"] == "SETTLEMENT_UNKNOWN"


def test_focus_revoked_during_book_fetch_blocks_paper_commit(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    ready_sequence(paths)
    at = T0 + timedelta(seconds=184)
    book_worker.run_once(now=at, book_loader=lambda _: book())
    request_open(paths, at)

    def book_loader_revokes_ready(_token):
        # Focus changes *after* Paper's first gate read, in the network window.
        payload = saved(paths["focus"])
        payload["state"] = "WAIT 2/3"
        payload["focus"]["status"] = "WAIT"
        payload["focus"]["progress"] = 2
        save_json_atomic(paths["focus"], payload)
        return book()

    state, events = paper_worker.run_once(
        now=at,
        book_loader=book_loader_revokes_ready,
        market_info_loader=market_info,
        identity_loader=settlement_identity,
        settlement_checker=unresolved,
    )
    assert state["positions"] == []
    assert events[0]["type"] == "REJECTED"
    assert events[0]["reason_code"] == "EXECUTION_GATE_CHANGED_FOCUS_NOT_READY"
