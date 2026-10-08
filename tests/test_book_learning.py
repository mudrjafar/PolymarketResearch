import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.learning_contract import LEARNING_SCHEMA_VERSION
from scripts.learning_ingest import (
    LearningIngestError,
    ingest_book_queue_file,
    ingest_focus_queue_file,
)
from scripts.learning_queue import enqueue_book_observation, enqueue_focus_event
from scripts.learning_store import LearningStore


T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
EVIDENCE = "0x" + "ab" * 32 + ":1"


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
        "config_fingerprint": "SRC-book-test",
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
        "source_evidence_id": "0x" + "aa" * 32 + ":0",
        "source_evidence_cursor": [99, 0],
        "source_evidence_at": T0.isoformat(),
        "condition_id": "condition-a",
        "token_id": "token-a",
        "outcome": "Yes",
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

    ready_at = T0 + timedelta(minutes=1)
    ready = dict(lock)
    ready.update(
        {
            "event_type": "READY",
            "event_at": ready_at.isoformat(),
            "source_generation_id": "GEN-R",
            "source_evidence_id": EVIDENCE,
            "source_evidence_cursor": [100, 1],
            "source_evidence_at": ready_at.isoformat(),
            "price": 0.50,
            "progress": 3,
            "ready_at": ready_at.isoformat(),
            "price_at_ready": 0.50,
        }
    )
    q = enqueue_focus_event(ready, data_dir=data, strategy_versions=versions())
    result = ingest_focus_queue_file(q["path"], store=store)
    return result["ready_id"], ready_at


def observation(at, *, status="OK", book_ok=True, reasons=None):
    return {
        "schema_version": 1,
        "generated_at": at.isoformat(),
        "status": status,
        "book_ok": book_ok,
        "reason_codes": list(reasons or []),
        "source_focus_generated_at": at.isoformat(),
        "source_generation_id": "GEN-R",
        "source_evidence_id": EVIDENCE,
        "token_id": "token-a",
        "condition_id": "condition-a",
        "outcome": "Yes",
        "question": "Market A",
        "book_hash": "hash-a" if status == "OK" else None,
        "book_timestamp": "123" if status == "OK" else None,
        "best_bid": 0.49 if status == "OK" else None,
        "best_ask": 0.50 if status == "OK" else None,
        "midpoint": 0.495 if status == "OK" else None,
        "spread": 0.01 if status == "OK" else None,
        "spread_bps_mid": 202.0202 if status == "OK" else None,
        "ask_depth_tokens": 300.0 if status == "OK" else None,
        "ask_depth_usd": 152.0 if status == "OK" else None,
        "bid_depth_tokens": 1000.0 if status == "OK" else None,
        "bid_depth_usd": 490.0 if status == "OK" else None,
        "focus_price": 0.50,
        "best_ask_move_from_focus": 0.0 if status == "OK" else None,
        "best_ask_move_from_focus_bps": 0.0 if status == "OK" else None,
        "quotes": [
            {
                "notional_usd": 25.0,
                "complete": True,
                "filled_usd": 25.0,
                "fill_fraction": 1.0,
                "outcome_tokens": 50.0,
                "vwap": 0.50,
                "worst_price": 0.50,
                "slippage": 0.0,
                "slippage_bps": 0.0,
            }
        ] if status == "OK" else [],
    }


def queue_book(data, payload):
    return enqueue_book_observation(
        payload,
        data_dir=data,
        strategy_versions=versions(),
    )


def test_book_pass_is_bound_to_exact_ready_and_keeps_execution_metrics(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        ready_id, ready_at = seed_ready(store, data)
        queued = queue_book(data, observation(ready_at + timedelta(seconds=5)))
        result = ingest_book_queue_file(queued["path"], store=store)

        assert result["status"] == "INGESTED"
        assert result["ready_id"] == ready_id
        assert result["book_ok"] is True

        row = store.conn.execute("SELECT * FROM book_decisions").fetchone()
        payload = json.loads(row["payload_json"])
        assert row["ready_id"] == ready_id
        assert row["status"] == "OK"
        assert row["book_ok"] == 1
        assert payload["measurement_status"] == "MEASURED"
        assert payload["best_bid"] == 0.49
        assert payload["best_ask"] == 0.50
        assert payload["spread"] == 0.01
        assert payload["ask_depth_usd"] == 152.0
        assert payload["quotes"][0]["vwap"] == 0.50


def test_book_block_is_observed_not_reinterpreted(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        ready_id, ready_at = seed_ready(store, data)
        payload = observation(
            ready_at + timedelta(seconds=5),
            book_ok=False,
            reasons=["INSUFFICIENT_DEPTH_100"],
        )
        queued = queue_book(data, payload)
        ingest_book_queue_file(queued["path"], store=store)

        row = store.conn.execute(
            "SELECT status,book_ok,reason_codes_json,payload_json "
            "FROM book_decisions"
        ).fetchone()
        assert row["status"] == "OK"
        assert row["book_ok"] == 0
        assert json.loads(row["reason_codes_json"]) == [
            "INSUFFICIENT_DEPTH_100"
        ]
        assert json.loads(row["payload_json"])["ready_id"] == ready_id


def test_api_error_is_unavailable_measurement_not_strategy_block(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        _, ready_at = seed_ready(store, data)
        payload = observation(
            ready_at + timedelta(seconds=5),
            status="API_ERROR",
            book_ok=False,
            reasons=["BOOK_API_ERROR", "HTTP_503"],
        )
        queued = queue_book(data, payload)
        ingest_book_queue_file(queued["path"], store=store)

        row = store.conn.execute(
            "SELECT status,book_ok,payload_json FROM book_decisions"
        ).fetchone()
        stored = json.loads(row["payload_json"])
        assert row["status"] == "API_ERROR"
        assert row["book_ok"] == 0
        assert stored["measurement_status"] == "UNAVAILABLE"
        assert stored["best_bid"] is None
        assert stored["quotes"] == []


def test_book_without_ready_fails_closed_and_can_be_retried_later(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"
    payload = observation(T0 + timedelta(minutes=1, seconds=5))
    queued = queue_book(data, payload)

    with LearningStore(db) as store:
        with pytest.raises(
            LearningIngestError,
            match="exactly one SYSTEM_READY",
        ):
            ingest_book_queue_file(queued["path"], store=store)
        assert store.conn.execute(
            "SELECT COUNT(*) FROM book_decisions"
        ).fetchone()[0] == 0


def test_book_replay_is_idempotent(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        _, ready_at = seed_ready(store, data)
        queued = queue_book(data, observation(ready_at + timedelta(seconds=5)))
        first = ingest_book_queue_file(queued["path"], store=store)
        second = ingest_book_queue_file(queued["path"], store=store)

        assert first["status"] == "INGESTED"
        assert second["status"] == "ALREADY_INGESTED"
        assert store.conn.execute(
            "SELECT COUNT(*) FROM book_decisions"
        ).fetchone()[0] == 1


def test_book_after_ready_window_end_is_rejected(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "learning.sqlite3"

    with LearningStore(db) as store:
        _, ready_at = seed_ready(store, data)
        ended_at = ready_at + timedelta(seconds=10)
        store.end_open_ready_opportunities(
            "condition-a",
            "token-a",
            ended_at.isoformat(),
        )
        queued = queue_book(
            data,
            observation(ended_at + timedelta(seconds=1)),
        )
        with pytest.raises(
            LearningIngestError,
            match="after READY ended",
        ):
            ingest_book_queue_file(queued["path"], store=store)
