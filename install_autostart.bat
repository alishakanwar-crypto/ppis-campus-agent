@echo off
REM ============================================================
REM PPIS Campus Agent — Windows Auto-Start Installer
REM Run this ONCE as Administrator to set up background service.
REM ============================================================
REM
REM This creates a Windows Scheduled Task that:
REM   1. Starts the agent at system startup (before any user logs in)
REM   2. Runs in the background (no visible window)
REM   3. Auto-restarts if the agent crashes (via run_forever.bat)
REM   4. Pulls latest code on each restart
REM   5. Cleans up old snapshot files automatically
REM
REM The agent will survive:
REM   - Closing all Command Prompt windows
REM   - Logging out of Windows
REM   - PC restarts and power cuts
REM   - Agent crashes (auto-restart in 10 seconds)
REM ============================================================

echo.
echo ============================================================
echo   PPIS Campus Agent — Background Service Installer
echo ============================================================
echo.

REM Check for admin privileges
net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: This script must be run as Administrator!
    echo Right-click and select "Run as administrator"
    echo.
    pause
    exit /b 1
)

REM Kill any existing agent processes
echo Stopping any running agent instances...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq PPIS*" >nul 2>&1

REM Remove old task if it exists
schtasks /delete /tn "PPIS Campus Agent" /f >nul 2>&1

REM Create scheduled task that runs at system startup
REM /sc onstart = runs at system boot (not just logon)
REM /rl highest = runs with admin privileges
REM /delay 0000:30 = 30 second delay after boot for network to be ready
echo Creating startup task...
schtasks /create /tn "PPIS Campus Agent" /tr "wscript.exe \"%~dp0run_hidden.vbs\"" /sc onstart /rl highest /delay 0000:30 /ru SYSTEM /f 2>nul

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo NOTE: SYSTEM user task failed, trying with current user...
    schtasks /create /tn "PPIS Campus Agent" /tr "wscript.exe \"%~dp0run_hidden.vbs\"" /sc onlogon /rl highest /f
)

REM Also add to Windows Startup folder as backup
echo Adding to Startup folder as backup...
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
echo Set WshShell = CreateObject("WScript.Shell") > "%STARTUP_DIR%\PPIS Agent.vbs"
echo scriptDir = "%~dp0" >> "%STARTUP_DIR%\PPIS Agent.vbs"
echo WshShell.CurrentDirectory = scriptDir >> "%STARTUP_DIR%\PPIS Agent.vbs"
echo WshShell.Run """" ^& scriptDir ^& "run_forever.bat""", 0, False >> "%STARTUP_DIR%\PPIS Agent.vbs"

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================================
    echo   SUCCESS! PPIS Campus Agent installed.
    echo ============================================================
    echo.
    echo   The agent will now:
    echo     - Start automatically on PC boot (no manual action needed)
    echo     - Run in the background (no window)
    echo     - Auto-restart if it crashes
    echo     - Pull latest code on each restart
    echo     - Clean up old files automatically
    echo.
    echo   Starting the agent now...
    echo.

    REM Start immediately
    wscript.exe "%~dp0run_hidden.vbs"

    echo   Agent is running in background!
    echo.
    echo   To CHECK status: Open browser to http://localhost:8897
    echo   To STOP:         taskkill /F /IM python.exe
    echo   To UNINSTALL:    schtasks /delete /tn "PPIS Campus Agent" /f
    echo.
) else (
    echo.
    echo ERROR: Failed to create scheduled task.
    echo Please run this script as Administrator.
    echo.
)

pause
