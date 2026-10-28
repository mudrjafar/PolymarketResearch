# GENERAL1_FINAL_INTEGRATION_READINESS

**Scope:** Decision after minimal Paper Request Integrity implementation on branch `fix/general1-request-integrity-final-20261028`; based on PR #50 source commit `5570535a86e7c02b937c2ab6747e05083ffc78ea`.

**Suggested final classification:** `READY_FOR_INDEPENDENT_FINAL_REVIEW` provided final-HEAD offline CI passes. This classification is intentionally **not** an APPROVE, MERGE, a general launch readiness label, or permission for any real/live paper test.

## Gate matrix

| Gate | Status | Evidence / remaining action |
|---|---|---|
| Manual Paper request identity, order, one terminal event per successful tick | PASS (offline) | Chronological aware UTC timestamp + request-ID tie; fail-closed invalid date; tentative positions cannot be CLOSEd in the same tick; terminal invariant before state commit |
| F50-01 random filename order | FIXED (offline) | Existing code misordered inverse UUID-lexical and submission time; reproducing test failed before patch and passed after |
| F50-02 dependent CLOSE lost by OPEN rollback | FIXED (offline) | Exact missing event reproduced red; same-tick CLOSE rejected, residual future dependent events converted to explicit REJECTED, terminal invariant |
| Execution book identity/freshness | PASS (offline), UNVERIFIED live | CLOB asset_id+market and conservative 30s book source / local receipt age, final commit; actual CLOB timestamp update semantics remain unverified |
| Focus alone authorizes OPEN | PASS (offline) | Existing durable Focus/view + Book binding + evidence and generation checks preserved |
| Existing committed position CLOSE while Focus invalid | PASS (offline) | No added Focus READY requirement for CLOSE; negative tests remain green |
| Paper state persistence / writer lock | PASS (offline) within current single local data root | Advisory lock, atomic JSON replacement, restart regression; Linux CI only |
| F3 Paper event durability | KNOWN MEDIUM / NOT FIXED | State saved before event append; crash can leave audit journal incomplete. Reconcile from paper_state.json; never treat event log as sole truth |
| F4 processed request retention | KNOWN MEDIUM / NOT FIXED | 1,000-entry nondeterministic cap; future archival/reconciliation before unattended long-run processing |
| Dashboard paper positions/P&L | NOT READY | `dashboard.py` still reads `paper_trades.json` (legacy), not `paper_state.json`; do not use dashboard to claim Paper E2E success until read-only compatibility fix or authoritative direct state audit |
| Telegram & Book READY+PASS semantics | NEEDS LABEL CLARIFICATION | `book_ok` is an *assessment* of focus-bound book identity/levels; Paper requires stronger quote freshness/finality/request checks. Never communicate READY_BOOK_PASS as guaranteed Paper fill |
| First natural chain-tip Collector throughput | UNKNOWN | Offline tests do not establish sustained >=chain rate or lag; requires separately authorized data-only runtime observation |
| Python 3.14 on Ubuntu 26.04 and Windows | UNVERIFIED | Existing GitHub Actions uses Ubuntu hosted runner Python 3.11; no evidence of exact target-environment dependency compatibility |
| Natural Focus READY and real-life Paper economics | UNKNOWN | Need measured naturally generated READY, book-block reasons, simulated fees/spread/slippage and marked/closed P&L |
| Real orders / automatic Paper execution | FORBIDDEN | No authorization granted; Telegram only creates explicitly requested Paper actions |

## F50-03/F50-04 follow-up scope decision

**F50-03 dashboard:** Confirmed source mismatch, NOT changed within the Request Integrity security patch. A separate read-only adaptation from `paper_state.json.positions` plus nested entry/mark/exit fields and mark_status is needed before Dashboard can be a reliable observation tool; otherwise report authoritative paper_state.json directly. The change has no trading authority.

**F50-04 execution messaging:** Confirmed semantic distinction: Telegram can show READY+BOOK PASS based on Book assessment and Focus binding, while Paper may reject its additional checks (CLOB timestamp, amount-specific depth, settlement, final Focus commit). Better label it `Preliminary Book Assessment` / `Paper worker will revalidate`, without weakening any gate. The existing message already mentions Paper revalidation; it should not be described as a guaranteed fill. Fix separately if it materially misleads users.

## Next authorized steps (in order; none authorized by this document)

1. Independent review of the narrow PR containing these changes; verify all required test paths and the final GitHub commit SHA.
2. With explicit user permission, integrate nested draft PR **new Request Integrity → #50 → #49 → #48**, then rerun full offline CI on one consolidated PR #48 head before evaluating merge into main.
3. Audit collector preflight, RPC environment, chain-tip ingestion throughput, and target Ubuntu 26.04/Python 3.14 in an isolated test environment, no real order keys.
4. For a separately authorized first **controlled manual Paper-only E2E**, use a disposable isolated data root, real public market/RPC reads only, and no synthetic READY in live data. Record chain lag, eligible trade identity share, Flow→Diamond→Risk→Focus LOCKED→READY→Book PASS funnel, source book timestamp rejection rate, Book/Paper quote age, explicit user Paper opens and virtual P&L/fees/spread/slippage. If no natural READY occurs, classify execution as INCONCLUSIVE; report stage counts/block reasons.
5. Reconcile authoritative state vs Paper event journal and requests at test end; if Dashboard has not been corrected, do not rely on its position/P&L view.

## Final recommendation

A narrowly scoped request-integrity patch with red-to-green security regression, no strategy/Collector redesign, and a small diff is the right next step. **After independent review, stop feature expansion and prioritize controlled integratability and measurable live data quality.** Historical F3/F4 and dashboard/messaging follow-ups remain explicitly tracked.
