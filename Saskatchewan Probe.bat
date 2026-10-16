@echo off
rem Saskatchewan probe: tick the CAPTCHA once; it searches a few well-known businesses and saves the pages
rem (screenshots + HTML) in logs\sk_probe_<date>, so the tool can be taught to read the results. Uses 3 of 100 daily searches.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo The tool isn't set up yet. Double-click "Test Setup.bat" first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" sk_probe.py %*
echo.
pause
