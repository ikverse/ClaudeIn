@echo off
rem Publishes a ClaudeIn release on GitHub, so installed copies can update themselves.
rem   Release.bat 0.2.0 "What changed"
rem   Release.bat 0.2.0 "What changed" --dry-run     (builds and signs only; publishes nothing)
rem Needs: Setup-ClaudeIn.bat run once, Inno Setup 6, gh signed in, and the signing key
rem (%USERPROFILE%\.claudein-release\signing-key.pem, made by packaging\make_signing_key.py).
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Run Setup-ClaudeIn.bat first.
    pause
    exit /b 1
)
if "%~1"=="" (
    echo Usage: Release.bat 0.2.0 "What changed"
    pause
    exit /b 2
)

".venv\Scripts\python.exe" -m pip install --quiet --upgrade -r requirements-build.txt
".venv\Scripts\python.exe" packaging\release.py %*
pause
