@echo off
rem Saskatchewan probe: tick the CAPTCHA once; it searches a few well-known businesses and saves the pages
rem (screenshots + HTML) in logs\sk_probe_<date>, so the tool can be taught to read the results. Uses 3 of 100 daily searches.
cd /d "%~dp0"
if not exist logs mkdir logs
echo Tool folder: %CD%
if not exist ".venv\Scripts\python.exe" (
    echo The tool isn't set up in this folder: there is no .venv folder here.
    echo Copy the new files over the folder you already use, or double-click "Test Setup.bat" first.
    echo The tool isn't set up in this folder ^(%CD%^): no .venv folder. > "logs\sk_probe_error.txt"
    pause
    exit /b 1
)
".venv\Scripts\python.exe" sk_probe.py %*
if errorlevel 1 echo The probe didn't finish; see logs\sk_probe_error.txt in this folder.
echo.
pause
