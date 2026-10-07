import hashlib
import json
import sqlite3
from pathlib import Path

from scripts.collector_db_preflight import audit_database, main


def h(n):
    return "0x" + f"{n:064x}"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def create_db(
    path,
    *,
    current=False,
    include_block=True,
    trade_timestamp=1700000000,
    stored_block_hash=None,
    payload=None,
    checkpoint_block=101,
    checkpoint_hash=None,
    extra_trade_column=False,
):
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
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
            """
        )

        if current:
            conn.executescript(
                """
                CREATE TABLE trades (
                    chain_id INTEGER NOT NULL,
                    transaction_hash TEXT NOT NULL,
                    log_index INTEGER NOT NULL,
                    block_number INTEGER NOT NULL,
                    block_hash TEXT NOT NULL,
                    block_timestamp INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (chain_id, transaction_hash, log_index),
                    FOREIGN KEY (chain_id, block_number)
                        REFERENCES blocks(chain_id, block_number)
                );
                """
            )
        else:
            conn.executescript(
                """
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

        if extra_trade_column:
            conn.execute("ALTER TABLE trades ADD COLUMN legacy_extra TEXT")

        conn.execute(
            "INSERT INTO checkpoint(chain_id,block_number,block_hash) VALUES(137,?,?)",
            (checkpoint_block, checkpoint_hash or h(checkpoint_block)),
        )

        if include_block:
            conn.execute(
                "INSERT INTO blocks(chain_id,block_number,block_hash,parent_hash,block_timestamp) "
                "VALUES(137,101,?,?,?)",
                (h(101), h(100), 1700000000),
            )

        if payload is None:
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
            if current:
                payload["block_hash"] = h(101)
                payload["block_timestamp"] = 1700000000

        fields = (
            "chain_id,transaction_hash,log_index,block_number,"
            + ("block_hash," if current else "")
            + "block_timestamp,payload_json"
        )
        placeholders = "?,?,?,?,?,?,?" if current else "?,?,?,?,?,?"
        values = [
            137,
            h(1001),
            7,
            101,
        ]
        if current:
            values.append(stored_block_hash or h(101))
        values.extend([trade_timestamp, json.dumps(payload, sort_keys=True)])

        conn.execute(
            f"INSERT INTO trades({fields}) VALUES({placeholders})",
            tuple(values),
        )
        conn.commit()
    finally:
        conn.close()


def test_known_legacy_database_is_migration_safe_and_byte_identical(tmp_path):
    db = tmp_path / "collector.sqlite3"
    create_db(db)
    before = sha256(db)

    report = audit_database(db)

    assert report["read_only"] is True
    assert report["schema_kind"] == "LEGACY_MISSING_BLOCK_HASH"
    assert report["result"] == "MIGRATION_SAFE"
    assert report["blockers"] == []
    assert report["counts"]["trades"] == 1
    assert report["counts"]["blocks"] == 1
    assert sha256(db) == before
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()


def test_current_database_is_current_schema_ok(tmp_path):
    db = tmp_path / "collector.sqlite3"
    create_db(db, current=True)

    report = audit_database(db)

    assert report["schema_kind"] == "CURRENT"
    assert report["result"] == "CURRENT_SCHEMA_OK"
    assert report["blockers"] == []


def test_missing_database_returns_error_and_is_not_created(tmp_path, capsys):
    db = tmp_path / "missing.sqlite3"

    rc = main(["--db", str(db)])

    assert rc == 3
    assert not db.exists()
    assert "PREFLIGHT ERROR" in capsys.readouterr().out


def test_trade_without_canonical_block_is_blocked(tmp_path):
    db = tmp_path / "collector.sqlite3"
    create_db(db, include_block=False)

    report = audit_database(db)

    assert report["result"] == "MIGRATION_BLOCKED"
    assert "TRADES_WITHOUT_CANONICAL_BLOCK:1" in report["blockers"]


def test_trade_timestamp_conflict_is_blocked(tmp_path):
    db = tmp_path / "collector.sqlite3"
    create_db(db, trade_timestamp=1700000001)

    report = audit_database(db)

    assert report["result"] == "MIGRATION_BLOCKED"
    assert "TRADE_TIMESTAMP_CONFLICTS:1" in report["blockers"]


def test_current_trade_block_hash_conflict_is_blocked(tmp_path):
    db = tmp_path / "collector.sqlite3"
    create_db(db, current=True, stored_block_hash=h(555))

    report = audit_database(db)

    assert report["result"] == "MIGRATION_BLOCKED"
    assert "TRADE_BLOCK_HASH_CONFLICTS:1" in report["blockers"]


def test_invalid_payload_json_is_blocked(tmp_path):
    db = tmp_path / "collector.sqlite3"
    create_db(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE trades SET payload_json='{broken'")
        conn.commit()
    finally:
        conn.close()

    report = audit_database(db)

    assert report["result"] == "MIGRATION_BLOCKED"
    assert "PAYLOAD_INVALID_JSON:1" in report["blockers"]


def test_payload_identity_mismatch_is_blocked(tmp_path):
    db = tmp_path / "collector.sqlite3"
    payload = {
        "block": 999,
        "transaction_hash": h(1001),
        "log_index": 7,
    }
    create_db(db, payload=payload)

    report = audit_database(db)

    assert report["result"] == "MIGRATION_BLOCKED"
    assert "PAYLOAD_IDENTITY_MISMATCH:1" in report["blockers"]


def test_missing_payload_identity_is_blocked(tmp_path):
    db = tmp_path / "collector.sqlite3"
    payload = {"condition_id": h(999)}
    create_db(db, payload=payload)

    report = audit_database(db)

    assert report["result"] == "MIGRATION_BLOCKED"
    assert "PAYLOAD_MISSING_IDENTITY:1" in report["blockers"]


def test_unknown_legacy_trade_column_is_blocked_without_mutation(tmp_path):
    db = tmp_path / "collector.sqlite3"
    create_db(db, extra_trade_column=True)
    before = sha256(db)

    report = audit_database(db)

    assert report["result"] == "MIGRATION_BLOCKED"
    assert any(x.startswith("UNSUPPORTED_TRADES_SCHEMA") for x in report["blockers"])
    assert sha256(db) == before


def test_checkpoint_tip_mismatch_is_blocked(tmp_path):
    db = tmp_path / "collector.sqlite3"
    create_db(db, checkpoint_block=102, checkpoint_hash=h(102))

    report = audit_database(db)

    assert report["result"] == "MIGRATION_BLOCKED"
    assert any(x.startswith("CHECKPOINT_NOT_AT_BLOCK_TIP") for x in report["blockers"])


def test_parent_hash_break_is_blocked(tmp_path):
    db = tmp_path / "collector.sqlite3"
    create_db(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO blocks(chain_id,block_number,block_hash,parent_hash,block_timestamp) "
            "VALUES(137,102,?,?,?)",
            (h(102), h(999), 1700000001),
        )
        conn.execute(
            "UPDATE checkpoint SET block_number=102, block_hash=? WHERE chain_id=137",
            (h(102),),
        )
        conn.commit()
    finally:
        conn.close()

    report = audit_database(db)

    assert report["result"] == "MIGRATION_BLOCKED"
    assert "BLOCK_PARENT_HASH_MISMATCHES:1" in report["blockers"]


def test_cli_success_exit_code_for_safe_legacy_db(tmp_path, capsys):
    db = tmp_path / "collector.sqlite3"
    create_db(db)

    rc = main(["--db", str(db)])

    output = capsys.readouterr().out
    assert rc == 0
    assert "RESULT: MIGRATION_SAFE" in output
    assert "READ ONLY" in output
