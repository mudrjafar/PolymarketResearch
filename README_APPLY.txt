POLYMARKET INTEGRATION PATCH — UNITS 2-4
=======================================

Contains only source/tests. No .env, secrets, runtime database, or live data.

Files:
  scripts\flow_tracker.py
  scripts\diamond_filter_v3.py
  scripts\risk_engine.py
  tests\test_flow_unit2.py
  tests\test_diamond_unit3.py
  tests\test_risk_engine.py

Important fixes:
- Flow verification state keyed by token_id, not condition_id.
- Opposing/legacy evidence cannot advance confirmations.
- Canonical block_timestamp propagated as last_trade_at.
- Flow uses event-time and canonical tx_hash+log_index evidence.
- Share counts are never treated directly as USD volume.
- Diamond requires condition_id + token_id + outcome and uses canonical freshness fields.
- Risk V2 independently re-checks Flow verified/confirmations/direction.
- Risk supports injected replay clock and stable reason_codes.

WINDOWS APPLY
-------------
1. Stop the current machine first.
2. Make a backup of the three current scripts.
3. Copy this patch's scripts and tests over the project paths.

TEST COMMANDS
-------------
cd /d C:\PolymarketResearch\V3_test\PolymarketResearch_machine

.venv\Scripts\python.exe -m pytest tests\test_flow_unit2.py tests\test_diamond_unit3.py tests\test_risk_engine.py -v
.venv\Scripts\python.exe scripts\risk_engine.py --self-test
.venv\Scripts\python.exe scripts\diamond_filter_v3.py --self-test
.venv\Scripts\python.exe -m pytest tests\ -v

Expected targeted tests: 27 PASS.
The final full-suite result must be verified on the user's Windows .venv before live integration.
