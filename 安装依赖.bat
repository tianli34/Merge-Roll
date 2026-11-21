@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

echo 使用解释器: %PY%
%PY% -c "import tkinter" 2>nul
if errorlevel 1 (
    echo [错误] 当前 Python 没有 tkinter，请安装 python.org 官方 Windows 版。
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo 正在创建虚拟环境 .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
)

echo 正在安装 Playwright ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 安装失败，可尝试国内镜像：
    echo   .venv\Scripts\python.exe -m pip install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
)

echo.
echo 尝试安装 Chromium 内核（默认用 Edge，失败也不影响使用）...
".venv\Scripts\python.exe" -m playwright install chromium

echo.
echo 安装完成，双击“启动.bat”运行。
pause
