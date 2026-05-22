@echo off
REM ============================================================
REM Hermes Gateway NSSM 卸载脚本
REM ============================================================
REM 需要以管理员权限运行

set NSSM_DIR=C:\tools
set SERVICE_NAME=HermesGateway

echo ============================================================
echo  Hermes Gateway NSSM 卸载脚本
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

echo 停止并删除 HermesGateway 服务...
"%NSSM_DIR%\nssm.exe" stop %SERVICE_NAME% >nul 2>&1
"%NSSM_DIR%\nssm.exe" remove %SERVICE_NAME% >nul 2>&1

echo.
echo ============================================================
echo  卸载完成！
echo ============================================================
pause
