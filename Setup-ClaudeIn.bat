@echo off
rem ClaudeIn setup: makes .venv with Python 3.14 and installs the add-ons.
rem Safe to run again; it updates the add-ons.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Making .venv with Python 3.14...
    py -3.14 -m venv .venv
    if errorlevel 1 (
        echo Could not make .venv. Is Python 3.14 installed?
        pause
        exit /b 1
    )
)

echo Installing add-ons...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
    echo Installing the add-ons failed.
    pause
    exit /b 1
)

echo Adding the Start Menu shortcut...
".venv\Scripts\python.exe" -m claudein --setup
if errorlevel 1 (
    echo Making the shortcut failed.
    pause
    exit /b 1
)

echo Starting ClaudeIn (its icon is in the hidden icons by the clock)...
start "" ".venv\Scripts\pythonw.exe" -m claudein

echo.
echo ClaudeIn setup finished.
pause
