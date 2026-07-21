@echo off
setlocal
cd /d "%~dp0"

set "DURATION_MINUTES=%~1"
if not defined DURATION_MINUTES set "DURATION_MINUTES=10"

echo ========================================================
echo   PPIS CP Plus Continuous Face Audit - %DURATION_MINUTES% Minutes
echo ========================================================
echo.
echo This is local, adult-only, and audit-only. It saves no images
echo or names and does not change headcount, attendance, reports,
echo or WhatsApp messages.
echo.

set "CPPLUS_FACE_AUDIT_ENABLED=1"
set "CPPLUS_RTSP_SUBTYPE=1"
python gate_face_audit.py --duration-minutes %DURATION_MINUTES% --interval-seconds 0.25

echo.
echo Send the final summary shown above for assessment.
if "%~1"=="" pause
