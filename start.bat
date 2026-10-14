@echo off
chcp 65001 >nul
REM Запуск из исходников (первый раз сам поставит зависимости)
cd /d "%~dp0"
if not exist .venv (
    py -3 -m venv .venv || python -m venv .venv
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)
start "" .venv\Scripts\pythonw.exe run.pyw %*
