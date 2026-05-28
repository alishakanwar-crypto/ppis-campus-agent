@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM PPIS Campus Agent — Windows Auto-Start Installer
REM Run this ONCE as Administrator to set up background service.
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

set AGENT_DIR=%~dp0
set TASK_OK=0
set WATCHDOG_OK=0

REM Kill any existing agent processes
echo [1/6] Stopping any running agent instances...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq PPIS*" >nul 2>&1

REM ── Task 1: Main startup task via XML ──────────────────────────

echo [2/6] Creating startup task...
schtasks /delete /tn "PPIS Campus Agent" /f >nul 2>&1

REM Generate XML task definition (most reliable method)
set XMLFILE=%TEMP%\ppis_agent_task.xml
(
echo ^<?xml version="1.0" encoding="UTF-16"?^>
echo ^<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"^>
echo   ^<Triggers^>
echo     ^<LogonTrigger^>
echo       ^<Enabled^>true^</Enabled^>
echo       ^<Delay^>PT30S^</Delay^>
echo     ^</LogonTrigger^>
echo     ^<BootTrigger^>
echo       ^<Enabled^>true^</Enabled^>
echo       ^<Delay^>PT60S^</Delay^>
echo     ^</BootTrigger^>
echo   ^</Triggers^>
echo   ^<Principals^>
echo     ^<Principal id="Author"^>
echo       ^<LogonType^>InteractiveToken^</LogonType^>
echo       ^<RunLevel^>HighestAvailable^</RunLevel^>
echo     ^</Principal^>
echo   ^</Principals^>
echo   ^<Settings^>
echo     ^<MultipleInstancesPolicy^>IgnoreNew^</MultipleInstancesPolicy^>
echo     ^<DisallowStartIfOnBatteries^>false^</DisallowStartIfOnBatteries^>
echo     ^<StopIfGoingOnBatteries^>false^</StopIfGoingOnBatteries^>
echo     ^<AllowHardTerminate^>true^</AllowHardTerminate^>
echo     ^<StartWhenAvailable^>true^</StartWhenAvailable^>
echo     ^<RunOnlyIfNetworkAvailable^>false^</RunOnlyIfNetworkAvailable^>
echo     ^<AllowStartOnDemand^>true^</AllowStartOnDemand^>
echo     ^<Enabled^>true^</Enabled^>
echo     ^<Hidden^>false^</Hidden^>
echo     ^<ExecutionTimeLimit^>PT0S^</ExecutionTimeLimit^>
echo     ^<Priority^>7^</Priority^>
echo     ^<RestartOnFailure^>
echo       ^<Interval^>PT1M^</Interval^>
echo       ^<Count^>999^</Count^>
echo     ^</RestartOnFailure^>
echo   ^</Settings^>
echo   ^<Actions Context="Author"^>
echo     ^<Exec^>
echo       ^<Command^>wscript.exe^</Command^>
echo       ^<Arguments^>"!AGENT_DIR!run_hidden.vbs"^</Arguments^>
echo       ^<WorkingDirectory^>!AGENT_DIR!^</WorkingDirectory^>
echo     ^</Exec^>
echo   ^</Actions^>
echo ^</Task^>
) > "%XMLFILE%"

schtasks /create /tn "PPIS Campus Agent" /xml "%XMLFILE%" /f >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    echo       Boot/Logon task created (XML method)
    set TASK_OK=1
) else (
    echo       XML method failed, trying simple command...
    schtasks /create /tn "PPIS Campus Agent" /tr "wscript.exe \"%AGENT_DIR%run_hidden.vbs\"" /sc onlogon /rl highest /f >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        echo       Logon task created (simple method)
        set TASK_OK=1
    ) else (
        echo       WARNING: Could not create scheduled task
    )
)
del "%XMLFILE%" >nul 2>&1

REM ── Task 2: Watchdog (every 5 minutes) via XML ────────────────

echo [3/6] Creating watchdog task (every 5 minutes)...
schtasks /delete /tn "PPIS Campus Agent Watchdog" /f >nul 2>&1

set XMLFILE2=%TEMP%\ppis_watchdog_task.xml
(
echo ^<?xml version="1.0" encoding="UTF-16"?^>
echo ^<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"^>
echo   ^<Triggers^>
echo     ^<TimeTrigger^>
echo       ^<Repetition^>
echo         ^<Interval^>PT5M^</Interval^>
echo         ^<StopAtDurationEnd^>false^</StopAtDurationEnd^>
echo       ^</Repetition^>
echo       ^<StartBoundary^>2026-01-01T00:00:00^</StartBoundary^>
echo       ^<Enabled^>true^</Enabled^>
echo     ^</TimeTrigger^>
echo   ^</Triggers^>
echo   ^<Principals^>
echo     ^<Principal id="Author"^>
echo       ^<LogonType^>InteractiveToken^</LogonType^>
echo       ^<RunLevel^>HighestAvailable^</RunLevel^>
echo     ^</Principal^>
echo   ^</Principals^>
echo   ^<Settings^>
echo     ^<MultipleInstancesPolicy^>IgnoreNew^</MultipleInstancesPolicy^>
echo     ^<DisallowStartIfOnBatteries^>false^</DisallowStartIfOnBatteries^>
echo     ^<StopIfGoingOnBatteries^>false^</StopIfGoingOnBatteries^>
echo     ^<AllowHardTerminate^>true^</AllowHardTerminate^>
echo     ^<StartWhenAvailable^>true^</StartWhenAvailable^>
echo     ^<AllowStartOnDemand^>true^</AllowStartOnDemand^>
echo     ^<Enabled^>true^</Enabled^>
echo     ^<Hidden^>true^</Hidden^>
echo     ^<ExecutionTimeLimit^>PT2M^</ExecutionTimeLimit^>
echo   ^</Settings^>
echo   ^<Actions Context="Author"^>
echo     ^<Exec^>
echo       ^<Command^>wscript.exe^</Command^>
echo       ^<Arguments^>"!AGENT_DIR!run_watchdog_hidden.vbs"^</Arguments^>
echo       ^<WorkingDirectory^>!AGENT_DIR!^</WorkingDirectory^>
echo     ^</Exec^>
echo   ^</Actions^>
echo ^</Task^>
) > "%XMLFILE2%"

schtasks /create /tn "PPIS Campus Agent Watchdog" /xml "%XMLFILE2%" /f >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    echo       Watchdog task created (XML method)
    set WATCHDOG_OK=1
) else (
    echo       XML method failed, trying simple command...
    schtasks /create /tn "PPIS Campus Agent Watchdog" /tr "wscript.exe \"%AGENT_DIR%run_watchdog_hidden.vbs\"" /sc minute /mo 5 /f >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        echo       Watchdog task created (simple method)
        set WATCHDOG_OK=1
    ) else (
        echo       WARNING: Could not create watchdog task
    )
)
del "%XMLFILE2%" >nul 2>&1

REM ── Startup folder shortcut (always works) ─────────────────────

echo [4/6] Adding startup folder shortcut...
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup

> "%STARTUP_DIR%\PPIS Agent.vbs" (
    echo Set WshShell = CreateObject^("WScript.Shell"^)
    echo WshShell.CurrentDirectory = "%AGENT_DIR%"
    echo WshShell.Run """" ^& "%AGENT_DIR%run_forever.bat" ^& """", 0, False
)
echo       Startup folder shortcut created

REM ── Verify tasks ───────────────────────────────────────────────

echo [5/6] Verifying installation...
echo.

schtasks /query /tn "PPIS Campus Agent" >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    echo       [OK] Boot/Logon task: INSTALLED
) else (
    echo       [!!] Boot/Logon task: NOT INSTALLED
)

schtasks /query /tn "PPIS Campus Agent Watchdog" >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    echo       [OK] Watchdog task:   INSTALLED
) else (
    echo       [!!] Watchdog task:   NOT INSTALLED
)

if exist "%STARTUP_DIR%\PPIS Agent.vbs" (
    echo       [OK] Startup folder:  INSTALLED
) else (
    echo       [!!] Startup folder:  NOT INSTALLED
)

REM ── Start the agent NOW ───────────────────────────────────────

echo.
echo [6/6] Starting the agent now...
start "" wscript.exe "%AGENT_DIR%run_hidden.vbs"

echo.
echo ============================================================
echo   INSTALLATION COMPLETE
echo ============================================================
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
endlocal
