# coding: utf-8
"""Shared defaults for optional backtest performance features."""
from __future__ import annotations

import datetime as _dt
from typing import Any, Mapping, Tuple

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional dependency fallback
    psutil = None


DEFAULT_PERFORMANCE_CONFIG = {
    # High-level UI/CLI preset.  The preset is expanded into the detailed
    # settings by the settings layer, keeping one shared source of truth.
    "preset": "balanced",

    # Memory profile is a high-level preset. "standard" keeps historical
    # behavior; "low"/"ultra_low" enable dynamic chunked loading on smaller
    # machines.
    # 实测全A分钟线: 峰值内存对 chunk_size 是 U 形曲线, 谷底在 ~20 个交易日
    #   chunk<20: 分段暴增、换段瞬时叠加(旧段+新段+#17预读)→ 内存反升, 且重载次数多→更慢;
    #   chunk>20: 单段窗口增大 → 内存线性升, 但段数少→更快(到~2段饱和)。
    # 故 chunk 下限钉在 20(内存谷底): 再小既不省内存又更慢。大内存要提速请往大调(performance档/手动)。
    "memory_profile": "auto",
    "memory_auto_dynamic_load": True,
    "memory_low_chunk_size": 20,
    "memory_ultra_low_chunk_size": 20,

    # khHistory keeps the public API unchanged while limiting backtest reads to
    # the useful window and preserving no-future-bar semantics.
    "khhistory_cache_mode": "backtest_window",
    "khhistory_prefetch_end": "current_day",
    "khhistory_memory_fastpath": True,
    "khhistory_missing_data_prompt": False,
    "khhistory_return_copy": True,

    # Backtest logging and current_data construction.
    "empty_data_log_mode": "summary",
    "current_data_row_mode": "series",
    "current_data_container_mode": "dict",
    "time_index_mode": "dict",

    # DuckDB read behavior. Keep ensure_tables=True for compatibility; advanced
    # stress configs may set it to false explicitly.
    "duckdb_order_mode": "verify_after_fetch",
    "duckdb_read_ensure_tables": True,
    # 回测连接池上限(有界池)。实测全A4752股: 100/600太小→连接淘汰churn把分段加载拖到假死;
    # 全持4752→吃47GB内存且DuckDB实例越多开越慢。800平衡: 内存~16GB、分段加载~5min、不假死。
    # 框架运行时按此值调 set_max_backtest_connections(会覆盖创建时的默认), 故这里才是真正生效的杠杆。
    "duckdb_load_max_connections": 800,
    "duckdb_max_connections": 800,
    "duckdb_load_batch_size": "auto",
    "duckdb_parallel_read_workers": 4,

    # Framework raw-data loader and optional preload.
    "framework_raw_duckdb_load": True,
    "framework_history_preload_days": 300,

    # Parquet cache pack is deliberately opt-in so normal backtests do not
    # silently create large sidecar files.
    "parquet_cache_pack": "off",
    "parquet_cache_pack_root": "",
    "parquet_cache_pack_batch_size": "auto",
    "parquet_cache_pack_workers": "auto",
    "parquet_cache_pack_compression": "SNAPPY",
}


PERFORMANCE_PRESETS = {
    "performance": {
        "label": "全量加载",
        "description": "强制一次性把全部行情载入内存(不分段)。数据量小、内存充足时最快；超大数据(如全A分钟线长周期)可能内存不足，溢出时会自动降级为分段重试。",
        "settings": {
            "memory_profile": "standard",
            "memory_auto_dynamic_load": True,
            "memory_low_chunk_size": DEFAULT_PERFORMANCE_CONFIG["memory_low_chunk_size"],
            "memory_ultra_low_chunk_size": 20,
            "khhistory_cache_mode": "backtest_window",
            "khhistory_prefetch_end": "backtest_end",
            "khhistory_memory_fastpath": True,
            "empty_data_log_mode": "summary",
            "duckdb_order_mode": "verify_after_fetch",
            "duckdb_load_batch_size": "auto",
            "duckdb_parallel_read_workers": DEFAULT_PERFORMANCE_CONFIG["duckdb_parallel_read_workers"],
            # cap 统一 800: OFAT实测全A4752股, cap>800 反而更慢+更吃内存(cap800→2000: +47%时间+88%内存);
            # churn减少省的 < DuckDB实例/缓冲增的。框架另有 max(,800) 保底, 故三档不再用 cap 区分。
            "duckdb_load_max_connections": 800,
            "duckdb_max_connections": 800,
            "framework_raw_duckdb_load": True,
            "framework_history_preload_days": 300,
        },
    },
    "balanced": {
        "label": "智能（推荐）",
        "description": "自动按数据规模和可用内存决定全量或分段：小数据全量加载、大数据自动分段、分段大小按可用内存自动放大。绝大多数情况用它即可。",
        "settings": {
            "memory_profile": "auto",
            "memory_auto_dynamic_load": True,
            "memory_low_chunk_size": DEFAULT_PERFORMANCE_CONFIG["memory_low_chunk_size"],
            "memory_ultra_low_chunk_size": DEFAULT_PERFORMANCE_CONFIG["memory_ultra_low_chunk_size"],
            "khhistory_cache_mode": "backtest_window",
            "khhistory_prefetch_end": "current_day",
            "khhistory_memory_fastpath": True,
            "empty_data_log_mode": "summary",
            "duckdb_order_mode": "verify_after_fetch",
            "duckdb_load_batch_size": "auto",
            "duckdb_parallel_read_workers": 4,
            "duckdb_load_max_connections": 800,
            "duckdb_max_connections": 800,
            "framework_raw_duckdb_load": True,
            "framework_history_preload_days": 300,
        },
    },
    "low_memory": {
        "label": "省内存（分段）",
        "description": "强制分段加载、最省内存。数据大或内存有限时用它；若仍不足会自动升到极低内存档重试。分段大小按可用内存自动调整(下限20交易日)。",
        "settings": {
            "memory_profile": "low",
            "memory_auto_dynamic_load": True,
            "memory_low_chunk_size": 20,
            "memory_ultra_low_chunk_size": 20,
            "khhistory_cache_mode": "backtest_window",
            "khhistory_prefetch_end": "current_day",
            "khhistory_memory_fastpath": True,
            "empty_data_log_mode": "summary",
            "duckdb_order_mode": "verify_after_fetch",
            "duckdb_load_batch_size": "auto",
            "duckdb_parallel_read_workers": DEFAULT_PERFORMANCE_CONFIG["duckdb_parallel_read_workers"],
            "duckdb_load_max_connections": 800,
            "duckdb_max_connections": 800,
            "framework_raw_duckdb_load": True,
            "framework_history_preload_days": 300,
        },
    },
}


def normalize_performance_preset(value: Any) -> str:
    raw = str(value or "balanced").strip().lower().replace("-", "_")
    aliases = {
        "fast": "performance",
        "speed": "performance",
        "max": "performance",
        "high": "performance",
        "normal": "balanced",
        "default": "balanced",
        "auto": "balanced",
        "memory": "low_memory",
        "low": "low_memory",
        "lowmemory": "low_memory",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in PERFORMANCE_PRESETS else "balanced"


def performance_preset_settings(preset: Any) -> dict:
    key = normalize_performance_preset(preset)
    return dict(PERFORMANCE_PRESETS[key]["settings"])


def normalize_memory_profile(profile: Any) -> str:
    raw = str(profile if profile is not None else "auto").strip().lower().replace("-", "_")
    if raw in ("", "default", "normal"):
        return "auto"
    if raw in ("ultralow",):
        return "ultra_low"
    if raw in ("auto", "standard", "low", "ultra_low"):
        return raw
    return "auto"


def next_lower_memory_profile(profile: Any) -> str | None:
    current = normalize_memory_profile(profile)
    order = ("standard", "low", "ultra_low")
    if current not in order:
        return None
    idx = order.index(current)
    if idx >= len(order) - 1:
        return None
    return order[idx + 1]


def _get_system_memory_mb() -> Tuple[int, int]:
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            total_mb = int(vm.total / (1024 * 1024))
            avail_mb = int(vm.available / (1024 * 1024))
            return total_mb, avail_mb
        except Exception:
            pass
    try:
        import os as _os
        import ctypes as _ctypes

        if _os.name == "nt":
            class _MEMORYSTATUSEX(_ctypes.Structure):
                _fields_ = [
                    ("dwLength", _ctypes.c_ulong),
                    ("dwMemoryLoad", _ctypes.c_ulong),
                    ("ullTotalPhys", _ctypes.c_ulonglong),
                    ("ullAvailPhys", _ctypes.c_ulonglong),
                    ("ullTotalPageFile", _ctypes.c_ulonglong),
                    ("ullAvailPageFile", _ctypes.c_ulonglong),
                    ("ullTotalVirtual", _ctypes.c_ulonglong),
                    ("ullAvailVirtual", _ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", _ctypes.c_ulonglong),
                ]

            status = _MEMORYSTATUSEX()
            status.dwLength = _ctypes.sizeof(_MEMORYSTATUSEX)
            if _ctypes.windll.kernel32.GlobalMemoryStatusEx(_ctypes.byref(status)):
                total_mb = int(status.ullTotalPhys / (1024 * 1024))
                avail_mb = int(status.ullAvailPhys / (1024 * 1024))
                return total_mb, avail_mb
    except Exception:
        pass
    return 0, 0


def _parse_ymd(value: Any):
    if not value:
        return None
    text = str(value).strip()
    if len(text) < 8:
        return None
    text = text[:8]
    try:
        return _dt.date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except Exception:
        return None


def _estimate_trading_days(start_time: Any, end_time: Any) -> int | None:
    start = _parse_ymd(start_time)
    end = _parse_ymd(end_time)
    if start is None or end is None or end < start:
        return None
    days = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days += 1
        cur += _dt.timedelta(days=1)
    return max(days, 1)


def _estimate_bars_per_day(period: Any) -> int:
    text = str(period or "").strip().lower()
    return {
        "tick": 2000,
        "1m": 240,
        "5m": 48,
        "15m": 16,
        "30m": 8,
        "1h": 4,
        "1d": 1,
    }.get(text, 1)


def _estimate_working_set_mb(stock_count: int | None, period: Any, start_time: Any, end_time: Any, field_count: int | None) -> tuple[int | None, int | None]:
    if stock_count is None or stock_count <= 0:
        return None, None
    trading_days = _estimate_trading_days(start_time, end_time)
    if trading_days is None:
        return None, None
    bars_per_day = _estimate_bars_per_day(period)
    rows = int(stock_count) * trading_days * bars_per_day
    cols = max(int(field_count or 0), 1)
    bytes_per_row = 64 + cols * 16
    rough_mb = int(rows * bytes_per_row / (1024 * 1024))
    return rows, rough_mb


def _count_stock_entries(data_config: Mapping[str, Any] | None, config_path: str | None = None) -> int:
    data = data_config or {}
    stocks = data.get("stock_list", data.get("stock_pool", []))
    if isinstance(stocks, (list, tuple, set)):
        return len(stocks)
    if isinstance(stocks, str) and stocks.strip():
        return len([s for s in stocks.replace("\n", ",").split(",") if s.strip()])

    stock_list_file = data.get("stock_list_file", "")
    if not stock_list_file:
        return 0
    try:
        import os as _os

        path = str(stock_list_file)
        if not _os.path.isabs(path) and config_path:
            path = _os.path.join(_os.path.dirname(_os.path.abspath(config_path)), path)
        if not _os.path.exists(path):
            return 0
        count = 0
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                text = line.strip()
                if not text:
                    continue
                if count == 0 and any(k in text.lower() for k in ("code", "stock", "代码", "证券")):
                    continue
                count += 1
        return count
    except Exception:
        return 0


def resolve_memory_profile_from_config(
    config: Mapping[str, Any] | None,
    *,
    config_path: str | None = None,
    available_mem_mb: int | None = None,
    total_mem_mb: int | None = None,
) -> tuple[str, dict]:
    cfg = config or {}
    data = cfg.get("data", {}) or {}
    backtest = cfg.get("backtest", {}) or {}
    fields = data.get("fields", [])
    field_count = len(fields) if isinstance(fields, (list, tuple, set)) else 0
    return resolve_memory_profile(
        cfg.get("performance", {}) or {},
        stock_count=_count_stock_entries(data, config_path=config_path),
        period=data.get("kline_period", "1d"),
        start_time=backtest.get("start_time", "20240101"),
        end_time=backtest.get("end_time", "20241231"),
        field_count=field_count,
        available_mem_mb=available_mem_mb,
        total_mem_mb=total_mem_mb,
    )


def resolve_memory_profile(
    performance: Mapping[str, Any] | None = None,
    *,
    stock_count: int | None = None,
    period: Any = "",
    start_time: Any = "",
    end_time: Any = "",
    field_count: int | None = None,
    available_mem_mb: int | None = None,
    total_mem_mb: int | None = None,
) -> tuple[str, dict]:
    perf = dict(performance or {})
    requested = normalize_memory_profile(perf.get("memory_profile"))
    total_mb = total_mem_mb
    avail_mb = available_mem_mb
    if total_mb is None or avail_mb is None:
        sys_total, sys_avail = _get_system_memory_mb()
        if total_mb is None:
            total_mb = sys_total
        if avail_mb is None:
            avail_mb = sys_avail or total_mb

    rows, rough_mb = _estimate_working_set_mb(stock_count, period, start_time, end_time, field_count)

    if requested != "auto":
        return requested, {
            "requested": requested,
            "effective": requested,
            "source": "explicit",
            "available_mem_mb": avail_mb or 0,
            "total_mem_mb": total_mb or 0,
            "estimated_rows": rows,
            "estimated_working_set_mb": rough_mb,
        }

    if rough_mb is None or avail_mb is None or avail_mb <= 0:
        effective = "standard" if rows is None or (rows < 10_000_000) else "low"
    else:
        if rough_mb <= avail_mb * 0.35:
            effective = "standard"
        else:
            effective = "low"

        if rows is not None and rows >= 120_000_000 and effective == "standard":
            effective = "low"
        if (
            rows is not None
            and rows >= 220_000_000
            and (
                rough_mb > avail_mb * 2.0
                or (total_mb is not None and total_mb > 0 and total_mb < 32_768)
                or avail_mb < 16_384
            )
        ):
            effective = "ultra_low"

    return effective, {
        "requested": "auto",
        "effective": effective,
        "source": "auto",
        "available_mem_mb": avail_mb or 0,
        "total_mem_mb": total_mb or 0,
        "estimated_rows": rows,
        "estimated_working_set_mb": rough_mb,
    }


def recommend_chunk_size(
    effective_profile: Any,
    *,
    base_chunk: int = 20,
    available_mem_mb: int = 0,
    estimated_working_set_mb: int = 0,
    trading_days: int = 0,
) -> int:
    """按可用内存把动态分段 chunk_size 放大到"能装下的最大"(少分段→少重载→提速)。

    实测全A分钟线: 峰值内存 ≈ ~10GB 地板(cap800池+框架) + 工作集×(chunk/区间);
    chunk_size 内存呈 U 形、谷底 ~20。本函数在 low/ultra_low 档下:
      - 大内存机 → 自动取能装下的最大 chunk(全年 chunk20 ~170min → chunk~96 ~70min);
      - 小内存机 → 退回下限 base_chunk(=20, 内存谷底)。
    只影响时间/内存, 不改回测结果。OOM 时框架的自动降档重试会落到更保守的 ultra_low。
    """
    base = max(20, int(base_chunk or 20))
    if effective_profile not in ("low", "ultra_low"):
        return base
    try:
        avail = float(available_mem_mb or 0)
        rough = float(estimated_working_set_mb or 0)
        td = int(trading_days or 0)
    except (TypeError, ValueError):
        return base
    if avail <= 0 or rough <= 0 or td <= 0:
        return base
    floor_mb = 10240.0  # ~10GB 地板(cap800 连接池 + 框架结构), 实测
    safety = 0.6 if effective_profile == "low" else 0.45  # ultra_low 更保守(OOM 重试会降到这档)
    budget_mb = avail * safety - floor_mb
    if budget_mb <= 0:
        return base  # 内存紧 → 下限 20
    chunk_max = int(td * budget_mb / rough)
    return max(base, min(chunk_max, td))


def normalize_performance_config(performance: Mapping[str, Any] | None = None) -> dict:
    """Return performance config with every supported key populated."""
    merged = dict(DEFAULT_PERFORMANCE_CONFIG)
    if isinstance(performance, Mapping):
        merged.update(dict(performance))
    return merged


def get_performance_config(config_or_dict: Any) -> dict:
    """Read and normalize the performance block from a KhConfig or raw dict."""
    if config_or_dict is None:
        return normalize_performance_config()

    config_dict = getattr(config_or_dict, "config_dict", config_or_dict)
    if not isinstance(config_dict, Mapping):
        return normalize_performance_config()
    return normalize_performance_config(config_dict.get("performance") or {})
