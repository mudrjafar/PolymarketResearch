"""Read-only Diamond/Risk learning ingestion.

Consumes already-published immutable Diamond generation artifacts plus the
authoritative Risk output for the same generation. It records research data in
learning.sqlite3 and has no authority over Diamond, Risk, Focus, Book, Paper,
Telegram, or execution decisions.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from scripts.learning_contract import LEARNING_SCHEMA_VERSION
from scripts.learning_store import DEFAULT_DB_FILE, LearningStore

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
GENERATION_MANIFEST_FILE = DATA_DIR / "diamond_generation.json"
GENERATIONS_DIR = DATA_DIR / "diamond_generations"
RISK_FILE = DATA_DIR / "risk_assessment.json"

class LearningIngestError(RuntimeError):
    pass


def _read_json(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _text(value):
    return str(value or "").strip()


def _identity(row):
    return (
        _text(row.get("condition_id")),
        _text(row.get("token_id")),
        _text(row.get("outcome")).lower(),
    )


def _stable_id(prefix, *parts):
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return prefix + "-" + hashlib.sha256(payload).hexdigest()[:24]


def _signal_payload(row, generation_id, published_at, rank):
    scores = row.get("scores") if isinstance(row.get("scores"), dict) else {}
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    cashflow = (
        row.get("cashflow_alert")
        if isinstance(row.get("cashflow_alert"), dict)
        else {}
    )
    resolution = (
        row.get("resolution") if isinstance(row.get("resolution"), dict) else {}
    )

    return {
        "observed_at": published_at,
        "source_generation_id": generation_id,
        "condition_id": row.get("condition_id"),
        "token_id": row.get("token_id"),
        "outcome": row.get("outcome"),
        "direction": row.get("direction"),
        "classification": row.get("classification"),
        "diamond": row.get("diamond") is True,
        "signal_price": row.get("price"),
        "signal_quality": scores.get("signal_quality"),
        "verification": scores.get("verification"),
        "entry_quality": scores.get("entry_quality"),
        "resolution_reliability": scores.get("resolution_reliability"),
        "verified": metrics.get("verified"),
        "confirmations": metrics.get("confirmations"),
        "data_confidence": metrics.get("data_confidence"),
        "trades_1m": metrics.get("trades_1m"),
        "trades_5m": metrics.get("trades_5m"),
        "trades_15m": metrics.get("trades_15m"),
        "volume_1m": metrics.get("volume_1m"),
        "volume_5m": metrics.get("volume_5m"),
        "volume_15m": metrics.get("volume_15m"),
        "net_flow_1m": metrics.get("net_1m"),
        "net_flow_5m": metrics.get("net_5m"),
        "net_flow_15m": metrics.get("net_15m"),
        "strength_1m": metrics.get("strength_1m"),
        "strength_5m": metrics.get("strength_5m"),
        "strength_15m": metrics.get("strength_15m"),
        "largest_trade_5m": metrics.get("largest_trade_5m"),
        "largest_trade_ratio": metrics.get("largest_trade_ratio"),
        "cashflow_active": cashflow.get("active"),
        "cashflow_tier": cashflow.get("tier"),
        "cashflow_quality": cashflow.get("quality"),
        "time_to_resolution_seconds": resolution.get("remaining_seconds"),
        "generation_rank": rank,
        "forecast_probability": None,
        "probability_model_version": None,
        "source_diamond": dict(row),
    }


def _risk_payload(row):
    return {
        "checked_at": row.get("checked_at"),
        "risk_ok": row.get("risk_ok"),
        "decision": row.get("decision"),
        "reason_codes": list(row.get("reason_codes") or []),
        "reasons": list(row.get("reasons") or []),
        "warnings": list(row.get("warnings") or []),
        "evidence_id": row.get("evidence_id"),
        "evidence_cursor": row.get("evidence_cursor"),
        "evidence_at": row.get("evidence_at"),
        "source_risk": dict(row),
    }


def ingest_generation(
    manifest,
    candidates,
    risk_payload,
    *,
    store,
    versions,
):
    if not isinstance(manifest, Mapping):
        raise LearningIngestError("Diamond generation manifest must be an object")
    generation_id = _text(manifest.get("generation_id"))
    published_at = manifest.get("published_at")
    if not generation_id or not published_at:
        raise LearningIngestError("Diamond generation manifest incomplete")
    if not isinstance(candidates, list):
        raise LearningIngestError("Diamond candidates must be a list")
    if not isinstance(risk_payload, Mapping):
        raise LearningIngestError("Risk snapshot must be an object")
    if _text(risk_payload.get("source_generation_id")) != generation_id:
        raise LearningIngestError("Risk generation does not match Diamond generation")
    risk_rows = risk_payload.get("results")
    if not isinstance(risk_rows, list):
        raise LearningIngestError("Risk results must be a list")

    by_identity = {}
    for row in risk_rows:
        if not isinstance(row, Mapping):
            raise LearningIngestError("Risk result must be an object")
        key = _identity(row)
        if not all(key):
            raise LearningIngestError("Risk result identity incomplete")
        if key in by_identity:
            raise LearningIngestError("Duplicate Risk result identity")
        by_identity[key] = row

    candidate_rows = []
    candidate_identities = set()
    for rank, row in enumerate(candidates, start=1):
        if not isinstance(row, Mapping):
            raise LearningIngestError("Diamond candidate row must be an object")
        key = _identity(row)
        if not all(key):
            raise LearningIngestError("Diamond signal identity incomplete")
        if key in candidate_identities:
            raise LearningIngestError("Duplicate Diamond candidate identity")
        candidate_identities.add(key)
        candidate_rows.append((rank, row, key))

    risk_identities = set(by_identity)
    missing = candidate_identities - risk_identities
    if missing:
        raise LearningIngestError("Risk result missing for Diamond candidate")

    # Validate the complete candidate/Risk batch before the first DB write.
    for _rank, _row, key in candidate_rows:
        risk_row = by_identity[key]
        if not _text(risk_row.get("checked_at")):
            raise LearningIngestError("Risk checked_at missing")
        if risk_row.get("risk_ok") not in (True, False):
            raise LearningIngestError("Risk decision boolean missing")
        if _text(risk_row.get("decision")) not in {"PASS", "BLOCK"}:
            raise LearningIngestError("Risk decision invalid")
        if not isinstance(risk_row.get("reason_codes"), list):
            raise LearningIngestError("Risk reason_codes invalid")

    version_id = store.register_strategy_version(versions)
    inserted_signals = 0
    inserted_risks = 0

    for rank, row, key in candidate_rows:
        risk_row = by_identity[key]

        signal_id = _stable_id(
            "SIG", generation_id, key[0], key[1], key[2]
        )
        signal = _signal_payload(row, generation_id, published_at, rank)
        if store.insert_signal_observation(signal_id, version_id, signal):
            inserted_signals += 1

        risk_decision_id = _stable_id("RISK", signal_id)
        risk = _risk_payload(risk_row)
        if store.insert_risk_decision(risk_decision_id, signal_id, risk):
            inserted_risks += 1

    return {
        "generation_id": generation_id,
        "version_id": version_id,
        "signals_seen": len(candidates),
        "signals_inserted": inserted_signals,
        "risk_decisions_inserted": inserted_risks,
    }


def ingest_latest(
    *,
    manifest_file=GENERATION_MANIFEST_FILE,
    generations_dir=GENERATIONS_DIR,
    risk_file=RISK_FILE,
    db_file=DEFAULT_DB_FILE,
    versions=None,
):
    manifest = _read_json(manifest_file)
    generation_id = _text(manifest.get("generation_id"))
    if not generation_id:
        raise LearningIngestError("Diamond generation id missing")

    generation_dir = Path(generations_dir) / generation_id
    candidates = _read_json(generation_dir / "diamond_candidates.json")
    risk_payload = _read_json(risk_file)

    versions = versions or manifest.get("strategy_versions")
    if not isinstance(versions, Mapping):
        raise LearningIngestError(
            "Diamond generation lacks frozen strategy_versions"
        )
    with LearningStore(db_file) as store:
        return ingest_generation(
            manifest,
            candidates,
            risk_payload,
            store=store,
            versions=versions,
        )
