@echo off
cd /d "%~dp0"
rem pythonw = Python without a console window. Errors go to nyx_app.log.
rem Uses the environment made by setup.exe if it exists, else Python 3.11.
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0nyx_app.py"
) else (
    start "" pyw -3.11 "%~dp0nyx_app.py"
)
