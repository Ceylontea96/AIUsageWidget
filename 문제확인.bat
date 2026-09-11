@echo off
cd /d "%~dp0"
echo FOLDER:
echo %CD%
echo.
if not exist "%~dp0setup_and_run.ps1" (
    echo Missing setup_and_run.ps1
    echo Unzip to a folder first.
    pause
    exit /b 1
)
if not exist "%~dp0usage_widget.py" (
    echo Missing usage_widget.py
    echo Unzip to a folder first.
    pause
    exit /b 1
)
mkdir "%APPDATA%\AiUsageWidget" 2>nul
echo %date% %time% bat start %CD%>> "%APPDATA%\AiUsageWidget\launch.log"
echo LOG:
echo %APPDATA%\AiUsageWidget\launch.log
echo.
echo Starting widget. Do not close this window.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_and_run.ps1"
echo.
echo EXIT:%ERRORLEVEL%
pause
