# GENERAL1 — Signal Funnel: Real Data (30 October 2026)

**Scope:** Source: 300.04-second public Polygon observation, [GitHub run 41067595593](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41067595593). This experiment used the 500-block Collector default lookback due to a previously identified test-harness bug; it is a *backlog catch-up* sample, not a near-tip freshness proof.

| Stage | Observed latest count | Main blockers or caveats |
| --- | ---: | --- |
| Persisted normalized trades | 6,532 | Legacy CTF only; 772 market conditions |
| Flow token states | 554 | 0 `verified` |
| Diamond analyzed (latest) | 539 | 539 `DATA_RISK`, 0 DIAMOND |
| Risk decisions (latest) | 539 BLOCK / 0 PASS | 539 STALE_TRADE, 539 STALE_FLOW_TRADE, 539 NOT_DIAMOND, 539 FLOW_NOT_VERIFIED, among *overlapping* reason codes |
| Focus LOCKED | 0 | No eligible Risk PASS |
| Focus WAIT | 0 | No LOCKED |
| Focus READY | 0 | No valid evidence lifecycle |
| Book PASS | 0 | Book `NOT_READY` and reason `FOCUS_NOT_READY` |
| Paper OPEN | 0 (forbidden) | Paper worker deliberately not launched |

The Learning sidecar recorded 7,893 selected generation signal rows, all `DATA_RISK`, and 7,893 corresponding Risk BLOCK rows. This is a repeated snapshot population, not 7,893 independent predictions.

Risk reason codes are **not mutually exclusive**. Counts must not be summed as if they described separate markets. Missing Risk PASS is upstream of Focus and Book; neither Focus exclusivity nor Book market depth can be evaluated economically without natural READY.

### Why stale data prevailed

The start configuration was incorrectly forwarded, creating an initial head/checkpoint lag of 529 blocks instead of the intended ~30-block lookback plus 20 confirmations. Although the Collector exceeded observed chain rate during the 300s run and the backlog decreased by 29 blocks, there were still 500 blocks of lag when the test ended. A recent Flow trade from those blocks was therefore too old for fresh Risk inputs. Distinguish event-time age from processing-time freshness.

### What could and could not be verified

**Observed:** Real Collector→Flow→Diamond→Risk data processing and Learning archival from 6,532 real normalized fills; fail-closed stale-data decisions; Focus IDLE; Book NOT_READY. Workloads ran throughout 300s with zero unexpected exits.

**Not observed:** Verified BUY Flow, DIAMOND classification, Risk PASS, a genuine LOCK/WAIT/READY, Book PASS, quote-age distributions or any Paper OPEN/CLOSE/settlement.

**Inconclusive:** Signal effectiveness; wait-to-READY price decay; true CLOB execution coverage. Not seeing READY during a deliberately stale catch-up trial cannot be interpreted as failure of the underlying quantitative strategy.

A corrected 30-block near-tip run, [GitHub Actions 41068606756](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41068606756), is separate. Its stage counts should be reported separately once verified.

**No changes to Diamond, Risk, Focus thresholds or lifecycle were made.**
