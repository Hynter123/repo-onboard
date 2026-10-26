@echo off
chcp 65001 >nul
title RepoOnboard
cd /d "%~dp0"

echo ============================================
echo   RepoOnboard · 开源仓库结构地图
echo ============================================
echo.
echo   启动本地解析服务，稍后会自动打开浏览器。
echo   停止服务：关闭这个窗口，或按 Ctrl+C。
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" src\server.py
) else (
    echo [提示] 未找到 .venv，改用系统 python。
    python src\server.py
)

echo.
echo 服务已停止。
pause
