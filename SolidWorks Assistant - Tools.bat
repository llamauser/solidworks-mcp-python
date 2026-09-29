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
echo    2  Start OpenCode in this window (SolidWorks agent)
echo    3  Check SolidWorks works (takes 1-3 minutes)
echo    4  Collect all logs for the developer
echo    5  Connect an AI provider (optional)
echo    6  Show OpenCode, AI providers and models
echo    7  Pack my builds and ratings to share
echo    8  Update everything (assistant and OpenCode)
echo    0  Exit
echo.
set "choice="
set /p "choice=Type a number and press Enter: "
if "%choice%"=="1" goto web
if "%choice%"=="2" goto cli
if "%choice%"=="3" goto check
if "%choice%"=="4" goto logs
if "%choice%"=="5" goto connect
if "%choice%"=="6" goto status
if "%choice%"=="7" goto share
if "%choice%"=="8" goto update
if "%choice%"=="0" exit /b 0
goto menu

:web
echo Starting OpenCode in the background, then the page opens in your browser.
echo Close the browser tab and press Ctrl+C here to stop.
"%PY%" -m sw_agent web
goto done

:cli
"%PY%" -m sw_agent cli
goto done

:check
"%PY%" -m sw_agent check
goto done

:logs
"%PY%" -m sw_agent logs
goto done

:connect
"%PY%" -m sw_agent connect
goto done

:status
"%PY%" -m sw_agent status
"%PY%" -m sw_agent models
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
rem generated files: keep the repository's version (put personal OpenCode settings in the global
rem OpenCode config, %USERPROFILE%\.config\opencode\opencode.json)
git checkout -- opencode.json .opencode/agents/solidworks.md >nul 2>nul
git pull
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Quiet -SkipTests
goto done

:done
echo.
pause
goto menu
