import json
from datetime import datetime, timezone

from machine_common import save_json_atomic
from scripts import paper_worker, settlement_reader


NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def configure(monkeypatch, tmp_path):
    data = tmp_path / "data"
    requests = data / "paper_requests"
    requests.mkdir(parents=True)
    monkeypatch.setattr(paper_worker, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(paper_worker, "FOCUS_FILE", data / "focused_market.json", raising=False)
    monkeypatch.setattr(paper_worker, "BOOK_FILE", data / "book_assessment.json", raising=False)
    monkeypatch.setattr(paper_worker, "REQUEST_DIR", requests, raising=False)
    monkeypatch.setattr(paper_worker, "STATE_FILE", data / "paper_state.json", raising=False)
    monkeypatch.setattr(paper_worker, "EVENTS_FILE", data / "paper_events.jsonl", raising=False)
    return data


def open_position(**updates):
    row = {
        "paper_id": "PAPER-X",
        "request_id": "REQ-X",
        "status": "OPEN",
        "opened_at": NOW.isoformat(),
        "token_id": "token-a",
        "condition_id": "0x" + "11" * 32,
        "outcome": "Yes",
        "outcome_index": 0,
        "ctf_contract": settlement_reader.POLYMARKET_CTF_ADDRESS,
        "settlement_source": "POLYGON_CTF_FINALIZED",
        "investment_usd": 25.0,
        "tokens": 40.0,
        "fee_rate": 0.0,
        "fee_exponent": 0.0,
        "mark_status": "PENDING",
        "mark_checked_at": None,
        "mark": None,
        "settlement_status": "PENDING",
        "settlement_checked_at": None,
        "settlement_error": None,
        "settlement": None,
    }
    row.update(updates)
    return row


def write_state(data, position):
    save_json_atomic(
        data / "paper_state.json",
        {
            "schema_version": 1,
            "updated_at": NOW.isoformat(),
            "positions": [position],
            "processed_request_ids": [],
        },
    )


def test_settlement_does_not_require_clob_exit_liquidity(monkeypatch, tmp_path):
    data = configure(monkeypatch, tmp_path)
    write_state(data, open_position())

    def settled(condition_id, outcome_index, *, ctf_contract):
        assert outcome_index == 0
        assert ctf_contract == settlement_reader.POLYMARKET_CTF_ADDRESS
        return {
            "status": "SETTLED",
            "finality_source": "POLYGON_RPC_FINALIZED_TAG",
            "finalized_block_number": 123,
            "finalized_block_hash": "0x" + "22" * 32,
            "ctf_contract": ctf_contract,
            "condition_id": condition_id,
            "outcome_index": outcome_index,
            "payout_numerator": 1,
            "payout_denominator": 1,
            "payout_per_token": 1.0,
        }

    def forbidden_book(_):
        raise AssertionError("CLOB must not be required for finalized settlement")

    state, events = paper_worker.run_once(
        now=NOW,
        book_loader=forbidden_book,
        market_info_loader=lambda _: {},
        settlement_loader=settled,
    )

    position = state["positions"][0]
    assert position["status"] == "SETTLED"
    assert position["mark_status"] == "SETTLED"
    assert position["settlement"]["settlement_value_usd"] == 40.0
    assert position["realized_pnl_usd"] == 15.0
    assert position["realized_return_pct"] == 60.0
    assert [event["type"] for event in events] == ["SETTLED"]


def test_fractional_settlement_uses_ctf_ratio_without_exit_fee(monkeypatch, tmp_path):
    data = configure(monkeypatch, tmp_path)
    write_state(data, open_position(tokens=30.0, investment_usd=20.0))

    def settled(condition_id, outcome_index, *, ctf_contract):
        return {
            "status": "SETTLED",
            "finality_source": "POLYGON_RPC_FINALIZED_TAG",
            "finalized_block_number": 124,
            "finalized_block_hash": "0x" + "23" * 32,
            "ctf_contract": ctf_contract,
            "condition_id": condition_id,
            "outcome_index": outcome_index,
            "payout_numerator": 1,
            "payout_denominator": 2,
            "payout_per_token": 0.5,
        }

    state, _ = paper_worker.run_once(
        now=NOW,
        book_loader=lambda _: (_ for _ in ()).throw(AssertionError("no CLOB")),
        market_info_loader=lambda _: {},
        settlement_loader=settled,
    )

    position = state["positions"][0]
    assert position["settlement"]["payout_per_token"] == 0.5
    assert position["settlement"]["settlement_value_usd"] == 15.0
    assert position["realized_pnl_usd"] == -5.0
    assert "fee_usd" not in position["settlement"]
    assert "slippage_bps" not in position["settlement"]


def test_unresolved_and_reader_error_keep_position_open(monkeypatch, tmp_path):
    data = configure(monkeypatch, tmp_path)
    write_state(data, open_position())

    state, events = paper_worker.run_once(
        now=NOW,
        book_loader=lambda _: {
            "market": "0x" + "11" * 32,
            "asset_id": "token-a",
            "timestamp": "1",
            "hash": "book",
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.41", "size": "100"}],
        },
        market_info_loader=lambda _: {},
        settlement_loader=lambda *args, **kwargs: {
            "status": "UNRESOLVED",
            "payout_denominator": 0,
            "payout_numerator": None,
            "payout_per_token": None,
        },
    )
    assert state["positions"][0]["status"] == "OPEN"
    assert state["positions"][0]["settlement_status"] == "UNRESOLVED"
    assert events == []

    def reader_error(*args, **kwargs):
        raise settlement_reader.SettlementReadError("RPC_ERROR")

    state, events = paper_worker.run_once(
        now=NOW,
        book_loader=lambda _: (_ for _ in ()).throw(RuntimeError("book down")),
        market_info_loader=lambda _: {},
        settlement_loader=reader_error,
    )
    assert state["positions"][0]["status"] == "OPEN"
    assert state["positions"][0]["settlement_status"] == "SETTLEMENT_CHECK_ERROR"
    assert state["positions"][0]["settlement_error"] == "RPC_ERROR"
    assert state["positions"][0]["mark_status"] == "BOOK_FETCH_book down"
    assert events == []


def test_legacy_open_position_without_binding_is_not_guessed(monkeypatch, tmp_path):
    data = configure(monkeypatch, tmp_path)
    legacy = open_position()
    legacy.pop("outcome_index")
    legacy.pop("ctf_contract")
    write_state(data, legacy)

    state, _ = paper_worker.run_once(
        now=NOW,
        book_loader=lambda _: (_ for _ in ()).throw(RuntimeError("book unavailable")),
        market_info_loader=lambda _: {},
        settlement_loader=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("settlement reader must not be called")
        ),
    )

    position = state["positions"][0]
    assert position["status"] == "OPEN"
    assert position["settlement_status"] == "BINDING_MISSING"
