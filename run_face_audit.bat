@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo   PPIS CP Plus Face Feasibility Audit - 10 Minutes
echo ========================================================
echo.
echo This is local, adult-only, and audit-only. It saves no images
echo or names and does not change headcount, attendance, reports,
echo or WhatsApp messages.
echo.

set "CPPLUS_FACE_AUDIT_ENABLED=1"
python gate_face_audit.py --duration-minutes 10 --interval-seconds 2

echo.
echo Send the final summary shown above for assessment.
pause
