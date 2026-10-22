# GENERAL1-FRESHNESS-1 — PRELIVE TROUBLESHOOTING & DECISION REPORT

**Status:** Offline implementation complete on an isolated feature branch. **No Polygon live trial has been run for this task.** Do not treat GitHub CI as a live-freshness proof.

**Source of truth:** `mudrjafar/PolymarketResearch`, starting from E2E2 feature head `2e6e685`; `main` was `97d8813` at audit time. Review final commits/CI before running.

## A. Troubleshooting report

| Symptom | Code path | Primary explanation | Evidence | Confidence | Remediation |
|---|---|---|---|---|---|
| Initial ~520-block head-to-checkpoint gap | `live_active_trades.initialize_store` | New empty database seeds at `safe_tip - INITIAL_LOOKBACK_BLOCKS + 1`; default 500 historical blocks plus 20 confirmations | Source defaults and last run lag 514 at start | High | Dedicated **test-only** lookback 30; do not change production defaults |
| No 1m / 5m trades | `flow_tracker.load_trades/analyze_flow` | Canonical block times are 11-13 minutes old when consumed | Snapshot evidence: 0 trades in 1m and 5m, 1818 window counts in 15m, lag 453 by final collector batch | High for this observed run | Near-tip experiment with canonical event-age measurements |
| All Risk decisions NEUTRAL/BLOCK | `flow_tracker` → `diamond_filter_v3` → `risk_engine.assess` | No recent 5m direction or new confirming evidence; Risk vetoes correctly | 362/362 NEUTRAL, 362/362 BLOCK, 0 verified, 362 DATA_RISK | High for observed downstream behavior | Do **not** relax confirmations, Risk, Focus or evidence gates |
| False stale Diamond times | `diamond_filter_v3.normalize` vs `machine_common.fresh` | V4 `block_timestamp` is integer Unix seconds while `fresh` expects ISO datetime | Exact code path and earlier regression; upstream E2E2 fix `2e6e685` | Confirmed code defect; real-world frequency unknown | Inherited UTC normalization, no detected_at substitution |
| Collector modest excess throughput | `live_active_trades.scan_once` | Batch size 5, 1.5s sleep, network metadata calls, SQLite/JSON costs | In last run 0.746 blocks/s collector vs 0.667 chain, 670 Gamma lookups, mean batch ~4216 ms | Measured throughput; **unproven** relative cost by phase | Reuse LIVE-1C counters; profile first, optimize only measured bottleneck |
| V3 identity not sufficient for paper entry | `paper_settlement`, V3 Data API fallback | Certain V3 identities have UNKNOWN tradeability/timing; settlement remains legacy CTF / Yes-No-focused | Source audit | High | Separate future scope, not a workaround for live freshness |

**Measured report snapshot (prior sandbox):** 429s interrupted; 5741 SQLite trades, 362 Diamond analyses/Risk decisions, 0 worker restarts, SQLite quick_check OK. Live trade-to-Risk generation binding true. This does **not** establish signal quality or strategy profitability.

**Causality confidence:** high for initialization/latency and missing short-window evidence. Exact share of time spent on RPC, Gamma, SQLite or snapshot publication remains unknown until the new timing report.

## B. Implementation report

Branch: `feat/general1-freshness-1-neartip-diagnostics-20261023` (based on E2E2 feature commit `2e6e685`).

- A separate `scripts/general1_freshness1_windows.cmd` creates `C:\PolymarketResearch\GENERAL1_FRESHNESS1_LIVE_SANDBOX` only when absent. It never reuses an old sandbox, launches no production wrapper and copies neither .env nor historical database.
- `scripts/general1_freshness1_live_probe.py` reuses the E2E2 supervisor/observer while enforcing the independent sandbox identity and forbidding Dashboard.
- `general1_e2e2_live_probe.py` gained **explicit near-tip mode**. The child environment forces `COLLECTOR_INITIAL_LOOKBACK_BLOCKS=30`, retains `COLLECTOR_CONFIRMATION_BLOCKS=20`, and removes inherited `COLLECTOR_START_BLOCK`. Default/production behavior remains unchanged.
- A sanitized report includes canonical checkpoint-block age p50/p95 over the run, age of saved V4 trade events, 1m/5m/15m snapshot counts, Flow market counts, verified count, Diamond class distribution, Risk PASS/BLOCK and top reason codes.
- Existing LIVE-1C phase counters are summarized as totals, p50/p95 and maxima. RPC / Gamma subphases can overlap: **never add their percentages**.
- The near-tip experiment cannot receive PASS unless **canonical checkpoint age p95 ≤120 seconds**, at least one V4 snapshot trade is within 5m, and at least one Flow market has 5m trades, in addition to existing supervisor/SQLite/real-trade-to-Risk safety conditions.
- `historical_backfill_complete=false` is explicit in the report. Freshness PASS is not Strategy PASS.

No live trading, real orders, autonomous Paper OPEN, Focus READY fabrication or changed Risk thresholds.

## C. Offline test plan and actual results

CI workflow: `.github/workflows/general1-freshness1-preflight.yml` runs in a separate GitHub Actions environment.

- Python syntax compilation
- Targeted safety/regression tests
- Full offline `pytest`
- Full `run_machine.py --self-test`

New offline fixtures cover: near-tip override isolation, no inherited start block, no inherited Telegram credentials, separate sandbox identity, canonical integer timestamps, duplicates, future and missing timestamps, stale-first vs fresh 1m/5m/15m windows, evidence cursor binding, no false Risk PASS, and fail-closed near-tip report classification.

**See the last workflow run for authoritative counts and pass/fail.** These tests use fixtures, mocks and no actual Polygon live trial.

## D. Performance status and required live report

The prior live summary establishes throughput but not a per-phase attribution. Existing in-process instrumentation provides: `batch_ms`, `rpc_ms`, `get_logs_ms`, `timestamp_ms`, `gamma_lookup_ms`, `gamma_refresh_ms`, `decode_ms`, `enrichment_ms`, `sqlite_ms`, `compatibility_ms`, and `backoff_ms`. The new sanitized report records timing distributions without raw RPC URLs, question titles, wallets, hashes or token IDs.

Live data still needed: checkpoint block age p50/p95, 1m and 5m counts, actual catch-up from 30-block initial history, failure/retry distribution, timing bottleneck. If zero transactions naturally occur in a short window, record this as inconclusive rather than fake a signal.

## E. Architecture Decision Record — PROVISIONAL

**Provisional preference: Option A**, retain the single authoritative canonical collector if the controlled near-tip experiment demonstrates p95 checkpoint age ≤120s and working 5m flow without safety regressions. This does not automatically solve continuous historical catch-up needs.

**Option B** if fresh start succeeds but sustained performance fails; use the phase timings to optimize a specific bottleneck.

**Option C** (separate live/backfill pipelines) only if concrete A/B tests show the shared processing/retention policy cannot meet the declared latency/finality and historical completeness requirements. If selected, require separate checkpoints, shared canonical txhash+log_index identity, reorg handling, deduplication and fail-closed recovery. Do not authorize this now.

**No business-validity claim:** A positive expected value or calibration has not been established.

## F. Next task and GO/STOP

**Next task after measured freshness success:** `GENERAL1-PAPER-1` — one manually requested, entirely simulated OPEN→MARK→CLOSE or finalized SETTLE with READY + Book PASS + identity/finality evidence, fee-aware execution, generation attribution, realized P&L and no real orders. Use an explicitly supported CTF market; do not bypass V3 gaps.

**STOP** if the new collector still exceeds 120s p95 checkpoint age, empty 5m remains unexplained, Risk/Focus constraints must be relaxed, or data consistency changes. Return to measured profiling or revisit the strategy's time horizon.

### Safe Windows steps after green offline CI

1. Confirm no other freshness test is running.
2. In CMD, from `C:\PolymarketResearch`, clone the new branch into a separate launcher-only checkout:
   `git clone --depth 1 --single-branch --branch feat/general1-freshness-1-neartip-diagnostics-20261023 https://github.com/mudrjafar/PolymarketResearch.git GENERAL1_FRESHNESS1_LAUNCHER`
3. Run `cd /d C:\PolymarketResearch\GENERAL1_FRESHNESS1_LAUNCHER && call scripts\general1_freshness1_windows.cmd`.
4. Never delete or overwrite an existing `GENERAL1_FRESHNESS1_LIVE_SANDBOX`; archive it first if repeating intentionally.
5. Share **only** `general1_freshness1_live_report.json`, not raw worker logs, .env or personal data.

The valid completion boundary for this report is **offline complete / controlled live verification pending**, until the user supplies an independently captured live report.
