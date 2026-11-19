@echo off
setlocal
title Install Local AI (Ollama)
cd /d "%~dp0"
echo.
echo  Optional: sets up the free local AI used by the Assistant tab.
echo.
echo  This installs Ollama (runs AI models on this computer) and downloads:
echo    - a chat model            (about 2 GB or 5 GB, picked from your RAM)
echo    - nomic-embed-text        (about 270 MB, for searching your documents)
echo  Nothing you ask the assistant is sent to an outside AI service.
echo.
echo  To use a specific model run:  "Install Local AI (optional).bat" qwen3:8b
echo.
pause

set "OLLAMA_DIR=%LOCALAPPDATA%\Programs\Ollama"
where ollama >nul 2>nul && goto have_ollama
if exist "%OLLAMA_DIR%\ollama.exe" goto add_path

echo.
echo Installing Ollama...
where winget >nul 2>nul
if errorlevel 1 goto download
winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
if exist "%OLLAMA_DIR%\ollama.exe" goto add_path

:download
echo Downloading the Ollama installer from ollama.com...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest 'https://ollama.com/download/OllamaSetup.exe' -OutFile \"$env:TEMP\OllamaSetup.exe\""
if errorlevel 1 goto fail_install
echo Running the installer - follow its steps, then come back to this window.
"%TEMP%\OllamaSetup.exe"
if not exist "%OLLAMA_DIR%\ollama.exe" goto fail_install

:add_path
set "PATH=%OLLAMA_DIR%;%PATH%"

:have_ollama
rem Start Ollama if it isn't running yet (the installer normally starts it).
powershell -NoProfile -Command "try { Invoke-RestMethod http://localhost:11434/api/version -TimeoutSec 3 | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 goto running
echo Starting Ollama...
start "" /min ollama serve
powershell -NoProfile -Command "for ($i=0; $i -lt 30; $i++) { try { Invoke-RestMethod http://localhost:11434/api/version -TimeoutSec 2 | Out-Null; exit 0 } catch { Start-Sleep 1 } }; exit 1"
if errorlevel 1 goto fail_start

:running
set "MODEL=%~1"
if not "%MODEL%"=="" goto pull
set "RAM=16"
for /f %%m in ('powershell -NoProfile -Command "[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB)"') do set "RAM=%%m"
rem An 8B model needs a graphics card (NVIDIA) to be quick; without one qwen2.5:3b is about 3x faster.
set "MODEL=qwen2.5:3b"
where nvidia-smi >nul 2>&1 && if %RAM% GEQ 15 set "MODEL=llama3.1:8b"
echo.
echo This computer has %RAM% GB of RAM, so the chat model will be %MODEL%.

:pull
echo.
echo Downloading %MODEL% ...
ollama pull %MODEL%
if errorlevel 1 goto fail_pull
echo.
echo Downloading nomic-embed-text ...
ollama pull nomic-embed-text
if errorlevel 1 goto fail_pull

rem Tell the API Client which chat model to use.
set "RC_MODEL=%MODEL%"
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -c "import os; from restclient.config import load_settings, save_settings; s = load_settings(); s.ollama_chat_model = os.environ['RC_MODEL']; save_settings(s)"

echo.
echo  Done! Open the API Client and go to the Assistant tab.
echo  (If the app was already open, click the refresh button in the Local AI box.)
echo.
pause
exit /b 0

:fail_install
echo.
echo Ollama could not be installed automatically.
echo Download it from https://ollama.com/download, install it, then run this again.
pause
exit /b 1

:fail_start
echo.
echo Ollama is installed but isn't responding. Start "Ollama" from the Start menu, then run this again.
pause
exit /b 1

:fail_pull
echo.
echo The model download failed. Check your internet connection (or proxy) and run this again.
pause
exit /b 1
