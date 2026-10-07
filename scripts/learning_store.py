"""Crash-safe SQLite foundation for PolymarketResearch learning data.

The store records research observations only. It has no authority over Diamond,
Risk, Focus, Book, Paper lifecycle, Telegram, or real-money execution.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from scripts.learning_contract import LEARNING_SCHEMA_VERSION, validate_strategy_versions

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_FILE = BASE_DIR / "data" / "learning.sqlite3"


class LearningStoreError(RuntimeError):
    pass


class LearningDataConflict(LearningStoreError):
    """An immutable identity was reused with different contents."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _version_identity(versions: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "pipeline_version": str(versions["pipeline_version"]),
        "diamond_version": str(versions["diamond_version"]),
        "risk_version": str(versions["risk_version"]),
        "focus_version": str(versions["focus_version"]),
        "book_version": str(versions["book_version"]),
        "paper_version": str(versions["paper_version"]),
        "learning_schema_version": int(versions["learning_schema_version"]),
        "git_commit_sha": str(versions["git_commit_sha"]),
        "config_fingerprint": str(versions["config_fingerprint"]),
    }


def _version_id(versions: Mapping[str, Any]) -> str:
    canonical = _json(_version_identity(versions)).encode("utf-8")
    return "SV-" + hashlib.sha256(canonical).hexdigest()[:24]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS learning_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    version_id TEXT PRIMARY KEY,
    pipeline_version TEXT NOT NULL,
    diamond_version TEXT NOT NULL,
    risk_version TEXT NOT NULL,
    focus_version TEXT NOT NULL,
    book_version TEXT NOT NULL,
    paper_version TEXT NOT NULL,
    learning_schema_version INTEGER NOT NULL,
    git_commit_sha TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_observations (
    signal_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source_generation_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    direction TEXT NOT NULL,
    classification TEXT NOT NULL,
    diamond INTEGER NOT NULL CHECK (diamond IN (0,1)),
    signal_price REAL,
    signal_quality REAL,
    verification REAL,
    entry_quality REAL,
    resolution_reliability REAL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (version_id) REFERENCES strategy_versions(version_id),
    UNIQUE (source_generation_id, token_id, outcome)
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    risk_decision_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    risk_ok INTEGER NOT NULL CHECK (risk_ok IN (0,1)),
    decision TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signal_observations(signal_id),
    UNIQUE (signal_id)
);

CREATE TABLE IF NOT EXISTS focus_events (
    focus_event_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_at TEXT NOT NULL,
    source_generation_id TEXT,
    source_evidence_id TEXT,
    condition_id TEXT,
    token_id TEXT,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (version_id) REFERENCES strategy_versions(version_id)
);

CREATE TABLE IF NOT EXISTS ready_opportunities (
    ready_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    focus_event_id TEXT NOT NULL UNIQUE,
    source_generation_id TEXT NOT NULL,
    source_evidence_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    ready_at TEXT NOT NULL,
    selection_status TEXT NOT NULL,
    selected_paper_id TEXT,
    ended_at TEXT,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (version_id) REFERENCES strategy_versions(version_id),
    FOREIGN KEY (focus_event_id) REFERENCES focus_events(focus_event_id)
);

CREATE TABLE IF NOT EXISTS book_decisions (
    book_decision_id TEXT PRIMARY KEY,
    ready_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    book_ok INTEGER NOT NULL CHECK (book_ok IN (0,1)),
    reason_codes_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (ready_id) REFERENCES ready_opportunities(ready_id)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    paper_id TEXT PRIMARY KEY,
    ready_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    open_request_id TEXT NOT NULL UNIQUE,
    opened_at TEXT NOT NULL,
    status TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    direction TEXT NOT NULL,
    investment_usd REAL NOT NULL,
    immutable_json TEXT NOT NULL,
    mutable_json TEXT NOT NULL,
    FOREIGN KEY (ready_id) REFERENCES ready_opportunities(ready_id),
    FOREIGN KEY (version_id) REFERENCES strategy_versions(version_id)
);

CREATE TABLE IF NOT EXISTS paper_marks (
    mark_id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    mark_status TEXT NOT NULL,
    current_value_usd REAL,
    pnl_usd REAL,
    return_pct REAL,
    book_hash TEXT,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (paper_id) REFERENCES paper_trades(paper_id)
);

CREATE TABLE IF NOT EXISTS paper_exits (
    exit_id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL UNIQUE,
    exit_type TEXT NOT NULL,
    exit_at TEXT NOT NULL,
    close_request_id TEXT,
    realized_pnl_usd REAL,
    realized_return_pct REAL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (paper_id) REFERENCES paper_trades(paper_id)
);

CREATE TABLE IF NOT EXISTS market_outcomes (
    outcome_id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    settled_at TEXT,
    resolved_outcome TEXT,
    payout_per_token REAL,
    forecast_evaluable INTEGER NOT NULL CHECK (forecast_evaluable IN (0,1)),
    payload_json TEXT NOT NULL,
    UNIQUE (condition_id, token_id)
);

CREATE INDEX IF NOT EXISTS idx_signal_market
    ON signal_observations(condition_id, token_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_signal_version
    ON signal_observations(version_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_focus_market
    ON focus_events(condition_id, token_id, event_at);
CREATE INDEX IF NOT EXISTS idx_ready_selection
    ON ready_opportunities(selection_status, ready_at);
CREATE INDEX IF NOT EXISTS idx_book_ready
    ON book_decisions(ready_id, checked_at);
CREATE INDEX IF NOT EXISTS idx_paper_opened
    ON paper_trades(opened_at, status);
CREATE INDEX IF NOT EXISTS idx_marks_paper
    ON paper_marks(paper_id, observed_at);
"""


class LearningStore:
    def __init__(self, path=DEFAULT_DB_FILE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _existing_user_tables(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {str(row["name"]) for row in rows}

    def _validate_existing_schema_marker(self, tables):
        if not tables:
            return False
        if "learning_meta" not in tables:
            raise LearningStoreError(
                "existing learning database is unversioned; refusing automatic mutation"
            )
        try:
            row = self.conn.execute(
                "SELECT value FROM learning_meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise LearningStoreError(
                f"learning database schema marker unreadable: {exc}"
            ) from exc
        if row is None:
            raise LearningStoreError("learning database schema version missing")
        try:
            version = int(row["value"])
        except (TypeError, ValueError) as exc:
            raise LearningStoreError("learning database schema version invalid") from exc
        if version != LEARNING_SCHEMA_VERSION:
            raise LearningStoreError("learning database schema mismatch")
        return True

    def _initialize(self):
        try:
            tables = self._existing_user_tables()
            has_schema_marker = self._validate_existing_schema_marker(tables)

            with self.conn:
                self.conn.executescript(SCHEMA_SQL)
                if not has_schema_marker:
                    self.conn.execute(
                        "INSERT INTO learning_meta(key,value) VALUES('schema_version',?)",
                        (str(LEARNING_SCHEMA_VERSION),),
                    )
        except LearningStoreError:
            raise
        except sqlite3.Error as exc:
            raise LearningStoreError(f"learning database init failed: {exc}") from exc

    def schema_version(self):
        row = self.conn.execute(
            "SELECT value FROM learning_meta WHERE key='schema_version'"
        ).fetchone()
        return int(row["value"])

    def _insert_immutable(self, table, key_column, key_value, values):
        columns = list(values)
        placeholders = ",".join("?" for _ in columns)
        sql = (
            f"INSERT INTO {table} ({','.join(columns)}) "
            f"VALUES ({placeholders})"
        )
        try:
            with self.conn:
                self.conn.execute(sql, [values[column] for column in columns])
            return True
        except sqlite3.IntegrityError as exc:
            existing = self.conn.execute(
                f"SELECT * FROM {table} WHERE {key_column}=?",
                (key_value,),
            ).fetchone()
            if existing is None:
                raise LearningStoreError(str(exc)) from exc
            for column, expected in values.items():
                if existing[column] != expected:
                    raise LearningDataConflict(
                        f"{table}.{key_column}={key_value} conflicts on {column}"
                    ) from exc
            return False

    def register_strategy_version(self, versions: Mapping[str, Any]) -> str:
        validate_strategy_versions(versions)
        payload = dict(versions)
        identity = _version_identity(payload)
        version_id = _version_id(identity)
        payload_json = _json(identity)

        existing = self.conn.execute(
            "SELECT payload_json FROM strategy_versions WHERE version_id=?",
            (version_id,),
        ).fetchone()
        if existing is not None:
            if existing["payload_json"] != payload_json:
                raise LearningDataConflict(
                    f"strategy_versions.version_id={version_id} conflicts"
                )
            return version_id

        values = {
            "version_id": version_id,
            **identity,
            "created_at": str(payload.get("created_at") or _utc_now()),
            "payload_json": payload_json,
        }
        self._insert_immutable(
            "strategy_versions", "version_id", version_id, values
        )
        return version_id

    def insert_signal_observation(self, signal_id: str, version_id: str, row: Mapping[str, Any]):
        if not str(signal_id or "").strip():
            raise LearningStoreError("signal_id is required")
        if not isinstance(row, Mapping):
            raise LearningStoreError("signal observation must be an object")
        values = {
            "signal_id": str(signal_id),
            "version_id": str(version_id),
            "observed_at": str(row["observed_at"]),
            "source_generation_id": str(row["source_generation_id"]),
            "condition_id": str(row["condition_id"]),
            "token_id": str(row["token_id"]),
            "outcome": str(row["outcome"]),
            "direction": str(row["direction"]),
            "classification": str(row["classification"]),
            "diamond": 1 if row.get("diamond") is True else 0,
            "signal_price": row.get("signal_price"),
            "signal_quality": row.get("signal_quality"),
            "verification": row.get("verification"),
            "entry_quality": row.get("entry_quality"),
            "resolution_reliability": row.get("resolution_reliability"),
            "payload_json": _json(dict(row)),
        }
        return self._insert_immutable(
            "signal_observations", "signal_id", str(signal_id), values
        )

    def insert_focus_event(self, focus_event_id: str, version_id: str, row: Mapping[str, Any]):
        values = {
            "focus_event_id": str(focus_event_id),
            "version_id": str(version_id),
            "event_type": str(row["event_type"]),
            "event_at": str(row["event_at"]),
            "source_generation_id": row.get("source_generation_id"),
            "source_evidence_id": row.get("source_evidence_id"),
            "condition_id": row.get("condition_id"),
            "token_id": row.get("token_id"),
            "payload_json": _json(dict(row)),
        }
        return self._insert_immutable(
            "focus_events", "focus_event_id", str(focus_event_id), values
        )

    def fetch_one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()
