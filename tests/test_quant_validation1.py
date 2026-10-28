"""Deterministic offline contract tests; fixtures are not market evidence."""
import json
import sqlite3

from scripts.quant_validation1 import collector_audit, learning_audit, run


def create_learning(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE signal_observations (
              signal_id TEXT, condition_id TEXT, token_id TEXT,
              observed_at TEXT, payload_json TEXT, classification TEXT,
              diamond INTEGER, direction TEXT);
            CREATE TABLE risk_decisions (
              signal_id TEXT, decision TEXT, risk_ok INTEGER);
            CREATE TABLE focus_events (event_type TEXT);
            CREATE TABLE ready_opportunities (
              condition_id TEXT, token_id TEXT, selection_status TEXT);
            CREATE TABLE book_decisions (status TEXT);
            CREATE TABLE paper_trades (status TEXT);
            CREATE TABLE paper_exits (realized_pnl_usd REAL);
            CREATE TABLE paper_marks (mark_status TEXT);
        """)


def test_missing_dbs_do_not_get_created(tmp_path):
    out = run(tmp_path)
    assert out["learning"]["status"] == "MISSING"
    assert out["collector"]["status"] == "MISSING"
    assert not list(tmp_path.iterdir())
    assert out["strategy_edge"] == "INCONCLUSIVE"


def test_readonly_counts_never_claim_alpha(tmp_path):
    dbpath = tmp_path / "learning.sqlite3"
    create_learning(dbpath)
    payload = {
        "direction": "BUY", "trades_5m": 10, "volume_5m": 750,
        "net_flow_5m": 400, "net_flow_15m": 100
    }
    with sqlite3.connect(dbpath) as db:
        db.execute(
            "INSERT INTO signal_observations VALUES (?,?,?,?,?,?,?,?)",
            ("s1", "c1", "t1", "2026-10-28T12:00:00+00:00",
             json.dumps(payload), "DIAMOND", 1, "BUY"))
        db.execute("INSERT INTO risk_decisions VALUES (?,?,?)",
                   ("s1", "PASS", 1))
        db.execute("INSERT INTO focus_events VALUES (?)", ("READY",))
        db.execute("INSERT INTO ready_opportunities VALUES (?,?,?)",
                   ("c1", "t1", "NOT_SELECTED_BEFORE_READY_ENDED"))
        db.execute("INSERT INTO paper_exits VALUES (?)", (100.0,))
    size_before = dbpath.stat().st_size
    result = learning_audit(dbpath)
    assert dbpath.stat().st_size == size_before
    assert result["status"] == "OBSERVATIONAL_FUNNEL_ONLY"
    assert result["diamond_risk_pass_tokens"] == 1
    assert result["ready_tokens"] == 1
    assert result["diagnostic_simple_flow_buy_tokens_in_selected_population"] == 1
    assert result["unselected_population_available"] is False
    assert result["paper_realized_usd_observed"] == 100.0
    assert result["quantitative_edge"] == "NOT_YET_MEASURABLE"


def test_invalid_timestamp_is_not_counted_as_momentum(tmp_path):
    p = tmp_path / "learning.sqlite3"
    create_learning(p)
    with sqlite3.connect(p) as db:
        db.execute(
            "INSERT INTO signal_observations VALUES (?,?,?,?,?,?,?,?)",
            ("s2", "c2", "t2", "2026-10-28T12:00:00",
             json.dumps({"direction": "BUY", "trades_5m": 8,
                         "volume_5m": 500, "net_flow_5m": 300,
                         "net_flow_15m": 20}), "CANDIDATE", 0, "BUY"))
    result = learning_audit(p)
    assert result["malformed_signal_snapshots"] == 1
    assert result["diagnostic_simple_flow_buy_tokens_in_selected_population"] == 0


def test_collector_db_is_readonly_and_not_chain_tip_proof(tmp_path):
    p = tmp_path / "collector.sqlite3"
    with sqlite3.connect(p) as db:
        db.executescript("""
            CREATE TABLE checkpoint(chain_id INTEGER, block_number INTEGER);
            CREATE TABLE blocks(block_timestamp INTEGER);
            CREATE TABLE trades(payload_json TEXT);
            INSERT INTO checkpoint VALUES (137, 100);
            INSERT INTO blocks VALUES (123456);
            INSERT INTO trades VALUES ('{}');
        """)
    result = collector_audit(p)
    assert result["status"] == "OFFLINE_DB_COUNTS_ONLY"
    assert result["blocks"] == 1 and result["trades"] == 1
    assert result["chain_tip_lag"] == "NOT_MEASURABLE_WITHOUT_LIVE_HEAD"
