"""Offline tests for the read-only Paper dashboard projection.

Covers real Paper state shape, fee-aware MARK, CLOSE, SETTLED, unknown/stale
values, corrupt files, and deliberate refusal to read old paper_trades.json.
"""

import json
from datetime import datetime, timedelta, timezone

from scripts.paper_dashboard import read_paper_summary


NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def _store(tmp_path, positions, *, when=NOW):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    payload = {
        "schema_version": 1,
        "updated_at": when.isoformat(),
        "positions": positions,
        "processed_request_ids": [],
    }
    (data / "paper_state.json").write_text(json.dumps(payload), encoding="utf-8")
    return data


def _position(paper_id, status, investment=25.0):
    return {
        "paper_id": paper_id,
        "status": status,
        "question": "Fixture Market",
        "outcome": "Yes",
        "investment_usd": investment,
        "entry": {"effective_entry_price": 0.51},
    }


def test_dashboard_reads_open_close_settled_with_fees_and_nonlegacy_sources(tmp_path):
    opened = _position("P1", "OPEN")
    opened.update(
        mark_status="OK",
        mark={
            "generated_at": NOW.isoformat(),
            "pnl_usd": -1.25,
            "return_pct": -5.0,
            "current_value_usd": 23.75,
        },
    )
    closed = _position("P2", "CLOSED")
    closed.update(
        realized_pnl_usd=2.50,
        realized_return_pct=10.0,
        exit={"net_proceeds_usd": 27.5},
    )
    settled = _position("P3", "SETTLED")
    settled.update(
        realized_pnl_usd=-25.0,
        realized_return_pct=-100.0,
        settlement_value_usd=0.0,
    )
    data = _store(tmp_path, [opened, closed, settled])
    (data / "paper_trades.json").write_text(
        '[{"status":"OPEN","pnl_usd":99999}]', encoding="utf-8"
    )

    result = read_paper_summary(data, now=NOW)

    assert result["status"] == "OK"
    assert result["open_count"] == 1
    assert result["realized_count"] == 2
    assert result["open_pnl_usd"] == -1.25
    assert result["realized_pnl_usd"] == -22.50
    assert {x["Status"] for x in result["rows"]} == {"OPEN", "CLOSED", "SETTLED"}
    assert result["rows"][0]["Current/Exit"] == 0.0
    assert result["rows"][1]["Current/Exit"] == 27.5
    assert result["rows"][2]["Mark status"] == "OK"
    assert all(x["P/L"] != 99999 for x in result["rows"])


def test_dashboard_does_not_turn_missing_mark_into_zero(tmp_path):
    open_position = _position("OPEN1", "OPEN")
    open_position["mark_status"] = "INSUFFICIENT_EXIT_DEPTH"
    data = _store(tmp_path, [open_position])
    result = read_paper_summary(data, now=NOW)
    assert result["status"] == "OK"
    assert result["open_pnl_usd"] is None
    assert result["rows"][0]["P/L"] is None
    assert result["rows"][0]["Mark status"] == "INSUFFICIENT_EXIT_DEPTH"


def test_dashboard_stale_mark_and_snapshot_never_claim_fresh_pnl(tmp_path):
    open_position = _position("OPEN1", "OPEN")
    open_position.update(
        mark_status="OK",
        mark={
            "generated_at": (NOW - timedelta(minutes=10)).isoformat(),
            "pnl_usd": 12.0,
            "return_pct": 48.0,
            "current_value_usd": 37.0,
        },
    )
    data = _store(tmp_path, [open_position])
    result = read_paper_summary(data, now=NOW)
    assert result["status"] == "OK"
    assert result["open_pnl_usd"] is None
    assert result["rows"][0]["Mark status"] == "MARK_UNKNOWN"

    data = _store(tmp_path, [open_position], when=NOW - timedelta(hours=1))
    result = read_paper_summary(data, now=NOW)
    assert result["status"] == "STALE"
    assert result["open_pnl_usd"] is None
    assert result["rows"][0]["Mark status"] == "STALE_SNAPSHOT"


def test_dashboard_missing_and_corrupt_state_does_not_fallback_to_old_file(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "paper_trades.json").write_text(
        '[{"status":"OPEN","pnl_usd":100}]', encoding="utf-8"
    )
    result = read_paper_summary(data, now=NOW)
    assert result["status"] == "MISSING"
    assert result["open_count"] is None
    assert result["rows"] == []

    (data / "paper_state.json").write_text("{broken", encoding="utf-8")
    assert read_paper_summary(data, now=NOW)["status"] == "CORRUPT"


def test_dashboard_duplicate_position_ids_and_invalid_numbers_fail_closed(tmp_path):
    p = _position("DUP", "OPEN")
    data = _store(tmp_path, [p, dict(p)])
    assert read_paper_summary(data, now=NOW)["status"] == "CORRUPT"
    valid = _position("ONLY", "OPEN")
    valid.update(
        mark_status="OK",
        mark={
            "generated_at": NOW.isoformat(),
            "pnl_usd": float("nan"),
            "return_pct": "nan",
            "current_value_usd": "nan",
        },
    )
    # json serialization allows NaN by default; the reader must still refuse it.
    data = _store(tmp_path, [valid])
    result = read_paper_summary(data, now=NOW)
    assert result["open_pnl_usd"] is None
    assert result["rows"][0]["P/L"] is None
