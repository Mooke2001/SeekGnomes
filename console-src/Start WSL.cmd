@echo off
wsl.exe --cd "%~dp0." bash ./Start.command
if errorlevel 1 pause
