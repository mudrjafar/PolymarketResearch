# collector_storage_v4

Versioned source for the active collector storage layer.

## Runtime contract

- SQLite is the durable collector source of truth.
- Checkpoint, canonical block headers and trades are committed in one transaction.
- Event identity is `(chain_id, transaction_hash, log_index)`.
- Replaying identical evidence is idempotent.
- Conflicting duplicate evidence, block gaps and parent-hash mismatches fail closed.
- `data/live_trades.jsonl` is an atomic compatibility snapshot of the most recent
  20 minutes by persisted block time.
- An existing pre-SQLite compatibility file is preserved once as
  `live_trades_before_sqlite.jsonl`.
- `CollectorLock` prevents two collector processes from owning the same runtime.

## Version-control boundary

This directory contains source code, tests and documentation only. Runtime SQLite
files, lock files, logs, caches and generated snapshots must not be committed.

## Offline checks

From the repository root:

```
python -B collector_storage_v4/test_storage.py
python -B tests/test_collector_sqlite.py
python -m pytest tests -q
python -B run_machine.py --self-test
```
