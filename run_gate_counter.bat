@echo off
title PPIS Gate Head Count Counter
echo ========================================
echo  PPIS Gate Head Count Counter
echo  Monitors entry gates for person count
echo ========================================
echo.

:loop
echo [%date% %time%] Starting gate counter...
python gate_counter.py
echo.
echo [%date% %time%] Gate counter exited (code %errorlevel%). Restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop
