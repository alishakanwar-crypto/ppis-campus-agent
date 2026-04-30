@echo off
REM Check if PPIS Campus Agent is running in the background
echo.
echo ============================================================
echo   PPIS Campus Agent — Status Check
echo ============================================================
echo.

tasklist /fi "imagename eq python.exe" 2>nul | find /i "python.exe" >nul
if %ERRORLEVEL% EQU 0 (
    echo   STATUS: RUNNING (agent is active in background)
    echo.
    echo   To STOP:    taskkill /F /IM python.exe
    echo   To RESTART: taskkill /F /IM python.exe && timeout /t 3 && wscript.exe "%~dp0run_hidden.vbs"
) else (
    echo   STATUS: STOPPED (agent is not running)
    echo.
    echo   To START:   wscript.exe "%~dp0run_hidden.vbs"
)

echo.
echo   Task Scheduler entry:
schtasks /query /tn "PPIS Campus Agent" 2>nul || echo   (Not installed - run install_autostart.bat)
echo.
pause
