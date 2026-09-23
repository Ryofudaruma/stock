@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo 先に setup.bat をダブルクリックしてセットアップしてください。
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0app.pyw"
