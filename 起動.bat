@echo off
rem camtrapdesk launcher (ASCII only; all messages are printed by tools\launch.py)
setlocal
cd /d "%~dp0"
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set "PY=python3"
if not defined PY for %%D in ("%USERPROFILE%\miniconda3" "%USERPROFILE%\anaconda3" "%USERPROFILE%\miniforge3" "%USERPROFILE%\mambaforge" "%LOCALAPPDATA%\miniconda3" "%LOCALAPPDATA%\anaconda3" "%ProgramData%\miniconda3" "%ProgramData%\anaconda3") do (
    if not defined PY if exist "%%~D\python.exe" set "PY=%%~D\python.exe"
)
if not defined PY (
    echo Python 3.10 or newer was not found.
    echo Install Python from https://www.python.org/downloads/ ^(check "Add python.exe to PATH"^), then run this file again.
    start "" https://www.python.org/downloads/
    pause
    exit /b 1
)
%PY% -X utf8 "%~dp0tools\launch.py"
if errorlevel 1 (
    pause
    exit /b 1
)
exit /b 0
