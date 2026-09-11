@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0usage_widget.py" (
    echo zip 안에서 실행하지 말고, 먼저 폴더로 압축을 푸세요.
    echo 그다음 start_usage_widget.vbs 또는 이 bat 파일을 더블클릭하세요.
    pause
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_and_run.ps1"
if errorlevel 1 (
    echo 실행에 실패했습니다. 위 안내를 확인하세요.
    pause
)
