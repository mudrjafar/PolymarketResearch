# GENERAL1 F3/F4 FINAL RISK DECISION
Read-only review of existing persistence behavior; no journal/retention redesign.

## F3 — Paper state save before event journal append
**Severity: MEDIUM, known audit-trail completeness gap. Not a blocker for the FIRST isolated, manual Paper-only E2E test provided durable paper_state.json is the authority and operators explicitly reconcile it.**

Evidence:
- `scripts/paper_worker.py` saves `paper_state.json` (via save_json_atomic) BEFORE calling `_append_events(events)` (fsync append to `paper_events.jsonl`).
- A crash or kill after the successful save and before the event append causes `OPENED`, `CLOSED` or `SETTLED` to be absent from journal even though authoritative state has changed. On recovery, `processed_request_ids` prevents reapplying the same fresh request.
- Existing `tests/test_paper_worker.py::test_crash_after_paper_state_save_does_not_reopen_request` exercises this case. The OPEN is not duplicated, but the missing event does not reappear.
- Telegram, Dashboard and Learning may disagree or not receive the same event; do not calculate certified realised P&L purely from the JSONL event feed.

Controls:
- Keep Paper state as source of truth, cross-check state positions and requests against event records, record missing entries as `JOURNAL_GAP`, do not imply journal is complete, do not treat missing event as proof OPEN never happened.
- For future operational telemetry/reporting: minimal durable outbox/reconciliation is preferable to weakening idempotency, but not implemented in this narrowly scoped change.
- Escalate to HIGH/block before relying on journal as sole source for trading statistics or autonomous downstream decisions.

## F4 — Request-ID retention and unchanged historical request JSON
**Severity: MEDIUM, bounded idempotency and retention policy. Not a blocker for first isolated Paper-only manually inspected test with short (<120s) request TTL and no external file writers.**

Evidence:
- `_run_once_unlocked` materializes `processed = set(...)`; `state["processed_request_ids"] = list(processed)[-MAX_PROCESSED_REQUEST_IDS:]` with limit 1,000.
- Set order is nondeterministic; after enough distinct requests, older IDs can drop even when requests remain as files.
- `_request_is_fresh` requires a recent requested_at timestamp, normally preventing an old file from reopening after it falls from the retained set. But freshness is not durable once a request file is deliberately rewritten with the SAME ID and a new timestamp; this must not be treated as permanent never-ever replay protection.
- Existing positive idempotence tests show recent duplicate suppression and PaperWorkerLock serializes cooperative workers, not a formal infinite retention guarantee.

Controls:
- First sandbox: inspect request directory, protect against unrelated writers, use naturally unique IDs, do not rewrite old files, use dedicated disposable data root and no real orders, reconcile Paper state before/after the run.
- Before long-running unattended monitoring, add deterministic retention/archival and restart tests; consider an append-only processed-request ledger or explicit acknowledged request deletion.
- No change in this scoped PR. Do not claim unconditional idempotence under arbitrarily long histories or file manipulation.

## Decision / scope guard

F3 and F4 remain known MEDIUM debts, not confirmed paths to an unauthorized real order or OPEN through current READY gating. No new subsystem built and no trade or code merge performed. Both become material release blockers once audit-grade event completeness or unattended multi-day automatic processing is required. First controlled manual Paper-only sandbox must still be separately authorized and postvalidated; no permission implied here.
