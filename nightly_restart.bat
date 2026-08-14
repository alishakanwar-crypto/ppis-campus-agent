@echo off
REM Unattended nightly refresh: stop every agent process and let the watchdog
REM bring them back on clean code. No prompts, so Task Scheduler can run it.
setlocal EnableDelayedExpansion

REM The git reset below rewrites this very file, and cmd.exe resumes a running
REM script by byte offset — so re-run from %TEMP% first (same guard as
REM restart_all.bat) and keep the repo path in %~1.
if /I not "%~1"=="--from-temp" (
    set "SELF_COPY=%TEMP%\ppis_nightly_restart.bat"
    copy /y "%~f0" "!SELF_COPY!" >nul 2>&1
    if exist "!SELF_COPY!" (
        call "!SELF_COPY!" --from-temp "%~dp0"
        exit /b !ERRORLEVEL!
    )
)

if /I "%~1"=="--from-temp" (
    set "AGENT_DIR=%~2"
) else (
    set "AGENT_DIR=%~dp0"
)
cd /d "%AGENT_DIR%"

set "LOGFILE=%AGENT_DIR%nightly_restart.log"

REM Keep the log small
for /f %%A in ('type "!LOGFILE!" 2^>nul ^| find /c /v ""') do set LINES=%%A
if defined LINES if !LINES! GTR 300 (
    more +100 "!LOGFILE!" > "!LOGFILE!.tmp" 2>nul
    move /y "!LOGFILE!.tmp" "!LOGFILE!" >nul 2>&1
)

echo [%DATE% %TIME%] NIGHTLY: stopping agents... >> "%LOGFILE%"

REM Stop the batch supervisors first so they do not relaunch mid-restart
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'run_forever|run_trueface|run_gate_counter' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
taskkill /F /IM python.exe >nul 2>&1
taskkill /F /IM py.exe >nul 2>&1
taskkill /F /IM pythonw.exe >nul 2>&1
taskkill /F /IM chromedriver.exe >nul 2>&1

timeout /t 5 /nobreak >nul

echo [%DATE% %TIME%] NIGHTLY: pulling latest code... >> "%LOGFILE%"
git fetch origin >> "%LOGFILE%" 2>&1
git reset --hard origin/main >> "%LOGFILE%" 2>&1

echo [%DATE% %TIME%] NIGHTLY: starting agents via watchdog... >> "%LOGFILE%"
call "!AGENT_DIR!watchdog.bat"

echo [%DATE% %TIME%] NIGHTLY: done >> "%LOGFILE%"
endlocal
exit /b 0
