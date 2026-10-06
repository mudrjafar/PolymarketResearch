\# PHASE 1 — RESTORE THE TRUSTED CORE PIPELINE



Read `CLAUDE.md` first.



This is an IMPLEMENTATION task.



Do not ask the user repeated questions. Work through this task completely, test your work, write the report, then STOP.



\## GOAL



Restore the active Flow Tracker + Diamond Filter to the already-intended tested semantics without changing trading strategy or numeric thresholds.



The existing `CLAUDE.md` invariants and regression tests are authoritative.



The `\*\_BEFORE\_V3\_3.py` files are reference implementations, NOT files to blindly overwrite from.



Compare them with the active implementation and port only the correct missing behavior.



\---



\## STEP 1 — SAFETY SNAPSHOT BEFORE EDITING



Before modifying source code:



1\. Confirm no project workers are currently running.

2\. Create a timestamped source snapshot OUTSIDE the project under:



`C:\\PolymarketResearch\\V3\_test\\\_snapshots\\`



3\. Include:

&#x20;  - source

&#x20;  - tests

&#x20;  - launchers

&#x20;  - documentation

&#x20;  - current non-secret configuration structure



4\. Exclude:

&#x20;  - `.env`

&#x20;  - `.venv`

&#x20;  - `\_\_pycache\_\_`

&#x20;  - caches

&#x20;  - temporary files

&#x20;  - large runtime logs



5\. Create a SHA256 manifest for the snapshot.



6\. Verify the snapshot before editing.



If snapshot creation fails:

STOP.



Do not expose secrets.



\---



\# STEP 2 — DETERMINE CORRECT LINEAGE



Compare:



Active:

\- `scripts/flow\_tracker.py`

\- `scripts/diamond\_filter\_v3.py`



Reference:

\- `scripts/flow\_tracker\_BEFORE\_V3\_3.py`

\- `scripts/diamond\_filter\_v3\_BEFORE\_V3\_3.py`



Also inspect:

\- `tests/test\_regressions.py`

\- `tests/test\_machine\_v3.py`



Do NOT choose a version based on filename or timestamp alone.



Use:



1\. CLAUDE.md invariants

2\. regression tests

3\. existing downstream consumers

4\. current collector schema



as the authority.



Do not replace entire files if a smaller controlled merge is safer.



\---



\# STEP 3 — RESTORE TOKEN-LEVEL MARKET IDENTITY



This is CRITICAL.



Flow state must not aggregate YES and NO together.



The authoritative market identity must preserve:



`condition\_id + token\_id + outcome`



At minimum:



\- group Flow by token/outcome, not condition alone

\- persist token\_id

\- persist outcome

\- retain condition\_id for parent-market relationship

\- verification state must not cross between YES and NO

\- latest evidence must belong to the same token/outcome

\- Diamond must consume the correct token-specific Flow



Do not use market title/question as authoritative identity when stable IDs exist.



Drive this change using:



`test\_token\_separation\_full\_cycle\_and\_empty\_cleanup`



Add/update implementation only.



Do NOT weaken the test.



\---



\# STEP 4 — RESTORE EVENT-TIME WINDOWS



1m / 5m / 15m must be rolling event-time windows.



Primary timestamp:



`block\_timestamp`



Fallback to `detected\_at` ONLY when legitimate historical/legacy data has no usable block timestamp.



Required semantics:



1m = previous 60 seconds

5m = previous 300 seconds

15m = previous 900 seconds



Do not use ingestion delay as market activity.



Do not reset windows at clock boundaries.



Do not change numeric window durations.



Preserve the collector's real block timestamps.



\---



\# STEP 5 — RESTORE REQUIRED FLOW OUTPUT FIELDS



Restore the active Flow output fields required by downstream logic where supported by the existing tested implementation:



\- schema\_version

\- source\_updated\_at

\- last\_trade\_at

\- direction

\- token\_id

\- outcome

\- condition\_id



Freshness timestamps must reflect the appropriate underlying evidence.



Do not fabricate missing values.



\---



\# STEP 6 — RESTORE DIAMOND API / FRESHNESS SAFETY



Restore the tested active interface expected by regression tests, including `analyze()` if that is the intended tested interface.



Diamond must reject or downgrade stale information.



Restore freshness gates from the tested/reference implementation where consistent with CLAUDE.md.



A stale VERIFIED Flow must not remain eligible for DIAMOND indefinitely.



Do NOT change numeric Diamond thresholds.



\---



\# STEP 7 — FIX EVIDENCE SEMANTICS



Confirmations must be token-specific.



Old evidence must not advance confirmation again merely because the algorithm reran.



Evidence identity should be based on stable blockchain identity where available:



transaction\_hash + log\_index



Do not require `detected\_at` as part of identity unless needed for legacy compatibility.



A trade for the opposite token/outcome must not confirm the current token.



Do NOT redesign Focus logic here.



Focus does not belong to Phase 1.



\---



\# STEP 8 — FIX VERIFIED REGRESSION DEFECTS IN SCOPE



Fix these implementation defects when they are directly related to the restored core:



\### Path compatibility

`\_save\_json\_atomic` must work with pathlib `Path` and string paths.



\### accepting\_orders

Use the correct normalized metadata field.



Do not silently ignore `accepting\_orders=False`.



\### stale signals.json

Legacy `signals.json` must NOT override fresher authoritative Flow fields.



If compatibility is needed, legacy data may fill genuinely missing non-authoritative fields only.



Fresh Flow wins.



\### resolution timing mismatch

Do not create a false resolution conflict merely because one module calls >24h FAR while another considers ≤72h closing soon.



Use one coherent meaning or normalize the comparison without changing trading thresholds.



Document the decision.



\---



\# STEP 9 — PRESERVE STRATEGY



DO NOT change these values merely to make tests pass:



\- minimum trade counts

\- minimum volume

\- 1m / 5m / 15m durations

\- directional-strength thresholds

\- required confirmations

\- whale concentration threshold

\- Diamond score thresholds

\- entry thresholds



Zero Diamonds remains acceptable.



This phase fixes correctness, not profitability.



\---



\# STEP 10 — DO NOT TOUCH YET



Do NOT implement in this phase:



\- Risk Engine

\- Focus Controller

\- Focus Monitor

\- READY

\- INVALIDATED

\- challenger logic

\- historical replay

\- live trading

\- wallet/order execution

\- new Telegram commands

\- major supervisor rewrite

\- performance refactor unrelated to correctness



Those are later phases.



\---



\# STEP 11 — FIX THE BROKEN DIAMOND LAUNCH ARGUMENT



After core tests are working:



Inspect `run\_machine.py`.



It currently launches Diamond using an unsupported `--watch` flag.



Make the smallest safe launcher correction so the command matches the actual supported Diamond CLI.



Do not redesign the supervisor yet.



Do not launch live RPC/Telegram services during this task.



Verify the command parser offline.



\---



\# STEP 12 — TEST UNTIL STABLE



Run with secrets unset and network disabled where appropriate.



Run:



1\. `tests/test\_machine\_v3.py`

2\. `scripts/diamond\_filter\_v3.py --self-test`

3\. `tests/test\_regressions.py`

4\. `collector\_storage\_v4/test\_storage.py`

5\. `tests/test\_collector\_sqlite.py`



Target:



ALL existing safe tests PASS.



Current baseline was:



46 total

35 PASS

11 ERROR



Desired Phase-1 result:



46 PASS

0 FAIL

0 ERROR



If some failures are genuinely outside Phase 1:



Do not fake a PASS.



Document them precisely.



Do not modify a correct test merely to achieve green status.



\---



\# STEP 13 — VERIFY DOWNSTREAM COMPATIBILITY



Without connecting to Telegram:



Verify statically/offline that active Diamond output now contains the fields expected by:



\- `telegram\_bot.py`

\- `dashboard.py`



Specifically check:



\- schema\_version

\- source\_updated\_at

\- last\_trade\_at

\- direction

\- market identity



Do not send Telegram messages.



Do not require Telegram credentials.



\---



\# STEP 14 — CHANGE CONTROL



Before finishing:



Compare changed files against the pre-change snapshot.



Only modify files required for this Phase-1 task.



Do not clean up unrelated obsolete files yet.



Do not delete backups.



\---



\# REQUIRED REPORT



Overwrite:



`CLAUDE\_REPORT.md`



with the Phase-1 implementation report.



Use:



\## PHASE

Phase 1 — Core Recovery



\## SNAPSHOT

Path and verification result.



\## FILES CHANGED

Exact list.



\## LINEAGE DECISION

Explain which logic was restored from which source and WHY.



\## MARKET IDENTITY

Before vs after.



\## EVENT TIME

Before vs after.



\## EVIDENCE / CONFIRMATIONS

Before vs after.



\## FLOW OUTPUT SCHEMA

Fields restored.



\## DIAMOND / FRESHNESS

Changes made.



\## LEGACY SIGNAL HANDLING

What changed.



\## LAUNCHER

What changed.



\## TEST RESULTS

Exact tests and totals.



\## DOWNSTREAM COMPATIBILITY

Telegram/Dashboard compatibility check.



\## STRATEGY CONFIRMATION

Explicitly confirm numeric strategy thresholds were not changed.



\## SAFETY

Confirm:

\- no live orders

\- no wallet interaction

\- no secrets printed

\- no live Telegram

\- no unrelated file deletion



\## UNRESOLVED

Only genuine remaining issues.



\## NEXT

Exactly ONE recommended next phase.



\## FINAL STATUS



PASS

WARNING

or

FAIL



Do not claim PASS unless Phase-1 objectives and relevant tests are satisfied.



After writing the report:



STOP.



Do not start Risk/Focus implementation automatically.

