# GENERAL1_REQUEST_INTEGRITY_TEST_REPORT

## Scope and exact source references

Repository: `mudrjafar/PolymarketResearch`; base PR #50 HEAD `5570535a86e7c02b937c2ab6747e05083ffc78ea`.
Implementation/test commit **`22dab81ac93bf7ba8b4d9e9d86314fb4db30fa66`**. Final branch HEAD also contains documentation commits; exact **final HEAD** and matching CI must be checked in the PR after all files are committed. No changes on PR #48/#49/#50 or main.

## Prove old failures (before the implementation)

GitHub Actions red run https://github.com/mudrjafar/PolymarketResearch/actions/runs/40677813722:
- 4 expected focused failures demonstrating chronological misorder, tentative OPEN+CLOSE, dependent CLOSE event loss, and malformed timestamp misclassification.
- 309 other focused tests + 61 subtests passed. These four tests were written on a clean branch from PR #50 before the fix.

## Green offline regression

GitHub Actions successful implementation+test run https://github.com/mudrjafar/PolymarketResearch/actions/runs/40678475606:
- Focused integration: **325 passed, 61 subtests passed**.
- Complete `python -m pytest -q`: **549 passed, 61 subtests passed**.
- `python run_machine.py --self-test`: **SELF-TEST OK / ALL TESTS PASSED** including Risk 7/7 and Focus/Book/Paper Engine/Worker checks.
- Compare to PR #50 baseline: **533 full tests**; **16 more passing regression cases**, including parameterized invalid request timestamp checks.
- Linux GitHub Actions `ubuntu-latest`, Python **3.11**; remote CLOB/RPC and trading endpoints mocked/never used.

## Required scenario matrix (reproduction vs previous coverage)

| Required scenario | Verified via |
|---|---|
| D1 chronological submission despite inverse random lexical filename order | NEW `test_request_order_follows_submission_time_not_random_filename` |
| D2 OPEN→CLOSE same tick cannot close tentative position | NEW `test_close_cannot_execute_tentative_open_in_same_paper_tick` |
| D3 OPEN→CLOSE→OPEN (3 terminal outcomes, one OPEN state) | NEW `test_open_close_open_same_tick_has_one_committed_position_and_three_outcomes` |
| D4 two CLOSEs of the same committed position | NEW `test_two_close_requests_for_committed_position_are_individually_accounted` |
| D5 Focus revoked before final commit; dependent CLOSE survives as explicit REJECTED | NEW `test_rejected_tentative_open_does_not_erase_dependent_close_request` |
| D6 Book generation replaced before final commit; dependent CLOSE never disappears | NEW `test_pending_open_then_book_generation_revoked_preserves_both_request_outcomes`; retained `test_book_generation_changed_after_early_gate_blocks_final_commit` |
| D7 bad requested_at, missing/naive/unparseable, distinguish from expired | NEW `test_bad_request_timestamp_is_distinct_from_expired_request` and parameterized `test_naive_missing_and_invalid_timestamp_fail_closed` |
| D8 equal timestamps deterministically break ties by ID, UTC offsets sorted by instant | NEW `test_same_request_timestamps_tie_break_by_request_id`, `test_timestamp_timezone_offsets_are_normalized_before_sorting` |
| D9 duplicate/replayed ID and changed in-flight file | NEW `test_rewrite_same_request_file_during_fetch_does_not_change_snapshot_or_replay`; existing `test_same_request_is_idempotent` |
| D10 crash after Paper state save before append | Retained `test_crash_after_paper_state_save_does_not_reopen_request`. F3 event lost remains documented; no false claim of journal atomicity |
| D11 user CLOSE after Focus revoked | Retained `test_close_survives_revoked_durable_focus_authority`; no new Focus gate on CLOSE |
| D12 concurrent Paper workers | Retained `test_concurrent_paper_workers_are_serialized_with_thread_barrier` and `test_second_paper_writer_cannot_enter_state_transaction` |
| Different-market request never borrows another Focus's authority | NEW `test_different_market_open_request_cannot_use_current_focus` |
| Deliberately erase a consumed request's event before commit | NEW `test_terminal_event_accounting_fails_closed_before_paper_state_save`; raises and leaves state/journal absent |

Additional retained regressions: exact token+condition+evidence+generation binding, zero network calls on Focus WAIT, invalid CLOB identities, stale source/receipt time, 7s slow-network reply, stale-close rollback, invalid mark-clearing, insufficient book depth, settlement finality, request TTL and Risk/Focus preconditions.

## Explicit caveats

These are **deterministic local Python** tests, not empirical evidence of actual profitable signals or execution against orderbook liquidity. An exact global causal order cannot be established by client-provided wall-clock timestamps; the documented policy uses UTC time + ID tie for current single-host Telegram use. File rewrites by privileged external actors and 1,000-ID retention are outside a forever-idempotent guarantee.

Ubuntu 26.04 / Python 3.14 runtime and Windows filesystem lock behavior are **UNVERIFIED** by this workflow. Do not substitute `ubuntu-latest` Python 3.11 for those platforms.

There is no test or live validation of naturally occurring Focus READY, chain-tip throughput, cache age semantics, or real filled orders. Historical F3/F4 remain known. The dashboard still reads the legacy file.

**Release gate:** new code is an offline-tested security improvement suitable for independent final patch review, **NOT authorisation to merge or initiate Paper/real trades**.
