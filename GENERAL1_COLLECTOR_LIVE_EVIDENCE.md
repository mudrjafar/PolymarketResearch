# GENERAL1 — Collector Live Evidence (30 October 2026)

## Scope and provenance

Two completed independent **real Polygon public-data observations** took place on ephemeral GitHub Actions Ubuntu/Python 3.11 runners. They used the public RPC provider `polygon.drpc.org`, a standalone empty worktree, 20 confirmation blocks, and exclusively public reads. No Paper worker, Telegram, real-money order or private credentials were used. The two live runs mistakenly retained the Collector's default **500-block lookback**, because the sandbox launcher did not forward the `COLLECTOR_INITIAL_LOOKBACK_BLOCKS=30` environment setting. This launcher defect has been fixed on the audit branch and regression-tested (CI 566 full tests + 61 subtests, 17 targeted tests).

**Crucial distinction:** Results below measure *catch-up from a large backlog*, not the intended 30-block near-tip startup.

## Run 1 — aborted after 75.50 seconds

- [GitHub Actions 41066783110](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41066783110), head `6d6cb9e7e56d9bdd2ea020e77a22c7355307cf17`.
- 15 durable blocks; 281 trades; 82 distinct conditions.
- Collector rate **0.2488 blocks/s**, chain **0.6799 blocks/s**; measured lag **529 → 555** (+26).
- Collector unexpectedly exited; LIVE-1C emitted 3 successful batches and 1 `FATAL_FAILURE`. Exact error reason is not contained in that earlier sanitized artifact.
- 105 Gamma fallback lookup observations; cumulative lookup time ~28.08 seconds; 0 RPC retries.
- Only `CTF_TOKEN` trades persisted; no V3 POSITION trades.
- Flow 98 tokens, 0 verified; all 98 current Diamond classes `DATA_RISK`; Risk PASS 0; natural READY 0; Book PASS 0.

**Decision:** FAIL for this observed run; no systemic failure rate inferred from one short sample.

## Run 2 — completed full 300.04 seconds

- [GitHub Actions 41067595593](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41067595593), head `ac970b6feba08ede99a6c2ef33c0f542f6849268`.
- 220 durable blocks; 6,532 trades; 772 distinct market conditions; `CTF_TOKEN` only.
- Collector rate **0.7721 blocks/s**, chain rate **0.6654 blocks/s**; measured lag **529 → 500** (improved by 29).
- **44 successful Collector batches, no fatal failures and no unexpected worker exits**.
- **440** RPC calls, 0 retries; 17,899 raw log entries inspected; 6,532 persisted normalized trades (other logs are not all persisted fills).
- **862 Gamma fallback observations**, 179.36 cumulative lookup seconds. Classification: **831 ACTIVE_OPEN**, **31 CLOSED_INACTIVE_HISTORICAL**, **0 classified lookup failures**. A fallback cache miss is NOT automatically a metadata resolution failure.
- 74 unsupported V3 events logged, 0 V3 POSITION records committed. V3 coverage therefore cannot be inferred from legacy CTF counts.
- Current Flow: 554 tokens, 0 verified; latest Diamond: 539 `DATA_RISK` classes; Risk: 0 PASS / 539 blocked; Focus IDLE/0 READY; Book not ready; zero Paper starts.
- Learning database: 7,893 historical *generation observations* and 7,893 BLOCK decisions in this sandbox; these are NOT independent markets and must not be interpreted as 7,893 unique trades.

**Decision:** PASS for a *bounded 300s uninterrupted throughput smoke test while catching up*; **NOT** a near-tip PASS or economically usable signal pipeline. Even after rate > chain for this window, 500 blocks of backlog remained and all downstream trade-age gates remained blocked.

## Root cause discovery

The intended `COLLECTOR_INITIAL_LOOKBACK_BLOCKS=30` was set in workflow YAML but was absent from the isolated child-process environment whitelist in `scripts/live_evidence_gate1.py`. The ~529-block initial head/checkpoint lag agrees with the untouched 500-block fallback plus confirmations/startup. The launcher now validates and forwards the collector's lookback, confirmation depth and optional batching switch, and records those settings in the sanitized report. This does NOT change the Collector's default or any trading/risk threshold.

## Next evidence

A third isolated public observation with *actual 30-block lookback*, expected in [near-tip GitHub Actions 41068606756](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41068606756), is a separate experiment; do not combine its sample counts with 500-block catch-up runs. Results must be added only after the completed exact-HEAD artifact is read and validated.

**Provisional Collector verdict: CONDITIONAL.** It demonstrated sustained above-chain throughput under one public-provider configuration but has not yet established near-tip fresh-signal coverage, fail-free repeatability or equivalence to the user's own Linux/QuickNode environment. Real-money and automatic Paper permissions remain NO-GO.
