@echo off
REM ============================================================
REM PPIS Campus Agent — Watchdog (runs every 5 minutes)
REM Checks if the agent is alive; restarts it if not.
REM This script is called by Windows Task Scheduler silently.
REM ============================================================

cd /d "%~dp0"

REM Log file for watchdog events
set LOGFILE=%~dp0watchdog.log

REM Check if python.exe is running
tasklist /fi "imagename eq python.exe" 2>nul | find /i "python.exe" >nul
if %ERRORLEVEL% EQU 0 (
    REM Agent is running — do nothing
    exit /b 0
)

REM Agent is NOT running — restart it
echo [%DATE% %TIME%] WATCHDOG: Agent not running, restarting... >> "%LOGFILE%"

REM Also check if run_forever.bat is somehow stuck — kill stale wscript
taskkill /F /IM wscript.exe /FI "WINDOWTITLE eq *" >nul 2>&1

REM Start the agent via the hidden runner
start "" /B wscript.exe "%~dp0run_hidden.vbs"

echo [%DATE% %TIME%] WATCHDOG: Restart triggered via run_hidden.vbs >> "%LOGFILE%"

REM Keep log file from growing too large (keep last 200 lines)
if exist "%LOGFILE%" (
    set LINES=0
    for /f %%a in ('type "%LOGFILE%" ^| find /c /v ""') do set LINES=%%a
    if %LINES% GTR 500 (
        move /y "%LOGFILE%" "%LOGFILE%.old" >nul 2>&1
        echo [%DATE% %TIME%] WATCHDOG: Log rotated >> "%LOGFILE%"
    )
)
