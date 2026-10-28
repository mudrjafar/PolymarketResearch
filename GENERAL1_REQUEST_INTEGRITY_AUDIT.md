# GENERAL1_REQUEST_INTEGRITY_AUDIT
## Independent source audit, red-to-green reproduction and minimum patch

**Repository:** `mudrjafar/PolymarketResearch`  
**Base:** PR #50 commit `5570535a86e7c02b937c2ab6747e05083ffc78ea`  
**Patch branch:** `fix/general1-request-integrity-final-20261028`  
**Mode:** isolated offline Python security fix, never merged; no live RPC/CLOB, real order or production data.

## A. Actual request lifecycle (source-derived)

1. Telegram `submit_open_request` and `submit_close_request` generate randomized `REQ-<uuid-prefix>`, an aware UTC `requested_at`, and write JSON atomically under `data/paper_requests`.
2. Paper `_load_requests()` reads the directory once in the serial `PaperWorkerLock` critical section; JSON payload is thereafter an in-memory snapshot, not a live view of later file edits.
3. `_run_once_unlocked()` validates request id/schema/time/action, processes each request and records its ID for deduplication. OPEN calls `read_execution_gate`, then a fresh external book, identity, full-depth fee simulation and settlement checks.
4. `_process_open` appends an OPEN position **tentatively**; it is not durable nor authoritative until the final shared Focus lock validates READY, Book binding, request time, CLOB source book age and local receipt age and atomically writes `paper_state.json`.
5. CLOSE can close a position committed in an earlier invocation independently of current Focus READY, subject to strict book identity, depth/source age, receipt-age and end-of-tick CLOSE rollback checks.
6. After durable Paper JSON state save, Paper appends `paper_events.jsonl`; restart keeps committed state and processed-ID information, but does **not** restore an event lost to a post-save crash (known F3). Processed IDs are bounded, unordered at the long-term 1,000-entry retention boundary (known F4).

## B. Reproduced failures on unmodified PR #50 logic

Red CI on **test-only** branch commit `1e93c3c8adccdcbd1d127a99f1ff85da9dd744df`: https://github.com/mudrjafar/PolymarketResearch/actions/runs/40677813722
**4 expected failing security regression tests, 309 other focused tests pass**:

- F50-01: randomized alphabetical filename ordering processed a *later* timestamp first, allowing the wrong OPEN of two valid requests to win the same Focus.
- F50-02a: a user CLOSE referencing a tentative OPEN in the same tick could create CLOSED status without that OPEN having passed final shared-lock authorization.
- F50-02b: if Focus changes during the tick, the tentative OPEN is rejected; the derived CLOSED event was filtered by paper_id while both request IDs were marked processed, permanently hiding the CLOSE result.
- Date parse: unparseable requested_at was reported as generic REQUEST_STALE rather than REQUEST_TIMESTAMP_INVALID, preventing operators from distinguishing malformed vs expired requests.

All four cases were reproduced by the real `paper_worker.run_once` and isolated on-disk test fixtures, with remote APIs mocked.

## C. Minimal implementation / authority contracts

**Ordering:** `_request_timestamp` parses only nonblank timezone-aware ISO UTC/offset strings, normalizes to UTC, fails closed for naive/invalid values. `_load_requests` sorts by **(valid timestamp ascending, request_id)**; invalid/corrupt timestamp files sort after valid ones and are then rejected, in deterministic request-ID order. Equal timestamps have a stable request-ID tie. The submitted timestamp is **not** cryptographic or a cross-host total-order guarantee; it is sufficient for an honest single-machine Telegram sender using wall-clock UTC and a serial Paper consumer.

**Tentative vs committed:** `tentative_open_ids` tracks only OPENED events generated during the same tick. A CLOSE targeting any of those IDs is rejected with `POSITION_NOT_COMMITTED`, without book fetch or state mutation. A CLOSE for an earlier *durably committed* OPEN is unaffected, including when Focus subsequently becomes WAIT/INVALIDATED.

**Event audit and rollback:** When the final Focus READY or quote validation fails, only informational child events of the tentative OPEN may be discarded. Any user request event carrying that revoked paper_id gets a terminal `DEPENDENT_OPEN_NOT_COMMITTED` rejection, rather than silently disappearing. Just before each Paper state save, `_assert_terminal_request_events` checks that every consumed request ID has **exactly one** OPENED/CLOSED/REJECTED terminal event. If the invariant fails, the state save aborts with PaperStateError. This is **in-memory-before-save integrity**, not a transactional journal guarantee: F3 remains.

**No behavior changes:** Collector/Flow/Diamond/Risk/Focus conditions, Book math or matching, source/receipt age limits, fees, settlement authority and permissions are all unchanged. No new network or lock acquisition added. Telegram cannot override READY, and the Paper worker remains the only state writer under the existing `PaperWorkerLock`.

## D. Limits / trade-offs

- Request `requested_at` comes from Telegram's host clock and can be modified by a writer with filesystem access. The new timestamp ordering does **not** promise exact global causal ordering across multiple hosts and does not replace a durable queue sequence.
- Request file contents after the initial read are not reloaded during the tick; this is intentional immutable in-memory execution and prevents mid-flight edits changing an already accepted request. Idempotence is still bounded by the F4 retention policy.
- A malformed timestamp is rejected rather than guessed/converted to UTC. A future timestamp that is otherwise parseable is still refused under the existing freshness bound; no bypass.
- F3's post-state-save/pre-journal-append gap and F4's nondeterministic long-horizon processed-ID eviction remain unchanged. Do not use event journal alone as authoritative trading/P&L history.
- A source-valid orderbook remains only a *simulation*, not evidence a real order would fill. The conservative 30-second book source-age rule may exclude quiet markets.
- The dashboard still reads `paper_trades.json` instead of authoritative `paper_state.json`; Book/Telegram `READY_BOOK_PASS` is a preliminary gate, not Paper's final execution guarantee. These are **separate MEDIUM operational follow-ups**, excluded from the narrow security patch.

**Audit conclusion:** Request ordering and the tentative CLOSE/rollback loss are independently reproducible and are corrected under the stated local single-host assumptions. Offline tests and current CI support an independent final review, not a merge or live-test authorization.
