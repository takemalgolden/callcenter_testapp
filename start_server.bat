@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0dist\CallCenterLocal.exe" (
  "%~dp0dist\CallCenterLocal.exe"
  pause
  exit /b %errorlevel%
)
if exist "%~dp0CallCenterLocal.exe" (
  "%~dp0CallCenterLocal.exe"
  pause
  exit /b %errorlevel%
)
echo Python-free runtime EXE was not found.
echo.
echo On a Windows build PC, install Python 3.10+ and internet access,
echo then run build_windows_exe.bat once. Copy dist\CallCenterLocal.exe here
echo or keep it in dist\CallCenterLocal.exe.
echo After the EXE is built, the administrator PC does not need Python.
echo.
pause
exit /b 1
