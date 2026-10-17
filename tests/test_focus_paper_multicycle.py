"""Offline integration coverage for the current Risk -> Focus -> Book -> Paper path.

Every worker uses isolated temporary files and synthetic V3 Binary identities.
No production service, network endpoint, Telegram API, or live checkpoint is used.
"""

import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

import machine_common
import telegram_bot
from machine_common import save_json_atomic
from scripts import (
    book_worker,
    focus_runner,
    paper_settlement,
    paper_worker,
    risk_engine,
    risk_worker,
)


class VirtualDateTime(datetime):
    """A shared injectable clock for the worker freshness checks."""

    current = None

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        if tz is not None:
            return value.astimezone(tz)
        return value.replace(tzinfo=None) if value.tzinfo else value


def _v3_binary_identity(module_id=1):
    arity = 2 if module_id == 2 else 0
    position_id = str((module_id << 248) | (123 << 120) | (arity << 104))
    condition_id = f"0x{(int(position_id) >> 8):062x}"
    return position_id, condition_id


def _risk_inputs(data_dir, now, generation, token_id, condition_id, evidence_no):
    evidence_at = now.isoformat()
    tx_hash = f"0x{evidence_no:064x}"
    evidence_id = f"{tx_hash}:0"
    block = 10_000 + evidence_no

    flow = {
        "schema_version": risk_engine.SCHEMA_VERSION,
        "token_id": token_id,
        "condition_id": condition_id,
        "asset_type": "POSITION",
        "protocol_version": "V2",
        "outcome": "Yes",
        "direction": "BUY",
        "verified": True,
        "confirmations": 3,
        "source_updated_at": evidence_at,
        "last_trade_at": evidence_at,
        "remaining_seconds": 86_400,
        "evidence_id": evidence_id,
        "evidence_cursor": [block, 0],
        "evidence_at": evidence_at,
        "market": {
            "active": True,
            "closed": False,
            "accepting_orders": True,
        },
    }
    diamond = {
        "schema_version": risk_engine.SCHEMA_VERSION,
        "source_updated_at": evidence_at,
        "last_trade_at": evidence_at,
        "market_key": f"{condition_id}:yes",
        "question": "Synthetic V3 Binary market",
        "condition_id": condition_id,
        "token_id": token_id,
        "asset_type": "POSITION",
        "protocol_version": "V2",
        "outcome": "Yes",
        "direction": "BUY",
        "price": 0.50,
        "classification": "DIAMOND",
        "diamond": True,
        "why_not_diamond": [],
        "metrics": {
            "verified": True,
            "confirmations": 3,
            "largest_trade_ratio": 0.20,
        },
        "resolution": {"remaining_seconds": 86_400},
        "cashflow_alert": {"quality": "BROAD"},
    }

    generation_dir = data_dir / "diamond_generations" / generation
    generation_dir.mkdir(parents=True)
    save_json_atomic(generation_dir / "diamond_analysis_v3.json", [diamond])
    save_json_atomic(generation_dir / "flow_state.json", {token_id: flow})
    save_json_atomic(
        data_dir / "diamond_generation.json",
        {
            "schema_version": 1,
            "generation_id": generation,
            "published_at": evidence_at,
            "generation_dir": f"diamond_generations/{generation}",
        },
    )
    return evidence_id


def _raw_book(token_id, condition_id):
    return {
        "asset_id": token_id,
        "market": condition_id,
        "timestamp": "synthetic-book-time",
        "hash": "synthetic-book-hash",
        "bids": [{"price": "0.49", "size": "1000"}],
        "asks": [{"price": "0.50", "size": "1000"}],
        "min_order_size": "5",
        "tick_size": "0.01",
        "neg_risk": False,
    }


def _configure(monkeypatch, tmp_path):
    data = tmp_path / "runtime-data"
    data.mkdir()
    request_dir = data / "paper_requests"

    for module, names in (
        (risk_engine, {"DATA_DIR": data, "ANALYSIS_FILE": data / "unused-analysis.json", "FLOW_FILE": data / "unused-flow.json", "RISK_FILE": data / "risk_assessment.json"}),
        (focus_runner, {"DATA_DIR": data, "RISK_FILE": data / "risk_assessment.json", "STATE_FILE": data / "focus_state.json", "FOCUS_FILE": data / "focused_market.json", "EVENTS_FILE": data / "focus_events.jsonl"}),
        (book_worker, {"DATA_DIR": data, "FOCUS_FILE": data / "focused_market.json", "BOOK_FILE": data / "book_assessment.json"}),
        (paper_worker, {"DATA_DIR": data, "FOCUS_FILE": data / "focused_market.json", "BOOK_FILE": data / "book_assessment.json", "REQUEST_DIR": request_dir, "STATE_FILE": data / "paper_state.json", "EVENTS_FILE": data / "paper_events.jsonl"}),
        (telegram_bot, {"DATA": data, "FOCUS": data / "focused_market.json", "BOOK": data / "book_assessment.json", "PAPER_REQUEST_DIR": request_dir, "PAPER_STATE": data / "paper_state.json", "STATE": data / "telegram_state.json"}),
    ):
        for name, value in names.items():
            monkeypatch.setattr(module, name, value)

    request_dir.mkdir()
    monkeypatch.setattr(risk_engine, "learning_strategy_versions", lambda: None)
    monkeypatch.setattr(risk_worker, "_LEARNING_INIT_ERROR", "offline test")
    monkeypatch.setattr(book_worker, "_queue_learning", lambda _output: None)
    monkeypatch.setattr(paper_worker, "_learning_versions_at_open", lambda: None)
    monkeypatch.setattr(machine_common, "datetime", VirtualDateTime)
    monkeypatch.setattr(risk_engine, "datetime", VirtualDateTime)
    monkeypatch.setattr(telegram_bot, "datetime", VirtualDateTime)
    monkeypatch.setattr(paper_worker, "datetime", VirtualDateTime)

    return data


def _advance_to_ready(monkeypatch, tmp_path, module_id=1):
    data = _configure(monkeypatch, tmp_path)
    token_id, condition_id = _v3_binary_identity(module_id)
    base = datetime.now(timezone.utc) - timedelta(seconds=183)
    evidence_ids = []

    for index in range(4):
        now = base + timedelta(seconds=61 * index)
        VirtualDateTime.current = now
        evidence_ids.append(
            _risk_inputs(
                data,
                now,
                f"GEN-{index}",
                token_id,
                condition_id,
                index + 1,
            )
        )
        monkeypatch.setattr(sys, "argv", ["risk_worker.py", "--once"])
        assert risk_worker.main() == 0
        risk_payload = json.loads((data / "risk_assessment.json").read_text(encoding="utf-8"))
        assert risk_payload["passed"] == 1
        assert risk_payload["results"][0]["asset_type"] == "POSITION"
        assert risk_payload["results"][0]["protocol_version"] == "V2"

        state, events, status = focus_runner.run_once(now=now)
        assert status == "OK"
        if index < 3:
            assert state["focus"]["status"] != "READY"
        else:
            assert state["focus"]["status"] == "READY"

    assert len(set(evidence_ids)) == 4
    focus_payload = json.loads((data / "focused_market.json").read_text(encoding="utf-8"))
    focus = focus_payload["focus"]
    assert focus["progress"] == 3
    assert focus["last_evidence_id"] == evidence_ids[-1]
    assert focus["last_evidence_id"] != evidence_ids[0]

    book_output = book_worker.run_once(
        now=VirtualDateTime.current,
        book_loader=lambda requested_token: _raw_book(requested_token, condition_id),
    )
    assert book_output["status"] == "OK"
    assert book_output["book_ok"] is True
    assert book_output["token_id"] == token_id
    assert book_output["condition_id"] == condition_id
    assert book_output["source_generation_id"] == focus_payload["source_generation_id"]
    assert book_output["source_evidence_id"] == focus["last_evidence_id"]
    return data, token_id, condition_id, focus_payload, book_output


def _paper_kwargs(token_id, condition_id):
    def resolve_identity(condition, token, **identity_type):
        return paper_settlement.resolve_position_identity(
            condition,
            token,
            **identity_type,
            validate_chain=False,
        )

    def settlement_unresolved(_position):
        return {
            "status": paper_settlement.UNRESOLVED,
            "reason_code": None,
            "settlement_read_block": 100,
            "settlement_read_block_hash": "0x" + "aa" * 32,
            "settlement_authority": "synthetic-read-only",
            "settlement_finality_source": "SYNTHETIC_TEST",
        }

    return {
        "book_loader": lambda requested_token: _raw_book(requested_token, condition_id),
        "market_info_loader": lambda _condition: {
            "t": [{"t": token_id}],
            "fd": {"r": 0, "e": 1},
        },
        "identity_loader": resolve_identity,
        "settlement_checker": settlement_unresolved,
    }


def _write_explicit_request(data, token_id, condition_id, focus_payload):
    request_id, error = telegram_bot.submit_open_request(25, "synthetic-chat")
    assert error is None
    request = json.loads(
        (data / "paper_requests" / f"{request_id}.json").read_text(encoding="utf-8")
    )
    assert request["action"] == "OPEN"
    assert request["token_id"] == token_id
    assert request["condition_id"] == condition_id
    assert request["source_generation_id"] == focus_payload["source_generation_id"]
    assert request["source_evidence_id"] == focus_payload["focus"]["last_evidence_id"]
    assert request["focus_locked_at"] == focus_payload["focus"]["locked_at"]
    assert not (data / "paper_state.json").exists()
    assert not (data / "telegram_state.json").exists()
    return request_id


@pytest.mark.parametrize(
    ("module_id", "expected_family"),
    [(1, "V2_BINARY"), (2, "V2_NEGRISK")],
)
def test_multicycle_v3_risk_focus_book_telegram_paper_path(
    monkeypatch, tmp_path, module_id, expected_family,
):
    data, token_id, condition_id, focus_payload, book_output = _advance_to_ready(
        monkeypatch, tmp_path, module_id=module_id,
    )
    request_id = _write_explicit_request(data, token_id, condition_id, focus_payload)

    state, events = paper_worker.run_once(
        now=VirtualDateTime.current,
        **_paper_kwargs(token_id, condition_id),
    )

    assert [event["type"] for event in events] == ["OPENED"]
    assert len(state["positions"]) == 1
    position = state["positions"][0]
    assert position["status"] == "OPEN"
    assert position["request_id"] == request_id
    assert position["token_id"] == token_id
    assert position["condition_id"] == condition_id
    assert position["asset_type"] == "POSITION"
    assert position["protocol_version"] == "V2"
    assert position["settlement_protocol"] == paper_settlement.PROTOCOL_V2_POSITION
    assert position["settlement_family"] == expected_family
    assert "ctf_contract" not in position
    assert position["position_module_id"] == module_id
    assert position["source_generation_id"] == book_output["source_generation_id"]
    assert position["source_evidence_id"] == book_output["source_evidence_id"]
    assert state["processed_request_ids"] == [request_id]


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("generated_at", "stale", "INPUT_NOT_OK"),
        ("token_id", "", "TOKEN_ID_MISMATCH"),
        ("token_id", "wrong-position", "TOKEN_ID_MISMATCH"),
        ("condition_id", "", "CONDITION_ID_MISMATCH"),
        ("condition_id", "0x" + "ff" * 31, "CONDITION_ID_MISMATCH"),
        ("source_generation_id", "OTHER-GEN", "GENERATION_ID_MISMATCH"),
        ("source_evidence_id", "0x" + "ee" * 32 + ":0", "EVIDENCE_ID_MISMATCH"),
    ],
)
def test_paper_worker_rejects_stale_or_unbound_book_after_user_request(
    monkeypatch, tmp_path, field, value, expected_reason,
):
    data, token_id, condition_id, focus_payload, _ = _advance_to_ready(
        monkeypatch, tmp_path,
    )
    request_id = _write_explicit_request(data, token_id, condition_id, focus_payload)

    book_path = data / "book_assessment.json"
    altered = json.loads(book_path.read_text(encoding="utf-8"))
    if field == "generated_at":
        altered[field] = (VirtualDateTime.current - timedelta(minutes=10)).isoformat()
        expected_reason = "INPUT_NOT_OK"
    else:
        altered[field] = value
    save_json_atomic(book_path, altered)

    state, events = paper_worker.run_once(
        now=VirtualDateTime.current,
        **_paper_kwargs(token_id, condition_id),
    )

    assert len(events) == 1
    assert events[0]["type"] == "REJECTED"
    assert events[0]["request_id"] == request_id
    assert expected_reason in events[0]["reason_code"]
    assert state["positions"] == []
