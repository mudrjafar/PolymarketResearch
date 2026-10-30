# GENERAL1 — Real Data Baseline · 30 October 2026

**Source:** Three isolated GitHub Actions worktrees observing actual public Polygon chain data via a public Polygon RPC; Ubuntu/Python 3.11; no Paper worker, private credentials or trades. These are independent experiments, NOT the user's historical local files. Original sanitized per-run artifacts are attached to linked workflow runs and expire per workflow retention.

| Metric | Run 1 — aborted | Run 2 — catch-up | Run 3 — near-tip |
|---|---:|---:|---:|
| GitHub Actions run | [41066783110](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41066783110) | [41067595593](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41067595593) | [41068606756](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41068606756) |
| Actual duration | 75.50s | 300.04s | **300.03s** |
| Effective initial lookback | 500 (unintended) | 500 (unintended) | **30 (confirmed)** |
| Confirmation setting | 20 | 20 | 20 |
| Persisted blocks | 15 | 220 | **227** |
| Persisted normalized trades | 281 | 6,532 | **5,619** |
| Distinct condition IDs | 82 | 772 | **787** |
| Collector rate, blocks/s | 0.2488 | 0.7721 | **0.8006** |
| Chain rate, blocks/s | 0.6799 | 0.6654 | **0.6684** |
| Head/checkpoint lag, first→last | 529→555 | 529→500 | **60→24** |
| Unexpected worker exits | 1 collector | 0 | **0** |
| Latest Flow tokens / verified | 98 / 0 | 554 / 0 | **950 / 11** |
| Latest DIAMOND classifications | 0 | 0 | **13** |
| Latest Risk PASS | 0 | 0 | **11** |
| Natural Focus lifecycle | None | None | **LOCKED 1; WAIT 2; READY 0** |
| Latest Book PASS | 0 | 0 | 0; Focus WAIT 2/3 |
| Paper OPEN | 0 (prohibited) | 0 (prohibited) | 0 (prohibited) |

**Caveat:** Runs 1 and 2 unintentionally used the Collector default 500-block lookback because the sandbox launcher initially omitted the configured `COLLECTOR_INITIAL_LOOKBACK_BLOCKS=30` from its child-process allowlist. That test-harness defect is fixed and unit-tested. Run 3 records the actual effective setting in the artifact: `COLLECTOR_INITIAL_LOOKBACK_BLOCKS=30`, `COLLECTOR_CONFIRMATION_BLOCKS=20`, batching off.

## Actual near-tip population (run 3)

- 950 Flow token states; 11 verified.
- 914 Diamond evaluations in the **last snapshot**: 583 DATA_RISK, 220 WATCH, 63 LOW, 26 CANDIDATE, 13 DIAMOND, 9 VERIFYING. Snapshot states are not independent events.
- Risk in last snapshot: 914 assessed, 11 PASS, 903 BLOCK; multiple overlapping reason codes per candidate.
- Learning sidecar: 7,518 selected generation observations, 7,518 Risk decisions, including 113 PASS and 7,405 BLOCK **across repeated generations**. 113 does **not** equal 113 independent trade opportunities.
- Focus events: exactly one LOCKED and two WAIT; Focus ended at WAIT 2/3; zero READY or Book PASS. This is natural chain evidence, not fixture-injected.
- Only legacy `CTF_TOKEN` trades persisted. V3 unsupported events were recorded; these cannot be counted as supported Paper-executable positions.
- No meaningful 5/15/60-minute net trading returns or settled-outcome evaluation are available.

## Limitations

No direct access to the user's own Linux/Windows `collector.sqlite3`, `learning.sqlite3`, `paper_state.json` or local historical P&L. GitHub Actions results prove isolated environment functionality, not exact equivalence to QuickNode on the user's hardware. Counts do not independently prove chain-log completeness or market identity; existing fail-closed canonical identity and checkpoint contracts are covered by offline tests.

For **local strictly read-only** counts on an existing checked-out repository:

```bash
python3 scripts/live_evidence_gate1.py --data-dir data
```

Only share its anonymized result; never send credentials, local DBs or raw process logs.
