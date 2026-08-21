@echo off
rem Continuous monitoring mode (Ctrl+C to stop). Requires finished install, see README.
chcp 65001 >nul
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
  echo [!] venv not found. Run:
  echo     python -m venv .venv
  echo     .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py run
pause
