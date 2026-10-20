# GENERAL1-E2E-2 — Isolated Live Runtime / Pre-Live Review

**TASK:** GENERAL1-E2E-2 — First Isolated Live Runtime Validation  
**SOURCE OF TRUTH:** GitHub main `97d881311a68c54ec15a4f537d8a055e04729f44`  
**IMPLEMENTATION BRANCH:** `feat/general1-e2e2-isolated-live-runtime-20261020`  
**MODE:** Dedicated, isolated Windows run; `main` unchanged.  
**STATUS:** PARTIAL — offline code and contracts tested; actual 15-minute RPC runtime not yet observed.

## Evidence

- Prior user Windows verification: 452 offline tests + 48 subtests; machine self-test OK (E2E-1 sandbox).
- Previous user read-only check of old 164,974,592-byte collector database:
  `UNSUPPORTED_TRADES_SCHEMA`, `MIGRATION_BLOCKED`, WAL sidecar present.
- That historical database is neither referenced nor used by the new observer.
- E2E-2 offline CI test outcomes: see GitHub Actions for this branch. **Do not infer Live PASS from CI.**

## Runtime topology

Existing `run_machine.py` already declares and the supervisor launches:
`collector`, `flow`, `diamond`, `risk`, `learning`, `focus`,
`book`, `paper`, `telegram`, `dashboard`.

The E2E-2 runner keeps all seven core worker processes and the Learning sidecar.
Telegram is forced DISABLED (even if credentials exist).
Dashboard is DISABLED by default to avoid conflicting desktop ports; enable only with `--dashboard`.

## Files examined

- `run_machine.py`, `runtime_supervisor.py`, `start_machine.bat`
- `scripts/prepare_v3_data.py`, `scripts/live_active_trades.py`
- `collector_storage_v4/storage.py`, `collector_storage_v4/bridge.py`
- `scripts/flow_tracker.py`, `scripts/diamond_filter_v3.py`
- `scripts/risk_engine.py`, `scripts/risk_worker.py`
- `scripts/focus_runner.py`, `scripts/book_worker.py`
- `scripts/paper_worker.py`, `scripts/learning_worker.py`
- `scripts/collector_db_preflight.py`, `system_status.py`
- `.env.example`, `requirements.txt`, `tests/test_runtime_supervisor.py`
- GENERAL1-H1.1 market discovery remains PR #41, unmerged.
- GENERAL1-E2E-1 remains PR #42, unmerged.

## Findings and boundaries

1. Normal launcher `start_machine.bat` loads .env, may install dependencies and calls
   `run_machine.py`, which runs `prepare_v3_data.py` and can archive existing files.
   **Never use that launcher for this isolated test.**
2. Collector initializes a fresh SQLite schema via `CollectorStore` after verifying
   chain 137. It uses a configurable lookback and emits `[LIVE-1C]` batch metrics.
3. The isolated runner launches the actual existing `RuntimeSupervisor` with
   its original core specs; no Collector/Risk/Focus/Book/Paper production logic changes.
4. The runner refuses to run outside an empty, uniquely named
   `C:\\PolymarketResearch\\GENERAL1_E2E2_LIVE_SANDBOX` Git clone, refuses
   to overwrite data, and refuses to copy/keep an `.env` in this sandbox.
5. It reads only `POLYMARKET_RPC_URL` from the original external .env into process memory,
   checks HTTPS, chain ID 137 and chain head **before creating runtime files**.
6. E2E-2 never creates paper OPEN/CLOSE requests; a surprise request causes an early stop.
7. Collector stdout/stderr are captured under the new sandbox's private
   `general1_e2e2_logs`. **Do not share or upload raw worker logs** until examined
   for credentials; the generated report intentionally avoids URLs and wallet identities.

## Output after a local 900-second session

- `general1_e2e2_live_report.json`: safe summary, separate pipeline/progress metrics
- `general1_e2e2_status_samples.jsonl`: time-sampled service statuses, no secrets
- `general1_e2e2_logs`: raw PRIVATE worker logs, do not upload unreviewed
- `data/collector.sqlite3`, `data/live_trades.jsonl` etc.: TEST data, not production

Collector metrics include chain/collector blocks-per-second, backlog movement,
checkpoint, retries, batch times, observed trade counts, Gamma lookup counts.
A matching DB trade, committed Diamond generation and generation-bound Risk
decision must all be present to mark `real_trade_to_risk_binding_observed=true`.
Worker `RUNNING` is not proof that the data handoff worked.

The run returns `PASS` only when:
- no fatal errors or critical restarts;
- every critical worker is observed RUNNING;
- collector commits actual blocks with acceptable catch-up evidence;
- `PRAGMA quick_check` is OK;
- at least one real on-chain trade can be matched through Flow generation,
  Diamond analysis and Risk;
- Paper stays empty without user instructions.

`PARTIAL` means some check could not be established; `BLOCKED` means
a critical fault, unsafe condition or data integrity problem.

## Limitations

- No network credentials are accessible in CI; cannot assert real live RPC success.
- A 15-minute sample is insufficient to claim long-term availability or sustained performance.
- No actual Diamond, READY or simulated trade must occur; legitimate fail-closed states are acceptable.
- No historical DB migration or sport-filter integration is included.

## Next task

After reviewing the generated JSON, GENERAL1-E2E-3 should resolve only measured runtime
blockers and add a separate, reviewed integration for the sport-excluded candidate watchlist.
Do not implement strategy changes until live delivery contracts are verified.

## FIRST USER WINDOWS LIVE SESSION — ACTUAL OBSERVED EVIDENCE

Source: user's local `general1_e2e2_live_report.json` (not GitHub CI). The report
contains no RPC URLs or transaction identifiers and may be shared.

- **RESULT:** PARTIAL; duration **815.03 s**, requested **900 s**, `interrupted=true`.
- RPC chain id **137**. Head `95883646 → 95884190`.
- Collector committed **225 blocks**, **5,870 trades** into a database with `quick_check_ok=true`.
- Collector rate **0.276 blocks/sec** vs chain **0.667 blocks/sec** (41.36% of chain).
- Lag increased **521 → 630**; `catchup_observed=false`.
- **866** Gamma lookups, mean success batch **8,791 ms**, max **20,168 ms**.
- Six **collector** worker restarts, with six consecutive failures and final
  `DEGRADED` status. All other enabled workers remained RUNNING with zero
  observed restarts. The summary did not contain crash exception classes.
- Flow markets 0, Diamond generation exists with analysis count 0, matching
  Risk generation but Risk decisions 0, `real_trade_to_risk_binding_observed=false`.
- Focus IDLE, Book NOT_READY, Paper 0 positions.

### Confirmed producer-consumer contract defect (source-level)

`scripts/live_active_trades.py:get_block_header()` emits `block_timestamp` as
Polygon's **integer Unix seconds**. `normalize_trade()` persists this integer
in V4 JSON trade rows. But old `scripts/flow_tracker.py:_strict_aware_time()`
previously attempted `datetime.fromisoformat(str(value))` and rejected
Unix integers. Every otherwise-valid V4 collector row with an integer timestamp
was rejected before Flow window eligibility; `load_trades()` filtered these
rows out. Additionally `last_trade_at` previously copied the raw numeric
timestamp and would be considered stale/unknown by Diamond's ISO-only
`machine_common.fresh()`.

**Minimal fix:** accept **integer Unix seconds** for the canonical V4 block
timestamp (while preserving strictly-aware ISO timestamp compatibility),
reject bool/negative/out-of-range timestamps; normalize Flow's exported
`market.last_trade_at` to timezone-aware ISO derived from the block time,
not `detected_at`. Dedicated unit tests preserve stale/invalid data
fail-closed behavior. Not yet proven on user hardware.

### Collector crash and performance remain open

Exit code 1 repeated six times. The safe live summary does not contain
exception types; no root cause should be invented. The branch now includes
`scripts/general1_e2e2_postmortem.py`, a read-only, credential-redacting
collector-log classifier and V4 snapshot timestamp/age report. Run it
locally against the original sandbox. **Do not upload raw collector.log**.

Collector catch-up remains a hard blocker independent of the fixed Flow
timestamp shape. Do not modify batch size, RPC concurrency, Gamma caching,
sleep, or retry semantics without separate measurements and authorization.

**Verdict:** GENERAL1-E2E-2 remains PARTIAL. The source-level fix is
pending local retest; the live collector crash and lag require diagnostics.
