@echo off
cd /d "%~dp0"
if exist "%~dp0dist\CallCenterLocal.exe" (
  "%~dp0dist\CallCenterLocal.exe"
  pause
  exit /b %errorlevel%
)
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or later was not found.
  echo Either install Python and enable "Add Python to PATH", or run build_windows_exe.bat on a Windows development PC first.
  pause
  exit /b 1
)
python --version
python server.py
pause
