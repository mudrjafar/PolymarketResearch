DIAMOND INTELLIGENCE V3 - INTEGRATED MACHINE

SQLITE COLLECTOR UPDATE:
- Collector source of truth: data/collector.sqlite3 (trades + durable block checkpoint).
- Restarts resume scanning after the saved block, with actual block timestamps.
- data/live_trades.jsonl is now an atomic 20-minute compatibility view.
- On first live startup, prior JSON history is preserved as data/live_trades_before_sqlite.jsonl.
- Initial scan: last 500 confirmed blocks; confirmation lag: 20 blocks.
- Existing flow, Diamond, Telegram and dashboard behavior remains in place.
- Offline validation: .venv\Scripts\python.exe -B run_machine.py --self-test
- Full recovery/configuration notes: collector_storage_v4/README.md

ONE START:
1. Keep your real .env in this folder (never share it).
2. Double-click start_machine.bat.
3. Open http://localhost:8501 for the HTML tracker.
4. In Telegram send /start once, then /markets.

ACTIVE PIPELINE:
V2 standard + NegRisk live blockchain scan
  -> flow_tracker (1m/5m/15m)
  -> Diamond Filter V3.1
       Signal Quality
       Verification (new-evidence confirmations)
       Entry Quality
       Resolution Reliability
       Huge Cashflow alert
  -> Telegram selective alerts + interactive PAPER entries
  -> HTML dashboard

TELEGRAM:
/markets   top interesting markets + Analyze/Paper buttons
/diamonds  verified Diamonds only
/positions open paper positions
/status    machine summary

AUTOMATIC TELEGRAM:
- newly entered verified Diamonds
- VERY_LARGE / EXTREME non-whale cashflow with signal quality >=65
- paper-trade close alerts

IMPORTANT AUDIT CHANGES:
- V2 BUY/SELL amount normalization corrected.
- Flow uses taker/aggressor OrderFilled only, avoiding maker+taker cancellation/double counting in normal matches.
- Both V2 standard and V2 NegRisk exchanges are scanned.
- Market metadata refreshes every 5 minutes.
- Confirmation count increases only when new trade evidence appears; rescanning unchanged data does not create confirmations.
- V3 reads flow_state directly; old signal_engine is no longer in the active pipeline.
- Old components are preserved under archive/legacy, not deleted.
- Pre-V3 live-trade data is automatically archived rather than mixed with the corrected V3 format.

PAPER WARNING:
Paper prices use the latest observed blockchain fill, not guaranteed executable bid/ask quotes. TP/SL/time exits are test rules, not calibrated profitability rules.
