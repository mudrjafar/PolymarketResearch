# GENERAL1 — Quantitative Observation Readiness (30 October 2026)

**Research classification: BLOCKED; STRATEGIC EDGE: INCONCLUSIVE.**

## What is actually measurable

The isolated 300.04s public live catch-up run [41067595593](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41067595593) produced 220 durable blocks, 6,532 normalized legacy CTF trades from 772 distinct market conditions, 554 tracked Flow tokens, and 7,893 Learning signal-generation observations with corresponding Risk BLOCK decisions. It did **not** produce verified Flow, a Diamond, Risk PASS, natural Focus READY, Book PASS or any Paper trade. All 539 latest Diamond observations were `DATA_RISK`. Reason-code histograms count overlapping causes, not independent rejected candidates.

The runner accidentally used the 500-block default lookback and finished 500 blocks behind the head. These market observations are real but cannot be treated as near-tip actionable signal evidence. A corrected 30-block test is tracked separately.

## Why an A/B/C performance test cannot yet be run

- Learning's `scripts/learning_queue.py` excludes LOW-only generations. A no-signal eligible market denominator is absent: **selection bias**.
- A) Full Diamond/Focus READY observations: **zero during this 500-block catch-up run**.
- B) Simple independent BUY-flow baseline: possible to define but must be evaluated on the same full predeclared market universe, which is unavailable in Learning.
- C) Matched no-signal baseline: no unbiased at-risk control population.
- Book snapshots: no time-indexed full two-sided depth for matched non-signals or 5/15/60min price markouts; the engine's `Book PASS` is not net-EV proof.
- No platform-validated executable quote age, fees at actual ask/bid depth, matched expiry/price categories or outcome settlement series in a usable prospective sample.
- Paper P&L: no automatic Paper worker was started, and separately selected manual trades are not an unbiased test.

**No hit-rate, win-rate, uplift, realized expected return, MFE/MAE, Brier score, Sharpe or calibration estimate may be claimed from this sample.**

## Minimal preregistered validation when near-tip viability is established

A = first naturally eligible Full Diamond+Risk PASS+Focus READY (record first seen/LOCK/READY separately).

B = fixed rule on 5m/15m net BUY Flow using only information available at decision time and the same condition/token universe.

C = predeclared at-risk market times without signal, matched by event category, price decile, market expiry band, orderbook liquidity and calendar time.

Collect non-signals, rejected candidates, provenance and exchange-source timestamps at all relevant cycles. Save separate bid/ask/depth snapshots at signal, LOCK, READY, 5/15/60 minutes and resolution. Calculate hypothetical net orderbook VWAP entry vs future executable bid minus fee; report no-fill and missing-book censoring transparently. Use market/event-clustered uncertainty and a chronological holdout, never random splitting overlapping repeated-generation observations.

## Scope of the next implementation

The Live Evidence Gate branch deliberately adds **no new trading model, Signal threshold or Risk/Focus bypass**. If the near-tip run proves viable, the next narrow branch should implement a **passive full at-risk universe plus sampled, two-sided CLOB quote recorder**. The recorder must not call any paper/order/open action or modify Decision authority. Historical performance cannot be reconstructed from only the last-trade price.

**Current decision:** QUANT VALIDATION READINESS = BLOCKED; REAL MONEY = NO-GO.
