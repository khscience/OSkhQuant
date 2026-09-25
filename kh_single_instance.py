# coding: utf-8
"""桌面主程序跨进程单实例锁（Windows 命名互斥量）。

锁名取自 kh_app_identity，与 CS 版不同名，两个软件可以同时打开。
"""

from __future__ import annotations

import os
import re
import sys
from typing import Optional

from kh_app_identity import APP_NAME, DESKTOP_INSTANCE_KEY as DEFAULT_DESKTOP_INSTANCE_KEY


class DesktopSingleInstanceLock:
    """按当前登录会话阻止重复桌面主程序；获取异常时安全放行。"""

    def __init__(self, key: Optional[str] = None):
        raw_key = key or os.environ.get("KHQUANT_SINGLE_INSTANCE_KEY")
        self.key = str(raw_key or DEFAULT_DESKTOP_INSTANCE_KEY)
        self.acquired = False
        self.error = ""
        self._handle = None

    def acquire(self) -> bool:
        if self.acquired:
            return True
        try:
            acquired = self._acquire_windows()
        except Exception as exc:
            # 单实例保护本身失效时保留软件可用性，后续由主日志给出警告。
            self.error = str(exc)
            self.acquired = True
            return True
        self.acquired = bool(acquired)
        return self.acquired

    def _acquire_windows(self) -> bool:
        import ctypes
        from ctypes import wintypes

        safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", self.key)[:180]
        mutex_name = f"Local\\{safe_key}"
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateMutexW(None, False, mutex_name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        error_already_exists = 183
        if ctypes.get_last_error() == error_already_exists:
            kernel32.CloseHandle(handle)
            return False
        self._handle = (kernel32, handle)
        return True

    def release(self):
        if self._handle is not None:
            kernel32, handle = self._handle
            self._handle = None
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
        self.acquired = False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"{APP_NAME}已在运行")
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.release()

def notify_already_running(
    message: str = f"{APP_NAME}已经在运行。请切换到现有窗口，无需重复启动。",
    title: str = APP_NAME,
):
    """在重型 GUI 导入前告知用户已有主程序，不结束任何现有进程。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)
        return
    except Exception:
        pass
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass
