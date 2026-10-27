@echo off
chcp 65001 >nul
title campus_tools_mcp - MCP Server (StreamableHTTP)
cd /d "%~dp0"
echo ============================================
echo   campus_tools_mcp 校园工具 MCP 服务器
echo   地址: http://127.0.0.1:8310/mcp
echo ============================================
echo 依赖检查安装中（首次运行需要几分钟）...
python -m pip install -r requirements.txt -q --disable-pip-version-check
echo.
echo 启动服务，按 Ctrl+C 停止...
python server.py
pause
