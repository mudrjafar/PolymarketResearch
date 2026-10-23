@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem GENERAL1-FRESHNESS-1 / one-shot sandbox only. DO NOT call production launchers.
set "BASE=C:\PolymarketResearch"
set "TARGET=%BASE%\GENERAL1_FRESHNESS1_LIVE_SANDBOX"
rem Prefer the isolated launcher's own venv for a fresh Windows machine.
for %%I in ("%~dp0..") do set "LAUNCHER=%%~fI"
set "PY=%LAUNCHER%\.venv\Scripts\python.exe"
rem Retain compatibility with the earlier tested environment on the original PC.
if not exist "%PY%" set "PY=%BASE%\GENERAL1_E2E1_SANDBOX\.venv\Scripts\python.exe"
set "BRANCH=feat/general1-freshness-1-neartip-diagnostics-20261023"

if not exist "%PY%" (
  echo [BLOCKED] Python venv missing. Create one in the launcher checkout with: py -3.11 -m venv .venv
  echo [BLOCKED] Then install requirements.txt using the venv Python before retrying.
  exit /b 11
)
if exist "%TARGET%" (
  echo [BLOCKED] Existing sandbox must be archived. No reuse, deletion or overwrite.
  exit /b 12
)
where git >nul 2>&1
if errorlevel 1 (
  echo [BLOCKED] Git missing from PATH.
  exit /b 13
)

echo [SETUP] Fresh separate near-tip checkout - only 30 initial blocks, 20 confirmations.
git clone --depth 1 --single-branch --branch "%BRANCH%" "https://github.com/mudrjafar/PolymarketResearch.git" "%TARGET%"
if errorlevel 1 exit /b 14
cd /d "%TARGET%"
echo [SETUP] Historical databases and .env are NOT copied. No backfill completeness claim.

if defined POLYMARKET_RPC_URL goto supplied
if exist "%BASE%\PolymarketResearch_machine\.env" (
  "%PY%" -B scripts\general1_freshness1_live_probe.py --source-env "%BASE%\PolymarketResearch_machine\.env" --duration 900
  goto done
)
if exist "%BASE%\V3_test\PolymarketResearch_machine\.env" (
  "%PY%" -B scripts\general1_freshness1_live_probe.py --source-env "%BASE%\V3_test\PolymarketResearch_machine\.env" --duration 900
  goto done
)
echo [BLOCKED] No existing external HTTPS RPC configuration.
exit /b 15

:supplied
"%PY%" -B scripts\general1_freshness1_live_probe.py --duration 900

:done
set "RESULT=%ERRORLEVEL%"
echo [FINAL] Freshness observer exit code: %RESULT%
if exist "%TARGET%\general1_freshness1_live_report.json" (
  explorer /select,"%TARGET%\general1_freshness1_live_report.json"
) else (
  echo [BLOCKED] No report produced.
)
exit /b %RESULT%
