# GENERAL1 Request Integrity — independent audit and minimal patch

Repository: mudrjafar/PolymarketResearch
Base: PR #50 exact commit 5570535a86e7c02b937c2ab6747e05083ffc78ea
New branch: fix/general1-request-integrity-review2-20261028
Mode: sandbox-free offline source audit and GitHub Actions CI, no main or existing PR modifications, no actual CLOB/RPC calls, no real orders or automated paper trades.

## Confirmed pre-fix findings

F50-01: scripts/paper_worker.py::_load_requests used sorted(REQUEST_DIR.glob("*.json")), so random Telegram UUID request IDs, not submission times, chose processing order. Deterministic lexical order is not meaningful temporal order. A CLOSE request alphabetically preceding an earlier OPEN could receive OPEN_POSITION_NOT_FOUND; the reverse ordering could close a tentative OPEN before final authorization.

F50-02: _run_once_unlocked appended each _process_open tentative OPEN position in memory, and _process_close could CLOSE that tentative position during the same batch. Final READY validation could then reject OPEN and remove its position; the dependent CLOSE event could be silently filtered by paper_id even though its request ID had already been marked processed. Depending on ordering, this creates inconsistent events/accounting; **not** a proven real order or automatic trading.

## Narrow implemented protection

- _request_timestamp requires string, timezone-aware parseable ISO 8601 datetime; normalizes to UTC. Naive, absent, corrupt or unsupported timestamps are invalid.
- _load_requests uses chronological UTC requested_at with request_id as stable tie-breaker; invalid records sort last and are independently rejected REQUEST_TIMESTAMP_INVALID / REQUEST_CORRUPT.
- UTC submission timestamps are *untrusted user-originated metadata*: this provides a reproducible logical order for cooperative writer files, not a cryptographically guaranteed arrival-order or monotonic global sequence. Requests written or altered while _load_requests scans are not a transactional batch snapshot; the Telegram producer uses atomic file replacement, but a foreign writer is beyond this scope.
- committed_position_ids snapshots IDs from the previously durable paper_state.json **before** any tentative OPEN is produced. CLOSE referencing a merely tentative OPEN in the same cycle gets REJECTED/POSITION_NOT_COMMITTED. Existing committed OPEN positions can CLOSE regardless of current Focus READY. Previous SETTLED/CLOSED positions retain legacy rejection codes; do not redefine them as tentative.
- No changes to Paper wallet/real order paths (none added), Focus READY authority, Collector/Flow/Diamond/Risk, Book depth/identity, CLOB quote freshness, settlement finality, or OS locking.

## Verified invariants / limitations

Offline CI on implementation SHA 3bb7f860c5fc659fbe505920457574d2ea62c3ed: 540 full tests +61 subtests, 316 focused +61 subtests, Machine self-test PASS. Run https://github.com/mudrjafar/PolymarketResearch/actions/runs/40678540007.

New cases: reverse filename/UTC order, same-tick tentative OPEN/CLOSE, final locked READY invalidation preserving request event, invalid UTC timestamp, equal-timestamp stable tie order, two CLOSEs for one committed position, OPEN/CLOSE/OPEN triad. Existing suites cover duplicate request ID, worker restart/crash journal gap, final evidence and generation binding, insufficient depth, Focus revoked after earlier OPEN, paper-state lock and settlement.

Unverified: adversarial writers changing request files during a read, two distinct valid requests with exactly identical timestamp but true order opposite their IDs (tie-break is ID by definition), true cross-host chronological delivery, exact process-kill durability on Ubuntu 26.04 / Python 3.14 / Windows OS.

Persisting processed IDs before appending events remains the explicitly known F3 journal gap; last-1000 nondeterministic request-ID retention remains F4. No outbox/DB queue was introduced.
