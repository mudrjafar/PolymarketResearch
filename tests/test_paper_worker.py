import json
from datetime import datetime, timedelta, timezone

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
        "focus_state": data / "focus_state.json",
        "book": data / "book_assessment.json",
        "requests": request_dir,
        "state": data / "paper_state.json",
        "events": data / "paper_events.jsonl",
    }

    monkeypatch.setattr(paper_worker, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(paper_worker, "FOCUS_FILE", paths["focus"], raising=False)
    monkeypatch.setattr(paper_worker, "FOCUS_STATE_FILE", paths["focus_state"], raising=False)
    monkeypatch.setattr(paper_worker, "BOOK_FILE", paths["book"], raising=False)
    monkeypatch.setattr(paper_worker, "REQUEST_DIR", paths["requests"], raising=False)
    monkeypatch.setattr(paper_worker, "STATE_FILE", paths["state"], raising=False)
    monkeypatch.setattr(paper_worker, "EVENTS_FILE", paths["events"], raising=False)
    return paths


def write_gate(
    paths,
    *,
    state="READY",
    generation="GEN-X",
    evidence=None,
    outcome="Yes",
):
    evidence = evidence or ("0x" + "ab" * 32 + ":7")
    focus = {
        "token_id": "token-a",
        "condition_id": "condition-a",
        "outcome": outcome,
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
        paths["focus_state"],
        {
            "schema_version": 1,
            "updated_at": NOW.isoformat(),
            "last_source_generation_id": generation,
            "engine_state": {"version": 3, "focus": focus},
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
        "timestamp": str(int(NOW.timestamp() * 1000)),
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
        "settlement_protocol": paper_settlement.PROTOCOL_LEGACY_CTF,
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
    assert position["settlement_protocol"] == paper_settlement.PROTOCOL_LEGACY_CTF
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


@pytest.mark.parametrize(
    ("settlement", "expected_reason"),
    [
        (lambda position: final_settlement(position), "SETTLEMENT_FINAL_SETTLED"),
        (lambda position: resolved_not_final(position), "SETTLEMENT_RESOLVED_NOT_FINAL"),
        (lambda position: settlement_error(position), "SETTLEMENT_SETTLEMENT_CHECK_ERROR"),
        (
            lambda _: {
                "status": paper_settlement.UNRESOLVED,
                "reason_code": "LATEST_DIAGNOSTIC_UNAVAILABLE",
            },
            "SETTLEMENT_UNRESOLVED_LATEST_DIAGNOSTIC_UNAVAILABLE",
        ),
        (lambda _: {"status": "UNRECOGNIZED"}, "SETTLEMENT_UNKNOWN"),
        (lambda _: None, "SETTLEMENT_RESPONSE_INVALID"),
        (lambda _: (_ for _ in ()).throw(RuntimeError("offline")),
         "SETTLEMENT_CHECK_ERROR_RuntimeError"),
    ],
)
def test_open_rejects_resolved_unknown_or_unavailable_settlement(
    monkeypatch, tmp_path, settlement, expected_reason
):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    book_loader, market_loader = loaders()
    checked = []

    def check(position):
        checked.append(position)
        return settlement(position)

    state, events = run_worker(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
        settlement_checker=check,
    )

    assert state["positions"] == []
    rejected = [event for event in events if event["type"] == "REJECTED"]
    assert len(rejected) == 1
    assert rejected[0]["reason_code"] == expected_reason
    assert len(checked) == 1
    assert checked[0]["condition_id"] == "condition-a"
    assert checked[0]["token_id"] == "token-a"
    assert checked[0]["outcome_index"] == 0


def test_open_settlement_gate_accepts_only_clean_unresolved(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    book_loader, market_loader = loaders()

    state, events = run_worker(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
        settlement_checker=unresolved_settlement,
    )

    assert [event["type"] for event in events] == ["OPENED"]
    assert len(state["positions"]) == 1


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
    assert position["settlement_protocol"] == paper_settlement.PROTOCOL_LEGACY_CTF
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


def test_open_rejects_outcome_label_that_disagrees_with_token_identity(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths, outcome="No")
    write_open_request(paths)
    book_loader, market_loader = loaders()

    state, events = run_worker(
        now=NOW,
        book_loader=book_loader,
        market_info_loader=market_loader,
    )

    assert state["positions"] == []
    assert len(events) == 1
    assert events[0]["type"] == "REJECTED"
    assert events[0]["reason_code"] == (
        "SETTLEMENT_IDENTITY_OUTCOME_LABEL_INDEX_MISMATCH"
    )


def test_legacy_backfill_rejects_outcome_identity_mismatch(monkeypatch, tmp_path):
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
    position["outcome"] = "No"
    save_json_atomic(paths["state"], state)

    state, _ = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )

    position = state["positions"][0]
    assert position["status"] == "OPEN"
    assert position["settlement_status"] == paper_settlement.IDENTITY_MISMATCH
    assert position["settlement_reason_code"] == "OUTCOME_LABEL_INDEX_MISMATCH"





def _learning_versions():
    return {
        "pipeline_version": "pipeline-v3",
        "diamond_version": "diamond-v3.1",
        "risk_version": "risk-v2",
        "focus_version": "focus-v3",
        "book_version": "book-v1",
        "paper_version": "paper-v1",
        "learning_schema_version": 1,
        "git_commit_sha": "a" * 40,
        "config_fingerprint": "SRC-paper-worker-test",
    }


def test_successful_open_freezes_provenance_and_queues_sanitized_snapshot(
    monkeypatch, tmp_path
):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    captured = []

    monkeypatch.setattr(
        paper_worker,
        "_LEARNING_IMPORT_ERROR",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        paper_worker,
        "runtime_strategy_versions",
        lambda: _learning_versions(),
        raising=False,
    )
    monkeypatch.setattr(
        paper_worker,
        "enqueue_paper_open_snapshot",
        lambda snapshot, **kwargs: captured.append((snapshot, kwargs))
        or {"status": "QUEUED"},
        raising=False,
    )

    state, events = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )

    assert any(event["type"] == "OPENED" for event in events)
    assert len(captured) == 1
    snapshot, kwargs = captured[0]
    position = state["positions"][0]

    assert position["learning_strategy_versions"] == _learning_versions()
    assert position["learning_open_queued_at"] == NOW.isoformat()
    assert snapshot["paper_id"] == position["paper_id"]
    assert snapshot["open_request_id"] == "REQ-open"
    assert snapshot["source_generation_id"] == "GEN-X"
    assert snapshot["source_evidence_id"].endswith(":7")
    assert snapshot["entry"]["effective_entry_price"] is not None
    assert "chat_id" not in snapshot
    assert kwargs["strategy_versions"] == _learning_versions()
    assert kwargs["data_dir"] == paths["data"]

    stored = json.loads(paths["state"].read_text(encoding="utf-8"))
    assert stored["positions"][0]["learning_open_queued_at"] == NOW.isoformat()


def test_learning_queue_failure_never_rejects_successful_paper_open(
    monkeypatch, tmp_path, capsys
):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)

    monkeypatch.setattr(
        paper_worker,
        "_LEARNING_IMPORT_ERROR",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        paper_worker,
        "runtime_strategy_versions",
        lambda: _learning_versions(),
        raising=False,
    )

    def broken_queue(*args, **kwargs):
        raise RuntimeError("learning unavailable")

    monkeypatch.setattr(
        paper_worker,
        "enqueue_paper_open_snapshot",
        broken_queue,
        raising=False,
    )

    state, events = run_worker(
        now=NOW,
        book_loader=lambda _: raw_book(),
        market_info_loader=lambda _: market_info(),
    )

    opened = [event for event in events if event["type"] == "OPENED"]
    assert len(opened) == 1
    assert state["positions"][0]["status"] == "OPEN"
    assert state["positions"][0]["learning_open_queued_at"] is None
    assert "PAPER_QUEUE_ERROR" in capsys.readouterr().out

    stored = json.loads(paths["state"].read_text(encoding="utf-8"))
    assert stored["positions"][0]["status"] == "OPEN"


def test_durable_focus_revocation_blocks_old_ready_view(monkeypatch, tmp_path):
    """A torn Focus publication must not leave an executable READY view."""
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    from machine_common import save_json_atomic
    persisted = json.loads(paths["focus_state"].read_text(encoding="utf-8"))
    persisted["engine_state"]["focus"]["status"] = "WAIT"
    persisted["engine_state"]["focus"]["progress"] = 2
    save_json_atomic(paths["focus_state"], persisted)
    assert paper_worker.read_execution_gate(now=NOW)[0] == "FOCUS_DURABLE_NOT_READY"


def test_missing_or_mismatched_durable_focus_never_authorizes_open(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    paths["focus_state"].unlink()
    assert paper_worker.read_execution_gate(now=NOW)[0] == "FOCUS_STATE_MISSING"

    write_gate(paths)
    persisted = json.loads(paths["focus_state"].read_text(encoding="utf-8"))
    persisted["updated_at"] = "2026-01-01T00:00:00+00:00"
    save_json_atomic(paths["focus_state"], persisted)
    assert paper_worker.read_execution_gate(now=NOW)[0] == "FOCUS_STATE_VIEW_MISMATCH"


def test_second_paper_writer_cannot_enter_state_transaction(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    lock_path = paths["state"].with_suffix(".lock")
    with paper_worker.PaperWorkerLock(lock_path):
        with pytest.raises(paper_worker.PaperStateError, match="Concurrent Paper worker"):
            run_worker(now=NOW, book_loader=lambda _: raw_book(),
                       market_info_loader=lambda _: market_info())
        assert not paths["state"].exists()
    # Lock release allows a subsequent worker (or supervised restart) to
    # process the same explicit request exactly once.
    state, events = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                               market_info_loader=lambda _: market_info())
    assert len(state["positions"]) == 1
    assert [event["type"] for event in events] == ["OPENED"]
    state2, events2 = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                                 market_info_loader=lambda _: market_info())
    assert len(state2["positions"]) == 1
    assert not any(event["type"] == "OPENED" for event in events2)


def test_close_survives_revoked_durable_focus_authority(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    state, _ = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                          market_info_loader=lambda _: market_info())
    paper_id = state["positions"][0]["paper_id"]
    committed = json.loads(paths["focus_state"].read_text(encoding="utf-8"))
    committed["engine_state"]["focus"] = None
    save_json_atomic(paths["focus_state"], committed)
    write_close_request(paths, paper_id)
    state2, events = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                                market_info_loader=lambda _: market_info())
    assert state2["positions"][0]["status"] == "CLOSED"
    assert any(event["type"] == "CLOSED" for event in events)
    assert paper_worker.read_execution_gate(now=NOW)[0] == "FOCUS_DURABLE_NOT_READY"


def test_unknown_focus_engine_version_blocks_new_open(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    payload = json.loads(paths["focus_state"].read_text(encoding="utf-8"))
    payload["engine_state"]["version"] = 999
    save_json_atomic(paths["focus_state"], payload)
    write_open_request(paths)
    assert paper_worker.read_execution_gate(now=NOW)[0] == "FOCUS_STATE_INVALID"
    state, events = run_worker(now=NOW, book_loader=lambda _: (_ for _ in ()).throw(
        AssertionError("No CLOB call for invalid Focus")),
        market_info_loader=lambda _: {})
    assert state["positions"] == []
    assert events[0]["reason_code"] == "FOCUS_STATE_INVALID"


def test_concurrent_paper_workers_are_serialized_with_thread_barrier(monkeypatch, tmp_path):
    import threading
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    entered = threading.Event()
    release = threading.Event()
    result = []

    def delayed_book(_):
        entered.set()
        assert release.wait(timeout=8), "offline barrier unexpectedly timed out"
        return raw_book()

    def first():
        try:
            result.append(run_worker(
                now=NOW, book_loader=delayed_book,
                market_info_loader=lambda _: market_info()
            ))
        except Exception as exc:
            result.append(exc)

    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert entered.wait(timeout=8), "first worker never reached network boundary"
        with pytest.raises(paper_worker.PaperStateError, match="Concurrent Paper worker"):
            run_worker(now=NOW, book_loader=lambda _: raw_book(),
                       market_info_loader=lambda _: market_info())
    finally:
        release.set()
        worker.join(timeout=8)
    assert not worker.is_alive()
    assert len(result) == 1 and not isinstance(result[0], Exception)
    assert len(result[0][0]["positions"]) == 1
    replay, events = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                                market_info_loader=lambda _: market_info())
    assert len(replay["positions"]) == 1
    assert not any(row["type"] == "OPENED" for row in events)


def test_crash_after_paper_state_save_does_not_reopen_request(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    append = paper_worker._append_events

    def crash_before_event_journal(_events):
        raise RuntimeError("simulated crash after durable Paper commit")

    monkeypatch.setattr(paper_worker, "_append_events", crash_before_event_journal)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_worker(now=NOW, book_loader=lambda _: raw_book(),
                   market_info_loader=lambda _: market_info())
    durable = json.loads(paths["state"].read_text(encoding="utf-8"))
    assert len(durable["positions"]) == 1
    assert "REQ-open" in durable["processed_request_ids"]
    monkeypatch.setattr(paper_worker, "_append_events", append)
    state, events = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                                market_info_loader=lambda _: market_info())
    assert len(state["positions"]) == 1
    assert not any(row["type"] == "OPENED" for row in events)


@pytest.mark.parametrize("field,reason", [
    ("asset_id", "TOKEN_ID_MISSING"),
    ("market", "CONDITION_ID_MISSING"),
])
def test_close_rejects_missing_clob_identity_without_changing_position(monkeypatch, tmp_path, field, reason):
    """F1 regression: no virtual realised P&L against unbound bid depth."""
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    state, _ = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                          market_info_loader=lambda _: market_info())
    paper_id = state["positions"][0]["paper_id"]
    write_close_request(paths, paper_id)
    unbound = raw_book()
    unbound.pop(field)
    state, events = run_worker(now=NOW, book_loader=lambda _: unbound,
                               market_info_loader=lambda _: market_info())
    assert state["positions"][0]["status"] == "OPEN"
    assert state["positions"][0]["mark_status"] == reason
    assert state["positions"][0].get("realized_pnl_usd") is None
    assert any(e["type"] == "REJECTED" and e["reason_code"] == reason for e in events)
    assert not any(e["type"] == "CLOSED" for e in events)


def test_mark_unbound_book_never_recalculates_pnl(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    state, _ = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                          market_info_loader=lambda _: market_info())
    bad = raw_book()
    bad.pop("market")
    state, _ = run_worker(now=NOW, book_loader=lambda _: bad,
                          market_info_loader=lambda _: market_info())
    position = state["positions"][0]
    assert position["status"] == "OPEN"
    assert position["mark_status"] == "CONDITION_ID_MISSING"


@pytest.mark.parametrize("value,reason", [
    (None, "BOOK_TIMESTAMP_MISSING"),
    ("123", "BOOK_TIMESTAMP_INVALID"),
    ("not-an-epoch", "BOOK_TIMESTAMP_INVALID"),
    ("STALE", "BOOK_TIMESTAMP_STALE"),
    ("FUTURE", "BOOK_TIMESTAMP_FUTURE"),
])
def test_open_rejects_untrusted_source_book_timestamp(monkeypatch, tmp_path, value, reason):
    """F2 regression: fresh local assessment cannot bless a stale source quote."""
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    bad = raw_book()
    if value == "STALE":
        value = str(int((NOW - timedelta(seconds=31)).timestamp() * 1000))
    if value == "FUTURE":
        value = str(int((NOW + timedelta(seconds=6)).timestamp() * 1000))
    if value is None:
        bad.pop("timestamp")
    else:
        bad["timestamp"] = value
    state, events = run_worker(now=NOW, book_loader=lambda _: bad,
                               market_info_loader=lambda _: market_info())
    assert state["positions"] == []
    assert any(e["type"] == "REJECTED" and e["reason_code"] == reason for e in events)
    assert not any(e["type"] == "OPENED" for e in events)


def test_final_paper_commit_rechecks_same_raw_quote_after_31s(monkeypatch, tmp_path):
    """F2 TOCTOU: local Focus+Book snapshots are fresh, but entry quote aged."""
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    late = NOW + timedelta(seconds=31)
    # Quote arrives at NOW; subsequent settlement / mark work advances time.
    ticks = iter((NOW, NOW, late))
    state, events = run_worker(now=NOW, clock=lambda: next(ticks),
                               book_loader=lambda _: raw_book(),
                               market_info_loader=lambda _: market_info())
    assert state["positions"] == []
    assert any(e["type"] == "REJECTED" and
               e["reason_code"] == "FINAL_COMMIT_BOOK_TIMESTAMP_STALE" for e in events)
    assert not any(e["type"] == "OPENED" for e in events)


def test_close_rejects_expired_quote_without_realizing_pnl(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    state, _ = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                          market_info_loader=lambda _: market_info())
    paper_id = state["positions"][0]["paper_id"]
    write_close_request(paths, paper_id)
    expired = raw_book()
    expired["timestamp"] = str(int((NOW - timedelta(seconds=60)).timestamp() * 1000))
    state, events = run_worker(now=NOW, book_loader=lambda _: expired,
                               market_info_loader=lambda _: market_info())
    assert state["positions"][0]["status"] == "OPEN"
    assert any(e["type"] == "REJECTED" and e["reason_code"] == "BOOK_TIMESTAMP_STALE" for e in events)


def test_close_accepts_book_arriving_after_seven_seconds(monkeypatch, tmp_path):
    """Old tick-start clock would falsely reject a fresh book as future."""
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    state, _ = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                          market_info_loader=lambda _: market_info())
    paper_id = state["positions"][0]["paper_id"]
    write_close_request(paths, paper_id)
    late = NOW + timedelta(seconds=7)
    book = raw_book()
    book["timestamp"] = str(int(late.timestamp() * 1000))
    state, events = run_worker(now=NOW, clock=lambda: late,
                               book_loader=lambda _: book,
                               market_info_loader=lambda _: market_info())
    assert state["positions"][0]["status"] == "CLOSED"
    assert state["positions"][0]["exit"]["book_received_at"] == late.isoformat()
    assert any(e["type"] == "CLOSED" for e in events)


def test_close_rolls_back_when_quote_expires_before_commit(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    state, _ = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                          market_info_loader=lambda _: market_info())
    paper_id = state["positions"][0]["paper_id"]
    write_close_request(paths, paper_id)
    times = iter((NOW, NOW + timedelta(seconds=31)))
    state, events = run_worker(
        now=NOW, clock=lambda: next(times),
        book_loader=lambda _: raw_book(), market_info_loader=lambda _: market_info()
    )
    position = state["positions"][0]
    assert position["status"] == "OPEN"
    assert position.get("realized_pnl_usd") is None
    assert position["mark"] is None
    assert any(e["type"] == "REJECTED" and
               e["reason_code"] == "FINAL_COMMIT_BOOK_TIMESTAMP_STALE" for e in events)
    assert not any(e["type"] == "CLOSED" for e in events)


def test_mark_uses_book_receipt_time_and_clears_previous_value(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    state, _ = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                          market_info_loader=lambda _: market_info())
    assert state["positions"][0]["mark_status"] == "OK"
    late = NOW + timedelta(seconds=7)
    fresh = raw_book()
    fresh["timestamp"] = str(int(late.timestamp() * 1000))
    state, _ = run_worker(
        now=NOW, clock=lambda: late,
        book_loader=lambda _: fresh, market_info_loader=lambda _: market_info()
    )
    assert state["positions"][0]["mark_status"] == "OK"
    assert state["positions"][0]["mark"]["book_received_at"] == late.isoformat()
    bad = dict(fresh)
    bad.pop("asset_id")
    state, _ = run_worker(
        now=NOW, clock=lambda: late,
        book_loader=lambda _: bad, market_info_loader=lambda _: market_info()
    )
    assert state["positions"][0]["mark_status"] == "TOKEN_ID_MISSING"
    assert state["positions"][0]["mark"] is None


def test_mark_rolls_to_stale_before_state_save(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    run_worker(now=NOW, book_loader=lambda _: raw_book(),
               market_info_loader=lambda _: market_info())
    times = iter((NOW, NOW + timedelta(seconds=31)))
    state, _ = run_worker(
        now=NOW, clock=lambda: next(times),
        book_loader=lambda _: raw_book(), market_info_loader=lambda _: market_info()
    )
    pos = state["positions"][0]
    assert pos["status"] == "OPEN"
    assert pos["mark_status"] == "BOOK_TIMESTAMP_STALE"
    assert pos["mark"] is None


def test_open_final_commit_catches_expired_local_receipt_even_if_source_is_recent(
    monkeypatch, tmp_path
):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    newer = raw_book()
    newer["timestamp"] = str(int((NOW + timedelta(seconds=5)).timestamp() * 1000))
    # Source timestamp is only 26s old at final commit, but our locally
    # received response is already 31s old: that must not create a Paper OPEN.
    times = iter((NOW, NOW, NOW + timedelta(seconds=31)))
    state, events = run_worker(
        now=NOW, clock=lambda: next(times),
        book_loader=lambda _: newer,
        market_info_loader=lambda _: market_info()
    )
    assert state["positions"] == []
    assert any(e["type"] == "REJECTED" and
               e["reason_code"] == "FINAL_COMMIT_BOOK_RECEIPT_STALE" for e in events)


def test_old_source_with_fresh_http_receipt_is_conservatively_rejected_for_open(
    monkeypatch, tmp_path
):
    """Unknown CLOB timestamp update guarantees: keep explicit fail-closed policy."""
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    old = raw_book()
    old["timestamp"] = str(int((NOW - timedelta(seconds=120)).timestamp() * 1000))
    state, events = run_worker(
        now=NOW, clock=lambda: NOW,
        book_loader=lambda _: old, market_info_loader=lambda _: market_info()
    )
    assert state["positions"] == []
    assert any(e["reason_code"] == "BOOK_TIMESTAMP_STALE" for e in events)


# GENERAL1-REQUEST-INTEGRITY-FINAL: reproducing tests, initially red on PR #50.
def _set_request_time(paths, request_id, requested_at):
    path = paths["requests"] / (request_id + ".json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["requested_at"] = requested_at
    save_json_atomic(path, payload)


def test_request_order_follows_submission_time_not_random_filename(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths, request_id="REQ-z-older")
    write_open_request(paths, request_id="REQ-a-newer")
    _set_request_time(paths, "REQ-z-older", (NOW - timedelta(seconds=2)).isoformat())
    _set_request_time(paths, "REQ-a-newer", (NOW - timedelta(seconds=1)).isoformat())

    state, events = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                               market_info_loader=lambda _: market_info())
    assert len(state["positions"]) == 1
    assert state["positions"][0]["request_id"] == "REQ-z-older"
    assert [(e["request_id"], e["type"]) for e in events
            if e.get("request_id")] == [
                ("REQ-z-older", "OPENED"),
                ("REQ-a-newer", "REJECTED"),
            ]


def test_close_cannot_execute_tentative_open_in_same_paper_tick(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths, request_id="REQ-a-open")
    write_close_request(paths, "PAPER-a-open", request_id="REQ-z-close")
    _set_request_time(paths, "REQ-a-open", (NOW - timedelta(seconds=1)).isoformat())

    state, events = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                               market_info_loader=lambda _: market_info())
    assert len(state["positions"]) == 1
    assert state["positions"][0]["status"] == "OPEN"
    assert [(e["request_id"], e["type"]) for e in events
            if e.get("request_id")] == [
                ("REQ-a-open", "OPENED"),
                ("REQ-z-close", "REJECTED"),
            ]
    assert events[1]["reason_code"] == "POSITION_NOT_COMMITTED"
    assert not any(e["type"] == "CLOSED" for e in events)


def test_rejected_tentative_open_does_not_erase_dependent_close_request(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths, request_id="REQ-a-open")
    write_close_request(paths, "PAPER-a-open", request_id="REQ-z-close")
    _set_request_time(paths, "REQ-a-open", (NOW - timedelta(seconds=1)).isoformat())
    calls = [0]

    def invalidate_at_second_quote_clock():
        calls[0] += 1
        if calls[0] == 2:
            write_gate(paths, state="WAIT")
        return NOW

    state, events = run_worker(
        now=NOW, clock=invalidate_at_second_quote_clock,
        book_loader=lambda _: raw_book(), market_info_loader=lambda _: market_info()
    )
    assert state["positions"] == []
    by_request = {e["request_id"]: e for e in events if e.get("request_id")}
    assert set(by_request) == {"REQ-a-open", "REQ-z-close"}
    assert by_request["REQ-a-open"]["type"] == "REJECTED"
    assert by_request["REQ-a-open"]["reason_code"] == "FINAL_COMMIT_FOCUS_NOT_READY"
    assert by_request["REQ-z-close"]["type"] == "REJECTED"
    assert by_request["REQ-z-close"]["reason_code"] == "POSITION_NOT_COMMITTED"
    assert {"REQ-a-open", "REQ-z-close"}.issubset(state["processed_request_ids"])


def test_bad_request_timestamp_is_distinct_from_expired_request(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths, request_id="REQ-bad-time")
    _set_request_time(paths, "REQ-bad-time", "unparseable")
    state, events = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                               market_info_loader=lambda _: market_info())
    assert state["positions"] == []
    assert len(events) == 1
    assert events[0]["reason_code"] == "REQUEST_TIMESTAMP_INVALID"


def test_open_close_open_same_tick_has_one_committed_position_and_three_outcomes(
    monkeypatch, tmp_path
):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths, request_id="REQ-z-first-open")
    write_close_request(paths, "PAPER-z-first-open", request_id="REQ-m-close")
    write_open_request(paths, request_id="REQ-a-second-open")
    _set_request_time(paths, "REQ-z-first-open", (NOW - timedelta(seconds=3)).isoformat())
    _set_request_time(paths, "REQ-m-close", (NOW - timedelta(seconds=2)).isoformat())
    _set_request_time(paths, "REQ-a-second-open", (NOW - timedelta(seconds=1)).isoformat())

    state, events = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                               market_info_loader=lambda _: market_info())
    assert [(e["request_id"], e["type"]) for e in events
            if e.get("request_id")] == [
                ("REQ-z-first-open", "OPENED"),
                ("REQ-m-close", "REJECTED"),
                ("REQ-a-second-open", "REJECTED"),
            ]
    assert events[1]["reason_code"] == "POSITION_NOT_COMMITTED"
    assert events[2]["reason_code"] == "POSITION_ALREADY_OPEN"
    assert len(state["positions"]) == 1
    assert state["positions"][0]["status"] == "OPEN"
    assert state["positions"][0]["request_id"] == "REQ-z-first-open"


def test_two_close_requests_for_committed_position_are_individually_accounted(
    monkeypatch, tmp_path
):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths)
    state, _ = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                          market_info_loader=lambda _: market_info())
    paper_id = state["positions"][0]["paper_id"]
    write_close_request(paths, paper_id, request_id="REQ-z-close-earlier")
    write_close_request(paths, paper_id, request_id="REQ-a-close-later")
    _set_request_time(paths, "REQ-z-close-earlier", (NOW - timedelta(seconds=2)).isoformat())
    _set_request_time(paths, "REQ-a-close-later", (NOW - timedelta(seconds=1)).isoformat())
    state, events = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                               market_info_loader=lambda _: market_info())
    assert [(e["request_id"], e["type"]) for e in events
            if e.get("request_id")] == [
                ("REQ-z-close-earlier", "CLOSED"),
                ("REQ-a-close-later", "REJECTED"),
            ]
    assert events[1]["reason_code"] == "OPEN_POSITION_NOT_FOUND"
    assert state["positions"][0]["status"] == "CLOSED"
    assert state["positions"][0]["close_request_id"] == "REQ-z-close-earlier"


def test_same_request_timestamps_tie_break_by_request_id(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_open_request(paths, request_id="REQ-z")
    write_open_request(paths, request_id="REQ-a")
    first = [row[0] for row in paper_worker._load_requests()]
    second = [row[0] for row in paper_worker._load_requests()]
    assert first == second == ["REQ-a", "REQ-z"]


def test_timestamp_timezone_offsets_are_normalized_before_sorting(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_open_request(paths, request_id="REQ-a-later")
    write_open_request(paths, request_id="REQ-z-earlier")
    _set_request_time(paths, "REQ-a-later", (NOW - timedelta(seconds=1)).isoformat())
    earlier = NOW - timedelta(seconds=2)
    # Equivalent UTC timestamp with a +02:00 offset must sort chronologically.
    plus_two = timezone(timedelta(hours=2))
    _set_request_time(paths, "REQ-z-earlier", earlier.astimezone(plus_two).isoformat())
    assert [r[0] for r in paper_worker._load_requests()] == [
        "REQ-z-earlier", "REQ-a-later"
    ]


@pytest.mark.parametrize("bad_time", ["", None, "2026-10-28T11:00:00", "NaN"])
def test_naive_missing_and_invalid_timestamp_fail_closed(monkeypatch, tmp_path, bad_time):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths, request_id="REQ-invalid")
    _set_request_time(paths, "REQ-invalid", bad_time)
    state, events = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                               market_info_loader=lambda _: market_info())
    assert state["positions"] == []
    assert len(events) == 1
    assert events[0]["reason_code"] == "REQUEST_TIMESTAMP_INVALID"
    assert "REQ-invalid" in state["processed_request_ids"]


def test_pending_open_then_book_generation_revoked_preserves_both_request_outcomes(
    monkeypatch, tmp_path
):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths, request_id="REQ-a-open")
    write_close_request(paths, "PAPER-a-open", request_id="REQ-z-close")
    _set_request_time(paths, "REQ-a-open", (NOW - timedelta(seconds=1)).isoformat())
    calls = [0]

    def fetch_and_revoke(_token):
        calls[0] += 1
        if calls[0] == 2:
            assessment = json.loads(paths["book"].read_text(encoding="utf-8"))
            assessment["source_generation_id"] = "DIFFERENT_GENERATION"
            save_json_atomic(paths["book"], assessment)
        return raw_book()

    state, events = run_worker(now=NOW, book_loader=fetch_and_revoke,
                               market_info_loader=lambda _: market_info())
    assert calls[0] == 2
    assert state["positions"] == []
    outcomes = {e["request_id"]: e for e in events if e.get("request_id")}
    assert outcomes["REQ-a-open"]["reason_code"] == "FINAL_COMMIT_GENERATION_ID_MISMATCH"
    assert outcomes["REQ-z-close"]["reason_code"] == "POSITION_NOT_COMMITTED"


def test_rewrite_same_request_file_during_fetch_does_not_change_snapshot_or_replay(
    monkeypatch, tmp_path
):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths, request_id="REQ-immutable")
    calls = [0]

    def fetch_with_request_rewrite(_token):
        calls[0] += 1
        if calls[0] == 1:
            path = paths["requests"] / "REQ-immutable.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["amount_usd"] = 100
            save_json_atomic(path, payload)
        return raw_book()

    state, events = run_worker(now=NOW, book_loader=fetch_with_request_rewrite,
                               market_info_loader=lambda _: market_info())
    assert len(state["positions"]) == 1
    assert state["positions"][0]["investment_usd"] == 25.0
    assert events[0]["type"] == "OPENED"
    state2, events2 = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                                 market_info_loader=lambda _: market_info())
    assert len(state2["positions"]) == 1
    assert not any(e.get("request_id") == "REQ-immutable" for e in events2)


def test_different_market_open_request_cannot_use_current_focus(monkeypatch, tmp_path):
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths, request_id="REQ-z-valid")
    write_open_request(paths, request_id="REQ-a-other")
    _set_request_time(paths, "REQ-z-valid", (NOW - timedelta(seconds=1)).isoformat())
    other = paths["requests"] / "REQ-a-other.json"
    payload = json.loads(other.read_text(encoding="utf-8"))
    payload["token_id"] = "token-b"
    payload["condition_id"] = "condition-b"
    save_json_atomic(other, payload)

    state, events = run_worker(now=NOW, book_loader=lambda _: raw_book(),
                               market_info_loader=lambda _: market_info())
    assert len(state["positions"]) == 1
    assert state["positions"][0]["token_id"] == "token-a"
    assert [e["type"] for e in events] == ["OPENED", "REJECTED"]
    assert events[1]["reason_code"] == "REQUEST_TOKEN_ID_MISMATCH"


def test_terminal_event_accounting_fails_closed_before_paper_state_save(
    monkeypatch, tmp_path
):
    """A future event-filter regression must never commit an unlogged request."""
    paths = configure(monkeypatch, tmp_path)
    write_gate(paths)
    write_open_request(paths, request_id="REQ-a-open")
    original = paper_worker._finalize_exits_and_marks

    def deliberately_erase_event(state, events, previous, commit_now):
        original(state, events, previous, commit_now)
        events[:] = [e for e in events if not e.get("request_id")]

    monkeypatch.setattr(
        paper_worker, "_finalize_exits_and_marks", deliberately_erase_event
    )
    with pytest.raises(paper_worker.PaperStateError, match="REQUEST_TERMINAL_EVENT_INCOMPLETE"):
        run_worker(now=NOW, book_loader=lambda _: raw_book(),
                   market_info_loader=lambda _: market_info())
    assert not paths["state"].exists()
    assert not paths["events"].exists()
