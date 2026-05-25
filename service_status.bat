@echo off
REM Check if PPIS Campus Agent is running and show full status
echo.
echo ============================================================
echo   PPIS Campus Agent — Status Check
echo ============================================================
echo.

REM Check if agent process is running
tasklist /fi "imagename eq python.exe" 2>nul | find /i "python.exe" >nul
if %ERRORLEVEL% EQU 0 (
    echo   AGENT STATUS:   RUNNING
) else (
    echo   AGENT STATUS:   STOPPED
)

echo.
echo   --- Scheduled Tasks ---
echo.

REM Check main startup task
echo   Boot Task:
schtasks /query /tn "PPIS Campus Agent" /fo LIST 2>nul | findstr /i "Status" || echo     (Not installed)

echo.

REM Check watchdog task
echo   Watchdog Task (every 5 min):
schtasks /query /tn "PPIS Campus Agent Watchdog" /fo LIST 2>nul | findstr /i "Status" || echo     (Not installed)

echo.
echo   --- Watchdog Log (last 10 entries) ---
echo.
if exist "%~dp0watchdog.log" (
    powershell -Command "Get-Content '%~dp0watchdog.log' | Select-Object -Last 10"
) else (
    echo     (No watchdog events yet - agent has been running continuously)
)

echo.
echo   --- Quick Actions ---
echo.
echo   START:      wscript.exe "%~dp0run_hidden.vbs"
echo   STOP:       taskkill /F /IM python.exe
echo   REINSTALL:  Run install_autostart.bat as Administrator
echo.
pause
