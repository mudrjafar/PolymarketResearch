import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from collector_storage_v4.storage import CollectorStore, StorageError
    from collector_storage_v4.bridge import CollectorLock, publish_snapshot
except ModuleNotFoundError:
    # run_machine.py executes this file directly; in that mode Python places
    # collector_storage_v4/ (not the repository root) on sys.path.
    from storage import CollectorStore, StorageError
    from bridge import CollectorLock, publish_snapshot


def h(n):
    return "0x" + f"{n:064x}"


def header(n, timestamp=None):
    return {
        "block_number": n,
        "block_hash": h(n),
        "parent_hash": h(n - 1),
        "block_timestamp": int(time.time()) if timestamp is None else timestamp,
    }


def trade(n=101, log_index=0, value="a", timestamp=None):
    ts = int(time.time()) if timestamp is None else timestamp
    return {
        "block": n,
        "block_hash": h(n),
        "block_timestamp": ts,
        "transaction_hash": h(n + 1000),
        "log_index": log_index,
        "token_id": "123",
        "outcome": "Yes",
        "value": value,
    }


class CollectorStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = CollectorStore(self.root / "collector.sqlite3")
        self.addCleanup(self.store.close)
        self.store.initialize(100, h(100))

    def test_initialize_is_durable_and_does_not_reset(self):
        self.assertEqual(self.store.checkpoint()["block_number"], 100)
        self.store.close()
        self.store = CollectorStore(self.root / "collector.sqlite3")
        self.assertEqual(self.store.checkpoint()["block_hash"], h(100))
        with self.assertRaises(StorageError):
            self.store.initialize(99, h(99))

    def test_commit_is_atomic_and_advances_checkpoint(self):
        ts = int(time.time())
        blocks = [header(101, ts), header(102, ts + 1)]
        rows = [trade(101, timestamp=ts), trade(102, timestamp=ts + 1)]
        self.assertEqual(self.store.commit_batch(blocks, rows), 2)
        self.assertEqual(self.store.checkpoint()["block_number"], 102)
        self.assertEqual(len(self.store.recent_trades(0)), 2)

    def test_gap_or_parent_mismatch_fails_closed(self):
        with self.assertRaises(StorageError):
            self.store.commit_batch([header(102)], [])
        bad = header(101)
        bad["parent_hash"] = h(999)
        with self.assertRaises(StorageError):
            self.store.commit_batch([bad], [])
        self.assertEqual(self.store.checkpoint()["block_number"], 100)

    def test_replay_is_idempotent_but_conflicting_event_is_rejected(self):
        ts = int(time.time())
        block = header(101, ts)
        row = trade(101, timestamp=ts)
        self.assertEqual(self.store.commit_batch([block], [row]), 1)
        self.assertEqual(self.store.commit_batch([block], [row]), 0)
        conflict = dict(row, value="changed")
        with self.assertRaises(StorageError):
            self.store.commit_batch([block], [conflict])
        self.assertEqual(len(self.store.recent_trades(0)), 1)

    def test_trade_must_match_canonical_header(self):
        ts = int(time.time())
        row = trade(101, timestamp=ts)
        row["block_hash"] = h(999)
        with self.assertRaises(StorageError):
            self.store.commit_batch([header(101, ts)], [row])
        self.assertEqual(self.store.checkpoint()["block_number"], 100)

    def test_recent_trades_are_ordered_and_time_filtered(self):
        now = int(time.time())
        blocks = [header(101, now - 2000), header(102, now - 10)]
        rows = [
            trade(101, 1, timestamp=now - 2000),
            trade(102, 0, timestamp=now - 10),
        ]
        self.store.commit_batch(blocks, rows)
        self.assertEqual(len(self.store.recent_trades(0)), 2)
        self.assertEqual([r["block"] for r in self.store.recent_trades(1200)], [102])

    def test_publish_snapshot_is_atomic_and_preserves_one_time_backup(self):
        path = self.root / "live_trades.jsonl"
        path.write_bytes(b'{"legacy":true}\n')
        ts = int(time.time())
        self.store.commit_batch(
            [header(101, ts)], [trade(101, timestamp=ts)]
        )
        publish_snapshot(self.store, path)
        backup = self.root / "live_trades_before_sqlite.jsonl"
        self.assertEqual(backup.read_bytes(), b'{"legacy":true}\n')
        first_backup = backup.read_bytes()
        publish_snapshot(self.store, path)
        self.assertEqual(backup.read_bytes(), first_backup)
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_failed_snapshot_replace_keeps_original(self):
        from collector_storage_v4 import bridge

        path = self.root / "live_trades.jsonl"
        original = b'{"legacy":true}\n'
        path.write_bytes(original)
        real = bridge.replace_file

        def fail_snapshot(src, dst):
            if Path(dst) == path:
                raise PermissionError("locked")
            return real(src, dst)

        with patch.object(bridge, "replace_file", side_effect=fail_snapshot):
            with self.assertRaises(PermissionError):
                publish_snapshot(self.store, path)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(
            (self.root / "live_trades_before_sqlite.jsonl").read_bytes(), original
        )

    def test_old_rows_stay_in_database_but_not_snapshot(self):
        old = 1700000000
        self.store.commit_batch(
            [header(101, old)], [trade(101, timestamp=old)]
        )
        path = self.root / "live_trades.jsonl"
        self.assertEqual(publish_snapshot(self.store, path), 0)
        self.assertEqual(path.read_text(encoding="utf-8"), "")
        self.assertEqual(len(self.store.recent_trades(0)), 1)

    def test_collector_lock_is_exclusive_and_reusable(self):
        path = self.root / "collector.lock"
        with CollectorLock(path):
            with self.assertRaises(RuntimeError):
                with CollectorLock(path):
                    pass
        with CollectorLock(path):
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
