@echo off
rem Full run: choose the supplier workbook and which taxes to check; saves <workbook>_tax_check.xlsx next to it (about 2-3 hours).
rem Options can be passed from a command prompt, e.g.:  "Run Tax Check.bat" --resume "logs\progress_....csv"
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo The tool isn't set up yet. Double-click "Test Setup.bat" first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" check_suppliers.py --ask --show-result %*
echo.
pause
