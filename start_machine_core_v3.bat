@echo off
setlocal
cd /d "%~dp0"

if not exist ".env" (
    echo ERROR: .env not found
    pause
    exit /b 1
)

for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
)

if not defined POLYMARKET_RPC_URL (
    echo ERROR: POLYMARKET_RPC_URL missing in .env
    pause
    exit /b 1
)

set "PY=%CD%\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo ERROR: .venv Python not found
    pause
    exit /b 1
)

start "POLYMARKET - COLLECTOR" cmd /k ""%PY%" -u scripts\live_active_trades.py"
timeout /t 2 /nobreak >nul
start "POLYMARKET - FLOW TRACKER" cmd /k ""%PY%" -u scripts\flow_tracker.py"
timeout /t 2 /nobreak >nul
start "POLYMARKET - DIAMOND + RISK" cmd /k run_analysis_stack.bat
timeout /t 2 /nobreak >nul
start "POLYMARKET - SYSTEM STATUS" cmd /k ""%PY%" -u system_status.py"
exit /b 0
