@echo off
rem Offline check: reads your supplier file and checks every number's format and check digit, with no lookups
rem (about 2 minutes). Saves <workbook>_tax_check.xlsx and a log in a "logs" folder next to the workbook.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo The tool isn't set up yet. Double-click "Test Setup.bat" first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" check_suppliers.py --offline --show-result %*
echo.
pause
