"""LD-2 ingestion from frozen Diamond generations + archived Risk decisions.

This module never calls Diamond or Risk decision functions. It consumes only
already-produced artifacts and writes observational learning rows.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from scripts.learning_store import LearningStore, LearningStoreError

LEARNING_CLASSIFICATIONS = frozenset(
    {"DIAMOND", "VERIFYING", "ENTRY_BLOCKED", "DATA_RISK", "CANDIDATE"}
)


class LearningIngestError(RuntimeError):
    pass


def _read_json(path: Path):
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _identity(row: Mapping[str, Any]):
    return (
        _text(row.get("condition_id")),
        _text(row.get("token_id")),
        _text(row.get("outcome")).casefold(),
    )


def _id(prefix: str, *parts: Any) -> str:
    raw = "\x00".join(str(part) for part in parts).encode("utf-8")
    return prefix + hashlib.sha256(raw).hexdigest()[:24]


def signal_id(generation_id: str, rank: int, row: Mapping[str, Any]) -> str:
    return _id(
        "SIG-",
        generation_id,
        _text(row.get("condition_id")),
        _text(row.get("token_id")),
        _text(row.get("outcome")).casefold(),
        _text(row.get("market_key")),
        rank,
    )


def risk_decision_id(signal: str) -> str:
    return _id("RISK-", signal)


def _flow_for(candidate: Mapping[str, Any], flow_state: Mapping[str, Any]):
    token_id = _text(candidate.get("token_id"))
    value = flow_state.get(token_id) if token_id else None
    return value if isinstance(value, dict) else None


def _scores(candidate):
    value = candidate.get("scores")
    return value if isinstance(value, dict) else {}


def _metrics(candidate):
    value = candidate.get("metrics")
    return value if isinstance(value, dict) else {}


def _cashflow(candidate):
    value = candidate.get("cashflow_alert")
    return value if isinstance(value, dict) else {}


def _resolution(candidate):
    value = candidate.get("resolution")
    return value if isinstance(value, dict) else {}


def _signal_row(
    generation_id: str,
    source_generated_at: str,
    rank: int,
    candidate: Mapping[str, Any],
    flow_state: Mapping[str, Any],
):
    scores = _scores(candidate)
    metrics = _metrics(candidate)
    cashflow = _cashflow(candidate)
    resolution = _resolution(candidate)
    flow = _flow_for(candidate, flow_state)

    evidence_id = flow.get("evidence_id") if flow else None
    evidence_cursor = flow.get("evidence_cursor") if flow else None
    evidence_at = flow.get("evidence_at") if flow else None

    return {
        "observed_at": candidate.get("generated_at") or source_generated_at,
        "source_generation_id": generation_id,
        "generation_rank": rank,
        "market_key": candidate.get("market_key"),
        "question": candidate.get("question"),
        "condition_id": _text(candidate.get("condition_id")),
        "token_id": _text(candidate.get("token_id")),
        "outcome": _text(candidate.get("outcome")),
        "direction": _text(candidate.get("direction")).upper(),
        "classification": _text(candidate.get("classification")).upper(),
        "diamond": candidate.get("diamond") is True,
        "signal_price": candidate.get("price"),
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
        "source_updated_at": candidate.get("source_updated_at"),
        "last_trade_at": candidate.get("last_trade_at"),
        "source_evidence_id": evidence_id,
        "source_evidence_cursor": evidence_cursor,
        "source_evidence_at": evidence_at,
        "why_not_diamond": candidate.get("why_not_diamond"),
        # This is already an aggregate Diamond feature snapshot, not raw trades.
        "diamond_snapshot": dict(candidate),
    }


def _validate_risk_row(candidate, risk):
    if not isinstance(risk, dict):
        raise LearningIngestError("Risk result must be an object")
    if _identity(candidate) != _identity(risk):
        raise LearningIngestError("Diamond/Risk identity mismatch")
    if _text(candidate.get("direction")).upper() != _text(risk.get("direction")).upper():
        raise LearningIngestError("Diamond/Risk direction mismatch")
    if not _text(risk.get("checked_at")):
        raise LearningIngestError("Risk checked_at missing")
    if not isinstance(risk.get("risk_ok"), bool):
        raise LearningIngestError("Risk risk_ok must be boolean")
    expected = "PASS" if risk["risk_ok"] else "BLOCK"
    if _text(risk.get("decision")).upper() != expected:
        raise LearningIngestError("Risk decision contradicts risk_ok")
    if not isinstance(risk.get("reason_codes"), list):
        raise LearningIngestError("Risk reason_codes must be a list")


def _risk_map(rows):
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise LearningIngestError("Risk results must contain only objects")
        key = _identity(row)
        if key in result:
            raise LearningIngestError("Duplicate Risk market identity")
        result[key] = row
    return result


def _candidate_map(rows):
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise LearningIngestError("Diamond candidates must contain only objects")
        key = _identity(row)
        if key in result:
            raise LearningIngestError("Duplicate Diamond candidate identity")
        result[key] = row
    return result


def ingest_queue_file(
    queue_file,
    *,
    data_dir,
    store: LearningStore,
    now=None,
):
    """Ingest one generation atomically-at-the-contract-level.

    Individual immutable inserts may commit before a crash. The generation
    marker is written only after all rows succeed, so retry is safe/idempotent.
    """
    now = now or datetime.now(timezone.utc)
    queue_file = Path(queue_file)
    data_dir = Path(data_dir)

    snapshot = _read_json(queue_file)
    if not isinstance(snapshot, dict):
        raise LearningIngestError("Risk queue snapshot must be an object")

    generation_id = _text(snapshot.get("source_generation_id"))
    source_generated_at = _text(snapshot.get("source_generated_at"))
    risk_generated_at = _text(snapshot.get("generated_at"))
    versions = snapshot.get("strategy_versions")
    results = snapshot.get("results")

    if not generation_id:
        raise LearningIngestError("source_generation_id missing")
    if not source_generated_at or not risk_generated_at:
        raise LearningIngestError("Risk generation timestamps missing")
    if not isinstance(versions, dict):
        raise LearningIngestError("strategy_versions missing from Risk queue snapshot")
    if not isinstance(results, list):
        raise LearningIngestError("Risk results must be a list")

    if store.generation_ingested(generation_id):
        return {
            "status": "ALREADY_INGESTED",
            "generation_id": generation_id,
            "signals_ingested": 0,
        }

    generation_dir = data_dir / "diamond_generations" / generation_id
    candidates_path = generation_dir / "diamond_candidates.json"
    flow_path = generation_dir / "flow_state.json"

    try:
        candidates = _read_json(candidates_path)
        flow_state = _read_json(flow_path)
    except Exception as exc:
        raise LearningIngestError(
            f"Diamond generation {generation_id} incomplete: {type(exc).__name__}"
        ) from exc

    if not isinstance(candidates, list):
        raise LearningIngestError("Diamond candidate snapshot must be a list")
    if not isinstance(flow_state, dict):
        raise LearningIngestError("Flow snapshot must be an object")

    markets_checked = snapshot.get("markets_checked")
    passed = snapshot.get("passed")
    if not isinstance(markets_checked, int) or isinstance(markets_checked, bool):
        raise LearningIngestError("Risk markets_checked invalid")
    if not isinstance(passed, int) or isinstance(passed, bool):
        raise LearningIngestError("Risk passed invalid")
    if markets_checked != len(results):
        raise LearningIngestError("Risk markets_checked does not match result count")

    risk_by_key = _risk_map(results)
    candidate_by_key = _candidate_map(candidates)

    relevant_risk_keys = {
        key
        for key, row in risk_by_key.items()
        if _text(row.get("classification")).upper() in LEARNING_CLASSIFICATIONS
        or row.get("cashflow_active") is True
    }
    if relevant_risk_keys != set(candidate_by_key):
        raise LearningIngestError(
            "Risk relevant population does not match frozen Diamond candidates"
        )

    version_id = store.register_strategy_version(versions)

    inserted = 0
    for rank, candidate in enumerate(candidates, start=1):
        risk = risk_by_key[_identity(candidate)]
        _validate_risk_row(candidate, risk)

        sig_id = signal_id(generation_id, rank, candidate)
        signal = _signal_row(
            generation_id,
            source_generated_at,
            rank,
            candidate,
            flow_state,
        )
        store.insert_signal_observation(sig_id, version_id, signal)

        risk_payload = dict(risk)
        risk_payload["source_generation_id"] = generation_id
        risk_payload["signal_id"] = sig_id
        store.insert_risk_decision(
            risk_decision_id(sig_id),
            sig_id,
            risk_payload,
        )
        inserted += 1

    marker = {
        "source_generated_at": source_generated_at,
        "risk_generated_at": risk_generated_at,
        "risk_markets_checked": markets_checked,
        "risk_passed": passed,
        "signals_ingested": inserted,
        "ingested_at": now.isoformat(),
        "queue_snapshot_file": queue_file.name,
    }
    store.insert_generation_ingestion(
        generation_id,
        version_id,
        marker,
    )

    return {
        "status": "INGESTED",
        "generation_id": generation_id,
        "signals_ingested": inserted,
    }


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _episode_bounds(store, version_id, condition_id, token_id, event_at):
    event_dt = _parse_time(event_at)
    if event_dt is None:
        raise LearningIngestError("Focus event_at invalid")

    last_invalidated = None
    rows = store.conn.execute(
        "SELECT event_at FROM focus_events "
        "WHERE version_id=? AND condition_id=? AND token_id=? "
        "AND event_type='INVALIDATED' ORDER BY event_at DESC",
        (str(version_id), str(condition_id), str(token_id)),
    ).fetchall()
    for row in rows:
        value = _parse_time(row["event_at"])
        if value is not None and value < event_dt:
            last_invalidated = value
            break

    return last_invalidated, event_dt


def _first_signal_for_episode(
    store,
    version_id,
    condition_id,
    token_id,
    event_at,
):
    lower, upper = _episode_bounds(
        store, version_id, condition_id, token_id, event_at
    )
    rows = store.conn.execute(
        "SELECT observed_at,signal_price,payload_json "
        "FROM signal_observations "
        "WHERE version_id=? AND condition_id=? AND token_id=? "
        "ORDER BY observed_at ASC",
        (str(version_id), str(condition_id), str(token_id)),
    ).fetchall()

    for row in rows:
        observed = _parse_time(row["observed_at"])
        if observed is None or observed > upper:
            continue
        if lower is not None and observed <= lower:
            continue
        return row
    return None


def _latest_focus_event_payload(
    store,
    version_id,
    condition_id,
    token_id,
    event_type,
    before_at,
    *,
    after_at=None,
):
    upper = _parse_time(before_at)
    lower = _parse_time(after_at) if after_at else None
    if upper is None:
        return None

    rows = store.conn.execute(
        "SELECT event_at,payload_json FROM focus_events "
        "WHERE version_id=? AND condition_id=? AND token_id=? "
        "AND event_type=? ORDER BY event_at DESC",
        (
            str(version_id),
            str(condition_id),
            str(token_id),
            str(event_type),
        ),
    ).fetchall()
    for row in rows:
        event_dt = _parse_time(row["event_at"])
        if event_dt is None or event_dt > upper:
            continue
        if lower is not None and event_dt < lower:
            continue
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def ingest_focus_queue_file(queue_file, *, store: LearningStore):
    """Ingest one durable authoritative Focus transition."""
    snapshot = _read_json(Path(queue_file))
    if not isinstance(snapshot, dict):
        raise LearningIngestError("Focus queue snapshot must be an object")

    focus_event_id = _text(snapshot.get("focus_event_id"))
    versions = snapshot.get("strategy_versions")
    event = snapshot.get("event")
    if not focus_event_id:
        raise LearningIngestError("focus_event_id missing")
    if not isinstance(versions, dict):
        raise LearningIngestError("Focus strategy_versions missing")
    if not isinstance(event, dict):
        raise LearningIngestError("Focus event missing")

    event_type = _text(event.get("event_type")).upper()
    event_at = _text(event.get("event_at"))
    generation_id = _text(event.get("source_generation_id"))
    condition_id = _text(event.get("condition_id"))
    token_id = _text(event.get("token_id"))
    if event_type not in {"LOCKED", "WAIT", "READY", "INVALIDATED"}:
        raise LearningIngestError("Focus event_type unsupported")
    if not all((event_at, generation_id, condition_id, token_id)):
        raise LearningIngestError("Focus event identity incomplete")

    version_id = store.register_strategy_version(versions)

    # If LD-2 already ingested this source generation, provenance must match.
    marker = store.fetch_one(
        "SELECT version_id FROM generation_ingestions WHERE generation_id=?",
        (generation_id,),
    )
    if marker is not None and str(marker["version_id"]) != str(version_id):
        raise LearningIngestError("Focus/Risk strategy provenance mismatch")

    lineage = dict(event)

    if event_type == "LOCKED":
        first_signal = _first_signal_for_episode(
            store, version_id, condition_id, token_id, event_at
        )
        if first_signal is None:
            raise LearningIngestError(
                "Focus LOCKED signal lineage not ingested yet"
            )
        lineage["candidate_first_seen_at"] = first_signal["observed_at"]
        lineage["price_at_first_seen"] = first_signal["signal_price"]
        lineage["locked_at"] = event_at
        lineage["price_at_lock"] = event.get("price_at_lock")
        lineage["first_ready_at"] = None
        lineage["price_at_first_ready"] = None
    else:
        lock = _latest_focus_event_payload(
            store,
            version_id,
            condition_id,
            token_id,
            "LOCKED",
            event_at,
        )
        if lock is None:
            raise LearningIngestError("Focus episode LOCKED event missing")
        for field in (
            "candidate_first_seen_at",
            "price_at_first_seen",
            "locked_at",
            "price_at_lock",
        ):
            lineage[field] = lock.get(field)

        first_ready = _latest_focus_event_payload(
            store,
            version_id,
            condition_id,
            token_id,
            "READY",
            event_at,
            after_at=lock.get("locked_at"),
        )
        if first_ready is not None:
            lineage["first_ready_at"] = (
                first_ready.get("first_ready_at")
                or first_ready.get("ready_at")
                or first_ready.get("event_at")
            )
            lineage["price_at_first_ready"] = (
                first_ready.get("price_at_first_ready")
                if first_ready.get("price_at_first_ready") is not None
                else first_ready.get("price_at_ready")
            )
        elif event_type == "READY":
            lineage["first_ready_at"] = event_at
            lineage["price_at_first_ready"] = event.get("price_at_ready")
        else:
            lineage["first_ready_at"] = None
            lineage["price_at_first_ready"] = None

    lineage["wait_confirmations"] = event.get("progress")
    lineage["invalidated"] = event_type == "INVALIDATED"
    if event_type == "READY":
        lineage["ready_at"] = event_at
        lineage["price_at_ready"] = event.get("price_at_ready")
    if event_type == "INVALIDATED":
        lineage["invalidated_at"] = event_at
        lineage["invalidation_reason_codes"] = list(
            event.get("invalidation_reason_codes") or []
        )

    inserted = store.insert_focus_event(
        focus_event_id,
        version_id,
        lineage,
    )
    return {
        "status": "INGESTED" if inserted else "ALREADY_INGESTED",
        "focus_event_id": focus_event_id,
        "event_type": event_type,
        "token_id": token_id,
    }


def ingest_pending(
    *,
    data_dir,
    db_path,
    now=None,
):
    data_dir = Path(data_dir)
    queue_dir = data_dir / "learning_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(queue_dir.glob("risk_*.json"))
    results = []

    with LearningStore(db_path) as store:
        already = store.ingested_generation_ids()

        for path in files:
            try:
                snapshot = _read_json(path)
                generation_id = (
                    _text(snapshot.get("source_generation_id"))
                    if isinstance(snapshot, dict)
                    else ""
                )
            except Exception:
                generation_id = ""

            if generation_id and generation_id in already:
                try:
                    path.unlink()
                except OSError:
                    pass
                results.append(
                    {
                        "status": "ALREADY_INGESTED",
                        "generation_id": generation_id,
                        "signals_ingested": 0,
                    }
                )
                continue

            result = ingest_queue_file(
                path,
                data_dir=data_dir,
                store=store,
                now=now,
            )
            results.append(result)
            if result["status"] in {"INGESTED", "ALREADY_INGESTED"}:
                try:
                    path.unlink()
                except OSError:
                    pass

    return results
