# GENERAL1-READY-1 — Deterministic Offline Integration Test Report

**Scope:** `Risk.assess → Risk candidate contract → durable Focus runner → Book worker → explicit Paper OPEN worker`, with network boundaries mocked and all state in per-test temporary directories. No real Polygon, CLOB, Telegram action, production database, secrets, orders or paper-requests written outside the test sandbox.

## 1. Baseline

Previous Gamma-1 branch `feat/general1-gamma-1-bounded-metadata-20261025` passed **507 tests + 61 subtests**. Its 300-second real-data comparison achieved Collector 0.753 blocks/s versus Polygon 0.670 blocks/s, 1 Risk PASS and a legitimate Focus LOCKED. The live comparison is a separate existing experiment; READY-1 did **not** repeat it.

## 2. Implementation

Branch: `feat/general1-ready1-focus-paper-integration-20261025`

Code changes:

- `scripts/focus_engine.py`: invalidate active Focus if current Risk row has different canonical condition or outcome despite matching token.
- `scripts/book_engine.py`: fail closed when the CLOB response does not contain the necessary asset ID or market ID.
- `scripts/paper_worker.py`: final Focus/Book/request-binding recheck after network/settlement operations and immediately before a virtual OPEN commit.
- `tests/test_general1_ready1.py`: isolated multi-worker integration and negative regressions, including actual Risk-assessed candidate publication.
- `.github/workflows/general1-ready1-preflight.yml`: compile, targeted tests, full regression suite, machine self-test.

There are **no** strategy-threshold changes, fake live evidence, automatic Paper OPEN creation, or real orders.

## 3. Complete positive scenario

The test uses four distinct canonical-looking txhash/log-index identities and increasing [block,logIndex] evidence cursors. They are **synthetic offline fixtures**, not historical on-chain assertions. Every candidate is produced through `risk_engine.assess` and `focus_candidate_result`, and the durable `focus_runner.run_once` reads an actual temporary `risk_assessment.json` and writes temporary authoritative Focus artifacts.

| Event-time offset | New Risk evidence | Observed Focus | Confirmations |
|---:|---|---|---:|
| 0s | A, Risk PASS | LOCKED | 0/3 |
| 61s | B, Risk PASS | WAIT 1/3 | 1/3 |
| 122s | C, Risk PASS | WAIT 2/3 | 2/3 |
| 183s | D, Risk PASS | READY | 3/3 |

The actual `book_worker.run_once` consumes READY, validates a mocked market+asset-bound ask/bid book, and produces a Book OK/PASS snapshot bound to the same Risk generation and evidence. Without an OPEN request the actual Paper worker opens nothing. Only a subsequently written explicit, schema-correct user request with $25 opens a fee-aware virtual position; replaying the request does not open a second position.

## 4. Negative coverage added

| Scenario | Safety expectation |
|---|---|
| New generation with identical blockchain evidence | No Focus progress |
| New evidence too soon in event time | Evidence consumed but no progress |
| Earlier/replayed cursor | Cannot progress |
| Different condition, same token, upstream Risk PASS | Immediate invalidation |
| Different outcome, same token, upstream Risk PASS | Immediate invalidation |
| Active market flag becomes false | Risk BLOCK and Focus INVALIDATED |
| Missing CLOB asset ID or market ID | Book BLOCK; Paper gate closed |
| Paper request with wrong token/condition/generation/evidence | Reject |
| Focus and Book snapshots stale | Reject |
| Focus READY downgraded before Paper request processed | Reject |
| Focus READY revoked *during* refreshed Book fetch | Final Paper gate rejects without OPEN |
| Book assessment refers to another generation | Reject |
| Fresh execution book insufficient for requested notional | Reject |
| Settlement authority returns UNKNOWN | Reject |
| Replay of same Paper OPEN request ID | No duplicate position |

Existing project tests also cover V3 identity, on-chain settlement cases, malicious/corrupt payload shapes, incomplete/ambiguous Gamma responses, collector checkpoint/idempotency, worker backoffs and executable liquidity checks. A single test suite PASS does not equate to a live data-source proof.

## 5. GitHub Actions result

Initial implementation workflow run:

- **Run:** https://github.com/mudrjafar/PolymarketResearch/actions/runs/40141286522
- **Result:** SUCCESS
- **Focused suite:** 132 passed at the initial checkpoint; final count is expected to increase by one after the added fetch-time invalidation test.
- **Full project pytest:** **524 passed, 61 subtests passed** at the initial checkpoint; final numbers are recorded by the latest branch-HEAD CI run.
- **`python run_machine.py --self-test`:** SELF-TEST OK; ALL TESTS PASSED. Risk 7/7, Focus engine/runner, Book engine/worker and Paper engine/worker pass.

Subsequent docs-only commits may trigger fresh CI; use the **latest** completed run at the feature-branch HEAD as the final check.

## 6. Limitations, release gates and decision

**Decision: `READY_FOR_CONTROLLED_PAPER_TEST`** as an **offline code-and-contract outcome**, not a real market execution claim. Pending:

1. Independent code and PR review; do not merge to `main` automatically.
2. Carefully select upstream core dependency commits (Gamma-1 has 70 ahead of main as audited).
3. Explicitly enable the Gamma batch optimisation **only** in a reviewed, fresh isolated sandbox; it is disabled by default.
4. Natural live READY must be observed without synthetic evidence or lowering thresholds.
5. User must explicitly submit each simulated Paper OPEN after a fresh Book PASS.
6. Confirm CLOB book timestamp semantics and worker TOCTOU boundaries before any broader operational claims.
7. No real money, order keys or unattended actions.

**No new live Polygon test was run for GENERAL1-READY-1.**
