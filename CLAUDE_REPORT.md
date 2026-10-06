# CLAUDE_REPORT — Baseline + Semantic Audit

Date: 2026-10-04 · Auditor: Claude Code (Opus 5.5) · Rules: `CLAUDE.md` · Task: `CURRENT_TASK.md`

Evidence tags: **[OBSERVED]** verified from code/tests/data · **[INFERRED]** strong conclusion, not fully proven · **[UNKNOWN]** insufficient evidence.
Line numbers refer to files as they were at audit time.

---

## TASK

Baseline + Semantic Audit

## MODE

READ-ONLY AUDIT

## WORKLOG (what Claude actually did, in order)

1. Before reading the rule files, copied `CLAUDE.md` and `CURRENT_TASK.md` (at the user's request) to a **read-only backup outside the project**:
   `C:\PolymarketResearch\V3_test\_original_rules_backup\20261004_185519\` (sha256 of the copies matched the originals).
2. Read all active source, launchers, tests, docs. Listed files (excluding `.venv`, `archive`).
3. Inspected data files read-only. The SQLite DB was queried from a **temporary copy** (the original DB was not opened).
4. Listed running Python processes. **None were running.**
5. Recorded a sha256 manifest of all project files (excluding `.venv` and `__pycache__`) in a system temp directory outside the project.
6. Checked Git. The project is **not** a Git repository.
7. Ran safe offline tests (results below) and the launcher argument check.
8. A planned offline reproduction of the JSONL-reader defect (temp directory only) was **not executed**. The user declined the tool call twice, so that finding is tagged [INFERRED from code].
9. Wrote this file.

## FILES INSPECTED

`scripts/live_active_trades.py`, `collector_storage_v4/storage.py`, `collector_storage_v4/bridge.py`, `scripts/flow_tracker.py`,
`scripts/diamond_filter_v3.py`, `scripts/diamond_filter_v3_BEFORE_V3_3.py` (grep), `scripts/flow_tracker_BEFORE_V3_3.py` (grep),
`telegram_bot.py`, `dashboard.py`, `system_status.py`, `machine_common.py`, `run_machine.py`, `start_machine.bat`,
`start_machine_core_v3.bat`, `start_machine_multi_v2.bat`, `scripts/prepare_v3_data.py`, `tests/test_machine_v3.py`,
`tests/test_regressions.py`, `tests/test_collector_sqlite.py`, `collector_storage_v4/test_storage.py` (structure),
`README.txt`, `CHANGELOG_V3.txt`, `PROJECT_AUDIT.txt`, `requirements.txt`, `.env.example`, `.env` (key names only, values never displayed),
data: `flow_state.json`, `verification_state.json`, `signals.json`, `diamond_analysis_v3.json`, `live_trades_before_sqlite.jsonl`,
`collector.sqlite3` (temp copy).

## FILES CHANGED

- `CLAUDE_REPORT.md` (this file) is the only project file created. See SAFETY CONFIRMATION for the integrity check.
- Outside the project: the rules backup folder from step 1 and a temporary hash manifest. No project source, test, config, or state was modified.

---

## ARCHITECTURE

Actual pipeline [OBSERVED]:

```
Collector  scripts/live_active_trades.py
  → data/collector.sqlite3 (source of truth) + data/live_trades.jsonl (atomic 20-min view, collector_storage_v4/bridge.py)
Flow Tracker  scripts/flow_tracker.py
  → data/flow_state.json, data/verification_state.json
Diamond Filter  scripts/diamond_filter_v3.py   (also merges stale data/signals.json!)
  → data/diamond_analysis_v3.json, diamond_candidates.json, diamonds.json
Telegram  telegram_bot.py (alerts + manual PAPER trades → data/paper_trades.json)
Dashboard  dashboard.py (Streamlit)
Console status  system_status.py
```

Missing compared with the pipeline in CLAUDE.md [OBSERVED: grep found no implementation]:
- **Risk Engine**: does not exist.
- **Focus Controller**: does not exist.
- **Focus Monitor**: does not exist.
- **READY, INVALIDATED, challenger**: none of these concepts exist.
- `system_status.py:15` reads `data/focused_market.json`, but nothing writes that file.

Obsolete, duplicate, or unused files [OBSERVED]:
- `scripts/*_BEFORE_PERFORMANCE.py` is byte-identical to `*_BEFORE_V3_3.py` (both the diamond and flow pairs).
- `run_machine_BEFORE_NO_TELEGRAM.py` is identical to `run_machine.py`.
- Also unused: `archive/legacy/*`, `archive/sqlite_integration_20261002_155556/*`, and the 2-byte stubs in `scripts/data/*.json`.
- Stray 0-byte files in the root: `Never`, `Use`, `python`. [INFERRED] These were accidentally created by shell redirection when text was pasted into CMD.
- `.env` contains an `OPENAI_API_KEY` that no code uses.

### CRITICAL: the active code has regressed relative to its own tests and backups [OBSERVED]
The active `flow_tracker.py` ("V3.2 ADAPTIVE HORIZON", 2026-10-02 23:00) and `diamond_filter_v3.py` (23:11) are older or different from what the test suite expects. Compared with `tests/test_regressions.py` and the `*_BEFORE_V3_3.py` copies (14:49), the active code has lost:
- token-level grouping (`flow_tracker_BEFORE_V3_3.py:970` groups by `token_id`)
- block-time windows (`BEFORE:150,279` use `block_timestamp`)
- `schema_version`, `source_updated_at`, `last_trade_at`, `direction`
- the freshness gates (`diamond_filter_v3_BEFORE_V3_3.py:267`)
- `analyze()`

As a result, 11 of 13 regression tests now error (see TEST RESULTS). Telegram and the Dashboard still depend on the missing fields, so they are silently blind (see TELEGRAM and DASHBOARD).

## ENTRY POINTS

| Launcher | Behavior | Status |
|---|---|---|
| `start_machine.bat` → `run_machine.py` | Creates the venv, runs **pip install on every start**, loads `.env`, runs `prepare_v3_data.py`, then starts collector, flow, diamond (`--watch --interval 10`), telegram, and Streamlit dashboard as child processes | **Broken** [OBSERVED]: `diamond_filter_v3.py` rejects `--watch` (exit 2), so `run_machine.py:56` raises and terminates **all** workers |
| `start_machine_core_v3.bat` | Opens 4 CMD windows (collector, flow, diamond `--interval 60`, system_status) using bare `python` | Does not load `.env`, so the collector exits with "Missing POLYMARKET_RPC_URL" unless the variable is set globally [INFERRED]. No supervision |
| `start_machine_multi_v2.bat` | Same as above plus `python -u dashboard.py` | Running a Streamlit script with plain `python` does not start a web server [INFERRED] |

`start_machine.bat:13`: the `%%A:~0,1` substring syntax does not work on FOR variables, so comment lines in `.env` are not skipped. Severity: LOW.

## FLOW WINDOWS

[OBSERVED] `flow_tracker.py:100-104, 366-381`: the windows are 1m/5m/15m `timedelta`s.
- Membership rule: `parse_time(trade["detected_at"]) >= now - window`, where `now = datetime.now(UTC)`.
- The windows are truly rolling. They are not clock-aligned and do not reset at boundaries.
- There is no upper bound, so future-dated rows would count.

Effects of using `detected_at`:
- **Window membership uses ingestion time, not block time.** Delayed ingestion moves trades into later windows.
- Real data: `detected_at − block_timestamp` was **814–853 s (median 842 s)** in `collector.sqlite3`. During the catch-up run, **1,211 trades shared one detected minute**. Backlog or catch-up therefore compresses history into the 1m/5m windows and fabricates bursts.
- Even in steady state, the 20-block confirmation lag (~40 s+) means a 1m window mostly reflects trades older than 1 minute [INFERRED].
- A single legitimate trade appears in 1m, 5m and 15m at once, which is correct.

Duplicates inside one window:
- The flow layer does no tx/log deduplication. It relies on the input file never repeating rows.
- In normal operation the reader does not create duplicates [INFERRED from code], but it **loses** rows (see EVENT IDENTITY).

Repeated recalculation: recomputing identical windows on a timer cannot add confirmations, because confirmations require a changed evidence ID (see NEW EVIDENCE).

## EVENT TIME

- Collector [OBSERVED]: stores the real `block_timestamp` from `eth_getBlockByNumber` (`live_active_trades.py:64-70, 361`). It also stores `detected_at` as local wall-clock time at normalization (`:327`).
- SQLite `recent_trades` and the JSON view order rows by block number and log index, using block time (`storage.py:221-229`, `bridge.py:44-45`).
- Flow Tracker, `system_status.py`, and the Diamond evidence ID all use **`detected_at`**.
- Telegram paper marks use `block_timestamp or detected_at` (`telegram_bot.py:131`).
- The timestamps are inconsistent across modules. That violates "Historical evaluation must obey event time" and makes live windows unreliable.

## EVENT IDENTITY / DEDUPLICATION

Collector / SQLite: strong [OBSERVED, tested]
- Primary key is `(chain_id, transaction_hash, log_index)` (`storage.py:74-82`).
- Each batch commits atomically: block headers, trades and checkpoint together (`:130-219`).
- Replaying a block is idempotent. A duplicate ID with different contents raises `StorageError`.
- Gaps and parent-hash mismatches fail closed.
- On restart the collector resumes from the durable checkpoint (`live_active_trades.py:365-381`).
- Only one collector can run: there is an OS file lock (`bridge.py:60-89`).

Reorgs [OBSERVED]:
- Mitigated only by the 20-block confirmation lag, header and hash checks, and a re-check of the final header (`live_active_trades.py:401, 413`).
- A conflict ends the collector (`SystemExit(1)`), and recovery is manual. **There is no automatic reorg repair.**

Flow layer: weak
- `load_trades()` (`flow_tracker.py:201-261`) is a byte-offset incremental reader, so it assumes the file is append-only.
- The collector instead **atomically replaces** the whole file with a fresh 20-minute snapshot on every batch (`bridge.py:46-56`).
- When old rows drop off the front of the snapshot but the file still grows, the reader seeks into shifted content. It then **silently skips about as many bytes of new trades as were removed from the front, plus one partial line** [INFERRED from code; offline reproduction was not executed].
- When the file shrinks, the reader resets fully and reloads (fine).
- **Net effect: lost evidence, undercounted volumes and trade counts, false negatives.**

Restart:
- The collector does not re-ingest.
- Flow reloads `verification_state.json`. Its stored `last_confirmation_evidence` prevents re-counting the latest trade.

Re-reading JSONL or SQLite:
- Does not create new evidence IDs, because `detected_at` is persisted in the payload [OBSERVED].

## BUY / SELL SEMANTICS

[OBSERVED] `live_active_trades.py:237-284, 403-406`:
- Decodes V2 `OrderFilled(bytes32 orderHash, address maker, address taker, uint8 side, uint256 tokenId, uint256 makerAmount, uint256 takerAmount, uint256 fee, bytes32, bytes32)`.
- Keeps **only events where `taker == exchange contract`**, i.e. the taker (aggressor) order's own fill. Maker-side fills are dropped, which avoids double counting.
- `side` 0 means BUY: `usd = makerAmount`, `shares = takerAmount`.
- `side` 1 means SELL: `shares = makerAmount`, `usd = takerAmount`.
- Price is `usd / shares`.
- Direction is relative to the filled `token_id` (outcome).
- Unknown side or a price outside (0,1) → the whole batch raises and retries forever, which blocks the cursor.

Concerns:
- Real data shows **BUY 3,038 / SELL 272 (92% BUY)** [OBSERVED]. This skew is suspicious and could mean the side or role interpretation is wrong, or that taker sells are under-captured [UNKNOWN].
- Fills where the taker is not the exchange (for example direct operator `fillOrder`) would be dropped [UNKNOWN].
- The event signature and topic hash could not be verified offline [UNKNOWN].
- Could one trade count once as BUY and once as SELL? Not within the collector [OBSERVED]. However, Flow **sums YES-BUY and NO-BUY as the same "BUY"** (see MARKET IDENTITY).

**Confidence: MEDIUM-LOW** (collector decoding MEDIUM; direction as used by Flow LOW).

## FLOW FORMULAS

Active formulas, from `flow_tracker.py:366-478` [OBSERVED]. Trades whose side is not BUY or SELL are skipped.

| Metric | Formula |
|---|---|
| trade_count | number of valid BUY/SELL trades in the window |
| buy_volume / sell_volume | Σ `trade_usd`. Fallbacks: `usd_size`, `size`, `amount`, `token_amount`. The last is a share count, not USD (`:289-320`) |
| total_volume | buy + sell |
| net_flow | buy − sell |
| directional_strength | \|net\| / total (0 if no volume) |
| direction | sign(net): BUY / SELL / NEUTRAL |
| largest_trade | max trade USD in the window |
| average_trade | total / count |
| single_trade_ratio | called `largest_trade_ratio` = largest / total |
| freshness | **not computed** |
| interest_score (`:485-557`) | 30·str5 + 20·str15 + direction agreement (25 if all 3 agree / 15 if 2) + count tiers (5/10/15) + 5 if largest ≥ $1000 + 5 if acceleration (1m > 2× the 5m average). Capped at 100. Not used by Diamond |
| data_confidence (`:564-630`) | count tiers (10/20/30) + 5m volume tiers (8/15/25) + directional windows (10/20/30) + concentration (15 if ≤0.40, 8 if ≤0.70). Range 0–100 |

Duplicate or divergent definitions [OBSERVED]:
- Diamond recomputes direction as `sign(net)` and normalizes confidence by dividing by 100.
- When `largest_trade_ratio` is missing, Diamond falls back to `trade_to_volume_ratio` (`diamond_filter_v3.py:408-419`). That is a **different metric**: trade USD divided by 24h market volume.
- Thresholds are duplicated between `flow_tracker.py:82-93` and `diamond_filter_v3.py:35-41` (8 trades, $500, 0.35, 0.25, 0.70, 3).
- `archive/legacy/scripts__signal_engine.py` contains a third implementation.

## MARKET IDENTITY

- **CRITICAL [OBSERVED]**: Flow groups trades by `condition_id` only (`flow_tracker.py:1194-1200`). YES and NO trades of one market share one flow, and a buy of NO counts as "BUY" in the same bucket as a buy of YES.
- State (`market_states`, `flow_states`) is keyed by `condition_id`. The `outcome`, `token_id` and `price` stored are taken from **whichever trade came last** (`:1207-1217, 1364-1381`).
- Real data: **63 of 388** conditions in SQLite had trades on more than one token.
- Diamond `market_key = condition:{cid}|outcome:{outcome of latest trade}` (`diamond_filter_v3.py:237-247`), so mixed flow gets labeled as one outcome. The key changes when the last-traded outcome flips.
- `build_dataset()` deep-merges `data/signals.json` **over** the flow rows (`:297-322`). Signals are processed second, so their values win. `signals.json` (388 rows) was regenerated **2026-10-04 16:46 UTC** by no active script [OBSERVED; writer UNKNOWN, probably legacy `signal_engine`]. Stale `state`, `confirmations`, `verified`, `data_confidence` and `resolution_state` can therefore override fresh flow values.
- `same_market()` falls back to **question-title equality** when IDs are missing (`:340-349`). `market_key` falls back to the question too (`:247`).
- `enrich_market` deep-merges the latest live trade row into the item, overwriting top-level fields with trade-level ones (`:364-371`).
- Telegram paper deduplication uses `market_key`. Paper mark-to-market matches by `token_id` (good).
- SQLite stores `condition_id`, `token_id` and `outcome` correctly (`storage.py:74-79`).

## NEW EVIDENCE

[OBSERVED] `flow_tracker.py:810-815, 884-920`:
- The evidence ID is `tx_hash:log_index:detected_at` of the **latest trade in the condition**.
- When `verification_check` passes and the ID differs from `last_confirmation_evidence`, confirmations increase by 1, capped at 3. At 3 the state becomes VERIFIED.
- A single failing check resets confirmations to 0.

Consequences:
- Any new trade counts as new confirming evidence: either outcome, any size (even dust), or opposite direction. The confirmation measures "a trade happened", not "the evidence confirms the direction" [OBSERVED].
- Timer firing, scheduler runs, restarts, re-reading state, and recalculating identical windows do **not** add confirmations, because the ID is unchanged and persisted [OBSERVED; test_machine_v3 passes].
- After a fail-reset, `last_confirmation_evidence` is `None`. If the window passes again without a new trade, the same old trade can count once more. Severity: LOW [INFERRED].
- Diamond has no evidence concept. It re-classifies whatever is in `flow_state.json` every 60 s, including stale entries, so a stale VERIFIED can keep producing DIAMOND [INFERRED].
- READY and Focus do not exist.
- The Telegram Diamond alert deduplicates by `market_key` (see TELEGRAM).

## SCHEDULER / RESCAN

[OBSERVED] `flow_tracker.py:23-55, 72-75, 764-851, 1182-1452`:
- There is a global tick every 5 s. Each condition has its own RAM schedule (`next_due`, `last_seen_evidence`), which is **not persisted**, so a restart makes every market due at once.
- Intervals:
  - VERIFIED: 5 s
  - VERIFYING: 10 s
  - CANDIDATE: 15 s, but **this state is never assigned** (a candidate is labeled VERIFYING, `:914-915`)
  - LOW: by time to end — URGENT 15 s, SOON 30 s, NEAR 60 s, MID 5 min, FAR 15 min, ULTRA_FAR 30 min, UNKNOWN 5 min
- Dead code: `LOW_INTERVAL`, `get_interval`.
- No FOCUSED state exists.
- A new trade wakes VERIFYING/VERIFIED markets and near markets. MID/FAR/ULTRA_FAR markets wake only above size or count thresholds (`:39-55`).
- Unchanged markets that are not yet due are skipped cheaply.
- Only markets that have trades in the in-memory cache are iterated. Markets with no recent trades are never re-evaluated, so expiry and staleness are never checked for them.
- `flow_state.json`, `verification_state.json` and `analysis_schedule` are **never pruned** (unbounded growth). If the tracker stops, stale VERIFIED entries remain on disk.
- The scheduler tracks last analysis evidence in RAM only. It does not record which evidence was consumed beyond the single latest ID.

## DIAMOND

[OBSERVED] `diamond_filter_v3.py`:
- **Technical gates** (`:1017-1090`): outcome present; price in (0,1); 5m trades ≥ 8; 5m volume ≥ $500; 5m strength ≥ 0.35; 15m strength ≥ 0.25; data confidence ≥ 0.55; largest-trade ratio < 0.70; 5m and 15m directions non-zero and equal.
- **Scores**:
  - Signal (persistence 25, strength 25, breadth 20, volume 15, concentration 15) ≥ 80
  - Verification (confirmations 50, tracker state 20, persistence 20, confidence 10) ≥ 75. In practice this needs 3 confirmations or the VERIFIED state.
  - Entry ≥ 40
  - Resolution ≥ 70
- **Output states** (`:1097-1122`): DIAMOND, VERIFYING, ENTRY_BLOCKED, DATA_RISK, CANDIDATE, WATCH, LOW. **INVALIDATED does not exist.** If a gate fails while the scores are high, the result falls through to CANDIDATE.
- 1m/5m/15m interaction: 5m and 15m must agree (gate). A 1m disagreement only lowers the score.
- Whale protection: largest ratio < 0.70 plus at least 8 trades. A single dominant trade (≥ 0.70) is blocked, but 69% concentration still passes.
- Cashflow alert tiers: LARGE $5k, VERY_LARGE $10k, EXTREME $25k, or a relative surge (5m ≥ $1.5k and 5m/24h ≥ 10%). These are attention signals only and cannot create a Diamond.

Defects and false-positive paths:
- **No freshness gate at all.** `updated_at` is ignored, so stale flow can be classified as DIAMOND [OBSERVED].
- `accepting_orders` is read from the non-existent key `acceptingOrders` (`:490`), so the acceptingOrders=False block **never fires** [OBSERVED].
- `closed` is read only from top-level fields that come from an enriched live row. Without a live match it is `None` [OBSERVED].
- `resolution_conflict` flags stored `FAR` when ≤ 72h remain (`:858`). Flow labels everything over 24h as FAR, so **every market 24–72h from its end date gets a false conflict**: resolution score 25, giving DATA_RISK or a blocked Diamond (a false negative) [OBSERVED]. This contradicts `PROJECT_AUDIT.txt` item 6.
- Near-$0 longshots get an entry score of 90 (`:772-776`). There is no low-price protection.
- Inputs are YES/NO-mixed flow (see MARKET IDENTITY), which is a major false-positive path.
- Inputs can be overridden by stale `signals.json`.

Current data: 388 analyses, **all LOW, 0 Diamonds** (the data is from 2026-10-02 23:08; nothing is running).

## RISK

**There is no Risk Engine** [OBSERVED]. The only blockers are the Diamond gates and scores, plus the Telegram paper-entry checks (`telegram_bot.py:105-122`). With no independent Risk layer, nothing can reject a strong signal on risk grounds. That violates the CLAUDE.md RISK ENGINE section. No execution path exists to bypass Risk, because there is no live execution.

## FOCUS

**Not implemented** [OBSERVED]. There is no Focus Lock, no challenger logic, no READY state, and no failure counting. `system_status.py` displays `focused_market.json`, which does not exist. None of the scenario questions can be answered because the feature is absent.

## PRICE / ENTRY

[OBSERVED]
- The collector stores both `price` (Gamma `outcomePrices` snapshot from the token map, refreshed every 5 min or on lookup) and `fill_price` (actual on-chain fill).
- Diamond `price_from_row` checks `market_price`, then `current_price`, then **`price`**, then `fill_price` (`diamond_filter_v3.py:352-361`). So the entry price is the **cached Gamma snapshot**, not the latest fill.
- The flow `market.price` is the latest fill of *whichever token* traded last.
- Price is matched to the correct outcome via `same_market` (condition + outcome), but it is paired with mixed-outcome flow.
- No bid/ask or order-book data is used anywhere. The displayed price is not an executable price.
- Paper entries use the analysis price (Gamma snapshot), while marks and exits use `fill_price`. This inconsistency biases paper P/L.
- Entry tiers come from the gross upside to $1: under 3% → 5; under 5% → 15; under 10% → 35; under 20% → 60; under 50% → 80; otherwise 90. Recent price movement subtracts up to 20.
- ENTRY_BLOCKED can override strong flow (entry < 40 blocks DIAMOND) [OBSERVED].
- Paper is forward-only, so no later price is used at entry.

## RESOLUTION

[OBSERVED]
- The only source is Gamma `endDate` (plus `active`, `closed`, `acceptingOrders`), captured at trade time.
- Flow labels (`flow_tracker.py:111-140`): EXPIRED ≤ 0, CRITICAL ≤ 5 min, IMMINENT ≤ 30 min, CLOSING_SOON ≤ 6 h, ACTIVE ≤ 24 h, FAR otherwise.
- Diamond uses a **different** scheme (`diamond_filter_v3.py:831-845`): CRITICAL ≤ 6 h, CLOSING_SOON ≤ 72 h, ACTIVE ≤ 30 d.
- There are no RESOLUTION_PENDING or RESOLVED states and no resolution-outcome source.
- An end date is never treated as resolution, and a winner is never inferred (good).
- `remaining_seconds` is frozen at flow-analysis time and is not recomputed by Diamond.

## PAPER / REPLAY SAFETY

- [OBSERVED] **No historical replay or backtest code exists.**
- The only paper mechanism is manual, forward-looking paper entries via Telegram buttons (`telegram_bot.py:105-148`). No look-ahead is possible there.
- Caveats: the TP/SL/time exits are uncalibrated, entry and mark prices come from different sources, and no evaluation methodology or ledger of signal outcomes exists.
- Any future replay would also inherit the `detected_at` timing problem.

## PERSISTENCE / RESTART

| What | Survives restart? | Notes |
|---|---|---|
| Collector cursor and trades | Yes (SQLite WAL, synchronous=FULL) | No duplicates; JSON view is repaired on startup [OBSERVED, tested] |
| Flow verification state | Yes (`verification_state.json`) | Write is non-fsync `.tmp` + `os.replace` with no retry (`flow_tracker.py:867-871`); a PermissionError on Windows only logs a warning |
| Flow schedule | **No** | All markets become due immediately |
| Flow cache | No; rebuilt from the 20-min view | |
| Diamond outputs | Rewritten every cycle | Stateless |
| Telegram `active_diamonds`, cashflow levels | Yes | Baseline on start prevents duplicate alerts [OBSERVED] |
| Paper trades | Yes | Atomic writes via `machine_common.save_json_atomic` |

Restart risks:
- Stale VERIFIED entries in `flow_state.json` are consumed as current by Diamond [INFERRED].
- Duplicate confirmations or alerts were not found [INFERRED].
- No backup-rotation logic exists, beyond the one-time `live_trades_before_sqlite.jsonl` copy and the `prepare_v3_data.py` legacy archiving.

## STORAGE

| Store | Responsibility | Risks |
|---|---|---|
| `data/collector.sqlite3` | Durable trades, blocks, checkpoint (7 MB + 4.7 MB WAL; 3,310 trades, 140 blocks) | Sound |
| `data/live_trades.jsonl` | 20-min compatibility view, fully rewritten (+fsync) twice per batch | Breaks the incremental reader (data loss); full rewrites |
| `flow_state.json` (1 MB), `verification_state.json` | Flow and confirmation state | Rewritten (indent=2) every tick that analyzes anything; never pruned |
| `signals.json` (1 MB) | **Legacy, stale**, still merged by Diamond | Overrides flow state |
| `diamond_analysis_v3.json` (1.5 MB) + candidates + diamonds | Diamond outputs | Full rewrite every 60 s |
| `paper_trades.json`, `telegram_state.json` | Paper ledger and alert dedup | `telegram_state.json` rewritten every 5 s |
| `live_trades_before_sqlite.jsonl` | One-time pre-SQLite backup | — |

## PERFORMANCE

Ranked by likely impact [INFERRED unless noted]:
1. Collector publishes the full 20-min snapshot with fsync **twice per batch** (`live_active_trades.py:386, 416`). This is wasted I/O and also causes the flow reader to lose data.
2. Collector throughput is 5 blocks per batch with about 9 sequential RPC calls plus a 2 s sleep. The 5-minute Gamma market refresh (up to 100 pages) runs synchronously in the scan loop. Lag risk: the observed ingestion lag was about 14 min during catch-up [OBSERVED].
3. Diamond `enrich_market` is O(markets × live rows) with string normalization, plus full JSON reads and writes (1–1.5 MB) every 60 s.
4. `flow_state.json` gets a full 1 MB rewrite per tick, and the state is unbounded.
5. The Telegram watcher re-reads the 1.5 MB analysis and the trade tail every 5 s, and rewrites its state file each time.
6. `system_status.py` reads the full trade file and flow state every 5 s and shells out `cls`.
7. `start_machine.bat` runs `pip install` on every launch.

No tight loops or process-spawn storms were found. `run_machine.py` never restarts workers.

## SUPERVISOR / WORKERS

[OBSERVED]
- `run_machine.py` runs workers as child processes of one supervisor in one console. The `.bat` variants use separate CMD windows.
- There is no restart logic and no crash-loop protection. **Any child exit terminates every worker** (`run_machine.py:53-65`).
- Telegram is mandatory (`validate()` requires its environment variables), so a Telegram failure kills the whole system.
- There is no degraded mode.
- Duplicate prevention exists only for the collector (OS lock). Flow, diamond, telegram and dashboard can run as duplicates, and two Telegram pollers would conflict [INFERRED].
- Workers are identified by name in `run_machine.py` and by window title in the `.bat` files.

## WORKER HEALTH

[OBSERVED]
- No heartbeats, no last-successful-cycle record, no error or crash counters, and no HEALTHY/STALE/DEGRADED/FAILED/STOPPED states.
- `run_machine.py` checks only whether each process is still alive.
- `system_status.py` shows file modification ages only.
- `machine_common.fresh()` (120 s) exists but is used only by Telegram and the Dashboard.
- Gap: the system cannot tell whether it is stale or healthy.

## TELEGRAM

[OBSERVED] `telegram_bot.py`:
- Library and startup: python-telegram-bot, `run_polling`. Startup requires the token and chat ID.
- Commands: `/start`, `/status`, `/diamonds`, `/markets`, `/positions`, plus inline Analyze and $25/$50/$100 PAPER buttons.
- Missing commands from CLAUDE.md: `/health`, `/workers`, `/focus`, `/why`, `/lastsignal`.
- Only the configured chat ID is authorized.
- Alerts: new Diamonds (deduplicated by the `market_key` set, persisted, with a baseline at startup); VERY_LARGE/EXTREME non-whale cashflow with signal ≥ 65 (escalation-only); paper-trade closes.
- **Effectively blind** [OBSERVED]: `diamonds()`, the cashflow alerts and `open_paper` all require `source_updated_at`, `last_trade_at`, `schema_version == 4` and `direction`. The active Diamond filter emits none of these, so Telegram can never alert on a Diamond or cashflow event, and every paper entry is rejected.
- `/status` always replies "🟢 System online", even when nothing is running. Stale state is reported as current.
- It can change project state: it writes `paper_trades.json` and `telegram_state.json` (paper only).
- **No live-order capability**: no CLOB, web3 or signing code exists anywhere [OBSERVED by grep].

## DASHBOARD

[OBSERVED] `dashboard.py`:
- Streamlit on port 8501, started by `run_machine.py`. It reads `diamond_analysis_v3.json` and `paper_trades.json` and writes nothing.
- It hides rows that lack fresh `source_updated_at`/`last_trade_at`. Since the active Diamond output never has those fields, **all 388 rows are hidden** as "stale".
- It shows no worker health and no Focus. It refreshes only on manual button press or page reload.

## TEST RESULTS

Environment for every run:
- Interpreter: `.venv\Scripts\python.exe -B` (no bytecode written).
- `POLYMARKET_RPC_URL=http://127.0.0.1:1`, Telegram and OpenAI variables unset.
- The tests patch network calls to raise, and their writes go to temp directories.

| Command | Run | Pass | Fail/Error | Skipped |
|---|---|---|---|---|
| `python -B tests/test_machine_v3.py` | 3 checks | 3 | 0 | 0 |
| `python -B scripts/diamond_filter_v3.py --self-test` | 6 checks | 6 | 0 | 0 |
| `python -B -m unittest -v tests/test_regressions.py` | 13 | 2 | **11 errors** | 0 |
| `python -B test_storage.py -v` (cwd `collector_storage_v4`) | 10 | 10 | 0 | 0 |
| `python -B tests/test_collector_sqlite.py -v` | 14 | 14 | 0 | 0 |
| **Total** | **46** | **35** | **11** | **0** |

Failing tests (all in `tests/test_regressions.py`):
- `AttributeError: module has no attribute 'analyze'`: test_buy_paper_and_limits, test_expired_and_closed_block_diamond, test_fresh_strong_setup, test_legacy_snapshot_blocked, test_sell_and_unauthorized_paper_blocked, test_stale_paper_entry_blocked, test_stale_price_blocks_diamond, test_stale_source_blocks_diamond_and_cashflow, test_time_exit_waits_for_fresh_price.
- `TypeError: update_market_state() takes 4 positional arguments but 5 were given`: test_confirmation_resets_on_reversal.
- `TypeError: unsupported operand type(s) for +: 'WindowsPath' and 'str'` (`flow_tracker._save_json_atomic` uses `path + ".tmp"`): test_token_separation_full_cycle_and_empty_cleanup.

Additional checks:
- `python -B scripts/diamond_filter_v3.py --watch --interval 10` → `error: unrecognized arguments: --watch`, exit code 2 [OBSERVED].
- Not run: `run_machine.py` and the `.bat` launchers (they need secrets and live network), and live collector/Telegram (they need credentials and external APIs).
- Not run: the JSONL-reader reproduction (the user declined the tool call).

Test classification:
- Collector/storage: test_storage, test_collector_sqlite.
- Flow/Diamond/Telegram: test_machine_v3, test_regressions.
- No tests exist for Focus, Risk, worker health, the scheduler, or no-lookahead behavior.

## GIT / WORKTREE

[OBSERVED] Git 2.55 is installed, but the project **is not a Git repository**. No working-tree state or history exists to protect, and versioning is currently done by `*_BEFORE_*` file copies and `archive/`.

## SEMANTIC MISMATCHES (vs CLAUDE.md)

| # | Mismatch | Severity |
|---|---|---|
| 1 | Flow aggregates YES+NO per `condition_id` | CRITICAL |
| 2 | Windows use ingestion time (`detected_at`), not event time; catch-up creates fake bursts | CRITICAL |
| 3 | Active Flow/Diamond regressed vs tests/backups (token identity, freshness, schema lost); 11/13 regressions error | CRITICAL |
| 4 | Official one-command launcher cannot stay up (`--watch`); one worker exit kills all | CRITICAL |
| 5 | Risk Engine absent; strong flow is never independently risk-checked | HIGH |
| 6 | Focus Controller/Monitor, READY, challenger, INVALIDATED absent | HIGH |
| 7 | Diamond has no freshness gate; stale `signals.json` merged over flow; stale VERIFIED can become DIAMOND | HIGH |
| 8 | Flow JSONL reader drops trades under atomic snapshot replacement | HIGH |
| 9 | Confirmation = "any new trade in condition" (any outcome, size, direction) | HIGH |
| 10 | No worker health semantics; process existence only; `/status` always online | HIGH |
| 11 | Telegram/Dashboard blind due to missing fields; required Telegram commands absent | HIGH |
| 12 | BUY/SELL 92% skew unexplained; direction confidence MEDIUM-LOW | HIGH |
| 13 | False DATA_RISK for 24–72h markets; `acceptingOrders` key bug | MEDIUM |
| 14 | Entry price = cached Gamma snapshot, not fill/executable; paper entry vs mark sources differ | MEDIUM |
| 15 | Thresholds and timing labels duplicated or divergent across modules | MEDIUM |
| 16 | Duplicate workers possible (only collector locked) | MEDIUM |
| 17 | Unbounded state files; repeated full JSON rewrites | MEDIUM |
| 18 | No automatic reorg repair (fails closed) | LOW |
| 19 | Title-based fallback matching in Diamond | LOW |
| 20 | Old evidence may re-count once after a fail-reset | LOW |
| 21 | Stray 0-byte root files; unused `OPENAI_API_KEY` in `.env`; `.env` comment parsing in bat | LOW |

## BUGS FOUND

1. `run_machine.py:13` passes `--watch`, which `diamond_filter_v3.py` does not accept, so the launcher terminates every worker [OBSERVED].
2. `flow_tracker.py:1196-1200`: grouping by condition mixes outcomes [OBSERVED].
3. `flow_tracker.py:373, 1209`: windows and latest-trade selection use `detected_at` [OBSERVED].
4. `flow_tracker.py:201-261`: the incremental reader is incompatible with atomic file replacement and loses rows [INFERRED from code].
5. `flow_tracker.py:867-871`: `_save_json_atomic` fails on `Path` inputs (test error) and has no fsync or retry [OBSERVED].
6. `diamond_filter_v3.py:490`: reads the wrong key (`acceptingOrders` instead of `accepting_orders`) [OBSERVED].
7. `diamond_filter_v3.py:858`: false FAR/72h resolution conflict [OBSERVED].
8. `diamond_filter_v3.py:297-322`: merges stale legacy `signals.json` over flow [OBSERVED].
9. `diamond_filter_v3.py:356`: prefers Gamma snapshot `price` over `fill_price` [OBSERVED].
10. `diamond_filter_v3.py` outputs lack the fields Telegram and the Dashboard require, so both are blind [OBSERVED].
11. `flow_tracker.py:914-915`: the CANDIDATE state is never produced, and `CANDIDATE_INTERVAL`/`LOW_INTERVAL`/`get_interval` are dead code [OBSERVED].
12. `start_machine_multi_v2.bat`: `python dashboard.py` does not serve Streamlit; the `.bat` variants don't load `.env` [INFERRED].
13. `telegram_bot.py:155`: `/status` reports "online" unconditionally [OBSERVED].

## AMBIGUITIES

- Whether the V2 `OrderFilled` ABI, the topic hash, and the "taker == exchange" interpretation are correct, and the cause of the 92% BUY skew [UNKNOWN; needs on-chain or Polymarket-docs verification].
- Which lineage is authoritative: the active V3.2 files or `*_BEFORE_V3_3.py` plus the tests [UNKNOWN; owner decision].
- Who regenerated `signals.json` on 2026-10-04 16:46 UTC [UNKNOWN].
- Live collector throughput versus Polygon block rate [UNKNOWN; not run].
- Whether direct fills (taker ≠ exchange) occur in practice [UNKNOWN].
- The exact magnitude of row loss in the flow reader [INFERRED; reproduction not executed].

## TOP 5 RISKS

1. **YES/NO flow mixing.**
   - WHY IT MATTERS: direction and strength are meaningless when buys of opposite outcomes add together.
   - EVIDENCE: `flow_tracker.py:1196-1200`; 63/388 conditions are multi-token.
   - LIKELY IMPACT: false Diamonds on the wrong outcome.
2. **Ingestion-time windows.**
   - WHY IT MATTERS: catch-up or restart compresses many minutes of trades into the 1m/5m windows.
   - EVIDENCE: lag 814–853 s; 1,211 trades in one detected minute.
   - LIKELY IMPACT: fabricated bursts and confirmations; invalid replay.
3. **Active code regressed relative to tests and backups.**
   - WHY IT MATTERS: protections that were already built (token identity, freshness, schema) are silently gone.
   - EVIDENCE: 11/13 regression errors; missing fields; the BEFORE files contain the logic.
   - LIKELY IMPACT: unsafe signals; downstream consumers blind.
4. **Launcher and supervisor fragility.**
   - WHY IT MATTERS: the official one-command start cannot run, and any single failure kills everything.
   - EVIDENCE: argparse exit 2; `run_machine.py:56-65`.
   - LIKELY IMPACT: no runtime at all, or silent full outages.
5. **Stale state treated as current; no Risk layer.**
   - WHY IT MATTERS: Diamond has no freshness gate, stale `signals.json` overrides flow, and there is no Risk Engine.
   - EVIDENCE: `diamond_filter_v3.py:297-322, 403-491`; no freshness check.
   - LIKELY IMPACT: confident signals on dead data.

## SAFETY SNAPSHOT RECOMMENDATION

Use **both Git and a ZIP**:
1. **Local Git (no remote)**: `git init` with a `.gitignore` excluding `.env`, `.venv/`, `__pycache__/`, `data/*.sqlite3*`, `*.tmp`, logs, and large runtime JSON. Commit the current tree as `LAST_KNOWN_GOOD` (it is the current baseline even though it is flawed). Do each change on a branch (`NEW_TEST_VERSION`). This gives cheap diffs and rollback without destructive commands.
2. **Timestamped ZIP outside the project**, e.g. `C:\PolymarketResearch\V3_test\_snapshots\LAST_KNOWN_GOOD_<ts>.zip`. Include source, tests, config, docs and `data/` state, with SQLite copied consistently (stop the collector first, or use the SQLite backup API). Exclude `.env`, `.venv` and caches. Store a sha256 manifest next to it.
3. Back up `.env` separately and privately. Never put it in Git or a shared archive.

## NEXT

Exactly one recommended first change (**not implemented**). This is an EXPLAIN-FIRST item, and the snapshot comes first:

> **Restore token_id-level market identity in the Flow Tracker** (group, key and persist flow by `token_id` with `condition_id` and `outcome` attached), driven by the existing failing regression test `test_token_separation_full_cycle_and_empty_cleanup`. As part of this, the owner decides which lineage (active V3.2 or `*_BEFORE_V3_3.py`) is authoritative.

## SAFETY CONFIRMATION

- No real-money capability was enabled. None exists in the code.
- No orders were submitted. No transactions were signed and no funds were moved.
- Secrets were not exposed. Only `.env` key **names** were listed; values were redacted. No RPC or Telegram calls were made.
- Strategy thresholds were not changed.
- Project source files were not changed.
- Tests were not modified.
- Only `CLAUDE_REPORT.md` was created in the project. This was verified with a before/after sha256 manifest of all project files excluding `.venv` and `__pycache__`; see the final chat summary.

## FINAL STATUS

**FAIL**

The current baseline violates CRITICAL CLAUDE.md semantics (market identity, event time), the official launcher cannot run, and 11 regression tests error. The live behavior of the collector, BUY/SELL correctness, and the reader-loss magnitude remain unverified.
