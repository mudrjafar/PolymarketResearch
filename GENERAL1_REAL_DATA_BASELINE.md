# GENERAL1 — Real Data Baseline (30 October 2026)

## Source and isolation

These are **actual public Polygon read observations** from isolated GitHub Actions runner worktrees, not fixtures and NOT the user's existing local databases. Raw transactions, wallet IDs, and tokens are never exported in the bundled sanitized artifacts; the raw ephemeral DBs are not persisted. Ubuntu/Python 3.11 GitHub runner; public `polygon.drpc.org` endpoint; no Paper/Telegram worker, no orders or private keys.

The two completed experiments had the **Collector's 500-block default lookback** due to a test-launcher environment forwarding mistake. This has since been repaired and regression-tested. Never combine stage counts across separate GitHub Actions experiments.

| Attribute | Run 1 (aborted) | Run 2 (full 300s) |
| --- | ---: | ---: |
| GitHub Actions ID | 41066783110 | 41067595593 |
| Runtime | 75.50s | 300.04s |
| Persisted canonical blocks | 15 | 220 |
| Persisted normalized trades | 281 | 6,532 |
| Distinct condition IDs | 82 | 772 |
| Protocol classification | CTF_TOKEN only | CTF_TOKEN only |
| Collector rate / chain rate (blocks/s) | 0.2488 / 0.6799 | 0.7721 / 0.6654 |
| Head minus checkpoint, first → last | 529→555 | 529→500 |
| Unexpected collector exits | 1 | 0 |
| Most recent Flow tracked tokens | 98 | 554 |
| Flow VERIFIED | 0 | 0 |
| Diamond CANDIDATE / DIAMOND | 0 / 0 | 0 / 0 |
| Diamond DATA_RISK latest | 98 | 539 |
| Risk PASS latest | 0 | 0 |
| Focus LOCKED/READY | 0 / 0 | 0 / 0 |
| Book PASS | 0 | 0 |
| Paper entries | 0 by design | 0 by design |
| Statistical alpha | NOT MEASURABLE | NOT MEASURABLE |

**Important:** Counts in the Learning DB represent repeated generation observations. Run 2 had 7,893 archived signal rows and 7,893 Risk BLOCK decisions; these must NOT be read as unique market events, independent samples, or failed trading predictions. The Risk reason histogram includes multiple overlapping blockers per candidate. At the final snapshot the 539 blocked candidates included 539 `STALE_TRADE` and 539 `STALE_FLOW_TRADE` reasons, consistent with the ~500-block backlog.

The two runs do NOT verify per-trade identity or historical completeness merely by reporting counts. The Collector storage implementation uses canonical chain+transaction+log-index identity and fail-closed checkpoint logic, verified in offline tests. True on-chain completeness requires replay/reconciliation against source RPC history.

## Missing user-local data

User's existing `data/collector.sqlite3`, `data/learning.sqlite3`, `focus_events.jsonl`, `paper_state.json`, and raw Paper/Book snapshots have not been accessed. No local P&L or historical natural READY can be claimed.

## Read-only local audit

From the correctly checked-out audit branch inside the user's own repository:

```bash
python3 scripts/quant_validation1.py --data-dir data
python3 scripts/live_evidence_gate1.py --data-dir data
```

Review the first output for potentially sensitive existing P&L or filesystem path before sharing; the second returns only aggregate counts. Both open SQLite with URI mode=ro, never create databases, never start workers.

## Open current gate

A separately bounded 30-block near-tip experiment is tracked in [GitHub Actions 41068606756](https://github.com/mudrjafar/PolymarketResearch/actions/runs/41068606756). Its result must be inspected before asserting current near-tip data freshness or natural Focus readiness.
