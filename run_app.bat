@echo off
cd /d "%~dp0"
rem pyw = Python without a console window. Errors go to nyx_app.log.
start "" pyw -3.11 "%~dp0nyx_app.py"
