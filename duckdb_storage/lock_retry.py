# -*- coding: utf-8 -*-
"""DuckDB 单股票文件占用识别、重试与诊断信息。"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable, Dict, Optional, TypeVar


T = TypeVar("T")

_LOCK_MARKERS = (
    "cannot open file",
    "file is already open in",
    "另一个程序正在使用此文件",
    "进程无法访问",
    "conflicting lock is held",
    "could not set lock on file",
)


def is_duckdb_lock_error(exc: BaseException) -> bool:
    """只识别文件锁冲突，避免把缺表、SQL、数据质量错误误当成占用。"""
    message = str(exc or "").lower()
    return any(marker in message for marker in _LOCK_MARKERS)


def parse_duckdb_lock_error(
    exc: BaseException,
    *,
    db_path: str = "",
    stock_code: str = "",
    period: str = "",
    operation: str = "",
    attempts: int = 0,
) -> Dict[str, Any]:
    """从 DuckDB 跨进程锁异常中提取可展示、可汇总的信息。"""
    message = str(exc or "")
    path = db_path
    if not path:
        match = re.search(r'Cannot open file\s+["\']([^"\']+)["\']', message, re.I)
        if match:
            path = match.group(1)
    pid_match = re.search(r"\bPID\s+(\d+)\b", message, re.I)
    process_match = re.search(r"File is already open in\s*\r?\n?\s*([^\r\n]+)", message, re.I)
    process = process_match.group(1).strip() if process_match else ""
    process = re.sub(r"\s*\(PID\s+\d+\)\s*$", "", process, flags=re.I)
    return {
        "stock": stock_code,
        "period": period,
        "operation": operation,
        "db_path": os.path.normpath(path) if path else "",
        "pid": int(pid_match.group(1)) if pid_match else None,
        "process": process,
        "attempts": int(attempts or 0),
        "error": message,
    }


def retry_on_duckdb_lock(
    operation: Callable[[], T],
    *,
    attempts: Optional[int] = None,
    base_delay: Optional[float] = None,
    max_delay: Optional[float] = None,
    on_retry: Optional[Callable[[BaseException, int, int, float], None]] = None,
) -> T:
    """对文件占用做有上限的指数退避；最终仍失败时保留原始异常。"""
    total_attempts = max(1, int(attempts or os.environ.get("KH_DUCKDB_LOCK_ATTEMPTS", "5")))
    delay = max(0.0, float(base_delay or os.environ.get("KH_DUCKDB_LOCK_RETRY_DELAY", "0.35")))
    delay_cap = max(delay, float(max_delay or os.environ.get("KH_DUCKDB_LOCK_RETRY_MAX_DELAY", "2.0")))

    for attempt in range(1, total_attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if not is_duckdb_lock_error(exc) or attempt >= total_attempts:
                raise
            wait_seconds = min(delay * (2 ** (attempt - 1)), delay_cap)
            if on_retry is not None:
                on_retry(exc, attempt, total_attempts, wait_seconds)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
    raise RuntimeError("unreachable")
