@echo off
cd /d "%~dp0"
rem Uses the environment made by setup.exe if it exists, else Python 3.11.
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0assistant.py"
) else (
    py -3.11 "%~dp0assistant.py"
)
if errorlevel 1 pause
