@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem GENERAL1-GAMMA-1: independent 900-second opt-in experiment.
set "BASE=C:\PolymarketResearch"
set "TARGET=%BASE%\GENERAL1_GAMMA1_LIVE_SANDBOX"
for %%I in ("%~dp0..") do set "LAUNCHER=%%~fI"
set "PY=%LAUNCHER%\.venv\Scripts\python.exe"
set "BRANCH=feat/general1-gamma-1-bounded-metadata-20261025"

if not exist "%PY%" (
  echo [BLOCKED] Missing local Python environment in launcher; no live worker started.
  exit /b 11
)
if exist "%TARGET%" (
  echo [BLOCKED] Already exists. No reuse or destructive cleanup.
  exit /b 12
)
where git >nul 2>&1
if errorlevel 1 (
  echo [BLOCKED] Git unavailable.
  exit /b 13
)
echo [SETUP] Cloning one-shot sandbox, preserving previous GENERAL1-FRESHNESS1 data...
git clone --depth 1 --single-branch --branch "%BRANCH%" "https://github.com/mudrjafar/PolymarketResearch.git" "%TARGET%"
if errorlevel 1 exit /b 14
cd /d "%TARGET%"

if defined POLYMARKET_RPC_URL goto supplied
if exist "%BASE%\PolymarketResearch_machine\.env" (
  "%PY%" -B scripts\general1_gamma1_live_probe.py --source-env "%BASE%\PolymarketResearch_machine\.env" --duration 900
  goto completed
)
if exist "%BASE%\V3_test\PolymarketResearch_machine\.env" (
  "%PY%" -B scripts\general1_gamma1_live_probe.py --source-env "%BASE%\V3_test\PolymarketResearch_machine\.env" --duration 900
  goto completed
)
echo [BLOCKED] Configure an external Polygon HTTPS RPC. No .env is copied.
exit /b 15

:supplied
"%PY%" -B scripts\general1_gamma1_live_probe.py --duration 900

:completed
set "RESULT=%ERRORLEVEL%"
echo [FINAL] GAMMA-1 observer exit code: %RESULT%
if exist "%TARGET%\general1_gamma1_live_report.json" (
  explorer /select,"%TARGET%\general1_gamma1_live_report.json"
) else (
  echo [BLOCKED] No report produced. Keep sandbox unchanged.
)
exit /b %RESULT%
