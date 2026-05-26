@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM PPIS Campus Agent — Watchdog (runs every 5 minutes)
REM Checks if the agent is alive; restarts it if not.
REM This script is called by Windows Task Scheduler silently.
REM ============================================================

cd /d "%~dp0"

REM Log file for watchdog events
set LOGFILE=%~dp0watchdog.log
set NEED_AGENT=0
set NEED_TRUEFACE=0

REM Check if campus agent (main.py) is running
wmic process where "name='python.exe'" get commandline 2>nul | find /i "main.py" >nul
if %ERRORLEVEL% NEQ 0 (
    set NEED_AGENT=1
)

REM Check if TrueFace poller (trueface_poller.py) is running
wmic process where "name='python.exe'" get commandline 2>nul | find /i "trueface_poller" >nul
if %ERRORLEVEL% NEQ 0 (
    set NEED_TRUEFACE=1
)

REM If both are running, do nothing
if "%NEED_AGENT%"=="0" if "%NEED_TRUEFACE%"=="0" exit /b 0

REM Restart campus agent if needed
if "%NEED_AGENT%"=="1" (
    echo [%DATE% %TIME%] WATCHDOG: Campus agent not running, restarting... >> "%LOGFILE%"
    REM Clean stale lock file
    if exist "%~dp0.agent_lock" del "%~dp0.agent_lock" >nul 2>&1
    start "" /B wscript.exe "%~dp0run_hidden.vbs"
    echo [%DATE% %TIME%] WATCHDOG: Campus agent restart triggered >> "%LOGFILE%"
)

REM Restart TrueFace poller if needed
if "%NEED_TRUEFACE%"=="1" (
    echo [%DATE% %TIME%] WATCHDOG: TrueFace poller not running, restarting... >> "%LOGFILE%"
    start "" /B wscript.exe "%~dp0run_trueface_hidden.vbs"
    echo [%DATE% %TIME%] WATCHDOG: TrueFace poller restart triggered >> "%LOGFILE%"
)

REM Keep log file from growing too large (rotate at 500 lines)
if exist "%LOGFILE%" (
    set LINES=0
    for /f %%a in ('type "%LOGFILE%" ^| find /c /v ""') do set LINES=%%a
    if !LINES! GTR 500 (
        move /y "%LOGFILE%" "%LOGFILE%.old" >nul 2>&1
        echo [%DATE% %TIME%] WATCHDOG: Log rotated >> "%LOGFILE%"
    )
)
