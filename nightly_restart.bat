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

REM Git refuses to work in a checkout owned by another account, and under
REM SYSTEM this checkout belongs to the campus user, so both commands below
REM would fail as an "unsafe repository" and the refresh would restart the
REM agent on last night's code. safe.directory is passed for this one path
REM only, rather than turning the ownership check off for the machine. The
REM setting is quoted because the campus path can hold a space, and unquoted
REM cmd would hand git half a path and run the rest of it as a command.
set "REPO=%AGENT_DIR%"
if "!REPO:~-1!"=="\" set "REPO=!REPO:~0,-1!"
set OWNED=-c "safe.directory=!REPO!"

set "REFRESHED=1"
echo [%DATE% %TIME%] NIGHTLY: pulling latest code... >> "%LOGFILE%"
git !OWNED! fetch origin >> "%LOGFILE%" 2>&1
if errorlevel 1 set "REFRESHED="
git !OWNED! reset --hard origin/main >> "%LOGFILE%" 2>&1
if errorlevel 1 set "REFRESHED="
if not defined REFRESHED echo [%DATE% %TIME%] NIGHTLY: could not take merged code; starting the agent on the code already here >> "%LOGFILE%"

REM Under SYSTEM (nobody logged on) only the campus agent can be started:
REM TrueFace needs a Chrome window and the gate counter needs native CP Plus,
REM and both would fail in session 0 leaving half-started wrappers behind.
REM The ordinary watchdog brings those two back within five minutes of logon.
set "WATCH_MODE="
whoami /user 2>nul | find /i "S-1-5-18" >nul && set "WATCH_MODE=agent-only"
if defined WATCH_MODE (
    echo [%DATE% %TIME%] NIGHTLY: running as SYSTEM; starting the campus agent only >> "%LOGFILE%"
)
echo [%DATE% %TIME%] NIGHTLY: starting agents via watchdog... >> "%LOGFILE%"
call "!AGENT_DIR!watchdog.bat" !WATCH_MODE!

echo [%DATE% %TIME%] NIGHTLY: done >> "%LOGFILE%"

REM The agents are started either way — a failed refresh must never leave the
REM campus without an agent — but the task ends non-zero so the night shows up
REM in health as a result instead of a silent success on stale code.
if not defined REFRESHED (
    endlocal
    exit /b 1
)
endlocal
exit /b 0
