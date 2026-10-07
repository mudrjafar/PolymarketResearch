import pytest

from scripts.learning_contract import LEARNING_SCHEMA_VERSION
from scripts.learning_store import (
    LearningDataConflict,
    LearningStore,
    LearningStoreError,
)

NOW = "2026-10-07T12:00:00+00:00"


def versions(**overrides):
    value = {
        "pipeline_version": "pipeline-v1",
        "diamond_version": "diamond-v3.1",
        "risk_version": "risk-v2",
        "focus_version": "focus-v3",
        "book_version": "book-v1",
        "paper_version": "paper-v1",
        "learning_schema_version": LEARNING_SCHEMA_VERSION,
        "git_commit_sha": "a" * 40,
        "config_fingerprint": "cfg-1",
    }
    value.update(overrides)
    return value


def signal_row(**overrides):
    value = {
        "observed_at": NOW,
        "source_generation_id": "GEN-1",
        "condition_id": "condition-a",
        "token_id": "token-a",
        "outcome": "Yes",
        "direction": "BUY",
        "classification": "DIAMOND",
        "diamond": True,
        "signal_price": 0.55,
        "signal_quality": 88.0,
        "verification": 90.0,
        "entry_quality": 70.0,
        "resolution_reliability": 85.0,
        "optional_unknown_feature": None,
    }
    value.update(overrides)
    return value


def test_schema_is_created_with_foreign_keys_and_wal(tmp_path):
    with LearningStore(tmp_path / "learning.sqlite3") as store:
        assert store.schema_version() == LEARNING_SCHEMA_VERSION
        assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {
            row[0]
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "strategy_versions",
            "signal_observations",
            "risk_decisions",
            "focus_events",
            "ready_opportunities",
            "book_decisions",
            "paper_trades",
            "paper_marks",
            "paper_exits",
            "market_outcomes",
        } <= tables


def test_strategy_version_registration_is_idempotent(tmp_path):
    with LearningStore(tmp_path / "learning.sqlite3") as store:
        first = store.register_strategy_version(versions())
        second = store.register_strategy_version(versions())
        assert first == second
        count = store.conn.execute("SELECT COUNT(*) FROM strategy_versions").fetchone()[0]
        assert count == 1


def test_different_git_sha_creates_different_strategy_version(tmp_path):
    with LearningStore(tmp_path / "learning.sqlite3") as store:
        first = store.register_strategy_version(versions())
        second = store.register_strategy_version(versions(git_commit_sha="b" * 40))
        assert first != second


def test_signal_insert_is_idempotent_but_conflict_fails_closed(tmp_path):
    with LearningStore(tmp_path / "learning.sqlite3") as store:
        version_id = store.register_strategy_version(versions())
        assert store.insert_signal_observation("SIG-1", version_id, signal_row()) is True
        assert store.insert_signal_observation("SIG-1", version_id, signal_row()) is False
        with pytest.raises(LearningDataConflict):
            store.insert_signal_observation(
                "SIG-1", version_id, signal_row(signal_price=0.60)
            )


def test_unknown_payload_value_is_preserved_as_json_null(tmp_path):
    with LearningStore(tmp_path / "learning.sqlite3") as store:
        version_id = store.register_strategy_version(versions())
        store.insert_signal_observation("SIG-1", version_id, signal_row())
        row = store.fetch_one(
            "SELECT payload_json FROM signal_observations WHERE signal_id=?",
            ("SIG-1",),
        )
        assert '"optional_unknown_feature":null' in row["payload_json"]
        assert '"optional_unknown_feature":0' not in row["payload_json"]


def test_foreign_key_prevents_orphan_signal(tmp_path):
    with LearningStore(tmp_path / "learning.sqlite3") as store:
        with pytest.raises(LearningStoreError, match="FOREIGN KEY"):
            store.insert_signal_observation("SIG-1", "SV-missing", signal_row())


def test_schema_mismatch_fails_before_creating_current_tables(tmp_path):
    import sqlite3

    db = tmp_path / "learning.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE learning_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO learning_meta(key,value) VALUES('schema_version','999')"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(LearningStoreError, match="schema mismatch"):
        LearningStore(db)

    conn = sqlite3.connect(db)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert tables == {"learning_meta"}
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal"
    finally:
        conn.close()


def test_unversioned_existing_database_fails_without_mutation(tmp_path):
    import sqlite3

    db = tmp_path / "learning.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE legacy_data (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO legacy_data(value) VALUES('keep-me')")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(LearningStoreError, match="unversioned"):
        LearningStore(db)

    conn = sqlite3.connect(db)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert tables == {"legacy_data"}
        assert conn.execute("SELECT value FROM legacy_data").fetchone()[0] == "keep-me"
    finally:
        conn.close()
