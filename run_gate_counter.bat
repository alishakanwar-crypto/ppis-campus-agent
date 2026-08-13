@echo off
setlocal enabledelayedexpansion
title PPIS Gate Head Count Counter
cd /d "%~dp0"
set "LOGFILE=%~dp0wrapper_gate_counter.log"

if not exist "%~dp0.locks\" mkdir "%~dp0.locks" >nul 2>&1
call :cap_log
call :run 9>"%~dp0.locks\gate_counter.lock"
if errorlevel 1 (
    echo [%DATE% %TIME%] WRAPPER: Gate-counter lock is already held; exiting cleanly. >> "%LOGFILE%"
    exit /b 0
)
exit /b 0

:run
echo [%date% %time%] Starting gate counter... >> "%LOGFILE%"

:loop
call :cap_log
set "REVISION=unknown"
for /f "delims=" %%H in ('git rev-parse --short HEAD 2^>nul') do set "REVISION=%%H"
echo [%date% %time%] Starting revision %REVISION% >> "%LOGFILE%"
python gate_counter.py
echo [%date% %time%] Gate counter exited (code %errorlevel%). Restarting in 10s... >> "%LOGFILE%"
timeout /t 10 /nobreak >nul
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
