import json
from datetime import datetime, timezone

import pytest

from machine_common import save_json_atomic
from scripts import paper_worker


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

    def unresolved_settlement(condition_id, token_id):
        return {
            "status": "UNRESOLVED",
            "condition_id": condition_id,
            "token_id": token_id,
            "finalized_block_number": 100,
            "finalized_block_hash": "0x" + "11" * 32,
            "finality_source": "RPC_FINALIZED",
            "ctf_contract": paper_worker.settlement_reader.CTF_ADDRESS,
            "payout_denominator": 0,
        }

    monkeypatch.setattr(
        paper_worker.settlement_reader,
        "read_settlement",
        unresolved_settlement,
    )
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


def test_open_requires_exact_ready_book_binding_and_marks_from_bids(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    book_loader, market_loader = loaders()

    state, events = paper_worker.run_once(
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

    state, events = paper_worker.run_once(
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

    state, events = paper_worker.run_once(
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

    state, events = paper_worker.run_once(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
    )
    assert len(state["positions"]) == 1
    assert len(events) == 1

    state, events = paper_worker.run_once(
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

    state, _ = paper_worker.run_once(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
    )
    paper_id = state["positions"][0]["paper_id"]

    write_gate(paths, state="WAIT")
    write_close_request(paths, paper_id)

    state, events = paper_worker.run_once(
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

    state, _ = paper_worker.run_once(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )
    paper_id = state["positions"][0]["paper_id"]
    write_close_request(paths, paper_id)

    state, events = paper_worker.run_once(
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
        paper_worker.run_once(
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


def settled_result(condition_id="condition-a", token_id="token-a", *, yes=True, tie=False):
    if tie:
        denominator = 2
        numerators = [1, 1]
    else:
        denominator = 1
        numerators = [1, 0] if yes else [0, 1]
    outcome_index = 0 if yes else 1
    return {
        "status": "SETTLED",
        "condition_id": condition_id,
        "token_id": token_id,
        "finalized_block_number": 101,
        "finalized_block_hash": "0x" + "22" * 32,
        "finality_source": "RPC_FINALIZED",
        "ctf_contract": paper_worker.settlement_reader.CTF_ADDRESS,
        "market_family": "STANDARD",
        "collateral": "0x" + "33" * 20,
        "outcome_index": outcome_index,
        "outcome": "YES" if outcome_index == 0 else "NO",
        "index_set": 1 if outcome_index == 0 else 2,
        "payout_denominator": denominator,
        "payout_numerators": numerators,
        "payout_numerator": numerators[outcome_index],
        "payout_per_token": numerators[outcome_index] / denominator,
    }


def test_finalized_settlement_wins_over_missing_clob(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)

    state, _ = paper_worker.run_once(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )
    tokens = state["positions"][0]["tokens"]

    monkeypatch.setattr(
        paper_worker.settlement_reader,
        "read_settlement",
        lambda condition_id, token_id: settled_result(condition_id, token_id),
    )

    def forbidden_book(_):
        raise AssertionError("CLOB must not be called after finalized settlement")

    state, events = paper_worker.run_once(
        now=NOW,
        book_loader=forbidden_book,
        market_info_loader=lambda _: market_info(),
    )

    position = state["positions"][0]
    assert position["status"] == "SETTLED"
    assert position["settlement_status"] == "SETTLED"
    assert position["settlement"]["settlement_value_usd"] == pytest.approx(tokens)
    assert position["realized_pnl_usd"] == pytest.approx(tokens - 25.0)
    assert [event["type"] for event in events] == ["SETTLED"]


def test_fractional_ctf_payout_settles_without_exit_fee(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)

    state, _ = paper_worker.run_once(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )
    tokens = state["positions"][0]["tokens"]

    monkeypatch.setattr(
        paper_worker.settlement_reader,
        "read_settlement",
        lambda condition_id, token_id: settled_result(
            condition_id,
            token_id,
            tie=True,
        ),
    )

    state, events = paper_worker.run_once(
        now=NOW,
        book_loader=lambda _: (_ for _ in ()).throw(
            AssertionError("CLOB must not be called")
        ),
        market_info_loader=lambda _: market_info(),
    )

    position = state["positions"][0]
    assert position["status"] == "SETTLED"
    assert position["settlement"]["payout_per_token"] == 0.5
    assert position["settlement"]["settlement_value_usd"] == pytest.approx(tokens * 0.5)
    assert "exit" not in position
    assert events[0]["type"] == "SETTLED"


def test_settlement_source_error_blocks_manual_close_but_keeps_mark(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)

    state, _ = paper_worker.run_once(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )
    paper_id = state["positions"][0]["paper_id"]
    write_close_request(paths, paper_id)

    def broken_settlement(_condition_id, _token_id):
        raise RuntimeError("RPC_DOWN")

    monkeypatch.setattr(
        paper_worker.settlement_reader,
        "read_settlement",
        broken_settlement,
    )

    state, events = paper_worker.run_once(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )

    position = state["positions"][0]
    assert position["status"] == "OPEN"
    assert position["settlement_status"] == "SETTLEMENT_CHECK_ERROR"
    assert position["settlement_error"] == "RPC_DOWN"
    assert position["mark_status"] == "OK"
    rejected = [event for event in events if event["type"] == "REJECTED"]
    assert rejected[0]["reason_code"] == "SETTLEMENT_STATE_UNKNOWN"


def test_invalid_settlement_binding_fails_closed(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)

    state, _ = paper_worker.run_once(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )

    bad = settled_result()
    bad["token_id"] = "different-token"
    monkeypatch.setattr(
        paper_worker.settlement_reader,
        "read_settlement",
        lambda _condition_id, _token_id: bad,
    )

    state, events = paper_worker.run_once(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )

    position = state["positions"][0]
    assert position["status"] == "OPEN"
    assert position["settlement_status"] == "SETTLEMENT_CHECK_ERROR"
    assert position["settlement_error"] == "SETTLEMENT_TOKEN_ID_MISMATCH"
    assert events == []
