@echo off
rem Relay installer wrapper (Windows).
rem
rem Invokes install.ps1 with a process-local ExecutionPolicy Bypass so
rem fresh users are not blocked by the default PowerShell execution
rem policy. The policy change applies to this one process only and is
rem never persisted to the machine or user scope.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
exit /b %errorlevel%
