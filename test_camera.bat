@echo off
REM =====================================================================
REM  PPIS Gate Camera Connectivity Test
REM  Double-click this file on the school PC to check every gate camera,
REM  including the CP Plus outside-gate camera (192.168.0.215).
REM  Look for a line ending in "OK - Frame WxH" for each camera.
REM =====================================================================
title PPIS Gate Camera Test
cd /d "%~dp0"

echo ========================================
echo  PPIS Gate Camera Connectivity Test
echo  (includes CP Plus outside gate 192.168.0.215)
echo ========================================
echo.

python gate_counter.py --test

echo.
echo ========================================
echo  Test finished. Review the lines above:
echo    OK      = camera reachable
echo    FAILED  = camera not reachable / wrong credentials
echo ========================================
pause
