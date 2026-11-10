@echo off
setlocal EnableExtensions
title Relief Studio
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (set "PYEXE=py -3") else (set "PYEXE=python")

%PYEXE% -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo Python 3.10+ is required: https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

%PYEXE% -m pip install --user --quiet numpy pillow
%PYEXE% relief_gui.py
pause
