@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem GENERAL1-E2E-1: SAFE local OFFLINE smoke + read-only production DB preflight.
rem NEVER starts the production machine; NEVER modifies production data or .env.
set "PROD=C:\PolymarketResearch\PolymarketResearch_machine"
set "SANDBOX=C:\PolymarketResearch\GENERAL1_E2E1_SANDBOX"
set "REPO=https://github.com/mudrjafar/PolymarketResearch.git"
set "BRANCH=feat/general1-e2e1-integration-smoke-20261019"

if not exist "%PROD%\.venv\Scripts\python.exe" (
  echo [BLOCKED] Existing Python environment missing: %PROD%\.venv
  exit /b 10
)
if exist "%SANDBOX%" (
  echo [BLOCKED] Sandbox already exists. No automatic deletion or overwrite: %SANDBOX%
  exit /b 11
)
where git >nul 2>&1
if errorlevel 1 (
  echo [BLOCKED] git not available.
  exit /b 12
)
git clone --depth 1 --single-branch --branch "%BRANCH%" "%REPO%" "%SANDBOX%"
if errorlevel 1 (
  echo [BLOCKED] Could not clone independent smoke-test checkout.
  exit /b 13
)
cd /d "%SANDBOX%"
set "PY=%PROD%\.venv\Scripts\python.exe"

echo.
echo === OFFLINE PIPELINE SMOKE (ISOLATED TEMP FILES) ===
"%PY%" -B -m pytest -q tests\test_general1_e2e_smoke.py
if errorlevel 1 (
  echo [BLOCKED] Isolated integration smoke failed.
  exit /b 20
)
echo.
echo === COMPLETE OFFLINE PROJECT REGRESSION ===
"%PY%" -B -m pytest -q
if errorlevel 1 (
  echo [BLOCKED] Full offline regression failed.
  exit /b 21
)
echo.
echo === MACHINE SELF-TEST (NO LIVE WORKERS) ===
"%PY%" -B run_machine.py --self-test
if errorlevel 1 (
  echo [BLOCKED] Machine self-test failed.
  exit /b 22
)

echo.
echo === EXISTING PRODUCTION SQLITE: READ-ONLY PREFLIGHT ===
if not exist "%PROD%\data\collector.sqlite3" (
  echo [OFFLINE PASS] Existing production DB not found. No live-start permission.
  echo Local sandbox: %SANDBOX%
  exit /b 0
)
"%PY%" -B scripts\collector_db_preflight.py --db "%PROD%\data\collector.sqlite3" --json > "general1_e2e1_db_preflight.json"
if errorlevel 1 (
  echo [OFFLINE PASS, DB BLOCKED] Preflight found an issue or legacy schema.
  echo Attach %SANDBOX%\general1_e2e1_db_preflight.json for review.
  exit /b 0
)
"%PY%" -B -c "import json; p=json.load(open('general1_e2e1_db_preflight.json',encoding='utf-8')); print('[DB PREFLIGHT]',p.get('result')); raise SystemExit(0 if p.get('result')=='CURRENT_SCHEMA_OK' else 2)"
if errorlevel 1 (
  echo [OFFLINE PASS, LIVE BLOCKED] Database needs separate review or migration.
  echo Attach %SANDBOX%\general1_e2e1_db_preflight.json for review.
  exit /b 0
)
echo [OFFLINE PASS, DB CURRENT] Tests passed and existing DB schema validated.
echo [NOTE] This script deliberately does NOT launch live workers.
echo Attach %SANDBOX%\general1_e2e1_db_preflight.json for review.
exit /b 0
