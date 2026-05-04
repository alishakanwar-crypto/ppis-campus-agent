@echo off
setlocal enabledelayedexpansion
REM PPIS Campus Agent — Auto-restart wrapper with PID lock
REM Features:
REM   - PID lock prevents multiple instances
REM   - Kills stale agent processes before starting
REM   - Auto-pulls latest code before each restart
REM   - Suppresses Windows error dialogs
REM   - Restarts after 10 second cooldown

title PPIS Campus Agent (24/7)
cd /d "%~dp0"

REM === PID Lock: prevent multiple instances ===
set "LOCKFILE=%~dp0agent.lock"

if exist "%LOCKFILE%" (
    echo [%DATE% %TIME%] Lock file found. Checking for running instance...
    set /p "OLD_PID=" < "%LOCKFILE%"
)

if defined OLD_PID (
    tasklist /FI "PID eq !OLD_PID!" 2>nul | find "!OLD_PID!" >nul
    if !errorlevel! equ 0 (
        echo [%DATE% %TIME%] Agent already running as PID !OLD_PID!. Exiting.
        exit /b 1
    )
    echo [%DATE% %TIME%] Stale lock. Cleaning up...
    del "%LOCKFILE%" 2>nul
)

REM Kill any stale python processes running main.py
tasklist /FI "IMAGENAME eq python.exe" >nul 2>&1
if %errorlevel% equ 0 (
    echo [%DATE% %TIME%] Checking for stale agent processes...
    wmic process where "name='python.exe'" get ProcessId,CommandLine /FORMAT:LIST 2>nul | find "main.py" >nul
    if !errorlevel! equ 0 (
        echo [%DATE% %TIME%] Killing stale agent processes...
        for /f "tokens=2 delims==" %%p in ('wmic process where "name='python.exe' and CommandLine like '%%main.py%%'" get ProcessId /VALUE 2^>nul ^| find "="') do (
            taskkill /PID %%p /F >nul 2>&1
        )
    )
)

REM Write our PID to lock file
echo %PID%> "%LOCKFILE%"

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
git pull 2>nul
echo.
echo [%DATE% %TIME%] Starting PPIS Campus Agent...
echo ============================================
py -3.12 main.py
echo.
echo [%DATE% %TIME%] Agent stopped (exit code: %ERRORLEVEL%). Restarting in 10 seconds...
timeout /t 10 /nobreak
goto loop
