@echo off
REM ================================================================
REM  PPIS Campus Agent — Diagnostic Runner
REM  Runs main.py in the foreground so you can see any errors.
REM  Share the output or screenshot with Devin for debugging.
REM ================================================================

cd /d "%~dp0"
echo.
echo ========================================================
echo   PPIS Campus Agent — Diagnostic Mode
echo ========================================================
echo.
echo   Current directory: %CD%
echo   Python version:
py -3.12 --version 2>nul || python --version 2>nul || echo   ERROR: Python not found!
echo.
echo   Checking port 8897:
netstat -ano | findstr ":8897" | findstr "LISTENING"
if %ERRORLEVEL% NEQ 0 echo   Port 8897 is free (OK)
echo.
echo   Deleting lock file...
if exist ".agent_lock" del ".agent_lock"
echo.
echo ========================================================
echo   Starting campus agent (errors will show below)...
echo ========================================================
echo.
py -3.12 -B main.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ========================================================
    echo   AGENT CRASHED with exit code: %ERRORLEVEL%
    echo   Try: python main.py
    echo ========================================================
    echo.
    python -B main.py
)
echo.
echo ========================================================
echo   Agent stopped. Press any key to close.
echo ========================================================
pause
