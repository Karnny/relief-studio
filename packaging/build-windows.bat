@echo off
setlocal EnableExtensions
title Relief Studio - Windows build
cd /d "%~dp0.."

echo.
echo   Relief Studio - standalone Windows build
echo   ---------------------------------------
echo.

rem --- find a Python 3.10+ interpreter -------------------------------------
where py >nul 2>nul
if not errorlevel 1 (
  set "PYEXE=py -3"
  goto :check
)
where python >nul 2>nul
if not errorlevel 1 (
  set "PYEXE=python"
  goto :check
)
goto :nopython

:check
%PYEXE% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 goto :oldpython

rem --- build environment ----------------------------------------------------
if not exist ".venv-build" (
  echo Creating build environment...
  %PYEXE% -m venv .venv-build
  if errorlevel 1 goto :fail
)
call ".venv-build\Scripts\activate.bat"

echo Installing build dependencies...
python -m pip install --upgrade pip --quiet
python -m pip install numpy pillow pyinstaller --quiet
if errorlevel 1 goto :fail

echo Building the executable (this takes a minute or two)...
python -m PyInstaller ReliefStudio.spec --noconfirm --clean
if errorlevel 1 goto :fail

echo.
echo   Done.
echo   Executable: "%CD%\dist\ReliefStudio.exe"
echo.
echo   Double-click it: the browser opens at http://127.0.0.1:8765/
echo   Close the console window that appears to stop the server.
echo.
pause
exit /b 0

:nopython
echo.
echo   Python 3.10 or newer was not found.
echo   1. Download it from https://www.python.org/downloads/windows/
echo   2. During setup tick "Add python.exe to PATH"
echo   3. Run this file again.
echo.
pause
exit /b 1

:oldpython
echo.
echo   Your Python is older than 3.10 (numpy 2.x needs 3.10+).
echo   Install a current version from https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:fail
echo.
echo   Build failed - see the messages above.
echo   Common causes: no network access for pip, antivirus blocking PyInstaller,
echo   or a half-created .venv-build (delete it and rerun).
echo.
pause
exit /b 1
