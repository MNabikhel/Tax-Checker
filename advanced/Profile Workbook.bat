@echo off
rem Workbook profile: counts and patterns of your supplier file (no names or numbers), for tailoring the tool.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo The tool isn't set up in this folder yet: double-click "Test Setup.bat" first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" profile_workbook.py %*
echo.
pause
