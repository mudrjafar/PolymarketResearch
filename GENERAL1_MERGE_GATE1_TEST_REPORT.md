# GENERAL1-MERGE-GATE-1 — Offline Regression Test Report

Repository `mudrjafar/PolymarketResearch`.
Code base: PR #45 `cdbec6bea8d6f83040c5822f0578e23c4d7a12e5` + PR #46 `a1f9de308d5029c722b6a51ca933a9748421f95e` + exclusive Paper-writer lock and three additional regression tests, on `feat/general1-merge-gate1-consolidated-20261025`.

## Actual CI evidence

Implementation CI: https://github.com/mudrjafar/PolymarketResearch/actions/runs/40150325269 — **SUCCESS**.

- Focused offline `pytest` (Collector, Flow, Diamond, Risk, Focus, Book, Paper, settlement, Gamma core): **285 passed, 61 subtests passed**.
- Full `python -m pytest -q`: **499 passed, 61 subtests passed**.
- `python run_machine.py --self-test`: **SELF-TEST OK; ALL TESTS PASSED**. Risk 7/7, Focus Engine/Runner, Book Engine/Worker and Paper Engine/Worker PASS.
- `python -m py_compile` on six modified original Core modules PASS.
- Final docs-only commits also trigger CI; verify latest final branch HEAD rather than assuming tests on older SHAs apply.

## Added independent cases for this gate

**Exclusive writer collision:** hold the actual platform advisory Paper lock while invoking a second worker; raises `PaperStateError`, no state committed. After release, a fresh invocation may OPEN once; subsequent replay is idempotent.

**Closing after durable Focus invalidation:** open a virtual position with an explicit test request, set committed Focus state to IDLE/None, submit explicit CLOSE; worker executes CLOSE and does not require READY for that operation.

**Unknown Focus engine version:** replace committed engine `version` with unsupported value; Paper execution gate reports `FOCUS_STATE_INVALID` and refuses any fresh Book request or OPEN.

Other retained regression cases (PR #45/#46):
- LOCKED → WAIT 1/3 → WAIT 2/3 → READY from four distinct offline canonical-looking events and 60+ second event-time spacing.
- Duplicate evidence/new generation, decreasing cursor, too-close evidence and wrong market/outcome hard blockers.
- Missing CLOB market/asset IDs and insufficient executable depth.
- Torn durable Focus state versus still-READY compatibility projection, missing/corrupt/mismatched durable state.
- Focus READY lost or invalidated in the middle of fresh Book fetch; changed Book evidence/generation.
- User-request token/condition/evidence/generation mismatch, duplicate requests and repeated-worker restart.
- Unknown/resolved or unverified on-chain settlement blocks simulated OPEN.
- Collector V4 checkpoint integrity, V3/NegRisk identity, Gamma fallback/truncation and no-data-loss checks.

## CI history and interpretation

Previous, narrower CI:
- PR #45: 493 passed / 61 subtests, machine PASS.
- PR #46: 496 passed / 61 subtests, machine PASS.
- Consolidated implementation: **499 passed / 61 subtests**, machine PASS.

These counts differ from earlier 525-test READY-1 branch because experiment-only Windows launchers, live probe modules and their tests are intentionally excluded from this minimal main-targeted integration. The only failed integration CI during the preparation of PR #45 was a copied test's import of excluded E2E2 probe code; that non-core test was removed from the copied test suite and all retained core tests passed. Do not hide this historical failure, but do not misrepresent it as a production regression.

## Boundaries

All tests executed through GitHub Actions with mocks/temp directories; **no Polygon RPC, real CLOB HTTP, wallet, real orders, old DB, or live-run calls** in this task. Synthetic evidence exists *only* inside offline tests; no runtime READY reconstruction. Actual startup performance, CLOB timestamp truth, multi-host paper coordination, cross-process post-final-gate atomicity and full live paper readiness remain separate questions.

**Result:** PASS for an offline, paper-only code review candidate; NOT an executed live Paper OPEN.
