@echo off
echo =============================================
echo   Lunch Box Detector - PPIS Campus Agent
echo =============================================
echo.

:: Use Python312 explicitly to avoid wrong Python
set PYTHON="C:\Users\DELL\AppData\Local\Programs\Python\Python312\python.exe"

:: Check if Python312 exists
if not exist %PYTHON% (
    echo Python312 not found, trying system python...
    set PYTHON=python
)

echo Using: %PYTHON%
echo.

:: Install requirements if needed
echo Checking dependencies...
%PYTHON% -m pip install -q ultralytics opencv-python customtkinter Pillow 2>nul

echo.
echo Starting Lunch Box Detector...
echo.
%PYTHON% lunchbox_detector.py

echo.
echo =============================================
echo   Detector has stopped.
echo =============================================
echo.
pause
