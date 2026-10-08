"""LD-2 ingestion from frozen Diamond generations + archived Risk decisions.

This module never calls Diamond or Risk decision functions. It consumes only
already-produced artifacts and writes observational learning rows.
"""

from __future__ import annotations

import hashlib
import json
import math
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


def ready_opportunity_id(focus_event_id: str) -> str:
    return _id("READY-", focus_event_id)


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


def _episode_bounds(store, condition_id, token_id, event_at):
    event_dt = _parse_time(event_at)
    if event_dt is None:
        raise LearningIngestError("Focus event_at invalid")

    last_invalidated = None
    rows = store.conn.execute(
        "SELECT event_at FROM focus_events "
        "WHERE condition_id=? AND token_id=? "
        "AND event_type='INVALIDATED' ORDER BY event_at DESC",
        (str(condition_id), str(token_id)),
    ).fetchall()
    for row in rows:
        value = _parse_time(row["event_at"])
        if value is not None and value < event_dt:
            last_invalidated = value
            break

    return last_invalidated, event_dt


def _first_signal_for_episode(
    store,
    condition_id,
    token_id,
    event_at,
):
    lower, upper = _episode_bounds(
        store, condition_id, token_id, event_at
    )
    rows = store.conn.execute(
        "SELECT version_id,observed_at,signal_price,payload_json "
        "FROM signal_observations "
        "WHERE condition_id=? AND token_id=? "
        "ORDER BY observed_at ASC",
        (str(condition_id), str(token_id)),
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
        "SELECT version_id,event_at,payload_json FROM focus_events "
        "WHERE condition_id=? AND token_id=? "
        "AND event_type=? ORDER BY event_at DESC",
        (
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
            payload = dict(payload)
            payload["_event_version_id"] = row["version_id"]
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
            store, condition_id, token_id, event_at
        )
        if first_signal is None:
            raise LearningIngestError(
                "Focus LOCKED signal lineage not ingested yet"
            )
        lineage["candidate_first_seen_at"] = first_signal["observed_at"]
        lineage["candidate_first_seen_version_id"] = first_signal["version_id"]
        lineage["price_at_first_seen"] = first_signal["signal_price"]
        lineage["locked_at"] = event_at
        lineage["lock_version_id"] = version_id
        lineage["focus_episode_mixed_version"] = False
        lineage["price_at_lock"] = event.get("price_at_lock")
        lineage["first_ready_at"] = None
        lineage["price_at_first_ready"] = None
    else:
        lock = _latest_focus_event_payload(
            store,
            condition_id,
            token_id,
            "LOCKED",
            event_at,
        )
        if lock is None:
            raise LearningIngestError("Focus episode LOCKED event missing")
        for field in (
            "candidate_first_seen_at",
            "candidate_first_seen_version_id",
            "price_at_first_seen",
            "locked_at",
            "lock_version_id",
            "price_at_lock",
        ):
            lineage[field] = lock.get(field)
        lineage["focus_episode_mixed_version"] = (
            str(lock.get("lock_version_id") or lock.get("_event_version_id") or "")
            != str(version_id)
        )

        first_ready = _latest_focus_event_payload(
            store,
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

    ready_id = None
    ready_inserted = False

    if event_type == "READY":
        ready_id = ready_opportunity_id(focus_event_id)
        ready_row = dict(lineage)
        ready_row.update(
            {
                "population": "SYSTEM_READY",
                "focus_event_id": focus_event_id,
                "ready_at": event_at,
                "selection_status": "PENDING",
                "selected_paper_id": None,
                "ended_at": None,
            }
        )
        ready_inserted = store.insert_ready_opportunity(
            ready_id,
            version_id,
            focus_event_id,
            ready_row,
        )

    elif event_type in {"WAIT", "INVALIDATED"}:
        store.end_open_ready_opportunities(
            condition_id,
            token_id,
            event_at,
        )

    return {
        "status": (
            "INGESTED"
            if inserted or ready_inserted
            else "ALREADY_INGESTED"
        ),
        "focus_event_id": focus_event_id,
        "event_type": event_type,
        "token_id": token_id,
        "ready_id": ready_id,
    }


def book_decision_id(book_observation_id: str) -> str:
    return _id("BOOK-", book_observation_id)


def ingest_book_queue_file(queue_file, *, store: LearningStore):
    """Bind one Book observation to exactly one SYSTEM_READY opportunity."""
    snapshot = _read_json(Path(queue_file))
    if not isinstance(snapshot, dict):
        raise LearningIngestError("Book queue snapshot must be an object")

    observation_id = _text(snapshot.get("book_observation_id"))
    versions = snapshot.get("strategy_versions")
    observation = snapshot.get("observation")
    if not observation_id:
        raise LearningIngestError("book_observation_id missing")
    if not isinstance(versions, dict):
        raise LearningIngestError("Book strategy_versions missing")
    if not isinstance(observation, dict):
        raise LearningIngestError("Book observation missing")

    generated_at = _text(observation.get("generated_at"))
    generation_id = _text(observation.get("source_generation_id"))
    evidence_id = _text(observation.get("source_evidence_id"))
    condition_id = _text(observation.get("condition_id"))
    token_id = _text(observation.get("token_id"))
    status = _text(observation.get("status"))
    if not all(
        (
            generated_at,
            generation_id,
            evidence_id,
            condition_id,
            token_id,
            status,
        )
    ):
        raise LearningIngestError("Book observation identity incomplete")

    version_id = store.register_strategy_version(versions)

    rows = store.conn.execute(
        "SELECT ready_id,version_id,ready_at,ended_at FROM ready_opportunities "
        "WHERE source_generation_id=? AND source_evidence_id=? "
        "AND condition_id=? AND token_id=?",
        (generation_id, evidence_id, condition_id, token_id),
    ).fetchall()
    if len(rows) != 1:
        raise LearningIngestError(
            "Book observation must match exactly one SYSTEM_READY opportunity"
        )

    ready = rows[0]
    if str(ready["version_id"]) != str(version_id):
        raise LearningIngestError("Book/READY strategy provenance mismatch")

    observed_dt = _parse_time(generated_at)
    ready_dt = _parse_time(ready["ready_at"])
    ended_dt = _parse_time(ready["ended_at"]) if ready["ended_at"] else None
    if observed_dt is None or ready_dt is None:
        raise LearningIngestError("Book/READY timestamp invalid")
    if observed_dt < ready_dt:
        raise LearningIngestError("Book observation predates READY")
    if ended_dt is not None and observed_dt > ended_dt:
        raise LearningIngestError("Book observation occurs after READY ended")

    book_ok = observation.get("book_ok")
    if not isinstance(book_ok, bool):
        raise LearningIngestError("Book book_ok invalid")
    reason_codes = observation.get("reason_codes")
    if not isinstance(reason_codes, list) or not all(
        isinstance(code, str) for code in reason_codes
    ):
        raise LearningIngestError("Book reason_codes invalid")

    row = dict(observation)
    row.update(
        {
            "book_observation_id": observation_id,
            "ready_id": ready["ready_id"],
            "checked_at": generated_at,
            "measurement_status": (
                "MEASURED" if status == "OK" else "UNAVAILABLE"
            ),
        }
    )
    decision_id = book_decision_id(observation_id)
    inserted = store.insert_book_decision(
        decision_id,
        ready["ready_id"],
        row,
    )
    return {
        "status": "INGESTED" if inserted else "ALREADY_INGESTED",
        "book_decision_id": decision_id,
        "book_observation_id": observation_id,
        "ready_id": ready["ready_id"],
        "book_status": status,
        "book_ok": book_ok,
    }


def ingest_paper_open_queue_file(queue_file, *, store: LearningStore):
    """Bind one authoritative successful Paper OPEN to exactly one READY."""
    snapshot = _read_json(Path(queue_file))
    if not isinstance(snapshot, dict):
        raise LearningIngestError("Paper OPEN queue snapshot must be an object")

    snapshot_id = _text(snapshot.get("paper_open_snapshot_id"))
    versions = snapshot.get("strategy_versions")
    paper_open = snapshot.get("paper_open")
    if not snapshot_id:
        raise LearningIngestError("paper_open_snapshot_id missing")
    if not isinstance(versions, dict):
        raise LearningIngestError("Paper OPEN strategy_versions missing")
    if not isinstance(paper_open, dict):
        raise LearningIngestError("Paper OPEN payload missing")

    required = (
        "paper_id",
        "open_request_id",
        "opened_at",
        "source_generation_id",
        "source_evidence_id",
        "condition_id",
        "token_id",
        "outcome",
        "direction",
        "investment_usd",
        "entry",
    )
    for field in required:
        if paper_open.get(field) is None or (
            field != "investment_usd"
            and field != "entry"
            and not _text(paper_open.get(field))
        ):
            raise LearningIngestError(f"Paper OPEN {field} missing")

    if not isinstance(paper_open.get("entry"), dict):
        raise LearningIngestError("Paper OPEN entry must be an object")

    paper_id = _text(paper_open.get("paper_id"))
    request_id = _text(paper_open.get("open_request_id"))
    opened_at = _text(paper_open.get("opened_at"))
    generation_id = _text(paper_open.get("source_generation_id"))
    evidence_id = _text(paper_open.get("source_evidence_id"))
    condition_id = _text(paper_open.get("condition_id"))
    token_id = _text(paper_open.get("token_id"))

    rows = store.conn.execute(
        "SELECT ready_id,version_id,ready_at,ended_at,payload_json "
        "FROM ready_opportunities "
        "WHERE source_generation_id=? AND source_evidence_id=? "
        "AND condition_id=? AND token_id=?",
        (generation_id, evidence_id, condition_id, token_id),
    ).fetchall()
    if len(rows) != 1:
        raise LearningIngestError(
            "Paper OPEN must match exactly one SYSTEM_READY opportunity"
        )

    ready = rows[0]
    opened_dt = _parse_time(opened_at)
    ready_dt = _parse_time(ready["ready_at"])
    ended_dt = _parse_time(ready["ended_at"]) if ready["ended_at"] else None
    if opened_dt is None or ready_dt is None:
        raise LearningIngestError("Paper OPEN/READY timestamp invalid")
    if opened_dt < ready_dt:
        raise LearningIngestError("Paper OPEN predates READY")
    if ended_dt is not None and opened_dt > ended_dt:
        raise LearningIngestError("Paper OPEN occurs after READY ended")

    try:
        ready_payload = json.loads(ready["payload_json"])
    except (TypeError, ValueError) as exc:
        raise LearningIngestError("READY payload unreadable") from exc
    if not isinstance(ready_payload, dict):
        raise LearningIngestError("READY payload invalid")

    if _text(ready_payload.get("outcome")).casefold() != _text(
        paper_open.get("outcome")
    ).casefold():
        raise LearningIngestError("Paper OPEN outcome does not match READY")
    if _text(ready_payload.get("direction")).upper() != _text(
        paper_open.get("direction")
    ).upper():
        raise LearningIngestError("Paper OPEN direction does not match READY")

    try:
        investment = float(paper_open.get("investment_usd"))
    except (TypeError, ValueError) as exc:
        raise LearningIngestError("Paper OPEN investment_usd invalid") from exc
    if not math.isfinite(investment) or investment <= 0:
        raise LearningIngestError("Paper OPEN investment_usd invalid")

    version_id = store.register_strategy_version(versions)

    immutable = {
        "paper_open_snapshot_id": snapshot_id,
        "paper_open": dict(paper_open),
        "ready": ready_payload,
        "ready_version_id": ready["version_id"],
    }
    mutable = {
        "status": "OPEN",
    }
    inserted = store.bind_paper_open(
        paper_id=paper_id,
        ready_id=ready["ready_id"],
        version_id=version_id,
        open_request_id=request_id,
        row={
            "opened_at": opened_at,
            "status": "OPEN",
            "condition_id": condition_id,
            "token_id": token_id,
            "outcome": paper_open.get("outcome"),
            "direction": _text(paper_open.get("direction")).upper(),
            "investment_usd": investment,
            "immutable": immutable,
            "mutable": mutable,
        },
    )

    return {
        "status": "INGESTED" if inserted else "ALREADY_INGESTED",
        "paper_open_snapshot_id": snapshot_id,
        "paper_id": paper_id,
        "ready_id": ready["ready_id"],
        "open_request_id": request_id,
    }


def finalize_ready_selection_from_paper_state(*, data_dir, store: LearningStore):
    """Reconcile user selection intent and ended READY outcomes.

    Explicit OPEN requests count as USER_SELECTED even if later execution is
    rejected. Successful Paper OPEN binding is handled separately and may add
    selected_paper_id. NOT_SELECTED is finalized only after Paper authority has
    advanced beyond the READY window and no matching successful position exists.
    """
    data_dir = Path(data_dir)
    request_dir = data_dir / "paper_requests"

    open_requests = []
    if request_dir.exists():
        for path in sorted(request_dir.glob("*.json")):
            try:
                payload = _read_json(path)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("schema_version") != 1:
                continue
            if _text(payload.get("request_id")) != path.stem:
                continue
            if _text(payload.get("action")).upper() != "OPEN":
                continue
            requested_dt = _parse_time(payload.get("requested_at"))
            if requested_dt is None:
                continue
            try:
                amount = float(payload.get("amount_usd"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(amount) or amount <= 0:
                continue
            open_requests.append((payload, requested_dt))

    pending_all = store.conn.execute(
        "SELECT ready_id,source_generation_id,source_evidence_id,"
        "condition_id,token_id,ready_at,ended_at "
        "FROM ready_opportunities "
        "WHERE selection_status='PENDING' AND selected_paper_id IS NULL "
        "ORDER BY ready_at"
    ).fetchall()

    selected = 0
    for ready in pending_all:
        ready_dt = _parse_time(ready["ready_at"])
        ended_dt = _parse_time(ready["ended_at"]) if ready["ended_at"] else None
        if ready_dt is None:
            continue

        matched_request = False
        for request, requested_dt in open_requests:
            if requested_dt < ready_dt:
                continue
            if ended_dt is not None and requested_dt > ended_dt:
                continue
            if (
                _text(request.get("source_generation_id"))
                != _text(ready["source_generation_id"])
                or _text(request.get("source_evidence_id"))
                != _text(ready["source_evidence_id"])
                or _text(request.get("condition_id"))
                != _text(ready["condition_id"])
                or _text(request.get("token_id"))
                != _text(ready["token_id"])
            ):
                continue
            matched_request = True
            break

        if matched_request and store.mark_ready_selected(ready["ready_id"]):
            selected += 1

    state_path = data_dir / "paper_state.json"
    if not state_path.exists():
        return {
            "status": "NO_PAPER_STATE",
            "selected": selected,
            "finalized": 0,
            "deferred": 0,
        }

    try:
        state = _read_json(state_path)
    except Exception as exc:
        return {
            "status": "PAPER_STATE_UNREADABLE",
            "selected": selected,
            "finalized": 0,
            "deferred": 0,
            "error": type(exc).__name__,
        }

    if (
        not isinstance(state, dict)
        or state.get("schema_version") != 1
        or not isinstance(state.get("positions"), list)
    ):
        return {
            "status": "PAPER_STATE_INVALID",
            "selected": selected,
            "finalized": 0,
            "deferred": 0,
        }

    state_updated = _parse_time(state.get("updated_at"))
    if state_updated is None:
        return {
            "status": "PAPER_STATE_INVALID",
            "selected": selected,
            "finalized": 0,
            "deferred": 0,
        }

    pending_ended = store.conn.execute(
        "SELECT ready_id,source_generation_id,source_evidence_id,"
        "condition_id,token_id,ready_at,ended_at "
        "FROM ready_opportunities "
        "WHERE selection_status='PENDING' "
        "AND selected_paper_id IS NULL AND ended_at IS NOT NULL "
        "ORDER BY ended_at"
    ).fetchall()

    finalized = 0
    deferred = 0
    positions = [row for row in state.get("positions", []) if isinstance(row, dict)]

    for ready in pending_ended:
        ended_dt = _parse_time(ready["ended_at"])
        ready_dt = _parse_time(ready["ready_at"])
        if ended_dt is None or ready_dt is None or state_updated <= ended_dt:
            deferred += 1
            continue

        matching_positions = []
        for position in positions:
            if (
                _text(position.get("source_generation_id"))
                != _text(ready["source_generation_id"])
                or _text(position.get("source_evidence_id"))
                != _text(ready["source_evidence_id"])
                or _text(position.get("condition_id"))
                != _text(ready["condition_id"])
                or _text(position.get("token_id"))
                != _text(ready["token_id"])
            ):
                continue

            opened_dt = _parse_time(position.get("opened_at"))
            if opened_dt is None:
                matching_positions.append(position)
                continue
            if ready_dt <= opened_dt <= ended_dt:
                matching_positions.append(position)

        if matching_positions:
            # A successful Paper OPEN exists. Its learning queue may be lagging.
            deferred += 1
            continue

        if store.finalize_ready_not_selected(ready["ready_id"]):
            finalized += 1

    return {
        "status": "OK",
        "selected": selected,
        "finalized": finalized,
        "deferred": deferred,
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
