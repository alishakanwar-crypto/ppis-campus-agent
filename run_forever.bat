@echo off
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

:loop
echo.
echo ============================================
echo [%DATE% %TIME%] Pulling latest code...
echo ============================================
REM Stash any local changes first (prevents git pull from failing)
git stash 2>nul
git pull 2>nul

REM Clean up old snapshot files (older than 1 day) to prevent disk fill
echo [%DATE% %TIME%] Cleaning old snapshots...
forfiles /p "%~dp0snapshots" /d -1 /c "cmd /c del @path" 2>nul
forfiles /p "%~dp0attendance_snapshots" /d -1 /c "cmd /c del @path" 2>nul

echo.
echo [%DATE% %TIME%] Starting PPIS Campus Agent...
echo ============================================
py -3.12 main.py
echo.
echo [%DATE% %TIME%] Agent stopped (exit code: %ERRORLEVEL%). Restarting in 10 seconds...
timeout /t 10 /nobreak
goto loop
