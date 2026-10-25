# GENERAL1-FINAL-GATE-1 — Offline Integration Regression

**Mode:** no Polygon RPC, no network CLOB, no wallet/order calls, no historical/production DB, no live signal injections. All runtime-like inputs are deterministic offline temp-directory fixtures.

## Baseline

- Original Draft PR #47 head `5f27710ee3761a22019488dd218adb93656a1f0d`.
- Baseline full project: **499 passed, 61 subtests passed**; `run_machine.py --self-test` PASS.
- Detected weaknesses: early final Paper gate did not guard a later actual Paper state save; tick-start time reused at commit after network delays. These were demonstrable from call ordering and addressed with deterministic interleavings, not relaxed thresholds.

## Test additions and observed results

| Test | Injection | Expected / observed |
|---|---|---|
| After-early-gate INVALIDATED | Actual Focus runner publishes Risk-BLOCK-triggered invalidation during later MARK | Final Paper OPEN becomes REJECTED, zero position |
| After-final-read INVALIDATED | Thread barrier stops Paper inside final state save; competing Focus runner attempts transition | Focus lock denies overtaking; after Paper commit Focus can invalidate |
| Slow Book/settlement freshness | Test clock advances by +121s at final commit | Stale Focus/Book/request rejected, zero OPEN |
| Book generation swap | Replaces published Book generation during later MARK | Final generation check rejects |
| Book publish during Paper final lock | Book worker attempts authorized output write under shared lock | Book write blocked until Paper release |
| Concurrent Paper workers | `threading.Event` barrier holds one Paper writer while another starts | Second writer blocked, first opens once |
| Crash after Paper save | Force failure during event append after Paper state write | Durable request remains processed; restart cannot re-open |
| Existing durable Focus recovery | Remove/corrupt durable state while view still READY | No OPEN |
| Existing multi-event Focus | Four canonical-looking offline events, spaced ≥60s | LOCKED → WAIT 1/3 → WAIT 2/3 → READY |
| Existing settlement, CTF/V3, evidence, fee/depth and identity | Existing mocked/checkpoint fixtures | No Safety regressions |

## GitHub CI

**Final code CI**: https://github.com/mudrjafar/PolymarketResearch/actions/runs/40227202624 — SUCCESS on `298fbffb9fc6c73be8dee3cbfc5ecfe9043f015b`.

- Focused Collector / Risk / Focus / Book / Paper tests: **293 passed, 61 subtests passed**.
- Full project `python -m pytest -q`: **507 passed, 61 subtests passed**.
- `python run_machine.py --self-test`: **SELF-TEST OK / ALL TESTS PASSED**, Risk 7/7, Focus Engine/Runner, Book Engine/Worker, Paper Engine/Worker PASS.
- Six core-module syntax compile stage passes; additional changed Focus Runner and Book Worker are imported and executed by full pytest/self-test.
- Docs-only head commits trigger a new CI run. Final PR approval must refer to the CI for the **actual final PR-head SHA**.

## Historical failures and fix scope

Previous integration history included a copied test referencing an excluded E2E2 probe module (resolved and excluded from minimal core in the earlier PR #45). The GENERAL1-FINAL-GATE-1 branch's observed GitHub offline CI iterations **passed**; there are no observed failing workflows on this new branch up to the successful final-code run. This is not proof of zero possible bugs.

## Boundaries

This regression establishes the cooperating local-worker, shared-lock and synthetic-Focus sequence, **not** live RPC coverage, genuinely occurring READY, real CLOB timestamp units, multi-host correctness, settlement/provider uptime, strategy profitability or real orders.

**Test decision: PASS** for a draft, main-targeted offline integration candidate.
