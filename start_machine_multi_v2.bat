@echo off
cd /d C:\PolymarketResearch\V3_test\PolymarketResearch_machine

start "POLYMARKET - COLLECTOR" cmd /k python -u scripts\live_active_trades.py
timeout /t 2 /nobreak >nul

start "POLYMARKET - FLOW TRACKER" cmd /k python -u scripts\flow_tracker.py
timeout /t 2 /nobreak >nul

start "POLYMARKET - DIAMOND" cmd /k python -u scripts\diamond_filter_v3.py --interval 60
timeout /t 2 /nobreak >nul

start "POLYMARKET - SYSTEM STATUS" cmd /k python -u system_status.py
timeout /t 2 /nobreak >nul

start "POLYMARKET - DASHBOARD" cmd /k python -u dashboard.py
exit
