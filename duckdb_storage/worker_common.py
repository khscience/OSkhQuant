# -*- coding: utf-8 -*-
"""
下载子进程共用的小工具：任务超时、可重试错误判断、跨进程 DataFrame 还原。

取自 CS 版 import_worker.py 中 BaoStock 下载实际用到的部分（该文件其余内容
服务于 miniQMT / 大QMT原生桥，开源版不含）。函数行为与原实现一致。
"""
from datetime import datetime
from typing import Dict, Mapping


# 与 history_sources._PERIOD_ALIASES 相同的周期别名表
_PERIOD_ALIASES = {
    "1d": "1d", "d": "1d", "day": "1d", "daily": "1d", "1day": "1d",
    "日": "1d", "日线": "1d",
    "1m": "1m", "m": "1m", "min": "1m", "minute": "1m", "1minute": "1m",
    "分钟": "1m", "1分钟": "1m",
    "5m": "5m", "5min": "5m", "5minute": "5m", "5分钟": "5m",
    "tick": "tick", "ticks": "tick", "分笔": "tick", "逐笔": "tick",
}


def _wire_period(value: object, *, default: str = "") -> str:
    """把周期规范成 1d / 1m / 5m / tick；不认识的周期原样保留。"""
    candidate = getattr(value, "value", value)
    if isinstance(candidate, bytes):
        candidate = candidate.decode("utf-8", "ignore")
    if candidate is None or not str(candidate).strip():
        return default
    raw = str(candidate).strip().casefold()
    key = raw.replace("_", "").replace("-", "")
    result = _PERIOD_ALIASES.get(raw) or _PERIOD_ALIASES.get(key)
    return result if result is not None else str(candidate).strip()


def _effective_task_timeout(payload: Dict, configured_timeout: float) -> float:
    """按数据粒度和区间放宽默认超时，避免把大型分钟/Tick任务误杀。"""
    base = float(configured_timeout or 0.0)
    if base <= 0 or base < 30:
        # 小于 30 秒仅用于显式低超时/自动测试，按用户值原样执行。
        return base
    period = _wire_period((payload or {}).get('period'))
    minimums = {'1d': base, '5m': 300.0, '1m': 600.0, 'tick': 900.0}
    effective = max(base, minimums.get(period, base))

    start = ''.join(
        ch for ch in str((payload or {}).get('start') or (payload or {}).get('start_date') or '')
        if ch.isdigit()
    )[:8]
    end = ''.join(
        ch for ch in str((payload or {}).get('end') or (payload or {}).get('end_date') or '')
        if ch.isdigit()
    )[:8]
    try:
        days = max(1, (datetime.strptime(end, '%Y%m%d') - datetime.strptime(start, '%Y%m%d')).days + 1)
    except (TypeError, ValueError):
        days = 1
    years = days / 365.0
    if period == 'tick':
        effective = max(effective, min(3600.0, 900.0 + years * 300.0))
    elif period == '1m':
        effective = max(effective, min(1800.0, 600.0 + years * 120.0))
    elif period == '5m':
        effective = max(effective, min(900.0, 300.0 + years * 60.0))
    return effective


_TRANSIENT_DATA_SOURCE_MARKERS = (
    "connection",
    "socket",
    "timeout",
    "timed out",
    "connectionreset",
    "connectionaborted",
    "connectionrefused",
    "remote host",
    "broken pipe",
    "winerror 100",
    "连接",
    "断开",
    "超时",
    "远程主机",
    "目标计算机",
    "服务未启动",
    "用户未登录",
    "未登录",
)

# 协议里稳定的错误码。刻意保持很短：请求格式错误、不支持的复权、重复主键
# 等问题必须立即暴露给调用方，不能藏在几次重试后面。
_TRANSIENT_DATA_SOURCE_CODES = frozenset({
    "BRIDGE_TIMEOUT",
    "BRIDGE_UNAVAILABLE",
    "HISTORY_INCOMPLETE",
    "RESULT_MISSING",
    "RESULT_CORRUPT",
    "WORKER_EXIT",
    "QUEUE_TIMEOUT",
    "TIMEOUT",
    "WORKER_EXITED",
})
_NON_TRANSIENT_DATA_SOURCE_CODES = frozenset({
    "DUPLICATE_TIMESTAMP",
    "INVALID_SOURCE",
    "UNSUPPORTED_SOURCE",
    "INVALID_REQUEST",
    "INVALID_CODE",
    "INVALID_TIME",
    "INVALID_RANGE",
    "RANGE_LIMIT",
    "OUT_OF_RETENTION",
    "UNSUPPORTED_PERIOD",
    "UNSUPPORTED_ADJUSTMENT",
    "LEGACY_SOURCE_DISABLED",
    "DEPENDENCY_MISSING",
    "API_MISSING",
    "DATA_QUALITY",
    "PROTOCOL_ERROR",
    "NO_DATA",
    "HISTORY_EMPTY",
    "EMPTY_RESULT",
    "LOCAL_ADJUSTMENT_EMPTY",
})


def _error_code_candidates(error: object, error_type: object = None) -> tuple[str, ...]:
    """Extract stable error codes from exceptions/result mappings.

    The importer receives errors from several process boundaries.  Depending
    on the boundary the code may be on ``error.code``, ``error.error_code``,
    a mapping's ``error`` object, or the worker's ``error_type`` field.  This
    helper intentionally does not parse arbitrary prose as a code; prose is
    handled by the legacy marker fallback below.
    """

    values = []

    def visit(value: object) -> None:
        if value is None:
            return
        if isinstance(value, Mapping):
            for key in ("code", "error_code", "error_type", "status_code"):
                if key in value:
                    visit(value.get(key))
            nested = value.get("error")
            if nested is not value:
                visit(nested)
            return
        for attr in ("code", "error_code", "error_type"):
            try:
                candidate = getattr(value, attr, None)
            except Exception:
                candidate = None
            if candidate is not None:
                visit(candidate)
        if isinstance(value, str):
            text = value.strip().upper().replace("-", "_").replace(" ", "_")
            if text:
                values.append(text)

    visit(error_type)
    visit(error)
    # Preserve order while removing duplicates.
    return tuple(dict.fromkeys(values))


def _is_transient_data_source_error(error: object, error_type: object = None) -> bool:
    """判断数据源错误是否适合换新进程后重试。"""
    codes = _error_code_candidates(error, error_type)
    # An explicit retryable=False is authoritative, including when the code
    # itself happens to be a generally transient one.
    for value in (error_type, error):
        retryable = None
        if isinstance(value, Mapping):
            retryable = value.get("retryable")
        else:
            try:
                retryable = getattr(value, "retryable", None)
            except Exception:
                retryable = None
        if retryable is False:
            return False
        if retryable is True:
            return True

    if any(code in _NON_TRANSIENT_DATA_SOURCE_CODES for code in codes):
        return False
    if any(code in _TRANSIENT_DATA_SOURCE_CODES for code in codes):
        return True

    normalized_type = str(error_type or "").strip().lower()
    if normalized_type in {"timeout", "worker_exit", "worker_exited", "queue_timeout"}:
        return True
    text = str(error or "").strip().lower().replace(" ", "")
    return bool(text) and any(
        marker.replace(" ", "") in text for marker in _TRANSIENT_DATA_SOURCE_MARKERS
    )


def dict_to_dataframe(df_dict: Dict):
    """
    将序列化的字典转换回 DataFrame

    Args:
        df_dict: {'data': dict, 'index': list, 'columns': list}

    Returns:
        pandas.DataFrame
    """
    import pandas as pd

    if df_dict is None:
        return None

    df = pd.DataFrame(df_dict['data'])
    df.index = df_dict['index']
    return df
