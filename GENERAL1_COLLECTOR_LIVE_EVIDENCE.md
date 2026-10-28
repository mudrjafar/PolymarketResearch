# GENERAL1 COLLECTOR LIVE EVIDENCE

**Status:** NOT EXECUTED IN THIS SESSION / INCONCLUSIVE.

The current working environment has no GitHub clone at a local filesystem path and no `POLYMARKET_RPC_URL` setting. GitHub connector can inspect code and CI but cannot run the user's Linux host or access its local private RPC. A 300-second run was therefore NOT claimed.

## Actual new implementation

- `scripts/live_evidence_gate1.py --run-seconds 300 --env-file ...` starts a **fixed allowlist only**: collector, flow, diamond, risk, focus, book, learning.
- Explicitly does not start `paper_worker.py`, `telegram_bot.py`, the supervisor or any wallet/real-money execution.
- Refuses startup unless `BASE.name` contains `livegate` **and** its local `data/` is empty/absent.
- Reads Polygon chain ID 137 and head via the configured HTTPS RPC; never reports or stores the RPC URL in sanitized output; it parses only `POLYMARKET_RPC_URL` from an optional .env text file.
- Samples chain head and durable Collector checkpoint up to every 15 seconds; captures sample lag, trade counts, chain and collector block rates, stages, and process exits.
- Stores raw process logs **locally**, not for sharing, at `data/livegate_logs/`; saves only aggregated counts and reason codes to `data/livegate_sanitized_report.json`.
- The result is not an automatic PASS: inspect source identity, exit codes, lag trend and logs before any trading gate approval.

## Verifiable predecessor evidence — separate legacy run

PR #43 `GENERAL1_E2E2_PRELIVE_REPORT.md` records a previous 815.03s Windows experiment: 225 blocks, 5,870 trades, collector 0.276 blocks/s vs chain 0.667 blocks/s, lag 521→630, 6 collector restarts. That particular system did not catch up. Later offline changes might address bottlenecks, but have not been proven by this new run.

## Exact local follow-up

A fresh Git worktree and a user-owned valid Polygon RPC are required. See the step-by-step commands in `GENERAL1_LIVE_EVIDENCE_FINAL_DECISION.md`. No live numbers exist for this audit yet.

## Assessment to apply to actual resulting JSON

- Track `lag_first`, `lag_last`, `lag_delta`, head/collector rates, and `unexpected_exits`.
- If collector is behind, demand catch-up rate > chain rate and decreasing lag. Stationary tip operation demands stable, reasonably low lag instead.
- For block identity, use the unchanged Collector durable checkpoint and canonical `tx_hash+log_index` primary key, but counts alone **do not** prove end-to-end chain completeness or correct identity.
- `CTF_TOKEN` versus `POSITION` are research protocol groups, not a blanket guarantee Paper execution support. V3 Protocol V2 settlement remains excluded by existing gates.
- A 300s time box is a runtime smoke gate, NOT sufficient on its own for signal return statistics or reliability guarantees.
