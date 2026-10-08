import json
from datetime import datetime, timedelta, timezone

import pytest

from machine_common import save_json_atomic
from scripts.learning_contract import (
    LEARNING_SCHEMA_VERSION,
    SELECTION_NOT_SELECTED,
    SELECTION_PENDING,
    SELECTION_SELECTED,
)
from scripts.learning_ingest import (
    LearningIngestError,
    finalize_ready_selection_from_paper_state,
    ingest_focus_queue_file,
    ingest_paper_open_queue_file,
)
from scripts.learning_queue import enqueue_focus_event, enqueue_paper_open_snapshot
from scripts.learning_store import LearningStore


T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
READY_AT = T0 + timedelta(minutes=1)
EVIDENCE = "0x" + "ab" * 32 + ":7"


def versions():
    return {
        "pipeline_version": "pipeline-v3",
        "diamond_version": "diamond-v3.1",
        "risk_version": "risk-v2",
        "focus_version": "focus-v3",
        "book_version": "book-v1",
        "paper_version": "paper-v1",
        "learning_schema_version": LEARNING_SCHEMA_VERSION,
        "git_commit_sha": "a" * 40,
        "config_fingerprint": "SRC-paper-test",
    }


def seed_ready(store, data):
    version_id = store.register_strategy_version(versions())
    store.insert_signal_observation(
        "SIG-A",
        version_id,
        {
            "observed_at": (T0 - timedelta(minutes=2)).isoformat(),
            "source_generation_id": "GEN-S",
            "condition_id": "condition-a",
            "token_id": "token-a",
            "outcome": "Yes",
            "direction": "BUY",
            "classification": "DIAMOND",
            "diamond": True,
            "signal_price": 0.48,
            "signal_quality": 90.0,
            "verification": 90.0,
            "entry_quality": 80.0,
            "resolution_reliability": 90.0,
        },
    )

    lock = {
        "event_type": "LOCKED",
        "event_at": T0.isoformat(),
        "source_generation_id": "GEN-L",
        "source_evidence_id": "0x" + "aa" * 32 + ":1",
        "source_evidence_cursor": [99, 1],
        "source_evidence_at": T0.isoformat(),
        "condition_id": "condition-a",
        "token_id": "token-a",
        "outcome": "Yes",
        "question": "Market A",
        "direction": "BUY",
        "price": 0.49,
        "progress": 0,
        "fail_count": 0,
        "locked_at": T0.isoformat(),
        "price_at_lock": 0.49,
        "challenger_present": False,
        "challenger": None,
    }
    q = enqueue_focus_event(lock, data_dir=data, strategy_versions=versions())
    ingest_focus_queue_file(q["path"], store=store)

    ready = dict(lock)
    ready.update(
        {
            "event_type": "READY",
            "event_at": READY_AT.isoformat(),
            "source_generation_id": "GEN-R",
            "source_evidence_id": EVIDENCE,
            "source_evidence_cursor": [100, 7],
            "source_evidence_at": READY_AT.isoformat(),
            "price": 0.50,
            "progress": 3,
            "ready_at": READY_AT.isoformat(),
            "price_at_ready": 0.50,
        }
    )
    q = enqueue_focus_event(ready, data_dir=data, strategy_versions=versions())
    result = ingest_focus_queue_file(q["path"], store=store)
    return result["ready_id"]


def paper_open(opened_at=None, paper_id="PAPER-open", request_id="REQ-open"):
    opened_at = opened_at or (READY_AT + timedelta(seconds=5))
    return {
        "paper_id": paper_id,
        "open_request_id": request_id,
        "opened_at": opened_at.isoformat(),
        "status": "OPEN",
        "condition_id": "condition-a",
        "token_id": "token-a",
        "outcome": "Yes",
        "question": "Market A",
        "direction": "BUY",
        "source_generation_id": "GEN-R",
        "source_evidence_id": EVIDENCE,
        "focus_locked_at": T0.isoformat(),
        "investment_usd": 25.0,
        "tokens": 49.5,
        "fee_rate": 0.05,
        "fee_exponent": 1.0,
        "settlement_protocol": "CTF",
        "settlement_family": "STANDARD",
        "ctf_contract": "0xctf",
        "position_collateral": "0xusdc",
        "outcome_index": 0,
        "entry": {
            "book_hash": "book-x",
            "book_timestamp": "123",
            "best_ask": 0.50,
            "vwap": 0.50,
            "effective_entry_price": 0.505,
            "gross_tokens": 50.0,
            "fee_usd": 0.25,
            "net_tokens": 49.5,
            "worst_price": 0.50,
            "slippage_bps": 0.0,
        },
    }


def queue_open(data, payload):
    return enqueue_paper_open_snapshot(
        payload,
        data_dir=data,
        strategy_versions=versions(),
    )


def test_successful_paper_open_selects_exact_ready_and_creates_trade(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        ready_id = seed_ready(store, data)
        queued = queue_open(data, paper_open())
        result = ingest_paper_open_queue_file(queued["path"], store=store)

        assert result["status"] == "INGESTED"
        assert result["ready_id"] == ready_id

        ready = store.conn.execute(
            "SELECT selection_status,selected_paper_id FROM ready_opportunities"
        ).fetchone()
        assert ready["selection_status"] == SELECTION_SELECTED
        assert ready["selected_paper_id"] == "PAPER-open"

        trade = store.conn.execute("SELECT * FROM paper_trades").fetchone()
        assert trade["paper_id"] == "PAPER-open"
        assert trade["ready_id"] == ready_id
        assert trade["open_request_id"] == "REQ-open"
        assert trade["status"] == "OPEN"
        assert trade["investment_usd"] == 25.0

        immutable = json.loads(trade["immutable_json"])
        assert immutable["paper_open"]["source_evidence_id"] == EVIDENCE
        assert immutable["ready"]["population"] == "SYSTEM_READY"
        assert "chat_id" not in json.dumps(immutable)


def test_paper_open_replay_is_idempotent(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        seed_ready(store, data)
        queued = queue_open(data, paper_open())
        first = ingest_paper_open_queue_file(queued["path"], store=store)
        second = ingest_paper_open_queue_file(queued["path"], store=store)

        assert first["status"] == "INGESTED"
        assert second["status"] == "ALREADY_INGESTED"
        assert store.conn.execute(
            "SELECT COUNT(*) FROM paper_trades"
        ).fetchone()[0] == 1


def test_multiple_successful_paper_entries_can_link_same_ready(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        ready_id = seed_ready(store, data)
        first = queue_open(data, paper_open())
        ingest_paper_open_queue_file(first["path"], store=store)

        second = queue_open(
            data,
            paper_open(
                opened_at=READY_AT + timedelta(seconds=8),
                paper_id="PAPER-other",
                request_id="REQ-other",
            ),
        )
        result = ingest_paper_open_queue_file(second["path"], store=store)

        assert result["ready_id"] == ready_id
        assert store.conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE ready_id=?",
            (ready_id,),
        ).fetchone()[0] == 2

        ready = store.conn.execute(
            "SELECT selection_status,selected_paper_id "
            "FROM ready_opportunities WHERE ready_id=?",
            (ready_id,),
        ).fetchone()
        assert ready["selection_status"] == SELECTION_SELECTED
        assert ready["selected_paper_id"] == "PAPER-open"


def test_open_after_ready_ended_is_rejected(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        seed_ready(store, data)
        ended_at = READY_AT + timedelta(seconds=10)
        store.end_open_ready_opportunities(
            "condition-a", "token-a", ended_at.isoformat()
        )
        queued = queue_open(
            data,
            paper_open(opened_at=ended_at + timedelta(seconds=1)),
        )
        with pytest.raises(LearningIngestError, match="after READY ended"):
            ingest_paper_open_queue_file(queued["path"], store=store)


def test_ended_ready_finalizes_not_selected_only_after_later_paper_cycle(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        seed_ready(store, data)
        ended_at = READY_AT + timedelta(seconds=10)
        store.end_open_ready_opportunities(
            "condition-a", "token-a", ended_at.isoformat()
        )

        save_json_atomic(
            data / "paper_state.json",
            {
                "schema_version": 1,
                "updated_at": ended_at.isoformat(),
                "positions": [],
                "processed_request_ids": [],
            },
        )
        result = finalize_ready_selection_from_paper_state(
            data_dir=data, store=store
        )
        assert result["finalized"] == 0

        save_json_atomic(
            data / "paper_state.json",
            {
                "schema_version": 1,
                "updated_at": (ended_at + timedelta(seconds=5)).isoformat(),
                "positions": [],
                "processed_request_ids": [],
            },
        )
        result = finalize_ready_selection_from_paper_state(
            data_dir=data, store=store
        )
        assert result["finalized"] == 1

        row = store.conn.execute(
            "SELECT selection_status FROM ready_opportunities"
        ).fetchone()
        assert row["selection_status"] == SELECTION_NOT_SELECTED


def test_existing_authoritative_paper_position_defers_not_selected(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        seed_ready(store, data)
        ended_at = READY_AT + timedelta(seconds=10)
        store.end_open_ready_opportunities(
            "condition-a", "token-a", ended_at.isoformat()
        )
        open_payload = paper_open(opened_at=READY_AT + timedelta(seconds=5))

        save_json_atomic(
            data / "paper_state.json",
            {
                "schema_version": 1,
                "updated_at": (ended_at + timedelta(seconds=5)).isoformat(),
                "positions": [
                    {
                        "paper_id": open_payload["paper_id"],
                        "opened_at": open_payload["opened_at"],
                        "source_generation_id": open_payload["source_generation_id"],
                        "source_evidence_id": open_payload["source_evidence_id"],
                        "condition_id": open_payload["condition_id"],
                        "token_id": open_payload["token_id"],
                    }
                ],
                "processed_request_ids": [],
            },
        )

        result = finalize_ready_selection_from_paper_state(
            data_dir=data, store=store
        )
        assert result["finalized"] == 0
        assert result["deferred"] == 1
        row = store.conn.execute(
            "SELECT selection_status FROM ready_opportunities"
        ).fetchone()
        assert row["selection_status"] == SELECTION_PENDING

        queued = queue_open(data, open_payload)
        ingest_paper_open_queue_file(queued["path"], store=store)
        row = store.conn.execute(
            "SELECT selection_status,selected_paper_id FROM ready_opportunities"
        ).fetchone()
        assert row["selection_status"] == SELECTION_SELECTED
        assert row["selected_paper_id"] == "PAPER-open"



def test_explicit_open_request_marks_user_selected_before_execution(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    request_dir = data / "paper_requests"
    request_dir.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        seed_ready(store, data)
        requested_at = READY_AT + timedelta(seconds=5)
        save_json_atomic(
            request_dir / "REQ-choice.json",
            {
                "schema_version": 1,
                "request_id": "REQ-choice",
                "requested_at": requested_at.isoformat(),
                "action": "OPEN",
                "amount_usd": 25,
                "token_id": "token-a",
                "condition_id": "condition-a",
                "source_generation_id": "GEN-R",
                "source_evidence_id": EVIDENCE,
                "focus_locked_at": T0.isoformat(),
                "chat_id": "ignored-by-learning",
            },
        )

        result = finalize_ready_selection_from_paper_state(
            data_dir=data,
            store=store,
        )
        assert result["selected"] == 1

        ready = store.conn.execute(
            "SELECT selection_status,selected_paper_id FROM ready_opportunities"
        ).fetchone()
        assert ready["selection_status"] == SELECTION_SELECTED
        assert ready["selected_paper_id"] is None


def test_selected_request_is_not_relabelled_not_selected_when_execution_rejects(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    request_dir = data / "paper_requests"
    request_dir.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        seed_ready(store, data)
        ended_at = READY_AT + timedelta(seconds=10)
        store.end_open_ready_opportunities(
            "condition-a",
            "token-a",
            ended_at.isoformat(),
        )

        save_json_atomic(
            request_dir / "REQ-choice.json",
            {
                "schema_version": 1,
                "request_id": "REQ-choice",
                "requested_at": (READY_AT + timedelta(seconds=5)).isoformat(),
                "action": "OPEN",
                "amount_usd": 25,
                "token_id": "token-a",
                "condition_id": "condition-a",
                "source_generation_id": "GEN-R",
                "source_evidence_id": EVIDENCE,
                "focus_locked_at": T0.isoformat(),
            },
        )
        save_json_atomic(
            data / "paper_state.json",
            {
                "schema_version": 1,
                "updated_at": (ended_at + timedelta(seconds=5)).isoformat(),
                "positions": [],
                "processed_request_ids": ["REQ-choice"],
            },
        )

        result = finalize_ready_selection_from_paper_state(
            data_dir=data,
            store=store,
        )

        assert result["selected"] == 1
        assert result["finalized"] == 0
        ready = store.conn.execute(
            "SELECT selection_status,selected_paper_id FROM ready_opportunities"
        ).fetchone()
        assert ready["selection_status"] == SELECTION_SELECTED
        assert ready["selected_paper_id"] is None


def test_successful_entry_can_attach_paper_id_after_selection_intent(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    request_dir = data / "paper_requests"
    request_dir.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        ready_id = seed_ready(store, data)
        save_json_atomic(
            request_dir / "REQ-open.json",
            {
                "schema_version": 1,
                "request_id": "REQ-open",
                "requested_at": (READY_AT + timedelta(seconds=3)).isoformat(),
                "action": "OPEN",
                "amount_usd": 25,
                "token_id": "token-a",
                "condition_id": "condition-a",
                "source_generation_id": "GEN-R",
                "source_evidence_id": EVIDENCE,
                "focus_locked_at": T0.isoformat(),
            },
        )
        finalize_ready_selection_from_paper_state(data_dir=data, store=store)

        queued = queue_open(data, paper_open())
        result = ingest_paper_open_queue_file(queued["path"], store=store)

        assert result["ready_id"] == ready_id
        ready = store.conn.execute(
            "SELECT selection_status,selected_paper_id FROM ready_opportunities"
        ).fetchone()
        assert ready["selection_status"] == SELECTION_SELECTED
        assert ready["selected_paper_id"] == "PAPER-open"
