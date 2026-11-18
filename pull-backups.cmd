@echo off
rem Copy the newest nightly database backup from the live server to this PC. Double-click, or:  pull-backups.cmd [-Dest D:\Backups\Wastewater]
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pull-backups.ps1" %*
echo.
pause
