@echo off
rem Double-click to check that the assistant can drive SolidWorks. The logs are packed at the end.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The assistant is not installed yet. Double-click "Install SolidWorks Assistant.bat" first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m sw_agent check
echo.
pause
