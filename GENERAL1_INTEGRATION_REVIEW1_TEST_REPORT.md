# GENERAL1-INTEGRATION-REVIEW-1 — Offline Regression Report

## Baseline, isolation, and implementation evidence

Source: `mudrjafar/PolymarketResearch` GitHub Actions on `feat/general1-integration-review1-core-20261025`, branched directly from `main` `97d881311a68c54ec15a4f537d8a055e04729f44`.

Only copied six existing core source file deltas, updated relevant offline Collector/Flow/Diamond test suites, a curated Gamma-batch core suite, READY-1 positive/negative fixture suite, and review docs/CI. No E2E2/near-tip live probe, one-shot Windows launcher, older sandbox snapshot, .env, wallet, Polygon RPC, CLOB live call or real/paper automatic OPEN request.

## CI record

**Verified complete implementation CI run:** https://github.com/mudrjafar/PolymarketResearch/actions/runs/40147605685 — **SUCCESS**, head `9a2058e5c7021c9dffad8781fde8f58477eae613` (final code modifications).

- Focused command (`pytest -q` over integration Gamma core, READY-1, Collector, Flow, Diamond, Focus, Risk, Book, Paper and Settlement): **279 passed, 61 subtests passed**.
- Entire project `python -m pytest -q`: **493 passed, 61 subtests passed**.
- `python run_machine.py --self-test`: **SELF-TEST OK / ALL TESTS PASSED**, including Risk 7/7, Focus Engine/Runner, Book Engine/Worker, Paper Engine/Worker.
- Compile stage: all six modified Python core modules `py_compile` PASS.
- Final docs-only commit triggers same CI; consult latest workflow status at branch HEAD before review approval.

### Regression failure detected and resolved while preparing this branch

Initial CI `40147430537` reported **1 failed, 276 passed, 61 subtests passed** in targeted tests, because `tests/test_collector_sqlite.py::CollectorV3CoverageTests::test_e2e2_live_identity_probe_codes_and_no_raw_ids` imported `scripts/general1_e2e2_v3_data_probe.py`. That file is an isolated experimental observer deliberately **not** included in the core integration. Removed **only that 36-line probe-specific test** from the copied Collector test file. Core V3 parsing/identity and full collector regressions remain; no production source was modified to make tests pass. The subsequent full run is green.

This explains why the minimal project's test count differs from READY-1 branch's 525: the old branch includes many probe/launcher-specific tests and files that are intentionally absent here. Comparing total test counts alone is not evidence of reduced safety for the core path.

## Positive evidence sequence

`tests/test_general1_ready1.py` uses actual Risk assessment and candidate publication, durable Focus runner, Book worker, Paper worker and on-disk isolated JSON artifacts; remote CLOB, market info and settlement checks are mocked at network boundaries. All evidence in this test is **synthetic offline**, never published to production or Live Focus.

| Sequence | Clock/event time since baseline | Expected |
|---|---:|---|
| Risk PASS / evidence A | 0 s | LOCKED 0/3 |
| Risk PASS / fresh evidence B | +61 s | WAIT 1/3 |
| Risk PASS / fresh evidence C | +122 s | WAIT 2/3 |
| Risk PASS / fresh evidence D | +183 s | READY 3/3 |
| User request absent | after READY | Paper positions remain 0 |
| Book PASS + explicit $25 Paper request | after READY | Virtual OPEN with bound IDs, depth and fee-aware entry |
| Replay/restart | next tick | No second virtual OPEN |

## Additional negative coverage

- Duplicate evidence despite a new Risk generation; decreasing cursor; too-short evidence event-time spacing.
- Risk hard block / market not active; token-identical but condition/outcome-divergent Risk PASS rows.
- Missing CLOB token and market identity; stale Focus or stale Book; mismatched generation/evidence/request.
- CLOB exit/entry insufficient depth; UNKNOWN/resolved settlement rejection; no eligible Book until Focus READY.
- Focus WAIT **or INVALIDATED** during fresh Book fetch; final Paper gate rejects.
- Book evidence changed during fresh Book fetch; final Paper gate rejects.
- Two distinct explicit Paper OPEN requests on the same token: only first opens, second rejects `POSITION_ALREADY_OPEN`; new worker tick does not duplicate durable state.
- Separate tests cover corruption, outcome settlement identity, unsupported V3 position family, canonical log dedupe, no checkpoint advance on unresolved assets, Gamma open/closed/malformed/unknown fallback, RPC retry and backwards-compatible outputs.

## Explicit limits

- The tests establish deterministic software behaviour, not real-world READY frequency, profitability, end-to-end historical chain completeness or complete support of all V3 variants.
- The successful earlier Gamma 300s near-tip run is separate. Gamma batching is **OFF by default** in normal run_machine.
- CLOB `timestamp` semantics and cross-process final-check atomicity are not fully proven. Paper final gate is a best-effort second read, not a transactional compare-and-swap.
- No new live Polygon/CLOB queries were used to validate this integration.
- No merge to `main` was performed.

## Test-based decision

**READY_FOR_MAIN_INTEGRATION_REVIEW**, conditional on the last branch-HEAD CI being green and an independent human accepting or assigning the documented timestamp, Focus projection and concurrency risks. This outcome means the **minimal PR can be reviewed**, not that it is authorised for merge, unattended Paper entry or real-money trading.
