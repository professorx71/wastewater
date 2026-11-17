@echo off
rem Release and deploy the Wastewater Dashboard. Double-click, or run:  release.cmd [-DryRun] [-NoDeploy] [-DeployOnly] [-Notes "..."]
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0release.ps1" %*
echo.
pause
