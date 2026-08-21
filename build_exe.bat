@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   打包 会话转 MD 为单文件 EXE
echo ============================================
echo.

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "会话转MD" ^
  --add-data "web;web" ^
  --collect-all webview ^
  --hidden-import clr ^
  app.py

echo.
if exist "dist\会话转MD.exe" (
  echo 打包完成：dist\会话转MD.exe
) else (
  echo 打包失败，请查看上方日志。
)
pause
