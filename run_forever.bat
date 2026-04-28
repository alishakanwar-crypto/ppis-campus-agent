@echo off
REM PPIS Campus Agent — Auto-restart wrapper
REM This script ensures the agent restarts automatically if it crashes.
REM Run this at Windows startup (via Task Scheduler) for 24/7 operation.

title PPIS Campus Agent (24/7)
cd /d "%~dp0"

:loop
echo [%DATE% %TIME%] Starting PPIS Campus Agent...
py -3.12 main.py
echo [%DATE% %TIME%] Agent stopped (exit code: %ERRORLEVEL%). Restarting in 10 seconds...
timeout /t 10 /nobreak
goto loop
