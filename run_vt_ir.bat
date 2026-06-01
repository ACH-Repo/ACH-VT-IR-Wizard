@echo off
REM ============================================================
REM   Double-click launcher for vt_ir.py
REM   Runs the VT-IR orchestrator and pauses on exit so any
REM   error message stays on screen.
REM ============================================================

setlocal
cd /d "%~dp0"

REM Use the "py" launcher if available; fall back to plain "python".
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 vt_ir.py %*
) else (
    python vt_ir.py %*
)

echo.
echo --- script exited with code %ERRORLEVEL% ---
pause
endlocal
