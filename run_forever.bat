@echo off
REM PPIS Campus Agent — Auto-restart wrapper with PID lock
REM This script ensures the agent restarts automatically if it crashes.
REM Run this at Windows startup (via Task Scheduler) for 24/7 operation.
REM
REM Features:
REM   - PID lock prevents multiple instances from running simultaneously
REM   - Suppresses Windows error dialogs so crashes auto-recover silently
REM   - Pulls latest code before each restart
REM   - Restarts after 10 second cooldown
REM   - Kills stale instances before starting

title PPIS Campus Agent (24/7)
cd /d "%~dp0"

REM === PID Lock: prevent multiple instances ===
set LOCKFILE=%~dp0agent.lock

REM Check if another instance of this script is already running
if exist "%LOCKFILE%" (
    set /p OLD_PID=<"%LOCKFILE%"
    REM Check if the process is still running
    tasklist /FI "PID eq %OLD_PID%" 2>nul | find "%OLD_PID%" >nul
    if not errorlevel 1 (
        echo [%DATE% %TIME%] Another agent instance is already running (PID: %OLD_PID%). Exiting.
        exit /b 1
    ) else (
        echo [%DATE% %TIME%] Stale lock file found. Cleaning up...
        del "%LOCKFILE%" 2>nul
    )
)

REM Kill any stale python processes running main.py
for /f "tokens=2" %%a in ('tasklist /FI "IMAGENAME eq python.exe" /FO LIST 2^>nul ^| find "PID:"') do (
    wmic process where "ProcessId=%%a" get CommandLine 2>nul | find "main.py" >nul
    if not errorlevel 1 (
        echo [%DATE% %TIME%] Killing stale agent process: %%a
        taskkill /PID %%a /F >nul 2>&1
    )
)

REM Write our PID to lock file
echo %~dp0> "%LOCKFILE%"

REM Suppress Windows Error Reporting dialogs (registry-level)
reg add "HKCU\Software\Microsoft\Windows\Windows Error Reporting" /v DontShowUI /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\Windows Error Reporting" /v Disabled /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\Software\Microsoft\Windows\Windows Error Reporting" /v DontShowUI /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\Software\Microsoft\Windows\Windows Error Reporting" /v Disabled /t REG_DWORD /d 1 /f >nul 2>&1

REM Suppress JIT debugger and app-crash popups (IFEO)
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\AeDebug" /v Auto /t REG_SZ /d 1 /f >nul 2>&1

REM Reduce memory footprint: skip .pyc file generation
set PYTHONDONTWRITEBYTECODE=1

:loop
echo.
echo ============================================
echo [%DATE% %TIME%] Pulling latest code...
echo ============================================
git pull 2>nul
echo.
echo [%DATE% %TIME%] Starting PPIS Campus Agent...
echo ============================================
py -3.12 main.py
echo.
echo [%DATE% %TIME%] Agent stopped (exit code: %ERRORLEVEL%). Restarting in 10 seconds...
timeout /t 10 /nobreak
goto loop
