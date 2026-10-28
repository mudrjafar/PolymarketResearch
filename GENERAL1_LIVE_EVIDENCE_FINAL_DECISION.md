# GENERAL1 LIVE EVIDENCE GATE 1 — FINAL DECISION

**Date:** 2026-10-28.
**Mode:** repo/CI inspection, passive observer implementation, negative tests; **no user-local live run**.

## A. Verified baseline

- `main`: `97d881311a68c54ec15a4f537d8a055e04729f44`.
- PR #52: `f379ac3af68d3fbe8159226ec2cd4cca9059dc26` (OPEN, not merged; 549 full pytest + 61 subtests and machine self-tests green in exact HEAD GitHub Actions `40678873832`).
- Audit predecessor: `audit/general1-quant-validation1-20261028`, `c1cfb59ca50b4d47a6de51ad1f11ecb55781d3fa`, four audit tests PASS.
- Current isolated gate: `audit/general1-live-evidence-gate1-20261028`, based on prior audit HEAD. Adds `scripts/live_evidence_gate1.py`, `tests/test_live_evidence_gate1.py`, dedicated offline CI, these reports. Core Collector, Flow, Diamond, Risk, Focus, Book, Paper and Trading policies UNCHANGED.
- Historical PR #43 815.03-second run was PARTIAL: collector 225 blocks/5,870 trades, 0.276 vs 0.667 blocks/s, lag 521→630, six restarts. Different branch/time; not evidence the new branch is now working.

## B. Actual live results

**NOT RUN HERE — NO CONFIGURED POLYGON RPC AND NO USER-LOCAL DATA ROOT.** This session cannot access the user's Linux laptop or execute its network/configuration-dependent services. GitHub CI is offline. The script provides a safe local procedure; no measured current chain rates, retry counts, runtime restarts, metadata conflicts or fresh CLOB quote ages exist to report.

## C. Signal Funnel

See `GENERAL1_SIGNAL_FUNNEL_REPORT.md`. No natural READY witnessed in the current session. A READY not observed in a short run is not a FAIL. Book/Paper E2E is **not** considered demonstrated.

## D. Economics

**BLOCKED** for a valid A (Diamond), B (simple Momentum), C (matched control) comparison. Current Learning excludes LOW-only/no-signal market universe; future executable two-sided CLOB quotes and matched category/time conditions are missing. No net positive expectancy claim is authorized.

## E. Classified findings

| Severity | Evidence | Operational/economic impact | Minimum next action |
| --- | --- | --- | --- |
| HIGH | PR #43 original runtime report | Collector fell further behind chain in historical real run; no proof latest patches fixed it | New sandbox chain-tip test |
| HIGH | `scripts/learning_queue.py` filters LOW-only generations | No unbiased C baseline or complete denominator | Passive at-risk control snapshots, AFTER runtime verified |
| HIGH | No user-local `collector.sqlite3`/`learning.sqlite3` attached | Current counts and natural READY unmeasurable | Run read-only diagnostics locally |
| MEDIUM | `focus_engine.py` 3 confirmations >=60s | Signal-to-READY price edge may decay | Compare signal-time vs READY and forward bid markouts |
| MEDIUM | `dashboard.py` paper_trades legacy read; #52 F3/F4 | Dashboard/journal cannot be treated as audit-grade Paper truth | Use authoritative paper_state and reconcile before reporting P&L |
| MEDIUM | `book_engine.py` book_ok checks depth and identity, not alpha | Book PASS does not imply positive net EV | Record executable ask/bid, fee and markout |

## F. Binding decision

- **COLLECTOR LIVE: INCONCLUSIVE**
- **PIPELINE E2E: INCONCLUSIVE**
- **NATURAL FOCUS READY: NOT OBSERVED IN CURRENT SESSION**
- **PAPER TEST READINESS: NO-GO**
- **QUANT VALIDATION READINESS: BLOCKED**
- **REAL-MONEY READINESS: NO-GO**

## G. Minimal executable next step (Ubuntu/Linux)

Do this from an existing authenticated local clone (`~/PolymarketResearch` is an example) with a trusted non-secret `.env` containing `POLYMARKET_RPC_URL`. **Do not paste the .env, API URL, private keys or local raw logs.**

```bash
cd ~/PolymarketResearch
git fetch origin audit/general1-live-evidence-gate1-20261028
git worktree add --detach "$HOME/PolymarketResearch-livegate-1" FETCH_HEAD
cd "$HOME/PolymarketResearch-livegate-1"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/live_evidence_gate1.py --run-seconds 300 --env-file "$HOME/PolymarketResearch/.env" > "$HOME/general1_livegate_result.json"
```

The separate worktree must not contain prior `data/`; this is enforced. If the originating repo is at a different path, adapt the two `~/PolymarketResearch` occurrences. If your Polygon RPC is *already in the calling process's environment*, the `--env-file` option may be omitted. The tool only reads that one setting from the file and never copies `.env` to the sandbox.

- This starts public read-only Polygon/Gamma/CLOB collection and the seven allowlisted observational workers, writing **only under the fresh worktree's data/**. No Paper, Telegram, real orders or automatic requests are started.
- The `> "$HOME/general1_livegate_result.json"` file contains only sanitized counters and clock/head/checkpoint metrics. The same output is saved inside the sandbox as `data/livegate_sanitized_report.json`. **Only provide this sanitized JSON back**, never the raw sandbox logs or databases.
- To check *existing* local data, use the READ-ONLY original audit: `python3 scripts/quant_validation1.py --data-dir /path/to/existing/data` (output contains local path and possibly user-selected P&L, so review/redact that output before sharing); alternatively the sanitized `live_evidence_gate1.py --data-dir /path/to/existing/data`.
- The 300s wall-time and performance are estimates only for user-controlled sandbox; no result or LIVE PASS is implied until reviewed.

**Next decision after sanitized report:** a) identify if Collector catches up and all services stay alive; b) if no, fix the single dominant bottleneck; c) if yes, schedule a longer naturally sampled READY/quote and full-universe shadow observation. Do not automatically approve Paper or change strategy thresholds.
