"""Durable LD-2 queue between authoritative Risk output and Learning SQLite.

Queue writes are best-effort and never change Risk decisions. Files are removed
only after the learning worker has committed the matching generation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from machine_common import save_json_atomic

LEARNING_CLASSIFICATIONS = frozenset(
    {"DIAMOND", "VERIFYING", "ENTRY_BLOCKED", "DATA_RISK", "CANDIDATE"}
)


class LearningQueueError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _read_json(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _relevant_result(row):
    return (
        isinstance(row, dict)
        and (
            _text(row.get("classification")).upper() in LEARNING_CLASSIFICATIONS
            or row.get("cashflow_active") is True
        )
    )


def enqueue_risk_snapshot(payload, *, data_dir, strategy_versions):
    if not isinstance(payload, Mapping):
        raise LearningQueueError("Risk snapshot must be an object")

    generation_id = _text(payload.get("source_generation_id"))
    results = payload.get("results")
    if not generation_id:
        raise LearningQueueError("Risk source_generation_id missing")
    if Path(generation_id).name != generation_id:
        raise LearningQueueError("Risk source_generation_id is not a safe filename")
    if not isinstance(results, list):
        raise LearningQueueError("Risk results must be a list")
    if not isinstance(strategy_versions, Mapping):
        raise LearningQueueError("strategy_versions must be an object")

    # Empty/LOW-only generations are scheduler cycles, not independent signal
    # observations. They do not belong in the learning population.
    if not any(_relevant_result(row) for row in results):
        return {
            "status": "NO_RELEVANT_SIGNALS",
            "generation_id": generation_id,
        }

    snapshot = dict(payload)
    snapshot["strategy_versions"] = dict(strategy_versions)

    queue_dir = Path(data_dir) / "learning_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    target = queue_dir / f"risk_{generation_id}.json"

    if target.exists():
        existing = _read_json(target)
        if existing != snapshot:
            raise LearningQueueError(
                f"Risk learning queue conflict for generation {generation_id}"
            )
        return {
            "status": "ALREADY_QUEUED",
            "generation_id": generation_id,
            "path": str(target),
        }

    save_json_atomic(target, snapshot)
    return {
        "status": "QUEUED",
        "generation_id": generation_id,
        "path": str(target),
    }


def focus_event_id(event):
    if not isinstance(event, Mapping):
        raise LearningQueueError("Focus learning event must be an object")
    required = (
        "event_type",
        "event_at",
        "source_generation_id",
        "condition_id",
        "token_id",
    )
    for field in required:
        if not _text(event.get(field)):
            raise LearningQueueError(f"Focus learning event {field} missing")
    raw = "\x00".join(
        [
            _text(event.get("source_generation_id")),
            _text(event.get("event_type")).upper(),
            _text(event.get("event_at")),
            _text(event.get("condition_id")),
            _text(event.get("token_id")),
            _text(event.get("source_evidence_id")),
        ]
    ).encode("utf-8")
    return "FOCUS-" + hashlib.sha256(raw).hexdigest()[:24]


def enqueue_focus_event(event, *, data_dir, strategy_versions):
    """Persist one already-authoritative Focus transition for Learning.

    This queue is observational only. Failure must never alter Focus state.
    """
    if not isinstance(event, Mapping):
        raise LearningQueueError("Focus learning event must be an object")
    if not isinstance(strategy_versions, Mapping):
        raise LearningQueueError("strategy_versions must be an object")

    event_id = focus_event_id(event)
    snapshot = {
        "schema_version": 1,
        "focus_event_id": event_id,
        "strategy_versions": dict(strategy_versions),
        "event": dict(event),
    }

    queue_dir = Path(data_dir) / "learning_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    target = queue_dir / f"focus_{event_id}.json"

    if target.exists():
        existing = _read_json(target)
        if existing != snapshot:
            raise LearningQueueError(
                f"Focus learning queue conflict for event {event_id}"
            )
        return {
            "status": "ALREADY_QUEUED",
            "focus_event_id": event_id,
            "path": str(target),
        }

    save_json_atomic(target, snapshot)
    return {
        "status": "QUEUED",
        "focus_event_id": event_id,
        "path": str(target),
    }


def book_observation_id(observation):
    if not isinstance(observation, Mapping):
        raise LearningQueueError("Book observation must be an object")
    required = (
        "generated_at",
        "source_generation_id",
        "source_evidence_id",
        "condition_id",
        "token_id",
        "status",
    )
    for field in required:
        if not _text(observation.get(field)):
            raise LearningQueueError(f"Book observation {field} missing")

    raw = "\x00".join(
        [
            _text(observation.get("source_generation_id")),
            _text(observation.get("source_evidence_id")),
            _text(observation.get("condition_id")),
            _text(observation.get("token_id")),
            _text(observation.get("generated_at")),
            _text(observation.get("status")),
            _text(observation.get("book_hash")),
        ]
    ).encode("utf-8")
    return "BOOKOBS-" + hashlib.sha256(raw).hexdigest()[:24]


def enqueue_book_observation(observation, *, data_dir, strategy_versions):
    """Queue one already-produced Book assessment for Learning only."""
    if not isinstance(observation, Mapping):
        raise LearningQueueError("Book observation must be an object")
    if not isinstance(strategy_versions, Mapping):
        raise LearningQueueError("strategy_versions must be an object")

    observation_id = book_observation_id(observation)
    snapshot = {
        "schema_version": 1,
        "book_observation_id": observation_id,
        "strategy_versions": dict(strategy_versions),
        "observation": dict(observation),
    }

    queue_dir = Path(data_dir) / "learning_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    target = queue_dir / f"book_{observation_id}.json"

    if target.exists():
        existing = _read_json(target)
        if existing != snapshot:
            raise LearningQueueError(
                f"Book learning queue conflict for observation {observation_id}"
            )
        return {
            "status": "ALREADY_QUEUED",
            "book_observation_id": observation_id,
            "path": str(target),
        }

    save_json_atomic(target, snapshot)
    return {
        "status": "QUEUED",
        "book_observation_id": observation_id,
        "path": str(target),
    }
