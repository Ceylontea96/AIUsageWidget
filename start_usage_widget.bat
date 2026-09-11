@echo off
cd /d "%~dp0"
if not exist "%~dp0usage_widget.py" (
    echo zip 안에서 실행하지 말고, 먼저 폴더로 압축을 푸세요.
    echo 그다음 start_usage_widget.vbs 를 더블클릭하세요.
    pause
    exit /b 1
)
start "" wscript.exe "%~dp0start_usage_widget.vbs"
exit /b 0
