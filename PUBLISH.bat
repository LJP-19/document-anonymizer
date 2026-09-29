@echo off
setlocal enabledelayedexpansion
:: ============================================================
::  Document Anonymizer - update and publish
::  Double-click this file. It will:
::    1. find the newest document-anonymizer-v*.zip you saved
::    2. copy it over this project (your settings are kept)
::    3. run the tests if this machine can
::    4. commit, push, and tag on GitHub
::  GitHub Actions then builds the Windows .exe and macOS .dmg.
:: ============================================================
cd /d "%~dp0"

where git >nul 2>&1
if errorlevel 1 (
    echo ERROR: Git is not installed.
    echo Install it from https://git-scm.com/download/win then run this again.
    pause
    exit /b 1
)

:: The pinned dependencies ship wheels for Python 3.11 and 3.12 only.
:: Prefer those, whatever else is on the machine.
set PY=
for %%V in (3.12 3.11) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)

if not defined PY (
    echo.
    echo Python 3.12 was not found on this machine.
    echo The publish step itself will still work, but the tests cannot
    echo run here. GitHub runs the full test suite on Linux, Windows and
    echo macOS before building anything, so this is safe to skip.
    echo.
    where winget >nul 2>&1
    if not errorlevel 1 (
        echo Install Python 3.12 now so tests can run locally too?
        choice /c YN /m "Install Python 3.12"
        if !errorlevel! equ 1 (
            echo Installing Python 3.12 -- Windows may ask for permission...
            winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
            py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
        )
    ) else (
        echo To install it yourself: https://www.python.org/downloads/release/python-3129/
        echo Tick "Add python.exe to PATH" during setup.
    )
)

:: Fall back to any Python at all -- enough to run the publish scripts.
if not defined PY (
    py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo ERROR: No Python found at all. Install Python 3.12 from
    echo https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

for /f "delims=" %%v in ('%PY% -c "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"') do set PYVER=%%v
echo Using Python %PYVER%
echo.

%PY% release\update.py %*
endlocal
