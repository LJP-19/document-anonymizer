@echo off
setlocal
:: Double-click this file on Windows to publish a release.
:: It commits the current project, pushes it to GitHub, and GitHub Actions
:: builds the Windows .exe and macOS .dmg on native runners.
::
:: Requirements: Git for Windows and Python 3.11+ on PATH.

cd /d "%~dp0\.."

where git >nul 2>&1
if errorlevel 1 (
    echo ERROR: Git is not installed.
    echo Install it from https://git-scm.com/download/win then run this again.
    pause
    exit /b 1
)

set PY=
where py >nul 2>&1 && set PY=py -3
if not defined PY where python >nul 2>&1 && set PY=python
if not defined PY (
    echo ERROR: Python is not installed.
    echo Install Python 3.12 from https://www.python.org/downloads/
    echo Tick "Add python.exe to PATH" during setup, then run this again.
    pause
    exit /b 1
)

%PY% -m pytest --version >nul 2>&1
if errorlevel 1 (
    echo Installing development dependencies, one moment...
    %PY% -m pip install -r requirements-dev.txt
)

%PY% release\release.py
endlocal
