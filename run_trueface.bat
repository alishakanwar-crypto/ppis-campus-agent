@echo off
setlocal enabledelayedexpansion
REM TrueFace 3000 Auto-Poller — Auto-restart wrapper
REM Runs alongside the main campus agent.
REM Auto-restarts if the poller crashes.

title TrueFace 3000 Poller (24/7)
cd /d "%~dp0"
set "LOGFILE=%~dp0wrapper_trueface.log"

if not exist "%~dp0.locks\" mkdir "%~dp0.locks" >nul 2>&1
call :cap_log
call :run 9>"%~dp0.locks\trueface.lock"
if errorlevel 1 (
    echo [%DATE% %TIME%] WRAPPER: TrueFace lock is already held; exiting cleanly. >> "%LOGFILE%"
    exit /b 0
)
exit /b 0

:run

set PYTHONDONTWRITEBYTECODE=1
REM Must match DUPLICATE_INSTANCE_EXIT_CODE in trueface_instance.py.
set "DUPLICATE_EXIT_CODE=75"

:loop
call :cap_log
echo [%DATE% %TIME%] Starting TrueFace Poller... >> "%LOGFILE%"

REM Fetch latest code, but never reset to an unverified stale remote ref.
git fetch origin >nul 2>&1
if errorlevel 1 (
    echo [%DATE% %TIME%] GIT: Fetch failed; keeping existing working tree. >> "%LOGFILE%"
) else (
    git checkout main >nul 2>&1
    if errorlevel 1 (
        echo [%DATE% %TIME%] GIT: Checkout main failed; keeping existing working tree. >> "%LOGFILE%"
    ) else (
        git reset --hard origin/main >nul 2>&1
        if errorlevel 1 echo [%DATE% %TIME%] GIT: Reset origin/main failed; keeping existing working tree. >> "%LOGFILE%"
    )
)

REM The Python mutex is the authoritative single-instance guard. This
REM check only waits for a process that is still shutting down; it must
REM never make the supervision wrapper exit.
set "DUPLICATE_WAIT=0"
:wait_for_existing_poller
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'python.exe' }; if ($p | Where-Object { $_.CommandLine -match 'trueface_poller' }) { exit 0 } else { exit 1 }" >nul 2>&1
if !ERRORLEVEL! NEQ 0 goto launch_poller
echo [%DATE% %TIME%] Existing TrueFace poller is still clearing; waiting... >> "%LOGFILE%"
if !DUPLICATE_WAIT! GEQ 30 (
    echo [%DATE% %TIME%] Existing poller did not clear after 30 seconds; proceeding and letting the mutex decide. >> "%LOGFILE%"
    goto launch_poller
)
set /a DUPLICATE_WAIT+=5
timeout /t 5 /nobreak >nul
goto wait_for_existing_poller

:launch_poller
set "REVISION=unknown"
for /f "delims=" %%H in ('git rev-parse --short HEAD 2^>nul') do set "REVISION=%%H"
echo [%DATE% %TIME%] Starting revision !REVISION! >> "%LOGFILE%"
py -3.12 trueface_poller.py
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="%DUPLICATE_EXIT_CODE%" (
    echo [%DATE% %TIME%] Another TrueFace poller owns the mutex; wrapper exiting cleanly. >> "%LOGFILE%"
    exit /b 0
)
echo [%DATE% %TIME%] Poller stopped (exit code: %EXIT_CODE%). Restarting in 10 seconds... >> "%LOGFILE%"
timeout /t 10 /nobreak
goto loop

:cap_log
if exist "%LOGFILE%" (
    set "LINES=0"
    for /f %%a in ('type "%LOGFILE%" ^| find /c /v ""') do set "LINES=%%a"
    if !LINES! GTR 500 (
        move /y "%LOGFILE%" "%LOGFILE%.old" >nul 2>&1
    )
)
exit /b 0
