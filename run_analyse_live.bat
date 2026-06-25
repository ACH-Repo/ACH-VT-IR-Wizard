@echo off
REM ============================================================
REM   Double-click launcher for the live plots, standalone.
REM   The wizard auto-launches both plot windows during a run;
REM   use this only to re-open a plot for a finished/!running run.
REM   Pass --ir to open the stacked-IR-spectrum window instead of
REM   the temperature overlay.  Extra args are forwarded.
REM ============================================================

setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"

set "MODULE=vtir_wizard.temp_plot"
if /I "%~1"=="--ir" (
    set "MODULE=vtir_wizard.ir_plot"
    shift
)

where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 -m %MODULE% %*
) else (
    python -m %MODULE% %*
)

echo.
echo --- plot exited with code %ERRORLEVEL% ---
pause
endlocal
