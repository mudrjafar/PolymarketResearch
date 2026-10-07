import json
from datetime import datetime, timezone

import pytest

from machine_common import save_json_atomic
from scripts import paper_settlement, paper_worker


NOW = datetime.now(timezone.utc)


def configure(monkeypatch, tmp_path):
    data = tmp_path / "data"
    request_dir = data / "paper_requests"
    request_dir.mkdir(parents=True)

    paths = {
        "data": data,
        "focus": data / "focused_market.json",
        "book": data / "book_assessment.json",
        "requests": request_dir,
        "state": data / "paper_state.json",
        "events": data / "paper_events.jsonl",
    }

    monkeypatch.setattr(paper_worker, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(paper_worker, "FOCUS_FILE", paths["focus"], raising=False)
    monkeypatch.setattr(paper_worker, "BOOK_FILE", paths["book"], raising=False)
    monkeypatch.setattr(paper_worker, "REQUEST_DIR", paths["requests"], raising=False)
    monkeypatch.setattr(paper_worker, "STATE_FILE", paths["state"], raising=False)
    monkeypatch.setattr(paper_worker, "EVENTS_FILE", paths["events"], raising=False)
    return paths


def write_gate(paths, *, state="READY", generation="GEN-X", evidence=None):
    evidence = evidence or ("0x" + "ab" * 32 + ":7")
    focus = {
        "token_id": "token-a",
        "condition_id": "condition-a",
        "outcome": "Yes",
        "question": "Market A",
        "direction": "BUY",
        "price": 0.50,
        "status": "READY" if state == "READY" else "WAIT",
        "progress": 3 if state == "READY" else 2,
        "last_evidence_id": evidence,
        "locked_at": NOW.isoformat(),
    }
    save_json_atomic(
        paths["focus"],
        {
            "schema_version": 1,
            "generated_at": NOW.isoformat(),
            "input_status": "OK",
            "source_generation_id": generation,
            "state": state,
            "focus": focus,
        },
    )
    save_json_atomic(
        paths["book"],
        {
            "schema_version": 1,
            "generated_at": NOW.isoformat(),
            "status": "OK",
            "book_ok": True,
            "reason_codes": [],
            "source_generation_id": generation,
            "source_evidence_id": evidence,
            "token_id": "token-a",
            "condition_id": "condition-a",
        },
    )
    return focus


def raw_book(*, bid="0.49", ask="0.50", bid_size="1000", ask_size="1000"):
    return {
        "market": "condition-a",
        "asset_id": "token-a",
        "timestamp": "123",
        "hash": "book-x",
        "bids": [{"price": bid, "size": bid_size}],
        "asks": [{"price": ask, "size": ask_size}],
    }


def market_info():
    return {
        "t": [{"t": "token-a"}],
        "fd": {"r": 0.05, "e": 1},
    }


def write_open_request(paths, *, request_id="REQ-open", generation="GEN-X", evidence=None):
    evidence = evidence or ("0x" + "ab" * 32 + ":7")
    save_json_atomic(
        paths["requests"] / f"{request_id}.json",
        {
            "schema_version": 1,
            "request_id": request_id,
            "requested_at": NOW.isoformat(),
            "action": "OPEN",
            "amount_usd": 25,
            "chat_id": "123",
            "token_id": "token-a",
            "condition_id": "condition-a",
            "source_generation_id": generation,
            "source_evidence_id": evidence,
            "focus_locked_at": NOW.isoformat(),
        },
    )


def write_close_request(paths, paper_id, *, request_id="REQ-close"):
    save_json_atomic(
        paths["requests"] / f"{request_id}.json",
        {
            "schema_version": 1,
            "request_id": request_id,
            "requested_at": NOW.isoformat(),
            "action": "CLOSE",
            "paper_id": paper_id,
            "chat_id": "123",
        },
    )


def loaders():
    return (
        lambda token_id: raw_book(),
        lambda condition_id: market_info(),
    )


def fake_identity(condition_id, token_id):
    return {
        "settlement_family": paper_settlement.FAMILY_STANDARD,
        "ctf_contract": paper_settlement.CTF_CONTRACT,
        "position_collateral": paper_settlement.STANDARD_USDCE,
        "outcome_index": 0,
    }


def unresolved_settlement(position):
    return {
        "status": paper_settlement.UNRESOLVED,
        "reason_code": None,
        "settlement_read_block": 100,
        "settlement_read_block_hash": "0x" + "aa" * 32,
        "settlement_authority": paper_settlement.CTF_CONTRACT,
        "settlement_finality_source": "RPC_FINALIZED",
    }


def run_worker(**kwargs):
    kwargs.setdefault("identity_loader", fake_identity)
    kwargs.setdefault("settlement_checker", unresolved_settlement)
    return paper_worker.run_once(**kwargs)


def test_open_requires_exact_ready_book_binding_and_marks_from_bids(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    book_loader, market_loader = loaders()

    state, events = run_worker(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
    )

    assert [event["type"] for event in events] == ["OPENED"]
    assert len(state["positions"]) == 1
    position = state["positions"][0]
    assert position["status"] == "OPEN"
    assert position["entry"]["fee_usd"] > 0
    assert position["tokens"] < 50
    assert position["mark_status"] == "OK"
    assert position["mark"]["current_value_usd"] < 25
    assert position["mark"]["pnl_usd"] < 0


def test_focus_wait_rejects_open_without_fetching_execution_book(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths, state="WAIT")
    write_open_request(paths)

    def forbidden(_):
        raise AssertionError("CLOB must not be called")

    state, events = run_worker(
        now=NOW,
        book_loader=forbidden,
        market_info_loader=forbidden,
    )

    assert state["positions"] == []
    assert events[0]["type"] == "REJECTED"
    assert events[0]["reason_code"] == "FOCUS_NOT_READY"


def test_generation_mismatch_rejects_request(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths, generation="GEN-X")
    write_open_request(paths, generation="GEN-OLD")
    book_loader, market_loader = loaders()

    state, events = run_worker(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
    )

    assert state["positions"] == []
    assert events[0]["reason_code"] == "REQUEST_SOURCE_GENERATION_ID_MISMATCH"


def test_same_request_is_idempotent(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    book_loader, market_loader = loaders()

    state, events = run_worker(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
    )
    assert len(state["positions"]) == 1
    assert len(events) == 1

    state, events = run_worker(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
    )
    assert len(state["positions"]) == 1
    assert events == []


def test_close_is_user_controlled_and_does_not_require_focus_ready(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    book_loader, market_loader = loaders()

    state, _ = run_worker(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
    )
    paper_id = state["positions"][0]["paper_id"]

    write_gate(paths, state="WAIT")
    write_close_request(paths, paper_id)

    state, events = run_worker(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
    )

    close_events = [event for event in events if event["type"] == "CLOSED"]
    assert len(close_events) == 1
    position = state["positions"][0]
    assert position["status"] == "CLOSED"
    assert position["realized_pnl_usd"] < 0
    assert position["exit"]["fee_usd"] > 0


def test_insufficient_exit_depth_rejects_close_and_keeps_position_open(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)

    state, _ = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )
    paper_id = state["positions"][0]["paper_id"]
    write_close_request(paths, paper_id)

    state, events = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(bid_size="1"),
        market_info_loader=lambda _: market_info(),
    )

    rejected = [event for event in events if event["type"] == "REJECTED"]
    assert rejected[0]["reason_code"] == "EXIT_NOT_FULLY_EXECUTABLE"
    assert state["positions"][0]["status"] == "OPEN"
    assert state["positions"][0]["mark_status"] == "INSUFFICIENT_EXIT_DEPTH"


def test_corrupt_durable_state_fails_closed(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    paths["state"].write_text("{bad-json", encoding="utf-8")

    with pytest.raises(paper_worker.PaperStateError):
        run_worker(
            now=NOW,
            book_loader=lambda _: raw_book(),
            market_info_loader=lambda _: market_info(),
        )

    assert paths["state"].read_text(encoding="utf-8") == "{bad-json"


def test_runtime_orders_book_before_paper_before_telegram():
    source = (paper_worker.BASE_DIR / "run_machine.py").read_text(encoding="utf-8")

    book_index = source.index('("book",')
    paper_index = source.index('("paper",')
    telegram_index = source.index('("telegram",')

    assert book_index < paper_index < telegram_index


def final_settlement(position):
    return {
        "status": paper_settlement.FINAL_SETTLED,
        "reason_code": None,
        "settlement_read_block": 200,
        "settlement_read_block_hash": "0x" + "bb" * 32,
        "settlement_authority": paper_settlement.CTF_CONTRACT,
        "settlement_finality_source": "RPC_FINALIZED",
        "payout_numerator": 1,
        "payout_denominator": 1,
    }


def resolved_not_final(position):
    return {
        "status": paper_settlement.RESOLVED_NOT_FINAL,
        "reason_code": None,
        "settlement_read_block": 199,
        "settlement_read_block_hash": "0x" + "cc" * 32,
        "settlement_authority": paper_settlement.CTF_CONTRACT,
        "settlement_finality_source": "RPC_FINALIZED",
    }


def settlement_error(position):
    return {
        "status": paper_settlement.SETTLEMENT_CHECK_ERROR,
        "reason_code": "RPC_UNAVAILABLE",
    }


def test_final_settlement_is_terminal_and_idempotent(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)

    state, _ = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )
    paper_id = state["positions"][0]["paper_id"]

    def forbidden_book(_):
        raise AssertionError("SETTLED position must not be marked from CLOB")

    state, events = run_worker(
        now=NOW,
        book_loader=forbidden_book,
        market_info_loader=lambda _: market_info(),
        settlement_checker=final_settlement,
    )

    settled = [event for event in events if event["type"] == "SETTLED"]
    assert len(settled) == 1
    position = state["positions"][0]
    assert position["paper_id"] == paper_id
    assert position["status"] == "SETTLED"
    assert position["settlement_status"] == paper_settlement.FINAL_SETTLED
    assert position["payout_per_token"] == 1.0
    assert position["settlement_value_usd"] == pytest.approx(position["tokens"])
    assert position["realized_pnl_usd"] == pytest.approx(
        position["settlement_value_usd"] - 25.0
    )
    assert position["mark_status"] == "SETTLED"

    state, events = run_worker(
        now=NOW,
        book_loader=forbidden_book,
        market_info_loader=lambda _: market_info(),
        settlement_checker=final_settlement,
    )
    assert events == []
    assert state["positions"][0]["status"] == "SETTLED"


def test_resolved_not_final_still_allows_user_close(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)

    state, _ = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )
    paper_id = state["positions"][0]["paper_id"]
    write_close_request(paths, paper_id)

    state, events = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
        settlement_checker=resolved_not_final,
    )

    closed = [event for event in events if event["type"] == "CLOSED"]
    assert len(closed) == 1
    assert state["positions"][0]["status"] == "CLOSED"


def test_settlement_source_error_does_not_block_user_close(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)

    state, _ = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )
    paper_id = state["positions"][0]["paper_id"]
    write_close_request(paths, paper_id)

    state, events = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
        settlement_checker=settlement_error,
    )

    assert any(event["type"] == "CLOSED" for event in events)
    assert state["positions"][0]["status"] == "CLOSED"


def test_legacy_open_position_identity_is_backfilled_deterministically(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)

    state, _ = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )
    position = state["positions"][0]
    for field in paper_worker.SETTLEMENT_IDENTITY_FIELDS:
        position.pop(field, None)
    save_json_atomic(paths["state"], state)

    state, _ = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )

    position = state["positions"][0]
    assert position["settlement_family"] == paper_settlement.FAMILY_STANDARD
    assert position["ctf_contract"] == paper_settlement.CTF_CONTRACT
    assert position["position_collateral"] == paper_settlement.STANDARD_USDCE
    assert position["outcome_index"] == 0


def test_close_after_settlement_is_rejected(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)

    state, _ = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )
    paper_id = state["positions"][0]["paper_id"]

    state, _ = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
        settlement_checker=final_settlement,
    )
    assert state["positions"][0]["status"] == "SETTLED"

    write_close_request(paths, paper_id)
    state, events = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
        settlement_checker=final_settlement,
    )

    rejected = [event for event in events if event["type"] == "REJECTED"]
    assert rejected
    assert rejected[0]["reason_code"] == "POSITION_ALREADY_SETTLED"
