# GENERAL1-MERGE-GATE-1 — Release / First Controlled Paper Test Checklist

## Confirmed by deterministic offline tests

- [x] GitHub branch is a descendant of reviewed #45+#46, with a narrow diff versus `main`.
- [x] No experimental Windows launchers, live probe modules, secrets, logs or prior sandbox DB copied into the candidate.
- [x] Collector/Flow/Diamond/Risk/Focus/Book/Paper contracts pass the project's full offline suite.
- [x] Canonical V4 evidence and three post-lock confirmations required for Focus READY.
- [x] Locked Focus condition/outcome checks and durable `focus_state.json` ↔ READY compatibility view check.
- [x] CLOB market and token identity required; inadequate ask depth blocks.
- [x] Final Paper gate rechecks focus/request binding after remote read operations.
- [x] Exactly one cooperating Paper writer can edit each Paper STATE_FILE at once via an OS file lock; failure is fail-closed.
- [x] Explicit user Paper request is mandatory; sequential replays idempotent; CLOSE and settlement remain possible after revoked READY.
- [x] No actual orders/wallet keys in the Paper worker.
- [x] Machine self-test and full CI green on implementation commit.

## Still required before merging to main

- [ ] Independent human review of the **single consolidated Draft PR** code and dependency scope.
- [ ] Final branch-HEAD CI success verified, no unreviewed code changes.
- [ ] Explicit user decision approving this precise PR/head SHA; **no merge in this task**.
- [ ] Explicit disposition of remaining cross-process Focus/Paper state atomicity limitation, including whether to require a separate CAS-style gate before even paper-only operational testing.
- [ ] Confirm acceptable CLOB Book timestamp/source freshness and on-chain settlement availability for the planned controlled sandbox.
- [ ] Review opt-in Gamma batching; it remains OFF by default in normal runtime and the earlier 300s run cannot establish long-term performance or historical completeness.

## Before first real-data *paper-only* exercise (later user-authorised task)

- [ ] Create a brand-new isolated sandbox and local DB; do not reuse user production or old test DB.
- [ ] Confirm genuine Polygon chain 137 RPC + safe-tip finality and canonical trade/evidence binding.
- [ ] Observe natural Focus LOCKED → READY with distinct, ordered blockchain confirmations. **Never inject READY into a running live instance.**
- [ ] Fetch fresh, market/token-matching Book with sufficient depth; verify source freshness and cost calculation.
- [ ] Explicit user action submits a permitted virtual $25/$50/$100 Paper OPEN amount.
- [ ] Confirm settlement identity and FINALIZED/UNRESOLVED authority; no live orders or keys.
- [ ] Confirm request idempotency, state integrity and later Paper CLOSE / settlement accounting.
- [ ] Collect non-sensitive, bounded metrics and review findings; do not silently graduate to autonomous trading.

## Disallowed

- Real trades, wallet keys, unauthorised Paper OPENs, synthetic live READY, DB migrations in this task, auto-merge, and claims that offline passes prove profitability or fully atomic cross-process execution.

## Decision

`READY_FOR_USER_MERGE_APPROVAL` means **eligible for deliberate human merge review**, not deployment or real trading authority. If final CI fails or reviewers require stronger atomicity, downgrade to a corresponding BLOCKED state.
