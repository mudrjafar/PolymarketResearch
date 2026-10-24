# GENERAL1-READY-1 — Core integration and Focus-to-Paper audit

**Code authority:** `mudrjafar/PolymarketResearch`, feature branch `feat/general1-ready1-focus-paper-integration-20261025`, based on `feat/general1-gamma-1-bounded-metadata-20261025`. **No changes or merges to `main`.** Review the exact PR and commit SHAs before integration.

## Decision scope and prior observed facts

This is an **offline software-readiness** audit, not a claim that live markets reliably reach READY or that simulated positions have positive expected value.

Recorded real-data observation (GENERAL1-GAMMA-1, separate 300s sandbox, 2026-10-24 UTC):

- Result PASS; uninterrupted 300s; Polygon chain 137.
- Collector 0.753 blocks/s versus chain 0.670 blocks/s; 59 → 0 reported safe-tip lag; checkpoint age p95 107.104s.
- 4925 canonical V4 trades, 8 Flow VERIFIED, 1 Diamond, 1 Risk PASS, Focus LOCKED, Book NOT_READY, 0 Paper positions.
- Risk generation CURRENT_MATCH and real Trade→Risk sample binding observed.
- Zero critical-service restarts, zero retries, SQLite quick_check OK.

The live report proves the upstream pipeline reached LOCKED, **not** READY, Book PASS or Paper OPEN. Five minutes cannot establish long-term collector throughput or recurring full Gamma-refresh behaviour.

## A. Branch readout and merge boundary

At audit, `main` was `97d8813` and Gamma-1 was `65ab5ce`. GitHub compare: Gamma-1 is **70 commits ahead** of main, zero behind. It includes E2E2 and Freshness1 Collector/Flow/Diamond changes, test and instrumentation files, plus the opt-in Gamma batch implementation. **Cherry-picking the Gamma method without dependent Collector V3 canonical evidence behaviour is unsafe.**

The opt-in flag `COLLECTOR_GAMMA_BATCH_LOOKUP=1` is **not** enabled by default in `scripts/live_active_trades.py`. Thus merging the branch alone would not automatically improve normal-runtime throughput. That is intentionally conservative pending code review and canary approval. The successful Gamma sandbox explicitly set this flag and used 30-block near-tip lookback, 20 confirmations; it did **not** prove historical completeness.

Integration policy:

1. Keep this READY-1 PR **targeted at Gamma-1**, not `main`: isolates only the READY-1 hardening and tests.
2. After independent approval, plan a separate reviewed integration of all necessary upstream canonical Collector/Flow/Diamond dependencies into `main`, ideally by audited PR sequence rather than broad indiscriminate merges.
3. Keep one-shot clone launchers, near-tip settings, probes and private raw logs out of normal production startup policy.
4. Do not enable batching by default, real trading, user-triggered Paper OPEN or future large live tests in this task.

## B. Data and authority reconstruction

Actual runtime process order in `run_machine.py`:
`collector → flow → diamond → risk → learning → focus → book → paper → telegram → dashboard`.
The services are independent workers; list order is launch order, **not a synchronous transactional guarantee**.

1. `collector_storage_v4` and `scripts/live_active_trades.py` establish canonical chain/log evidence and publish a bounded V4 snapshot. DB checkpoint advances only after canonical headers/logs, identity resolution and successful write. Atomic JSON projection is downstream. Unknown asset identity must not advance the checkpoint.
2. `scripts/flow_tracker.py` groups by `condition_id + token_id + outcome` and publishes `evidence_id` (tx hash + log index), `evidence_cursor` ([block, log index]) and `evidence_at` (event time). Unlike a tick timestamp, cursor is grounded in blockchain evidence.
3. `scripts/diamond_filter_v3.py` builds immutable generation assets and publishes `diamond_generation.json` manifest. `scripts/risk_engine.py` binds *one* manifest generation; it loads that generation's archived Flow and Diamond and publishes `risk_assessment.json` atomically. Same-generation re-read does not synthesize new Risk results.
4. `scripts/risk_engine.assess` applies DIAMOND, BUY, active/unexpired, fresh source/trade, confirmed Flow, risk concentration and canonical evidence gates; `focus_candidate_result` copies the exact Flow evidence triple into Risk result.
5. `scripts/focus_runner.py` consumes the Risk snapshot if status OK/current, calls `focus_engine.step`, then atomically writes the durable `focus_state.json` before publishing informational events and `focused_market.json`. The state, not Telegram or dashboard, is Focus authority.
6. **Focus state contract:** first Risk PASS locks but contributes **zero** of the three post-lock confirmations. Each advancement needs a new `evidence_id`, strictly increasing `evidence_cursor`, Risk PASS and at least **60s blockchain event-time** since last counted evidence. Successive eligible events yield `WAIT 1/3`, `WAIT 2/3`, `READY`. Repeated snapshots and supervisor ticks add no confirmations. Stale Risk input does not advance Focus. Missing rows pause/timeout; hard blockers invalidate; challengers never displace an active Focus.
7. `scripts/book_worker.py` reads `focused_market.json` and calls GET-only CLOB `/book` only when the current Focus view is READY and input status OK; stores a fresh bound Book assessment. `scripts/book_engine.py` checks exact CLOB token and market IDs, valid asks/bids and sufficient depth for the configured notionals. Book does not create Paper requests.
8. `telegram_bot.submit_open_request` requires interactive Paper amount selection ($25/$50/$100), reads Focus and Book, binds generation/evidence/token/condition/locked_at and atomically writes `data/paper_requests/REQ-*.json`. This is the user-control boundary. The dashboard is a read-only summary.
9. `scripts/paper_worker.py` only acts on an explicitly submitted and fresh Paper request. The OPEN gate rechecks authoritative Focus READY and Book OK/PASS with matching generation, evidence, token and condition, and validates the request's locked_at binding. It fetches the current CLOB book again, re-analyses exact entry notional and depth, obtains fee parameters and on-chain outcome/settlement identity, and requires clean `UNRESOLVED` settlement status. Then it records fee-aware entry and marking in `paper_state.json`; repeated request IDs are idempotent. No live order signing or real exchange order submission exists in this request path.

## C. Two demonstrated safety defects corrected

### C1: same-token changed-market evidence could increment a pre-existing Focus

**Before:** `focus_engine._build_current_by_token` and the active-focus selection matched only token ID. An individually valid Risk PASS for the *same token* but different `condition_id` or `outcome` could be treated as another confirmation for an older locked Focus, because the Focus engine did not check those canonical identity fields against the locked snapshot.

**Fix:** Before neutral/soft-fail/evidence processing, compare the incoming condition and outcome against the locked Focus, case-insensitively for canonical text representations. On mismatch **INVALIDATED (IDENTITY_MISMATCH)**; no advancement, no immediate challenger takeover. Existing exclusive-focus policy unchanged.

**Tests:** `test_focus_cannot_confirm_different_condition_under_same_token`, `test_focus_cannot_confirm_different_outcome_under_same_token`; both use independently Risk-PASSable market rows so this is not a contrived already-blocked input.

### C2: absent CLOB market or asset identity could still obtain Book PASS

**Before:** `book_engine.analyze_book` rejected ID *mismatches* if optional CLOB `asset_id` and `market` were present, but did not reject missing identifiers. A depth-filled Book payload without either ID could pass.

**Fix:** Require both CLOB `asset_id` and `market` as well as Focus token/condition; absent identity produces `TOKEN_ID_MISSING`/`CONDITION_ID_MISSING` and `book_ok=false`. Mismatches remain hard blockers. No liquidity or strategy thresholds changed.

**Tests:** both absent-identifier cases, `book_worker.run_once` output, and `paper_worker.read_execution_gate` rejection.

### C3: READY could be revoked while Paper was fetching fresh data

**Before:** `paper_worker._process_open` checked Focus READY and Book PASS before multiple HTTP/settlement operations, then could append a new virtual position without rechecking. If Focus became WAIT/INVALIDATED during the Book request, a Paper OPEN could still be committed from the earlier allowed snapshot.

**Fix:** Re-run `read_execution_gate` and `_matches_open_request` immediately before the virtual OPEN append. If Focus/Book or generation/evidence/request binding changed, emit a rejected event with `EXECUTION_GATE_CHANGED_*`, no position created. Network and settlement authority are unchanged.

**Regression:** `test_focus_revoked_during_book_fetch_blocks_paper_commit` mutates the Focus file *within the mocked Book loader*, after the initial gate check, and verifies rejection.

## D. Important residual limits (not hidden by a PASS)

- No sustained live readiness/market prevalence evidence: the Gamma run had Focus LOCKED, not READY. **One Risk PASS is not enough**. Future live READY requires three subsequent qualifying blockchain events spaced ≥60s. Failure to reach READY within 300s need not be a bug.
- The collector batching remains opt-in; historical backfill is not complete. Gamma metadata batching supports only legacy CTF here; V3 remains strict nonbatched.
- The current Book freshness check covers a timely fetched and published response; it does **not** independently establish freshness from the CLOB `timestamp` field, whose semantics must be validated against real API data before claiming an orderbook-timestamp proof. Paper still re-fetches and re-analyzes the current HTTP response before OPEN.
- Separate runtime workers and JSON snapshots are not a single atomic transaction. The additional final Paper-gate recheck closes the known *during-network-fetch* window, but cannot mathematically eliminate a concurrent mutation **after** that last read and before disk commit without a shared lock or CAS. No real money is at risk in Paper mode; full cross-process atomicity hardening is a separate release decision.
- Offline mocks prove function contracts, not remote service availability, native V3 execution or final settlement results.
- No new user-authorised Paper OPEN has been observed using *real* fresh-market READY evidence. The offline OPEN is synthetic and confined to isolated pytest fixtures.

## E. Acceptance and decision

**`READY_FOR_CONTROLLED_PAPER_TEST`** for a **reviewed, isolated, user-invoked paper-only sandbox test**. This means:

- genuine Risk-assessed multi-evidence Focus READY works offline;
- the actual Book worker and Paper worker accept correct bound inputs and reject unsafe/unknown states;
- there is no need to weaken Risk/Focus gates or fabricate a live READY event;
- outstanding review is required before the branch is a production baseline.

**Not authorised:** merge to `main` automatically, trade real funds, generate Paper OPEN without a user request, or claim a real-world READY market has passed the full execution path.

The next operational step **after branch review** is passive natural-READY observation on a fresh isolated, fully consented run; only once Focus READY is observed naturally may the user explicitly choose whether to request a Paper OPEN. No new live test was run under READY-1.
