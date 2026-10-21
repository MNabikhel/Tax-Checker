@echo off
rem One-click test on public test data (no vendor data): setup check, profile, a full run stopped part way, the
rem resume, and one zip of every log and result to send back. You only tick the Saskatchewan CAPTCHA once.
cd /d "%~dp0.."
where python >nul 2>nul
if errorlevel 1 (
    echo Python isn't installed or isn't on PATH. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during installation, then run this again.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo First run in this folder: creating the tool's Python environment. This takes a few minutes...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade pip
)
if not exist ".venv\Scripts\python.exe" (
    echo Python couldn't create the tool's environment. Install Python from https://www.python.org/downloads/
    echo ^(tick "Add python.exe to PATH"^), then run this again.
    pause
    exit /b 1
)
echo Checking the required packages are installed...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo Retrying with the Windows certificate store...
    ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --use-feature=truststore -r requirements.txt
)
echo Checking the browser (this can take a minute and shows nothing)...
".venv\Scripts\python.exe" -m playwright install chromium >nul 2>nul
".venv\Scripts\python.exe" run_test.py
echo.
pause
