# GENERAL1 Request Integrity — deterministic offline test report

## Baseline and execution

Base: PR #50 5570535a86e7c02b937c2ab6747e05083ffc78ea
Branch: fix/general1-request-integrity-review2-20261028
Implementation checked in offline CI: 3bb7f860c5fc659fbe505920457574d2ea62c3ed

Run: https://github.com/mudrjafar/PolymarketResearch/actions/runs/40678540007
Outcome: SUCCESS on Ubuntu / Python 3.11.
- Focused integration, collector and security regressions: **316 passed, 61 subtests passed**
- Entire project pytest: **540 passed, 61 subtests passed**
- Machine self-test: SELF-TEST OK / ALL TESTS PASSED (including Risk 7/7, Focus Engine/Runner, Book Engine/Worker, Paper Engine/Worker).

The first CI on the branch, 40678342337, FAILED 2 cases. An initial blanket tentative CLOSE guard inadvertently changed the legacy SETTLED rejection, and a first attempt at the final-READY-revocation test revoked during an earlier preflight rather than the locked final commit. Both were corrected narrowly. The final regression ran clean.

## New tests in tests/test_paper_worker.py

1. test_manual_requests_sort_by_utc_submission_time_not_random_filename — reverse lexical order.
2. test_same_tick_close_of_tentative_open_is_rejected_but_open_may_commit — no prematurely CLOSED tentative position.
3. test_failed_tentative_open_keeps_dependent_close_rejection_event — mocked final third READY read, two explicit rejections, no silent missing request and no OPEN.
4. test_invalid_submission_time_gets_explicit_rejection — invalid timestamp.
5. test_same_instant_requests_tie_break_by_id — deterministic equal-time ordering and exclusive OPEN.
6. test_two_close_requests_for_committed_position_have_one_closed_event — one success and one explicit rejection.
7. test_open_close_open_same_tick_preserves_each_request_outcome — no second OPEN and no disappearing CLOSE.

## Existing coverage retained

- Repeated same request is idempotent, restart after paper state save doesn't reopen, failed journal append is known F3.
- Invalidation of durable Focus, mixed Book generation/evidence, settlement status, missing IDs and stale/expired quotes, specific Time-of-Check-Time-of-Use fences.
- Close after Focus revocation is still permitted, while close of already SETTLED receives POSITION_ALREADY_SETTLED.
- OS advisory lock serializes two local Paper workers; Focus/Book lock contention tested with synthetic barriers.
- No new network reads or wallet use.

## Additional tests / residual uncertainties

- Concurrency across distinct machines/shared network filesystem: unverified.
- Untrusted file rewritten after first _load_requests read: not transactionally protected (single trusted Telegram producer assumed).
- Sorting equal requested_at uses request_id, not physical write-order; expected deterministic fallback.
- Python 3.14 on Ubuntu 26.04, as well as Windows OS advisory locks: unverified; CI uses Python 3.11 on Ubuntu runners.
- Live Polygon/CLOB settlement quality and natural READY rates: not tested or authorized.

## Release interpretation

Passing offline regression is **not** a merge authorization nor a proof that paper trades are economically realistic. Require independent patch review, final SHA CI, explicit merge decision, and separate authorization before controlled E2E observation.
