@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -m venv .venv
)
call ".venv\Scripts\activate.bat"
python -m pip install -r requirements.txt
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" if not "%%A:~0,1"=="#" set "%%A=%%B"
  )
) else (
  echo ERROR: .env not found. Copy .env.example to .env and add your keys.
  pause
  exit /b 2
)
python run_machine.py
pause
