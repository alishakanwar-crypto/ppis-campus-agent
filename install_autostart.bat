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
echo       ^<Arguments^>"!AGENT_DIR!run_watchdog_hidden.vbs"^</Arguments^>
echo       ^<WorkingDirectory^>!AGENT_DIR!^</WorkingDirectory^>
echo     ^</Exec^>
echo   ^</Actions^>
echo ^</Task^>
) > "%XMLFILE%"

schtasks /create /tn "PPIS Campus Agent" /xml "%XMLFILE%" /f >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    echo       Boot/Logon task created - XML method
    set TASK_OK=1
) else (
    echo       XML method failed, trying simple command...
    schtasks /create /tn "PPIS Campus Agent" /tr "wscript.exe \"%AGENT_DIR%run_watchdog_hidden.vbs\"" /sc onlogon /rl highest /f >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        echo       Logon task created - simple method
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
    echo       Watchdog task created - XML method
    set WATCHDOG_OK=1
) else (
    echo       XML method failed, trying simple command...
    schtasks /create /tn "PPIS Campus Agent Watchdog" /tr "wscript.exe \"%AGENT_DIR%run_watchdog_hidden.vbs\"" /sc minute /mo 5 /f >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        echo       Watchdog task created - simple method
        set WATCHDOG_OK=1
    ) else (
        echo       WARNING: Could not create watchdog task
    )
)
del "%XMLFILE2%" >nul 2>&1

REM ── Task 3: SYSTEM watchdog for the campus agent alone ────────
REM The watchdog above runs on an interactive token, so it does nothing while
REM the PC sits at the login screen — a night-time reboot or crash then left
REM the campus with no agent until somebody logged on and restarted by hand.
REM This copy runs as SYSTEM, needs no logon, and starts only the campus
REM agent, because TrueFace needs a Chrome window and the gate counter needs
REM native CP Plus, neither of which works outside a logged-on desktop.

echo [4/7] Creating logon-free watchdog for parent photos...
schtasks /delete /tn "PPIS Campus Agent Watchdog (System)" /f >nul 2>&1
schtasks /create /tn "PPIS Campus Agent Watchdog (System)" /tr "wscript.exe \"%AGENT_DIR%run_watchdog_agent_only_hidden.vbs\"" /sc minute /mo 5 /ru SYSTEM /rl highest /f >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    echo       Logon-free watchdog created - runs as SYSTEM every 5 minutes
) else (
    echo       WARNING: Could not create the logon-free watchdog
)

REM ── Remove the legacy startup-folder launcher ──────────────────

echo [5/7] Creating nightly refresh task (03:00 IST)...
schtasks /delete /tn "PPIS Nightly Restart" /f >nul 2>&1
REM Runs as SYSTEM so the refresh happens whether or not anyone is logged on.
schtasks /create /tn "PPIS Nightly Restart" /tr "cmd.exe /c \"%AGENT_DIR%nightly_restart.bat\"" /sc daily /st 03:00 /ru SYSTEM /rl highest /f >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    echo       Nightly refresh task created
) else (
    echo       WARNING: Could not create nightly refresh task
)

echo       Removing duplicate launchers...
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
if exist "%STARTUP_DIR%\PPIS Agent.vbs" del "%STARTUP_DIR%\PPIS Agent.vbs" >nul 2>&1
REM Superseded by "PPIS Campus Agent" + watchdog; leaving them starts duplicates
schtasks /delete /tn "PPIS Agent Autostart" /f >nul 2>&1
schtasks /delete /tn "PPIS TrueFace Poller" /f >nul 2>&1
echo       Legacy launchers removed

REM ── Verify tasks ───────────────────────────────────────────────

echo [6/7] Verifying installation...
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

schtasks /query /tn "PPIS Campus Agent Watchdog (System)" >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    echo       [OK] Logon-free watchdog: INSTALLED
) else (
    echo       [!!] Logon-free watchdog: NOT INSTALLED
)

echo       [OK] Startup folder:  DISABLED (single scheduled launcher)

REM Print the real triggers so a botched task definition is visible here.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "foreach ($t in 'PPIS Campus Agent','PPIS Campus Agent Watchdog') { $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue; if ($task) { foreach ($trig in $task.Triggers) { Write-Host ('      ' + $t + ' trigger: ' + $trig.CimClass.CimClassName) } } }" 2>nul

REM ── Start the agent NOW ───────────────────────────────────────

echo.
echo [7/7] Starting the agents now...
REM The watchdog starts every missing process, not just the campus agent, so
REM boot/logon brings up TrueFace and the gate counter too.
start "" wscript.exe "%AGENT_DIR%run_watchdog_hidden.vbs"

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
echo              schtasks /delete /tn "PPIS Campus Agent Watchdog (System)" /f
echo              del "%STARTUP_DIR%\PPIS Agent.vbs" (already removed)
echo.

pause
endlocal
