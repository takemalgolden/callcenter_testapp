@echo off
setlocal
chcp 65001 >nul
echo Create a desktop shortcut for the Call Center Break System.
echo Enter the administrator PC device name, not its IP address.
set /p SERVERNAME=Administrator PC device name: 
if "%SERVERNAME%"=="" (
  echo A device name is required.
  pause
  exit /b 1
)
set /p PORT=Port number [8765]: 
if "%PORT%"=="" set PORT=8765
set "SHORTCUT=%USERPROFILE%\Desktop\Break Management System.url"
(
  echo [InternetShortcut]
  echo URL=http://%SERVERNAME%:%PORT%/
) > "%SHORTCUT%"
echo.
echo Created: "%SHORTCUT%"
echo If the page does not open, the company network may not resolve PC names.
echo Ask IT whether an internal DNS name can be provided.
pause
