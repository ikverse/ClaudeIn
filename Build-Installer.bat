@echo off
rem Builds ClaudeIn-Setup.exe (the installer for any Windows PC). For the owner's own use; needs Setup-ClaudeIn.bat run first.
rem Needs Inno Setup 6 for the installer step:  winget install JRSoftware.InnoSetup
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Run Setup-ClaudeIn.bat first.
    pause
    exit /b 1
)

echo Installing the build tool...
".venv\Scripts\python.exe" -m pip install --upgrade -r requirements-build.txt
if errorlevel 1 (
    echo Installing the build tool failed.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" packaging\build.py %*
if errorlevel 1 (
    echo The build failed.
    pause
    exit /b 1
)

echo.
echo Done. The installer is in the build\installer folder.
pause
