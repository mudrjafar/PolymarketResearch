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

from scripts.learning_contract import (
    LEARNING_SCHEMA_VERSION,
    SELECTION_NOT_SELECTED,
    SELECTION_PENDING,
    SELECTION_SELECTED,
    SELECTION_STATUSES,
    validate_strategy_versions,
)

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

CREATE TABLE IF NOT EXISTS generation_ingestions (
    generation_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    source_generated_at TEXT NOT NULL,
    risk_generated_at TEXT NOT NULL,
    risk_markets_checked INTEGER NOT NULL,
    risk_passed INTEGER NOT NULL,
    signals_ingested INTEGER NOT NULL,
    ingested_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (version_id) REFERENCES strategy_versions(version_id)
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

CREATE INDEX IF NOT EXISTS idx_generation_version
    ON generation_ingestions(version_id, source_generated_at);
CREATE INDEX IF NOT EXISTS idx_signal_market
    ON signal_observations(condition_id, token_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_signal_version
    ON signal_observations(version_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_risk_decision
    ON risk_decisions(decision, checked_at);
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

    def _initialize(self):
        try:
            with self.conn:
                self.conn.executescript(SCHEMA_SQL)
                current = self.conn.execute(
                    "SELECT value FROM learning_meta WHERE key='schema_version'"
                ).fetchone()
                if current is None:
                    self.conn.execute(
                        "INSERT INTO learning_meta(key,value) VALUES('schema_version',?)",
                        (str(LEARNING_SCHEMA_VERSION),),
                    )
                elif int(current["value"]) != LEARNING_SCHEMA_VERSION:
                    raise LearningStoreError("learning database schema mismatch")
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

    def generation_ingested(self, generation_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM generation_ingestions WHERE generation_id=?",
            (str(generation_id),),
        ).fetchone()
        return row is not None

    def ingested_generation_ids(self) -> set[str]:
        return {
            str(row["generation_id"])
            for row in self.conn.execute(
                "SELECT generation_id FROM generation_ingestions"
            )
        }

    def insert_generation_ingestion(
        self,
        generation_id: str,
        version_id: str,
        row: Mapping[str, Any],
    ):
        values = {
            "generation_id": str(generation_id),
            "version_id": str(version_id),
            "source_generated_at": str(row["source_generated_at"]),
            "risk_generated_at": str(row["risk_generated_at"]),
            "risk_markets_checked": int(row["risk_markets_checked"]),
            "risk_passed": int(row["risk_passed"]),
            "signals_ingested": int(row["signals_ingested"]),
            "ingested_at": str(row.get("ingested_at") or _utc_now()),
            "payload_json": _json(dict(row)),
        }
        return self._insert_immutable(
            "generation_ingestions",
            "generation_id",
            str(generation_id),
            values,
        )

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

    def insert_risk_decision(
        self,
        risk_decision_id: str,
        signal_id: str,
        row: Mapping[str, Any],
    ):
        if not isinstance(row, Mapping):
            raise LearningStoreError("risk decision must be an object")
        if not isinstance(row.get("risk_ok"), bool):
            raise LearningStoreError("risk_ok must be boolean")
        reason_codes = row.get("reason_codes")
        if not isinstance(reason_codes, list):
            raise LearningStoreError("risk reason_codes must be a list")

        values = {
            "risk_decision_id": str(risk_decision_id),
            "signal_id": str(signal_id),
            "checked_at": str(row["checked_at"]),
            "risk_ok": 1 if row["risk_ok"] else 0,
            "decision": str(row["decision"]),
            "reason_codes_json": _json(reason_codes),
            "payload_json": _json(dict(row)),
        }
        return self._insert_immutable(
            "risk_decisions",
            "risk_decision_id",
            str(risk_decision_id),
            values,
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

    def insert_book_decision(
        self,
        book_decision_id: str,
        ready_id: str,
        row: Mapping[str, Any],
    ):
        if not isinstance(row, Mapping):
            raise LearningStoreError("Book decision must be an object")
        status = str(row.get("status") or "").strip()
        if not status:
            raise LearningStoreError("Book status is required")
        book_ok = row.get("book_ok")
        if not isinstance(book_ok, bool):
            raise LearningStoreError("Book book_ok must be boolean")
        reason_codes = row.get("reason_codes")
        if not isinstance(reason_codes, list) or not all(
            isinstance(code, str) for code in reason_codes
        ):
            raise LearningStoreError("Book reason_codes must be a string list")
        checked_at = str(row.get("checked_at") or "").strip()
        if not checked_at:
            raise LearningStoreError("Book checked_at is required")

        values = {
            "book_decision_id": str(book_decision_id),
            "ready_id": str(ready_id),
            "checked_at": checked_at,
            "status": status,
            "book_ok": int(book_ok),
            "reason_codes_json": _json(reason_codes),
            "payload_json": _json(dict(row)),
        }
        return self._insert_immutable(
            "book_decisions",
            "book_decision_id",
            str(book_decision_id),
            values,
        )

    def insert_ready_opportunity(
        self,
        ready_id: str,
        version_id: str,
        focus_event_id: str,
        row: Mapping[str, Any],
    ):
        if not isinstance(row, Mapping):
            raise LearningStoreError("READY opportunity must be an object")
        selection_status = str(row.get("selection_status") or SELECTION_PENDING)
        if selection_status not in SELECTION_STATUSES:
            raise LearningStoreError("READY selection_status invalid")
        if selection_status != SELECTION_PENDING:
            raise LearningStoreError("READY opportunity must be created as PENDING")

        source_evidence_id = str(row.get("source_evidence_id") or "").strip()
        if not source_evidence_id:
            raise LearningStoreError("READY source_evidence_id is required")

        values = {
            "ready_id": str(ready_id),
            "version_id": str(version_id),
            "focus_event_id": str(focus_event_id),
            "source_generation_id": str(row["source_generation_id"]),
            "source_evidence_id": source_evidence_id,
            "condition_id": str(row["condition_id"]),
            "token_id": str(row["token_id"]),
            "ready_at": str(row["ready_at"]),
            "selection_status": SELECTION_PENDING,
            "selected_paper_id": None,
            "ended_at": None,
            "payload_json": _json(dict(row)),
        }

        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO ready_opportunities "
                    "(ready_id,version_id,focus_event_id,source_generation_id,"
                    "source_evidence_id,condition_id,token_id,ready_at,"
                    "selection_status,selected_paper_id,ended_at,payload_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(values.values()),
                )
            return True
        except sqlite3.IntegrityError as exc:
            existing = self.conn.execute(
                "SELECT * FROM ready_opportunities WHERE ready_id=?",
                (str(ready_id),),
            ).fetchone()
            if existing is None:
                raise LearningStoreError(str(exc)) from exc

            # READY identity/attribution is immutable. Selection linkage and
            # ended_at are intentionally mutable in later learning phases.
            for column in (
                "version_id",
                "focus_event_id",
                "source_generation_id",
                "source_evidence_id",
                "condition_id",
                "token_id",
                "ready_at",
                "payload_json",
            ):
                if existing[column] != values[column]:
                    raise LearningDataConflict(
                        f"ready_opportunities.ready_id={ready_id} conflicts on {column}"
                    ) from exc
            return False

    def end_open_ready_opportunities(
        self,
        condition_id: str,
        token_id: str,
        ended_at: str,
    ) -> int:
        """End READY availability without deciding User selection.

        LD-4 records the system population. SELECTED vs NOT_SELECTED remains
        unresolved until Paper binding can prove whether the user opened a
        Paper trade for that READY opportunity.
        """
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE ready_opportunities "
                "SET ended_at=? "
                "WHERE condition_id=? AND token_id=? AND ended_at IS NULL",
                (
                    str(ended_at),
                    str(condition_id),
                    str(token_id),
                ),
            )
        return int(cursor.rowcount or 0)

    def bind_paper_open(
        self,
        *,
        paper_id: str,
        ready_id: str,
        version_id: str,
        open_request_id: str,
        row: Mapping[str, Any],
    ):
        """Atomically bind one successful Paper OPEN to one SYSTEM_READY."""
        if not isinstance(row, Mapping):
            raise LearningStoreError("Paper OPEN row must be an object")
        required = (
            "opened_at",
            "condition_id",
            "token_id",
            "outcome",
            "direction",
            "investment_usd",
            "immutable",
            "mutable",
        )
        for field in required:
            if field not in row or row.get(field) is None:
                raise LearningStoreError(f"Paper OPEN {field} is required")

        immutable_json = _json(row["immutable"])
        mutable_json = _json(row["mutable"])
        values = {
            "paper_id": str(paper_id),
            "ready_id": str(ready_id),
            "version_id": str(version_id),
            "open_request_id": str(open_request_id),
            "opened_at": str(row["opened_at"]),
            "status": str(row.get("status") or "OPEN"),
            "condition_id": str(row["condition_id"]),
            "token_id": str(row["token_id"]),
            "outcome": str(row["outcome"]),
            "direction": str(row["direction"]),
            "investment_usd": float(row["investment_usd"]),
            "immutable_json": immutable_json,
            "mutable_json": mutable_json,
        }

        try:
            with self.conn:
                ready = self.conn.execute(
                    "SELECT selection_status,selected_paper_id "
                    "FROM ready_opportunities WHERE ready_id=?",
                    (str(ready_id),),
                ).fetchone()
                if ready is None:
                    raise LearningStoreError("READY opportunity not found")

                selection_status = str(ready["selection_status"])
                selected_paper_id = ready["selected_paper_id"]
                if selection_status == SELECTION_SELECTED:
                    if str(selected_paper_id or "") != str(paper_id):
                        raise LearningDataConflict(
                            f"ready_opportunities.ready_id={ready_id} "
                            "already selected by another Paper trade"
                        )
                elif selection_status == SELECTION_NOT_SELECTED:
                    raise LearningDataConflict(
                        f"ready_opportunities.ready_id={ready_id} "
                        "already finalized NOT_SELECTED"
                    )
                elif selection_status != SELECTION_PENDING:
                    raise LearningStoreError("READY selection_status invalid")

                existing = self.conn.execute(
                    "SELECT * FROM paper_trades WHERE paper_id=?",
                    (str(paper_id),),
                ).fetchone()
                if existing is None:
                    self.conn.execute(
                        "INSERT INTO paper_trades "
                        "(paper_id,ready_id,version_id,open_request_id,opened_at,"
                        "status,condition_id,token_id,outcome,direction,"
                        "investment_usd,immutable_json,mutable_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        tuple(values.values()),
                    )
                    inserted = True
                else:
                    for column, expected in values.items():
                        if existing[column] != expected:
                            raise LearningDataConflict(
                                f"paper_trades.paper_id={paper_id} "
                                f"conflicts on {column}"
                            )
                    inserted = False

                self.conn.execute(
                    "UPDATE ready_opportunities "
                    "SET selection_status=?, selected_paper_id=? "
                    "WHERE ready_id=?",
                    (
                        SELECTION_SELECTED,
                        str(paper_id),
                        str(ready_id),
                    ),
                )
            return inserted
        except sqlite3.IntegrityError as exc:
            raise LearningStoreError(str(exc)) from exc

    def finalize_ready_not_selected(self, ready_id: str) -> bool:
        """Finalize one ended, still-unselected READY opportunity."""
        with self.conn:
            row = self.conn.execute(
                "SELECT selection_status,selected_paper_id,ended_at "
                "FROM ready_opportunities WHERE ready_id=?",
                (str(ready_id),),
            ).fetchone()
            if row is None:
                raise LearningStoreError("READY opportunity not found")
            if row["selection_status"] == SELECTION_SELECTED:
                return False
            if row["selection_status"] == SELECTION_NOT_SELECTED:
                return False
            if row["selection_status"] != SELECTION_PENDING:
                raise LearningStoreError("READY selection_status invalid")
            if row["selected_paper_id"] is not None:
                raise LearningDataConflict(
                    f"READY {ready_id} has Paper linkage but PENDING status"
                )
            if row["ended_at"] is None:
                return False

            cursor = self.conn.execute(
                "UPDATE ready_opportunities "
                "SET selection_status=? "
                "WHERE ready_id=? AND selection_status=? "
                "AND selected_paper_id IS NULL AND ended_at IS NOT NULL",
                (
                    SELECTION_NOT_SELECTED,
                    str(ready_id),
                    SELECTION_PENDING,
                ),
            )
        return bool(cursor.rowcount)

    def fetch_one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()
