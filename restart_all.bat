@echo off
setlocal enabledelayedexpansion
REM ================================================================
REM  PPIS Campus Agent — One-Click Restart
REM  Cleanly stops ALL running agents, pulls latest code, and
REM  restarts all 3 processes (Campus Agent + TrueFace + Gate Counter).
REM
REM  Usage: Right-click > Run as Administrator
REM         (Admin is needed to kill processes started by other windows)
REM ================================================================

REM Re-run from a temp copy: step 3 pulls code, and rewriting the script
REM cmd.exe is still reading by byte offset corrupts the rest of the run.
if /I not "%~1"=="--from-temp" (
    set "SELF_COPY=%TEMP%\ppis_restart_all.bat"
    copy /y "%~f0" "!SELF_COPY!" >nul 2>&1
    if exist "!SELF_COPY!" (
        call "!SELF_COPY!" --from-temp "%~dp0"
        exit /b !ERRORLEVEL!
    )
    echo   WARNING: Could not copy this script to %TEMP%; running in place.
)

if /I "%~1"=="--from-temp" (
    cd /d "%~2"
) else (
    cd /d "%~dp0"
)
echo.
echo ========================================================
echo   PPIS Campus Agent — Restarting All Processes
echo ========================================================
echo.

REM --- Step 1: Kill all hidden cmd.exe processes running our batch files ---
echo [Step 1/5] Killing hidden batch file processes...
powershell -Command "Get-WmiObject Win32_Process -Filter \"Name='cmd.exe'\" | Where-Object { $_.CommandLine -match 'run_forever|run_trueface|run_gate_counter|run_chairman_mood' } | ForEach-Object { Write-Host ('  Killed cmd.exe PID ' + $_.ProcessId); $_.Terminate() | Out-Null }" 2>nul
timeout /t 3 /nobreak >nul

REM --- Step 2: Kill all Python processes (loop until none remain) ---
REM py.exe / pythonw.exe must go too: a surviving launcher keeps the
REM wrapper lock handles open, which makes later wrapper starts no-op.
REM tasklist ANDs several /FI IMAGENAME filters, so the old check could never
REM match and the kill was skipped: agents survived and kept running old code.
echo [Step 2/5] Killing all Python processes...
set "RETRIES=0"
:kill_loop
taskkill /F /IM python.exe >nul 2>&1
taskkill /F /IM py.exe >nul 2>&1
taskkill /F /IM pythonw.exe >nul 2>&1
timeout /t 2 /nobreak >nul
call :count_python
if !PYCOUNT! GTR 0 (
    set /a RETRIES+=1
    if !RETRIES! GEQ 10 (
        echo   WARNING: !PYCOUNT! Python process^(es^) survived 10 kill attempts.
        echo   Run this script as Administrator - the agents are still on old code.
        goto start_agents
    )
    echo   !PYCOUNT! Python process^(es^) still alive, killing again...
    goto kill_loop
)
echo   All Python processes terminated.

REM --- Step 3: Pull latest code ---
echo [Step 3/5] Pulling latest code from GitHub...
git fetch origin 2>nul
echo   Using branch: main
git checkout main 2>nul
git reset --hard origin/main 2>nul
echo   Code updated.

REM --- Step 4: Clean up lock file ---
echo [Step 4/5] Cleaning up...
if exist ".agent_lock" del ".agent_lock" >nul 2>&1
if exist "__pycache__" rmdir /s /q "__pycache__" 2>nul
REM Ensure config.json exists and is valid (empty file causes crash)
if not exist "config.json" echo {} > "config.json"
for %%A in (config.json) do if %%~zA EQU 0 echo {} > "config.json"
REM Also kill any stale port hold from previous run
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8897 ^| findstr LISTENING') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul

:start_agents
REM --- Step 5: Start all 3 agents with delays ---
echo [Step 5/5] Starting agents...
echo.

REM Kill any process holding port 8897 before starting campus agent
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8897 ^| findstr LISTENING') do (
    echo   Killing stale process on port 8897 PID %%a...
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 3 /nobreak >nul

echo   Starting Campus Agent...
wscript.exe run_hidden.vbs
timeout /t 20 /nobreak >nul

echo   Starting TrueFace Poller...
wscript.exe run_trueface_hidden.vbs
timeout /t 10 /nobreak >nul
call :verify_trueface

echo   Starting Gate Counter...
wscript.exe run_gate_counter_hidden.vbs
timeout /t 10 /nobreak >nul

REM --- Verify ---
echo.
echo ========================================================
echo   Verifying running processes:
echo ========================================================
REM Name what is running: a python process can be a worker a real agent
REM spawned, so a bare count cannot tell "all three are up" from "one is
REM missing and something else is running twice".
powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe' or Name='py.exe'\" -ErrorAction SilentlyContinue | ForEach-Object { $agent = 'other'; if ($_.CommandLine -match 'trueface_poller') { $agent = 'TrueFace Poller' } elseif ($_.CommandLine -match 'gate_counter') { $agent = 'Gate Counter' } elseif ($_.CommandLine -match 'main.py') { $agent = 'Campus Agent' }; Write-Host ('    PID ' + $_.ProcessId + '  started ' + [Management.ManagementDateTimeConverter]::ToDateTime($_.CreationDate).ToString('HH:mm:ss') + '  ' + $agent) }"
echo.

call :count_python
set "COUNT=!PYCOUNT!"
echo   Python processes running: !COUNT!
REM A process older than this run is one the kill step missed: it is still
REM executing the code from before the pull, so say so instead of "success".
set "STALE=0"
for /f %%a in ('powershell.exe -NoProfile -Command "@(Get-Process python,py,pythonw -ErrorAction SilentlyContinue ^| Where-Object { $_.StartTime -lt (Get-Date).AddMinutes(-3) }).Count"') do set STALE=%%a
if !STALE! GTR 0 (
    echo   [WARNING] !STALE! process^(es^) survived the restart and still run the OLD code.
    powershell.exe -NoProfile -Command "Get-Process python,py,pythonw -ErrorAction SilentlyContinue | Where-Object { $_.StartTime -lt (Get-Date).AddMinutes(-3) } | ForEach-Object { Write-Host ('    stale PID ' + $_.Id + ' started ' + $_.StartTime) }"
    echo   Re-run this script as Administrator.
    goto verified
)
REM Judge by which agents are running, not by the total: an agent is free to
REM spawn helper processes of its own.
set "MISSING="
REM Only python processes count: every other process list includes this very
REM command line, whose own text names all three agents, so an unfiltered
REM search always "finds" them and can never report a missing agent.
for /f %%a in ('powershell.exe -NoProfile -Command "$c = (Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe' or Name='py.exe'\" -ErrorAction SilentlyContinue ^| Select-Object -ExpandProperty CommandLine) -join ' '; @('main.py','trueface_poller','gate_counter') ^| Where-Object { $c -notmatch $_ }"') do set "MISSING=!MISSING! %%a"
if "!MISSING!"=="" (
    echo   [OK] All 3 agents started successfully!
) else (
    echo   [WARNING] Not running:!MISSING!
    echo   Check the logs for the missing agent.
)
:verified
echo.
echo ========================================================
echo   Done! You can close this window now.
echo ========================================================
pause
exit /b 0

REM --- Count every Python process, including launcher-hosted ones ---
:count_python
set "PYCOUNT=0"
for /f %%a in ('powershell.exe -NoProfile -Command "@(Get-Process python,py,pythonw -ErrorAction SilentlyContinue).Count"') do set PYCOUNT=%%a
exit /b 0

REM --- Retry the TrueFace poller once, visibly, if it did not come up ---
:verify_trueface
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'trueface_poller' }; if ($p) { exit 0 } else { exit 1 }" >nul 2>&1
if !ERRORLEVEL! EQU 0 exit /b 0
echo   TrueFace poller did not start; retrying and showing the error...
if exist ".locks\trueface.lock" del ".locks\trueface.lock" >nul 2>&1
start "TrueFace Poller" /min cmd /c "run_trueface.bat"
timeout /t 15 /nobreak >nul
exit /b 0
