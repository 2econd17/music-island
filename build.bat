@echo off
chcp 65001 >nul
REM Сборка одного файла dist\MusicIsland.exe
setlocal
cd /d "%~dp0"
if not exist .venv (
    py -3 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller || goto :error
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name MusicIsland ^
  --icon assets\icon.ico ^
  --collect-submodules winrt ^
  --hidden-import pyaudiowpatch ^
  run.pyw || goto :error
echo.
echo Готово: dist\MusicIsland.exe
pause
exit /b 0
:error
echo Сборка не удалась
pause
exit /b 1
