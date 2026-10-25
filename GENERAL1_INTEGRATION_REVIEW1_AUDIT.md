# GENERAL1-INTEGRATION-REVIEW-1 — Independent Code / Security / Dependency Audit

**Repository:** mudrjafar/PolymarketResearch
**Baseline:** `main` `97d881311a68c54ec15a4f537d8a055e04729f44`
**Upstream compared:** Gamma-1 `65ab5ce8a68337675291a1012a6a49dbb54818d1` and READY-1 `d61fff69824a8674b11f91194cafb91c2ae507b4`.
**Minimal integration branch:** `feat/general1-integration-review1-core-20261025`, created from the exact `main` SHA, with **no merge of the 81 upstream commits**.
**Mode:** offline review, minimal code copies, CI regression; NO Polygon/CLOB execution probe, live orders, credentials, local sandbox reuse, or changes to `main`.

## 1. Source-of-truth comparison

GitHub `compare_commits(main,READY-1)`: READY-1 81 commits ahead, zero behind; **34 changed paths** but only **six pre-existing runtime source files** are modified:

- `scripts/live_active_trades.py` +270/-15 (V3 fallback/metadata, strict batching opt-in and metadata telemetry);
- `scripts/flow_tracker.py` +10/-2 (canonical confirmation evidence derivation);
- `scripts/diamond_filter_v3.py` +30/-1 (V3 Data API provenance and invalid timestamp handling);
- `scripts/focus_engine.py` +12 (locked condition/outcome identity);
- `scripts/book_engine.py` +8/-3 (CLOB IDs must be present and match);
- `scripts/paper_worker.py` +11 (post-network/pre-OPEN final gate recheck).

`run_machine.py`, `risk_engine.py`, `risk_worker.py`, `focus_runner.py`, `book_worker.py`, `paper_engine.py`, `paper_settlement.py`, `telegram_bot.py` and `collector_storage_v4/*` have **no file differences** versus `main` in this comparison. These are still critical runtime authorities and existing behaviour is covered by regression.

Other 28 paths are old E2E2/Freshness/Gamma/READY workflows, reports, one-shot Windows launchers, live probes and test files. Not necessary for the canonical core.

Selected review scope: copy the six source files plus the modified existing Collector/Flow/Diamond tests, `tests/test_general1_ready1.py` and a **curated, self-contained Gamma batch core test** to a fresh branch *from main*. The curated test omits only Gamma-specific live report and launcher tests. One test `test_e2e2_live_identity_probe_codes_and_no_raw_ids` was separately excluded from the copied Collector test module because it imports a non-core experimental probe. No production code was modified to make that test pass. CI initially demonstrated this missing dependency; rerun tests on the isolated branch after exclusion.

### Open-PR boundary

At review, #9, #24, #35–#44 were open; **not an implicit merge queue**. #43 contains previous isolated runtime probe, #44 is READY-1 based on Gamma-1 (rather than main), and #35/#36/#38/#39/#40 are overlapping prior safety/integration experiments. Do not cherry-pick all competing PRs. New integration PR must show only the audited six core source files + regression tests + reports relative to main. PR #44 remains open/draft/unchanged.

## 2. Actual authority path

`run_machine.SERVICES`: Collector → Flow → Diamond → Risk → Learning → Focus → Book → Paper → Telegram → Dashboard. These are **independent workers**; declaration order does not impose an atomic synchronous transaction.

1. **Collector:** `scripts/live_active_trades.py` reads Polygon RPC, validates block headers/log hashes and taker-aggressor events, isolates CTF from supported V3 identities, then commits via `CollectorStore.commit_batch(headers, rows)` and publishes bounded `live_trades.jsonl`. SQLite block checkpoint is authority. If the JSON projection fails after commit, the DB remains authoritative; next scan republishes.
2. **Flow:** `scripts/flow_tracker.py` produces exact `evidence_id = txhash:log_index`, `evidence_cursor=[block, log_index]` and event-time `evidence_at` from one confirming trade, grouped by token+condition+outcome. Invalid/legacy evidence does not become a valid confirmation.
3. **Diamond:** `scripts/diamond_filter_v3.py` freezes archived Flow and Diamond analysis into a unique immutable generation and atomically publishes its manifest last. **Risk** consumes one archived generation per evaluation and publishes `risk_assessment.json`. No latest-root-flow cross-generation substitution.
4. **Focus:** `scripts/focus_runner.py` accepts only OK/fresh Risk, persists `focus_state.json` first, then `focused_market.json` view. Focus engine takes first valid Risk PASS as baseline `LOCKED` **0/3**. Three *subsequent* canonical events each ≥60s event-time from last counted evidence advance WAIT 1/3 → WAIT 2/3 → READY, with strictly increasing cursor and new ID. Challenger never steals active focus; hard errors invalidate.
5. **Book:** `scripts/book_worker.py` fetches a GET-only CLOB order book only on current Focus READY view; `scripts/book_engine.py` requires matching present `asset_id` (outcome token) and `market` (condition), noncrossed bids/asks, adequate requested depth. A Risk PASS alone cannot perform a Book request.
6. **Paper:** `scripts/paper_worker.py` consumes only explicit fresh OPEN/CLOSE request files, binds Focus+Book generation, token, condition, evidence and Focus lock, re-fetches Book and analyses depth/fees, verifies exact CTF/position identity and finalized-onchain `UNRESOLVED` status before virtual entry. It rechecks both gate and request identity after network work and before an OPEN. Durable Paper state is idempotent for replay of processed IDs. No wallet signing or live order endpoint is present in this Paper path.

## 3. Independent security review

### S1 — Focus exact outcome/condition
The READY-1 correction in `focus_engine.step` checks locked condition/outcome on every later same-token Risk row *before* consuming evidence; mismatch invalidates with `IDENTITY_MISMATCH`. Token key alone had been insufficient. The Risk input itself requires canonical evidence triple, market-active/accepting, BUY, freshness and verification; all identity and replay constraints are tested. Persisted Focus authority loads fail-closed on corrupt state; it does not re-lock on a restart.

**Residual:** `focus_engine._cursor` alone is permissive when handed fake records, but the upstream Risk canonical evidence contract is strict; keep both as separate security boundaries and do not expose unvalidated external Focus input. The locked condition/outcome casefold comparison is a match check, not an EVM normalization algorithm.

### S2 — CLOB identity
READY-1 patch in `book_engine.analyze_book` blocks both *missing* and *mismatched* Book `asset_id` / `market`. Public CLOB GET `/book?token_id=...` is the intended read route; Polymarket's published Python client GET uses the token parameter. CLOB summary schema describes `asset_id` as token and `market` as condition; this makes the strict check appropriate rather than mere mock-specific policy.

Code references: `scripts/book_engine.py`, `scripts/book_worker.py`, tests `test_book_worker.py`, `test_book_engine.py`, `test_general1_ready1.py`.
Reference: https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/client.py ; https://www.polytest.io/docs/polymarket-api/clob/get-book .

**Residual:** `book_engine` does not independently prove the age of the CLOB `timestamp`, and any fetched HTTP response can be out of date due to remote caching. This remains an explicit release check; avoid guessing the timestamp's unit or fabricating a freshness guarantee.

### S3 — Paper execution and races
READY-1 patch provides a second Focus READY/Book PASS/request match read after refresh Book, fee and settlement checks. Negative fixtures confirm Focus WAIT or INVALIDATED, changed Book evidence and changed request identity reject OPEN. Same-token duplicate requests cannot create two sequential OPEN positions. Restart reloads processed request IDs and positions from disk.

**Residual: not a fully atomic cross-process transaction.**
- A writer can change `focused_market.json` or `book_assessment.json` **after** final gate recheck but before `paper_state.json` commit.
- The final recheck uses the `now` value passed to the worker at run start; long network calls are not re-aged against a new wall clock.
- `focus_runner` publishes durable `focus_state.json` before its derived `focused_market.json` view; the paper consumer reads the latter, and an interrupted publication can leave it temporarily behind the durable engine state. There is no two-file generation/CAS lock.
- Two *concurrent independent* Paper workers (rather than the one supervised worker) could race on request IDs/state; current tests demonstrate idempotency under sequential restart/replay, **not** a global multi-process uniqueness guarantee.

Because **all operations are virtual Paper requests**, not real orders, this is a disclosed integration risk requiring explicit review before widening the system to actual execution. Never claim these tests mathematically eliminate the race.

### S4 — Collector: gamma batching and canonical checkpoints

`COLLECTOR_GAMMA_BATCH_LOOKUP=1` is opt-in; unset/default remains **OFF**. `_prefetch_legacy_assets` validates blockHash vs canonical header and exact taker-exchange context, dedupes unknown CTF token IDs, splits into ≤12-token Gamma batches, checks response shape/truncation/identity, queries closed only for unresolved, and falls back to strict singleton lookups. V3 uses separate `decode_position_id` and Gamma→Data API route; never batch V3 as CTF. Cache entries are published only after successful prefetch; unsatisfied requests raise and must not commit the current scan.

`scan_once` rechecks the last canonical block header before SQLite commit. DB commit precedes compatibility JSON publication. Canonical trade uniqueness remains `transaction_hash + log_index` and exchange-aware protocol context, under existing `collector_storage_v4` and the Collector regressions.

**Important explicit coverage policy:** unsupported V3 position classes are deliberately excluded from the binary decision pipeline with diagnostic counters; this is NOT evidence that every Polygon exchange event was recorded. The old 300s run used 30-block initial lookback and 20 confirmations; it proves near-tip catch-up *under those settings* only, not complete backfill, refresh stability or full V3 protocol coverage. Any Gamma API omissions resolved by singleton lookup are safe only if the singleton truly proves exact identity; ambiguous/unknown must fail closed.

Gamma batching filter arrays are documented by third-party API descriptions, and supported empirically by the 300s isolated run, but the live endpoint's changing semantics and result limits should remain an integration monitoring concern.
Reference: https://haut.github.io/polymarket/polymarket/Polymarket/Gamma/index.html .

## 4. Review gates and operational limitations

- No default batching promotion in this PR; normal `run_machine.py` stays unchanged, so the prior performance improvement is **not automatically active** after merge.
- No live Gamma/Polygon/CLOB request or real Order/Wallet interaction performed under this review. Remote API data and timestamps cannot be certified by mock tests.
- No existing database/sandbox touched or migrated. A future approved live check must create a unique fresh sandbox and use externally supplied RPC config.
- Known residual Paper view/atomicity and Book timestamp issues must be documented and explicitly accepted or separately hardened before any broader production deployment.
- The integration PR is a *review candidate*, not a claim of production readiness, full protocol coverage or live paper profitability.

## 5. Decision criteria

See `GENERAL1_INTEGRATION_REVIEW1_TEST_REPORT.md` for exact CI run and pass counts. **Recommended decision: READY_FOR_MAIN_INTEGRATION_REVIEW** *only when branch-HEAD offline CI succeeds*. This means a narrow PR directly against main is ready for independent human review, NOT approved for merge/real-money execution.

If CI fails: `BLOCKED_BY_REGRESSION`; if the review requires atomic multi-process authority before permitting Paper even in a controlled sandbox: `BLOCKED_BY_SECURITY` pending separate lifecycle architecture work.
