@echo off
rem ---------------------------------------------------------------------------
rem  Schoology API Client launcher - double-click me.
rem  First run: creates a private Python environment and installs components.
rem  After that it just opens the app in your browser.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto check_requirements

echo Setting up the Schoology API Client for the first time. This takes a minute or two...
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m venv .venv
) else (
    python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  Python 3.10 or newer is required but was not found.
    echo  Install Python from the Company Portal or python.org - tick "Add python.exe to PATH" -
    echo  then double-click this file again.
    echo.
    pause
    exit /b 1
)

:check_requirements
rem Re-install components only when requirements.txt has changed, e.g. after an update
fc /b "requirements.txt" ".venv\installed-requirements.txt" >nul 2>nul
if %errorlevel%==0 goto run
echo Installing components...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if not %errorlevel%==0 (
    echo.
    echo  Installing components failed - see the messages above.
    echo  If you are on the company network, a proxy may be blocking pip. Ask the app admin.
    echo.
    pause
    exit /b 1
)
copy /y "requirements.txt" ".venv\installed-requirements.txt" >nul

:run
start "" ".venv\Scripts\pythonw.exe" -m restclient
exit /b 0
