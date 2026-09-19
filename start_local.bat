@echo off
chcp 65001 >nul
title 外贸询盘助手 - 本地服务启动
cd /d "%~dp0"
echo ===================================================
echo   外贸询盘助手 - 本地服务启动
echo   (自动挂载同级保密数据金库: 外贸询盘助手-保密数据)
echo ===================================================
echo.

if exist ".venv\Scripts\python.exe" (
    set "PY_EXE=.venv\Scripts\python.exe"
) else (
    set "PY_EXE=python"
)

"%PY_EXE%" serve.py
pause
