@echo off
rem Trial run: 25 real lookups from the top of the workbook you choose (about 2 minutes).
rem Saves <workbook>_tax_check_TRIAL.xlsx next to the workbook and opens that folder when done.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo The tool isn't set up yet. Double-click "Tax Checker.bat" first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" check_suppliers.py --limit 25 --ask --show-result %*
echo.
pause
