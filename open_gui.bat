@echo off
rem One-click Web GUI launcher. Double-click to open the local dashboard in browser.
chcp 65001 >nul
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
  echo [!] venv not found. Run:
  echo     python -m venv .venv
  echo     .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)
echo.
echo   bmon Web GUI starting... http://127.0.0.1:8322
echo   Close this window to stop the server and scheduler.
echo.
".venv\Scripts\python.exe" main.py gui
pause
