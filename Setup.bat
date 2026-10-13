@echo off
rem One-time setup: creates a private Python environment in this folder and installs what the tool needs.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python isn't installed or isn't on PATH. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during installation, then run this again.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" python -m venv .venv
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
".venv\Scripts\python.exe" -m playwright install chromium
".venv\Scripts\python.exe" -m unittest discover tests
echo.
echo Setup finished. Double-click "Run Tax Check.bat" to check a supplier workbook.
pause
