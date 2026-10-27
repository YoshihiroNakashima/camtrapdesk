@echo off
setlocal
cd /d "%~dp0"
set VENV=%~dp0.venv
set LOG=%~dp0camtrapdesk-setup.log

if exist "%VENV%\Scripts\pythonw.exe" goto :run

echo ==========================================================
echo  camtrapdesk 初回セットアップ
echo  必要なライブラリを取得します（数分かかります）
echo ==========================================================
set PY=
py -3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1 && set "PY=python"
if not defined PY python3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1 && set "PY=python3"
if not defined PY for %%D in ("%USERPROFILE%\miniconda3" "%USERPROFILE%\anaconda3" "%USERPROFILE%\miniforge3" "%USERPROFILE%\mambaforge" "%LOCALAPPDATA%\miniconda3" "%LOCALAPPDATA%\anaconda3" "%ProgramData%\miniconda3" "%ProgramData%\anaconda3") do (
    if not defined PY if exist "%%~D\python.exe" set "PY=%%~D\python.exe"
)
if not defined PY (
    echo Python 3.10 以上が見つかりません。
    echo https://www.python.org/downloads/ から Python をインストールし（Add python.exe to PATH にチェック）、
    echo もう一度この「起動.bat」を実行してください。
    pause
    exit /b 1
)
echo Python: %PY%
%PY% -m venv "%VENV%" > "%LOG%" 2>&1 || goto :fail
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip >> "%LOG%" 2>&1
"%VENV%\Scripts\python.exe" -m pip install -e . >> "%LOG%" 2>&1 || goto :fail
echo セットアップ完了。

:run
"%VENV%\Scripts\python.exe" -c "import camtrapdesk, PySide6, openpyxl" > "%LOG%" 2>&1 || goto :fail
start "" "%VENV%\Scripts\pythonw.exe" -m camtrapdesk
exit /b 0

:fail
echo.
echo セットアップに失敗しました。ログ: %LOG%
type "%LOG%"
pause
exit /b 1
