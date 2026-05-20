@echo off
REM ============================================================
REM PPIS Campus Agent — Windows Auto-Start Installer
REM Run this ONCE as Administrator to set up background service.
REM ============================================================
REM
REM This creates TWO Windows Scheduled Tasks:
REM   1. "PPIS Campus Agent" — starts on boot/logon
REM   2. "PPIS Campus Agent Watchdog" — runs every 5 minutes to
REM      verify the agent is alive and restarts it if not
REM
REM The agent will survive:
REM   - Closing all windows
REM   - Logging out of Windows
REM   - PC restarts and power cuts
REM   - Agent crashes (auto-restart in 10 seconds via run_forever)
REM   - run_forever.bat itself dying (watchdog restarts within 5 min)
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
echo [1/5] Stopping any running agent instances...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq PPIS*" >nul 2>&1

REM ── Task 1: Main startup task ──────────────────────────────────

REM Remove old task if it exists
schtasks /delete /tn "PPIS Campus Agent" /f >nul 2>&1

REM Try SYSTEM-level onstart first (survives logoff)
echo [2/5] Creating startup task (boot trigger)...
schtasks /create /tn "PPIS Campus Agent" /tr "wscript.exe \"%~dp0run_hidden.vbs\"" /sc onstart /rl highest /delay 0000:30 /ru SYSTEM /f 2>nul

if %ERRORLEVEL% NEQ 0 (
    echo       SYSTEM task failed, trying current user with logon trigger...
    schtasks /create /tn "PPIS Campus Agent" /tr "wscript.exe \"%~dp0run_hidden.vbs\"" /sc onlogon /rl highest /f
)

REM ── Task 2: Watchdog (every 5 minutes) ────────────────────────

schtasks /delete /tn "PPIS Campus Agent Watchdog" /f >nul 2>&1

echo [3/5] Creating watchdog task (every 5 minutes)...
schtasks /create /tn "PPIS Campus Agent Watchdog" /tr "cmd.exe /c \"%~dp0watchdog.bat\"" /sc minute /mo 5 /rl highest /ru SYSTEM /f 2>nul

if %ERRORLEVEL% NEQ 0 (
    echo       SYSTEM watchdog failed, trying current user...
    schtasks /create /tn "PPIS Campus Agent Watchdog" /tr "cmd.exe /c \"%~dp0watchdog.bat\"" /sc minute /mo 5 /rl highest /f
)

REM ── Startup folder shortcut (belt-and-suspenders) ─────────────

echo [4/5] Adding startup folder shortcut as backup...
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup

REM Create a VBS shortcut in Startup folder as a third fallback
echo Set WshShell = CreateObject("WScript.Shell") > "%STARTUP_DIR%\PPIS Agent.vbs"
echo scriptDir = "%~dp0" >> "%STARTUP_DIR%\PPIS Agent.vbs"
echo WshShell.CurrentDirectory = scriptDir >> "%STARTUP_DIR%\PPIS Agent.vbs"
echo WshShell.Run """" ^& scriptDir ^& "run_forever.bat""", 0, False >> "%STARTUP_DIR%\PPIS Agent.vbs"

REM ── Start the agent NOW ───────────────────────────────────────

echo [5/5] Starting the agent now...
start "" /B wscript.exe "%~dp0run_hidden.vbs"

echo.
echo ============================================================
echo   SUCCESS! PPIS Campus Agent fully installed.
echo ============================================================
echo.
echo   THREE layers of protection installed:
echo.
echo   1. BOOT TRIGGER  — Agent starts when PC turns on
echo   2. WATCHDOG       — Checks every 5 min, restarts if dead
echo   3. STARTUP FOLDER — Backup trigger on user login
echo.
echo   The agent is now RUNNING in the background.
echo.
echo   Status:    service_status.bat
echo   Stop:      taskkill /F /IM python.exe
echo   Uninstall: schtasks /delete /tn "PPIS Campus Agent" /f
echo              schtasks /delete /tn "PPIS Campus Agent Watchdog" /f
echo              del "%STARTUP_DIR%\PPIS Agent.vbs"
echo.

pause
