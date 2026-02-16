@echo off
echo ========================================
echo Building PriceTag Bridge Agent EXE
echo ========================================

python -m pip install --upgrade pip

python -m pip install PyInstaller

where PyInstaller
if %errorlevel% neq 0 (
    echo PyInstaller не найден. Используем python -m pyinstaller
    set PYINSTALLER=python -m PyInstaller
) else (
    set PYINSTALLER=PyInstaller
)

%PYINSTALLER% --onefile ^
    --name "pricetag-bridge" ^
    --add-data "config.yaml;." ^
    --hidden-import win32timezone ^
    --console ^
    bridge_agent.py

echo.
echo EXE created: dist\pricetag-bridge.exe
pause