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

## E2E-2 COLLECTOR POSITION METADATA CRASH — VERIFIED LOCAL FAILURE

Read-only user diagnostics from the original Windows sandbox established:

- `collector.log`: 1,070,673 bytes, **six** exact `[COLLECTOR] RuntimeError:` reports.
- Every error: `Aggressor position metadata unavailable; batch will be retried`.
- `live_trades.jsonl`: **0 bytes** after the run. This alone cannot prove whether
  the historical DB's block times or the V4 producer-consumer time bug emptied the view.
- 45 batch commits observed in collector.log; sqlite quick_check was OK.

### Narrow source-level patch

In `scripts/live_active_trades.py:lookup_position_market`, `decode_position_id`
and `SUPPORTED` classification continue to gate V3. The function still queries
Gamma exact `position_ids` for open then closed markets and rejects any non-empty
but ineligible Gamma identity as `GammaIdentityConflict(StorageError)` (fatal).

**Only when BOTH Gamma position searches return empty arrays** (metadata
unavailable and no contradictory evidence), the function now raises
`RetryableCollectorError` rather than a generic `RuntimeError`.
The existing main-loop `except RetryableCollectorError` applies unchanged
backoff of 1, 2, 5, 10, then 30 seconds. The retry is against the same
uncommitted block range; no trade is fabricated, dropped or reclassified,
no checkpoint is advanced, and no autonomous order is placed.

Additional regression tests assert: open+closed search order, exact position
filter, `RetryableCollectorError` class, no batch commit/snapshot/checkpoint
advance on a missing supported position, and fatal classification for a
non-empty but identity-ineligible Gamma result.

**Scope:** Prevents unnecessary exit/restart on recoverable missing metadata,
NOT a guarantee of progress. If Gamma never returns this position, the
collector will correctly remain blocked on that batch, so the catch-up test
may continue to fail. This unresolved Gamma availability / fallback policy
requires separate evidence and approval. Never assume missing == unsupported.

CI run on `f3115439659a2aa2a89c240caaf08310bea221a7`:
https://github.com/mudrjafar/PolymarketResearch/actions/runs/39506395713 —
**472 pytest passed, 48 subtests passed**, machine self-test pass; does not
constitute a fresh live runtime PASS.

## V3 GAMMA-TO-DATA-API CONTRACT — 2026-10-22 FIX

Live user-run forensic observations in the first blocked batch after
checkpoint 95883352:

- Range: 95883353–95883357, 326 total exchange events, 27 V3 logs,
  11 V3 aggressor fills, 5 research-only, 6 supported binary fills
  (six distinct positions).
- Local SQLite: 5,870 legacy CTF trades, 0 persisted V3 POSITION rows.
- Gamma /markets position_ids searched open and closed: 0/12 hits.
  Gamma /markets/keyset position and 31/32-byte condition attempts:
  0/3 hits. Gamma keyset parameter assumptions were not sufficient
  to establish provider coverage; do not present HTTP 200 as proof
  of valid lookup semantics.
- Public `https://data-api.polymarket.com/v2/tokens?token_id=...`
  returned 6/6 exact decimal ERC-1155 token ids, 6/6 binary module,
  correct outcome_index and structural_condition_id.
  Data API market condition is a **different 32-byte namespace** from
  the V3 on-chain structural condition on carried-over markets.
- Crucially, six outcome **labels** are basketball team names, not
  canonical "Yes"/"No"; index=1 cannot safely be relabeled "No".
- Relevant primary API contract:
  https://data-api.polymarket.com/v2/openapi.json (GET /v2/tokens;
  `condition_id`, `structural_condition_id`, `outcome`, `outcome_index`,
  `opposite_token_id`, `module`, `question_index`, title and slug).

### Production core fix on isolated branch

When Gamma open/closed exact V3 search finds no match, the collector now
calls the documented public Data API token resolver. The fallback validates
exact token id, decoded V3 module (binary/neg-risk only), outcome index,
structural condition, canonical 32-byte market condition, opposite
position bitfield identity, valid text label, question and market slug,
and neg-risk question index when applicable. Every mismatch fails closed,
and a missing token retries the same uncommitted batch using existing
bounded retry/backoff. No token, market id, outcome or quote is invented.

For Data API V3 evidence, the recorded `condition_id` is the **market**
32-byte id; the `structural_condition_id` is retained separately.
The real market-defined outcome label is retained unchanged (including
team names); legacy CTF records and their prior identity logic are untouched.
Data API does not certify execution price, live order acceptance, market
end time or actual depth: these fields remain explicit `None`. Thus
**this change is only a metadata/collector recovery, NOT V3 Book/Paper
authorization**. Existing Risk/Book gates must separately establish
current executable state; no real orders.

Read-only diagnostic:
`scripts/general1_e2e2_v3_data_probe.py`, using the six historical
positions in exactly five Polygon blocks, prints only identity PASS/FAIL
and non-sensitive counters, never full token ids, wallets or RPC URLs.
No file mutations or SQLite reads; the real execution remains on the user's
local configured Polygon RPC.

Offline tests include sports-outcome/structural/market separation, fail-closed
invalid identities, retry without SQLite checkpoint advance, and
privacy-safe diagnostic status. Offline CI passing is NOT the same as
a real local V3 probe PASS, actual pipeline catch-up, or paper readiness.

**Remaining live gates:** run read-only historical six-token probe,
isolate V3 metadata request costs, prove supported first blocked batch
can commit in a fresh dedicated sandbox, sustained collector blocks/sec
> chain blocks/sec, decreasing lag, and real Collector→Flow→Diamond→Risk
binding. PR remains DRAFT and unmerged until those checks pass.
