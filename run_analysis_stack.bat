@echo off
cd /d "C:\PolymarketResearch\V3_test\PolymarketResearch_machine"
start /b "" "C:\PolymarketResearch\V3_test\PolymarketResearch_machine\.venv\Scripts\python.exe" -u scripts\diamond_filter_v3.py --interval 60
:loop
"C:\PolymarketResearch\V3_test\PolymarketResearch_machine\.venv\Scripts\python.exe" -u scripts\risk_engine.py
timeout /t 15 /nobreak >nul
goto loop
