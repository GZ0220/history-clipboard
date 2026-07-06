@echo off
setlocal
cd /d "%~dp0"

echo [1/4] Checking PyInstaller...
python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo PyInstaller is not installed.
    echo Run this first:
    echo   python -m pip install -r requirements-dev.txt
    pause
    exit /b 1
)

echo [2/4] Building lightweight portable app...
python -m PyInstaller 历史粘贴板.spec --noconfirm --clean
if errorlevel 1 (
    echo Main app build failed.
    pause
    exit /b 1
)

echo [3/4] Building optional OCR component...
python -m PyInstaller ocr_worker.spec --noconfirm --clean --distpath dist\OCR组件
if errorlevel 1 (
    echo OCR component build failed.
    pause
    exit /b 1
)

echo [4/4] Done.
if exist "dist\历史粘贴板\clipboard_history_config.json" del /q "dist\历史粘贴板\clipboard_history_config.json"
echo Portable app folder:
echo   %~dp0dist\历史粘贴板
echo.
echo Optional OCR component:
echo   %~dp0dist\OCR组件\ocr_runtime
echo.
echo Send the whole "dist\历史粘贴板" folder to another Windows computer.
echo To enable OCR, copy "ocr_runtime" or the whole "OCR组件" folder next to 历史粘贴板.exe.
pause
