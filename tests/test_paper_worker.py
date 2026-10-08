import json
from datetime import datetime, timezone

import pytest

from machine_common import save_json_atomic
from scripts import paper_worker


NOW = datetime.now(timezone.utc)


def settlement_binding():
    return {
        "source": "POLYGON_FINALIZED_CTF",
        "chain_id": 137,
        "ctf_contract": "0xctf",
        "condition_id": "condition-a",
        "token_id": "token-a",
        "market_family": "STANDARD_CTF_V2",
        "exchange_contract": "0xexchange",
        "ctf_collateral": "0xcollateral",
        "outcome_index": 0,
        "index_set": 1,
        "verified_block_number": 100,
        "verified_block_hash": "0x" + "11" * 32,
    }


def unresolved_resolution(position):
    binding = position["settlement_binding"]
    return {
        **binding,
        "status": "UNRESOLVED",
        "finalized_block_number": 101,
        "finalized_block_hash": "0x" + "22" * 32,
        "payout_denominator": 0,
        "payout_numerators": None,
        "payout_numerator": None,
        "payout_per_token": None,
    }


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
    monkeypatch.setattr(
        paper_worker.settlement_resolver,
        "bind_position",
        lambda token_id, condition_id: settlement_binding(),
    )
    monkeypatch.setattr(
        paper_worker.settlement_resolver,
        "check_position",
        unresolved_resolution,
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


def test_open_persists_finalized_ctf_settlement_binding(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    book_loader, market_loader = loaders()

    state, events = paper_worker.run_once(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
    )

    assert events[0]["type"] == "OPENED"
    position = state["positions"][0]
    assert position["status"] == "OPEN"
    assert position["settlement_binding"]["source"] == "POLYGON_FINALIZED_CTF"
    assert position["settlement_binding"]["outcome_index"] == 0
    assert position["settlement_binding"]["index_set"] == 1
    assert position["settlement_status"] == "UNRESOLVED"


def test_resolved_position_settles_before_book_fetch_with_fractional_payout(
    monkeypatch,
    tmp_path,
):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    book_loader, market_loader = loaders()

    state, _ = paper_worker.run_once(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
    )
    tokens = state["positions"][0]["tokens"]

    def resolved(position):
        return {
            **position["settlement_binding"],
            "status": "RESOLVED",
            "finalized_block_number": 102,
            "finalized_block_hash": "0x" + "33" * 32,
            "payout_denominator": 2,
            "payout_numerators": [1, 1],
            "payout_numerator": 1,
            "payout_per_token": 0.5,
        }

    monkeypatch.setattr(
        paper_worker.settlement_resolver,
        "check_position",
        resolved,
    )

    def forbidden_book(_):
        raise AssertionError("book must not be fetched after finalized resolution")

    state, events = paper_worker.run_once(
        now=NOW,
        book_loader=forbidden_book,
        market_info_loader=market_loader,
    )

    settled = [event for event in events if event["type"] == "SETTLED"]
    assert len(settled) == 1
    position = state["positions"][0]
    assert position["status"] == "SETTLED"
    assert position["settlement_status"] == "SETTLED"
    assert position["settlement"]["payout_per_token"] == 0.5
    assert position["settlement"]["payout_denominator"] == 2
    assert position["settlement"]["settlement_value_usd"] == round(tokens * 0.5, 8)
    assert position["mark_status"] == "SETTLED"
    assert "exit" not in position
    assert position["realized_pnl_usd"] == round(tokens * 0.5 - 25.0, 8)


def test_settlement_source_error_keeps_position_open_and_book_mark_available(
    monkeypatch,
    tmp_path,
):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    book_loader, market_loader = loaders()

    state, _ = paper_worker.run_once(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
    )

    def broken(_):
        raise RuntimeError("RPC_DOWN")

    monkeypatch.setattr(
        paper_worker.settlement_resolver,
        "check_position",
        broken,
    )

    state, events = paper_worker.run_once(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
    )

    assert not [event for event in events if event["type"] == "SETTLED"]
    position = state["positions"][0]
    assert position["status"] == "OPEN"
    assert position["settlement_status"] == "SETTLEMENT_CHECK_ERROR"
    assert position["settlement_error"] == "RPC_DOWN"
    assert position["mark_status"] == "OK"


def test_legacy_open_position_backfills_binding_before_settlement_check(
    monkeypatch,
    tmp_path,
):
    paths = configure(monkeypatch, tmp_path)
    state = paper_worker.new_state(NOW)
    state["positions"].append(
        {
            "paper_id": "PAPER-legacy",
            "request_id": "REQ-legacy",
            "status": "OPEN",
            "opened_at": NOW.isoformat(),
            "token_id": "token-a",
            "condition_id": "condition-a",
            "outcome": "Yes",
            "question": "Market A",
            "direction": "BUY",
            "investment_usd": 25.0,
            "tokens": 50.0,
            "fee_rate": 0.0,
            "fee_exponent": 0.0,
            "entry": {},
            "mark_status": "PENDING",
            "mark_checked_at": None,
            "mark": None,
        }
    )
    save_json_atomic(paths["state"], state)

    state, _ = paper_worker.run_once(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )

    position = state["positions"][0]
    assert position["status"] == "OPEN"
    assert position["settlement_binding"]["source"] == "POLYGON_FINALIZED_CTF"
    assert position["settlement_status"] == "UNRESOLVED"
