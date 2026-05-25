@echo off
echo =============================================
echo   Lunch Box Detector - PPIS Campus Agent
echo =============================================
echo.

:: Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.10+ from https://python.org
    pause
    exit /b 1
)

:: Install requirements if needed
echo Checking dependencies...
pip install -q ultralytics opencv-python customtkinter Pillow

echo.
echo Starting Lunch Box Detector...
echo.
python lunchbox_detector.py

if errorlevel 1 (
    echo.
    echo Error occurred. Press any key to exit.
    pause
)
