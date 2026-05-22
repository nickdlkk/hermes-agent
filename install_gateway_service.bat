@echo off
REM ============================================================
REM Hermes Gateway Windows Service Installer
REM ============================================================
REM 需要以管理员权限运行
REM
REM 使用方式:
REM   install.bat    - 安装并启动服务
REM   uninstall.bat  - 停止并卸载服务
REM ============================================================

set SERVICE_NAME=HermesGateway
set SERVICE_DISPLAY=Hermes Agent Gateway
set HERMES_DIR=C:\Users\Administrator\AppData\Local\hermes\hermes-agent
set HERMES_HOME=C:\Users\Administrator\AppData\Local\hermes
set PYTHON_EXE=%HERMES_DIR%\venv\Scripts\python.exe
set GATEWAY_SCRIPT=%HERMES_DIR%\hermes_cli\main.py

echo ============================================================
echo  Hermes Gateway Windows Service 安装脚本
echo ============================================================
echo.

REM 检查管理员权限
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 请以管理员身份运行此脚本！
    echo 右键 -> 以管理员身份运行
    pause
    exit /b 1
)

REM 检查 Python
if not exist "%PYTHON_EXE%" (
    echo [错误] 找不到 Python: %PYTHON_EXE%
    pause
    exit /b 1
)

REM 创建服务
echo [1/3] 创建 Windows 服务...
sc create %SERVICE_NAME% binPath= "\"%PYTHON_EXE%\" \"%GATEWAY_SCRIPT%\" gateway" DisplayName= "%SERVICE_DISPLAY%" start= auto
sc description %SERVICE_NAME% "Hermes Agent Messaging Gateway"

REM 设置环境变量
echo [2/3] 配置服务参数...
sc config %SERVICE_NAME% obj= "NT AUTHORITY\LocalService"

REM 创建日志目录
if not exist "%HERMES_HOME%\logs" mkdir "%HERMES_HOME%\logs"

REM 启动服务
echo [3/3] 启动服务...
net start %SERVICE_NAME%

echo.
echo ============================================================
echo  安装完成！
echo ============================================================
echo.
echo 服务名称: %SERVICE_NAME%
echo 状态: 运行中
echo.
echo 常用命令:
echo   net stop %SERVICE_NAME%   - 停止服务
echo   net start %SERVICE_NAME%  - 启动服务
echo   sc delete %SERVICE_NAME%  - 删除服务
echo.
pause
