@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-webgis.ps1" -OpenBrowser
if errorlevel 1 (
  echo.
  echo Startup failed. See tmp\logs for details.
  pause
)
