@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python Launcher was not found. Install Python 3.10+ on this build PC.
  pause
  exit /b 1
)
py -3 -c "import sys; assert sys.version_info >= (3,10), 'Python 3.10 or newer is required'"
if errorlevel 1 goto :error
py -3 -m pip --version >nul 2>nul
if errorlevel 1 (
  echo pip not found. Trying ensurepip...
  py -3 -m ensurepip --upgrade
  if errorlevel 1 goto :error
)
py -3 -m pip install --upgrade pyinstaller
if errorlevel 1 goto :error
py -3 -m PyInstaller --noconfirm --clean --onefile --name CallCenterLocal --add-data "static;static" server.py
if errorlevel 1 goto :error
if not exist "dist\CallCenterLocal.exe" goto :error
echo.
echo Build complete: dist\CallCenterLocal.exe
echo To launch this application run start_server.bat.
pause
exit /b 0
:error
echo.
echo [ERROR] Build failed. Check the output above.
pause
exit /b 1
