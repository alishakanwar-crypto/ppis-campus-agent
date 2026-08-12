@echo off
setlocal enabledelayedexpansion
REM TrueFace 3000 Auto-Poller — Auto-restart wrapper
REM Runs alongside the main campus agent.
REM Auto-restarts if the poller crashes.

title TrueFace 3000 Poller (24/7)
cd /d "%~dp0"

set PYTHONDONTWRITEBYTECODE=1
REM Must match DUPLICATE_INSTANCE_EXIT_CODE in trueface_instance.py.
set "DUPLICATE_EXIT_CODE=75"

:loop
echo.
echo ============================================
echo [%DATE% %TIME%] Starting TrueFace Poller...
echo ============================================

REM Pull latest code
git fetch origin 2>nul
git reset --hard origin/main 2>nul

REM The Python mutex is the authoritative single-instance guard. This
REM check only waits for a process that is still shutting down; it must
REM never make the supervision wrapper exit.
set "DUPLICATE_WAIT=0"
:wait_for_existing_poller
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'python.exe' }; if ($p | Where-Object { $_.CommandLine -match 'trueface_poller' }) { exit 0 } else { exit 1 }" >nul 2>&1
if !ERRORLEVEL! NEQ 0 goto launch_poller
echo [%DATE% %TIME%] Existing TrueFace poller is still clearing; waiting... >> "%~dp0trueface_poller.log"
if !DUPLICATE_WAIT! GEQ 30 (
    echo [%DATE% %TIME%] Existing poller did not clear after 30 seconds; proceeding and letting the mutex decide. >> "%~dp0trueface_poller.log"
    goto launch_poller
)
set /a DUPLICATE_WAIT+=5
timeout /t 5 /nobreak >nul
goto wait_for_existing_poller

:launch_poller
py -3.12 trueface_poller.py
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="%DUPLICATE_EXIT_CODE%" (
    echo [%DATE% %TIME%] Another TrueFace poller owns the mutex; wrapper exiting cleanly.
    exit /b 0
)
echo.
echo [%DATE% %TIME%] Poller stopped (exit code: %EXIT_CODE%). Restarting in 10 seconds...
timeout /t 10 /nobreak
goto loop
