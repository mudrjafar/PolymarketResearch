# GENERAL1 — Final Integration Readiness

Status: READY_FOR_INDEPENDENT_FINAL_REVIEW, **not** authorized to merge or live-run.
Source: PR #50 base and isolated Request Integrity patch; see GENERAL1_REQUEST_INTEGRITY_AUDIT.md.

| Dimension | Assessment | Evidence / remaining requirement |
|---|---|---|
| Request integrity | FIXED, offline regression PASS | UTC chronological ordering; no CLOSE of tentative OPEN; explicit rejection for every processed request in successful cycle |
| Execution freshness | Prior PR #49/#50 implementation stays unchanged | Source epoch-ms freshness + local receipt-age commit bounds; policy may conservatively reject quiet markets |
| Focus authority | UNCHANGED | Durable Focus generation/evidence binding and final synchronized READY gate |
| Book binding | UNCHANGED | Exact token+market identity, depth, generation/evidence; Book PASS not identical to final Paper authorization |
| Paper durability | KNOWN MEDIUM DEBTS | F3 state-before-journal crash gap; F4 1000 set-ID retention. Reconcile state vs journal before deriving statistics |
| Dashboard | KNOWN MEDIUM | dashboard.py reads legacy paper_trades.json; authoritative worker saves paper_state.json. Do not trust dashboard as ledger until separately fixed |
| Telegram UX | PRELIMINARY | READY+BOOK PASS is only an invitation to submit manual request; Paper independently validates final source quote, receipt time, settlement, READY |
| Runtime compatibility | NOT YET VERIFIED on target | Ubuntu 26.04 / Python 3.14 and Windows locks not established by Ubuntu/Python3.11 CI |
| Collector throughput / natural READY | UNVERIFIED | Requires separate explicitly approved isolated E2E run |
| Economic performance | UNKNOWN | No real execution guarantee or proof of alpha, fee/slippage outcome or sufficient signal frequency |

## First sandbox, only after separate user approval

Dedicated disposable data root, no production DB, never real orders or forced READY.
Before run: verify RPC configuration, chain safe-tip and checkpoint, supervised services, Telegram credentials or explicit manual queue, state snapshots and no duplicate writers.
Record funnel counts Collector -> Flow -> Diamond -> Risk PASS -> Focus LOCKED/READY -> Book PASS -> Paper requests; canonical identity, lag, source quote age/receipt age, fee/VWAP/slippage and mark/settlement.
If no naturally READY signal, mark Paper outcome INCONCLUSIVE, not PASS/FAIL, but still assess collector and diagnostic coverage.

## Separate small follow-ups

Dashboard read-only schema migration to paper_state.json (do not conflate with execution change).
Operator-facing text: Book PASS preliminary and does not promise fill.
F3/F4 future crash-safe reconciliation and deterministic request ID retention before unattended long-term learning.

## Release gate

This branch is suitable for **independent final diff review**, pending exact final-head CI success. Explicit user authorization is required separately for any merge, live RPC/CLOB query, manual or automated paper OPEN, or downstream trading.
