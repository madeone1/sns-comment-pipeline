@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 (
  echo Cannot open the folder that contains this file.
  pause
  exit /b 1
)
rem UTF-8 console and Python stdio. Korean messages are printed by the ps1.
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0change-access-token.ps1" %*
set "RESULT=%ERRORLEVEL%"
exit /b %RESULT%
