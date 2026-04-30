@echo off
REM ============================================================
REM PPIS Campus Agent — Windows Service Installer
REM Run this ONCE as Administrator to set up background service.
REM ============================================================
REM
REM This creates a Windows Scheduled Task that:
REM   1. Starts the agent at system startup (before any user logs in)
REM   2. Runs in the background (no visible window)
REM   3. Auto-restarts if the agent crashes
REM   4. Pulls latest code on each restart
REM
REM The agent will survive:
REM   - Closing all Command Prompt windows
REM   - Logging out of Windows
REM   - PC restarts and power cuts
REM ============================================================

echo.
echo ============================================================
echo   PPIS Campus Agent — Background Service Installer
echo ============================================================
echo.

REM Remove old task if it exists
schtasks /delete /tn "PPIS Campus Agent" /f >nul 2>&1

REM Create scheduled task that runs at system startup using the hidden VBS wrapper
REM /sc onstart = runs at system boot (not just logon)
REM /rl highest = runs with admin privileges
REM /delay 0000:30 = 30 second delay after boot for network to be ready
schtasks /create /tn "PPIS Campus Agent" /tr "wscript.exe \"%~dp0run_hidden.vbs\"" /sc onstart /rl highest /delay 0000:30 /f

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo NOTE: /sc onstart requires SYSTEM or admin password.
    echo Falling back to /sc onlogon (starts when you log in)...
    echo.
    schtasks /create /tn "PPIS Campus Agent" /tr "wscript.exe \"%~dp0run_hidden.vbs\"" /sc onlogon /rl highest /f
)

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================================
    echo   SUCCESS! PPIS Campus Agent installed as background service.
    echo ============================================================
    echo.
    echo   The agent will now:
    echo     - Run in the background (no window needed)
    echo     - Start automatically on PC boot/login
    echo     - Auto-restart if it crashes
    echo     - Pull latest code on each restart
    echo.
    echo   To START now:  wscript.exe "%~dp0run_hidden.vbs"
    echo   To STOP:       taskkill /F /IM python.exe
    echo   To UNINSTALL:  schtasks /delete /tn "PPIS Campus Agent" /f
    echo.
) else (
    echo.
    echo ERROR: Failed to create scheduled task.
    echo Please run this script as Administrator.
    echo.
)

pause
