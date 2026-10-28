# GENERAL1 FINAL MVP — PHASE 1 INDEPENDENT INTEGRATION REVIEW

Date: 2026-10-28. Reviewer: independent GitHub source/CI review.
Mode: OFFLINE, DRAFT, NO MERGE, NO LIVE ORDERS, NO LIVE TEST.

## Exact authority

- `main` ancestor: `97d881311a68c54ec15a4f537d8a055e04729f44`
- PR #48: `2c4ff4aee030b21fbb3b636d9dfa64e4f21a9191`
- PR #49: `96a1ccf8fc0e65e3ddf06c7d0efcb60a4f2460b9`
- PR #50: `5570535a86e7c02b937c2ab6747e05083ffc78ea`
- PR #51 (alternative, NOT included): `d914a0abc55c0d798ddbc79b20ed034b5b74d7a3`
- PR #52 (selected): `f379ac3af68d3fbe8159226ec2cd4cca9059dc26`

GitHub compare confirms PR #52 is 52 commits ahead and 0 behind the above `main`, and contains the earlier stacked PR ancestry. Do not independently merge #48–#52 on top of the candidate. PR #51 and #52 diverge from PR #50 and are mutually alternative implementations.

## Request integrity decision

Selected PR #52 instead of #51. Both timestamp-sort by aware UTC submission time with request-ID tie-break and explicitly reject invalid dates. Both prevent same-tick CLOSE of provisional OPEN. PR #52 additionally preserves terminal outcomes after late OPEN revocation and enforces one terminal event for every newly consumed request ID before committing state. Its negative tests cover UTC offset normalization, corrupted time, revised request files, duplicate IDs, Focus and Book revocation, close of a previously committed position, and missing terminal event.

Source-inspected `scripts/paper_worker.py`:
- `_load_requests`, `_request_timestamp`, `_run_once_unlocked`, `_assert_terminal_request_events`
- `_process_open`, `_process_close`, `_finalize_exits_and_marks`
- `read_execution_gate`, `_durable_focus_gate`, `_refresh_settlements`, `run_once`

Important limitations:
- `requested_at` is untrusted metadata: not cryptographic arrival order.
- No global distributed transaction: advisory locks require the same local data root and cooperating writers.
- Invariant is only pre-save/in-memory; event journal is appended AFTER state save.
- Existing `MAX_PROCESSED_REQUEST_IDS=1000` uses unsorted `set` conversion and retained request files. Long-run replay cannot be excluded.
- Ubuntu 26.04, real CLOB source timestamp behavior, and sustainable Polygon ingestion not validated.

## Verified source CI — selected final HEAD

https://github.com/mudrjafar/PolymarketResearch/actions/runs/40678873832

- HEAD `f379ac3af68d3fbe8159226ec2cd4cca9059dc26`
- GitHub Actions conclusion: SUCCESS.
- Focused regression: 325 passed + 61 subtests.
- Entire suite: 549 passed + 61 subtests.
- Machine self-test step: SUCCESS.
- Runner: Ubuntu 24.04 / Python 3.11; not a local Ubuntu 26.04/Python 3.14 validation.

PR #51 final CI: https://github.com/mudrjafar/PolymarketResearch/actions/runs/40678806664 (SUCCESS).

## Consolidation mechanism

This candidate branches from the selected PR #52 exact HEAD, whose history already contains #50, #49, #48 and `main`. Only ONE candidate is directed to `main`; upstream PRs remain open and untouched. The candidate adds this report plus a separate offline-only validation workflow. No trading authority, strategies or production code were edited for consolidation.

## Blockers and next gates

F3 (state/event journal crash gap), F4 (non-durable bounded processed-ID history), F50-03 (Dashboard legacy `paper_trades.json`), F50-04 (Book PASS vs Paper actual authorization messaging) are still outstanding. Fix before claiming full MVP reliability. The collector's sustained chain-tip throughput and real quote freshness are unknown; do not claim READY or paper trade success based on offline tests.

Decision: SELECT_PR52_FOR_CONSOLIDATION / OFFLINE-CANDIDATE_ONLY. No merge/launch GO. Candidate must pass its own exact-HEAD CI and further source review before any explicit user merge approval. No live-test authorization.
