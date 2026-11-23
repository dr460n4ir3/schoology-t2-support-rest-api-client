@echo off
rem ---------------------------------------------------------------------------
rem  Admin only: builds a stand-alone app (no Python needed on team PCs).
rem  Output: dist\Schoology API Client\  -> zip that folder and share it.
rem  Team members unzip it and double-click "Schoology API Client.exe".
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run "Launch API Client.bat" once first to create the Python environment.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller
if not %errorlevel%==0 goto failed
rem nicegui-pack calls "pyinstaller" by name, so put the venv's tools first on PATH
set "PATH=%~dp0.venv\Scripts;%PATH%"
rem remember which GitHub repo this build came from (for its "Check for updates")
".venv\Scripts\python.exe" -m restclient.updater stamp
".venv\Scripts\nicegui-pack.exe" --onedir --windowed --noconfirm --clean ^
    --name "Schoology API Client" ^
    --add-data "examples;examples" ^
    --add-data "collections;collections" ^
    --add-data "build_repo.txt;." ^
    run_app.py
if not %errorlevel%==0 goto failed
echo.
echo Done. Zip and share the folder:  dist\Schoology API Client
pause
exit /b 0

:failed
echo.
echo Build failed - see the messages above.
pause
exit /b 1
