# GENERAL1 SIGNAL FUNNEL REPORT

**Status:** NOT OBSERVED DURING THIS AUDIT / INCONCLUSIVE. No local data supplied.

| Stage | Measured count | What will establish it |
| --- | --- | --- |
| Eligible Flow | UNKNOWN | `flow_state.json` snapshot / independent flow observations |
| Diamond analyzed / CANDIDATE / DIAMOND | UNKNOWN | `diamond_analysis_v3.json` class histogram |
| Risk PASS | UNKNOWN | `risk_assessment.json` current generation + Learning risk_decisions |
| Focus LOCKED | UNKNOWN | `focus_events.jsonl` event histogram |
| Focus WAIT | UNKNOWN | `focus_events.jsonl` event histogram |
| Focus READY | NOT OBSERVED IN THIS SESSION | `focus_events.jsonl` READY with canonical lineage |
| Book PASS | UNKNOWN | `book_assessment.json` + linked `book_decisions` |
| Paper OPEN | NOT PERMITTED IN THIS TEST | The Paper worker is deliberately not started |

### Implementation and evidence caveats

- `scripts/focus_engine.py`: 3 accepted new-evidence confirmations with >=60 seconds evidence-time separation. This provides a theoretical minimum of ~120s from first to third counted confirmation, not a guarantee of READY.
- `scripts/risk_engine.py`: authenticated Signal/Diamond/Flow checks can veto for freshness, identity, direction, verification; a risk block by itself is not collector failure.
- `scripts/book_worker.py`: `READY_BOOK_PASS` or Book OK is preliminary and does not guarantee the stronger Paper freshness/settlement/request checks.
- Latest snapshots may reflect a single tick, not cumulative counts; use `learning.sqlite3` and `focus_events.jsonl` for historical stage/event counts. Duplicate WAIT transitions and repeated signals from the same market must not be treated as independent events.
- Naturally absent READY in 300 seconds should be described as NOT OBSERVED **IN WINDOW**, not as a predictive system failure.
- No artificial candidate, READY or fabricated Paper trade is injected.

### Next evaluation

From sanitized local outputs, distinguish "no eligible Flow at all" vs "Flow arrived, but Diamond/Risk blocked" vs "Risk passed, Focus timed out" vs "READY existed, but CLOB quote blocked." The observed bottleneck dictates any next code change, not intuition.
