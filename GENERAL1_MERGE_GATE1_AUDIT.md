# GENERAL1-MERGE-GATE-1 — Independent Integration & Security Audit

Repository: `mudrjafar/PolymarketResearch`. Reviewed on 2026-10-25.
Authority: current GitHub `main` `97d881311a68c54ec15a4f537d8a055e04729f44`, Draft PR #45 head `cdbec6bea8d6f83040c5822f0578e23c4d7a12e5`, Draft PR #46 head `a1f9de308d5029c722b6a51ca933a9748421f95e`. The consolidated branch `feat/general1-merge-gate1-consolidated-20261025` descends from PR #46 (which itself descends from PR #45) and adds only the Paper-exclusive-writer correction, regressions and release documentation. The old PRs were not modified or merged.

## A. Independent review of PR #46 (Durable Focus authority)

**Contract verified:** `scripts/focus_engine.py` declares `STATE_VERSION=3` and `new_state()` stores `{"version":3,"focus":...}`. `scripts/focus_runner.py` declares `RUNNER_SCHEMA_VERSION=1`; `_state_wrapper` writes `schema_version`, `updated_at`, `last_source_generation_id`, `engine_state`. `_view` projects the same engine focus into `focused_market.json`, with `generated_at=now.isoformat()` and `source_generation_id` from the same Risk snapshot.

**Crash window:** `focus_runner.run_once` persists `focus_state.json` before publishing informational events and `focused_market.json`. An interruption between them can leave an old, still-fresh READY view. PR #46's `paper_worker._durable_focus_gate` requires both views to agree before a Paper OPEN: durable schema 1, engine version 3, durable focus status READY, identical full focus object, identical generation and identical update/generation timestamps. Missing, corrupt or mismatched durable state is rejected. Paper also rechecks this gate after refreshed Book/settlement calls before virtual OPEN.

**Outcome/token/condition/evidence:** full focus-dict equality protects locked token, condition, outcome, last evidence/cursor and lock timestamp; Paper's `read_execution_gate` additionally binds Book token, condition, evidence ID and generation ID and `_matches_open_request` binds the submitted request. This is stronger than token-only projection binding. Because the check is read-only, Paper CLOSE and ordinary on-chain settlement evaluation do not depend on Focus READY.

**Tests:** `tests/test_paper_worker.py` missing/mismatched/revoked Focus scenarios, CLOSE with revoked durable authority; `tests/test_general1_ready1.py` torn Focus publication and negative execution gates; `run_machine.py --self-test` Paper worker.

**Residual:** the verification and state commit are not a single transaction across Focus and Paper writers. If the Focus state changes after Paper's final validation but before its own disk commit, the Paper-only simulation can still reflect a race. Claiming absolute atomicity would be false. The durable check also conservatively rejects OPEN when Focus runner updated only the compatibility view after a non-OK Risk read, even if durable engine state still has READY; this is a safety-preserving false negative.

## B. Multi-worker and request concurrency

`run_machine.py` defines exactly one supervised critical `paper` service, but the code previously did **not** prevent an operator or second supervisor from launching another `paper_worker.py`. Both processes could read the same `paper_state.json`, independently simulate OPEN and race to replace it, violating processed-ID and position exclusivity.

**Demonstrated design defect / minimal correction:** the consolidated branch adds `PaperWorkerLock` based on existing cross-platform `collector_storage_v4.bridge.CollectorLock`, using a **dedicated `paper_state.lock`** (derived from the configured Paper `STATE_FILE`). `run_once` now wraps the *entire* read → settlement refresh → explicit request consumption → Paper state atomic save → downstream learning queue cycle in this OS advisory file lock. On collision it raises `PaperStateError` before any state read/write or remote fetch. The lock is released on normal exit/exception and by the OS on process death; its presence on disk is not itself a stale-lock indicator. This affects Paper only, never Collector's lock.

**Tests:** a second worker calling `run_once` while the lock is held is rejected without writing state; following release, one valid OPEN succeeds and a repeated tick cannot duplicate it. Separate tests verify two distinct explicit requests for an already-open token cannot create a second sequential position. Self-tests PASS.

**Remaining boundary:** the lock serializes Paper writers that run this code and use the *same physical STATE_FILE/lock path*. It cannot serialize rogue writers bypassing the lock, workers on separate data directories, or Focus and Book writers, which do not take the Paper lock. The tests establish nested lock contention using the same OS lock primitive; they do not prove every possible multi-host filesystem and race schedule. Atomic save protects the individual JSON file from partial content, not transactions across multiple files. Paper request files are individually atomic but multiple request files remain a queue.

**Restart:** durable Paper positions/processed IDs are reloaded and re-processed request IDs are skipped. A crash after a committed Paper state but before events are appended may lose that event (not duplicate the entry). A crash before the Paper state commit may require processing again. A lock-free duplicate is prevented for cooperating workers.

## C. Collector / Flow / Diamond / Risk / Focus / Book / Paper integration

GitHub compare `main` → #46 changes six original runtime modules: `scripts/live_active_trades.py`, `scripts/flow_tracker.py`, `scripts/diamond_filter_v3.py`, `scripts/focus_engine.py`, `scripts/book_engine.py`, and `scripts/paper_worker.py`; PR #46 adds only Paper Focus-state checks/tests. The new consolidated branch retains these changes, now with the exclusive Paper writer.

Existing untouched authorities on `main`: `collector_storage_v4/*`, `scripts/risk_engine.py`, `scripts/risk_worker.py`, `scripts/focus_runner.py`, `scripts/book_worker.py`, `scripts/paper_engine.py`, `scripts/paper_settlement.py`, `run_machine.py`, Telegram and Dashboard. No normal runtime worker autogenerates a Paper OPEN: only explicit user request files are consumed.

Safety properties (offline-verifiable):
- Canonical Collector uses chain 137, exact header/hash and log identity, `transaction_hash+log_index` dedupe and fail-closed SQLite checkpoints; V3 unsupported classes are intentionally excluded and must not be silently mislabeled CTF.
- Gamma legacy CTF batching is **off by default**, guarded by `COLLECTOR_GAMMA_BATCH_LOOKUP=1`, with capped batch and singleton fallback. Its past 300s near-tip throughput test does **not** prove complete historical/V3 coverage.
- Flow derives confirming evidence ID, cursor and event timestamp from a single canonical event; archived Diamond/Flow generation binds Risk.
- Focus starts LOCKED 0/3; only three further qualifying distinct, ordered, ≥60s-spaced canonical events can cause READY; wrong condition/outcome under the same token invalidates.
- Book requires actual matching CLOB `asset_id` and `market`, correct Focus generation/evidence and sufficient executable orderbook depth.
- Paper OPEN requires explicit amount/request, fresh READY+Book PASS, precise bound identities, current refreshed Book depth, fees and finalized-onchain UNRESOLVED settlement result, then final durable authority recheck. No live order endpoint is used.
- CLOSE and settlement continue even after Focus invalidation.

## D. Known residual issues and classification

1. **Focus↔Paper non-atomic window** — residual cross-process TOCTOU after final check, confined to virtual Paper mode. **Requires separate CAS/transaction protocol** if zero residual race is mandatory. This task deliberately does not invent a heavyweight shared-state coordinator.
2. **Request/event crash consistency** — state write precedes event append. If interrupted between, processed state is authoritative but informational event may be lost; not an unintended extra OPEN.
3. **Snapshot time during network round trip** — `paper_worker.run_once(now)` holds one logical now across its operations; actual external latency can consume input freshness budget. Worker re-fetches Book, but the risk should be revisited before broader unattended simulated deployments. CLOB `timestamp` semantics and remote caching are not independently certified.
4. **Historical coverage and real READY** — unsupported V3 classes, Gamma API metadata limits, recurring refresh, long-run catch-up, real-world READY frequency and real CLOB/settlement success remain unproven by offline CI. No live tests were run for this task.
5. **Supervision topology** — default `run_machine.py` launches one Paper service, with the new lock preventing an additional *cooperating* instance from overwriting its state. Independent worker deployments to *different data roots* are outside this protection.
6. **Integration release** — all branches/PRs are Draft or feature-only. No merge to `main`, no auto-merge, wallet keys, real orders, DB migration or synthetic live READY.

## E. Decision

**READY_FOR_USER_MERGE_APPROVAL** for an *offline-validated, consolidated, main-targeted Draft PR*, provided final branch-HEAD GitHub Actions is SUCCESS. It is **not** a certificate of fully atomic paper execution or a green light for real-money or autonomous trading. A human must explicitly approve the PR and accept or separately mitigate the documented Paper-only residual race before any integration.
