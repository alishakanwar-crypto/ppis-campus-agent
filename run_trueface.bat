@echo off
REM TrueFace 3000 Auto-Poller — Auto-restart wrapper
REM Runs alongside the main campus agent.
REM Auto-restarts if the poller crashes.

title TrueFace 3000 Poller (24/7)
cd /d "%~dp0"

set PYTHONDONTWRITEBYTECODE=1

:loop
echo.
echo ============================================
echo [%DATE% %TIME%] Starting TrueFace Poller...
echo ============================================

REM Pull latest code
git fetch origin 2>nul
git reset --hard origin/main 2>nul

py -3.12 trueface_poller.py
echo.
echo [%DATE% %TIME%] Poller stopped (exit code: %ERRORLEVEL%). Restarting in 10 seconds...
timeout /t 10 /nobreak
goto loop
