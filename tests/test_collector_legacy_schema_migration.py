import json
import sqlite3

import pytest

from collector_storage_v4.storage import (
    CollectorStore,
    STORAGE_SCHEMA_VERSION,
    StorageError,
)


def h(n):
    return "0x" + f"{n:064x}"


def create_legacy_db(path, *, include_block_history=True, conflicting_timestamp=False):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE checkpoint (
            chain_id INTEGER PRIMARY KEY,
            block_number INTEGER NOT NULL,
            block_hash TEXT NOT NULL
        );

        CREATE TABLE blocks (
            chain_id INTEGER NOT NULL,
            block_number INTEGER NOT NULL,
            block_hash TEXT NOT NULL,
            parent_hash TEXT NOT NULL,
            block_timestamp INTEGER NOT NULL,
            PRIMARY KEY (chain_id, block_number),
            UNIQUE (chain_id, block_hash)
        );

        CREATE TABLE trades (
            chain_id INTEGER NOT NULL,
            transaction_hash TEXT NOT NULL,
            log_index INTEGER NOT NULL,
            block_number INTEGER NOT NULL,
            block_timestamp INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (chain_id, transaction_hash, log_index)
        );
        """
    )

    conn.execute(
        "INSERT INTO checkpoint(chain_id,block_number,block_hash) VALUES(137,101,?)",
        (h(101),),
    )

    if include_block_history:
        conn.execute(
            "INSERT INTO blocks(chain_id,block_number,block_hash,parent_hash,block_timestamp) "
            "VALUES(137,101,?,?,?)",
            (h(101), h(100), 1700000000),
        )

    payload = {
        "collector_version": 3,
        "block": 101,
        "transaction_hash": h(1001),
        "log_index": 7,
        "condition_id": h(999),
        "token_id": "123",
        "outcome": "Yes",
        "side_label": "BUY",
        "trade_usd": 25.0,
    }
    conn.execute(
        "INSERT INTO trades("
        "chain_id,transaction_hash,log_index,block_number,block_timestamp,payload_json"
        ") VALUES(137,?,?,?,?,?)",
        (
            h(1001),
            7,
            101,
            1700000001 if conflicting_timestamp else 1700000000,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        ),
    )
    conn.commit()
    conn.close()


def test_legacy_trades_schema_migrates_block_hash_and_payload(tmp_path):
    db = tmp_path / "collector.sqlite3"
    create_legacy_db(db)

    with CollectorStore(db) as store:
        schema = {
            row["name"]: row
            for row in store.conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        assert "block_hash" in schema
        assert schema["block_hash"]["notnull"] == 1
        assert schema["block_timestamp"]["notnull"] == 1
        assert schema["payload_json"]["notnull"] == 1

        foreign_keys = store.conn.execute(
            "PRAGMA foreign_key_list(trades)"
        ).fetchall()
        assert any(
            row["table"] == "blocks"
            and row["from"] == "block_number"
            and row["to"] == "block_number"
            for row in foreign_keys
        )

        row = store.conn.execute(
            "SELECT block_hash, block_timestamp, payload_json FROM trades"
        ).fetchone()
        assert row["block_hash"] == h(101)

        payload = json.loads(row["payload_json"])
        assert payload["block_hash"] == h(101)
        assert payload["block_timestamp"] == 1700000000

        version = store.conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == STORAGE_SCHEMA_VERSION


def test_migrated_legacy_row_is_published_with_canonical_block_evidence(tmp_path):
    db = tmp_path / "collector.sqlite3"
    create_legacy_db(db)

    with CollectorStore(db) as store:
        rows = store.recent_trades(seconds=0)

    assert len(rows) == 1
    assert rows[0]["block_hash"] == h(101)
    assert rows[0]["block_timestamp"] == 1700000000


def test_migration_fails_closed_when_trade_has_no_canonical_block_header(tmp_path):
    db = tmp_path / "collector.sqlite3"
    create_legacy_db(db, include_block_history=False)

    with pytest.raises(
        StorageError,
        match="trade rows are missing canonical block history",
    ):
        CollectorStore(db)

    conn = sqlite3.connect(db)
    try:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        assert "block_hash" not in columns
    finally:
        conn.close()


def test_migration_fails_closed_on_timestamp_conflict(tmp_path):
    db = tmp_path / "collector.sqlite3"
    create_legacy_db(db, conflicting_timestamp=True)

    with pytest.raises(
        StorageError,
        match="trade timestamps conflict with canonical block history",
    ):
        CollectorStore(db)

    conn = sqlite3.connect(db)
    try:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        assert "block_hash" not in columns
    finally:
        conn.close()


def test_current_schema_is_noop_and_sets_schema_version(tmp_path):
    db = tmp_path / "collector.sqlite3"

    with CollectorStore(db) as store:
        first_columns = {
            row["name"]
            for row in store.conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        assert "block_hash" in first_columns
        assert (
            store.conn.execute("PRAGMA user_version").fetchone()[0]
            == STORAGE_SCHEMA_VERSION
        )

    with CollectorStore(db) as store:
        second_columns = {
            row["name"]
            for row in store.conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        assert second_columns == first_columns
        assert (
            store.conn.execute("PRAGMA user_version").fetchone()[0]
            == STORAGE_SCHEMA_VERSION
        )



def test_unknown_legacy_trade_columns_fail_closed_instead_of_being_discarded(tmp_path):
    db = tmp_path / "collector.sqlite3"
    create_legacy_db(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN legacy_extra TEXT")
        conn.execute("UPDATE trades SET legacy_extra='preserve-me'")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(
        StorageError,
        match="Unsupported collector trades schema",
    ):
        CollectorStore(db)

    conn = sqlite3.connect(db)
    try:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        assert "legacy_extra" in columns
        assert conn.execute(
            "SELECT legacy_extra FROM trades"
        ).fetchone()[0] == "preserve-me"
    finally:
        conn.close()
