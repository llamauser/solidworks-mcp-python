@echo off
rem Double-click to open the SolidWorks Assistant in your browser. Close this window to stop it.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The assistant is not installed yet.
  echo Double-click "Install SolidWorks Assistant.bat" first.
  echo.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m sw_agent web
if errorlevel 1 pause
