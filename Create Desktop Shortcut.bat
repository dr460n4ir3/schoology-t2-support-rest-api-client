@echo off
rem Puts a "Schoology API Client" shortcut on your desktop that starts the app.
setlocal
set "TARGET=%~dp0Launch API Client.bat"
set "WORKDIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Schoology API Client.lnk');" ^
  "$s.TargetPath = $env:TARGET; $s.WorkingDirectory = $env:WORKDIR; $s.WindowStyle = 7;" ^
  "$s.IconLocation = \"$env:SystemRoot\System32\shell32.dll,13\"; $s.Description = 'Schoology API Client'; $s.Save()"
if %errorlevel%==0 (
    echo Shortcut created on your desktop: "Schoology API Client"
) else (
    echo Could not create the shortcut.
)
pause
