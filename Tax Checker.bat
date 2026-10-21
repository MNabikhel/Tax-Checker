@echo off
rem Tax Checker: double-click this. The first time it installs what the tool needs (a few minutes), then the
rem window opens. Everything else (setup check, full check, Saskatchewan batches, results) is in the window.
rem The older one-job .bat files are in the "advanced" folder and still work.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" if exist ".venv\.installed" goto open
where python >nul 2>nul
if errorlevel 1 (
    echo Python isn't installed or isn't on PATH. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during installation, then double-click this again.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo First run in this folder: setting up the tool. This takes a few minutes; leave this window open...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade pip
)
if not exist ".venv\Scripts\python.exe" (
    echo Python couldn't create the tool's environment. Install Python from https://www.python.org/downloads/
    echo ^(tick "Add python.exe to PATH"^), then double-click this again.
    pause
    exit /b 1
)
echo Installing the required packages...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo Retrying with the Windows certificate store...
    ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --use-feature=truststore -r requirements.txt
)
if errorlevel 1 (
    echo The packages couldn't be installed ^(see the messages above; often the network or a proxy^).
    echo Double-click this again once the network is fine.
    pause
    exit /b 1
)
rem The tool falls back to Microsoft Edge when this download is blocked (as on some corporate networks).
echo Checking the browser (this can take a minute and shows nothing)...
".venv\Scripts\python.exe" -m playwright install chromium >nul 2>nul
".venv\Scripts\python.exe" -c "import tkinter" 2>nul
if errorlevel 1 (
    echo This Python has no window support ^(tkinter^). Reinstall Python from https://www.python.org/downloads/
    echo with "tcl/tk and IDLE" ticked, delete the .venv folder here, then double-click this again.
    pause
    exit /b 1
)
echo ok> ".venv\.installed"
copy /y requirements.txt ".venv\.requirements" >nul
:open
rem requirements.txt changed since the install (a newer version of the tool unzipped over this one): install again.
fc /b requirements.txt ".venv\.requirements" >nul 2>nul
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
    if not errorlevel 1 copy /y requirements.txt ".venv\.requirements" >nul
)
start "" ".venv\Scripts\pythonw.exe" gui.py
