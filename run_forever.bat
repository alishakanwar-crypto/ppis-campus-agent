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

REM Suppress Windows Error Reporting dialogs
reg add "HKCU\Software\Microsoft\Windows\Windows Error Reporting" /v DontShowUI /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\Windows Error Reporting" /v Disabled /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\Software\Microsoft\Windows\Windows Error Reporting" /v DontShowUI /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\Software\Microsoft\Windows\Windows Error Reporting" /v Disabled /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\AeDebug" /v Auto /t REG_SZ /d 1 /f >nul 2>&1

set PYTHONDONTWRITEBYTECODE=1
REM Must match DUPLICATE_INSTANCE_EXIT_CODE in campus_instance.py.
set "DUPLICATE_EXIT_CODE=75"

:loop
echo ============================================
echo [%DATE% %TIME%] Pulling latest code...
echo ============================================
REM Force-sync to latest remote main branch
git fetch origin 2>nul
git checkout main 2>nul
git reset --hard origin/main 2>nul

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
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="%DUPLICATE_EXIT_CODE%" (
    echo [%DATE% %TIME%] Another campus agent owns the mutex; wrapper exiting cleanly.
    exit /b 0
)
echo.
echo [%DATE% %TIME%] Agent stopped (exit code: %EXIT_CODE%). Restarting in 10 seconds...
timeout /t 10 /nobreak
goto loop
