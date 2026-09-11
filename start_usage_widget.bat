@echo off
cd /d "%~dp0"
if not exist "%~dp0usage_widget.py" (
    echo Missing usage_widget.py
    echo Unzip to a folder first.
    pause
    exit /b 1
)
start "" wscript.exe "%~dp0start_usage_widget.vbs"
exit /b 0
