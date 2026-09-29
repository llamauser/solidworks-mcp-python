@echo off
setlocal
rem SolidWorks Assistant - everything in one menu. Double-click it.
cd /d "%~dp0"
title SolidWorks Assistant - Tools
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo The assistant is not installed yet. Installing it now...
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
  if not exist "%PY%" (
    echo.
    echo The installation did not finish. Please send a screenshot of this window to the developer.
    pause
    exit /b 1
  )
)

:menu
cls
echo ==================================================
echo    SolidWorks Assistant - Tools
echo ==================================================
echo.
echo    1  Start the assistant (in your browser)
echo    2  Start the assistant (in this window)
echo    3  Check SolidWorks works (takes 1-3 minutes)
echo    4  Collect all logs for the developer
echo    5  Connect or change AI providers
echo    6  Show connected AI providers
echo    7  Score the AI models (uses some free requests)
echo    8  Update to the latest version
echo    9  Pack my builds and ratings to share
echo    0  Exit
echo.
set "choice="
set /p "choice=Type a number and press Enter: "
if "%choice%"=="1" goto web
if "%choice%"=="2" goto chat
if "%choice%"=="3" goto check
if "%choice%"=="4" goto logs
if "%choice%"=="5" goto setup
if "%choice%"=="6" goto status
if "%choice%"=="7" goto bench
if "%choice%"=="8" goto update
if "%choice%"=="9" goto share
if "%choice%"=="0" exit /b 0
goto menu

:web
echo Starting... a browser page will open. Close the browser tab and press Ctrl+C here to stop.
"%PY%" -m sw_agent web
goto done

:chat
"%PY%" -m sw_agent chat
goto done

:check
"%PY%" -m sw_agent check
goto done

:logs
"%PY%" -m sw_agent logs
goto done

:setup
"%PY%" -m sw_agent setup
goto done

:status
"%PY%" -m sw_agent status
goto done

:bench
"%PY%" -m sw_agent bench
goto done

:share
"%PY%" -m sw_agent share
goto done

:update
where git >nul 2>nul
if errorlevel 1 (
  echo Git is not installed, so this copy cannot update itself.
  echo Download the latest ZIP from GitHub, unzip it over this folder, then run this menu again.
  goto done
)
git checkout -- opencode.json >nul 2>nul
git pull
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Quiet
goto done

:done
echo.
pause
goto menu
