@echo off
title PPIS Gate Head Count Counter
cd /d "%~dp0"
set "LOGFILE=%~dp0gate_counter.log"

if not exist "%~dp0.locks\" mkdir "%~dp0.locks" >nul 2>&1
call :run 9>"%~dp0.locks\gate_counter.lock"
if errorlevel 1 (
    echo [%DATE% %TIME%] WRAPPER: Gate-counter lock is already held; exiting cleanly. >> "%LOGFILE%"
    exit /b 0
)
exit /b 0

:run
echo ========================================
echo  PPIS Gate Head Count Counter
echo  Monitors entry gates for person count
echo ========================================
echo.

:loop
echo [%date% %time%] Starting gate counter...
for /f "delims=" %%H in ('git rev-parse --short HEAD 2^>nul') do set "REVISION=%%H"
if not defined REVISION set "REVISION=unknown"
echo [%date% %time%] Starting revision %REVISION% >> "%LOGFILE%"
python gate_counter.py
echo.
echo [%date% %time%] Gate counter exited (code %errorlevel%). Restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop
