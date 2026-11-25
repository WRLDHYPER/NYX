@echo off
cd /d "%~dp0"
py -3.11 "%~dp0assistant.py"
if errorlevel 1 pause
