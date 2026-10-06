import json
import sqlite3
from pathlib import Path

CHAIN_ID = 137


class StorageError(RuntimeError):
    pass


class CollectorStore:
    def __init__(self, path, chain_id=CHAIN_ID):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.chain_id = int(chain_id)
        self.conn = sqlite3.connect(str(self.path), timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _create_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS checkpoint (
                chain_id INTEGER PRIMARY KEY,
                block_number INTEGER NOT NULL,
                block_hash TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS blocks (
                chain_id INTEGER NOT NULL,
                block_number INTEGER NOT NULL,
                block_hash TEXT NOT NULL,
                parent_hash TEXT NOT NULL,
                block_timestamp INTEGER NOT NULL,
                PRIMARY KEY (chain_id, block_number),
                UNIQUE (chain_id, block_hash)
            );

            CREATE TABLE IF NOT EXISTS trades (
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

            CREATE INDEX IF NOT EXISTS idx_trades_block
              ON trades(chain_id, block_number, log_index);
            CREATE INDEX IF NOT EXISTS idx_trades_time
              ON trades(chain_id, block_timestamp, block_number, log_index);
            """
        )
        self.conn.commit()

    @staticmethod
    def _hash(value, label):
        if not isinstance(value, str) or not value.strip():
            raise StorageError(f"{label} is required")
        return value.strip().lower()

    @staticmethod
    def _integer(value, label, minimum=0):
        if isinstance(value, bool):
            raise StorageError(f"{label} must be an integer")
        try:
            result = int(value)
        except (TypeError, ValueError):
            raise StorageError(f"{label} must be an integer") from None
        if result < minimum:
            raise StorageError(f"{label} is out of range")
        return result

    @staticmethod
    def _payload_text(row):
        try:
            return json.dumps(row, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise StorageError("Trade payload is not JSON serializable") from exc

    def checkpoint(self):
        row = self.conn.execute(
            "SELECT chain_id, block_number, block_hash FROM checkpoint WHERE chain_id=?",
            (self.chain_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def initialize(self, block_number, block_hash):
        number = self._integer(block_number, "block_number")
        digest = self._hash(block_hash, "block_hash")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.conn.execute(
                "SELECT block_number, block_hash FROM checkpoint WHERE chain_id=?",
                (self.chain_id,),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO checkpoint(chain_id, block_number, block_hash) VALUES(?,?,?)",
                    (self.chain_id, number, digest),
                )
            elif int(existing["block_number"]) != number or existing["block_hash"].lower() != digest:
                raise StorageError("Collector checkpoint is already initialized differently")
            self.conn.commit()
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise
        return self.checkpoint()

    def _validate_headers(self, headers, checkpoint):
        if not isinstance(headers, (list, tuple)) or not headers:
            raise StorageError("A non-empty contiguous block batch is required")
        normalized = []
        seen = set()
        for raw in headers:
            if not isinstance(raw, dict):
                raise StorageError("Block header must be a mapping")
            number = self._integer(raw.get("block_number"), "block_number")
            block_hash = self._hash(raw.get("block_hash"), "block_hash")
            parent_hash = self._hash(raw.get("parent_hash"), "parent_hash")
            timestamp = self._integer(raw.get("block_timestamp"), "block_timestamp")
            if number in seen:
                raise StorageError("Duplicate block number in batch")
            seen.add(number)
            normalized.append({
                "block_number": number,
                "block_hash": block_hash,
                "parent_hash": parent_hash,
                "block_timestamp": timestamp,
            })
        normalized.sort(key=lambda x: x["block_number"])
        for previous, current in zip(normalized, normalized[1:]):
            if current["block_number"] != previous["block_number"] + 1:
                raise StorageError("Block batch contains a gap")
            if current["parent_hash"] != previous["block_hash"]:
                raise StorageError("Block parent hash does not match prior block")

        cp_number = int(checkpoint["block_number"])
        cp_hash = checkpoint["block_hash"].lower()
        first = normalized[0]
        if first["block_number"] > cp_number + 1:
            raise StorageError("Block batch starts after the durable checkpoint")
        if first["block_number"] == cp_number + 1 and first["parent_hash"] != cp_hash:
            raise StorageError("First block parent hash does not match checkpoint")
        return normalized

    def commit_batch(self, headers, trades):
        checkpoint = self.checkpoint()
        if checkpoint is None:
            raise StorageError("Collector store is not initialized")
        blocks = self._validate_headers(headers, checkpoint)
        by_number = {b["block_number"]: b for b in blocks}
        if trades is None:
            trades = []
        if not isinstance(trades, (list, tuple)):
            raise StorageError("Trades must be a sequence")

        inserted = 0
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cp_number = int(checkpoint["block_number"])
            cp_hash = checkpoint["block_hash"].lower()

            for block in blocks:
                existing = self.conn.execute(
                    "SELECT block_hash, parent_hash, block_timestamp FROM blocks "
                    "WHERE chain_id=? AND block_number=?",
                    (self.chain_id, block["block_number"]),
                ).fetchone()
                if existing is not None:
                    if (existing["block_hash"].lower() != block["block_hash"] or
                        existing["parent_hash"].lower() != block["parent_hash"] or
                        int(existing["block_timestamp"]) != block["block_timestamp"]):
                        raise StorageError("Stored block conflicts with replayed header")
                    continue

                if block["block_number"] <= cp_number:
                    raise StorageError("Checkpoint references a block missing from block history")
                if block["block_number"] != cp_number + 1:
                    raise StorageError("New block history is not contiguous with checkpoint")
                if block["parent_hash"] != cp_hash:
                    raise StorageError("New block parent hash does not match checkpoint")
                self.conn.execute(
                    "INSERT INTO blocks(chain_id,block_number,block_hash,parent_hash,block_timestamp) "
                    "VALUES(?,?,?,?,?)",
                    (self.chain_id, block["block_number"], block["block_hash"],
                     block["parent_hash"], block["block_timestamp"]),
                )
                cp_number = block["block_number"]
                cp_hash = block["block_hash"]

            for trade in trades:
                if not isinstance(trade, dict):
                    raise StorageError("Trade must be a mapping")
                tx = self._hash(trade.get("transaction_hash"), "transaction_hash")
                log_index = self._integer(trade.get("log_index"), "log_index")
                block_number = self._integer(trade.get("block"), "block")
                block_hash = self._hash(trade.get("block_hash"), "block_hash")
                block = by_number.get(block_number)
                if block is None or block["block_hash"] != block_hash:
                    raise StorageError("Trade is not anchored to this canonical block batch")
                if self._integer(trade.get("block_timestamp"), "block_timestamp") != block["block_timestamp"]:
                    raise StorageError("Trade block timestamp does not match canonical header")
                payload = self._payload_text(trade)
                existing = self.conn.execute(
                    "SELECT block_number, block_hash, block_timestamp, payload_json "
                    "FROM trades WHERE chain_id=? AND transaction_hash=? AND log_index=?",
                    (self.chain_id, tx, log_index),
                ).fetchone()
                if existing is not None:
                    if (int(existing["block_number"]) != block_number or
                        existing["block_hash"].lower() != block_hash or
                        int(existing["block_timestamp"]) != block["block_timestamp"] or
                        existing["payload_json"] != payload):
                        raise StorageError("Duplicate event identity has conflicting contents")
                    continue
                self.conn.execute(
                    "INSERT INTO trades(chain_id,transaction_hash,log_index,block_number,block_hash,block_timestamp,payload_json) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (self.chain_id, tx, log_index, block_number, block_hash,
                     block["block_timestamp"], payload),
                )
                inserted += 1

            latest = blocks[-1]
            if latest["block_number"] > int(checkpoint["block_number"]):
                self.conn.execute(
                    "UPDATE checkpoint SET block_number=?, block_hash=? WHERE chain_id=?",
                    (cp_number, cp_hash, self.chain_id),
                )
            self.conn.commit()
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise
        return inserted

    def recent_trades(self, seconds=1200):
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            raise StorageError("seconds must be numeric") from None
        params = [self.chain_id]
        where = "chain_id=?"
        if seconds > 0:
            where += " AND block_timestamp >= CAST(strftime('%s','now') AS INTEGER) - ?"
            params.append(int(seconds))
        rows = self.conn.execute(
            "SELECT payload_json FROM trades WHERE " + where +
            " ORDER BY block_number ASC, log_index ASC, transaction_hash ASC",
            tuple(params),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]
