#!/usr/bin/env python3
"""
Hermes Gateway Windows Service Wrapper
使用 pywin32 将 Hermes Gateway 注册为 Windows 系统服务

依赖: pip install pywin32
注册服务: python hermes_gateway_service.py install
启动服务: net start HermesGateway
停止服务: net stop HermesGateway
卸载服务: python hermes_gateway_service.py remove
"""

import os
import sys
import servicemanager
import win32service
import win32serviceutil
import win32con
from pathlib import Path

# 配置区
HERMES_HOME = os.path.expanduser("~/.hermes")
HERMES_AGENT_DIR = r"C:\Users\Administrator\AppData\Local\hermes\hermes-agent"
PYTHON_EXE = sys.executable
GATEWAY_SCRIPT = os.path.join(HERMES_AGENT_DIR, "hermes_cli", "main.py")


class HermesGatewayService(win32serviceutil.ServiceFramework):
    _svc_name_ = "HermesGateway"
    _svc_display_name_ = "Hermes Agent Gateway"
    _svc_description_ = "Hermes Agent Messaging Gateway - 消息网关服务"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, "")
        )
        self.main()

    def main(self):
        """启动 Gateway 子进程"""
        import subprocess
        import signal

        env = os.environ.copy()
        env["HERMES_HOME"] = HERMES_HOME
        env["_HERMES_GATEWAY"] = "1"

        # 启动 gateway 进程
        proc = subprocess.Popen(
            [PYTHON_EXE, GATEWAY_SCRIPT, "gateway"],
            cwd=HERMES_AGENT_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        servicemanager.LogInfoMsg(f"HermesGateway started with PID: {proc.pid}")

        # 等待停止信号
        while True:
            ret = win32event.WaitForSingleObject(self.stop_event, 5000)
            if ret == win32event.WAIT_OBJECT_0:
                break
            if proc.poll() is not None:
                # 进程异常退出，记录日志
                servicemanager.LogErrorMsg(f"HermesGateway process died unexpectedly")
                break

        # 优雅关闭
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except:
            proc.kill()
        servicemanager.LogInfoMsg("HermesGateway stopped")


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(HermesGatewayService)
