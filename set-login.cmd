@echo off
rem Add or change the Operator/Admin login on the live server. Double-click, or:  set-login.cmd [-Role Operator] [-Username operator] [-Show]
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0set-login.ps1" %*
echo.
pause
