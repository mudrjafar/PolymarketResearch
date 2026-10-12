from copy import deepcopy

import pytest

from scripts.learning_contract import (
    FORECAST_PENDING,
    LEARNING_SCHEMA_VERSION,
    LearningContractError,
    nullable_finite_number,
    validate_paper_trade_record,
)

NOW = "2026-10-07T12:00:00+00:00"
EVIDENCE = "0x" + "ab" * 32 + ":7"


def record():
    return {
        "learning_schema_version": LEARNING_SCHEMA_VERSION,
        "paper_id": "PAPER-1",
        "open_request_id": "REQ-1",
        "close_request_id": None,
        "ready_id": "READY-1",
        "lineage": {
            "condition_id": "condition-a",
            "token_id": "token-a",
            "outcome": "Yes",
            "direction": "BUY",
            "source_generation_id": "GEN-1",
            "source_evidence_id": EVIDENCE,
            "source_evidence_cursor": [100, 7],
            "source_evidence_at": NOW,
            "focus_locked_at": NOW,
            "focus_ready_at": NOW,
            "paper_opened_at": NOW,
            "paper_closed_at": None,
            "settled_at": None,
        },
        "versions": {
            "pipeline_version": "pipeline-v1",
            "diamond_version": "diamond-v3.1",
            "risk_version": "risk-v2",
            "focus_version": "focus-v3",
            "book_version": "book-v1",
            "paper_version": "paper-v1",
            "learning_schema_version": LEARNING_SCHEMA_VERSION,
            "git_commit_sha": "a" * 40,
            "config_fingerprint": "cfg-1",
        },
        "signal": {
            "signal_generated_at": NOW,
            "classification": "DIAMOND",
            "diamond": True,
            "direction": "BUY",
            "signal_price": 0.55,
            "signal_quality": 88.0,
            "verification": 90.0,
            "entry_quality": 70.0,
            "resolution_reliability": 85.0,
            "verified": True,
            "confirmations": 3,
            "data_confidence": 0.9,
            "trades_1m": 4,
            "trades_5m": 12,
            "trades_15m": 22,
            "volume_1m": 400.0,
            "volume_5m": 2500.0,
            "volume_15m": 5000.0,
            "net_flow_1m": 300.0,
            "net_flow_5m": 1800.0,
            "net_flow_15m": 3200.0,
            "strength_1m": 0.6,
            "strength_5m": 0.55,
            "strength_15m": 0.4,
            "largest_trade_5m": 500.0,
            "largest_trade_ratio": 0.2,
            "cashflow_active": False,
            "time_to_resolution_seconds": 3600.0,
            "forecast_probability": None,
            "probability_model_version": None,
        },
        "risk": {
            "risk_checked_at": NOW,
            "risk_ok": True,
            "risk_decision": "PASS",
            "risk_reason_codes": [],
        },
        "focus": {
            "candidate_first_seen_at": NOW,
            "locked_at": NOW,
            "ready_at": NOW,
            "price_at_first_seen": 0.54,
            "price_at_lock": 0.55,
            "price_at_ready": 0.56,
            "wait_confirmations": 3,
            "ready_evidence_id": EVIDENCE,
            "ready_evidence_cursor": [100, 7],
            "ready_evidence_at": NOW,
            "invalidated": False,
        },
        "entry_execution": {
            "book_hash": "book-1",
            "book_timestamp": "1",
            "best_bid": 0.55,
            "best_ask": 0.56,
            "midpoint": 0.555,
            "spread": 0.01,
            "spread_bps": 180.18,
            "entry_vwap": 0.561,
            "effective_entry_price": 0.562,
            "worst_entry_price": 0.57,
            "entry_fee_usd": 0.05,
            "entry_slippage_bps": 17.86,
            "gross_tokens": 44.56,
            "net_tokens": 44.48,
            "investment_usd": 25.0,
        },
        "position_path": {},
        "exit": None,
        "forecast_result": {
            "status": FORECAST_PENDING,
            "forecast_evaluable": False,
            "direction_correct": None,
            "forecast_target_y": None,
            "brier_score": None,
            "log_loss": None,
        },
    }


def test_open_record_validates_without_probability_or_exit():
    validate_paper_trade_record(record())


def test_unknown_numeric_is_never_coerced_to_zero():
    assert nullable_finite_number(None) is None
    assert nullable_finite_number("") is None
    assert nullable_finite_number("not-a-number") is None
    assert nullable_finite_number("0") == 0.0


def test_probability_requires_real_model_version():
    value = record()
    value["signal"]["forecast_probability"] = 0.7
    with pytest.raises(LearningContractError, match="probability_model_version"):
        validate_paper_trade_record(value)


def test_signal_score_is_not_implicitly_a_probability():
    value = record()
    value["signal"]["signal_quality"] = 87.0
    value["signal"]["forecast_probability"] = None
    validate_paper_trade_record(value)
    assert value["signal"]["forecast_probability"] is None


def test_ready_evidence_must_match_paper_lineage():
    value = deepcopy(record())
    value["focus"]["ready_evidence_cursor"] = [101, 1]
    with pytest.raises(LearningContractError, match="READY evidence cursor"):
        validate_paper_trade_record(value)


def test_empty_canonical_identity_is_rejected():
    value = record()
    value["paper_id"] = ""
    with pytest.raises(LearningContractError, match="root.paper_id"):
        validate_paper_trade_record(value)

    value = record()
    value["lineage"]["token_id"] = "   "
    with pytest.raises(LearningContractError, match="lineage.token_id"):
        validate_paper_trade_record(value)


def test_signal_direction_must_match_lineage_direction():
    value = record()
    value["signal"]["direction"] = "SELL"
    with pytest.raises(LearningContractError, match="signal.direction"):
        validate_paper_trade_record(value)


def test_book_identity_fields_must_be_nonempty():
    value = record()
    value["entry_execution"]["book_hash"] = ""
    with pytest.raises(LearningContractError, match="entry_execution.book_hash"):
        validate_paper_trade_record(value)
