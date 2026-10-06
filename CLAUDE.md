\# PolymarketResearch — Claude Code Rules



\## MISSION



This is a Polymarket market-intelligence and paper-validation system.



Goal:

Find rare, explainable, high-confidence opportunities while minimizing false signals.



Signal quantity is NOT a success metric.

Zero Diamonds is acceptable when evidence is insufficient.



Current intended pipeline:



Collector

→ Flow Tracker

→ Diamond Filter

→ Risk Engine

→ Focus Controller

→ Focus Monitor

→ Telegram / Dashboard



Preserve the modular architecture unless there is strong technical evidence to change it.



REAL-MONEY TRADING MUST REMAIN DISABLED.



\---



\## CHANGE LEVELS



\### SAFE — Claude may implement and test directly



\- clear bug fixes

\- regression tests

\- logging improvements

\- performance fixes that do not change strategy

\- validation/error handling

\- crash-loop fixes

\- deterministic state handling

\- Telegram status/reporting improvements

\- launcher/supervisor reliability

\- duplicate-work prevention



\### EXPLAIN FIRST



Before changing any of these, explain the proposal and consequences:



\- Diamond thresholds

\- Flow thresholds

\- scoring logic

\- Risk rules

\- Focus Lock behavior

\- scheduler/state transitions

\- market identity

\- database schema

\- historical replay methodology

\- worker/process architecture

\- major module restructuring

\- deleting or merging files



\### FORBIDDEN



Never:



\- enable real-money trading

\- submit Polymarket orders

\- sign transactions

\- move funds

\- access/expose private keys

\- expose API/RPC/Telegram credentials

\- weaken safety checks just to generate signals

\- fabricate missing data

\- use future information in historical evaluation

\- automatically delete important project files

\- run destructive Git reset/clean operations



\---



\## SAFETY SNAPSHOT — REQUIRED



Before any significant multi-file change, refactor, architecture change, or strategy-related modification:



1\. Check current project state.

2\. Check `git status` if Git is available.

3\. Run relevant existing tests when possible.

4\. Create a recoverable pre-change snapshot.

5\. Verify the snapshot exists.

6\. Only then modify files.



Required sequence:



WORKING BASELINE

→ TEST

→ SAFETY SNAPSHOT

→ CHANGE

→ TEST

→ COMPARE

→ ACCEPT OR ROLLBACK



Never destroy the last known-good version.



If snapshot creation fails:

STOP before making significant changes.



Backups should exclude unnecessary generated data when practical:



\- `.venv`

\- `\_\_pycache\_\_`

\- caches

\- huge temporary/runtime logs



Never expose secrets while creating a backup.



\---



\## ENGINEERING WORKFLOW



For every task:



1\. Understand the requested outcome.

2\. Inspect only relevant files first.

3\. Understand callers/dependencies.

4\. Check existing tests.

5\. Make the smallest logical change.

6\. Run focused tests.

7\. Run regression tests when appropriate.

8\. Inspect the resulting diff.

9\. Report what changed.



Do not rewrite working modules unnecessarily.



Do not expand scope because unrelated improvements were discovered.



Put unrelated findings under NEXT.



Preserve unrelated user changes.



Do not automatically commit or push.



\---



\## MARKET IDENTITY — CRITICAL



Never aggregate flow by market title/question alone.



Correctly separate using appropriate identifiers, especially:



\- condition\_id

\- token\_id

\- outcome



YES and NO flow must never be accidentally combined.



Changes here require regression tests.



\---



\## BLOCKCHAIN EVIDENCE



The same evidence must never count twice.



Reprocessing old evidence must not:



\- increase confirmations

\- advance READY

\- create another Diamond confirmation

\- falsely refresh a signal



Use stable event identity whenever possible.



Event ordering must be deterministic.



\---



\## FLOW / RESCAN LOGIC



Do not fully re-analyze unchanged markets unnecessarily.



Expected priority:



LOW → infrequent

CANDIDATE → more frequent

VERIFYING → frequent

VERIFIED / FOCUSED → highest monitoring priority



Fresh blockchain evidence may trigger earlier evaluation.



The system should know:



\- when a market was last analyzed

\- why it should be analyzed again

\- whether new evidence exists

\- which evidence was already consumed



Avoid expensive scanning simply because a timer fired.



\---



\## DIAMOND RULES



Never lower thresholds merely because zero Diamonds are appearing.



Every Diamond must have an explainable evidence chain.



Consider at minimum:



\- trade count

\- volume

\- directional flow

\- 1m / 5m / 15m agreement

\- single-trade concentration

\- liquidity

\- entry price quality

\- evidence freshness

\- resolution state

\- data confidence

\- contradictory evidence



One whale transaction alone is not sufficient evidence.



Important decisions should remain explainable:



DIAMOND

VERIFYING

WATCH

ENTRY\_BLOCKED

DATA\_RISK

INVALIDATED



\---



\## FOCUS LOCK



When Market A becomes focused:



\- A remains primary while valid.

\- Stronger Market B may become challenger.

\- B must not immediately steal focus.

\- READY progression requires NEW evidence.

\- Old evidence must not increase confirmation count.

\- One temporary failure should not automatically invalidate A.

\- Repeated confirmed failure may invalidate A.

\- After invalidation, the best valid challenger may acquire focus.



Transitions must be deterministic and tested.



\---



\## RISK ENGINE



Strong Flow must never automatically bypass Risk checks.



Risk rejection must remain possible even for a strong signal.



Do not turn safety controls into score bonuses.



When evidence is insufficient or contradictory, prefer:



WAIT

DATA\_RISK

INVALIDATED



over unjustified confidence.



\---



\## HISTORICAL / PAPER VALIDATION



Historical evaluation must obey event time.



At time T, only information available at or before T may affect the decision.



Never use:



\- future trades

\- future prices

\- final resolution

\- later order-book state



to construct an earlier signal.



Resolution data may be used later to evaluate the result.



Explicitly protect against look-ahead bias.



Prefer reusing live production logic in replay rather than maintaining a separate strategy.



\---



\## PERFORMANCE



Identify bottlenecks before optimizing.



Pay special attention to:



\- `live\_trades.jsonl` growth

\- repeated full-file parsing

\- repeated scanning of every market

\- unnecessary JSON rewrites

\- SQLite access

\- duplicate RPC/API requests

\- tight polling loops

\- unbounded memory structures

\- worker restart loops

\- duplicate Telegram alerts



Prefer:



\- incremental processing

\- checkpoints

\- SQLite for structured persistent state

\- cached metadata

\- state-aware scheduling

\- processing only new evidence



\---



\## SUPERVISOR / WORKERS



Final system should support:



\- Collector

\- Flow Tracker

\- Diamond Filter

\- Risk Engine

\- Focus Controller

\- Focus Monitor

\- Telegram Bot

\- Dashboard



The user should eventually start the required system from ONE launcher.



Preferred entry point:



`start\_machine.bat`



or one equivalent supervisor command.



Development may use multiple CMD windows/processes when useful.



Workers must be clearly identifiable.



Avoid:



\- duplicate workers

\- silent crashes

\- uncontrolled restart loops

\- one failed worker unnecessarily killing healthy workers



Worker states should support concepts such as:



RUNNING

HEALTHY

STALE

DEGRADED

FAILED

STOPPED



A process merely existing does not prove it is healthy.



Use heartbeat / last successful cycle / data freshness when appropriate.



\---



\## TELEGRAM — REQUIRED FINAL COMPONENT



Telegram is part of the final system.



It should function as an operator/status interface, not merely spam alerts.



It should eventually be able to report:



\- overall system health

\- worker health

\- Collector freshness

\- Flow status

\- latest Diamond state

\- current focused market

\- challenger when relevant

\- latest important warning/error

\- latest signal explanation



Useful command concepts:



`/status`

`/health`

`/workers`

`/focus`

`/diamond`

`/why`

`/lastsignal`



Keep the command set minimal and useful.



Telegram must NEVER expose credentials.



Telegram must not submit real-money trades in the current system.



\---



\## TELEGRAM ALERT SAFETY



Do not repeatedly send the same alert every processing loop.



Alerts must be deduplicated.



Meaningful state changes may trigger new alerts, for example:



VERIFYING → READY

READY → INVALIDATED

Focus changed

Worker became FAILED

Worker recovered



Avoid Telegram spam.



A stale worker must not be reported as healthy merely because its process exists.



\---



\## DATA DURABILITY



Persistent state should survive normal restart safely.



Prefer atomic writes when rewriting important files.



A corrupt database/file must not silently replace a known-good backup.



Do not rotate active logs in ways that conflict with running workers.



Restart must not create duplicate:



\- confirmations

\- blockchain processing

\- Focus transitions

\- Telegram alerts



\---



\## AUDITABILITY



Important decisions should be reconstructable.



Where appropriate record:



\- timestamp

\- condition\_id

\- token\_id

\- outcome

\- previous state

\- new state

\- reason

\- evidence id/timestamp

\- relevant Flow metrics

\- Risk result

\- Focus result

\- configuration/code fingerprint when available



Keep logs compact.



Never log secrets.



\---



\## SECRETS



Do not expose, print, hard-code, commit, or unnecessarily modify:



\- `.env`

\- API keys

\- QuickNode credentials

\- Telegram tokens

\- authentication tokens

\- wallet/private keys



Use existing environment/config mechanisms.



Never print secret environment variables to terminal output.



\---



\## TEST POLICY



Existing tests are part of the specification.



Do not change a test merely because the implementation fails it.



Prefer:



BUG

→ regression test

→ fix

→ test passes



Critical areas requiring deterministic tests include:



\- market identity

\- evidence deduplication

\- Flow transitions

\- Diamond decisions

\- Risk decisions

\- Focus Lock

\- READY confirmations

\- challenger behavior

\- scheduler gating

\- restart persistence

\- crash-loop handling

\- Telegram alert deduplication

\- worker health

\- historical no-lookahead behavior



Never run real-money execution tests.



\---



\## WINDOWS ENVIRONMENT



Primary environment:



Windows

Python 3.14.x

`.venv`



Project root:



`C:\\PolymarketResearch\\V3\_test\\PolymarketResearch\_machine`



When giving commands to the user:



Prefer copy/paste-ready Windows CMD commands.



Do not assume PowerShell.



Do not use obsolete D:\\ project paths.



Eventually packaging into an `.exe` may be considered only after the multi-worker system is stable.



\---



\## TOKEN / API COST EFFICIENCY



API usage may be paid per token.



Do not repeatedly reread the entire repository.



Prefer:



search

→ relevant file

→ relevant function

→ relevant tests



Do not:



\- dump entire large source files unnecessarily

\- repeat analysis already established

\- perform unrelated repository-wide audits

\- make cosmetic edits unrelated to the task



Use concise diffs and summaries.



Accuracy remains more important than token savings.



\---



\## REPORT FORMAT



After engineering work report:



CHANGED:

files modified



WHY:

short reason



TESTED:

tests/commands executed



RESULT:

PASS / WARNING / FAIL



RUNTIME:

workers verified when relevant



TELEGRAM:

status when relevant



UNRESOLVED:

real remaining problems only



NEXT:

one most logical next step



\---



\## FINAL DEFINITION OF DONE



A coding task is complete only when:



\- intended behavior exists

\- relevant tests pass

\- regressions were checked

\- unrelated behavior was preserved

\- the change is understandable

\- state remains safe when relevant

\- no real-money capability was enabled



The PROJECT is not finished until:



\- Collector works

\- Flow Tracker works

\- Diamond Filter works

\- Risk Engine works

\- Focus Controller works

\- Focus Monitor works

\- Telegram works

\- Dashboard works

\- one launcher starts the required runtime

\- duplicate workers are prevented

\- stale/failed workers can be detected

\- Telegram reports system health correctly

\- Telegram alerts are deduplicated

\- restart behavior is safe

\- decisions are auditable

\- full safe integration testing passes



Use PASS / WARNING / FAIL honestly.



Never report PASS when critical validation is incomplete.

