@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title OBS Qwen-Audio Realtime Caption
set "VENV_PY=.venv\Scripts\python.exe"

if exist "%VENV_PY%" goto check_deps

echo [1/3] Creating Python virtual environment...
where py >nul 2>nul
if errorlevel 1 goto plain_python
py -3.13 -m venv .venv 2>nul
if exist "%VENV_PY%" goto check_python
py -3 -m venv .venv 2>nul
goto check_python

:plain_python
python -m venv .venv

:check_python
if not exist "%VENV_PY%" (
  echo.
  echo  ERROR: Python 3 was not found.
  echo  Install Python 3.10+ and enable "Add Python to PATH".
  echo  https://www.python.org/downloads/windows/
  echo.
  pause
  exit /b 1
)

:check_deps
"%VENV_PY%" -c "import pyaudiowpatch, numpy, websocket" >nul 2>nul
if not errorlevel 1 goto run

echo [2/3] Installing dependencies (first run only)...
"%VENV_PY%" -m pip install --disable-pip-version-check -q -U pip
"%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo.
  echo  ERROR: dependency installation failed.
  echo  Try the Tsinghua mirror:
  echo    .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  echo.
  pause
  exit /b 1
)

:run
echo [3/3] Starting caption service...
echo.
"%VENV_PY%" main.py --open %*
echo.
echo Service stopped.
pause
