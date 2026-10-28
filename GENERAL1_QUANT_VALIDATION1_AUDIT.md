# GENERAL1-QUANT-VALIDATION-1 — Independent Technical & Strategy Value Audit

Date: 2026-10-28. Mode: independent code/CI inspection plus a strictly read-only diagnostic utility. No user-local live database, RPC credentials, market orders or private keys accessed. No live tests were executed in this audit.

## Executive decision

- **TECHNICAL MVP: INCONCLUSIVE** (offline core is promising; actual end-to-end readiness unverified).
- **CONTROLLED PAPER TEST: NO-GO at present** (not on unmerged, unreviewed development branch; a new isolated and explicitly authorized read-only observation must precede manual Paper OPEN).
- **STRATEGIC EDGE: INCONCLUSIVE** (no valid prospective executable A/B/C comparison).
- **REAL MONEY: NO-GO**.

This is a technically credible order-flow attention/filtering system, not an empirically demonstrated profitable model.

## Verifiable baseline

- `main`: `97d881311a68c54ec15a4f537d8a055e04729f44`.
- PR #43: `2e6e685685789bebc6df423c1b17eee13d6dd32a` (open, draft, not merged).
- PR #44: `d61fff69824a8674b11f91194cafb91c2ae507b4` (open, draft, based on Gamma branch).
- PR #48: `2c4ff4aee030b21fbb3b636d9dfa64e4f21a9191` (open, based on main).
- PR #49: `96a1ccf8fc0e65e3ddf06c7d0efcb60a4f2460b9` (based on #48).
- PR #50: `5570535a86e7c02b937c2ab6747e05083ffc78ea` (based on #49).
- PR #51: `d914a0abc55c0d798ddbc79b20ed034b5b74d7a3` (alternative to #52 based on #50).
- PR #52: `f379ac3af68d3fbe8159226ec2cd4cca9059dc26` (based on #50).
- PR #51 vs #52: **diverged**, common ancestor is PR #50 HEAD. Do not stack #51 on #52.
- #52 is 52 commits ahead of main, 0 behind; the leading *offline reviewed candidate*, **not** a merge or execution approval.
- GitHub run `40678873832` for exact #52 HEAD: job logs independently confirm **325 focused tests + 61 subtests, 549 full tests + 61 subtests**; machine self-tests passed. This is Ubuntu/Python 3.11 offline CI.
- Own audit branch `audit/general1-quant-validation1-20261028` starts at #52 HEAD; adds only diagnostic script, its tests, audit CI and this report. No changes to core thresholds or original branches.

Sources: GitHub PR endpoints, `https://github.com/mudrjafar/PolymarketResearch/actions/runs/40678873832`.

## Quant logic findings

### Q-01 HIGH: Unvalidated alpha hypothesis and heuristic score
`scripts/diamond_filter_v3.py` lines 23–38, 179–224, 282–323.

Signal Quality adds fixed points for direction alignment, trade breadth, volume, strength, concentration. Thresholds 80/75/40/70 are not learned/calibrated probabilities. Several terms summarize the same 1m/5m/15m overlapping trades. Three confirmations are distinct evidence IDs but NOT statistically independent events; correlated trades and repeated window information remain. No historical out-of-sample test of incremental future executable returns is available.

**Remedy:** Prospective A vs B vs C on synchronized *as-of* prices; cluster by event/market, chronological train/test split; do not tune thresholds on the final test period.

### Q-02 HIGH: Entry-score upside is not EV
`scripts/diamond_filter_v3.py` lines 222–250. `max_upside=(1-p)/p` is maximal gross payout room if YES wins, not calibrated event probability or expected return. Price often comes from latest on-chain fill (lines 138–155), not executable ask. Score can favor longshots with poor actual winning probability. Price-level controls mandatory.

**Remedy:** Require an explicit forecast model *only after* measuring calibration; evaluate price-improvement and spread-adjusted markout independently now.

### Q-03 HIGH: Missing denominator and future bid/ask history
`scripts/learning_queue.py` lines 16–20, 33–65; `scripts/learning_ingest.py` lines 18–22, 95–165; `scripts/learning_store.py` lines 104–221.

LOW-only generations do not enter the Learning queue; even non-LOW signal rows are prefiltered. Therefore Learning cannot establish the complete at-risk market universe or matched no-signal controls. `book_decisions` reflect Focus/READY observations, not systematic historical two-sided execution quotes at 5/15/60 minute horizons. Even an extant paper exit is selected by the user and cannot prove strategy alpha.

**Remedy:** One narrow *passive observational dataset* covering every eligible market (including LOW/no-signal), predeclared systematic quote snapshots, market/category/time/price/volume strata, independent clock timestamps and full version lineage. Do NOT wire new fields into trading authorization.

### Q-04 MEDIUM: Focus delay/selection bias
`scripts/focus_engine.py` lines 15–22, 250–265, 388–745.

READY needs three accepted evidence increments with >=60s evidence-time spacing. Earliest theoretical READY after LOCKED is >=120s **if** the first counted event can occur promptly; empirical delays can be much longer. Extra confirmation improves gate reliability but may erase early momentum. Exclusive Focus picks first eligible upstream ranked candidate and blocks challengers until lifecycle releases. The incremental value versus at-LOCK and first-pass prices is unknown.

**Remedy:** Quantify first_seen/LOCKED/READY time, markout, decay and missed challenger opportunities before changing the exclusivity contract.

### Q-05 MEDIUM: Book PASS is executability, not positive net EV
`scripts/book_engine.py` lines 88–198. It validates ids, levels, crossed books, full depth for fixed notionals and quotes. `book_ok = not reason_codes`, without a dedicated cost/net-alpha gate. Paper uses bid/ask depth/VWAP plus market fee metadata; it is a book-snapshot simulation, not a guarantee a real market order would fill. Spreads and taker fees need category- and time-specific validation against official provider terms.

**Remedy:** For each candidate record effective entry at ask VWAP, immediately reversible value at bid VWAP, fees, adverse selection, and 5/15/60m forward executable markout. Do not change trading thresholds until verified.

### Q-06 HIGH: Live chain-tip and natural READY unverified
PR #43 records partial Windows live test ~0.276 blocks/s vs observed chain ~0.667 and lag increasing, later collector/gamma/identity fixes mostly proven offline. No independent completed sustained catch-up and naturally generated READY→fresh CLOB→manual Paper OPEN run verified for #52. Collector #52 has an opt-in gamma batching flag disabled by default.

**Remedy:** isolated disposable fresh sandbox with user-authorized *data-only* observation; measure per-window block rate vs chain rate, checkpoint lag slope, RPC/Gamma failure, no skipped canonical events, full signal funnel. No synthetic READY injected into live state.

### Q-07 MEDIUM: P&L audit and long-run operational debt
`dashboard.py` line 19 still reads `paper_trades.json` rather than the authoritative `paper_state.json`. `scripts/paper_worker.py` line 1170 trims the unordered processed-ID set to the last 1000, and state save can precede event-journal append, leaving a crash window. These issues are documented in #52. Use direct Paper state as truth until reconciliation and dashboard fix.

**Remedy:** separate small read-only dashboard correction and state/journal reconciliation before claiming audit-grade P&L. No need to redesign risk/flow.

### Q-08 MEDIUM: Protocol coverage differs by tradeability
#52 includes V3 Collector/Flow research support, but execution and CTF settlement deliberately exclude unsupported V3 families. Data API fallback may lack active/accepting-order metadata and must fail closed. Verify the **eligible legacy CTF** market population separately from observed V3 trades; a high collector trade count alone is not execution opportunity coverage.

## Validation protocol — not yet run on live observations

**A**: first genuine Focus READY after Diamond+Risk passed, immutable signal/evidence lineage.

**B**: first predeclared simple BUY-flow event (same market universe, BUY 5m/15m net direction; >=8 trades & $500/5m), no Diamond/Focus gate. Treat this as a baseline rule, not an optimized alternative.

**C**: non-signal observations from the SAME predeclared at-risk universe, matched by market category, YES/NO price decile, time-to-expiry band, order-book liquidity and observation calendar hour. Exclude near-duplicates within a market and repeated outcome representations.

Freeze signal threshold rules before collection. Use decision-time snapshots actually available at the time (source event time, observed time, book receive time). Execute virtual entry on snapshot *asks* using full available depth + fees; exits/markouts on timestamp-eligible *bids* after 5, 15, 60 minutes and at resolution. Mark missing quotes as censored/unevaluable (not zero profit). Separately examine 1) signal-to-LOCK and LOCK-to-READY price movement, 2) spread-adjusted forward markouts, 3) realized settlement returns. Prefer market/event-cluster bootstrap intervals and chronological held-out periods. Compare A–B and A–C, including signal frequency and coverage. Report sample size, independence limits, regime mix, number of exclusions and worst-case missingness sensitivity. No inference from selected Paper positions alone.

**All comparative results currently: NOT_YET_MEASURABLE**. There is no user runtime SQLite or historic CLOB stream in this GitHub repository. The added `quant_validation1.py` is intentionally a *read-only readiness/funnel assessment*, not a fabricated backtest.

## Immediate tool & test

`python scripts/quant_validation1.py --data-dir data`

Reads existing `data/collector.sqlite3` and `data/learning.sqlite3` only. Produces JSON stage counts, data readiness flags, cohort-coverage diagnostics, and explicit inability to infer returns. It never creates missing DBs, writes DBs, calls the market/RPC, or authorizes trading. Tests cover missing DBs, fake positive P&L still **not** proving alpha, malformed timestamps and collector counts not proving live chain-tip.

## Five-step MVP acceleration gate

| Priority | Action | Minimum required acceptance |
|---|---|---|
| P0 | Independent diff review of #52 vs #51, then consolidate **one** correct tested candidate (without touching main absent approval) | one exact-HEAD CI-green candidate; no regression/security authority weakening |
| P0 | Run read-only local quant audit on existing DB | honest stage counts, source compatibility and no fabricated conclusions |
| P0 | User-authorized isolated data-only chain-tip run | stable catch-up vs measured chain rate, decreasing/low lag, canonical identity and no silent missing data |
| P1 | Validate natural READY + real CLOB quote coverage | genuine LOCK→READY frequency, delays, bid/ask cost, quote age, blocker breakdown; ZERO synthetic READY |
| P1 | Capture full unbiased at-risk sample and matched quotes, perform predeclared 5/15/60m A/B/C shadow comparison | cluster-aware out-of-sample A–B and A–C net differences with uncertainty intervals; inconclusive if inadequate |

**Do not** interpret no natural READY in a short run as "strategy unprofitable"; it only means unmeasured. Keep dashboard repair/journal reconciliation within narrowly scoped operations before relying on presented P&L.

## Final independent recommendation

**Continue the project as a controlled research engine, halt feature expansion, do not authorize real money.** The research question is not "does strong on-chain BUY flow exist?" but "does its forward, *executable net value after it is detected and confirmed* beat a simple flow rule and matched no-signal markets out of sample?" No currently verified data establishes this.
