# GENERAL1 QUANT DATA READINESS

**Status: BLOCKED / NOT YET MEASURABLE.**

The existing Learning sidecar records selected candidate classes only. `scripts/learning_queue.py` does not enqueue LOW-only generations; thus there is no complete no-signal "at-risk" denominator in the Learning DB. `book_decisions` provides Focus-linked preliminary Book observations, not complete bid/ask snapshots for all candidate and matched non-signal markets at 0, 5, 15, 60 minute horizons. User-selected Paper OPENs are not unbiased trade observations.

## Prospective unmodified strategy comparison

A = Full Diamond + Risk PASS + natural Focus READY;
B = fixed simple positive 5m/15m BUY Flow rule;
C = matched at-risk non-signal market observations.

For all three, collect **as-of** outcome/market identity, timestamp of source event and independent receipt/decision times, market type/category, price/expiry strata, both-sided book depth and freshness, fee metadata, predeclared exclusion rules, at-risk population including LOW and rejected candidates, and eventual settlement identity/outcome. Use separate first_signal/LOCKED/READY and +5/+15/+60m timestamps. No post-event lookahead.

Shadow P&L = simulated ask VWAP entry to future bid VWAP exit with full depth, taker fees, quoted slippage and explicit failed/missing fills. Compare market/event-clustered held-out net A-B and A-C with uncertainty bands. Do not train weights or modify Risk/Focus based on early data. Overlapping windows and multiple outcomes of the same condition are statistically dependent.

## Minimal instrumentation decision

No trading-logic instrumentation was wired in this first live gate, because the chain-tip Collector and natural Focus READY haven't been shown to work in a current live test. The new observer is passive; it does not mutate Learning SQL schema or gate outcomes. If local diagnostics show a functioning pipeline, the subsequent tightly scoped effort should add a passive **full-universe + timed bid/ask observer**, not redesign Diamond.

**No out-of-sample results, MFE/MAE, win-rate, resolution calibration or net returns are established by this branch.**
