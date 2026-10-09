@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python Launcher was not found. Install Python 3.10+ first.
  pause
  exit /b 1
)
py -m pip install --upgrade pyinstaller
if errorlevel 1 goto :error
py -m PyInstaller --noconfirm --clean --onefile --name CallCenterLocal --add-data "static;static" server.py
if errorlevel 1 goto :error
echo.
echo Build complete: dist\CallCenterLocal.exe
echo Copy the EXE to the administrator PC and run it. Data is stored under %%LOCALAPPDATA%%\CallCenterLocal\data.
pause
exit /b 0
:error
echo Build failed. Check the output above.
pause
exit /b 1
