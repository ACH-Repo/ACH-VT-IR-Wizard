@echo off
REM ============================================================
REM   Double-click launcher for the VT-IR wizard.
REM   Requires the package to be installed once:
REM       pip install vtir-wizard
REM   (or, from a source checkout:  pip install .)
REM   Pauses on exit so any error message stays on screen.
REM ============================================================

setlocal
cd /d "%~dp0"

REM Prefer the installed console command; fall back to running the module
REM directly from a source checkout (src/ layout) if it isn't on PATH yet.
where vtir-wizard >nul 2>&1
if %ERRORLEVEL%==0 (
    vtir-wizard %*
) else (
    set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
    where py >nul 2>&1
    if %ERRORLEVEL%==0 (
        py -3 -m vtir_wizard.orchestrator %*
    ) else (
        python -m vtir_wizard.orchestrator %*
    )
)

echo.
echo --- wizard exited with code %ERRORLEVEL% ---
pause
endlocal
