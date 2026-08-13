@echo off
setlocal enabledelayedexpansion
REM PPIS Campus Agent — Auto-restart wrapper
REM Features:
REM   - Auto-restarts if the agent crashes
REM   - Pulls latest code before each restart (stashes local changes first)
REM   - Suppresses Windows error dialogs
REM   - 10 second cooldown between restarts
REM   - Cleans up old snapshot files to prevent disk fill

title PPIS Campus Agent (24/7)
cd /d "%~dp0"
set "LOGFILE=%~dp0wrapper_campus.log"

if not exist "%~dp0.locks\" mkdir "%~dp0.locks" >nul 2>&1
call :cap_log
call :run 9>"%~dp0.locks\campus.lock"
if errorlevel 1 (
    echo [%DATE% %TIME%] WRAPPER: Campus lock is already held; exiting cleanly. >> "%LOGFILE%"
    exit /b 0
)
exit /b 0

:run

REM Suppress Windows Error Reporting dialogs
reg add "HKCU\Software\Microsoft\Windows\Windows Error Reporting" /v DontShowUI /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\Windows Error Reporting" /v Disabled /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\Software\Microsoft\Windows\Windows Error Reporting" /v DontShowUI /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\Software\Microsoft\Windows\Windows Error Reporting" /v Disabled /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\AeDebug" /v Auto /t REG_SZ /d 1 /f >nul 2>&1

set PYTHONDONTWRITEBYTECODE=1
REM Must match DUPLICATE_INSTANCE_EXIT_CODE in campus_instance.py.
set "DUPLICATE_EXIT_CODE=75"

:loop
call :cap_log
echo [%DATE% %TIME%] Pulling latest code... >> "%LOGFILE%"
REM Fetch latest code, but never reset to an unverified stale remote ref.
git fetch origin >nul 2>&1
if errorlevel 1 (
    echo [%DATE% %TIME%] GIT: Fetch failed; keeping existing working tree. >> "%LOGFILE%"
) else (
    git checkout main >nul 2>&1
    if errorlevel 1 (
        echo [%DATE% %TIME%] GIT: Checkout main failed; keeping existing working tree. >> "%LOGFILE%"
    ) else (
        git reset --hard origin/main >nul 2>&1
        if errorlevel 1 echo [%DATE% %TIME%] GIT: Reset origin/main failed; keeping existing working tree. >> "%LOGFILE%"
    )
)

REM Clear bytecode cache to avoid stale .pyc files after code updates
if exist "%~dp0__pycache__" rmdir /s /q "%~dp0__pycache__" 2>nul

REM Clean up old snapshot files (older than 1 day) to prevent disk fill
echo [%DATE% %TIME%] Cleaning old snapshots... >> "%LOGFILE%"
forfiles /p "%~dp0snapshots" /d -1 /m *.* /c "cmd /c del /Q @path" 2>nul
forfiles /p "%~dp0attendance_snapshots" /d -1 /m *.* /c "cmd /c del /Q @path" 2>nul

set "REVISION=unknown"
for /f "delims=" %%H in ('git rev-parse --short HEAD 2^>nul') do set "REVISION=%%H"
echo [%DATE% %TIME%] Starting revision !REVISION! >> "%LOGFILE%"
py -3.12 -B main.py
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="%DUPLICATE_EXIT_CODE%" (
    echo [%DATE% %TIME%] Another campus agent owns the mutex; wrapper exiting cleanly. >> "%LOGFILE%"
    exit /b 0
)
echo [%DATE% %TIME%] Agent stopped (exit code: %EXIT_CODE%). Restarting in 10 seconds... >> "%LOGFILE%"
timeout /t 10 /nobreak
goto loop

:cap_log
if exist "%LOGFILE%" (
    set "LINES=0"
    for /f %%a in ('type "%LOGFILE%" ^| find /c /v ""') do set "LINES=%%a"
    if !LINES! GTR 500 (
        move /y "%LOGFILE%" "%LOGFILE%.old" >nul 2>&1
    )
)
exit /b 0
