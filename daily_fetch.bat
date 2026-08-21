@echo off
rem Daily auto-fetch task, called by Windows Task Scheduler "BiliMonDailyFetch" (21:30 daily).
rem Can also be double-clicked manually to run one collection cycle.
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
  echo [!] venv not found, see README for install >> data\scheduled.log 2>&1
  exit /b 1
)
".venv\Scripts\python.exe" main.py fetch >> data\scheduled.log 2>&1
