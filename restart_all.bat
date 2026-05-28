@echo off
setlocal enabledelayedexpansion
REM ================================================================
REM  PPIS Campus Agent — One-Click Restart
REM  Cleanly stops ALL running agents, pulls latest code, and
REM  restarts all 4 processes (Campus Agent + TrueFace + Gate Counter + Mood).
REM
REM  Usage: Right-click > Run as Administrator
REM         (Admin is needed to kill processes started by other windows)
REM ================================================================

cd /d "%~dp0"
echo.
echo ========================================================
echo   PPIS Campus Agent — Restarting All Processes
echo ========================================================
echo.

REM --- Step 1: Kill all hidden cmd.exe processes running our batch files ---
echo [Step 1/5] Killing hidden batch file processes...
powershell -Command "Get-WmiObject Win32_Process -Filter \"Name='cmd.exe'\" | Where-Object { $_.CommandLine -match 'run_forever|run_trueface|run_gate_counter|run_chairman_mood' } | ForEach-Object { Write-Host ('  Killed cmd.exe PID ' + $_.ProcessId); $_.Terminate() | Out-Null }" 2>nul
timeout /t 3 /nobreak >nul

REM --- Step 2: Kill all python.exe processes (loop until none remain) ---
echo [Step 2/5] Killing all Python processes...
set "RETRIES=0"
:kill_loop
tasklist /FI "IMAGENAME eq python.exe" 2>nul | findstr /I "python.exe" >nul
if !ERRORLEVEL! EQU 0 (
    taskkill /F /IM python.exe >nul 2>&1
    set /a RETRIES+=1
    if !RETRIES! GEQ 10 (
        echo   WARNING: Could not kill all Python processes after 10 attempts.
        echo   Please close Task Manager and try running as Administrator.
        goto start_agents
    )
    timeout /t 2 /nobreak >nul
    goto kill_loop
)
echo   All Python processes terminated.

REM --- Step 3: Pull latest code ---
echo [Step 3/5] Pulling latest code from GitHub...
git fetch origin 2>nul
git reset --hard origin/main 2>nul
echo   Code updated.

REM --- Step 4: Clean up lock file ---
echo [Step 4/5] Cleaning up...
if exist ".agent_lock" del ".agent_lock" >nul 2>&1
if exist "__pycache__" rmdir /s /q "__pycache__" 2>nul

:start_agents
REM --- Step 5: Start all 4 agents with delays ---
echo [Step 5/5] Starting agents...
echo.

echo   Starting Campus Agent...
wscript.exe run_hidden.vbs
timeout /t 15 /nobreak >nul

echo   Starting TrueFace Poller...
wscript.exe run_trueface_hidden.vbs
timeout /t 15 /nobreak >nul

echo   Starting Gate Counter...
wscript.exe run_gate_counter_hidden.vbs
timeout /t 15 /nobreak >nul

echo   Starting Mood Monitor...
wscript.exe run_chairman_mood_hidden.vbs
timeout /t 15 /nobreak >nul

REM --- Verify ---
echo.
echo ========================================================
echo   Verifying running processes:
echo ========================================================
tasklist /FI "IMAGENAME eq python.exe"
echo.

REM Count python processes
set "COUNT=0"
for /f %%a in ('tasklist /FI "IMAGENAME eq python.exe" ^| findstr /I "python.exe" ^| find /c /v ""') do set COUNT=%%a
echo   Python processes running: !COUNT!
if !COUNT! EQU 4 (
    echo   [OK] All 4 agents started successfully!
) else (
    echo   [WARNING] Expected 4 processes but found !COUNT!.
    echo   Check Task Manager for details.
)
echo.
echo ========================================================
echo   Done! You can close this window now.
echo ========================================================
pause
