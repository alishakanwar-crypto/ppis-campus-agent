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
set NEED_GATE_COUNTER=0

REM Check if campus agent (main.py) is running. If PowerShell/CIM fails,
REM the nonzero status deliberately fails open and requests a start.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'python.exe' }; if ($p | Where-Object { $_.CommandLine -match 'main.py' }) { exit 0 } else { exit 1 }" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    set NEED_AGENT=1
)

REM Check if TrueFace poller (trueface_poller.py) is running. If the
REM process query fails, request a start; the Python mutex remains
REM authoritative if an old process is still shutting down.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'python.exe' }; if ($p | Where-Object { $_.CommandLine -match 'trueface_poller' }) { exit 0 } else { exit 1 }" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    set NEED_TRUEFACE=1
)

REM Check if the gate counter is running. If the query fails, fail open and
REM request a start so native CP Plus counts self-heal.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'python.exe' }; if ($p | Where-Object { $_.CommandLine -match 'gate_counter\.py' }) { exit 0 } else { exit 1 }" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    set NEED_GATE_COUNTER=1
)

REM If all three are running, do nothing
if "%NEED_AGENT%"=="0" if "%NEED_TRUEFACE%"=="0" if "%NEED_GATE_COUNTER%"=="0" exit /b 0

REM Restart campus agent if needed
if "%NEED_AGENT%"=="1" (
    echo [%DATE% %TIME%] WATCHDOG: Campus agent not running, restarting... >> "%LOGFILE%"
    REM Clean stale lock file
    if exist "%~dp0.agent_lock" del "%~dp0.agent_lock" >nul 2>&1
    call :recover_stale_wrapper "Campus agent" "run_forever" "main\.py"
    if !ERRORLEVEL! EQU 1 (
        echo [%DATE% %TIME%] WATCHDOG: Recent campus-agent wrapper is still starting; skipping this cycle. >> "%LOGFILE%"
    ) else (
        start "" /B wscript.exe "%~dp0run_hidden.vbs"
        echo [%DATE% %TIME%] WATCHDOG: Campus agent restart triggered >> "%LOGFILE%"
    )
)

REM Restart TrueFace poller if needed
if "%NEED_TRUEFACE%"=="1" (
    echo [%DATE% %TIME%] WATCHDOG: TrueFace poller not running, restarting... >> "%LOGFILE%"
    call :recover_stale_wrapper "TrueFace poller" "run_trueface" "trueface_poller"
    if !ERRORLEVEL! EQU 1 (
        echo [%DATE% %TIME%] WATCHDOG: Recent TrueFace wrapper is still starting; skipping this cycle. >> "%LOGFILE%"
    ) else (
        start "" /B wscript.exe "%~dp0run_trueface_hidden.vbs"
        echo [%DATE% %TIME%] WATCHDOG: TrueFace poller restart triggered >> "%LOGFILE%"
    )
)

REM Restart gate counter if needed
if "%NEED_GATE_COUNTER%"=="1" (
    echo [%DATE% %TIME%] WATCHDOG: Gate counter not running, restarting... >> "%LOGFILE%"
    call :recover_stale_wrapper "Gate counter" "run_gate_counter" "gate_counter\.py"
    if !ERRORLEVEL! EQU 1 (
        echo [%DATE% %TIME%] WATCHDOG: Recent gate-counter wrapper is still starting; skipping this cycle. >> "%LOGFILE%"
    ) else (
        start "" /B wscript.exe "%~dp0run_gate_counter_hidden.vbs"
        echo [%DATE% %TIME%] WATCHDOG: Gate counter restart triggered >> "%LOGFILE%"
    )
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

exit /b 0

:recover_stale_wrapper
set "PRODUCER=%~1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try { $all=@(Get-CimInstance Win32_Process -ErrorAction Stop) } catch { exit 3 }; $wrappers=@($all | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -match '%~2(\.bat)?' }); if ($wrappers.Count -eq 0) { exit 0 }; $cutoff=(Get-Date).AddMinutes(-3); $recent=@($wrappers | Where-Object { $_.CreationDate -and $_.CreationDate -gt $cutoff }); if ($recent.Count -gt 0) { exit 1 }; $wrapperIds=@($wrappers | ForEach-Object { $_.ProcessId }); $children=@($all | Where-Object { $wrapperIds -contains $_.ParentProcessId -and $_.Name -in @('py.exe','python.exe') -and $_.CommandLine -match '%~3' }); @($wrappers + $children) | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; exit 2" >nul 2>&1
if !ERRORLEVEL! EQU 1 (
    echo [%DATE% %TIME%] WATCHDOG: Recent !PRODUCER! wrapper is still starting; leaving it alone. >> "%LOGFILE%"
    exit /b 1
) else if !ERRORLEVEL! EQU 2 (
    echo [%DATE% %TIME%] WATCHDOG: Stale !PRODUCER! wrapper found; termination attempted. >> "%LOGFILE%"
    timeout /t 2 /nobreak >nul
    exit /b 2
)
echo [%DATE% %TIME%] WATCHDOG: No usable !PRODUCER! wrapper result; starting anyway. >> "%LOGFILE%"
exit /b 0
