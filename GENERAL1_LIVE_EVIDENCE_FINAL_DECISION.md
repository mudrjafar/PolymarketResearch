# GENERAL1-LIVE-EVIDENCE-GATE-1 — FINAL INDEPENDENT DECISION

**Date:** 30 October 2026
**Mode:** Existing-main/PR read-only inspection, bounded public Polygon live collection in ephemeral isolated GitHub Actions worktrees, new diagnostics and tests on the audit branch only. **No core strategy threshold changes, no merge, no Paper worker, no orders.**

## A. Verified integration baseline

- `main` HEAD: `97d881311a68c54ec15a4f537d8a055e04729f44` (unchanged).
- PR #52: `f379ac3af68d3fbe8159226ec2cd4cca9059dc26` OPEN, not merged, CI 549 Pytest +61 subtests; PR #51 is an alternative sibling, not an additional merge layer.
- This work: `audit/general1-live-evidence-gate1-20261028` based on the previous Quant audit; adds only isolated observer, telemetry, safe test launcher, Github CI and documentation. It never changes existing production Collector/Risk/Focus/Book/Paper decision functions or the user/local DB.
- Latest verified code CI prior to final report: [GitHub Actions 41067915470](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41067915470), **566 Pytest, 61 subtests, 17 targeted observer tests and machine self-test PASS**. Exact-head CI after final docs should also be checked before using any merged code.

## B. Actual live runtime evidence

Real public Polygon PoS chain observations, independent GitHub Ubuntu/Python3.11 runner, public `polygon.drpc.org`, 20 confirmations:

| Metric | Aborted initial, 500-block default | Full catch-up, 500-block default | **Corrected near-tip, 30-block lookback** |
|---|---:|---:|---:|
| GitHub run | [41066783110](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41066783110) | [41067595593](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41067595593) | **[41068606756](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41068606756)** |
| Actual seconds | 75.50 | 300.04 | **300.03** |
| Durable blocks | 15 | 220 | **227** |
| Normalized trades | 281 | 6,532 | **5,619** |
| Distinct market conditions | 82 | 772 | **787** |
| Collector/chain blocks per second | 0.2488/0.6799 | 0.7721/0.6654 | **0.8006/0.6684** |
| Head-checkpoint lag | 529→555 | 529→500 | **60→24** |
| Unexpected exits | 1 | 0 | **0** |
| Successful batches | 3 | 44 | **63** |

At the near-tip run, 542 RPC calls had 0 recorded retries, 813 Gamma fallback lookups (812 active/open, 1 historical), 0 classified lookup failures, cumulative Gamma lookup time 141.98s, 73 V3 unsupported events, only legacy `CTF_TOKEN` normalized trades persisted. Counted raw logs are **not** all eligible persisted CTF fills. V3 Paper execution remains unsupported.

**P0 configuration correction:** The launcher originally failed to forward `COLLECTOR_INITIAL_LOOKBACK_BLOCKS=30`, accidentally using default 500. This caused the first two experiments' ~529-block initial lag and stale signals. Fixed in `scripts/live_evidence_gate1.py:_build_worker_env`, with fail-closed validation, verified child-environment credential exclusion and negative regression tests. The third run explicitly attests 30 initial lookback, 20 confirmations, Gamma batching OFF. No core Collector code was changed.

## C. Naturally observed signal funnel — third run only

- 950 Flow token states; **11 VERIFIED**.
- Final Diamond snapshot: 914 analyzed including **13 DIAMOND**, 26 CANDIDATE, 9 VERIFYING, 583 DATA_RISK, 63 LOW, 220 WATCH.
- Final Risk: 914 assessed, **11 PASS**, 903 BLOCK. Reason codes overlap (e.g., 899 CONFIRMATIONS_LOW, 643 WHALE_CONCENTRATION, 579 STALE_TRADE); sums do not equal separate markets.
- **Focus LOCKED 1, WAIT events 2, final WAIT 2/3**, from natural real evidence. **READY 0**.
- Book latest `NOT_READY` / `FOCUS_NOT_READY`; Book PASS 0. Paper never started, 0 OPEN.
- Learning archive: 7,518 repeated generation observations (113 PASS, 7,405 BLOCK), NOT 7,518 independent signal tests or 113 independent profitable trades.

This independently establishes an actual Polygon→Collector→Flow→Diamond→Risk→Focus WAIT2/3 chain **without synthetic READY or safety-gate bypass**. An entire Focus→Book→Paper lifecycle and quote execution have NOT been observed.

## D. Economics and experimental readiness

**NOT YET MEASURABLE / BLOCKED.** Positive predictive alpha cannot be inferred from genuine Diamonds, Risk PASS or WAIT. An unbiased A/B/C test still lacks (1) full LOW/non-signal at-risk market population because `scripts/learning_queue.py` filters LOW-only generations, (2) synchronized two-sided bid/ask/depth and actual fees at signal/LOCK/READY and +5/15/60 minute horizons for candidates and matched controls, (3) sufficient held-out independent events and settlement outcomes. Observations repeated within a condition are correlated. No statistical edge, net expectancy, confidence interval, win-rate or Sharpe result is claimed.

## E. Findings and smallest fixes

| Severity | Grounded finding | Impact | Minimal action |
|---|---|---|---|
| HIGH — FIXED IN AUDIT HARNESS | `scripts/live_evidence_gate1.py` omitted Collector lookback from sanitized child env | Initial 500-backlog mislabeled near-tip; stale data | Forward and attest validated 30/20 setting; negative test green; confirmed in run 3 |
| HIGH — STILL OPEN | Natural Focus READY 3/3 and fresh Book PASS not witnessed | Paper/E2E readiness not proven | Continue a controlled, persistent passive session to natural READY; record exact binding/quote age |
| HIGH — STILL OPEN | `scripts/learning_queue.py` LOW-only exclusion; no forward executable CLOB history | Cannot estimate unbiased A/B/C net advantage | Add independent passive full-universe + timed bid/ask observer, no trading authority |
| MEDIUM — MONITOR | Gamma fallback 813 lookups, 141.98s cumulative in 300s near-tip | Performance sensitivity and rate-limit risk | Observe p95/latency/caching only if lag/throughput deteriorates; no premature refactor |
| MEDIUM — OPEN | `dashboard.py` reads legacy `paper_trades.json` vs authoritative `paper_state.json`; historical F3 event-journal crash gap, F4 limited dedup | Future Paper P&L evidence may be wrong | Direct authoritative state reconciliation before any claimed E2E P&L |
| MEDIUM — LIMIT | Public GitHub runner with dRPC and Ubuntu3.11, not user Linux/QuickNode; V3 unsupported | External generalizability incomplete | Short identical user-host isolated replay after independent approval |

One initial short Collector abort did not repeat in either 300-second run. Its error detail was not in the original sanitized artifact; root cause is **unproven**. Do not attribute it to Gamma, exchange identity or RPC without evidence.

## F. Binding gate decisions

| Gate | Decision | Rationale |
|---|---|---|
| **COLLECTOR LIVE** | **PASS — bounded near-tip smoke only** | 300s uninterrupted, >chain block rate, decreasing lag, 0 unexpected exits |
| **PIPELINE E2E** | **INCONCLUSIVE** | Natural Risk PASS→Focus WAIT 2/3 verified; no READY→Book PASS→Paper |
| **NATURAL FOCUS READY** | **NOT OBSERVED IN WINDOW** | Focus was progressing through natural LOCKED and WAIT states |
| **PAPER TEST READINESS** | **NO-GO for now** | No natural READY, no Book PASS, unmerged candidate integration reviews and local target-runtime gate remain |
| **QUANT VALIDATION READINESS** | **BLOCKED** | Missing unbiased control population and future executable quote history |
| **REAL-MONEY READINESS** | **NO-GO** | No calibrated positive net expectancy or live trading authorization |

## G. No-more-than-three next actions

1. **Observe Focus 2/3→3/3 naturally and fresh identity-bound Book PASS** in a persistent user-authorized isolated data-only runtime. Keep existing Evidence, Risk and Focus gates. A new ephemeral GitHub run starts fresh; it cannot resume the previously locked Focus state, so prefer a persistent sandbox when practical.
2. **Independently review PR #52 integration** against its #48→#49→#50 stack and alternative #51; consolidate only with explicit user approval. Verify Linux/Python3.14 or Windows target runtime; no automatic Paper action or main merge without approval.
3. **Introduce a separate passive full-market/quote research recorder**, after runtime observability is established, for a preregistered A (Diamond), B (simple Flow), C (matched no-signal) executable net markout test.

**Executive conclusion:** The engine has passed a real 300s near-tip Collector/Flow/Risk/Focus observation and produces authentic Diamonds and evidence-driven Focus WAIT states. It is **not** yet a verified profitable quantitative strategy or a fully proven paper-trading system. Stop feature expansion until natural READY/Book and economic controls are measured.
