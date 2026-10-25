# GENERAL1 QUOTE FRESHNESS AUDIT
Independent offline-only source and execution-clock review

## Scope and provenance

- Repository: mudrjafar/PolymarketResearch
- Fixed starting commit: PR #49 `96a1ccf8fc0e65e3ddf06c7d0efcb60a4f2460b9`.
- Actual review: scripts/paper_worker.py, paper_engine.py, book_engine.py, book_worker.py, focus_runner.py and machine_common.py, together with the Focus/Book/Paper offline regression tests.
- Mode: official-public-documentation and read-only SDK-code research, deterministic offline tests, minimal branch patch. NO network calls to the CLOB/RPC, wallet signing, manual READY in production, real orders, or merge.

## Evidence: what the timestamp does and does not prove

1. Official Polymarket Python SDK: https://github.com/Polymarket/py-sdk/blob/main/tests/unit/test_frames_order_book.py, `timestamp_ms=1_700_000_000_000` is converted to 2023-11-14 22:13:20 UTC. So this code consumes the OrderBook timestamp as **Unix epoch milliseconds**.
2. Official Polymarket TypeScript SDK: https://github.com/Polymarket/ts-sdk/blob/main/packages/bindings/src/subscriptions/clob.ts, the market book event payload types its timestamp as `EpochMilliseconds | null`.
3. Official Polymarket agent skills: https://github.com/Polymarket/agent-skills/blob/main/websocket.md, documents market-channel `book` events on subscription and when a trade affects the book, and separate `price_change` events on order placement/cancellation. It lists hash and timestamp as book event fields.
4. The official TypeScript CLOB client calls GET /book by token and returns an OrderBookSummary without adding a local request-completion timestamp: https://github.com/Polymarket/clob-client/blob/main/src/client.ts.
5. The official Python CLOB client uses the `timestamp` property in a hash representation of the server-generated orderbook: https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/utilities.py.

**Established:** milliseconds, source book-event/generation-related data, identity and hash fields; an HTTP response has a distinct local receipt time. **NOT established by these sources:** that GET /book rewrites its embedded source timestamp on each HTTP request, that a quiet market always has a recent timestamp, cache freshness guarantees, latency SLA, or a cryptographic proof of present executable depth. HTTP-200 plus well-formed identity is NOT an execution guarantee.

A quiet book may therefore plausibly be returned unchanged with an old embedded generation time even when freshly fetched. This is a risk model/inference, NOT a confirmed empirical REST observation; no live API check was performed.

## Confirmed bugs in PR #49

- `paper_worker._run_once_unlocked` created one `now` at tick start, then `_process_close` and `_position_mark` checked remote quote timestamps against this old time. If a fresh quote arrived after >5s of earlier work, it could look more than 5s in the future. Conversely a source quote could age without a final CLOSE recheck.
- Successful MARK could persist an old `mark` object even after a subsequent failed refresh, potentially inviting callers to treat the previous P&L as current. `mark_status` was changed but the last `mark` object remained.
- PR #49's OPEN final commit already rechecked the source timestamp but not a separately recorded LOCAL response-receipt age.

## Implemented narrow correction

- Preserve a **fail-closed source timestamp gate**: 13-digit epoch milliseconds, max 30s behind validation clock and 5s allowed future skew. These are CONSERVATIVE LOCAL PAPER POLICY choices, not official Polymarket guarantees. Does NOT automatically upgrade an old book to PASS because a recent HTTP response was received. This may reject valid unchanged quiet-market books; count and analyze `BOOK_TIMESTAMP_STALE` failures before declaring natural READY+Paper availability.
- `paper_engine.validate_book_receipt_age` separately validates recorded local receipt timestamps, max 30s receipt-to-commit age, at most 5s forward clock skew. This does NOT attest to remote depth.
- OPEN captures `entry.book_received_at` immediately after its book fetch and rechecks both source time and local receipt age under the existing Focus/Book/Paper final commit lock.
- CLOSE captures `exit.book_received_at` immediately after fetching bids, with a new clock sample after the fetch, not tick-start `now`. A new Paper-only commit fence verifies source and local receipt age before the durable state save. If stale, CLOSE event becomes a REJECTED event, original OPEN position is restored, and stale MARK data are invalidated. CLOSE never requires Focus READY.
- MARK is assessed against receipt time and revalidated at Paper commit. Failed or timed-out MARK refresh clears the old mark rather than leaving old P&L inside a non-OK mark.
- The existing Paper file lock serializes state/request handling. The shared Focus lock still protects only final OPEN authorization and publication; new network calls and lock order were NOT introduced.
- Request-opened_at and other existing legacy fields remain. New optional `book_received_at` per position entry, exit and mark are provenance; no schema version bump or migrations of existing position records are needed for future OPEN/CLOSE only.
- The worker uses injectable `clock()` to replay elapsed wall-clock time. Production uses aware UTC; deterministic tests use explicit times. A monotonic latency timer or a real-server time attestation was not implemented.

## Risk boundaries and decision

The strict 30s exchange-source age policy is intentionally conservative until actual provider semantics can be established in an independently authorized sandbox. It is safe in the sense of **refusing to fabricate simulated execution from uncertain evidence**, but may cause false BLOCKs or even few/no simulated trades in inactive markets. An official real-time execution or HTTP freshness guarantee has NOT been proved.

The final source+receipt checks reduce TOCTOU risk in a single-process/local-volume Paper simulation; they do not create an atomic transaction against a changing external CLOB or guarantee that liquidity is still there. F3 event-journal and F4 request retention are separately documented.

**Assessment:** ready for additional INTEGRATION REVIEW and later explicitly authorized isolated sandbox tests, NOT proven production/live-trading ready.
