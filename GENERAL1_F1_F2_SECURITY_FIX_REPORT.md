# GENERAL1 F1/F2 — Minimal Paper Execution Security Fix

**Base:** PR #48 at 2c4ff4aee030b21fbb3b636d9dfa64e4f21a9191.  
**Branch:** fix/general1-paper-f1-f2-20261025.  
**Mode:** Offline-only, no merge, no RPC/CLOB network tests or real orders.  
**Status:** READY_FOR_INDEPENDENT_REVIEW, not authorised for merge.

## F1 — Strict CLOSE/MARK market identity

The previous paper_engine.validate_book_identity accepted books missing asset_id and/or market, because comparisons ran only when the fields were present. The helper now strictly requires nonempty expected token and condition and present, matching CLOB asset_id and market. This is consistent with the Book OPEN identity contract. No current Focus READY is newly required for CLOSE.

A missing asset or market now blocks simulated CLOSE and MARK. Previously opened positions are preserved, and realised P&L is not invented from unbound bids. Dedicated negative tests cover each missing field, CLOSE and MARK outcomes, and the pure helper.

## F2 — Real execution-quote freshness

First-party primary evidence: the official Polymarket Python SDK test at https://github.com/Polymarket/py-sdk/blob/main/tests/unit/test_frames_order_book.py uses timestamp_ms and checks that 1_700_000_000_000 converts to UTC 2023-11-14 22:13:20. The book response timestamp is thus represented as UNIX **milliseconds**, not seconds, in this primary code fixture. A live response has not been queried.

Conservative Paper-only limits (new policy, not vendor SLA): 13-digit decimal milliseconds, maximum source quote age **30 seconds**, maximum future clock skew **5 seconds**. Missing, malformed, old, or implausibly future timestamps block the operation.

- _process_open validates the raw book returned for the virtual purchase.
- _process_close and _position_mark validate their actual quote timestamps.
- At final OPEN commit, the same stored entry quote timestamp is rechecked against the fresh commit clock, independently from focused_market.json and book_assessment.json timestamps. Tentative OPEN is reversed if stale.
- Fees, settlement finality, Focus READY, identity and strategy thresholds are unchanged. No new remote requests or shared locks.

## Evidence / tests

Successful GitHub Actions workflow: https://github.com/mudrjafar/PolymarketResearch/actions/runs/40241906422, implementation commit 516792da90e898d17694f584bdc4c74d4dbc8d8e.
- Focused: 303 passed, 61 subtests passed.
- Complete pytest: 521 passed, 61 subtests passed.
- Machine self-test: SELF-TEST OK / ALL TESTS PASSED.

New negative cases include missing CLOB identity at CLOSE and MARK; missing, invalid, seconds-vs-milliseconds, 31-second old and 6-second future book timestamps; expiry after initial check but before final commit; and expired CLOSE quote. Positive tests and the built-in Paper self-test use valid millisecond book timestamps rather than the previously accepted placeholders "123"/"1".

## Remaining nonblocking technical debts / limitations

MEDIUM F3: Paper state is committed before the informational event journal append; an immediate crash can lose the event although replay does not duplicate the position.

MEDIUM F4: processed request IDs are truncated from a set to 1,000 entries and historic request files remain; long-horizon idempotency deserves deterministic retention.

A syntactically valid timestamp and correct identity do NOT cryptographically certify real provider freshness, execution, or availability. The 30-second policy can conservatively reject unchanged but legitimate quotes. No live Polygon/CLOB/settlement test, historical completeness proof, or naturally observed READY was performed here.

OS advisory locks protect cooperating local writers on one data root only; no distributed or cross-host guarantee.

**Decision:** Two HIGH findings are corrected and offline regression passes, but independent patch review and exact final-head CI remain prerequisites to explicit user merge approval. PR #48 and main were not modified.
