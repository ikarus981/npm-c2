@echo off
rem Wrapper so the console runs from Explorer or cmd without touching the
rem PowerShell execution policy. Arguments are passed straight through.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0c2.ps1" %*
