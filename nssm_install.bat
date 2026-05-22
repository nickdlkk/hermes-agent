@echo off
REM ============================================================
REM Hermes Gateway NSSM 自动安装脚本
REM ============================================================
REM 需要以管理员权限运行

set NSSM_DIR=C:\tools
set NSSM_URL=https://nssm.cc/release/nssm-2.51.zip
set NSSM_ZIP=%TEMP%\nssm-2.51.zip
set SERVICE_NAME=HermesGateway
set HERMES_DIR=C:\Users\Administrator\AppData\Local\hermes\hermes-agent
set PYTHON_EXE=%HERMES_DIR%\venv\Scripts\python.exe
set GATEWAY_SCRIPT=%HERMES_DIR%\hermes_cli\main.py

echo ============================================================
echo  Hermes Gateway NSSM 安装脚本
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

REM 下载 NSSM
if not exist "%NSSM_DIR%\nssm.exe" (
    echo [1/4] 下载 NSSM...
    if not exist "%NSSM_DIR%" mkdir "%NSSM_DIR%"
    powershell.exe -Command "Invoke-WebRequest -Uri '%NSSM_URL%' -OutFile '%NSSM_ZIP%'"
    echo [2/4] 解压 NSSM...
    powershell.exe -Command "Expand-Archive -Path '%NSSM_ZIP%' -DestinationPath '%TEMP%\nssm' -Force"
    copy /Y "%TEMP%\nssm*\nssm-2.51\win64\nssm.exe" "%NSSM_DIR%\"
    del /F /Q "%NSSM_ZIP%"
    echo      完成!
) else (
    echo [1/4] NSSM 已安装, 跳过下载
    echo [2/4] NSSM 已安装, 跳过解压
)

REM 停止已存在的服务
echo [3/4] 配置 HermesGateway 服务...
"%NSSM_DIR%\nssm.exe" stop %SERVICE_NAME% >nul 2>&1
"%NSSM_DIR%\nssm.exe" remove %SERVICE_NAME% >nul 2>&1

REM 安装服务
"%NSSM_DIR%\nssm.exe" install %SERVICE_NAME% "%PYTHON_EXE%" "%GATEWAY_SCRIPT%" gateway
"%NSSM_DIR%\nssm.exe" set %SERVICE_NAME% AppDirectory "%HERMES_DIR%"
"%NSSM_DIR%\nssm.exe" set %SERVICE_NAME% AppEnvironment "HERMES_HOME=%HERMES_DIR%" "_HERMES_GATEWAY=1"
"%NSSM_DIR%\nssm.exe" set %SERVICE_NAME% DisplayName "Hermes Agent Gateway"
"%NSSM_DIR%\nssm.exe" set %SERVICE_NAME% Description "Hermes Agent Messaging Gateway"
"%NSSM_DIR%\nssm.exe" set %SERVICE_NAME% Start SERVICE_AUTO_START

REM 启动服务
echo [4/4] 启动服务...
"%NSSM_DIR%\nssm.exe" start %SERVICE_NAME%

REM 清理
rd /S /Q "%TEMP%\nssm" 2>nul

echo.
echo ============================================================
echo  安装完成！
echo ============================================================
echo.
echo 服务名称: %SERVICE_NAME%
"%NSSM_DIR%\nssm.exe" status %SERVICE_NAME%
echo.
echo 常用命令:
echo   C:\tools\nssm.exe edit %SERVICE_NAME%  - GUI配置
echo   C:\tools\nssm.exe stop %SERVICE_NAME%     - 停止
echo   C:\tools\nssm.exe start %SERVICE_NAME%    - 启动
echo   C:\tools\nssm.exe remove %SERVICE_NAME%   - 卸载
echo.
pause
