@echo off
setlocal
title 工业模型查看器 - 启动器

echo 正在检查环境...

set VENV_PATH=.venv
set PYTHON_EXE=%VENV_PATH%\Scripts\python.exe

if not exist "%PYTHON_EXE%" (
    echo [错误] 未找到虚拟环境！正在尝试使用系统 Python 创建...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 无法创建虚拟环境，请确保已安装 Python。
        pause
        exit /b
    )
    set PYTHON_EXE=.venv\Scripts\python.exe
)

echo 正在检查并更新依赖库 (requirements.txt)...
"%PYTHON_EXE%" -m pip install -r requirements.txt --quiet

echo.
echo ==========================================
echo    工业模型查看器 (ShipViewer) 正在启动...
echo ==========================================
echo.

"%PYTHON_EXE%" main.py

if errorlevel 1 (
    echo.
    echo [提示] 程序异常退出。
    pause
)

endlocal
