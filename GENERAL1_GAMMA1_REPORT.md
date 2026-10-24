# GENERAL1-GAMMA-1 — AUDIT, PATCH, TESTS, BASELINE AND LIVE DECISION

**Branch:** `feat/general1-gamma-1-bounded-metadata-20261025`. **Baseline:** `feat/general1-freshness-1-neartip-diagnostics-20261023` at `46953cd`. **main untouched.** No production deployment, orders, Paper OPEN or real-money trading.

## 1. Root-cause investigation

Real GENERAL1-FRESHNESS-1 observer data from the prior isolated test:

| Metric | Baseline |
|---|---:|
| Duration | 683.36 s of 900, interrupted |
| Collector blocks/s | 0.29999 |
| Chain blocks/s | 0.66729 |
| Safe-tip lag | 62 → 269 blocks |
| Successful / failed batches | 41 / 0 |
| Blocks / persisted trades | 205 / 4582 |
| Legacy Gamma single-token lookup calls | 1088 |
| Gamma single-token timing | 435,314 ms |
| Mean complete batch | 12,681 ms |
| SQLite write, mean | 18.6 ms |
| 5m snapshot trades at end | 0 |
| 5m Flow markets in older state | 62 |
| Flow VERIFIED | 0 |
| Risk decisions | 1141 BLOCK |
| Latest Diamond | 1171 DATA_RISK |
| Risk vs latest manifest | mismatched |
| Worker restarts / DB quick_check | 0 / OK |

The prior `gamma_lookup_count` measures **legacy** `lookup_market` calls, not all V3 calls. Thus a precise Legacy/V3 time split is **not available from the old report**. Even if metadata calls are optimized, sustained performance and freshness are NOT proven until a new comparable live test.

The existing `GammaTokenMap` already caches successful canonical identities in process; fallback cache does NOT treat mutable market status as timeless. The major avoidable cost is multiple remote round trips for many **new unique** legacy CTF tokens within one 5-block scan. Each singleton tries open then closed Gamma `/markets` filters. V3 takes its own position-ID Gamma→Data API path. Blockchain safety checks, trade decode, commits and snapshots are already established.

## 2. Minimal implementation

In `scripts/live_active_trades.py`:

- **Opt-in only:** `COLLECTOR_GAMMA_BATCH_LOOKUP=1` in a dedicated isolated comparison sandbox. Normal production behavior unchanged.
- Preinspect each complete batch's decoded aggressor events with the original canonical header and taker check. Unique missing **legacy CTF IDs only** are gathered.
- Resolve chunks of ≤12 token IDs via Gamma `/markets?clob_token_ids=...`, querying open first and closed for remaining IDs, max 100 markets/response.
- Use existing `build_token_map`, `_gamma_identity`, `_check_gamma_identity`, and `GammaTokenMap.remember_fallback` identity ledger. All requested IDs must resolve; missing IDs follow their unchanged singleton lookup (which can fail closed).
- Rejected malformed/truncated responses or identity contradictions still block the batch, with no SQLite checkpoint advancement.
- Cache changes occur only after all prefetch requests and singleton fallbacks succeeded. No fabricated active/closed flags, evidence IDs or timestamps.
- V3 position lookup is intentionally **not** batched in this change to preserve its distinct structural-condition and Data API fallback contract. V3 lookup duration/count now measured separately.
- Adds bounded aggregate diagnostics: batch HTTP count, unique legacy cache misses, cache-hit events, batch tokens requested/resolved, V3 lookup/event counts and costs. No raw tokens, wallet addresses or hashes in the sanitized report.

**External API assumption:** Gamma documents an array of `clob_token_ids` filter values. The live trial determines whether provider-specific filtering/throughput matches the mocked offline contract. If the API omits an ID, singleton lookup is mandatory; no block skipping.

## 3. Generation consistency

Previously the observer compared the **latest** `diamond_generation.json` manifest to `risk_assessment.json` and required exact latest-generation matching. During runtime, Risk can legitimately lag the latest Diamond generation by a publication cycle.

The patch retains the conservative `risk_generation_matches` and existing PASS criterion. Separately it inspects the **exact Risk-specified archived generation**:

- `CURRENT_MATCH`: latest manifest and Risk source generation agree.
- `LAGGED_ARCHIVED_GENERATION`: Risk source differs but a valid archived Flow+Diamond generation exists.
- `UNVERIFIED_ARCHIVE_MISSING`: source refers to an unavailable snapshot.
- `MISSING_GENERATION`: missing manifest or Risk generation.

The new `risk_source_archive_real_trade_binding_observed` establishes **sampled identity overlap**, not full evidence-chain validation; the SQLite sample remains bounded to recent rows. A matching archive alone is **not enough** to set `risk_generation_matches=true` or Risk PASS.

A residual mismatch with archived evidence is informative rather than automatically a pipeline defect. It must be interpreted alongside per-worker generations and timestamps during the next live run.

## 4. Test and safety verification

GitHub Actions: `.github/workflows/general1-gamma1-preflight.yml`

- Compile changed source and new launcher.
- Gamma-1 targeted tests (batch calls, open/closed filter, malformed response, network failures, cached identities, V3 isolation, and archived generation status).
- Existing Collector SQLite, E2E2 and Freshness-1 tests.
- Full offline regression suite and machine self-test.

**Authoritative PASS/failure status:** read latest GitHub Actions run on this feature branch. A passing CI proves offline contracts only.

## 5. Comparison trial — NOT YET PERFORMED

Launch a **new**, one-shot `GENERAL1_GAMMA1_LIVE_SANDBOX`; do not reuse, delete, migrate or overwrite the previous Freshness-1 sandbox. The launcher uses its own venv, external RPC only, 30 initial lookback blocks and 20 confirmations, with Gamma batch prefetch enabled. Telegram and Dashboard disabled; no Paper requests or real orders.

Compare baseline and new experiment:

| Metric | Baseline | New live |
|---|---:|---|
| Collector blocks/s | 0.300 | PENDING |
| Chain blocks/s | 0.667 | PENDING |
| Safe-tip lag change | +207 | PENDING |
| Complete batch p95 | 20.35 s | PENDING |
| Gamma single-token calls | 1088 | PENDING |
| Gamma batching HTTP/tokens | N/A | PENDING |
| Separate V3 lookup ms | N/A | PENDING |
| Canonical checkpoint block age p95 | 441.6 s | PENDING |
| 5m snapshot trades | 0 | PENDING |
| SQLite quick_check | OK | PENDING |
| Generation status | Mismatch to latest | PENDING |

**Targets:** preferably ≥0.8 blocks/s sustainably under comparable load, lag decreasing or stable close to finalized/safe tip, checkpoint age p95 ≤120s, real 1m/5m evidence and bounded Risk/Focus behavior. Short observations and provider variation cannot guarantee throughput; fresh input does not imply strategy edge.

## 6. GO / STOP

- **GO (limited):** If the newly measured collector outpaces the chain without integrity regressions, inspect natural Flow→Diamond→Risk behavior and consistency; a later manually authorized paper-only lifecycle can be considered.
- **STOP:** If rate stays below chain-rate, trace V3/Data API cost, Gamma API batch filter behavior, periodic full-market refresh and/or current public RPC. Do not claim success from synthetic signals or relax Risk/Focus.
- **No automatic merge to main**, no live money and no unattended paper position.

## 7. User steps (Windows, new PC)

1. `cd /d C:\PolymarketResearch`
2. `git clone --single-branch --branch feat/general1-gamma-1-bounded-metadata-20261025 https://github.com/mudrjafar/PolymarketResearch.git GENERAL1_GAMMA1_LAUNCHER`
3. `cd /d C:\PolymarketResearch\GENERAL1_GAMMA1_LAUNCHER`
4. `py -3.11 -m venv .venv`
5. `.venv\Scripts\python.exe -m pip install -r requirements.txt`
6. `.venv\Scripts\python.exe -m pytest -q`
7. `.venv\Scripts\python.exe run_machine.py --self-test`
8. **Only after CI and local tests PASS**, in the same CMD session as `POLYMARKET_RPC_URL`, `call scripts\general1_gamma1_windows.cmd`
9. Share only `C:\PolymarketResearch\GENERAL1_GAMMA1_LIVE_SANDBOX\general1_gamma1_live_report.json`, no raw logs or credentials.

If the launcher or sandbox folder already exists, do not delete it or run clone twice. Report the state before further actions.
