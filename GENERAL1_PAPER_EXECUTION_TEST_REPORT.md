# GENERAL1 PAPER EXECUTION TEST REPORT
Offline-only finalization review; branch based on PR #49

## Exact code revision tested
- Baseline PR #49: `96a1ccf8fc0e65e3ddf06c7d0efcb60a4f2460b9`.
- Code/test implementation commit: `9590c162e6d1225ca25759b41a16a43e5ceb3c97`.
- CI run on EXACT implementation commit: https://github.com/mudrjafar/PolymarketResearch/actions/runs/40244647323, **SUCCESS**.
- Target workflow `.github/workflows/general1-integration-review1-ci.yml`, Python 3.11, Ubuntu GitHub Actions.
- **309 focused pytest passed, 61 subtests passed.**
- **533 full pytest passed, 61 subtests passed.**
- **python run_machine.py --self-test: SELF-TEST OK / ALL TESTS PASSED** (Risk 7/7, Focus Engine/Runner, Book Engine/Worker, Paper Engine/Worker).
- All tests were OFFLINE, mocked external CLOB/chain/metadata/settlement boundaries; no real token, wallet, trade or production DB.

## Recorded failures and corrections

First branch CI run `40244390049` failed 2 existing concurrency tests: their injected `clock()` callback unconditionally asserted possession of the final shared Focus lock, but newly inserted receive-time `clock()` calls happen earlier, outside that lock. This was NOT evidence that the lock failed: final shared-lock check was unchanged. The tests now distinguish first book-receive, MARK-receive and final commit clock reads, asserting lock contention only on the third read. CI `40244490716` succeeded with **527 tests + 61 subtests**. Six pure receipt-validation parameter cases were added; CI `40244647323` succeeded with **533 tests + 61 subtests**.

## Reproductions and outcomes

| Scenario | Result / evidence |
|---|---|
| Incoming fresh CLOSE quote 7s after tick start | Accept based on actual response-receive clock, not false FUTURE |
| CLOSE quote fresh on fetch, >30s old before save | Roll back tentative CLOSED to OPEN; reject event; no realised-P&L mutation |
| OPEN quote source fresh at read, stale by final commit | Final Focus/Book lock rechecks same source timestamp; reject OPEN |
| OPEN quote local receipt >30s at commit while source timestamp still within 30s | Reject OPEN with FINAL_COMMIT_BOOK_RECEIPT_STALE |
| Source book generated 120s earlier but HTTP received now | Explicit conservative fail-closed OPEN, pending empirical CLOB contract verification |
| Missing/invalid/seconds-vs-ms source timestamp | Reject; no fabricated execution |
| Missing CLOB identity and wrong condition/token | Existing F1 and Book OPEN gates remain fail-closed |
| Inadequate bids/asks for requested size | Reject CLOSE / OPEN according to existing depth contract |
| Previously good MARK followed by missing market/token | Clear old MARK object, update status, do not display stale P&L as current |
| MARK returned after long previous RPC work | Evaluate against MARK response-receive time, not worker tick-start |
| MARK expires between fetch and Paper save | Clear MARK, report BOOK_TIMESTAMP_STALE, preserve position |
| Focus invalidated during open processing | Existing durable shared Focus lock + final READY gate reject invalid OPEN |
| Book generation swapped after initial gate | Existing generation binding/final shared lock prevents unauthorized OPEN |
| Worker restart and duplicate OPEN ID | Existing paper_state lock / processed-request tests pass in controlled limited retention horizon |
| Paper state save succeeds but journal append crashes | No duplicate OPEN; missing journal event acknowledged as F3 |
| Request-ID retention >1,000 | Risk assessed (F4); no id ledger redesign in this scope |

## Contract and diff guard

Changed existing code only in:
- scripts/paper_engine.py (local receipt-age validation helper)
- scripts/paper_worker.py (clock-at-receipt, provenance, CLOSE rollback and MARK invalidation; final OPEN source+receipt fence)
- tests/test_paper_worker.py, tests/test_paper_engine.py and tests/test_general1_ready1.py
- offline CI branch trigger

No collector, flow, diamond, risk thresholds, Focus evidence/counting, Book OPEN price mathematics, paper settlement finality, telegram READY, real orders, or external integration has been changed. Lock acquisition order unchanged; no network calls under the short shared Focus/Book/Paper commit lock. PR #48 and PR #49 remained unchanged.

## Not proved / next gate

Tests are **Linux Python 3.11**; Windows filesystem lock/fault behavior is not reproduced by this workflow. We do not have an authenticated live CLOB freshness/latency SLA or actual sample stream. The 30s source-book policy can block legitimate quiet books; document/count before deciding whether to alter it. CLOB quote liquidity may disappear immediately after fetching, therefore Paper is a retrospective estimate, never guaranteed execution.

The F3/F4 decision is in `GENERAL1_F3_F4_RISK_DECISION.md`. This patch is suitable for an independent integration review and explicit future controlled paper-only sandbox authorization. **NO merge, live test or autonomous paper trade has been approved.**
