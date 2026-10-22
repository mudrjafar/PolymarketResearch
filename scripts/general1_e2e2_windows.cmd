@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem GENERAL1-E2E-2 / one-shot isolated live test. Never call production launcher.
set "BASE=C:\PolymarketResearch"
set "TARGET=%BASE%\GENERAL1_E2E2_LIVE_SANDBOX"
set "PY=%BASE%\GENERAL1_E2E1_SANDBOX\.venv\Scripts\python.exe"
set "BRANCH=feat/general1-e2e2-isolated-live-runtime-20261020"

if not exist "%PY%" (
  echo [BLOCKED] Existing tested Python environment not found: %PY%
  exit /b 11
)
if exist "%TARGET%" (
  echo [BLOCKED] Live sandbox already exists; no reuse, deletion or overwrite.
  echo Inspect your previous report in %TARGET% if this is a repeat attempt.
  exit /b 12
)
where git >nul 2>&1
if errorlevel 1 (
  echo [BLOCKED] Git is not installed or not on PATH.
  exit /b 13
)

echo [SETUP] Cloning a clean independent live checkout...
git clone --depth 1 --single-branch --branch "%BRANCH%" "https://github.com/mudrjafar/PolymarketResearch.git" "%TARGET%"
if errorlevel 1 exit /b 14
cd /d "%TARGET%"
echo [SETUP] Sandbox ready. Production databases will NOT be copied or migrated.
echo [SETUP] RPC configuration is read into memory from an external .env, never copied to the clone.

if defined POLYMARKET_RPC_URL goto supplied_environment
if exist "%BASE%\PolymarketResearch_machine\.env" (
  "%PY%" -B scripts\general1_e2e2_live_probe.py --source-env "%BASE%\PolymarketResearch_machine\.env" --duration 900
  goto completed
)
if exist "%BASE%\V3_test\PolymarketResearch_machine\.env" (
  "%PY%" -B scripts\general1_e2e2_live_probe.py --source-env "%BASE%\V3_test\PolymarketResearch_machine\.env" --duration 900
  goto completed
)
echo [BLOCKED] No POLYMARKET_RPC_URL environment variable or known external .env.
echo Set your RPC only locally, then run the Python observer directly from this sandbox.
exit /b 15

:supplied_environment
"%PY%" -B scripts\general1_e2e2_live_probe.py --duration 900

:completed
set "RESULT=%ERRORLEVEL%"
echo.
echo [FINAL] Live observer exit code: %RESULT%
if exist "%TARGET%\general1_e2e2_live_report.json" (
  explorer /select,"%TARGET%\general1_e2e2_live_report.json"
) else (
  echo [BLOCKED] No live report produced. Review the console status.
)
exit /b %RESULT%
