"""Read-only safety audit for the local Collector SQLite database.

This tool never creates, migrates, vacuums, checkpoints, or mutates the
database. It opens SQLite with URI mode=ro and reports whether the database is
already on the current Collector schema or is safe for the known legacy
block_hash migration.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


CHAIN_ID = 137
CURRENT_SCHEMA_VERSION = 2

CHECKPOINT_REQUIRED = {"chain_id", "block_number", "block_hash"}
BLOCKS_REQUIRED = {
    "chain_id",
    "block_number",
    "block_hash",
    "parent_hash",
    "block_timestamp",
}
CURRENT_TRADES = {
    "chain_id",
    "transaction_hash",
    "log_index",
    "block_number",
    "block_hash",
    "block_timestamp",
    "payload_json",
}
LEGACY_TRADES = CURRENT_TRADES - {"block_hash"}

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "collector.sqlite3"
MAX_SAMPLES = 5


class PreflightError(RuntimeError):
    pass


def _db_uri(path):
    return path.resolve().as_uri() + "?mode=ro"


def _table_columns(conn, table):
    return {
        str(row["name"]): dict(row)
        for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _count(conn, sql, params=()):
    return int(conn.execute(sql, params).fetchone()[0])


def _sample_rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchmany(MAX_SAMPLES)]


def _normalize_hash(value):
    return str(value or "").strip().lower()


def _int_equal(value, expected):
    if isinstance(value, bool):
        return False
    try:
        return int(value) == int(expected)
    except (TypeError, ValueError):
        return False


def _text_equal(value, expected):
    return str(value or "").strip().lower() == str(expected or "").strip().lower()


def _sidecar_info(path):
    wal = Path(str(path) + "-wal")
    shm = Path(str(path) + "-shm")
    return {
        "wal_exists": wal.exists(),
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "shm_exists": shm.exists(),
        "shm_bytes": shm.stat().st_size if shm.exists() else 0,
    }


def _validate_schema(conn, blockers, warnings):
    tables = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    for required in ("checkpoint", "blocks", "trades"):
        if required not in tables:
            blockers.append(f"MISSING_TABLE:{required}")

    if blockers:
        return "UNKNOWN", {}

    checkpoint = _table_columns(conn, "checkpoint")
    blocks = _table_columns(conn, "blocks")
    trades = _table_columns(conn, "trades")

    cp_names = set(checkpoint)
    block_names = set(blocks)
    trade_names = set(trades)

    if not CHECKPOINT_REQUIRED.issubset(cp_names):
        blockers.append("UNSUPPORTED_CHECKPOINT_SCHEMA")
    if not BLOCKS_REQUIRED.issubset(block_names):
        blockers.append("UNSUPPORTED_BLOCKS_SCHEMA")

    extra_cp = sorted(cp_names - CHECKPOINT_REQUIRED)
    extra_blocks = sorted(block_names - BLOCKS_REQUIRED)
    if extra_cp:
        warnings.append("EXTRA_CHECKPOINT_COLUMNS:" + ",".join(extra_cp))
    if extra_blocks:
        warnings.append("EXTRA_BLOCK_COLUMNS:" + ",".join(extra_blocks))

    schema_kind = "UNKNOWN"
    if trade_names == LEGACY_TRADES:
        schema_kind = "LEGACY_MISSING_BLOCK_HASH"
    elif trade_names == CURRENT_TRADES:
        schema_kind = "CURRENT"
    elif CURRENT_TRADES.issubset(trade_names):
        blockers.append("UNEXPECTED_CURRENT_TRADE_COLUMNS:" + ",".join(sorted(trade_names - CURRENT_TRADES)))
    else:
        blockers.append("UNSUPPORTED_TRADES_SCHEMA")

    if schema_kind in {"CURRENT", "LEGACY_MISSING_BLOCK_HASH"}:
        expected = CURRENT_TRADES if schema_kind == "CURRENT" else LEGACY_TRADES
        for name in expected:
            if int(trades[name].get("notnull", 0)) != 1:
                blockers.append(f"TRADE_COLUMN_NOT_NOTNULL:{name}")

        expected_pk = {"chain_id": 1, "transaction_hash": 2, "log_index": 3}
        for name, ordinal in expected_pk.items():
            if int(trades[name].get("pk", 0)) != ordinal:
                blockers.append(f"TRADE_PRIMARY_KEY_MISMATCH:{name}")

    if schema_kind == "CURRENT":
        fk_rows = conn.execute('PRAGMA foreign_key_list("trades")').fetchall()
        mappings = {(str(r["from"]), str(r["to"]), str(r["table"])) for r in fk_rows}
        needed = {
            ("chain_id", "chain_id", "blocks"),
            ("block_number", "block_number", "blocks"),
        }
        if not needed.issubset(mappings):
            blockers.append("CURRENT_TRADES_FOREIGN_KEY_MISMATCH")

    return schema_kind, {
        "tables": sorted(tables),
        "checkpoint_columns": sorted(cp_names),
        "blocks_columns": sorted(block_names),
        "trades_columns": sorted(trade_names),
    }


def _validate_chain_ids(conn, blockers):
    for table in ("checkpoint", "blocks", "trades"):
        rows = conn.execute(
            f'SELECT DISTINCT chain_id FROM "{table}" ORDER BY chain_id'
        ).fetchall()
        ids = [int(row[0]) for row in rows]
        unsupported = [value for value in ids if value != CHAIN_ID]
        if unsupported:
            blockers.append(
                f"UNSUPPORTED_CHAIN_ID:{table}:" + ",".join(str(x) for x in unsupported)
            )


def _validate_checkpoint(conn, blockers, warnings):
    cp_rows = conn.execute(
        "SELECT chain_id, block_number, block_hash FROM checkpoint WHERE chain_id=?",
        (CHAIN_ID,),
    ).fetchall()
    if len(cp_rows) > 1:
        blockers.append("MULTIPLE_POLYGON_CHECKPOINTS")
        return None
    if not cp_rows:
        warnings.append("CHECKPOINT_NOT_INITIALIZED")
        return None

    cp = dict(cp_rows[0])
    cp["block_number"] = int(cp["block_number"])
    cp["block_hash"] = _normalize_hash(cp["block_hash"])
    if not cp["block_hash"]:
        blockers.append("CHECKPOINT_BLOCK_HASH_EMPTY")

    block_count = _count(
        conn,
        "SELECT COUNT(*) FROM blocks WHERE chain_id=?",
        (CHAIN_ID,),
    )
    if block_count:
        max_row = conn.execute(
            "SELECT block_number, block_hash FROM blocks "
            "WHERE chain_id=? ORDER BY block_number DESC LIMIT 1",
            (CHAIN_ID,),
        ).fetchone()
        if int(max_row["block_number"]) != cp["block_number"]:
            blockers.append(
                f"CHECKPOINT_NOT_AT_BLOCK_TIP:{cp['block_number']}!={int(max_row['block_number'])}"
            )
        if int(max_row["block_number"]) == cp["block_number"] and (
            _normalize_hash(max_row["block_hash"]) != cp["block_hash"]
        ):
            blockers.append("CHECKPOINT_BLOCK_HASH_MISMATCH")

    return cp


def _validate_block_history(conn, blockers):
    cursor = conn.execute(
        "SELECT block_number, block_hash, parent_hash FROM blocks "
        "WHERE chain_id=? ORDER BY block_number ASC",
        (CHAIN_ID,),
    )
    previous = None
    gap_count = 0
    parent_mismatch_count = 0
    samples = []

    for row in cursor:
        number = int(row["block_number"])
        block_hash = _normalize_hash(row["block_hash"])
        parent_hash = _normalize_hash(row["parent_hash"])
        if not block_hash or not parent_hash:
            blockers.append("EMPTY_BLOCK_HASH_OR_PARENT_HASH")
            break
        if previous is not None:
            if number != previous["block_number"] + 1:
                gap_count += 1
                if len(samples) < MAX_SAMPLES:
                    samples.append(
                        {
                            "kind": "gap",
                            "previous": previous["block_number"],
                            "current": number,
                        }
                    )
            if parent_hash != previous["block_hash"]:
                parent_mismatch_count += 1
                if len(samples) < MAX_SAMPLES:
                    samples.append(
                        {
                            "kind": "parent_hash",
                            "block": number,
                            "expected": previous["block_hash"],
                            "actual": parent_hash,
                        }
                    )
        previous = {"block_number": number, "block_hash": block_hash}

    if gap_count:
        blockers.append(f"BLOCK_HISTORY_GAPS:{gap_count}")
    if parent_mismatch_count:
        blockers.append(f"BLOCK_PARENT_HASH_MISMATCHES:{parent_mismatch_count}")

    return {
        "gap_count": gap_count,
        "parent_mismatch_count": parent_mismatch_count,
        "samples": samples,
    }


def _validate_trade_anchors(conn, schema_kind, blockers):
    missing_headers = _count(
        conn,
        """
        SELECT COUNT(*)
          FROM trades AS t
          LEFT JOIN blocks AS b
            ON b.chain_id=t.chain_id
           AND b.block_number=t.block_number
         WHERE t.chain_id=?
           AND b.block_hash IS NULL
        """,
        (CHAIN_ID,),
    )
    timestamp_conflicts = _count(
        conn,
        """
        SELECT COUNT(*)
          FROM trades AS t
          JOIN blocks AS b
            ON b.chain_id=t.chain_id
           AND b.block_number=t.block_number
         WHERE t.chain_id=?
           AND t.block_timestamp != b.block_timestamp
        """,
        (CHAIN_ID,),
    )

    hash_conflicts = 0
    if schema_kind == "CURRENT":
        hash_conflicts = _count(
            conn,
            """
            SELECT COUNT(*)
              FROM trades AS t
              JOIN blocks AS b
                ON b.chain_id=t.chain_id
               AND b.block_number=t.block_number
             WHERE t.chain_id=?
               AND lower(trim(t.block_hash)) != lower(trim(b.block_hash))
            """,
            (CHAIN_ID,),
        )

    if missing_headers:
        blockers.append(f"TRADES_WITHOUT_CANONICAL_BLOCK:{missing_headers}")
    if timestamp_conflicts:
        blockers.append(f"TRADE_TIMESTAMP_CONFLICTS:{timestamp_conflicts}")
    if hash_conflicts:
        blockers.append(f"TRADE_BLOCK_HASH_CONFLICTS:{hash_conflicts}")

    return {
        "missing_headers": missing_headers,
        "timestamp_conflicts": timestamp_conflicts,
        "block_hash_conflicts": hash_conflicts,
    }


def _validate_payloads(conn, schema_kind, blockers):
    sql = """
        SELECT
            t.transaction_hash,
            t.log_index,
            t.block_number,
            t.block_timestamp,
            %s
            t.payload_json,
            b.block_hash AS canonical_block_hash,
            b.block_timestamp AS canonical_block_timestamp
        FROM trades AS t
        LEFT JOIN blocks AS b
          ON b.chain_id=t.chain_id
         AND b.block_number=t.block_number
        WHERE t.chain_id=?
    """ % ("t.block_hash AS stored_block_hash, " if schema_kind == "CURRENT" else "")

    counts = {
        "invalid_json": 0,
        "non_object_json": 0,
        "missing_identity": 0,
        "identity_mismatch": 0,
        "payload_block_hash_conflict": 0,
        "payload_timestamp_conflict": 0,
    }
    samples = []

    for row in conn.execute(sql, (CHAIN_ID,)):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            counts["invalid_json"] += 1
            if len(samples) < MAX_SAMPLES:
                samples.append(
                    {
                        "kind": "invalid_json",
                        "transaction_hash": row["transaction_hash"],
                        "log_index": row["log_index"],
                    }
                )
            continue

        if not isinstance(payload, dict):
            counts["non_object_json"] += 1
            if len(samples) < MAX_SAMPLES:
                samples.append(
                    {
                        "kind": "non_object_json",
                        "transaction_hash": row["transaction_hash"],
                        "log_index": row["log_index"],
                    }
                )
            continue

        required_identity = ("transaction_hash", "log_index", "block")
        missing = [name for name in required_identity if payload.get(name) is None]
        if missing:
            counts["missing_identity"] += 1
            if len(samples) < MAX_SAMPLES:
                samples.append(
                    {
                        "kind": "missing_identity",
                        "transaction_hash": row["transaction_hash"],
                        "log_index": row["log_index"],
                        "missing": missing,
                    }
                )
        else:
            mismatch = (
                not _text_equal(payload.get("transaction_hash"), row["transaction_hash"])
                or not _int_equal(payload.get("log_index"), row["log_index"])
                or not _int_equal(payload.get("block"), row["block_number"])
            )
            if mismatch:
                counts["identity_mismatch"] += 1
                if len(samples) < MAX_SAMPLES:
                    samples.append(
                        {
                            "kind": "identity_mismatch",
                            "transaction_hash": row["transaction_hash"],
                            "log_index": row["log_index"],
                        }
                    )

        canonical_hash = _normalize_hash(row["canonical_block_hash"])
        payload_hash = payload.get("block_hash")
        if payload_hash is not None and canonical_hash:
            if not _text_equal(payload_hash, canonical_hash):
                counts["payload_block_hash_conflict"] += 1

        payload_timestamp = payload.get("block_timestamp")
        if payload_timestamp is not None and row["canonical_block_timestamp"] is not None:
            if not _int_equal(payload_timestamp, row["canonical_block_timestamp"]):
                counts["payload_timestamp_conflict"] += 1

    for key, value in counts.items():
        if value:
            blockers.append(f"PAYLOAD_{key.upper()}:{value}")

    return {"counts": counts, "samples": samples}


def audit_database(path):
    path = Path(path)
    if not path.exists():
        raise PreflightError(f"Database does not exist: {path}")
    if not path.is_file():
        raise PreflightError(f"Database path is not a file: {path}")

    blockers = []
    warnings = []
    sidecars = _sidecar_info(path)

    try:
        conn = sqlite3.connect(_db_uri(path), uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise PreflightError(f"Cannot open database read-only: {exc}") from exc

    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")

        quick_rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
        quick_ok = quick_rows == ["ok"]
        if not quick_ok:
            blockers.append("SQLITE_QUICK_CHECK_FAILED")

        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])

        schema_kind, schema = _validate_schema(conn, blockers, warnings)

        counts = {}
        checkpoint = None
        block_history = {}
        trade_anchors = {}
        payloads = {}

        if schema_kind != "UNKNOWN" and not any(x.startswith("MISSING_TABLE:") for x in blockers):
            _validate_chain_ids(conn, blockers)
            counts = {
                "checkpoint": _count(conn, "SELECT COUNT(*) FROM checkpoint"),
                "blocks": _count(conn, "SELECT COUNT(*) FROM blocks"),
                "trades": _count(conn, "SELECT COUNT(*) FROM trades"),
            }
            checkpoint = _validate_checkpoint(conn, blockers, warnings)
            block_history = _validate_block_history(conn, blockers)
            trade_anchors = _validate_trade_anchors(conn, schema_kind, blockers)
            payloads = _validate_payloads(conn, schema_kind, blockers)

            if schema_kind == "CURRENT":
                fk_violations = [
                    dict(row) for row in conn.execute("PRAGMA foreign_key_check").fetchmany(MAX_SAMPLES)
                ]
                if fk_violations:
                    blockers.append("FOREIGN_KEY_VIOLATIONS")
            else:
                fk_violations = []
        else:
            fk_violations = []

        conn.rollback()
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise PreflightError(f"Read-only audit failed: {exc}") from exc
    finally:
        conn.close()

    if sidecars["wal_exists"]:
        warnings.append("WAL_SIDECAR_PRESENT_STOP_COLLECTOR_BEFORE_MIGRATION")

    if blockers:
        result = "MIGRATION_BLOCKED"
    elif schema_kind == "CURRENT":
        result = "CURRENT_SCHEMA_OK"
    elif schema_kind == "LEGACY_MISSING_BLOCK_HASH":
        result = "MIGRATION_SAFE"
    else:
        result = "MIGRATION_BLOCKED"

    return {
        "preflight_version": 1,
        "read_only": True,
        "database": str(path.resolve()),
        "database_bytes": path.stat().st_size,
        "sidecars": sidecars,
        "journal_mode": journal_mode,
        "user_version": user_version,
        "schema_kind": schema_kind,
        "schema": schema,
        "counts": counts,
        "checkpoint": checkpoint,
        "quick_check_ok": quick_ok,
        "block_history": block_history,
        "trade_anchors": trade_anchors,
        "payloads": payloads,
        "foreign_key_violation_samples": fk_violations,
        "warnings": sorted(set(warnings)),
        "blockers": sorted(set(blockers)),
        "result": result,
    }


def _print_human(report):
    print("=" * 72)
    print("COLLECTOR DATABASE PREFLIGHT - READ ONLY")
    print("=" * 72)
    print(f"Database       : {report['database']}")
    print(f"Size           : {report['database_bytes']} bytes")
    print(f"Schema         : {report['schema_kind']}")
    print(f"SQLite version : user_version={report['user_version']}")
    print(f"Journal mode   : {report['journal_mode']}")
    counts = report.get("counts") or {}
    if counts:
        print(
            "Rows           : "
            f"checkpoint={counts.get('checkpoint', 0)} "
            f"blocks={counts.get('blocks', 0)} "
            f"trades={counts.get('trades', 0)}"
        )
    cp = report.get("checkpoint")
    if cp:
        print(
            f"Checkpoint     : block={cp['block_number']} "
            f"hash={cp['block_hash']}"
        )
    print(f"SQLite check   : {'OK' if report['quick_check_ok'] else 'FAILED'}")

    if report["warnings"]:
        print()
        print("WARNINGS")
        for warning in report["warnings"]:
            print(f"  - {warning}")

    if report["blockers"]:
        print()
        print("BLOCKERS")
        for blocker in report["blockers"]:
            print(f"  - {blocker}")

    payload_samples = (report.get("payloads") or {}).get("samples") or []
    if payload_samples:
        print()
        print("PAYLOAD SAMPLES")
        for sample in payload_samples:
            print("  - " + json.dumps(sample, ensure_ascii=False, sort_keys=True))

    print()
    print("=" * 72)
    print(f"RESULT: {report['result']}")
    print("=" * 72)

    if report["result"] == "MIGRATION_SAFE":
        print("Known legacy schema is internally consistent and safe for the planned migration.")
    elif report["result"] == "CURRENT_SCHEMA_OK":
        print("Collector database is already on the current validated schema.")
    else:
        print("Do NOT migrate this database until the blockers above are resolved.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only preflight for data/collector.sqlite3"
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help="Collector SQLite path (default: data/collector.sqlite3)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete report as JSON",
    )
    args = parser.parse_args(argv)

    try:
        report = audit_database(args.db)
    except PreflightError as exc:
        if args.json:
            print(json.dumps({"read_only": True, "result": "PREFLIGHT_ERROR", "error": str(exc)}))
        else:
            print(f"[PREFLIGHT ERROR] {exc}")
        return 3

    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        _print_human(report)

    return 0 if report["result"] in {"MIGRATION_SAFE", "CURRENT_SCHEMA_OK"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
