@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  株価アラート セットアップ
echo ============================================
echo.

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [エラー] Python が見つかりません。
    echo README.md の「1. Python をインストールする」の手順で Python をインストールしてから、
    echo もう一度このファイルをダブルクリックしてください。
    echo.
    pause
    exit /b 1
)

echo Python の仮想環境を作成しています...
%PY% -m venv .venv
if errorlevel 1 (
    echo [エラー] 仮想環境を作成できませんでした。
    pause
    exit /b 1
)

echo 必要なライブラリをインストールしています(数分かかることがあります)...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [エラー] ライブラリをインストールできませんでした。インターネット接続を確認してください。
    pause
    exit /b 1
)

echo.
echo セットアップが完了しました。start.bat をダブルクリックするとアプリが起動します。
echo.
pause
