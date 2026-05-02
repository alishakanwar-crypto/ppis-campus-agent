@echo off
REM PPIS Campus Agent — Auto-restart wrapper
REM This script ensures the agent restarts automatically if it crashes.
REM Run this at Windows startup (via Task Scheduler) for 24/7 operation.
REM
REM Features:
REM   - Suppresses Windows error dialogs so crashes auto-recover silently
REM   - Pulls latest code before each restart
REM   - Restarts after 10 second cooldown

title PPIS Campus Agent (24/7)
cd /d "%~dp0"

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
