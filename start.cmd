@echo off
rem v2i 一键启动：缺依赖自动装 -> 起本地 Web GUI -> 自动开浏览器
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0vendor;%~dp0"
set "PYTHONUTF8=1"

set "PYEXE="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3"
if not defined PYEXE (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
  echo [v2i] 没找到 Python，请先安装 Python 3.10+ 再双击本文件。
  pause
  exit /b 2
)

if not exist "vendor\faster_whisper" (
  echo [v2i] 首次运行，正在安装依赖（约 250MB，走阿里云源）...
  %PYEXE% -m pip install --target "vendor" --upgrade -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
  if errorlevel 1 (
    echo [v2i] 依赖安装失败：检查网络后重试，或按 README 手动安装。
    pause
    exit /b 1
  )
)

if not exist ".env" copy /y ".env.example" ".env" >nul

echo [v2i] 启动 Web GUI ... 关闭本窗口即停止服务
%PYEXE% -m v2i web
pause