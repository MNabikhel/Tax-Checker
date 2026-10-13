@echo off
rem Opens a window to choose the supplier workbook, then checks it. Results and logs are saved next to it.
rem Options can be passed from a command prompt, e.g.:  "Run Tax Check.bat" --sk-assist --trust-gst-number
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo The tool isn't set up yet. Double-click Setup.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" check_suppliers.py %*
echo.
pause
