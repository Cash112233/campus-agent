@echo off
chcp 65001 >nul
title 校园小助手 Campus Agent
cd /d "%~dp0backend"

where python >nul 2>nul
if %errorlevel%==0 (
  set PY=python
) else (
  where py >nul 2>nul
  if %errorlevel%==0 (
    set PY=py
  ) else (
    echo 未检测到 Python，请先安装 Python 3.10+ 再运行本脚本。
    pause
    exit /b 1
  )
)

echo ============================================
echo   校园小助手 - Campus Agent
echo ============================================
echo.
echo [1/2] 检查并安装依赖，首次运行需要几分钟，请耐心等待...
%PY% -m pip install -r requirements.txt -q --disable-pip-version-check

echo.
echo [2/2] 启动服务，浏览器将自动打开 http://127.0.0.1:8300 ...
echo       按 Ctrl+C 可停止服务
echo.
%PY% main.py
pause
