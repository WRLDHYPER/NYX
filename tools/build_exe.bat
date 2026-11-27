@echo off
rem ===========================================================================
rem  Builds NYX.exe (the app launcher, with the NYX icon) in the Nyx folder.
rem  Uses the C# compiler that ships with Windows - nothing extra to install.
rem  To redraw the icon first:  python tools\make_icon.py
rem ===========================================================================
setlocal
cd /d "%~dp0.."

set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" (
    echo Could not find the .NET Framework C# compiler ^(csc.exe^).
    exit /b 1
)
if not exist "assets\nyx.ico" (
    echo assets\nyx.ico is missing - run: python tools\make_icon.py
    exit /b 1
)

"%CSC%" /nologo /target:winexe /optimize+ /platform:anycpu ^
    /win32icon:assets\nyx.ico /reference:System.Windows.Forms.dll ^
    /out:NYX.exe tools\NyxLauncher.cs
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)
echo Built %CD%\NYX.exe
