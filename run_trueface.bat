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

REM The Python mutex is the authoritative single-instance guard. This
REM process check avoids needless wrapper churn during normal startup.
wmic process where "name='python.exe'" get commandline 2>nul | find /i "trueface_poller" >nul
if %ERRORLEVEL% EQU 0 (
    echo [%DATE% %TIME%] TrueFace poller already running; launcher exiting.
    exit /b 0
)

py -3.12 trueface_poller.py
echo.
echo [%DATE% %TIME%] Poller stopped (exit code: %ERRORLEVEL%). Restarting in 10 seconds...
timeout /t 10 /nobreak
goto loop
