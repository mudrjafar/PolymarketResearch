# GENERAL1-INTEGRATION-REVIEW-1 — Minimal Dependency & Merge Matrix

**Comparison:** main `97d88131` → READY-1 `d61fff69`. 81 historical commits ahead, 34 changed files. **The integration branch does not merge these commits**; it begins at main and carries targeted file-content changes only.

| Component | Already in main | Delta selected for integration branch | Independent tests | Direct dependencies | Decision |
|---|---|---|---|---|---|
| Collector V4 / SQLite | Yes, `collector_storage_v4` | `scripts/live_active_trades.py` V3 identity provenance, lookup and strict optional batching; no storage schema alteration | `test_collector_sqlite.py`, curated Gamma tests | Existing storage+V3 decode+RPC parser | **COPY + REVIEW** |
| Legacy Gamma batch | Previous singleton lookup | ≤12 exact CTF IDs, open then missing closed, singleton fallback, response validation; default OFF | `test_integration1_gamma_core.py` | Collector cache/identity, same uncommitted scan; only legacy CTF | **INCLUDE OPT-IN ONLY** |
| Flow / evidence triple | V4 Flow already | Canonical same-event triple passed through confirmation state | `test_flow_unit2.py`, Risk Focus tests | Blockchain tx hash, log index, timestamp | **COPY + REVIEW** |
| Diamond immutable generation | Exists | V3 Data API handling and conservative block time normalization | `test_diamond_unit3.py`, Risk generation tests | V4 Flow, frozen generations | **COPY + REVIEW** |
| Risk → Focus | Risk/runner exist | Focus condition+outcome mismatch invalidation | `test_general1_ready1.py`, Focus/Risk suites | Archived Risk generation and exact evidence cursor | **COPY + REVIEW** |
| Book identity | Worker and Engine exist | Require both CLOB `asset_id` + `market` rather than accepting missing fields | `test_book_engine.py`, `test_book_worker.py`, READY tests | READY view, exact token and condition | **COPY + REVIEW** |
| Paper final gate | Worker and Engine exist | Revalidate Focus, Book, request after network calls | `test_paper_worker.py`, READY + new race/duplicate tests | Authoritative Focus view + Book + settlement identity | **COPY + REVIEW** |
| Risk engine, Focus runner, Book worker, Paper engine, Settlement engine | All already in main | No source diff; retain existing logic | Existing complete regression | Above output contracts | **NO COPY** |
| Telegram & Dashboard | Already in main | No source diff | Existing self-tests and tests | Optional read/UI only | **NO COPY** |
| Experimental Windows launchers, E2E2/Freshness/Gamma live probes | Absent from main | Intentionally omitted; run from separate experiment branches only | Their own experimental CI, not required for normal machine | Fresh sandbox path, external RPC | **EXCLUDE** |
| Experimental diagnostic markdown/CI and probe-specific tests | Absent from main | Only new integration review docs and isolated offline CI | Core CI excludes probe API | None for production | **EXCLUDE** |
| Other open PRs #9/#24/#35–#43 | Not automatically merged | No bulk merge or silent dependency import | Need separate comparative reviews for alternatives | Different parent branches/historical contexts | **DEFER / REVIEW SEPARATELY** |

## Specific included paths

Production changes (exactly **six**): `scripts/live_active_trades.py`, `scripts/flow_tracker.py`, `scripts/diamond_filter_v3.py`, `scripts/focus_engine.py`, `scripts/book_engine.py`, `scripts/paper_worker.py`.

Offline testing: updated `tests/test_collector_sqlite.py`, `tests/test_flow_unit2.py`, `tests/test_diamond_unit3.py`, copied `tests/test_general1_ready1.py` plus core-only `tests/test_integration1_gamma_core.py`; a dedicated review CI workflow.

## Required order for independent merge review

1. Confirm branch HEAD and compare output contains no other runtime source changes.
2. Review Collector/V3 identity and Flow triple before approving the batch cache.
3. Verify Diamond archived generation binding, Focus confirmations and exact market identity.
4. Review CLOB response shape, Book identity and execution-depth rejection.
5. Review Paper final gate under concurrency and its remaining non-atomicity.
6. Run branch-HEAD focused + full offline CI and `run_machine.py --self-test`.
7. Keep `COLLECTOR_GAMMA_BATCH_LOOKUP` OFF by default; require a separate opt-in for any future benchmark.
8. Only after human approval, merge **the reviewed PR**, not the Gamma/READY experiment branches wholesale. No merge happens automatically in this task.

## Non-negotiable acceptance checks

- No direct real orders or key handling.
- No synthetic live READY. Synthetic offline fixtures are permitted.
- No weakened Risk/Book/Settlement gates.
- No false claim that unsupported V3 events/backfill were completely captured.
- No databases, .env, launch scripts, logs, production/previous sandboxes or snapshots copied into integration.
- A Draft PR only; `main` SHA must remain unchanged.
