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
REM Every git command used to be silenced and the script said "Code updated"
REM regardless, so a broken repository on the PC left the agents on old code
REM for days while the window reported success. Now git's own errors show,
REM a damaged .git is replaced with a fresh clone, and the commit the agents
REM will actually run is printed.
echo [Step 3/5] Pulling latest code from GitHub...
echo   Using branch: main
call :git_update
if !GIT_OK! EQU 0 (
    echo   Git failed above - repairing the repository with a fresh clone...
    call :git_repair
    call :git_update
)
if !GIT_OK! EQU 1 (
    for /f "tokens=*" %%c in ('git log -1 --format^="%%h %%s" 2^>nul') do echo   Code updated: %%c
) else (
    echo   ************************************************************
    echo   CODE NOT UPDATED - the agents below will run the OLD code.
    echo   Send this window to Devin; the agents still start normally.
    echo   ************************************************************
)

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

REM Each agent is waited for by name, not by a fixed sleep: TrueFace opens
REM Chrome and can take longer than any delay we would dare put here, which
REM is how a restart printed "all 3 started" with no poller running at all.
echo   Starting Campus Agent...
wscript.exe run_hidden.vbs
call :wait_for "main.py" "Campus Agent" 20

echo   Starting TrueFace Poller...
wscript.exe run_trueface_hidden.vbs
call :wait_for "trueface_poller" "TrueFace Poller" 20
if !WAITED! NEQ 0 call :retry_trueface

echo   Starting Gate Counter...
wscript.exe run_gate_counter_hidden.vbs
call :wait_for "gate_counter" "Gate Counter" 20

REM --- Verify ---
echo.
echo ========================================================
echo   Verifying running processes:
echo ========================================================
REM Name what is running: a python process can be a worker a real agent
REM spawned, so a bare count cannot tell "all three are up" from "one is
REM missing and something else is running twice".
powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe' or Name='py.exe'\" -ErrorAction SilentlyContinue | ForEach-Object { $agent = 'other'; if ($_.CommandLine -match 'trueface_poller') { $agent = 'TrueFace Poller' } elseif ($_.CommandLine -match 'gate_counter') { $agent = 'Gate Counter' } elseif ($_.CommandLine -match 'main.py') { $agent = 'Campus Agent' }; Write-Host ('    PID ' + $_.ProcessId + '  started ' + $_.CreationDate.ToString('HH:mm:ss') + '  ' + $agent) }" 2>nul
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
set "RETRIED=0"
:check_agents
set "MISSING="
REM Only python processes count: every other process list includes this very
REM command line, whose own text names all three agents, so an unfiltered
REM search always "finds" them and can never report a missing agent.
REM Accept only the three known names: any PowerShell error text would
REM otherwise be printed back as the list of missing agents.
for /f %%a in ('powershell.exe -NoProfile -Command "$c = (Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe' or Name='py.exe'\" -ErrorAction SilentlyContinue ^| Select-Object -ExpandProperty CommandLine) -join ' '; @('main.py','trueface_poller','gate_counter') ^| Where-Object { $c -notmatch $_ }" 2^>nul') do (
    if "%%a"=="main.py" set "MISSING=!MISSING! Campus Agent"
    if "%%a"=="trueface_poller" set "MISSING=!MISSING! TrueFace Poller"
    if "%%a"=="gate_counter" set "MISSING=!MISSING! Gate Counter"
)
if "!MISSING!"=="" (
    echo   [OK] All 3 agents started successfully!
    goto verified
)
REM Start whichever agent is missing once more and look again: only then may
REM this script say anything about what is running.
if "!RETRIED!"=="0" (
    set "RETRIED=1"
    echo   Not running:!MISSING! - starting again...
    echo !MISSING! | find "Campus Agent" >nul && wscript.exe run_hidden.vbs
    echo !MISSING! | find "TrueFace Poller" >nul && call :retry_trueface
    echo !MISSING! | find "Gate Counter" >nul && wscript.exe run_gate_counter_hidden.vbs
    timeout /t 20 /nobreak >nul
    goto check_agents
)
echo   [WARNING] Not running:!MISSING!
echo   Check the logs for the missing agent.
:verified
echo.
echo ========================================================
echo   Done! You can close this window now.
echo ========================================================
pause
exit /b 0

REM --- Fetch and hard-reset to origin/main, reporting git's own errors ---
:git_update
set "GIT_OK=1"
REM A repository cloned by another administrator account counts as
REM dubious ownership here and every git command refuses it, which no
REM fresh clone can repair, so trust this folder before giving up on it.
git config --global --add safe.directory "%CD%" >nul 2>&1
git fetch origin main || set "GIT_OK=0"
if !GIT_OK! EQU 1 git checkout main || set "GIT_OK=0"
if !GIT_OK! EQU 1 git reset --hard origin/main || set "GIT_OK=0"
exit /b 0

REM --- Replace a damaged .git with a fresh clone's, keeping local files ---
REM Only .git is swapped, so config.json, the databases, the logs and the
REM chrome profiles stay exactly where they are; the old .git is kept as
REM .git.broken in case it is ever needed.
:git_repair
set "FRESH=%TEMP%\ppis_campus_agent_fresh"
if exist "!FRESH!" rmdir /s /q "!FRESH!" 2>nul
REM A leftover directory here would be taken for this run's clone and could
REM reset the agents to whatever repository it came from.
if exist "!FRESH!" (
    echo   Could not clear !FRESH! - repair skipped.
    exit /b 0
)
REM Clone the URL this PC is already authenticated for, if it can still be
REM read, so a private repository does not start asking for a login.
set "ORIGIN_URL=https://github.com/alishakanwar-crypto/ppis-campus-agent.git"
for /f "tokens=*" %%u in ('git remote get-url origin 2^>nul') do set "ORIGIN_URL=%%u"
git clone --no-checkout "!ORIGIN_URL!" "!FRESH!"
if not exist "!FRESH!\.git" (
    echo   Fresh clone failed - check this PC's internet access or GitHub login.
    exit /b 0
)
REM Park the old repository under its own name: a second repair must not
REM throw away the first backup, which may hold refs kept nowhere else.
set "BROKEN=.git.broken"
if exist "!BROKEN!" set "BROKEN=.git.broken.%RANDOM%"
if exist ".git" move /y ".git" "!BROKEN!" >nul 2>&1
move /y "!FRESH!\.git" ".git" >nul 2>&1
rmdir /s /q "!FRESH!" 2>nul
if exist ".git" (
    echo   Repository replaced with a fresh clone; the old one is !BROKEN!.
) else (
    echo   Could not install the fresh clone; restoring the old repository.
    if exist "!BROKEN!" move /y "!BROKEN!" ".git" >nul 2>&1
)
exit /b 0

REM --- Count every Python process, including launcher-hosted ones ---
:count_python
set "PYCOUNT=0"
for /f %%a in ('powershell.exe -NoProfile -Command "@(Get-Process python,py,pythonw -ErrorAction SilentlyContinue).Count"') do set PYCOUNT=%%a
exit /b 0

REM --- Wait until a named agent is actually running ---
REM %1 command-line text, %2 name to print, %3 seconds to wait.
:wait_for
set "WAITED=1"
set "WAIT_LEFT=%~3"
:wait_loop
powershell.exe -NoProfile -Command "$c = (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe' -or $_.Name -eq 'py.exe' } | Select-Object -ExpandProperty CommandLine) -join ' '; if ($c -match [regex]::Escape('%~1')) { exit 0 } else { exit 1 }" >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    set "WAITED=0"
    echo     %~2 is running.
    exit /b 0
)
set /a WAIT_LEFT-=2
if !WAIT_LEFT! LEQ 0 (
    echo     %~2 has not come up yet.
    exit /b 0
)
timeout /t 2 /nobreak >nul
goto wait_loop

REM --- Start the TrueFace poller again, visibly, and free its lock first ---
:retry_trueface
echo   TrueFace poller did not start; retrying and showing the error...
if exist ".locks\trueface.lock" del ".locks\trueface.lock" >nul 2>&1
start "TrueFace Poller" /min cmd /c "run_trueface.bat"
call :wait_for "trueface_poller" "TrueFace Poller" 30
exit /b 0
