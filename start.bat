@echo off
REM One-click launcher for DAAgent. Delegates to start.ps1 (UTF-8).
REM Usage: start.bat [dev|backend|frontend|prod]
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "EXITCODE=%ERRORLEVEL%"
pause
exit /b %EXITCODE%
