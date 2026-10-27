@echo off
rem setup.bat -- one-time setup on Windows. Double-click it, or run it from cmd.
rem
rem Creates a private Python environment in .venv\ and installs the four
rem packages in requirements.txt into it. Nothing is installed system-wide.
rem After this, run.bat uses .venv automatically.

setlocal EnableExtensions
cd /d "%~dp0"

rem Find a Python 3.11+ interpreter: the py launcher first, then python.
set "BASEPY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "BASEPY=py -3"
if not defined BASEPY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "BASEPY=python"
)
if not defined BASEPY (
    echo ERROR: Python 3.11 or newer was not found.
    echo.
    echo   Install it, then run setup.bat again:
    echo     1. Go to https://www.python.org/downloads/ and download Python 3.12.
    echo     2. Run the installer and TICK "Add python.exe to PATH" on the first screen.
    echo     3. Close this window, open a new one, and run setup.bat again.
    echo.
    echo   Or, in a terminal:  winget install Python.Python.3.12
    pause
    exit /b 1
)

echo Using: %BASEPY%
if not exist ".venv\Scripts\python.exe" (
    echo Creating the project environment in .venv ...
    %BASEPY% -m venv .venv || (
        echo ERROR: could not create .venv
        pause
        exit /b 1
    )
)

echo Installing packages: pandas, numpy, scipy, matplotlib ...
.venv\Scripts\python.exe -m pip install --upgrade pip >nul
.venv\Scripts\python.exe -m pip install -r requirements.txt || (
    echo ERROR: package install failed. Check your internet connection and run setup.bat again.
    pause
    exit /b 1
)

echo.
echo Setup complete. Next:
echo   run.bat test      run the unit tests
echo   run.bat demo      run the full pipeline on synthetic data
pause
exit /b 0
