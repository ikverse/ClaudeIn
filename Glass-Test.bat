@echo off
rem Opens the glass check window (no console window stays open).
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Run Setup-ClaudeIn.bat first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m claudein.glass_test
