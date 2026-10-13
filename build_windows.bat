@echo off
REM Muse Bridge - Windows build script (THE single packaging step).
REM Run this on your Windows PC: double-click build_windows.bat
REM Requires: Python 3.10+ from python.org (check "Add python.exe to PATH").
REM The finished EXE needs no Python - this script is only for building it.
setlocal

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install Python 3.10+ from https://www.python.org/downloads/
  echo and check "Add python.exe to PATH" during install. Then run this again.
  pause
  exit /b 1
)

echo Installing build tools...
python -m pip install --upgrade pip
python -m pip install pyinstaller pywinauto

echo Building MuseBridge.exe ...
echo Cleaning stale build output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name MuseBridge ^
  main.py

if errorlevel 1 (
  echo BUILD FAILED. See output above.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  DONE: dist\MuseBridge.exe
echo  Double-click it to start. No Python needed afterwards.
echo  No API keys. No fees. Just your ChatGPT + Muse windows.
echo ============================================================
pause
