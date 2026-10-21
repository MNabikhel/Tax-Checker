@echo off
rem Saskatchewan PST only, in daily batches: the registry allows 100 searches a day. Each batch checks the
rem Saskatchewan suppliers of the last full run's workbook that have no answer yet (answers are kept 60 days),
rem up to the day's limit, and writes <workbook>_saskatchewan.xlsx with every Saskatchewan supplier so far.
rem You tick the CAPTCHA once at the start. Run it once a day until it says all are done.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo The tool isn't set up yet. Double-click "Tax Checker.bat" first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" check_suppliers.py --sk-only --show-result %*
echo.
pause
