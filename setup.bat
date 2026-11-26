@echo off
rem ===========================================================================
rem  NYX setup - downloads and installs everything Nyx needs.
rem  Installs into the folder this file is in. Safe to run again: anything
rem  already done is skipped, so it also works as a "repair".
rem ===========================================================================
setlocal
cd /d "%~dp0"
title NYX Setup

rem ---- Settings you can change ----------------------------------------------
set "PYTHON_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
set "OLLAMA_URL=https://ollama.com/download/OllamaSetup.exe"
set "VOICE_NAME=en_GB-vctk-medium"
set "VOICE_URL=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/vctk/medium"
set "OLLAMA_MODELS=dolphin-llama3:8b"
set "SMART_MODEL=qwen2.5:14b"
set "WHISPER_GPU_MODEL=large-v3"
set "WHISPER_CPU_MODEL=small"
rem ---------------------------------------------------------------------------

set "APPDIR=%~dp0"
set "VENV=%APPDIR%.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "VPYW=%VENV%\Scripts\pythonw.exe"
set "DL=%TEMP%\nyx-setup"
if not exist "%DL%" mkdir "%DL%"

echo.
echo   NYX setup
echo   ---------
echo   This downloads and installs everything Nyx needs into:
echo     %APPDIR%
echo.
echo     - Python 3.11 (if missing) and the Python packages
echo     - NVIDIA CUDA libraries (only if you have an NVIDIA card)
echo     - Whisper speech-recognition model (~3 GB)
echo     - Ollama + AI model %OLLAMA_MODELS% (~5 GB)
echo     - Piper voice %VOICE_NAME% (~100 MB)
echo.
echo   Total download: roughly 9-10 GB. It can take a while.
echo.

rem ---- Optional smart model -------------------------------------------------
set "SMART_HINT=not recommended: no NVIDIA card found, it would be very slow"
nvidia-smi -L >nul 2>&1 && set "SMART_HINT=recommended: NVIDIA card found"
echo   Optional: smart model %SMART_MODEL% (~9 GB more).
echo   Nyx uses it for long questions and deep mode. It gives better answers
echo   but needs a strong graphics card. [%SMART_HINT%]
echo.
set "SMART_ANSWER="
set /p "SMART_ANSWER=  Install the smart model? (y/N) "
if /i "%SMART_ANSWER%"=="y" set "OLLAMA_MODELS=%OLLAMA_MODELS% %SMART_MODEL%"
echo.
echo   AI models to download: %OLLAMA_MODELS%
echo.
pause

where curl.exe >nul 2>&1 || call :fail "curl.exe not found. Windows 10 (1803) or newer is needed."
if errorlevel 1 exit /b 1

rem ===========================================================================
rem  1. Python 3.11
rem ===========================================================================
call :step "Checking for Python 3.11"
call :find_python
if defined PY goto :have_python
echo     Python 3.11 not found. Downloading it...
call :download "%PYTHON_URL%" "%DL%\python-3.11.9-amd64.exe" || exit /b 1
echo     Installing Python 3.11 (just for this user)...
"%DL%\python-3.11.9-amd64.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 InstallLauncherAllUsers=0 Include_tcltk=1 Include_pip=1 Include_test=0
if errorlevel 1 call :fail "The Python installer failed." & exit /b 1
call :find_python
if not defined PY call :fail "Python 3.11 was installed but can't be found." & exit /b 1
:have_python
echo     Using %PY%

rem ===========================================================================
rem  2. Nyx's own Python environment + packages
rem ===========================================================================
call :step "Setting up Nyx's Python environment (.venv)"
if not exist "%VPY%" "%PY%" -m venv "%VENV%"
if not exist "%VPY%" call :fail "Could not create the Python environment." & exit /b 1
"%VPY%" -c "import tkinter" >nul 2>&1
if errorlevel 1 call :fail "This Python has no tkinter. Reinstall Python 3.11 with 'tcl/tk and IDLE' ticked." & exit /b 1

call :step "Installing Python packages (this can take several minutes)"
"%VPY%" -m pip install --upgrade pip --disable-pip-version-check
if errorlevel 1 call :fail "Upgrading pip failed." & exit /b 1
"%VPY%" -m pip install -r "%APPDIR%requirements.txt" --disable-pip-version-check
if errorlevel 1 call :fail "Installing the Python packages failed." & exit /b 1

set "GPU="
nvidia-smi -L >nul 2>&1 && set "GPU=1"
if not defined GPU goto :no_gpu
call :step "NVIDIA card found - installing CUDA libraries (~1 GB)"
"%VPY%" -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9,<10" --disable-pip-version-check
if errorlevel 1 call :fail "Installing the CUDA libraries failed." & exit /b 1
set "WHISPER=%WHISPER_GPU_MODEL%"
goto :whisper
:no_gpu
call :step "No NVIDIA card found - speech recognition will run on the CPU"
set "WHISPER=%WHISPER_CPU_MODEL%"

rem ===========================================================================
rem  3. Whisper model
rem ===========================================================================
:whisper
call :step "Downloading Whisper model '%WHISPER%'"
"%VPY%" -c "from faster_whisper import download_model; print(download_model('%WHISPER%'))"
if errorlevel 1 call :fail "Downloading the Whisper model failed." & exit /b 1

rem ===========================================================================
rem  4. Piper voice
rem ===========================================================================
call :step "Downloading Piper voice %VOICE_NAME%"
if not exist "%APPDIR%voices" mkdir "%APPDIR%voices"
if not exist "%APPDIR%voices\%VOICE_NAME%.onnx" call :download "%VOICE_URL%/%VOICE_NAME%.onnx" "%APPDIR%voices\%VOICE_NAME%.onnx" || exit /b 1
if not exist "%APPDIR%voices\%VOICE_NAME%.onnx.json" call :download "%VOICE_URL%/%VOICE_NAME%.onnx.json" "%APPDIR%voices\%VOICE_NAME%.onnx.json" || exit /b 1
echo     Voice ready.

rem ===========================================================================
rem  5. Ollama + AI models
rem ===========================================================================
call :step "Checking for Ollama"
call :find_ollama
if defined OLLAMA goto :have_ollama
echo     Ollama not found. Downloading it...
call :download "%OLLAMA_URL%" "%DL%\OllamaSetup.exe" || exit /b 1
echo     Installing Ollama...
"%DL%\OllamaSetup.exe" /VERYSILENT /NORESTART /SUPPRESSMSGBOXES
if errorlevel 1 call :fail "The Ollama installer failed." & exit /b 1
call :find_ollama
if not defined OLLAMA call :fail "Ollama was installed but can't be found." & exit /b 1
:have_ollama
echo     Using %OLLAMA%

curl.exe -s -o nul http://127.0.0.1:11434/api/version && goto :ollama_running
echo     Starting Ollama (the first start can take a minute)...
rem  Prefer the tray app: it runs the server in the background like normal.
for %%D in ("%OLLAMA%") do set "OLLAMA_APP=%%~dpDollama app.exe"
if exist "%OLLAMA_APP%" (start "" "%OLLAMA_APP%") else (start "Ollama" /min "%OLLAMA%" serve)
set /a TRIES=0
:wait_ollama
timeout /t 2 /nobreak >nul
curl.exe -s -o nul http://127.0.0.1:11434/api/version && goto :ollama_running
set /a TRIES+=1
if %TRIES% lss 60 goto :wait_ollama
call :fail "Ollama did not start. Run 'ollama serve' by hand, then run setup again." & exit /b 1
:ollama_running

for %%M in (%OLLAMA_MODELS%) do (
    call :step "Downloading AI model %%M"
    "%OLLAMA%" pull %%M
    if errorlevel 1 call :fail "Downloading %%M failed." & exit /b 1
)

rem ===========================================================================
rem  6. Shortcuts (Desktop + Start Menu)
rem ===========================================================================
call :step "Creating shortcuts"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "foreach ($dir in [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs')) {" ^
  "  $s = $ws.CreateShortcut((Join-Path $dir 'NYX.lnk'));" ^
  "  $s.TargetPath = $env:VPYW;" ^
  "  $s.Arguments = '\"' + $env:APPDIR + 'nyx_app.py\"';" ^
  "  $s.WorkingDirectory = $env:APPDIR;" ^
  "  $s.Description = 'NYX voice assistant';" ^
  "  $s.Save() }"
if errorlevel 1 (echo     Could not create shortcuts - use run_app.bat instead.) else (echo     Desktop and Start Menu shortcuts created.)

rmdir /s /q "%DL%" >nul 2>&1

echo.
echo   All done! Start Nyx with the NYX shortcut on your desktop, or run_app.bat.
echo.
set "RUN="
set /p "RUN=Start Nyx now? (Y/n) "
if /i "%RUN%"=="n" exit /b 0
start "" "%VPYW%" "%APPDIR%nyx_app.py"
exit /b 0


rem ===========================================================================
rem  Helpers
rem ===========================================================================
:step
echo.
echo ==^> %~1
exit /b 0

:download
rem  %1 = url, %2 = destination file
curl.exe -L --fail --retry 3 -# -o "%~2.part" "%~1"
if errorlevel 1 (
    del "%~2.part" >nul 2>&1
    call :fail "Could not download %~1 - check your internet connection."
    exit /b 1
)
move /y "%~2.part" "%~2" >nul
exit /b 0

:find_python
set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if defined PY exit /b 0
if exist "%ProgramFiles%\Python311\python.exe" set "PY=%ProgramFiles%\Python311\python.exe"
if defined PY exit /b 0
for /f "delims=" %%I in ('py -3.11 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%I"
exit /b 0

:find_ollama
set "OLLAMA="
for /f "delims=" %%I in ('where ollama.exe 2^>nul') do if not defined OLLAMA set "OLLAMA=%%I"
if defined OLLAMA exit /b 0
if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
exit /b 0

:fail
echo.
echo SETUP FAILED: %~1
echo.
pause
exit /b 1
