"""GENERAL1 QUANT VALIDATION 1: read-only data-population and funnel audit.

Does not run the trading stack, write to either database, fetch a book, or
turn paper/selected-trade results into a claim of predictive alpha.
Standard library only. Usage:
  python scripts/quant_validation1.py --data-dir data
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


def _open_readonly(path: Path):
    if not path.is_file():
        return None
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _tables(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}


def _count(conn, table):
    return conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def _groups(conn, table, field):
    return {str(row[0]): row[1] for row in conn.execute(
        "SELECT " + field + ", COUNT(*) FROM " + table +
        " GROUP BY " + field
    )}


def _utc_valid(value):
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.tzinfo is not None and dt.utcoffset() is not None
    except (TypeError, ValueError):
        return False


def learning_audit(path: Path):
    out = {"exists": path.is_file(), "status": "MISSING",
           "quantitative_edge": "NOT_YET_MEASURABLE"}
    conn = _open_readonly(path)
    if conn is None:
        out["reason"] = "No learning.sqlite3 at this path"
        return out
    try:
        tables = _tables(conn)
        needed = {"signal_observations", "risk_decisions", "focus_events",
                  "ready_opportunities", "book_decisions", "paper_trades",
                  "paper_exits", "paper_marks"}
        absent = sorted(needed - tables)
        if absent:
            out.update(status="INCOMPLETE_SCHEMA", missing_tables=absent)
            return out
        out["counts"] = {name: _count(conn, name) for name in sorted(needed)}
        out["signal_classes"] = _groups(
            conn, "signal_observations", "classification")
        out["risk_decisions"] = _groups(conn, "risk_decisions", "decision")
        out["focus_events"] = _groups(conn, "focus_events", "event_type")
        out["ready_selection"] = _groups(
            conn, "ready_opportunities", "selection_status")
        out["book_status"] = _groups(conn, "book_decisions", "status")
        out["paper_status"] = _groups(conn, "paper_trades", "status")
        out["observed_tokens"] = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT condition_id, token_id "
            "FROM signal_observations)"
        ).fetchone()[0]
        out["ready_tokens"] = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT condition_id, token_id "
            "FROM ready_opportunities)"
        ).fetchone()[0]
        out["diamond_risk_pass_tokens"] = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT s.condition_id, s.token_id "
            "FROM signal_observations s JOIN risk_decisions r "
            "ON r.signal_id=s.signal_id "
            "WHERE s.diamond=1 AND s.direction='BUY' AND r.risk_ok=1)"
        ).fetchone()[0]
        out["gross_observations"] = out["counts"]["signal_observations"]
        # This is only a diagnostic baseline, not a counterfactual backtest.
        # Read the already-persisted aggregate signal snapshot, not future data.
        flow_candidates = set()
        malformed = 0
        for signal_id, market, token, obs_at, raw in conn.execute(
            "SELECT signal_id, condition_id, token_id, observed_at, payload_json "
            "FROM signal_observations"
        ):
            try:
                row = json.loads(raw)
                if not isinstance(row, dict) or not _utc_valid(obs_at):
                    malformed += 1
                    continue
                if (row.get("direction") == "BUY"
                    and float(row.get("trades_5m") or 0) >= 8
                    and float(row.get("volume_5m") or 0) >= 500
                    and float(row.get("net_flow_5m") or 0) > 0
                    and float(row.get("net_flow_15m") or 0) > 0):
                    flow_candidates.add((market, token))
            except (ValueError, TypeError, OverflowError):
                malformed += 1
        out["diagnostic_simple_flow_buy_tokens_in_selected_population"] = len(
            flow_candidates)
        out["malformed_signal_snapshots"] = malformed
        out["unselected_population_available"] = False
        out["baseline_limitation"] = (
            "Learning filters out LOW-only generations. The full at-risk "
            "universe is missing; these are not valid matched controls.")
        out["horizon_limitation"] = (
            "No systematic historical two-sided executable CLOB books for "
            "5/15/60-minute signal and control cohorts in this schema.")
        out["manual_selection_limitation"] = (
            "Paper opens are user-selected and are not an unbiased measure "
            "of all eligible Diamond opportunities.")
        out["paper_realized_usd_observed"] = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl_usd),0) FROM paper_exits "
            "WHERE realized_pnl_usd IS NOT NULL"
        ).fetchone()[0]
        out["status"] = "OBSERVATIONAL_FUNNEL_ONLY"
        return out
    finally:
        conn.close()


def collector_audit(path: Path):
    out = {"exists": path.is_file(), "status": "MISSING"}
    conn = _open_readonly(path)
    if conn is None:
        out["reason"] = "No collector.sqlite3 at this path"
        return out
    try:
        expected = {"checkpoint", "blocks", "trades"}
        absent = sorted(expected - _tables(conn))
        if absent:
            out.update(status="INCOMPLETE_SCHEMA", missing_tables=absent)
            return out
        out["blocks"] = _count(conn, "blocks")
        out["trades"] = _count(conn, "trades")
        cp = conn.execute(
            "SELECT chain_id, block_number FROM checkpoint "
            "ORDER BY chain_id LIMIT 1"
        ).fetchone()
        out["checkpoint"] = (
            {"chain_id": cp[0], "block_number": cp[1]} if cp else None)
        bounds = conn.execute(
            "SELECT MIN(block_timestamp), MAX(block_timestamp) FROM blocks"
        ).fetchone()
        out["block_time_bounds_unix_s"] = list(bounds)
        out["status"] = "OFFLINE_DB_COUNTS_ONLY"
        out["chain_tip_lag"] = "NOT_MEASURABLE_WITHOUT_LIVE_HEAD"
        return out
    finally:
        conn.close()


def run(data_dir: Path):
    data_dir = Path(data_dir)
    return {
        "audit": "GENERAL1-QUANT-VALIDATION-1",
        "mode": "READ_ONLY",
        "data_dir": str(data_dir),
        "learning": learning_audit(data_dir / "learning.sqlite3"),
        "collector": collector_audit(data_dir / "collector.sqlite3"),
        "comparative_returns_5_15_60m": "NOT_YET_MEASURABLE",
        "settlement_calibration": "NOT_YET_MEASURABLE",
        "strategy_edge": "INCONCLUSIVE",
        "next_required_data": [
            "Complete timestamped candidate AND non-candidate at-risk population",
            "As-of source timestamps and separate decision/receipt timestamps",
            "Historical two-sided executable order books at decision and horizons",
            "Market categories, outcome identities, resolution outcomes, costs",
            "Predeclared cohort rules and out-of-sample, market-clustered analysis",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    try:
        result = run(args.data_dir)
    except (sqlite3.Error, OSError) as exc:
        parser.exit(2, f"READ_ONLY_AUDIT_ERROR: {type(exc).__name__}: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
