# GENERAL1-FINAL-GATE-1 — Independent Concurrency & Freshness Audit

**Repo:** `mudrjafar/PolymarketResearch`
**Date:** 2026-10-25
**Original main:** `97d881311a68c54ec15a4f537d8a055e04729f44`
**Original PR #47:** `5f27710ee3761a22019488dd218adb93656a1f0d`
**Patch branch:** `feat/general1-final-gate1-atomic-paper-20261025`
**Mode:** strictly offline, no main merge, no production DB, no RPC/CLOB access.

## 1. Source-level authority trace and reproduced defect

On PR #47, `scripts/focus_runner.py::run_once` loads the durable Focus V3 state, computes its successor, and writes `focus_state.json` and then `focused_market.json`. The latter is a *compatibility projection*, not authority. `scripts/book_worker.py` reads this view and publishes a generation/evidence-bound `book_assessment.json` after a network Book fetch. The Paper worker (`scripts/paper_worker.py`) reads these views, checks durable Focus and Book binding, then performs CLOB/settlement calls.

**Original deterministic race:** PR #47's `_process_open` rechecked READY immediately after network/settlement calls and appended a *tentative* OPENED position to an in-memory `state`. After that function returned, `_run_once_unlocked` still refreshed settlements, performed network Book MARK for open positions and **only then** wrote `paper_state.json`. Thus a cooperative Focus worker could invalidate READY between `_process_open`'s final gate and the later Paper state save; the stored state would contain an OPEN despite the intervening Focus invalidation. This is a genuine interleaving exposed by source order, not evidence of an actual live trade.

**Original freshness issue:** Paper reused `now` captured at the start of the worker tick for its final `read_execution_gate` after network activity. A focus view, Book assessment or user request that became stale during network operations could be treated as fresh at commit.

## 2. Minimal corrections

### A. Short shared Focus/Book/Paper commit lock

Reuse existing, cross-platform `collector_storage_v4.bridge.CollectorLock` (advisory OS lock) on a dedicated `focus_state.lock`, derived from the Focus state path. The *same file path* is used by:

- `scripts/focus_runner.py::run_once`: acquire before loading durable state and evaluating next transition; write both `focus_state.json` and derived `focused_market.json` under the lock. Release **before** emitting informational Focus events / Learning queue, so no external/observational side effects are under the shared lock.
- `scripts/book_worker.py::_publish_book`: publish `book_assessment.json` under this lock, after remote Book fetch and Book analysis. No CLOB request is made while holding this shared lock.
- `scripts/paper_worker.py::_run_once_unlocked`: after all Book and settlement work, for tentative OPEN entries acquire the shared lock; obtain **commit-time wall-clock**; re-read the Focus/Book/durable-authority gate and the explicit request binding; atomically write `paper_state.json` **before releasing** the lock. Any missing, stale, mismatched or invalidated gate revokes the tentative OPEN and replaces its OPENED event with a REJECTED event; linked provisional settlement events are dropped. No outbound network/learning calls are made under the shared lock.

The existing `PaperWorkerLock` still serializes the *entire* Paper-state load/process/save cycle against a second cooperating Paper worker on the same state file. Lock acquisition is **nonblocking and fail-closed**; no attempt to wait indefinitely for the Focus writer.

**Lock order:** Paper lock → shared Focus/Book commit lock, while Focus/Book independently acquire only the shared lock. No reverse lock acquisition was introduced.

### B. Fresh clock at final commit

`paper_worker.run_once` defaults to a real current UTC clock at final commit if called without a test timestamp. A `now` explicitly passed by the tests remains deterministic; a separately injectable `clock` callback supports a precise simulated elapsed-time interleaving. Under the final shared lock, Paper rechecks Focus view freshness, Book snapshot freshness and user-request freshness against `commit_now`. A network callback that crossed the 120-second freshness limit results in a rejected simulated OPEN.

No CLOB source timestamp units are guessed or used as authoritative freshness evidence; separate verification is still required. The existing Book re-fetch and finality/settlement checks remain unchanged.

## 3. Deterministically scheduled interleavings

The new offline tests in `tests/test_general1_ready1.py` and `tests/test_paper_worker.py` exercise:

1. Actual `focus_runner.run_once` writes an INVALIDATED state after Paper's early gate and before final Paper commit. **Result: final gate rejects, no Paper position.**
2. A barrier interrupts Paper at the **final state save** (after the final gate) while a Focus transition attempts to invalidate READY. The shared lock prevents Focus from writing until Paper's commit completes. After release, Focus invalidates. **Result: serialized order, not OPEN-after-invalidated.**
3. Book generation changes during Paper's later MARK/book fetch. **Result: final gate rejects.**
4. Book writer tries publishing during Paper's final authorization. **Result: shared lock prevents publication until Paper commits.**
5. Slow remote calls simulated by advancing `clock` by 121 seconds from the READY/Book/request snapshots. **Result: final gate rejects stale input.**
6. Two concurrent Paper workers coordinated with `threading.Event`; second fails exclusive-state lock before reading/writing. **Result: no duplicate OPEN.**
7. Crash injected after durable Paper save but before event journal append. On restart the processed request remains durable. **Result: no duplicate OPEN; informational event may be lost.**
8. Existing READY-1/merge-gate tests: wrong identity, missing/corrupt durable Focus, torn projection, settlement unknown/complete, stale Book, request replay, CLOSE after invalidation.

All synthetic Evidence/Focus fixtures exist only in test temp directories. Tests exercise actual runtime functions but remote market information, CLOB and settlement readers are mock boundaries.

## 4. Guarantees and explicit limitations

**What has been proven by the tests:** cooperating Focus, Book and Paper processes writing within the **same local data directory and shared advisory lock** cannot transition committed Focus READY or publish a changed Book snapshot during the final Paper gate + Paper file commit. A second compliant Paper writer is excluded. Freshness is rechecked at commit-time; no auto Paper OPEN exists.

**What remains unsupported/unproven:**
- Processes bypassing these locks, separate data roots, unreliable distributed/network filesystems, and non-cooperating manual file edits are outside the locking contract.
- `PaperWorkerLock` (pre-existing) still surrounds Paper's full network-containing run loop. Its bounded HTTP calls and external handlers may delay another Paper instance; the **new shared Focus/Book lock**, by contrast, is never held across RPC/HTTP. Unbounded custom test loaders are outside production assumptions.
- New Book/settlement information can become stale after network retrieval. This fix proves snapshot freshness at commit, not independent CLOB `timestamp` truth, latency-free quotes, or on-chain freshness throughout execution.
- `focus_state.json`, `focused_market.json`, Book and Paper are separate atomic files; there is no full multi-file transaction or crash-atomic journal. Focus state is published before view under the shared lock; a crash can leave a torn view. Durable/view equality makes Paper fail closed on recovery.
- If Paper state saves but the informational event append fails, state remains authoritative and replay is idempotent, but the audit event may not be present until a separate reconciliation is designed.
- Current `run_machine.py` starts one Paper worker by default. Multi-host workers, malicious writers or real-money execution are not authorised.
- Legacy CTF Gamma batching remains **opt-in**; earlier five-minute near-tip performance does not prove historical completeness, all V3 protocol families or ongoing RPC/Gamma correctness.
- Strategy thresholds, Focus 3/3 confirmation rule, settlement finality rules and Paper amount limits are unchanged.

## 5. Decision

**`READY_FOR_USER_MERGE_APPROVAL`** is appropriate *only as a narrowly scoped, offline-tested Draft PR review decision* if the final candidate HEAD GitHub CI is green. An explicit user approval of the precise consolidated PR and its HEAD is required before merging into main.

Not a guarantee of profitable signal quality, full distributed transaction atomicity, verified remote CLOB timestamp freshness, natural live READY, real-money execution or automatic simulated trades. The next user-authorised step after PR review is a **fresh isolated paper-only sandbox**, never production data reuse.
