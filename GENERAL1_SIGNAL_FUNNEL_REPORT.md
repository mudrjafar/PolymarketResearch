# GENERAL1 — Natural Signal Funnel · 30 October 2026

**Primary evidence:** [public Polygon near-tip observation, Actions run 41068606756](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41068606756), 300.03s, effective 30-block initial lookback and 20 confirmations. This is an independently observed real network run, not simulated input.

| Pipeline stage | Actual observed (last snapshot except events) | Interpretation |
|---|---:|---|
| Legacy CTF fills persisted | 5,619 | 227 blocks, 787 distinct market condition IDs |
| Flow token states | 950 | 11 verified |
| Diamond analyzed | 914 | 13 DIAMOND, 26 CANDIDATE, 9 VERIFYING, 583 DATA_RISK, 63 LOW, 220 WATCH |
| Risk assessed | 914 | **11 PASS**, 903 BLOCK (overlapping reasons) |
| Focus LOCKED | **1 event** | Naturally generated |
| Focus WAIT | **2 events** | Engine ended at WAIT 2/3 |
| Focus READY | **0** | Not observed in the 300s bounded window |
| Book PASS | **0** | Status NOT_READY, blocker FOCUS_NOT_READY |
| Paper OPEN | **0 by design** | Paper worker never started |

**Important distinction:** The Learning sidecar archived 7,518 selected signal-generation rows and 7,518 corresponding Risk decisions, with 113 PASS and 7,405 BLOCK, **not 113 independent live signals**. A market may recur in many generated snapshots. The 11 Risk-PASS entries in the latest snapshot and the 113 Learning PASS rows refer to different denominators.

Selected latest BLOCK reasons: `CONFIRMATIONS_LOW` 899; `FLOW_NOT_VERIFIED` 899; `WHALE_CONCENTRATION` 643; `STALE_TRADE` 579. Each market may have several reasons simultaneously; do not add these totals.

Focus evidence was sufficient for **LOCKED + WAIT 1/3 + WAIT 2/3**, with the immutable market/outcome/cursor and ≥60-second evidence-time spacing gates left intact. Missing READY is properly classified NOT OBSERVED IN WINDOW, **not** a strategy failure. The last Book assessment said `FOCUS_NOT_READY`; no CLOB depth/quote success or execution realism was established.

## Contrast with stale catch-up

The previous full 300s run [41067595593](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41067595593) inadvertently used 500 initial lookback, ended at lag500, had 554 Flow tracked tokens but 0 verified, 539 DATA_RISK Diamond rows and 0 Risk PASS or Focus events. The corrected near-tip test ended at lag24, with 11 verified, 13 Diamond, 11 Risk PASS and Focus 2/3. These are independent 300-second observational cohorts, not a controlled randomized strategy experiment, so the difference must **not** be ascribed solely to one code change or confused with future profit.

## Remaining evidence gate

Observe a naturally generated 3/3 READY and a **fresh identity-bound Book PASS** with real quote-source timestamps; then, subject to independent review, user-controlled Paper OPEN remains separately authorized and not automated. Record signal/LOCK/READY prices, exact event and receipt times, book source age and blocked causes. This observation must be done without lowering any Risk or Focus threshold.
