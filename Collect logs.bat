@echo off
rem Double-click to put the assistant's logs in a zip on your Desktop (no API keys inside).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The assistant is not installed yet.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m sw_agent logs
echo.
pause
