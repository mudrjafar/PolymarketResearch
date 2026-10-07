"""Canonical learning-data contract for Paper performance research.

This module is deliberately side-effect free. It defines storage/validation
semantics only and MUST NOT make trading, ranking, Focus, Book, or Paper-open
choices.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Mapping

LEARNING_SCHEMA_VERSION = 1

MUST = "MUST"
SHOULD = "SHOULD"
OPTIONAL = "OPTIONAL"

EXIT_MANUAL_CLOSE = "MANUAL_CLOSE"
EXIT_SETTLEMENT = "SETTLEMENT"
EXIT_TYPES = frozenset({EXIT_MANUAL_CLOSE, EXIT_SETTLEMENT})

SELECTION_PENDING = "PENDING"
SELECTION_SELECTED = "SELECTED"
SELECTION_NOT_SELECTED = "NOT_SELECTED_BEFORE_READY_ENDED"
SELECTION_STATUSES = frozenset(
    {SELECTION_PENDING, SELECTION_SELECTED, SELECTION_NOT_SELECTED}
)

FORECAST_PENDING = "PENDING"
FORECAST_EVALUABLE = "EVALUABLE"
FORECAST_NOT_EVALUABLE = "NOT_EVALUABLE"
FORECAST_STATUSES = frozenset(
    {FORECAST_PENDING, FORECAST_EVALUABLE, FORECAST_NOT_EVALUABLE}
)

# The contract intentionally stores compact attribution features, not raw trades.
PAPER_TRADE_FIELD_REQUIREMENTS = {
    "root": {
        "learning_schema_version": MUST,
        "paper_id": MUST,
        "open_request_id": MUST,
        "close_request_id": SHOULD,
        "ready_id": MUST,
    },
    "lineage": {
        "condition_id": MUST,
        "token_id": MUST,
        "outcome": MUST,
        "direction": MUST,
        "source_generation_id": MUST,
        "source_evidence_id": MUST,
        "source_evidence_cursor": MUST,
        "source_evidence_at": MUST,
        "focus_locked_at": MUST,
        "focus_ready_at": MUST,
        "paper_opened_at": MUST,
        "paper_closed_at": SHOULD,
        "settled_at": SHOULD,
        "question": SHOULD,
    },
    "versions": {
        "pipeline_version": MUST,
        "diamond_version": MUST,
        "risk_version": MUST,
        "focus_version": MUST,
        "book_version": MUST,
        "paper_version": MUST,
        "learning_schema_version": MUST,
        "git_commit_sha": MUST,
        "config_fingerprint": MUST,
    },
    "signal": {
        "signal_generated_at": MUST,
        "classification": MUST,
        "diamond": MUST,
        "direction": MUST,
        "signal_price": MUST,
        "signal_quality": MUST,
        "verification": MUST,
        "entry_quality": MUST,
        "resolution_reliability": MUST,
        "verified": MUST,
        "confirmations": MUST,
        "data_confidence": MUST,
        "trades_1m": MUST,
        "trades_5m": MUST,
        "trades_15m": MUST,
        "volume_1m": MUST,
        "volume_5m": MUST,
        "volume_15m": MUST,
        "net_flow_1m": MUST,
        "net_flow_5m": MUST,
        "net_flow_15m": MUST,
        "strength_1m": MUST,
        "strength_5m": MUST,
        "strength_15m": MUST,
        "largest_trade_5m": MUST,
        "largest_trade_ratio": MUST,
        "cashflow_active": MUST,
        "cashflow_tier": SHOULD,
        "cashflow_quality": SHOULD,
        "time_to_resolution_seconds": MUST,
        "generation_rank": SHOULD,
        "wallet_evidence": OPTIONAL,
        "forecast_probability": OPTIONAL,
        "probability_model_version": OPTIONAL,
    },
    "risk": {
        "risk_checked_at": MUST,
        "risk_ok": MUST,
        "risk_decision": MUST,
        "risk_reason_codes": MUST,
        "risk_warnings": SHOULD,
        "risk_reasons": SHOULD,
        # No risk_score field: current Risk is a deterministic veto/gate.
    },
    "focus": {
        "candidate_first_seen_at": MUST,
        "locked_at": MUST,
        "ready_at": MUST,
        "price_at_first_seen": MUST,
        "price_at_lock": MUST,
        "price_at_ready": MUST,
        "wait_confirmations": MUST,
        "ready_evidence_id": MUST,
        "ready_evidence_cursor": MUST,
        "ready_evidence_at": MUST,
        "invalidated": MUST,
        "invalidated_at": SHOULD,
        "invalidation_reason_codes": SHOULD,
        "challenger_present": SHOULD,
        "challenger_token_id": OPTIONAL,
        "challenger_signal_quality": OPTIONAL,
    },
    "entry_execution": {
        "book_hash": MUST,
        "book_timestamp": MUST,
        "best_bid": MUST,
        "best_ask": MUST,
        "midpoint": MUST,
        "spread": MUST,
        "spread_bps": MUST,
        "entry_vwap": MUST,
        "effective_entry_price": MUST,
        "worst_entry_price": MUST,
        "entry_fee_usd": MUST,
        "entry_slippage_bps": MUST,
        "gross_tokens": MUST,
        "net_tokens": MUST,
        "investment_usd": MUST,
    },
    "position_path": {
        "mark_status": SHOULD,
        "mfe_pnl_usd": SHOULD,
        "mfe_return_pct": SHOULD,
        "mfe_at": SHOULD,
        "mae_pnl_usd": SHOULD,
        "mae_return_pct": SHOULD,
        "mae_at": SHOULD,
        "mark_observation_count": SHOULD,
        "largest_mark_gap_seconds": SHOULD,
    },
    "exit": {
        "exit_type": SHOULD,
        "exit_at": SHOULD,
        "best_bid": SHOULD,
        "exit_vwap": SHOULD,
        "effective_exit_price": SHOULD,
        "worst_exit_price": SHOULD,
        "exit_slippage_bps": SHOULD,
        "exit_fee_usd": SHOULD,
        "gross_proceeds_usd": SHOULD,
        "net_proceeds_usd": SHOULD,
        "holding_seconds": SHOULD,
        "realized_pnl_usd": SHOULD,
        "realized_return_pct": SHOULD,
        "resolved_outcome": OPTIONAL,
        "payout_per_token": OPTIONAL,
        "settlement_value_usd": OPTIONAL,
    },
    "forecast_result": {
        "status": MUST,
        "forecast_evaluable": MUST,
        "direction_correct": OPTIONAL,
        "forecast_target_y": OPTIONAL,
        "brier_score": OPTIONAL,
        "log_loss": OPTIONAL,
    },
}

STRATEGY_VERSION_REQUIRED_FIELDS = (
    "pipeline_version",
    "diamond_version",
    "risk_version",
    "focus_version",
    "book_version",
    "paper_version",
    "git_commit_sha",
    "config_fingerprint",
)


class LearningContractError(ValueError):
    """Raised when a learning record violates the frozen data contract."""


def nullable_finite_number(value: Any) -> float | None:
    """Parse a finite number without converting UNKNOWN to zero."""
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _nonempty(value: Any) -> bool:
    return bool(str(value or "").strip())


def _timezone_aware_iso(value: Any) -> bool:
    if not _nonempty(value):
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return False
    return parsed.tzinfo is not None


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LearningContractError(f"{label} must be an object")
    return value


def validate_strategy_versions(versions: Mapping[str, Any]) -> None:
    versions = _mapping(versions, "versions")
    if versions.get("learning_schema_version") != LEARNING_SCHEMA_VERSION:
        raise LearningContractError("learning schema version mismatch")
    for field in STRATEGY_VERSION_REQUIRED_FIELDS:
        if not _nonempty(versions.get(field)):
            raise LearningContractError(f"versions.{field} is required")


def _require_fields(section_name: str, section: Mapping[str, Any]) -> None:
    requirements = PAPER_TRADE_FIELD_REQUIREMENTS[section_name]
    for field, level in requirements.items():
        if level != MUST:
            continue
        if field not in section or section.get(field) is None:
            raise LearningContractError(f"{section_name}.{field} is required")


def _validate_cursor(value: Any, label: str) -> None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise LearningContractError(f"{label} must be [block, log_index]")
    block, log_index = value
    if isinstance(block, bool) or isinstance(log_index, bool):
        raise LearningContractError(f"{label} must contain integers")
    if not isinstance(block, int) or not isinstance(log_index, int):
        raise LearningContractError(f"{label} must contain integers")
    if block < 0 or log_index < 0:
        raise LearningContractError(f"{label} cannot be negative")


def validate_paper_trade_record(record: Mapping[str, Any]) -> None:
    """Validate an OPEN-capable canonical learning trade record.

    Exit and settlement fields are intentionally allowed to remain absent/null
    while the position is open. Forecast UNKNOWN is represented by null, never 0.
    """
    record = _mapping(record, "record")
    if record.get("learning_schema_version") != LEARNING_SCHEMA_VERSION:
        raise LearningContractError("learning schema version mismatch")

    _require_fields("root", record)
    for section_name in (
        "lineage",
        "versions",
        "signal",
        "risk",
        "focus",
        "entry_execution",
        "forecast_result",
    ):
        section = _mapping(record.get(section_name), section_name)
        _require_fields(section_name, section)

    validate_strategy_versions(record["versions"])

    lineage = record["lineage"]
    _validate_cursor(lineage.get("source_evidence_cursor"), "lineage.source_evidence_cursor")
    for field in (
        "source_evidence_at",
        "focus_locked_at",
        "focus_ready_at",
        "paper_opened_at",
    ):
        if not _timezone_aware_iso(lineage.get(field)):
            raise LearningContractError(f"lineage.{field} must be timezone-aware ISO-8601")

    focus = record["focus"]
    _validate_cursor(focus.get("ready_evidence_cursor"), "focus.ready_evidence_cursor")
    for field in ("candidate_first_seen_at", "locked_at", "ready_at", "ready_evidence_at"):
        if not _timezone_aware_iso(focus.get(field)):
            raise LearningContractError(f"focus.{field} must be timezone-aware ISO-8601")

    if str(lineage.get("source_evidence_id")) != str(focus.get("ready_evidence_id")):
        raise LearningContractError("READY evidence id must match Paper lineage evidence id")
    if list(lineage.get("source_evidence_cursor")) != list(focus.get("ready_evidence_cursor")):
        raise LearningContractError("READY evidence cursor must match Paper lineage evidence cursor")

    risk = record["risk"]
    if not isinstance(risk.get("risk_ok"), bool):
        raise LearningContractError("risk.risk_ok must be boolean")
    if not isinstance(risk.get("risk_reason_codes"), list):
        raise LearningContractError("risk.risk_reason_codes must be a list")

    signal = record["signal"]
    probability = signal.get("forecast_probability")
    if probability is not None:
        probability = nullable_finite_number(probability)
        if probability is None or not 0.0 <= probability <= 1.0:
            raise LearningContractError("signal.forecast_probability must be in [0,1] or null")
        if not _nonempty(signal.get("probability_model_version")):
            raise LearningContractError(
                "signal.probability_model_version is required when forecast_probability exists"
            )

    forecast = record["forecast_result"]
    if forecast.get("status") not in FORECAST_STATUSES:
        raise LearningContractError("forecast_result.status invalid")
    if not isinstance(forecast.get("forecast_evaluable"), bool):
        raise LearningContractError("forecast_result.forecast_evaluable must be boolean")

    exit_payload = record.get("exit")
    if exit_payload is not None:
        exit_payload = _mapping(exit_payload, "exit")
        exit_type = exit_payload.get("exit_type")
        if exit_type is not None and exit_type not in EXIT_TYPES:
            raise LearningContractError("exit.exit_type invalid")
