@echo off
title OmniAgent-CLI
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8

python -m omniagent.main %*
set EXIT_CODE=%ERRORLEVEL%

:: 非交互模式（--goal）不暂停，直接退出
echo %* | find "--goal" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    exit /b %EXIT_CODE%
)

echo.
if %EXIT_CODE% NEQ 0 (
    echo [ERROR] Exit code: %EXIT_CODE%
    pause
)
