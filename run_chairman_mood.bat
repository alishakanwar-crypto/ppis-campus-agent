@echo off
title PPIS Chairman Mood Monitor
echo ============================================
echo  Chairman Mood ^& Temperament Monitor
echo  PP International School
echo ============================================
echo.

cd /d "%~dp0"

:loop
echo [%date% %time%] Starting chairman mood monitor...
python chairman_mood.py
echo.
echo [%date% %time%] Process exited. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
