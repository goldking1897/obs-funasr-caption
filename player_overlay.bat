@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Qwen-Audio Player Caption

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo  Please run start.bat first to create .venv and install dependencies.
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" player_overlay.py %*
echo.
echo Overlay stopped.
pause