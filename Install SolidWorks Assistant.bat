@echo off
rem Double-click to install (or update) the SolidWorks Assistant. Safe to run again.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
