@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 会话转 MD ...
python app.py
if errorlevel 1 (
  echo.
  echo 启动失败，请确认已安装 Python 与依赖：pip install -r requirements.txt
  pause
)
