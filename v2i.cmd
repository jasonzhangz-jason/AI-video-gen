@echo off
rem v2i 启动器：不带参数双击 = 起 Web GUI；带参数 = 当 CLI 用
setlocal
chcp 65001 >nul
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%vendor;%ROOT%"
set "PYTHONUTF8=1"
set "PYEXE="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3"
if not defined PYEXE (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
  echo [v2i] Python not found. Install Python 3.10+ first.
  exit /b 2
)
if "%~1"=="" (
  %PYEXE% -m v2i web
) else (
  %PYEXE% -m v2i %*
)

