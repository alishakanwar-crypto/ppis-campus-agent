@echo off
setlocal enabledelayedexpansion
REM PPIS Campus Agent — Auto-restart wrapper
REM Features:
REM   - Auto-restarts if the agent crashes
REM   - Pulls latest code before each restart (stashes local changes first)
REM   - Suppresses Windows error dialogs
REM   - 10 second cooldown between restarts
REM   - Cleans up old snapshot files to prevent disk fill

title PPIS Campus Agent (24/7)
cd /d "%~dp0"

REM Prevent multiple instances — check if port 8897 is already in use
set "LOCKFILE=%~dp0.agent_lock"
if exist "%LOCKFILE%" (
    REM Lock file exists — check if campus agent is actually listening on port 8897
    netstat -ano 2>nul | findstr ":8897" | findstr "LISTENING" >nul
    if !ERRORLEVEL! EQU 0 (
        echo Another campus agent is already running on port 8897! Exiting.
        exit /b 1
    )
    REM Port not in use — stale lock file from previous session
    del "%LOCKFILE%" >nul 2>&1
)
echo %DATE% %TIME% > "%LOCKFILE%"

REM Suppress Windows Error Reporting dialogs
reg add "HKCU\Software\Microsoft\Windows\Windows Error Reporting" /v DontShowUI /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\Windows Error Reporting" /v Disabled /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\Software\Microsoft\Windows\Windows Error Reporting" /v DontShowUI /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\Software\Microsoft\Windows\Windows Error Reporting" /v Disabled /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\AeDebug" /v Auto /t REG_SZ /d 1 /f >nul 2>&1

set PYTHONDONTWRITEBYTECODE=1

:loop
echo.
echo ============================================
echo [%DATE% %TIME%] Killing any existing agent on port 8897...
echo ============================================
REM Kill any process holding port 8897 (main.py also does this early)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8897 ^| findstr LISTENING') do (
    echo Killing PID %%a on port 8897...
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo ============================================
echo [%DATE% %TIME%] Pulling latest code...
echo ============================================
REM Force-sync to latest remote code (nuclear but reliable)
git fetch origin 2>nul
for /f "tokens=*" %%b in ('git symbolic-ref refs/remotes/origin/HEAD 2^>nul') do set "DEFAULT_BRANCH=%%b"
if defined DEFAULT_BRANCH (
    set "DEFAULT_BRANCH=!DEFAULT_BRANCH:refs/remotes/origin/=!"
    git reset --hard "origin/!DEFAULT_BRANCH!" 2>nul
) else (
    git reset --hard origin/main 2>nul
)

REM Clear bytecode cache to avoid stale .pyc files after code updates
if exist "%~dp0__pycache__" rmdir /s /q "%~dp0__pycache__" 2>nul

REM Clean up old snapshot files (older than 1 day) to prevent disk fill
echo [%DATE% %TIME%] Cleaning old snapshots...
forfiles /p "%~dp0snapshots" /d -1 /m *.* /c "cmd /c del /Q @path" 2>nul
forfiles /p "%~dp0attendance_snapshots" /d -1 /m *.* /c "cmd /c del /Q @path" 2>nul

echo.
echo [%DATE% %TIME%] Starting PPIS Campus Agent...
echo ============================================
py -3.12 -B main.py
echo.
echo [%DATE% %TIME%] Agent stopped (exit code: %ERRORLEVEL%). Restarting in 10 seconds...
timeout /t 10 /nobreak
goto loop

:cleanup
REM Clean up lock file on exit
if exist "%LOCKFILE%" del "%LOCKFILE%" 2>nul
