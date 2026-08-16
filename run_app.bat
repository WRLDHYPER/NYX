@echo off
cd /d "C:\EDITH"
py -3.11 "C:\EDITH\app_gui.py"
if errorlevel 1 pause