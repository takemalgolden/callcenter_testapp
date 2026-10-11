@echo off
setlocal
net session >nul 2>&1
if errorlevel 1 (
  echo Please right-click this file and select "Run as administrator".
  pause
  exit /b 1
)
echo This creates a Windows Firewall rule for TCP port 8765 from LocalSubnet only.
echo Confirm this is allowed by your company's IT policy before continuing.
choice /M "Create the LAN-only firewall rule"
if errorlevel 2 exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -Command "$old=Get-NetFirewallRule -DisplayName 'CallCenterLocal LAN only' -ErrorAction SilentlyContinue; if($old){$old | Remove-NetFirewallRule}; New-NetFirewallRule -DisplayName 'CallCenterLocal LAN only' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -RemoteAddress LocalSubnet -Profile Domain,Private | Out-Null"
if errorlevel 1 (
  echo Failed to create the rule. Ask your IT administrator.
  pause
  exit /b 1
)
echo Rule created. It allows TCP 8765 from LocalSubnet on Domain/Private profiles.
echo This does not hide the server IP from network discovery and may not match all company networks.
pause
