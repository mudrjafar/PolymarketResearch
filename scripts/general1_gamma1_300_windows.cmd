@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Fixed 300-second Gamma comparison; preserves all previously created sandboxes.
set "BASE=C:\PolymarketResearch"
set "TARGET=%BASE%\GENERAL1_GAMMA1_300_LIVE_SANDBOX"
for %%I in ("%~dp0..") do set "LAUNCHER=%%~fI"
set "PY=%LAUNCHER%\.venv\Scripts\python.exe"
set "BRANCH=feat/general1-gamma-1-bounded-metadata-20261025"

if not exist "%PY%" (
  echo [BLOCKED] Launcher venv missing. No worker started.
  exit /b 11
)
if exist "%TARGET%" (
  echo [BLOCKED] The dedicated 300s sandbox exists. Never reuse or delete it automatically.
  exit /b 12
)
where git >nul 2>&1
if errorlevel 1 (
  echo [BLOCKED] Git unavailable.
  exit /b 13
)
echo [SETUP] Dedicated 300s clone. Existing GENERAL1-GAMMA1_LIVE_SANDBOX preserved.
git clone --depth 1 --single-branch --branch "%BRANCH%" "https://github.com/mudrjafar/PolymarketResearch.git" "%TARGET%"
if errorlevel 1 exit /b 14
cd /d "%TARGET%"
if errorlevel 1 exit /b 16

if defined POLYMARKET_RPC_URL goto supplied
if exist "%BASE%\PolymarketResearch_machine\.env" (
  "%PY%" -B scripts\general1_gamma1_300_live_probe.py --source-env "%BASE%\PolymarketResearch_machine\.env"
  goto completed
)
if exist "%BASE%\V3_test\PolymarketResearch_machine\.env" (
  "%PY%" -B scripts\general1_gamma1_300_live_probe.py --source-env "%BASE%\V3_test\PolymarketResearch_machine\.env"
  goto completed
)
echo [BLOCKED] Set POLYMARKET_RPC_URL in this CMD session before starting.
exit /b 15

:supplied
"%PY%" -B scripts\general1_gamma1_300_live_probe.py

:completed
set "RESULT=%ERRORLEVEL%"
echo [FINAL] 300s Gamma observer exit code: %RESULT%
if exist "%TARGET%\general1_gamma1_300_live_report.json" (
  explorer /select,"%TARGET%\general1_gamma1_300_live_report.json"
) else (
  echo [BLOCKED] No report produced. Keep sandbox unchanged.
)
exit /b %RESULT%
