@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 현재 폴더:
echo %CD%
echo.
if not exist "%~dp0setup_and_run.ps1" (
    echo setup_and_run.ps1 이 없습니다.
    echo zip 창 안에서 실행하지 말고, 먼저 폴더로 압축을 푸세요.
    pause
    exit /b 1
)
if not exist "%~dp0usage_widget.py" (
    echo usage_widget.py 가 없습니다. zip을 폴더로 다시 푸세요.
    pause
    exit /b 1
)
mkdir "%APPDATA%\AiUsageWidget" 2>nul
echo %date% %time% bat start %CD%>> "%APPDATA%\AiUsageWidget\launch.log"
echo launch.log 위치:
echo %APPDATA%\AiUsageWidget\launch.log
echo.
echo 위젯을 시작합니다. 오류가 나오면 이 창을 캡처하세요.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_and_run.ps1"
echo.
echo 종료 코드: %ERRORLEVEL%
echo 끝나면 아무 키나 누르세요.
pause
