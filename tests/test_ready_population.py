import json
from datetime import datetime, timedelta, timezone

from scripts.learning_contract import (
    LEARNING_SCHEMA_VERSION,
    SELECTION_PENDING,
)
from scripts.learning_ingest import ingest_focus_queue_file
from scripts.learning_queue import enqueue_focus_event
from scripts.learning_store import LearningStore


T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
E0 = "0x" + "aa" * 32 + ":1"
E1 = "0x" + "bb" * 32 + ":2"
E2 = "0x" + "cc" * 32 + ":3"


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
        "config_fingerprint": "SRC-ready-test",
    }


def signal_row(at=T0 - timedelta(minutes=2), price=0.40):
    return {
        "observed_at": at.isoformat(),
        "source_generation_id": "GEN-SIGNAL",
        "condition_id": "condition-A",
        "token_id": "A",
        "outcome": "Yes",
        "direction": "BUY",
        "classification": "DIAMOND",
        "diamond": True,
        "signal_price": price,
        "signal_quality": 90.0,
        "verification": 90.0,
        "entry_quality": 80.0,
        "resolution_reliability": 90.0,
    }


def focus_event(
    event_type,
    at,
    evidence_id,
    cursor,
    *,
    generation,
    price,
    progress,
    reasons=None,
):
    return {
        "event_type": event_type,
        "event_at": at.isoformat(),
        "source_generation_id": generation,
        "source_generated_at": at.isoformat(),
        "source_evidence_id": evidence_id,
        "source_evidence_cursor": list(cursor),
        "source_evidence_at": at.isoformat(),
        "condition_id": "condition-A",
        "token_id": "A",
        "outcome": "Yes",
        "question": "Market A",
        "direction": "BUY",
        "price": price,
        "state": event_type,
        "progress": progress,
        "fail_count": 0,
        "locked_at": T0.isoformat(),
        "price_at_lock": 0.42,
        "ready_at": at.isoformat() if event_type == "READY" else None,
        "price_at_ready": price if event_type == "READY" else None,
        "invalidated_at": at.isoformat() if event_type == "INVALIDATED" else None,
        "invalidation_reason_codes": list(reasons or []),
        "challenger_present": False,
        "challenger": None,
    }


def queued_focus_path(data_dir, event):
    result = enqueue_focus_event(
        event,
        data_dir=data_dir,
        strategy_versions=versions(),
    )
    return data_dir / "learning_queue" / f"focus_{result['focus_event_id']}.json"


def seed_lock(store, data_dir):
    version_id = store.register_strategy_version(versions())
    store.insert_signal_observation(
        "SIG-A",
        version_id,
        signal_row(),
    )
    lock = focus_event(
        "LOCKED",
        T0,
        E0,
        [100, 1],
        generation="GEN-LOCK",
        price=0.42,
        progress=0,
    )
    ingest_focus_queue_file(
        queued_focus_path(data_dir, lock),
        store=store,
    )


def ingest_ready(store, data_dir, at=None, evidence_id=E1, cursor=(103, 2), generation="GEN-R1"):
    at = at or (T0 + timedelta(minutes=3))
    event = focus_event(
        "READY",
        at,
        evidence_id,
        cursor,
        generation=generation,
        price=0.50,
        progress=3,
    )
    return ingest_focus_queue_file(
        queued_focus_path(data_dir, event),
        store=store,
    )


def test_every_system_ready_creates_population_row_without_paper_selection(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        seed_lock(store, data)
        result = ingest_ready(store, data)

        assert result["status"] == "INGESTED"
        assert result["ready_id"]

        row = store.conn.execute(
            "SELECT * FROM ready_opportunities"
        ).fetchone()
        payload = json.loads(row["payload_json"])

        assert row["ready_id"] == result["ready_id"]
        assert row["focus_event_id"] == result["focus_event_id"]
        assert row["source_generation_id"] == "GEN-R1"
        assert row["source_evidence_id"] == E1
        assert row["condition_id"] == "condition-A"
        assert row["token_id"] == "A"
        assert row["ready_at"] == (T0 + timedelta(minutes=3)).isoformat()
        assert row["selection_status"] == SELECTION_PENDING
        assert row["selected_paper_id"] is None
        assert row["ended_at"] is None
        assert payload["population"] == "SYSTEM_READY"
        assert payload["candidate_first_seen_at"] == (
            T0 - timedelta(minutes=2)
        ).isoformat()
        assert payload["locked_at"] == T0.isoformat()
        assert payload["price_at_lock"] == 0.42
        assert payload["price_at_ready"] == 0.50
        assert payload["wait_confirmations"] == 3


def test_ready_replay_is_idempotent_even_after_mutable_end_time_changes(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"
    ready_at = T0 + timedelta(minutes=3)

    with LearningStore(db) as store:
        seed_lock(store, data)
        first = ingest_ready(store, data, at=ready_at)

        wait_at = ready_at + timedelta(minutes=1)
        wait = focus_event(
            "WAIT",
            wait_at,
            E2,
            [104, 3],
            generation="GEN-WAIT",
            price=0.48,
            progress=2,
        )
        ingest_focus_queue_file(
            queued_focus_path(data, wait),
            store=store,
        )

        # Recreate the identical READY queue message as if cleanup/retry happened.
        replay = ingest_ready(store, data, at=ready_at)

        assert replay["ready_id"] == first["ready_id"]
        assert store.conn.execute(
            "SELECT COUNT(*) FROM ready_opportunities"
        ).fetchone()[0] == 1

        row = store.conn.execute(
            "SELECT selection_status,ended_at FROM ready_opportunities"
        ).fetchone()
        assert row["selection_status"] == SELECTION_PENDING
        assert row["ended_at"] == wait_at.isoformat()


def test_wait_ends_ready_window_but_does_not_claim_not_selected(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"
    ready_at = T0 + timedelta(minutes=3)
    wait_at = ready_at + timedelta(minutes=1)

    with LearningStore(db) as store:
        seed_lock(store, data)
        ingest_ready(store, data, at=ready_at)

        wait = focus_event(
            "WAIT",
            wait_at,
            E2,
            [104, 3],
            generation="GEN-WAIT",
            price=0.48,
            progress=2,
        )
        ingest_focus_queue_file(
            queued_focus_path(data, wait),
            store=store,
        )

        row = store.conn.execute(
            "SELECT selection_status,selected_paper_id,ended_at "
            "FROM ready_opportunities"
        ).fetchone()
        assert row["ended_at"] == wait_at.isoformat()
        assert row["selection_status"] == SELECTION_PENDING
        assert row["selected_paper_id"] is None


def test_invalidation_ends_open_ready_window_without_fabricating_selection(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"
    ready_at = T0 + timedelta(minutes=3)
    invalidated_at = ready_at + timedelta(minutes=1)

    with LearningStore(db) as store:
        seed_lock(store, data)
        ingest_ready(store, data, at=ready_at)

        invalidated = focus_event(
            "INVALIDATED",
            invalidated_at,
            E2,
            [104, 3],
            generation="GEN-INVALID",
            price=0.47,
            progress=3,
            reasons=["MARKET_EXPIRED"],
        )
        ingest_focus_queue_file(
            queued_focus_path(data, invalidated),
            store=store,
        )

        row = store.conn.execute(
            "SELECT selection_status,ended_at FROM ready_opportunities"
        ).fetchone()
        assert row["selection_status"] == SELECTION_PENDING
        assert row["ended_at"] == invalidated_at.isoformat()


def test_ready_wait_ready_creates_two_distinct_system_opportunities(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"
    first_ready_at = T0 + timedelta(minutes=3)
    wait_at = first_ready_at + timedelta(minutes=1)
    second_ready_at = wait_at + timedelta(minutes=1)

    with LearningStore(db) as store:
        seed_lock(store, data)
        first = ingest_ready(store, data, at=first_ready_at)

        wait = focus_event(
            "WAIT",
            wait_at,
            E2,
            [104, 3],
            generation="GEN-WAIT",
            price=0.48,
            progress=2,
        )
        ingest_focus_queue_file(
            queued_focus_path(data, wait),
            store=store,
        )

        second = ingest_ready(
            store,
            data,
            at=second_ready_at,
            evidence_id="0x" + "dd" * 32 + ":4",
            cursor=(105, 4),
            generation="GEN-R2",
        )

        assert first["ready_id"] != second["ready_id"]
        rows = store.conn.execute(
            "SELECT ready_id,ready_at,ended_at,selection_status "
            "FROM ready_opportunities ORDER BY ready_at"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["ready_id"] == first["ready_id"]
        assert rows[0]["ended_at"] == wait_at.isoformat()
        assert rows[1]["ready_id"] == second["ready_id"]
        assert rows[1]["ended_at"] is None
        assert all(row["selection_status"] == SELECTION_PENDING for row in rows)


def test_non_ready_focus_events_do_not_create_ready_population(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        seed_lock(store, data)

        wait = focus_event(
            "WAIT",
            T0 + timedelta(minutes=1),
            E1,
            [101, 2],
            generation="GEN-WAIT",
            price=0.44,
            progress=1,
        )
        ingest_focus_queue_file(
            queued_focus_path(data, wait),
            store=store,
        )

        assert store.conn.execute(
            "SELECT COUNT(*) FROM ready_opportunities"
        ).fetchone()[0] == 0
