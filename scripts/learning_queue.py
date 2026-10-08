"""Durable LD-2 queue between authoritative Risk output and Learning SQLite.

Queue writes are best-effort and never change Risk decisions. Files are removed
only after the learning worker has committed the matching generation.
"""

from __future__ import annotations

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
