@echo off
REM ============================================================
REM TrueFace 3000 Poller — Windows Auto-Start Installer
REM Run this ONCE as Administrator.
REM ============================================================

echo.
echo ============================================================
echo   TrueFace 3000 Poller — Auto-Start Installer
echo ============================================================
echo.

REM Check for admin privileges
net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: This script must be run as Administrator!
    echo Right-click and select "Run as administrator"
    echo.
    pause
    exit /b 1
)

REM Install Selenium if not present
echo Installing Selenium...
py -3.12 -m pip install selenium --quiet 2>nul
if %ERRORLEVEL% NEQ 0 (
    pip install selenium --quiet 2>nul
)

REM Download chromedriver-autoinstaller for automatic version matching
echo Installing chromedriver-autoinstaller...
py -3.12 -m pip install chromedriver-autoinstaller --quiet 2>nul
if %ERRORLEVEL% NEQ 0 (
    pip install chromedriver-autoinstaller --quiet 2>nul
)

REM Auto-install matching chromedriver
echo Downloading matching chromedriver...
py -3.12 -c "import chromedriver_autoinstaller; chromedriver_autoinstaller.install()" 2>nul

REM Remove old scheduled task if it exists
schtasks /delete /tn "PPIS TrueFace Poller" /f >nul 2>&1

REM Create scheduled task
echo Creating startup task...
schtasks /create /tn "PPIS TrueFace Poller" /tr "wscript.exe \"%~dp0run_trueface_hidden.vbs\"" /sc onstart /rl highest /delay 0001:00 /ru SYSTEM /f 2>nul

if %ERRORLEVEL% NEQ 0 (
    echo NOTE: SYSTEM user task failed, trying with current user...
    schtasks /create /tn "PPIS TrueFace Poller" /tr "wscript.exe \"%~dp0run_trueface_hidden.vbs\"" /sc onlogon /rl highest /f
)

echo.
echo ============================================================
echo   SUCCESS! TrueFace Poller auto-start installed.
echo ============================================================
echo.
echo   The poller will auto-start on next PC reboot.
echo.
echo   To START NOW:    run_trueface.bat
echo   To TEST:         py -3.12 trueface_poller.py --test
echo   To STOP:         taskkill /F /FI "WINDOWTITLE eq TrueFace*"
echo   To UNINSTALL:    schtasks /delete /tn "PPIS TrueFace Poller" /f
echo.

pause
