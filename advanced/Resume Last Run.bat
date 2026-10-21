@echo off
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo The tool isn't set up yet. Double-click "Tax Checker.bat" first.
    pause
    exit /b 1
)
rem Continues the latest full run (same workbook and answers) after a restart, network drop or Ctrl+C.
".venv\Scripts\python.exe" check_suppliers.py --resume last --show-result %*
echo.
pause
