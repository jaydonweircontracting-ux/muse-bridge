@echo off
REM Muse Bridge - Windows build script (THE single packaging step).
REM Run this on your Windows PC: double-click build_windows.bat
REM Requires: Python 3.10+ from python.org (check "Add python.exe to PATH").
setlocal

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install Python 3.10+ from https://www.python.org/downloads/
  echo and check "Add python.exe to PATH" during install. Then run this again.
  pause
  exit /b 1
)

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install pyinstaller pywinauto pyautogui

echo Building MuseBridge.exe ...
python -m PyInstaller --noconfirm --onefile --windowed ^
  --name MuseBridge ^
  --add-data "README.md;." ^
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
echo ============================================================
pause
