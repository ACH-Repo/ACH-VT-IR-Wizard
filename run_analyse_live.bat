@echo off
REM ============================================================
REM   Double-click launcher for analyse_live.py
REM   Opens the live VT-IR overlay plot in its own console
REM   so it can run alongside run_vt_ir.bat.
REM ============================================================

setlocal
cd /d "%~dp0"

where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 analyse_live.py %*
) else (
    python analyse_live.py %*
)

echo.
echo --- script exited with code %ERRORLEVEL% ---
pause
endlocal
