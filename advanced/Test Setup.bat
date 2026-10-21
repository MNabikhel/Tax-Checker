@echo off
rem Setup test: repairs the install if needed, checks every government site and the browser,
rem asks for the supplier workbook, and tries a few real lookups. It doesn't create a results workbook;
rem when it says READY it offers the 25-supplier trial, which does.
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
echo Checking the required packages are installed...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo Retrying with the Windows certificate store...
    ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --use-feature=truststore -r requirements.txt
)
rem The tool falls back to Microsoft Edge when this download is blocked (as on some corporate networks).
".venv\Scripts\python.exe" -m playwright install chromium >nul 2>nul
echo.
if exist ".last_workbook" del ".last_workbook"
".venv\Scripts\python.exe" check_setup.py %*
if errorlevel 1 goto done
if not exist ".last_workbook" goto done
set /p WORKBOOK=<".last_workbook"
echo.
choice /c YN /m "Run the 25-supplier trial on this workbook now (about 2 minutes, creates the results workbook)"
if errorlevel 2 goto done
echo.
".venv\Scripts\python.exe" check_suppliers.py "%WORKBOOK%" --limit 25 --ask --show-result
:done
echo.
pause
