# coding: utf-8
"""桌面主进程与工作子进程的固定日志文件角色选择。"""


def select_startup_log_name(process_name, argv):
    """仅顶层进程写固定轮转日志，避免 spawn 子进程争抢文件。"""
    if str(process_name or "") != "MainProcess":
        return ""
    return "app.log"
