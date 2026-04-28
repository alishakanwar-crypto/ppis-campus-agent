@echo off
REM Install PPIS Campus Agent as a Windows Startup Task
REM Run this ONCE as Administrator to enable auto-start on boot.

echo Installing PPIS Campus Agent auto-start...

REM Create a scheduled task that runs at logon
schtasks /create /tn "PPIS Campus Agent" /tr "\"%~dp0run_forever.bat\"" /sc onlogon /rl highest /f

if %ERRORLEVEL% EQU 0 (
    echo.
    echo SUCCESS: PPIS Campus Agent will auto-start on Windows login.
    echo The agent will also auto-restart if it crashes.
    echo.
    echo To remove auto-start: schtasks /delete /tn "PPIS Campus Agent" /f
) else (
    echo.
    echo ERROR: Failed to create scheduled task.
    echo Please run this script as Administrator.
)

pause
