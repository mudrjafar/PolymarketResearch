# GENERAL1 — Collector Live Evidence · 30 October 2026

## Verified real-world observations

**Run 1:** [Actions 41066783110](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41066783110), 75.50s, 15 blocks/281 trades, rate 0.2488 vs chain 0.6799 blocks/s, lag 529→555, one fatal Collector exit (root exception text not available in sanitized first artifact). FAILED single-run smoke.

**Run 2:** [Actions 41067595593](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41067595593), 300.04s, 220 blocks/6,532 trades from 772 conditions, 44 successful batches and zero unexpected exits. Collector 0.7721 vs chain 0.6654 blocks/s; lag 529→500. 862 Gamma fallback resolutions were classified 831 active/open, 31 historical, 0 actual lookup failures. **PASS for sustained backlog catch-up throughput; not near-tip readiness.**

**Run 3 — target configuration:** [Actions 41068606756](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41068606756), HEAD `e9921344961b9803347ec66ce7697fe6cf2d6a5e`; 2026-10-30 14:04:44Z to 14:09:44Z, **300.03s**, Python 3.11 GitHub runner, public `polygon.drpc.org`:
- Verified effective settings: **30 initial lookback**, **20 confirmations**, optional Gamma batching OFF.
- **227 consecutive durably stored blocks**, 5,619 normalized fills, 787 distinct condition IDs; all persisted records classified `CTF_TOKEN`.
- **Collector 0.8006 blocks/s vs Polygon 0.6684 blocks/s** (~1.20× chain rate). Head-checkpoint lag **60→24** (−36 blocks). The raw head/checkpoint lag includes the configured confirmation depth; no claim of zero age.
- **63 SUCCESS batches, zero fatal failures, zero unexpected worker exits**, zero RPC retries across 542 RPC calls.
- **813 Gamma fallback observations**, 812 active/open and 1 historical/inactive; total measured Gamma lookup time 141.98 seconds, which is a major potential performance cost. These are **successful metadata fallback classifications, not 813 failures**.
- 15,573 raw exchange logs inspected; 5,619 normalized fills committed; a raw log is not by definition a supported eligible trade. 73 V3 unsupported events; 0 V3 POSITION persisted. No claim of V3 execution support.
- At the end the active pipeline had 11 verified Flow states, 13 Diamonds, 11 Risk PASS, a natural Focus WAIT 2/3, but **no READY, Book PASS or Paper OPEN**.

## Technical root-cause and remediation

The earlier 500-block default start was the fault of the **audit runner's environment whitelist**, not a proven Collector bug. Even though the GitHub workflow requested `COLLECTOR_INITIAL_LOOKBACK_BLOCKS=30`, the child environment did not inherit it. We repaired `scripts/live_evidence_gate1.py` to validate and forward only the nonsecret Collector lookback/confirmation/batching controls and echo effective numeric settings in the sanitized report. Regression tests also prove sensitive Telegram and wallet variables are not forwarded. The actual near-tip test confirmed the fix.

An earlier transient fatal Collector event is **not reproducible** in the following two full 300s runs; do not speculate about its underlying exception. No Collector error-handling, metadata identity rule, Risk policy or trading threshold was relaxed.

## Decision

**COLLECTOR LIVE: PASS for a bounded 300s near-tip throughput and stability smoke test on the public GitHub runner.** Not a production uptime SLA, not proof of complete V3 coverage, not verification of the user's own QuickNode RPC or Linux/Python3.14 environment. Book/Paper E2E and economic advantage remain inconclusive.

Best next action is to observe a **natural WAIT 2/3→READY event** in a persistent, isolated data-only session while preserving the current immutable focus and evidence; never synthesize or accelerate confirmations.
