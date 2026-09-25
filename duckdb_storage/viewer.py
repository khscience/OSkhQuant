# -*- coding: utf-8 -*-
"""
DuckDB数据查看器 - 图形界面

提供数据浏览、查询、统计和从MiniQMT导入数据功能
"""

import os
import sys
import json
import logging
import subprocess
import tempfile
import time
import uuid
from threading import Event, Lock
from datetime import date as datetime_date, datetime, timedelta
from typing import Any, Optional, List, Dict, Tuple, Mapping

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeWidget, QTreeWidgetItem, QTableView, QGroupBox,
    QLabel, QLineEdit, QPushButton, QComboBox, QDateEdit, QTextEdit,
    QFileDialog, QMessageBox, QProgressBar, QStatusBar, QTabWidget,
    QHeaderView, QAbstractItemView, QMenu, QAction, QToolBar,
    QFrame, QGridLayout, QSpinBox, QCheckBox, QDialog, QListWidget,
    QListWidgetItem, QDialogButtonBox, QRadioButton, QButtonGroup,
    QDesktopWidget, QScrollArea
)
from PyQt5.QtCore import Qt, QDate, QSize, QAbstractTableModel, QModelIndex, QThread, pyqtSignal, QTimer, QMutex, QWaitCondition, QSettings
from PyQt5.QtGui import QFont, QIcon, QColor, QPixmap, QPainter, QFontMetrics, QPalette
from PyQt5 import sip
from khPathUtils import get_stock_pool_path
from khUiScale import get_ui_font_scale, get_preferred_ui_font_family, install_wheel_guard
from security_type_utils import split_security_code
from tushare_config import load_tushare_settings
from duckdb_storage.lock_retry import is_duckdb_lock_error, parse_duckdb_lock_error
from duckdb_storage.lock_diagnostics import inspect_process_identity
from duckdb_storage.lock_diagnostics_dialog import DatabaseOccupancyDialog
from duckdb_storage.native_options import (
    NATIVE_EXECUTION_OPTION_KEYS,
    NATIVE_EXECUTION_DEFAULTS,
    normalize_native_execution_options,
    merge_native_execution_options,
    read_qsettings_options,
    write_qsettings_options,
)

try:
    from kh_platform import MINIQMT_LAUNCH_ENABLED, windowed_python_executable
except ImportError:
    MINIQMT_LAUNCH_ENABLED = sys.platform == "win32"

    def windowed_python_executable():
        return sys.executable

try:
    from duckdb_storage.history_sources import normalize_source, MINIQMT, QMT_NATIVE
except Exception:
    MINIQMT = "miniqmt"
    QMT_NATIVE = "qmt_native"

    def normalize_source(value, default=MINIQMT):
        raw = str(value or default).strip().lower()
        if raw in {"xtdata", "miniqmt", "mini_qmt", "qmt"}:
            return MINIQMT
        if raw in {
            "qmt_native", "qmt-native", "native_bigqmt", "native-bigqmt",
            "bigqmt_native", "bigqmt-native", "native_qmt", "native-qmt",
        }:
            return QMT_NATIVE
        raise ValueError(f"未知历史数据源: {value}")

try:
    # Keep every GUI tick entry point on the same retention protocol as the
    # planner/native adapter. ``incremental`` is dependency-light and does
    # not import Qt or xtquant, so importing these constants here is safe.
    from duckdb_storage.incremental import (
        TICK_MAX_AGE_DAYS,
        TICK_MAX_SPAN_DAYS,
        TICK_MIN_ROWS_PER_DAY,
    )
except Exception:  # pragma: no cover - single-file/packaged fallback
    TICK_MAX_AGE_DAYS = 31
    TICK_MAX_SPAN_DAYS = 31
    TICK_MIN_ROWS_PER_DAY = 4465

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
import matplotlib.font_manager as fm

# 设置matplotlib中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示问题

DOWNLOAD_LOG_MAX_BLOCKS = 100
DOWNLOAD_UI_UPDATE_MIN_INTERVAL = 0.2


def _new_native_progress_stats() -> dict:
    """Create a bounded accumulator for native QMT progress diagnostics.

    The existing Qt ``download_progress`` signal is intentionally unchanged
    for compatibility.  Native events are folded into a compact message so
    operators can see bridge jobs/segments/rows/bytes/retries without moving
    full manifests or DataFrames through Qt signals.
    """
    return {
        "job_ids": set(),
        "segments_by_task": {},
        "rows_by_task": {},
        "bytes_by_task": {},
        "phase_by_task": {},
        "timing_by_task": {},
        "retries": 0,
    }


def _native_progress_number(value, default=0):
    """Parse a non-negative finite progress counter without truthiness casts."""
    import math
    if isinstance(value, bool):
        return int(default)
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)
    if number < 0:
        return int(default)
    return number


def _native_progress_segment_totals(event: Mapping) -> Tuple[int, int]:
    """Extract bounded segment counts from compact native job details."""
    raw_details = event.get("native_job_details", event.get("job_details"))
    if isinstance(raw_details, Mapping):
        raw_details = list(raw_details.values())
    if not isinstance(raw_details, (list, tuple)):
        return 0, 0
    seen = {}
    for detail in list(raw_details)[:256]:
        if not isinstance(detail, Mapping):
            continue
        nested = detail.get("job")
        source = nested if isinstance(nested, Mapping) else detail
        key = (
            str(source.get("job_id") or detail.get("job_id") or ""),
            str(detail.get("code") or ""),
            str(detail.get("adjustment") or ""),
        )
        finished = _native_progress_number(
            source.get("finished_segments", detail.get("finished_segments")), 0
        )
        total = _native_progress_number(
            source.get("total_segments", detail.get("total_segments")), 0
        )
        old_finished, old_total = seen.get(key, (0, 0))
        seen[key] = max(old_finished, finished), max(old_total, total)
    return sum(pair[0] for pair in seen.values()), sum(pair[1] for pair in seen.values())


def _consume_native_progress_stats(stats: dict, event: object) -> None:
    """Merge one worker/bridge event into the bounded native progress view."""
    if not isinstance(stats, dict) or not isinstance(event, Mapping):
        return
    event_type = str(event.get("type") or "").strip().lower()
    raw_job_ids = event.get("job_ids")
    if isinstance(raw_job_ids, (str, bytes)):
        raw_job_ids = [raw_job_ids]
    if isinstance(raw_job_ids, (list, tuple, set, frozenset)):
        for value in list(raw_job_ids)[:128]:
            text = str(value or "").strip()
            if text:
                stats.setdefault("job_ids", set()).add(text)
    # A result envelope may carry IDs under ``native_job_ids`` rather than
    # ``job_ids``.  Keep the two event spellings equivalent.
    native_ids = event.get("native_job_ids")
    if isinstance(native_ids, (str, bytes)):
        native_ids = [native_ids]
    if isinstance(native_ids, (list, tuple, set, frozenset)):
        for value in list(native_ids)[:128]:
            text = str(value or "").strip()
            if text:
                stats.setdefault("job_ids", set()).add(text)

    task_id = str(event.get("task_id") or event.get("job_id") or "")
    if not task_id:
        # Keep one bounded bucket for direct callbacks that omit task_id.
        task_id = "__aggregate__"
    if event_type in {"retry", "retrying"}:
        stats["retries"] = _native_progress_number(stats.get("retries"), 0) + 1

    stage = str(event.get("stage") or event.get("phase") or "").strip().lower()
    if stage:
        phase_map = stats.setdefault("phase_by_task", {})
        if len(phase_map) < 10000 or task_id in phase_map:
            phase_map[task_id] = stage
    timing_names = (
        "download_wait_ms", "cache_read_ms", "stability_signature_ms",
        "full_validation_ms", "coverage_validation_ms", "result_write_ms",
    )
    if any(name in event for name in timing_names):
        timing_map = stats.setdefault("timing_by_task", {})
        values = dict(timing_map.get(task_id) or {})
        for name in timing_names:
            if name not in event:
                continue
            try:
                values[name] = max(
                    float(values.get(name, 0.0) or 0.0),
                    max(0.0, float(event.get(name) or 0.0)),
                )
            except (TypeError, ValueError, OverflowError):
                continue
        if values and (len(timing_map) < 10000 or task_id in timing_map):
            timing_map[task_id] = values

    finished = event.get("finished_segments", event.get("segments_finished"))
    total = event.get("total_segments", event.get("segments_total"))
    detail_finished, detail_total = _native_progress_segment_totals(event)
    if detail_finished:
        finished = max(_native_progress_number(finished, 0), detail_finished)
    if detail_total:
        total = max(_native_progress_number(total, 0), detail_total)
    if finished is not None or total is not None:
        segment_map = stats.setdefault("segments_by_task", {})
        if len(segment_map) < 10000 or task_id in segment_map:
            old_finished, old_total = segment_map.get(task_id, (0, 0))
            segment_map[task_id] = (
                max(old_finished, _native_progress_number(finished, old_finished)),
                max(old_total, _native_progress_number(total, old_total)),
            )

    rows_value = event.get("records", event.get("row_count", event.get("rows")))
    if rows_value is not None:
        rows_map = stats.setdefault("rows_by_task", {})
        if len(rows_map) < 10000 or task_id in rows_map:
            rows_map[task_id] = max(
                _native_progress_number(rows_map.get(task_id), 0),
                _native_progress_number(rows_value, 0),
            )
    bytes_value = event.get(
        "transport_bytes",
        event.get("bytes", event.get("result_bytes")),
    )
    if bytes_value is not None:
        bytes_map = stats.setdefault("bytes_by_task", {})
        if len(bytes_map) < 10000 or task_id in bytes_map:
            bytes_map[task_id] = max(
                _native_progress_number(bytes_map.get(task_id), 0),
                _native_progress_number(bytes_value, 0),
            )


def _format_native_progress_summary(stats: dict, eta_seconds: int = 0) -> str:
    """Format the compact native counters appended to the existing status."""
    if not isinstance(stats, dict):
        return ""
    segments = stats.get("segments_by_task") or {}
    finished = sum(pair[0] for pair in segments.values() if isinstance(pair, tuple))
    total = sum(pair[1] for pair in segments.values() if isinstance(pair, tuple))
    rows = sum(_native_progress_number(value, 0) for value in (stats.get("rows_by_task") or {}).values())
    bytes_total = sum(_native_progress_number(value, 0) for value in (stats.get("bytes_by_task") or {}).values())
    retries = _native_progress_number(stats.get("retries"), 0)
    phases = list((stats.get("phase_by_task") or {}).values())
    phase = phases[-1] if phases else ""
    phase_text = {
        "qmt_download_wait": "QMT下载等待",
        "integrity_stability": "缓存稳定确认",
        "integrity_validation": "完整性验证",
        "local_read": "本地复权读取",
        "local_read_done": "本地复权完成",
    }.get(phase, phase or "等待")
    timings = stats.get("timing_by_task") or {}
    download_wait_ms = sum(
        float(values.get("download_wait_ms", 0.0) or 0.0)
        for values in timings.values() if isinstance(values, Mapping)
    )
    validation_ms = sum(
        float(values.get("full_validation_ms", 0.0) or 0.0)
        + float(values.get("coverage_validation_ms", 0.0) or 0.0)
        for values in timings.values() if isinstance(values, Mapping)
    )
    eta = _native_progress_number(eta_seconds, 0)
    if eta:
        eta_text = "%ss" % eta
    else:
        eta_text = "计算中"
    return (
        "桥接 jobs=%d segments=%d/%d rows=%d bytes=%d retries=%d "
        "阶段=%s 等待=%.1fs 验证=%.1fs ETA=%s"
        % (
            len(stats.get("job_ids") or ()), finished, total, rows,
            bytes_total, retries, phase_text, download_wait_ms / 1000.0,
            validation_ms / 1000.0, eta_text,
        )
    )

# 历史导入来源与重试参数的稳定协议。这里仅保存轻量配置，不在模块导入
# 阶段加载 xtquant 或原生桥客户端；实际探测/连接均延迟到任务开始前。
HISTORY_IMPORT_SOURCE_KEY = "history_import_source"
HISTORY_BRIDGE_DIR_KEY = "qmt_native_bridge_dir"
HISTORY_SOURCE_STATUS_KEY = "history_import_source_status"
HISTORY_LAST_RUN_STATUS_KEY = "history_import_source_last_run"
HISTORY_MAX_RETRIES_KEY = "history_import_max_task_retries"
HISTORY_RETRY_BACKOFF_KEY = "history_import_retry_backoff"
DEFAULT_HISTORY_MAX_TASK_RETRIES = 3
DEFAULT_HISTORY_RETRY_BACKOFF = (30.0, 120.0, 300.0)
# Native bridge request defaults.  Keep these in the GUI layer as a single
# policy so every importer (including a restarted worker pool) sends the same
# execution contract.  ``max_attempts`` is the number of bridge attempts,
# while ``max_task_retries`` above remains the outer process retry budget.
DEFAULT_NATIVE_MAX_ATTEMPTS = 6
INSTANCE_GENERATION_MAX_LENGTH = 256


def _native_options_for_source(source: object, values: Optional[Mapping] = None) -> dict:
    """Return canonical native execution options for an importer.

    The options are intentionally omitted for MiniQMT callers at the final
    constructor boundary; keeping this helper source-aware prevents a legacy
    third-party MiniQMT importer from rejecting new keyword arguments while
    still making QMT defaults explicit and inspectable.
    """
    options = normalize_native_execution_options(values or {}, strict=False)
    try:
        normalized_source = _canonical_history_source(source)
    except Exception:
        normalized_source = str(source or "").strip().lower()
    if normalized_source != QMT_NATIVE:
        return {}
    return {
        "native_profile": options["native_profile"],
        "max_inflight": options["max_inflight"],
        "batch_size": options["batch_size"],
        "span_rows": options["span_rows"],
        "span_bytes": options["span_bytes"],
        "mode": options["mode"],
        "cache_strategy": options["cache_strategy"],
    }


def _native_options_from_owner(owner: object) -> dict:
    """Extract/normalize options from a thread/dialog-like object."""
    return normalize_native_execution_options(
        {
            key: getattr(owner, key, None)
            for key in NATIVE_EXECUTION_OPTION_KEYS
            if hasattr(owner, key)
        },
        strict=False,
    )


def _calendar_day_span(start_value, end_value) -> int:
    """Return an inclusive calendar-day span for ``YYYYMMDD`` values.

    Date strings are not integers: subtracting ``20240229`` from
    ``20240301`` gives ``72`` instead of two days.  The import scanners use
    this value when proportionally clipping an overlap, so always parse real
    calendar dates and retain a conservative integer fallback for legacy
    callers that pass unusual values.
    """
    try:
        def _parse(value):
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, datetime_date):
                return value
            text = str(value).strip().replace("-", "").replace("/", "")
            return datetime.strptime(text[:8], "%Y%m%d").date()

        span = (_parse(end_value) - _parse(start_value)).days + 1
        return int(span)
    except Exception:
        try:
            return int(end_value) - int(start_value) + 1
        except Exception:
            return 0


def _tick_scan_retained_dates(
    values,
    *,
    now=None,
    max_age_days: Optional[int] = None,
):
    """回退扫描使用的 tick 保留窗过滤。

    主扫描器由 ``khQTTools.check_duckdb_data_integrity`` 完成；这里单独
    保留一个无 Qt/无客户端依赖的小 helper，确保该函数异常回退时也不会
    把超限或未来日期排进下载队列。当 max_age_days 为 None 时不按天数裁剪。
    """
    try:
        from duckdb_storage.incremental import tick_retained_trade_dates
        retained, _ignored = tick_retained_trade_dates(
            values,
            now=now,
            max_age_days=max_age_days,
        )
        return set(retained)
    except Exception:
        # 出现异常时不放宽窗口；按当前日期做一个保守过滤。
        current = now or datetime.now()
        if isinstance(current, datetime):
            today = current.date()
        elif isinstance(current, datetime_date):
            today = current
        else:
            today = datetime.now().date()
        if max_age_days is not None:
            try:
                retention_days = max(0, int(max_age_days))
            except Exception:
                retention_days = 31
            cutoff = today - timedelta(days=retention_days)
        else:
            cutoff = None
        result = set()
        for value in values or ():
            text = str(value).strip().replace("-", "")[:8]
            try:
                date_value = datetime.strptime(text, "%Y%m%d").date()
            except Exception:
                continue
            if (cutoff is None or cutoff <= date_value) and date_value <= today:
                result.add(text)
        return result


def _tick_scan_complete_dates(
    manager,
    stock: str,
    start_date: str,
    end_date: str,
    *,
    min_rows: int = 4465,
):
    """从 manager 的按日完整度接口读取 tick 完整日期。

    回退路径不能只调用 ``get_existing_dates_batch``：那样当天只有一行
    tick 也会被误判为完整。若旧 manager 没有完整度接口，则返回空集合，
    让调用方重新下载（安全但可能多一次请求）。
    """
    try:
        threshold = max(1, int(min_rows))
    except Exception:
        threshold = 4465
    getter = getattr(manager, "get_existing_date_completeness", None)
    coverage = None
    if callable(getter):
        try:
            coverage = getter(
                stock,
                "tick",
                required_columns=("lastPrice", "volume", "amount"),
                raise_on_error=False,
                start_date=start_date,
                end_date=end_date,
            )
        except TypeError:
            # 兼容较老的 manager 替身/插件签名；仍不吞掉真实质量结果。
            try:
                coverage = getter(stock, "tick")
            except Exception:
                coverage = None
        except Exception:
            coverage = None
    complete = set()
    if isinstance(coverage, dict):
        for raw_date, stats in coverage.items():
            date8 = str(raw_date).replace("-", "")[:8]
            try:
                total = int(stats.get("total_rows", 0) or 0)
                valid = int(stats.get("valid_rows", 0) or 0)
            except Exception:
                continue
            if total >= threshold and valid >= threshold:
                complete.add(date8)
    return complete


def _fallback_kline_complete_dates(
    manager,
    stock: str,
    period: object,
    start_date: str,
    end_date: str,
    dividend_types,
):
    """返回回退扫描中复权字段完整的 K 线日期。

    主扫描器 ``khQTTools.check_duckdb_data_integrity`` 会直接在 SQL 中对
    选中的每个复权口径执行 OHLC 四列门禁。若主扫描因临时异常进入旧的
    GUI 回退路径，不能退回到“当天有任意一行就算完整”，否则旧库里
    ``front_ratio``/``back_ratio`` 的 NULL 行会被静默漏过。正式
    ``DuckDBManager`` 已提供按日 total/valid 统计，因此这里复用该接口；
    没有该接口的第三方 manager 仍返回 ``None``，由调用方保留原兼容逻辑。
    """

    period_value = str(getattr(period, "value", period) or "").strip().lower()
    if period_value == "tick":
        return None
    getter = getattr(manager, "get_existing_date_completeness", None)
    if not callable(getter):
        return None

    # ``None`` means the historical importer default: all four adjusted
    # families.  An empty list deliberately means raw-only.
    selected = _integrity_dividend_types(dividend_types)
    required_columns = tuple(
        f"{field}_{adjustment}"
        for adjustment in selected
        for field in ("open", "high", "low", "close")
    )
    try:
        coverage = getter(
            stock,
            period_value,
            required_columns=required_columns,
            raise_on_error=False,
            start_date=start_date,
            end_date=end_date,
        )
    except TypeError:
        # Older plugin/test doubles may expose only ``(stock, period)``.  Do
        # not pretend that such a call checked adjustment columns.
        return None
    except Exception:
        return None

    if not isinstance(coverage, Mapping):
        return None
    complete = set()
    for raw_date, stats in coverage.items():
        if isinstance(raw_date, datetime):
            text = raw_date.strftime("%Y%m%d")
        elif isinstance(raw_date, datetime_date):
            text = raw_date.strftime("%Y%m%d")
        else:
            text = str(raw_date).strip().replace("-", "").replace("/", "")[:8]
        if len(text) != 8 or not text.isdigit():
            continue
        if not isinstance(stats, Mapping):
            continue
        try:
            total_rows = int(stats.get("total_rows", 0) or 0)
            valid_rows = int(stats.get("valid_rows", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        # With selected adjustments every physical row must carry all four
        # fields.  For raw-only fallback retain the old “at least one row”
        # semantics (the production scanner's historical compatibility mode).
        if required_columns:
            if total_rows > 0 and valid_rows >= total_rows:
                complete.add(text)
        elif total_rows > 0:
            complete.add(text)
    return complete


def _tick_scan_groups(missing_dates, expected_dates, *, max_span_days: int = 31):
    """按完整日期和 native bridge 的自然日上限生成 tick 缺口组。"""
    missing = sorted(set(missing_dates or ()))
    expected = sorted(set(expected_dates or ()))
    if not missing:
        return []
    try:
        from duckdb_storage.incremental import group_missing_trade_dates_with_counts
        return list(group_missing_trade_dates_with_counts(
            missing,
            expected,
            max_span_days=max_span_days,
        ))
    except Exception:
        # 最保守的回退：逐交易日请求，绝不越过跨度限制。
        return [(date8, date8, 1) for date8 in missing]


def _tick_safe_date_ranges(
    start_date,
    end_date,
    *,
    now=None,
    max_age_days: Optional[int] = None,
    max_span_days: int = TICK_MAX_SPAN_DAYS,
):
    """返回 QMT tick 可接受的日期分段（闭区间）。

    这是没有交易日历/扫描结果可用的导入入口（旧 ImportThread、强制覆写
    回退）的最后一道前门禁：把每段限制在 native bridge 的自然日跨度内。
    若指定 max_age_days 则裁到保留窗。返回空元组表示日期非法。
    """

    try:
        from duckdb_storage.incremental import normalize_date8

        start8 = normalize_date8(start_date)
        end8 = normalize_date8(end_date)
        span = max(1, int(max_span_days))
    except Exception:
        return ()
    try:
        current = now or datetime.now()
        if isinstance(current, datetime):
            today = current.date()
        elif isinstance(current, datetime_date):
            today = current
        else:
            today = datetime.now().date()
        if max_age_days is not None:
            age = max(0, int(max_age_days))
            cutoff = today - timedelta(days=age)
            lower = max(
                datetime.strptime(start8, "%Y%m%d").date(),
                cutoff,
            )
        else:
            lower = datetime.strptime(start8, "%Y%m%d").date()
        upper = min(
            datetime.strptime(end8, "%Y%m%d").date(),
            today,
        )
        if lower > upper:
            return ()
        result = []
        cursor = lower
        while cursor <= upper:
            chunk_end = min(cursor + timedelta(days=span - 1), upper)
            result.append((cursor.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
            cursor = chunk_end + timedelta(days=1)
        return tuple(result)
    except Exception:
        return ()


def _force_task_groups(
    period,
    start_date,
    end_date,
    trade_days,
    *,
    now=None,
    max_age_days: Optional[int] = None,
    max_span_days: int = TICK_MAX_SPAN_DAYS,
):
    """生成强制覆写任务组，并对 tick 应用保留窗/跨度策略。

    强制覆写只改变“是否检查本地覆盖”，不能绕过数据源的能力边界。
    日线/分钟线继续保留旧的单区间任务形状；tick 则只为保留窗内交易日
    生成最多 31 个自然日的组，窗口外为空时直接不建任务。
    """

    period_value = getattr(period, "value", period)
    period_value = str(period_value or "").strip().lower()
    days = set(trade_days or ())
    if period_value == "tick":
        retained = _tick_scan_retained_dates(
            days,
            now=now,
            max_age_days=max_age_days,
        )
        if not retained:
            return []
        return _tick_scan_groups(
            retained,
            retained,
            max_span_days=max_span_days,
        )
    return [(start_date, end_date, len(days))]


def _validate_tick_frame_retention(
    frame,
    *,
    now=None,
    max_age_days: Optional[int] = None,
) -> None:
    """拒绝写入未来或超出指定保留窗的 Tick 行。

    ``FullIncrementThread`` 的统一写入器之外，旧的单线程、多进程和本地
    MiniQMT 入口也会直接调用 ``manager.save_tick_data``。这个轻量门禁放在
    viewer 层，确保这些路径不会因上游忽略日期边界而把不可再补的数据写入
    DuckDB。若返回表没有显式 ``time`` 列，则兼容以 DatetimeIndex/索引作为
    时间键的 MiniQMT 结果。
    """

    if frame is None or len(frame) == 0:
        return

    columns = getattr(frame, "columns", ())
    values = None
    if "time" in columns:
        values = frame["time"]
    else:
        index = getattr(frame, "index", None)
        # StockDB accepts a named/DatetimeIndex (and legacy unnamed datetime
        # indexes) as the time column. A plain RangeIndex is not a timestamp.
        if index is not None and index.__class__.__name__ != "RangeIndex":
            values = index
    if values is None:
        error = RuntimeError("tick 返回缺少 time，拒绝写入")
        setattr(error, "code", "DATA_QUALITY")
        raise error

    from .time_utils import coerce_market_time

    parsed = coerce_market_time(values)
    if parsed.isna().any():
        error = RuntimeError("tick 返回含无效 time，拒绝写入")
        setattr(error, "code", "DATA_QUALITY")
        raise error
    current = now or datetime.now()
    if isinstance(current, datetime):
        today = current.date()
    elif isinstance(current, datetime_date):
        today = current
    else:  # pragma: no cover - defensive for third-party clock objects
        today = datetime.now().date()

    dates = parsed.dt.date
    if bool((dates > today).any()):
        error = RuntimeError("tick 返回含未来日期，拒绝写入")
        setattr(error, "code", "OUT_OF_RETENTION")
        raise error

    if max_age_days is not None:
        try:
            age = max(0, int(max_age_days))
        except (TypeError, ValueError, OverflowError):
            age = None
        if age is not None:
            cutoff = today - timedelta(days=age)
            if bool((dates < cutoff).any()):
                error = RuntimeError("tick 返回超出近一个月保留窗口，拒绝写入")
                setattr(error, "code", "OUT_OF_RETENTION")
                raise error

    # 同一批返回中的重复时间戳不能静默去重：tick 的盘口快照可能在同一
    # 毫秒内携带不同字段，交给 StockDB 的唯一键约束前先给出稳定错误码。
    duplicate_mask = parsed.duplicated(keep=False)
    if bool(duplicate_mask.any()):
        duplicate_count = int(duplicate_mask.sum())
        error = RuntimeError(
            f"tick 输入包含 {duplicate_count} 个重复时间戳，拒绝写入"
        )
        setattr(error, "code", "DUPLICATE_TIMESTAMP")
        setattr(error, "duplicate_count", duplicate_count)
        raise error


def _canonical_history_source(value: object = None, *, default: str = MINIQMT) -> str:
    """统一 GUI 历史导入来源；旧 ``bigqmt`` 必须显式迁移。

    ``duckdb_storage.history_sources.normalize_source`` 为兼容旧 live 配置，
    仍把 ``qmt`` 解释为 MiniQMT；历史导入界面需要区分大 QMT 原生桥，
    因此优先复用 ``cli.settings`` 的历史导入规范化器。
    """
    candidate = value
    if candidate is None or not str(candidate).strip():
        candidate = default
    enum_value = getattr(candidate, "value", None)
    if isinstance(enum_value, str):
        candidate = enum_value
    try:
        from kh_settings import normalize_history_import_source

        normalized = normalize_history_import_source(candidate)
    except ImportError:
        raw = str(candidate).strip().lower().replace(" ", "_")
        aliases = {
            "miniqmt": MINIQMT,
            "mini_qmt": MINIQMT,
            "mini-qmt": MINIQMT,
            "xt": MINIQMT,
            "xtdata": MINIQMT,
            "qmt_native": QMT_NATIVE,
            "qmt-native": QMT_NATIVE,
            "native_bigqmt": QMT_NATIVE,
            "native-bigqmt": QMT_NATIVE,
            "bigqmt_native": QMT_NATIVE,
            "bigqmt-native": QMT_NATIVE,
            "native_qmt": QMT_NATIVE,
            "native-qmt": QMT_NATIVE,
            "qmt": QMT_NATIVE,
        }
        if raw in {"bigqmt", "big_qmt", "legacy_bigqmt", "legacy-bigqmt"}:
            error = ValueError(
                "legacy_bigqmt_requires_migration: 旧 bigqmt 不会自动转换为 qmt_native"
            )
            setattr(error, "code", "legacy_bigqmt_requires_migration")
            raise error
        normalized = aliases.get(raw)
        if normalized is None:
            raise ValueError(f"不支持的历史行情导入源: {candidate}")
    if normalized not in (MINIQMT, QMT_NATIVE):
        raise ValueError(f"不支持的历史行情导入源: {candidate}")
    return str(normalized)


def _default_allow_gaps_for_source(source: object) -> bool:
    """Return the interactive import policy for an historical source.

    Big QMT can legitimately return verified rows for only part of a requested
    range (for example, the current trading day is still empty before the
    market opens).  The desktop import flow should keep those rows instead of
    discarding the whole bundle.  This remains an explicit source policy:
    MiniQMT keeps its historical strict behaviour, and the native write gate
    still rejects malformed, pending, empty-only, or quality-invalid results.
    """
    return _canonical_history_source(source) == QMT_NATIVE


def _history_source_display_name(value: object = None) -> str:
    """返回面向用户的来源名称，避免状态文案固定写死 MiniQMT。"""
    try:
        source = _canonical_history_source(value)
    except Exception:
        source = str(value or "").strip().lower()
    # 保留旧界面文案中的 ``miniQMT`` 大小写，兼容已有自动化脚本/测试；
    # 原生桥使用明确的中文名称，避免两种来源混淆。
    return "大QMT原生桥" if source == QMT_NATIVE else "miniQMT"


def _history_probe_details(result: object) -> dict:
    """把 ProbeResult/映射转换成可序列化的扁平详情。"""
    if result is None:
        return {}
    if isinstance(result, dict):
        details = dict(result)
    else:
        try:
            details = dict(result.to_dict())  # type: ignore[attr-defined]
        except Exception:
            try:
                details = dict(result.as_dict())  # type: ignore[attr-defined]
            except Exception:
                details = {}
    nested = details.get("details")
    if isinstance(nested, dict):
        merged = dict(nested)
        merged.update({key: value for key, value in details.items() if key != "details"})
        details = merged
    return details


def _history_probe_ready(result: object, source: object = None) -> bool:
    """判断当前来源是否真的可执行；原生桥严格要求 ``bridge_ready``。"""
    details = _history_probe_details(result)
    try:
        normalized = _canonical_history_source(
            source if source is not None else details.get("source")
        )
    except Exception:
        normalized = str(source or details.get("source") or "")
    if normalized == QMT_NATIVE:
        # 原生桥的 endpoint/process 可见并不等于协议可用；唯一允许
        # 启动下载的前门禁是 detector 明确给出的 bridge_ready=True。
        return bool(details.get("bridge_ready", False))
    for key in ("available", "ok", "healthy", "ready"):
        if key in details:
            return bool(details.get(key))
    for key in ("available", "ok", "healthy"):
        if hasattr(result, key):
            try:
                return bool(getattr(result, key))
            except Exception:
                return False
    return bool(result)


def _coerce_retry_settings(max_task_retries: object = None,
                           retry_backoff: object = None) -> tuple[int, tuple[float, ...]]:
    """解析重试配置，坏值安全回到稳定默认值。"""
    try:
        retries = max(0, min(10, int(max_task_retries)))
    except Exception:
        retries = DEFAULT_HISTORY_MAX_TASK_RETRIES
    if retry_backoff is None:
        backoff = DEFAULT_HISTORY_RETRY_BACKOFF
    else:
        try:
            if isinstance(retry_backoff, str):
                retry_backoff = json.loads(retry_backoff)
            backoff = tuple(max(0.0, float(value)) for value in retry_backoff)
            if not backoff:
                backoff = DEFAULT_HISTORY_RETRY_BACKOFF
        except Exception:
            backoff = DEFAULT_HISTORY_RETRY_BACKOFF
    return retries, tuple(backoff)


def _history_instance_generation(value: object = None) -> str:
    """从探测详情提取原生桥代次（兼容历史字段名）。

    Detector/heartbeat versions in the wild use ``generation``,
    ``instance_generation`` or ``expected_generation`` interchangeably.  The
    worker protocol has one canonical field, so GUI code resolves aliases at
    the boundary and never invents a generation when the probe did not report
    one.  Nested ``details`` mappings are handled because ProbeResult is often
    persisted in that shape by QSettings.
    """

    seen: set[int] = set()

    def visit(candidate: object) -> str:
        if candidate is None:
            return ""
        # Constructors commonly receive the already-normalized generation as
        # a plain string.  Treat scalar identity values directly; otherwise
        # the mapping/attribute walk below would (correctly for arbitrary
        # objects) find no field and silently drop the generation.
        if isinstance(candidate, (str, bytes, int, float)) and not isinstance(candidate, bool):
            if isinstance(candidate, bytes):
                candidate = candidate.decode("utf-8", "ignore")
            text = str(candidate).strip()
            return text
        if isinstance(candidate, Mapping):
            marker = id(candidate)
            if marker in seen:
                return ""
            seen.add(marker)
            for key in (
                "instance_generation", "generation", "expected_generation",
                "bridge_generation", "connection_generation",
            ):
                raw = candidate.get(key)
                if raw is not None and str(raw).strip():
                    return str(raw).strip()
            nested = candidate.get("details")
            if nested is not candidate:
                result = visit(nested)
                if result:
                    return result
            return ""
        for name in (
            "instance_generation", "generation", "expected_generation",
            "bridge_generation", "connection_generation",
        ):
            try:
                raw = getattr(candidate, name, None)
            except Exception:
                raw = None
            if raw is not None and str(raw).strip():
                return str(raw).strip()
        return ""

    return visit(value)


def _normalise_generation_aliases(
    instance_generation: object = None,
    generation: object = None,
    expected_generation: object = None,
) -> str:
    """Validate generation aliases and return the canonical string.

    A disagreement means the caller may be targeting a different QMT
    strategy instance after a restart; silently preferring one alias would be
    unsafe.  Raise the same stable code used by the bridge/service layer.
    """

    values = []
    for value in (instance_generation, generation, expected_generation):
        if isinstance(value, bool):
            error = ValueError("实例代次必须是字符串")
            setattr(error, "code", "INVALID_REQUEST")
            raise error
        text = str(value or "").strip()
        if not text:
            continue
        if len(text) > INSTANCE_GENERATION_MAX_LENGTH or any(
            ord(character) < 32 for character in text
        ):
            error = ValueError(
                f"实例代次非法（最多 {INSTANCE_GENERATION_MAX_LENGTH} 个字符且不得含控制字符）"
            )
            setattr(error, "code", "INVALID_REQUEST")
            raise error
        if text not in values:
            values.append(text)
    if len(values) > 1:
        error = ValueError(
            "instance_generation、generation、expected_generation 不一致"
        )
        setattr(error, "code", "INVALID_REQUEST")
        raise error
    return values[0] if values else ""


def _strict_native_bool(value: object, name: str, *, allow_none: bool = False):
    """Validate a native execution flag without truthiness coercion."""
    if value is None and allow_none:
        return None
    if type(value) is not bool:
        error = ValueError(f"{name} 必须是布尔值")
        setattr(error, "code", "INVALID_REQUEST")
        raise error
    return value


def _configure_history_importer_defaults(
    importer: object,
    source: object,
    *,
    force: bool = False,
    allow_gaps: bool = False,
    local_only: bool = False,
    idempotent: bool = True,
    max_attempts: object = None,
    # ``None`` means the caller did not pin the incremental flag.  For the
    # native bridge derive it from an explicitly selected execution mode
    # below; using ``True`` as the helper default used to emit the contradictory
    # pair ``incrementally=True, mode=historical-backfill`` whenever a GUI or
    # scheduled caller selected backfill without also passing the legacy flag.
    incrementally: object = None,
    instance_generation: object = None,
    generation: object = None,
    expected_generation: object = None,
    native_profile: object = None,
    profile: object = None,
    max_inflight: object = None,
    batch_size: object = None,
    span_rows: object = None,
    span_bytes: object = None,
    mode: object = None,
    cache_strategy: object = None,
    cancel_after: object = None,
) -> object:
    """Attach execution defaults to a ``MultiProcessImporter`` instance.

    Older plugins/tests provide a compatible importer with a narrower
    constructor.  Setting attributes after construction preserves that API
    while the bundled worker's ``add_task`` reads these defaults into every
    payload.  Native QMT gets explicit safe defaults; MiniQMT keeps its
    historical omission of native-only ``max_attempts``/generation fields.
    """

    normalized_source = _canonical_history_source(source)
    canonical_generation = _normalise_generation_aliases(
        instance_generation, generation, expected_generation
    )

    # These four flags are harmless for MiniQMT and are intentionally set for
    # all sources so custom importer implementations can inspect a uniform
    # execution contract.
    values = {
        "default_force": bool(force),
        "default_allow_gaps": bool(allow_gaps),
        "default_local_only": bool(local_only),
        "default_idempotent": bool(idempotent),
    }
    for name, value in values.items():
        try:
            setattr(importer, name, value)
        except Exception:
            pass

    # Normalize execution hints before deriving the native tri-state
    # ``incrementally`` default.  The mode is the authoritative choice when
    # the legacy flag was omitted.
    native_options = normalize_native_execution_options(
        {
            "native_profile": native_profile,
            "profile": profile,
            "max_inflight": max_inflight,
            "batch_size": batch_size,
            "span_rows": span_rows,
            "span_bytes": span_bytes,
            "mode": mode,
            "cache_strategy": cache_strategy,
        },
        strict=False,
    )

    # ``None`` means “leave the legacy MiniQMT worker default alone”.  Native
    # bridge requests must be explicit and deterministic, including the
    # incrementally bit and bridge attempt budget.
    if normalized_source == QMT_NATIVE:
        if incrementally is not None:
            # Keep this module Qt/self-contained; importing the data-source
            # manager here would create a circular import during GUI startup.
            # Validate the wire flag locally instead of relying on an
            # unavailable helper (older revisions accidentally raised
            # ``NameError`` for every explicit value).
            if type(incrementally) is not bool:
                error = ValueError("incrementally 必须是布尔值")
                setattr(error, "code", "INVALID_REQUEST")
                raise error
            if (
                native_options["mode"] == "historical-backfill"
                and incrementally is True
            ) or (
                native_options["mode"] == "tail-sync"
                and incrementally is False
            ):
                error = ValueError("incrementally 与 mode 的组合不一致")
                setattr(error, "code", "INVALID_REQUEST")
                raise error
        if incrementally is None:
            incrementally = (
                False
                if native_options["mode"] == "historical-backfill"
                else True
            )
        try:
            attempts = DEFAULT_NATIVE_MAX_ATTEMPTS if max_attempts is None else int(max_attempts)
        except (TypeError, ValueError, OverflowError):
            attempts = DEFAULT_NATIVE_MAX_ATTEMPTS
        if attempts < 1:
            attempts = DEFAULT_NATIVE_MAX_ATTEMPTS
        native_values = {
            "default_incrementally": bool(incrementally),
            "default_max_attempts": attempts,
            "default_instance_generation": canonical_generation,
            # Aliases make this helper work with older/newer worker builds.
            "default_generation": canonical_generation,
            "default_expected_generation": canonical_generation,
        }
        native_values.update({
            "default_native_profile": native_options["native_profile"],
            "default_profile": native_options["profile"],
            "default_max_inflight": native_options["max_inflight"],
            "default_batch_size": native_options["batch_size"],
            "default_span_rows": native_options["span_rows"],
            "default_span_bytes": native_options["span_bytes"],
            "default_native_mode": native_options["mode"],
            "default_mode": native_options["mode"],
            "default_cache_strategy": native_options["cache_strategy"],
            "default_cancel_after": native_options["cancel_after"],
        })
        for name, value in native_values.items():
            try:
                setattr(importer, name, value)
            except Exception:
                pass
    else:
        # Do not add new keys to legacy MiniQMT payloads unless a caller
        # explicitly requested them; this preserves exact task dictionaries
        # expected by older integrations.
        if incrementally is not None:
            try:
                setattr(importer, "default_incrementally", bool(incrementally))
            except Exception:
                pass
        if max_attempts is not None:
            try:
                setattr(importer, "default_max_attempts", int(max_attempts))
            except (TypeError, ValueError, OverflowError):
                pass
        if canonical_generation:
            for name in (
                "default_instance_generation", "default_generation",
                "default_expected_generation",
            ):
                try:
                    setattr(importer, name, canonical_generation)
                except Exception:
                    pass
    return importer


def _effective_history_workers(source: object, requested: object, *, default: int = 1) -> tuple[int, int]:
    """Return ``(requested, effective)`` worker counts for GUI importers.

    MiniQMT keeps its historical caller-selected concurrency.  The native
    bridge has a single QMT-side consumer, so GUI fan-out is counterproductive
    and makes cancellation/ordering less deterministic; expose both values so
    the UI can explain why ``--workers``/the spin box was capped.
    """
    try:
        requested_value = int(requested or default)
    except (TypeError, ValueError, OverflowError):
        requested_value = int(default or 1)
    requested_value = max(1, min(requested_value, 12))
    try:
        normalized = _canonical_history_source(source)
    except Exception:
        normalized = str(source or "").strip().lower()
    effective = 1 if normalized == QMT_NATIVE else requested_value
    return requested_value, effective


# A native worker is deliberately single-process, but it still needs enough
# queued descriptors to merge compatible stock codes into one bridge bundle.
# Descriptors are tiny and native results cross the process boundary as file
# references, so a bounded 64-task look-ahead gives daily K-lines their full
# bridge batch without creating a DataFrame/IPC memory spike.
NATIVE_GUI_PREFETCH_LIMIT = 64


def _history_task_sort_key(task: Mapping, source: object) -> tuple:
    """Group native tasks by period/range so worker look-ahead can bundle.

    MiniQMT keeps the historical stock-first ordering.  For native QMT the
    bridge compatibility key includes period and range; interleaving three
    periods stock-by-stock prevents otherwise compatible codes from ever
    becoming adjacent in the queue.
    """
    stock = str(task.get("stock") or task.get("stock_code") or "")
    period = str(getattr(task.get("period"), "value", task.get("period") or ""))
    start = str(task.get("start") or task.get("start_date") or "")
    end = str(task.get("end") or task.get("end_date") or "")
    try:
        normalized = _canonical_history_source(source)
    except Exception:
        normalized = str(source or "").strip().lower()
    if normalized == QMT_NATIVE:
        return period.lower(), start, end, stock
    return stock, period.lower(), start, end


def _history_prefetch_limit(source: object, total: object, workers: object) -> int:
    """Return a bounded task-descriptor queue target for GUI importers."""
    try:
        total_value = max(0, int(total or 0))
    except (TypeError, ValueError, OverflowError):
        total_value = 0
    try:
        worker_value = max(1, int(workers or 1))
    except (TypeError, ValueError, OverflowError):
        worker_value = 1
    try:
        normalized = _canonical_history_source(source)
    except Exception:
        normalized = str(source or "").strip().lower()
    target = NATIVE_GUI_PREFETCH_LIMIT if normalized == QMT_NATIVE else worker_value
    return min(total_value, max(worker_value, target))


def _cancel_history_runner(runner: object, *, timeout: float = 2.0) -> int:
    """Best-effort cancellation shared by direct and multiprocess GUI paths."""
    if runner is None:
        return 0
    for name in ("cancel", "cancel_active", "stop"):
        method = getattr(runner, name, None)
        if not callable(method):
            continue
        try:
            if name == "cancel":
                try:
                    import inspect
                    parameters = inspect.signature(method).parameters
                except (TypeError, ValueError):
                    parameters = None
                if parameters is None or "timeout" in parameters or any(
                    parameter.kind == parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                ):
                    result = method(timeout=timeout)
                else:
                    result = method()
            else:
                result = method()
        except Exception:
            continue
        try:
            return int(result or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _finalize_native_worker_result(
    result: object,
    *,
    frame: object = None,
    runner: object = None,
    committed: bool = False,
    status_callback=None,
) -> dict:
    """ACK/清理原生 worker 结果（MiniQMT 路径自动无操作）。"""
    outcome = finalize_native_result(
        result,
        frame=frame,
        runner=runner,
        committed=committed,
    )
    if status_callback and isinstance(outcome, Mapping):
        for item in outcome.get("ack_errors") or ():
            if isinstance(item, Mapping):
                status_callback(
                    f"⚠ 原生桥任务 {item.get('job_id') or ''} ACK 清理失败: "
                    f"{item.get('error') or '未知错误'}"
                )
    return outcome


def _is_native_legal_empty(result: object, source: str = "") -> bool:
    """Return whether a task result represents an authenticated legal empty interval (e.g. suspension)."""
    if not isinstance(result, Mapping):
        return False
    if result.get("legal_empty") is True:
        return True
    if str(result.get("coverage_state") or "").strip().lower() == "legal_empty":
        return True
    meta = result.get("metadata")
    if isinstance(meta, Mapping):
        if meta.get("legal_empty") is True:
            return True
        if str(meta.get("coverage_state") or "").strip().lower() == "legal_empty":
            return True
    quality = result.get("quality")
    if isinstance(quality, Mapping):
        by_adj = quality.get("by_adjustment")
        if isinstance(by_adj, Mapping):
            for q in by_adj.values():
                if isinstance(q, Mapping) and (
                    q.get("legal_empty") is True
                    or str(q.get("coverage_state") or "").strip().lower() == "legal_empty"
                ):
                    return True
    source_name = str(source or result.get("source") or "").strip().lower()
    if (
        source_name == QMT_NATIVE
        and bool(result.get("success"))
        and int(result.get("records", 0) or 0) == 0
        and str(result.get("state") or "").strip().lower() in {"success", "done", "ok", "complete", "completed"}
    ):
        return True
    return False


def _native_worker_result_write_safe(
    result: object,
    *,
    allow_gaps: bool = False,
) -> bool:
    """Return whether a native worker envelope may enter a GUI write path.

    The bundled worker applies this gate before decoding.  Keep the check at
    each GUI boundary as well because frozen/third-party worker replacements
    can still return a truthy ``success`` flag alongside an aggregate partial
    marker.  ``allow_gaps`` is an explicit opt-in; unknown/malformed native
    envelopes fail closed.
    """
    try:
        def _payload_shape_safe(value):
            """Validate the compact worker payload without decoding rows."""
            if isinstance(value, Mapping):
                root_value = value
            else:
                converter_value = getattr(value, "as_dict", None)
                root_value = None
                if callable(converter_value):
                    try:
                        root_value = converter_value(include_rows=False)
                    except TypeError:
                        try:
                            root_value = converter_value()
                        except Exception:
                            root_value = None
                    except Exception:
                        root_value = None
                if not isinstance(root_value, Mapping):
                    return False
            payload_value = root_value.get("df_dict")
            if payload_value is None:
                payload_value = root_value.get("data_ref")
            if not isinstance(payload_value, Mapping):
                return False
            transport_value = str(payload_value.get("transport") or "").strip()
            if transport_value:
                path_value = payload_value.get("path")
                if isinstance(path_value, bytes):
                    try:
                        path_value = path_value.decode("utf-8", "strict")
                    except UnicodeDecodeError:
                        return False
                if not isinstance(path_value, str) or not path_value.strip():
                    return False
                return True
            data_value = payload_value.get("data")
            if not isinstance(data_value, (Mapping, list, tuple)):
                return False
            columns_value = payload_value.get("columns")
            index_value = payload_value.get("index")
            return (
                (columns_value is None or isinstance(columns_value, (list, tuple)))
                and (index_value is None or isinstance(index_value, (list, tuple)))
            )

        # A terminal metadata envelope still needs a structurally valid
        # compact frame reference.  This check deliberately does not inspect
        # row values; ``dict_to_dataframe``/the integrity scanner performs the
        # expensive validation after this bounded fence.
        if _native_result_completion_gate(result):
            return _payload_shape_safe(result)
        if not allow_gaps:
            return False

        # ``allow_gaps`` permits writing *verified child rows* from a partial
        # bundle; it is not a bypass for an arbitrary truthy envelope.  The
        # worker normally performs the expensive row/quality checks before
        # crossing this GUI boundary, so keep this second fence compact and
        # metadata-only.  In particular, QMT's ``True``/``None`` acceptance
        # sentinel, a pending state, a null marker, or a malformed frame
        # reference must never reach DuckDB even when gaps were opted in.
        if isinstance(result, Mapping):
            root = result
        else:
            converter = getattr(result, "as_dict", None)
            root = None
            if callable(converter):
                try:
                    root = converter(include_rows=False)
                except TypeError:
                    try:
                        root = converter()
                    except Exception:
                        root = None
                except Exception:
                    root = None
            if not isinstance(root, Mapping):
                return False

        def _strict_bool_marker(container, name):
            if name not in container:
                return True
            return type(container.get(name)) is bool

        def _nonnegative_counter(container, name, *, allow_positive=True):
            if name not in container:
                return True
            value = container.get(name)
            if isinstance(value, bool) or value is None:
                return False
            if isinstance(value, float) and not value.is_integer():
                return False
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                return False
            return parsed >= 0 and (allow_positive or parsed == 0)

        def _partial_metadata_ok(container):
            if not isinstance(container, Mapping):
                return False
            if not _strict_bool_marker(container, "native_complete"):
                return False
            if not _strict_bool_marker(container, "bundle_incomplete"):
                return False
            for name in ("missing", "missing_segments", "gaps"):
                if name in container and not isinstance(
                    container.get(name), (list, tuple)
                ):
                    return False
            # A positive missing count is expected under allow_gaps.  A
            # malformed/truncated count is not evidence that child rows were
            # verified, so reject it instead of silently writing them.
            for name in ("missing_count", "gap_count", "empty_segments_count"):
                if not _nonnegative_counter(container, name):
                    return False
            for name in ("segments_malformed_count", "segments_incomplete_count"):
                if not _nonnegative_counter(container, name, allow_positive=False):
                    return False
            if "missing_segments_malformed" in container and (
                type(container.get("missing_segments_malformed")) is not bool
                or container.get("missing_segments_malformed") is True
            ):
                return False
            if "missing_segments_truncated" in container:
                value = container.get("missing_segments_truncated")
                if (
                    isinstance(value, bool)
                    or value is None
                    or (isinstance(value, float) and not value.is_integer())
                ):
                    return False
                try:
                    if int(value) != 0:
                        return False
                except (TypeError, ValueError, OverflowError):
                    return False
            return True

        # Validate the root and its compact metadata.  ``success``/``ok``
        # must be real booleans; a string/1 is a protocol violation rather
        # than proof that the frame is writable.
        for name in ("success", "ok"):
            if name in root and root.get(name) is not True:
                return False
            if name in root and type(root.get(name)) is not bool:
                return False
        if not _strict_bool_marker(root, "native_complete"):
            return False
        if root.get("error") not in (None, "") or root.get("error_code") not in (None, ""):
            return False
        state = str(root.get("state", root.get("status", "")) or "").strip().lower()
        partial_states = {"partial", "done_with_gaps"}
        nonterminal_states = {
            "accepted", "queued", "pending", "running", "waiting",
            "processing", "submitted", "cancelling",
        }
        failed_states = {"failed", "cancelled", "canceled", "error", "timeout"}
        if state in nonterminal_states or state in failed_states:
            return False
        if state and state not in partial_states:
            # A complete state would have passed the strict gate.  Treat an
            # unknown/contradictory state as malformed rather than accepting
            # it merely because ``allow_gaps`` is true.
            return False
        marker = root.get("native_complete")
        metadata = root.get("metadata")
        if metadata is not None and not _partial_metadata_ok(metadata):
            return False
        if not _partial_metadata_ok(root):
            return False

        metadata_state = ""
        if isinstance(metadata, Mapping):
            metadata_state = str(
                metadata.get("bundle_state", metadata.get("state", "")) or ""
            ).strip().lower()
            if metadata_state in nonterminal_states or metadata_state in failed_states:
                return False
            if metadata_state and metadata_state not in partial_states:
                return False
        # There must be explicit evidence that this is a partial terminal
        # result.  An omitted state/marker is the old asynchronous acceptance
        # shape and is intentionally rejected here.
        if state not in partial_states and metadata_state not in partial_states and marker is not False:
            return False

        # Worker payloads are either the inline ``{data,index,columns}``
        # shape or the bounded file reference.  Requiring a mapping with a
        # non-empty data/path field catches ``df_dict=True``/``None`` and
        # prevents the GUI from interpreting an acceptance envelope as an
        # empty successful frame.
        if not _payload_shape_safe(root):
            return False
        if "records" in root and not _nonnegative_counter(root, "records"):
            return False

        details = root.get("native_job_details")
        if details is None and isinstance(metadata, Mapping):
            details = metadata.get("native_job_details")
        if isinstance(details, Mapping):
            details = list(details.values())
        if details is not None and not isinstance(details, (list, tuple, set, frozenset)):
            return False
        if isinstance(details, (list, tuple, set, frozenset)):
            for detail in list(details)[:512]:
                if not isinstance(detail, Mapping) or not _partial_metadata_ok(detail):
                    return False
                detail_state = str(
                    detail.get("state", detail.get("status", "")) or ""
                ).strip().lower()
                if detail_state in nonterminal_states:
                    return False
                if detail_state and detail_state not in (
                    partial_states | failed_states | {"done", "success", "ok", "complete", "completed", "empty"}
                ):
                    return False
                for nested_name in ("job", "handle"):
                    nested = detail.get(nested_name)
                    if nested is not None and (
                        not isinstance(nested, Mapping)
                        or not _partial_metadata_ok(nested)
                    ):
                        return False
                    if isinstance(nested, Mapping):
                        nested_state = str(nested.get("state", "") or "").strip().lower()
                        if nested_state in nonterminal_states:
                            return False
        return True
    except Exception:
        return False


def _native_bundle_completion_status(value: object) -> tuple[bool, dict]:
    """Return a bounded completion decision for a native bundle.

    ``NativeBundleResult`` deliberately keeps row payloads under
    ``by_code[*].results`` while the aggregate state lives on the outer
    envelope/manifest.  The direct GUI importer used to decode those rows
    without checking the outer state, which could write a partial bundle when
    ``allow_gaps=False``.  Keep this helper metadata-only and conservative;
    an unknown envelope is never considered ACK/write-safe.

    The returned diagnostics contain only small scalar fields and a bounded
    gap list, so they are safe to put on the GUI result dictionary.
    """

    complete_states = {"success", "done", "ok", "complete", "completed"}
    bad_states = {
        "partial", "done_with_gaps", "failed", "cancelled", "canceled",
        "error", "timeout", "accepted", "queued", "pending", "running",
        "waiting", "processing", "submitted", "cancelling",
    }

    def _mapping(item: object):
        if isinstance(item, Mapping):
            return item
        converter = getattr(item, "as_dict", None)
        if callable(converter):
            try:
                converted = converter(include_rows=False)
            except TypeError:
                try:
                    converted = converter()
                except Exception:
                    converted = None
            except Exception:
                converted = None
            if isinstance(converted, Mapping):
                return converted
        return None

    diagnostics: dict = {}

    def _gap_fields_bad(container: object) -> bool:
        """Validate compact gap diagnostics without truthiness coercion."""

        if not isinstance(container, Mapping):
            return False
        for key in ("missing", "missing_segments", "gaps"):
            if key not in container:
                continue
            raw = container.get(key)
            if not isinstance(raw, (list, tuple)) or raw:
                return True
        for key in ("missing_count", "gap_count"):
            if key not in container:
                continue
            raw = container.get(key)
            if raw is None or isinstance(raw, bool):
                return True
            try:
                parsed = int(raw)
                if isinstance(raw, float) and not raw.is_integer():
                    return True
                if parsed < 0 or parsed > 0:
                    return True
            except (TypeError, ValueError, OverflowError):
                return True
        if "missing_segments_malformed" in container:
            raw = container.get("missing_segments_malformed")
            if type(raw) is not bool or raw is True:
                return True
        if "missing_segments_truncated" in container:
            raw = container.get("missing_segments_truncated")
            if isinstance(raw, bool) or raw is None:
                return True
            try:
                parsed = int(raw)
                if isinstance(raw, float) and not raw.is_integer():
                    return True
                if parsed < 0 or parsed > 0:
                    return True
            except (TypeError, ValueError, OverflowError):
                return True
        for key in (
            "empty_segments_count", "segments_malformed_count",
            "segments_incomplete_count",
        ):
            if key not in container:
                continue
            raw = container.get(key)
            if isinstance(raw, bool) or raw is None:
                return True
            try:
                parsed = int(raw)
                if isinstance(raw, float) and not raw.is_integer():
                    return True
                if parsed < 0 or parsed > 0:
                    return True
            except (TypeError, ValueError, OverflowError):
                return True
        return False

    root = _mapping(value)
    if root is None:
        # A frozen compatibility service may still return a list of
        # HistoryResult objects.  Accept it only when every item explicitly
        # reports a gap-free terminal success.
        if isinstance(value, (list, tuple)) and value:
            for item in value:
                ok = getattr(item, "ok", getattr(item, "success", False))
                status = str(
                    getattr(item, "status", getattr(item, "state", "")) or ""
                ).strip().lower()
                if ok is not True or status in bad_states or (
                    status and status not in complete_states
                ):
                    return False, {"reason": "legacy_result_incomplete"}
                if bool(getattr(item, "is_partial", False)) or bool(
                    getattr(item, "missing", ())
                ):
                    return False, {"reason": "legacy_result_gap"}
                metadata = getattr(item, "metadata", None)
                if isinstance(metadata, Mapping):
                    if metadata.get("bundle_incomplete") is True or (
                        "native_complete" in metadata
                        and metadata.get("native_complete") is not True
                    ) or _gap_fields_bad(metadata):
                        return False, {"reason": "legacy_metadata_incomplete"}
            return True, {}
        return False, {"reason": "invalid_bundle"}

    diagnostics["bundle_id"] = str(root.get("bundle_id") or "")[:128]
    state_value = root.get("state", root.get("status"))
    state = str(state_value or "").strip().lower()
    if state:
        diagnostics["state"] = state
    marker_present = "native_complete" in root
    marker = root.get("native_complete")
    if marker_present:
        diagnostics["native_complete"] = marker

    gap_items = []

    def _gap_marker(container: Mapping) -> bool:
        raw = None
        for marker_name in ("missing", "missing_segments", "gaps"):
            if marker_name not in container:
                continue
            candidate = container.get(marker_name)
            if not isinstance(candidate, (list, tuple)):
                gap_items.append({"value": str(candidate)[:128]})
                return True
            if candidate:
                raw = candidate
                break
        if raw:
            if isinstance(raw, Mapping):
                raw = [raw]
            if isinstance(raw, (list, tuple)):
                for item in raw[:16]:
                    if isinstance(item, Mapping):
                        gap_items.append(dict(item))
                    else:
                        gap_items.append({"value": str(item)[:128]})
            else:
                gap_items.append({"value": str(raw)[:128]})
            return True
        for key in ("missing_count", "gap_count"):
            if key not in container:
                continue
            raw = container.get(key)
            if raw is None or isinstance(raw, bool):
                return True
            try:
                parsed = int(raw)
                if isinstance(raw, float) and not raw.is_integer():
                    return True
                if parsed < 0 or parsed > 0:
                    return True
            except (TypeError, ValueError, OverflowError):
                return True
        if "missing_segments_malformed" in container:
            raw = container.get("missing_segments_malformed")
            if type(raw) is not bool or raw is True:
                return True
        if "missing_segments_truncated" in container:
            raw = container.get("missing_segments_truncated")
            if isinstance(raw, bool) or raw is None:
                return True
            try:
                parsed = int(raw)
                if isinstance(raw, float) and not raw.is_integer():
                    return True
                if parsed < 0 or parsed > 0:
                    return True
            except (TypeError, ValueError, OverflowError):
                return True
        for key in (
            "empty_segments_count", "segments_malformed_count",
            "segments_incomplete_count",
        ):
            if key not in container:
                continue
            raw = container.get(key)
            if isinstance(raw, bool) or raw is None:
                return True
            try:
                parsed = int(raw)
                if isinstance(raw, float) and not raw.is_integer():
                    return True
                if parsed < 0 or parsed > 0:
                    return True
            except (TypeError, ValueError, OverflowError):
                return True
        return False

    def _container_bad(container: object, *, is_manifest: bool = False) -> bool:
        if not isinstance(container, Mapping):
            return False
        marker_present = "native_complete" in container
        marker_value = container.get("native_complete")
        # ``native_complete=None`` is QMT's asynchronous acceptance/unknown
        # value, not a completion proof.  Distinguish an omitted legacy field
        # from an explicitly malformed marker and fail closed in both cases
        # where the producer attempted to assert completion.
        if marker_present and marker_value is not True:
            diagnostics.setdefault("reason", "native_complete_false")
            return True
        state_value = container.get("state", container.get("status"))
        state_value = str(state_value or "").strip().lower()
        if state_value in bad_states or (
            state_value and state_value not in complete_states
        ):
            diagnostics.setdefault("reason", "aggregate_state_%s" % state_value)
            return True
        if _gap_marker(container):
            diagnostics.setdefault("reason", "aggregate_gap")
            return True
        failed = container.get("failed")
        if failed:
            diagnostics.setdefault("reason", "aggregate_failed")
            if isinstance(failed, Mapping):
                diagnostics["failed_codes"] = [str(key)[:128] for key in list(failed)[:32]]
            elif isinstance(failed, (list, tuple, set, frozenset)):
                diagnostics["failed_codes"] = [str(item)[:128] for item in list(failed)[:32]]
            return True
        return False

    incomplete = _container_bad(root)
    manifest = root.get("manifest")
    if isinstance(manifest, Mapping):
        incomplete = _container_bad(manifest, is_manifest=True) or incomplete

    # Inspect explicit child state/metadata markers as a second fence.  Do
    # not inspect ``results`` so a large DataFrame/list is never traversed.
    by_code = root.get("by_code")
    if isinstance(by_code, Mapping):
        for code, item in list(by_code.items())[:256]:
            if not isinstance(item, Mapping):
                incomplete = True
                diagnostics.setdefault("reason", "invalid_code_entry")
                continue
            if "native_complete" in item and item.get("native_complete") is not True:
                incomplete = True
                diagnostics.setdefault("reason", "code_native_complete_false")
            item_state = str(item.get("state", item.get("status", "")) or "").strip().lower()
            if item_state in bad_states or (item_state and item_state not in complete_states):
                incomplete = True
                diagnostics.setdefault("reason", "code_state_%s" % item_state)
            if item.get("error") or item.get("error_code"):
                incomplete = True
                diagnostics.setdefault("reason", "code_error")
            if _gap_marker(item):
                incomplete = True
                diagnostics.setdefault("reason", "code_gap")
            metadata = item.get("adjustment_meta")
            if isinstance(metadata, Mapping):
                for view, meta in list(metadata.items())[:32]:
                    if not isinstance(meta, Mapping):
                        incomplete = True
                        diagnostics.setdefault("reason", "invalid_view_metadata")
                        continue
                    if "native_complete" in meta and meta.get("native_complete") is not True:
                        incomplete = True
                        diagnostics.setdefault("reason", "view_native_complete_false")
                    view_state = str(meta.get("state", meta.get("status", "")) or "").strip().lower()
                    if view_state in bad_states or (view_state and view_state not in complete_states):
                        incomplete = True
                        diagnostics.setdefault("reason", "view_state_%s" % view_state)
                    if _gap_marker(meta):
                        incomplete = True
                        diagnostics.setdefault("reason", "view_gap")

    if gap_items:
        diagnostics["gaps"] = gap_items[:16]
        diagnostics["gap_count"] = len(gap_items)
    return (not incomplete), diagnostics


def _task_dividend_types(period: object, dividend_types: Optional[List[str]]):
    """Tick 请求永远只提交不复权；bar 保留用户勾选的复权组合。"""
    period_value = getattr(period, "value", period)
    return [] if str(period_value or "").strip().lower() == "tick" else dividend_types


def _integrity_dividend_types(dividend_types):
    """规范化传给完整性扫描器的复权口径集合。

    ``None`` 在旧导入线程中表示“默认请求全部复权”；空列表表示用户只选
    不复权。返回空元组让扫描器保持 raw-only 语义，返回元组则要求每一种
    选中的复权列都存在。Tick 由扫描器自行忽略复权要求。
    """

    if dividend_types is None:
        return ("front", "back", "front_ratio", "back_ratio")
    if isinstance(dividend_types, str):
        dividend_types = (dividend_types,)
    allowed = {"front", "back", "front_ratio", "back_ratio"}
    result = []
    for value in dividend_types or ():
        text = str(getattr(value, "value", value) or "").strip().lower()
        if text in allowed and text not in result:
            result.append(text)
    return tuple(result)


def _probe_miniqmt_processes() -> dict:
    """只读收集 MiniQMT 进程提示，不把进程名当作 API 健康度。"""
    names = {"xtminiqmt.exe", "miniqmt.exe", "miniquote.exe", "xtdata.exe"}
    try:
        import psutil  # type: ignore
    except Exception:
        return {"process_open": None, "process_names": [], "process_pids": [], "process_probe": "unavailable"}
    found = []
    try:
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                info = proc.info or {}
                name = str(info.get("name") or "").strip()
                exe = str(info.get("exe") or "").strip()
                basename = os.path.basename(exe) if exe else name
                if name.casefold() in names or basename.casefold() in names:
                    found.append({
                        "pid": int(info.get("pid") or proc.pid),
                        "name": name or basename,
                        "exe": exe or None,
                    })
            except Exception:
                continue
    except Exception as exc:
        return {"process_open": None, "process_names": [], "process_pids": [], "process_probe": f"error:{type(exc).__name__}"}
    return {
        "process_open": bool(found),
        "process_names": sorted({item["name"] for item in found if item.get("name")}),
        "process_pids": sorted({item["pid"] for item in found if item.get("pid")}),
        "processes": found,
        "process_probe": "psutil",
    }


class _SkipQmtBenchmark(Exception):
    """内部控制流：原生桥模式不应触碰 xtquant 基准指数检查。"""


def _qt_object_is_deleted(obj) -> bool:
    """返回 PyQt 包装对象的底层 C++ 实例是否已经销毁。"""
    if obj is None:
        return True
    try:
        return bool(sip.isdeleted(obj))
    except (TypeError, RuntimeError):
        # 单元测试会使用轻量替身；非 SIP 对象按仍可用处理。
        return False


def _safe_qt_method_call(obj, method_name: str, *args) -> bool:
    """仅在 Qt 对象仍存活时调用方法，避免销毁阶段再次进入 SIP。"""
    if _qt_object_is_deleted(obj):
        return False
    try:
        getattr(obj, method_name)(*args)
        return True
    except (AttributeError, RuntimeError):
        return False


def _dispatch_import_dialog_destroyed(viewer, attr_name: str) -> None:
    """在获取已销毁 Qt 对象的绑定方法之前先完成 SIP 存活检查。"""
    if _qt_object_is_deleted(viewer):
        return
    DuckDBViewer._on_import_dialog_destroyed(viewer, attr_name)


def _dispatch_initial_viewer_scale(viewer) -> None:
    """忽略窗口已销毁后才到达事件队列的首帧缩放回调。"""
    if _qt_object_is_deleted(viewer):
        return
    DuckDBViewer.apply_ui_scale(viewer, viewer.font_scale)


def _resolve_preset_stock_pool_file(filename: str, legacy_dirs=()) -> Optional[str]:
    """解析预设股票池文件，用户更新优先，打包资源及旧目录兜底。"""
    managed_path = get_stock_pool_path(filename)
    if os.path.isfile(managed_path):
        return managed_path
    for directory in legacy_dirs:
        candidate = os.path.join(directory, filename)
        if os.path.isfile(candidate):
            return candidate
    return None


def _normalize_market_security_code(code: str) -> str:
    """统一数据管理各导入窗口的证券代码市场后缀规则。"""
    raw = str(code or "").strip().upper()
    numeric_code, explicit_market = split_security_code(raw)
    if explicit_market and numeric_code:
        return f"{numeric_code}.{explicit_market}"
    if not (numeric_code.isdigit() and len(numeric_code) == 6):
        return raw

    # 深市 12 系转债以及 15/16/17/18 系场内基金必须优先于笼统的
    # “1 开头默认上海”规则，否则 161226 会被误写为 161226.SH。
    if numeric_code.startswith(("12", "15", "16", "17", "180", "184")):
        return f"{numeric_code}.SZ"
    if numeric_code.startswith(("0", "2", "3")):
        return f"{numeric_code}.SZ"
    if numeric_code.startswith(("4", "8")):
        return f"{numeric_code}.BJ"
    if numeric_code.startswith(("1", "5", "6", "9")):
        return f"{numeric_code}.SH"
    return raw


def _increment_task_key(
    task: dict,
    dividend_types: Optional[List[str]] = None,
    force_overwrite: bool = False,
    source: Optional[str] = None,
) -> str:
    """生成断点任务键；影响结果的下载选项必须进入键空间。"""
    dividends = ",".join(sorted(set(dividend_types or []))) or "none-only"
    source_value = source or task.get("source") or MINIQMT
    try:
        source_value = normalize_source(source_value, default=None)
    except Exception:
        source_value = str(source_value).strip().lower() or MINIQMT
    return (
        f"v2|{task.get('stock', '')}|{task.get('period', '')}|"
        f"{task.get('start', '')}|{task.get('end', '')}|"
        f"div={dividends}|overwrite={int(bool(force_overwrite))}|source={source_value}"
    )


def _coalesce_metadata_records(records) -> List[tuple]:
    """同一证券/周期只刷新一次元数据，并合并可用的写入条数。"""
    merged = {}
    for record in records or []:
        if isinstance(record, dict):
            stock = record.get('stock_code') or record.get('stock')
            period = record.get('period')
            count = record.get('records')
        else:
            values = tuple(record)
            if len(values) < 2:
                continue
            stock, period = values[:2]
            count = values[2] if len(values) >= 3 else None
        if not stock or not period:
            continue
        key = (str(stock), str(period))
        if count is not None:
            merged[key] = int(merged.get(key) or 0) + int(count or 0)
        elif key not in merged:
            merged[key] = None
    return [
        (stock, period, count) if count is not None else (stock, period)
        for (stock, period), count in merged.items()
    ]


def _progress_key_stock_period(key: str) -> Optional[tuple]:
    """从 v2 断点键提取证券/周期，用于崩溃后的元数据可见性修复。"""
    if not isinstance(key, str) or not (key.startswith('v2|') or key.startswith('v3|')):
        return None
    parts = key.split('|')
    if len(parts) < 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


class ScheduledSyncStartupDialog(QDialog):
    """独立定时补充进程启动期间的无边框居中提示。"""

    def __init__(self, parent=None, scale: float = 1.0):
        super().__init__(parent)
        self.setObjectName("ScheduledSyncStartupDialog")
        self.setWindowModality(Qt.ApplicationModal)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setMinimumWidth(max(390, int(round(390 * scale))))
        self.setStyleSheet(f"""
            QDialog#ScheduledSyncStartupDialog {{
                background-color: #2b2b2b;
                border: 1px solid #4b4f54;
                border-radius: 9px;
            }}
            QLabel#StartupTitle {{
                color: #f0f0f0;
                background-color: transparent;
                font-size: {max(14, int(round(17 * scale)))}px;
                font-weight: bold;
            }}
            QLabel#StartupHint {{
                color: #a8adb3;
                background-color: transparent;
                font-size: {max(11, int(round(13 * scale)))}px;
            }}
            QProgressBar {{
                min-height: 7px;
                max-height: 7px;
                border: none;
                border-radius: 3px;
                background-color: #404348;
                text-align: center;
            }}
            QProgressBar::chunk {{
                border-radius: 3px;
                background-color: #168fe5;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 21, 24, 21)
        layout.setSpacing(9)
        title = QLabel("正在启动数据定时补充")
        title.setObjectName("StartupTitle")
        hint = QLabel("正在释放数据库连接并加载配置，请稍候…")
        hint.setObjectName("StartupHint")
        progress = QProgressBar()
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addSpacing(4)
        layout.addWidget(progress)

    def show_centered(self):
        """在父窗口所在显示器的可用区域正中央显示。"""
        self.adjustSize()
        desktop = QDesktopWidget()
        parent = self.parentWidget()
        screen_index = desktop.screenNumber(parent) if parent is not None else desktop.primaryScreen()
        screen_rect = desktop.availableGeometry(screen_index)
        self.move(screen_rect.center() - self.rect().center())
        self.show()
        self.raise_()


def _configure_download_log_widget(widget):
    """限制下载日志显示规模，避免长任务中 QTextDocument 持续膨胀。"""
    try:
        widget.document().setMaximumBlockCount(DOWNLOAD_LOG_MAX_BLOCKS)
    except Exception:
        try:
            widget.setMaximumBlockCount(DOWNLOAD_LOG_MAX_BLOCKS)
        except Exception:
            pass
    try:
        widget.setUndoRedoEnabled(False)
    except Exception:
        pass


def _append_download_log(widget, line: str):
    scrollbar = widget.verticalScrollBar()
    at_bottom = scrollbar.value() >= scrollbar.maximum() - 2
    if hasattr(widget, 'appendPlainText'):
        widget.appendPlainText(line)
    else:
        widget.append(line)
    if at_bottom:
        scrollbar.setValue(scrollbar.maximum())


def _should_update_download_ui(owner, attr_name: str, current: int = None, total: int = None) -> bool:
    force_update = current is None or current <= 1 or (total is not None and total > 0 and current >= total)
    now = time.monotonic()
    last_update = getattr(owner, attr_name, 0.0)
    if force_update or now - last_update >= DOWNLOAD_UI_UPDATE_MIN_INTERVAL:
        setattr(owner, attr_name, now)
        return True
    return False

# 尝试导入DuckDB模块
try:
    from .manager import DuckDBManager
    from .config import DuckDBConfig
    from .import_worker import (
        MultiProcessImporter,
        dict_to_dataframe,
        finalize_native_result,
        _native_result_completion_gate,
    )
except ImportError as e:
    try:
        from manager import DuckDBManager
        from config import DuckDBConfig
        from import_worker import (
            MultiProcessImporter,
            dict_to_dataframe,
            finalize_native_result,
            _native_result_completion_gate,
        )
    except ImportError:
        # A packaged/third-party deployment may still ship the pre-finalize
        # worker. Keep the GUI importable and make cleanup a no-op there.
        def finalize_native_result(*_args, **_kwargs):
            return {
                "source": "", "acknowledged": 0,
                "cleaned_ipc": 0, "ack_errors": [],
            }

        def _native_result_completion_gate(_value):
            # A packaged worker without the aggregate completion helper cannot
            # prove that a native result is gap-free.  Direct/MP GUI callers
            # therefore fail closed and retain the bridge artifact for TTL
            # recovery instead of writing an unknown partial envelope.
            return False

try:
    from .wal_repair import repair_data_root
except ImportError:
    try:
        from wal_repair import repair_data_root
    except ImportError:
        def repair_data_root(*args, **kwargs):
            logging.warning("wal_repair module not found")
            return []

# 尝试导入khQTTools
try:
    import khQTTools
except ImportError:
    try:
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import khQTTools
    except ImportError:
        khQTTools = None


class PandasModel(QAbstractTableModel):
    """Pandas DataFrame的Qt模型"""
    
    def __init__(self, df: pd.DataFrame = None):
        super().__init__()
        self._df = df if df is not None else pd.DataFrame()
        self._header_map = {
            'time': '时间',
            'open': '开盘价',
            'high': '最高价',
            'low': '最低价',
            'close': '收盘价',
            'volume': '成交量',
            'amount': '成交额',
            'settelementPrice': '今结算',
            'settlementPrice': '今结算',
            'openInterest': '持仓量',
            'openInt': '持仓量',
            'preClose': '前收价',
            'suspendFlag': '停牌标记',
            'open_front': '前复权开盘',
            'high_front': '前复权最高',
            'low_front': '前复权最低',
            'close_front': '前复权收盘',
            'open_back': '后复权开盘',
            'high_back': '后复权最高',
            'low_back': '后复权最低',
            'close_back': '后复权收盘',
            'open_front_ratio': '等比前复权开盘',
            'high_front_ratio': '等比前复权最高',
            'low_front_ratio': '等比前复权最低',
            'close_front_ratio': '等比前复权收盘',
            'open_back_ratio': '等比后复权开盘',
            'high_back_ratio': '等比后复权最高',
            'low_back_ratio': '等比后复权最低',
            'close_back_ratio': '等比后复权收盘',
            'turn': '换手率',
            'pctChg': '涨跌幅',
            'peTTM': '滚动市盈率',
            'psTTM': '滚动市销率',
            'pcfNcfTTM': '滚动市现率',
            'pbMRQ': '市净率',
            'isST': '是否ST',
            'lastPrice': '最新价',
            'lastClose': '昨收',
            'pvolume': '现量',
            'stockStatus': '状态',
            'lastSettlementPrice': '昨结算',
            'askPrice1': '卖一价',
            'askPrice2': '卖二价',
            'askPrice3': '卖三价',
            'askPrice4': '卖四价',
            'askPrice5': '卖五价',
            'bidPrice1': '买一价',
            'bidPrice2': '买二价',
            'bidPrice3': '买三价',
            'bidPrice4': '买四价',
            'bidPrice5': '买五价',
            'askVol1': '卖一量',
            'askVol2': '卖二量',
            'askVol3': '卖三量',
            'askVol4': '卖四量',
            'askVol5': '卖五量',
            'bidVol1': '买一量',
            'bidVol2': '买二量',
            'bidVol3': '买三量',
            'bidVol4': '买四量',
            'bidVol5': '买五量',
            'transactionNum': '成交笔数'
        }
        self._header_map_lower = {k.lower(): v for k, v in self._header_map.items()}
    
    def rowCount(self, parent=None):
        return len(self._df)
    
    def columnCount(self, parent=None):
        return len(self._df.columns)
    
    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        
        if role == Qt.DisplayRole:
            value = self._df.iloc[index.row(), index.column()]
            # 格式化显示
            if isinstance(value, float):
                return f"{value:.4f}"
            elif isinstance(value, (datetime, pd.Timestamp)):
                return value.strftime("%Y-%m-%d %H:%M:%S")
            return str(value)
        
        elif role == Qt.TextAlignmentRole:
            return Qt.AlignCenter
        
        return None
    
    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            if orientation == Qt.Horizontal:
                field = str(self._df.columns[section])
                normalized = field.strip()
                cn_name = self._header_map.get(normalized)
                if not cn_name:
                    cn_name = self._header_map_lower.get(normalized.lower())
                if cn_name:
                    return f"{normalized}({cn_name})"
                return field
            else:
                return str(section + 1)
        return None
    
    def update_data(self, df: pd.DataFrame):
        """更新数据"""
        self.beginResetModel()
        self._df = df if df is not None else pd.DataFrame()
        self.endResetModel()


class DataLoadThread(QThread):
    """数据加载线程"""
    finished = pyqtSignal(object)  # 返回DataFrame
    error = pyqtSignal(str)
    progress = pyqtSignal(int)
    
    def __init__(self, manager: DuckDBManager, stock_code: str, period: str,
                 start_time: str = None, end_time: str = None):
        super().__init__()
        self.manager = manager
        self.stock_code = stock_code
        self.period = period
        self.start_time = start_time
        self.end_time = end_time
    
    def run(self):
        try:
            self.progress.emit(50)
            df = self.manager.get_kline_data(
                self.stock_code, self.period,
                self.start_time, self.end_time
            )
            self.progress.emit(100)
            self.finished.emit(df)
        except Exception as e:
            self.error.emit(str(e))


class ScanThread(QThread):
    """只读核验未纳入索引的数据库文件。"""
    result_ready = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(int)
    
    def __init__(self, manager: DuckDBManager):
        super().__init__()
        self.manager = manager
        self.result = None
        self.error_message = ""
    
    def run(self):
        try:
            summary = self.manager.audit_unindexed_databases(
                progress_callback=self.progress.emit,
                should_stop=self.isInterruptionRequested,
                process_isolation=True,
            )
            self.result = summary
            self.result_ready.emit(summary)
        except Exception as e:
            self.error_message = str(e)
            if not self.isInterruptionRequested():
                self.error.emit(self.error_message)


class WalRepairThread(QThread):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, data_root: str, memory_limit: str = "256MB"):
        super().__init__()
        self.data_root = data_root
        self.memory_limit = memory_limit

    def run(self):
        try:
            result = repair_data_root(self.data_root, self.memory_limit)
            if result is None:
                self.error.emit("修复失败：无效的数据目录")
                return
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class WrapHeaderView(QHeaderView):
    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setDefaultAlignment(Qt.AlignCenter)
        self.setMinimumHeight(48)

    def _wrap_text(self, text: str) -> str:
        try:
            import re
            s = str(text)
            if "(" in s:
                return s.replace("(", "\n(")
            s = s.replace("_", "\n")
            s = re.sub(r'([a-z])([A-Z])', r'\1\n\2', s)
            return s
        except Exception:
            return str(text)

    def paintSection(self, painter, rect, logicalIndex):
        if not rect.isValid():
            return
        painter.save()
        bg_color = self.palette().color(QPalette.Button)
        border_color = self.palette().color(QPalette.Dark)
        painter.fillRect(rect, bg_color)
        painter.setPen(border_color)
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        model = self.model()
        if model is not None:
            text = model.headerData(logicalIndex, self.orientation(), Qt.DisplayRole)
        else:
            text = None
        if text is not None:
            display_text = self._wrap_text(text)
            painter.setFont(self.font())
            painter.setPen(self.palette().color(QPalette.ButtonText))
            painter.drawText(
                rect.adjusted(4, 2, -4, -2),
                Qt.AlignCenter | Qt.TextWordWrap,
                display_text
            )
        painter.restore()

    def sectionSizeFromContents(self, logicalIndex):
        size = super().sectionSizeFromContents(logicalIndex)
        text = self.model().headerData(logicalIndex, self.orientation(), Qt.DisplayRole)
        if text is None:
            return size
        display_text = self._wrap_text(text)
        fm = QFontMetrics(self.font())
        lines = display_text.split("\n")
        max_line_w = max((fm.horizontalAdvance(line) for line in lines), default=size.width())
        height = max(size.height(), fm.height() * len(lines) + 8)
        width = max(size.width(), max_line_w + 8)
        return QSize(width, height)


class DuckDBViewer(QMainWindow):
    """DuckDB数据查看器主窗口"""

    def __init__(self, data_root: str = None, read_only: bool = True):
        super().__init__()

        # 设置窗口为非模态，确保主界面可以继续操作
        self.setWindowFlags(Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose)

        self.data_root = data_root
        # 默认只读打开，避免单纯浏览数据时长期占用 metadata.db 写锁。
        # 需要扫描/导入/删除时，再通过 _ensure_writable_manager() 临时切到写连接。
        self.default_read_only = bool(read_only)
        self._manager_read_only = bool(read_only)
        self.manager: Optional[DuckDBManager] = None
        self.current_stock = None
        self.current_period = '1d'
        self.import_dialog = None  # 保存导入对话框引用
        self.baostock_import_dialog = None
        self.tushare_import_dialog = None
        self.tencent_import_dialog = None
        self.ths_import_dialog = None
        self.http_import_dialog = None
        self._pending_close_after_tushare_stop = False
        self._pending_import_dialogs_to_close = set()
        self._viewer_closing = False
        self.font_scale = get_ui_font_scale()
        self._base_style_raw = None
        self.wal_repair_thread = None
        self.scan_thread = None
        self._pending_close_after_scan = False
        self._last_reindex_summary = None

        self.init_ui()

        # 设置Windows暗色标题栏
        self._set_dark_titlebar()

        # 如果指定了数据目录，自动加载
        if data_root and os.path.exists(data_root):
            self.load_data_root(data_root)

    def _set_dark_titlebar(self):
        """设置Windows暗色标题栏"""
        try:
            import platform
            if platform.system() == "Windows":
                from ctypes import windll, c_int, byref, sizeof
                from ctypes.wintypes import DWORD
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                DWMWA_CAPTION_COLOR = 35
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_USE_IMMERSIVE_DARK_MODE,
                    byref(c_int(2)),
                    sizeof(c_int)
                )
                caption_color = DWORD(0x333333)  # 与主界面背景色一致
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_CAPTION_COLOR,
                    byref(caption_color),
                    sizeof(caption_color)
                )
        except Exception as e:
            import logging
            logging.debug(f"设置暗色标题栏失败: {str(e)}")

    def _scale_stylesheet(self, style: str, scale: float) -> str:
        if not style:
            return style
        import re

        def repl(match):
            value = float(match.group(1))
            unit = match.group(2)
            scaled = max(6, int(round(value * float(scale))))
            return f"font-size: {scaled}{unit}"

        return re.sub(
            r"font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(px|pt)",
            repl,
            style,
            flags=re.IGNORECASE
        )

    def _set_scaled_stylesheet(self, widget: QWidget, style: str):
        widget.setProperty("ui_base_stylesheet", style)
        widget.setStyleSheet(self._scale_stylesheet(style, self.font_scale))

    def _set_scaled_font(self, widget: QWidget, base_pt: int):
        widget.setProperty("ui_base_font_pt", base_pt)
        font = widget.font()
        font.setPointSize(max(6, int(round(base_pt * self.font_scale))))
        widget.setFont(font)

    def apply_ui_scale(self, scale=None):
        if scale is None:
            scale = get_ui_font_scale()
        self.font_scale = scale
        if self._base_style_raw:
            self.setStyleSheet(self._scale_stylesheet(self._base_style_raw, self.font_scale))
        try:
            for child in self.findChildren(QWidget):
                base_ss = child.property("ui_base_stylesheet")
                if base_ss:
                    child.setStyleSheet(self._scale_stylesheet(base_ss, self.font_scale))
                base_pt = child.property("ui_base_font_pt")
                if base_pt:
                    font = child.font()
                    font.setPointSize(max(6, int(round(float(base_pt) * self.font_scale))))
                    child.setFont(font)
        except Exception:
            pass

    def _scale_stylesheet(self, style: str, scale: float) -> str:
        """按倍率缩放样式表中的 font-size"""
        if not style:
            return style
        import re

        def repl(match):
            value = float(match.group(1))
            unit = match.group(2)
            scaled = max(6, int(round(value * float(scale))))
            return f"font-size: {scaled}{unit}"

        return re.sub(
            r"font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(px|pt)",
            repl,
            style,
            flags=re.IGNORECASE
        )

    def _set_scaled_stylesheet(self, widget: QWidget, style: str):
        """设置并记录可缩放样式表"""
        widget.setProperty("ui_base_stylesheet", style)
        widget.setStyleSheet(self._scale_stylesheet(style, self.font_scale))

    def _set_scaled_font(self, widget: QWidget, base_pt: int):
        """设置并记录可缩放字体"""
        widget.setProperty("ui_base_font_pt", base_pt)
        font = widget.font()
        font.setPointSize(max(6, int(round(base_pt * self.font_scale))))
        widget.setFont(font)

    def apply_ui_scale(self, scale=None):
        """应用界面字号倍率到当前窗口"""
        if scale is None:
            scale = get_ui_font_scale()
        self.font_scale = scale
        if self._base_style_raw:
            self.setStyleSheet(self._scale_stylesheet(self._base_style_raw, self.font_scale))
        try:
            for child in self.findChildren(QWidget):
                base_ss = child.property("ui_base_stylesheet")
                if base_ss:
                    child.setStyleSheet(self._scale_stylesheet(base_ss, self.font_scale))
                base_pt = child.property("ui_base_font_pt")
                if base_pt:
                    font = child.font()
                    font.setPointSize(max(6, int(round(float(base_pt) * self.font_scale))))
                    child.setFont(font)
        except Exception:
            pass

    def _get_icon_path(self, icon_name):
        """获取图标文件的正确路径"""
        # 源码环境
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'icons', icon_name)

    def _scale_stylesheet(self, style: str, scale: float) -> str:
        """按倍率缩放样式表中的 font-size"""
        if not style:
            return style
        import re

        def repl(match):
            value = float(match.group(1))
            unit = match.group(2)
            scaled = max(6, int(round(value * float(scale))))
            return f"font-size: {scaled}{unit}"

        return re.sub(
            r"font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(px|pt)",
            repl,
            style,
            flags=re.IGNORECASE
        )

    def apply_ui_scale(self, scale=None):
        """应用界面字号倍率到当前窗口"""
        if scale is None:
            scale = get_ui_font_scale()
        self.font_scale = scale

        # 腾讯窗口从固定像素基准自行缩放，父窗口不再采集其已缩放字体。
        def independently_scaled(child):
            current = child
            while current is not None and current is not self:
                if current.property('ui_scale_managed'):
                    return True
                current = current.parentWidget()
            return False

        # 1) 缩放子控件样式中的 font-size
        try:
            for child in self.findChildren(QWidget):
                if independently_scaled(child):
                    continue
                ss = child.styleSheet() or ''
                if ss:
                    base_ss = child.property("ui_base_stylesheet")
                    if base_ss is None:
                        base_ss = ss
                        child.setProperty("ui_base_stylesheet", base_ss)
                    scaled_ss = self._scale_stylesheet(base_ss, self.font_scale)
                    if scaled_ss != ss:
                        child.setStyleSheet(scaled_ss)
        except Exception:
            pass

        # 2) 统一缩放显式设置过字体的控件
        try:
            for child in self.findChildren(QWidget):
                if independently_scaled(child):
                    continue
                font = child.font()
                base_pt = child.property("ui_base_font_pt")
                if base_pt is None and font.pointSize() > 0:
                    base_pt = font.pointSize()
                    child.setProperty("ui_base_font_pt", base_pt)
                if base_pt:
                    new_pt = max(6, int(round(float(base_pt) * self.font_scale)))
                    if font.pointSize() != new_pt:
                        font.setPointSize(new_pt)
                        child.setFont(font)
        except Exception:
            pass

        # 3) 应用缩放后的基础样式
        base_style = self._base_style_raw or ""
        self.setStyleSheet(self._scale_stylesheet(base_style, self.font_scale))

        # 刷新工具栏及其子按钮的几何尺寸，确保缩放后文字完整不截断
        try:
            tb = self.findChild(QToolBar, "DataManagerToolbar")
            if tb:
                for act in tb.actions():
                    w = tb.widgetForAction(act)
                    if w:
                        w.updateGeometry()
                        w.adjustSize()
                tb.updateGeometry()
                tb.adjustSize()
        except Exception:
            pass

        # 4) 同步定时补充窗口的字体缩放
        try:
            if hasattr(self, "scheduled_sync_window") and self.scheduled_sync_window:
                if hasattr(self.scheduled_sync_window, "apply_ui_scale"):
                    self.scheduled_sync_window.apply_ui_scale(self.font_scale)
        except Exception:
            pass

        # 5) 同步MiniQMT导入对话框的字体缩放
        try:
            if hasattr(self, "import_dialog") and self.import_dialog:
                if hasattr(self.import_dialog, "apply_ui_scale"):
                    self.import_dialog.apply_ui_scale(self.font_scale)
        except Exception:
            pass
        try:
            if hasattr(self, "baostock_import_dialog") and self.baostock_import_dialog:
                if hasattr(self.baostock_import_dialog, "apply_ui_scale"):
                    self.baostock_import_dialog.apply_ui_scale(self.font_scale)
        except Exception:
            pass
        if getattr(self, "tencent_import_dialog", None):
            self.tencent_import_dialog.apply_ui_scale(self.font_scale)
        if getattr(self, "ths_import_dialog", None):
            self.ths_import_dialog.apply_ui_scale(self.font_scale)
    
    def init_ui(self):
        """初始化UI"""
        self.setWindowTitle("看海数据管理模块")

        # 根据屏幕分辨率设置窗口大小（屏幕的2/3）
        desktop = QDesktopWidget()
        screen_rect = desktop.availableGeometry(desktop.primaryScreen())
        window_width = int(screen_rect.width() * 2 / 3)
        window_height = int(screen_rect.height() * 2 / 3)
        # 居中显示
        x = (screen_rect.width() - window_width) // 2
        y = (screen_rect.height() - window_height) // 2
        self.setGeometry(x, y, window_width, window_height)

        # 设置窗口图标（与主界面一致）
        icon_path = self._get_icon_path("stock_icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        else:
            # 尝试png格式
            icon_path_png = self._get_icon_path("stock_icon.png")
            if os.path.exists(icon_path_png):
                self.setWindowIcon(QIcon(icon_path_png))

        # 设置主题样式（与主界面一致的暗色主题）
        self._base_style_raw = """
            QMainWindow, QWidget {
                background-color: #333333;
                color: #e8e8e8;
                font-family: "Microsoft YaHei UI";
                font-size: 14px;
            }
            QLabel {
                color: #e8e8e8;
                font-size: 14px;
            }
            QLineEdit, QTextEdit {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                padding: 5px;
                font-size: 14px;
            }
            QPushButton {
                background-color: #0078d4;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-family: "Microsoft YaHei UI";
                font-weight: normal;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:pressed {
                background-color: #005a9e;
            }
            QPushButton:disabled {
                background-color: #555555;
                color: #888888;
            }
            QComboBox {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                padding: 5px;
                font-size: 14px;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #e8e8e8;
                margin-right: 5px;
            }
            QComboBox QAbstractItemView {
                background-color: #404040;
                color: #e8e8e8;
                selection-background-color: #0078d4;
                font-size: 14px;
            }
            QTreeWidget, QTableView {
                background-color: #333333;
                alternate-background-color: #383838;
                color: #e8e8e8;
                border: 1px solid #404040;
                gridline-color: #404040;
                font-size: 14px;
            }
            QTreeWidget::item:selected, QTableView::item:selected {
                background-color: #505050;
                color: #ffffff;
            }
            QTreeWidget::item:hover, QTableView::item:hover {
                background-color: #404040;
            }
            QHeaderView::section {
                background-color: #404040;
                color: #e8e8e8;
                border: none;
                border-right: 1px solid #4d4d4d;
                border-bottom: 1px solid #4d4d4d;
                padding: 8px;
                font-weight: bold;
                font-size: 14px;
            }
            QGroupBox {
                background-color: #333333;
                border: 1px solid #404040;
                border-radius: 6px;
                margin-top: 1em;
                padding-top: 1em;
                color: #e8e8e8;
                font-size: 14px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: #e8e8e8;
                font-weight: bold;
                background-color: #333333;
            }
            QProgressBar {
                background-color: #404040;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                text-align: center;
                color: #e8e8e8;
                font-size: 14px;
            }
            QProgressBar::chunk {
                background-color: #0078d4;
            }
            QStatusBar {
                background-color: #333333;
                color: #e8e8e8;
                border-top: 1px solid #404040;
                font-size: 14px;
            }
            QStatusBar QLabel {
                color: #e8e8e8;
            }
            QToolBar {
                background-color: #333333;
                border: none;
                border-bottom: 1px solid #404040;
                spacing: 3px;
                padding: 5px;
            }
            QToolBar::separator {
                background-color: #404040;
                width: 1px;
                margin: 8px 5px;
            }
            QToolButton {
                background-color: #505050;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                margin: 2px;
                font-family: "Microsoft YaHei UI";
                font-weight: normal;
                font-size: 14px;
            }
            QToolButton:hover {
                background-color: #606060;
            }
            QToolButton:pressed {
                background-color: #454545;
            }
            QToolButton:disabled {
                background-color: #404040;
                color: #808080;
            }
            QSpinBox, QDateEdit {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                padding: 5px;
                font-size: 14px;
            }
            QSpinBox::up-button, QDateEdit::up-button {
                background-color: #555555;
                border: none;
            }
            QSpinBox::down-button, QDateEdit::down-button {
                background-color: #555555;
                border: none;
            }
            QTabWidget::pane {
                border: 1px solid #404040;
                background-color: #333333;
            }
            QTabBar::tab {
                background-color: #404040;
                color: #e8e8e8;
                padding: 8px 16px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                font-size: 14px;
            }
            QTabBar::tab:selected {
                background-color: #333333;
                border-bottom: 2px solid #007acc;
            }
            QTabBar::tab:hover {
                background-color: #505050;
            }
            QScrollBar:vertical {
                background-color: #333333;
                width: 12px;
            }
            QScrollBar::handle:vertical {
                background-color: #555555;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #666666;
            }
            QScrollBar:horizontal {
                background-color: #333333;
                height: 12px;
            }
            QScrollBar::handle:horizontal {
                background-color: #555555;
                border-radius: 6px;
            }
            QScrollBar::handle:horizontal:hover {
                background-color: #666666;
            }
            QMenu {
                background-color: #333333;
                color: #e8e8e8;
                border: 1px solid #404040;
                font-size: 14px;
            }
            QMenu::item {
                padding: 6px 20px;
                background-color: transparent;
            }
            QMenu::item:selected {
                background-color: #505050;
            }
            QMenu::separator {
                height: 1px;
                background-color: #404040;
                margin: 2px 0px;
            }
            QToolTip {
                background-color: #555555;
                color: #e8e8e8;
                border: 1px solid #666666;
                padding: 4px;
                border-radius: 3px;
                font-size: 12px;
            }
        """
        _ui_font = get_preferred_ui_font_family() or "Microsoft YaHei UI"
        if _ui_font != "Microsoft YaHei UI":
            self._base_style_raw = self._base_style_raw.replace('"Microsoft YaHei UI"', f'"{_ui_font}"')
        self.setStyleSheet(self._base_style_raw)

        # 创建中央部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # 主布局
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(5, 5, 5, 5)

        # ========== 工具栏 ==========
        self._create_toolbar()

        # ========== 顶部：数据目录选择 ==========
        top_layout = QHBoxLayout()
        top_layout.addWidget(QLabel("数据目录:"))

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("选择或输入DuckDB数据存储目录...")
        self.path_edit.setReadOnly(True)  # 只读，通过浏览按钮选择
        top_layout.addWidget(self.path_edit, 1)

        self.browse_btn = QPushButton("浏览...")
        self.browse_btn.setMinimumWidth(80)
        self.browse_btn.clicked.connect(self.browse_data_root)
        top_layout.addWidget(self.browse_btn)

        main_layout.addLayout(top_layout)

        # ========== 主要内容区域 ==========
        splitter = QSplitter(Qt.Horizontal)

        # 左侧：股票列表树
        left_panel = self._create_left_panel()
        splitter.addWidget(left_panel)

        # 右侧：数据展示
        right_panel = self._create_right_panel()
        splitter.addWidget(right_panel)

        splitter.setSizes([300, 1100])
        main_layout.addWidget(splitter, 1)

        # ========== 状态栏 ==========
        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(200)
        self.progress_bar.setVisible(False)
        self.statusBar.addPermanentWidget(self.progress_bar)

        self.statusBar.showMessage("就绪")

        # 在界面构建完成后统一应用缩放，避免后续样式覆盖字号设置
        QTimer.singleShot(
            0,
            lambda owner=self: _dispatch_initial_viewer_scale(owner),
        )

    def _create_toolbar(self):
        """创建工具栏"""
        toolbar = QToolBar("工具栏")
        toolbar.setObjectName("DataManagerToolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        if MINIQMT_LAUNCH_ENABLED:
            qmt_action = QAction("MiniQMT导入", self)
            qmt_action.setToolTip("从 MiniQMT 下载并导入行情数据")
            qmt_action.triggered.connect(
                lambda _checked=False: self.show_import_dialog()
            )
            toolbar.addAction(qmt_action)

        baostock_action = QAction("BaoStock导入", self)
        baostock_action.setToolTip("通过 BaoStock 下载并导入行情数据")
        baostock_action.triggered.connect(self.show_baostock_import_dialog)
        toolbar.addAction(baostock_action)

        tencent_action = QAction("tx数据导入", self)
        tencent_action.setObjectName("tencent_import_action")
        tencent_action.setToolTip("tx日线、1分钟、5分钟行情及复权导入，无需Token")
        tencent_action.triggered.connect(self.show_tencent_import_dialog)
        toolbar.addAction(tencent_action)

        ths_action = QAction("同花顺数据导入", self)
        ths_action.setObjectName("ths_import_action")
        ths_action.setToolTip("同花顺（扶摇开放平台）A股日线行情及复权导入")
        ths_action.triggered.connect(self.show_ths_import_dialog)
        toolbar.addAction(ths_action)

        tushare_action = QAction("Tushare导入", self)
        tushare_action.setToolTip("使用Tushare接口下载股票行情（需在软件设置中配置Token）")
        tushare_action.triggered.connect(self.show_tushare_import_dialog)
        toolbar.addAction(tushare_action)

        http_action = QAction("HTTP远程桥接", self)
        http_action.setToolTip(
            "连接远程 GUIBridgeServer（端口 8001）导入历史行情，专供 macOS、Docker 或跨机器局域网使用"
        )
        http_action.triggered.connect(self.show_legacy_http_import_dialog)
        toolbar.addAction(http_action)

        if MINIQMT_LAUNCH_ENABLED:
            toolbar.addSeparator()

            # 定时补充 - 使用橙色样式突出显示（股票软件避免使用绿色）
            self.schedule_action = QAction("定时补充", self)
            self.schedule_action.setToolTip("打开定时数据补充模块")
            self.schedule_action.triggered.connect(self.open_scheduled_sync)
            toolbar.addAction(self.schedule_action)

            # 获取定时补充按钮并设置突出样式（橙色）
            schedule_btn = toolbar.widgetForAction(self.schedule_action)
            if schedule_btn:
                _ui_font = get_preferred_ui_font_family() or "Microsoft YaHei UI"
                schedule_btn.setStyleSheet(f"""
                    QToolButton {{
                        background-color: #e67e22;
                        color: #ffffff;
                        border: none;
                        border-radius: 4px;
                        padding: 6px 12px;
                        margin: 2px;
                        font-family: "{_ui_font}";
                        font-weight: normal;
                        font-size: 14px;
                    }}
                    QToolButton:hover {{
                        background-color: #f39c12;
                    }}
                    QToolButton:pressed {{
                        background-color: #d35400;
                    }}
                """)
        else:
            self.schedule_action = None

        toolbar.addSeparator()

        # 占用诊断是数据库报错后的首要排查入口，放在维护区首位并保持醒目。
        self.occupancy_action = QAction("占用诊断", self)
        self.occupancy_action.setToolTip(
            "查看当前数据目录由哪些模块和PID占用，并按精确进程释放连接"
        )
        self.occupancy_action.triggered.connect(self.show_duckdb_occupancy_dialog)
        toolbar.addAction(self.occupancy_action)

        occupancy_button = toolbar.widgetForAction(self.occupancy_action)
        if occupancy_button:
            occupancy_button.setObjectName("DatabaseOccupancyButton")
            _ui_font = get_preferred_ui_font_family() or "Microsoft YaHei UI"
            occupancy_button.setStyleSheet(f"""
                QToolButton {{
                    background-color: #1677c8;
                    color: #ffffff;
                    border: none;
                    border-radius: 4px;
                    padding: 6px 12px;
                    margin: 2px;
                    font-family: "{_ui_font}";
                    font-weight: normal;
                    font-size: 14px;
                }}
                QToolButton:hover {{
                    background-color: #2389da;
                }}
                QToolButton:pressed {{
                    background-color: #0f65ad;
                }}
            """)

        # 统计
        stats_action = QAction("统计信息", self)
        stats_action.setToolTip("查看元数据索引、数据库文件和各周期的数据统计")
        stats_action.triggered.connect(self.show_statistics)
        toolbar.addAction(stats_action)

        self.reindex_action = QAction("核验索引", self)
        self.reindex_action.setToolTip(
            "只读核验未纳入索引的数据库；仅对确有行情的库备份后事务写入元数据"
        )
        self.reindex_action.triggered.connect(self.scan_data_directory)
        toolbar.addAction(self.reindex_action)

        wal_repair_action = QAction("WAL修复", self)
        wal_repair_action.setToolTip("检测并修复本地数据库WAL错误")
        wal_repair_action.triggered.connect(self.run_wal_repair)
        toolbar.addAction(wal_repair_action)

    def show_duckdb_occupancy_dialog(self):
        """打开进程级 DuckDB 占用诊断，不主动连接任何数据库文件。"""
        if self._reject_while_reindex_active("打开占用诊断"):
            return
        if not self.data_root or not os.path.isdir(self.data_root):
            QMessageBox.warning(self, "提示", "请先加载 DuckDB 数据目录")
            return
        dialog = DatabaseOccupancyDialog(self)
        dialog.exec_()

    def _create_left_panel(self) -> QWidget:
        """创建左侧面板：股票列表"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # 搜索框
        search_layout = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索股票代码...")
        self.search_edit.textChanged.connect(self.filter_stock_list)
        search_layout.addWidget(self.search_edit)
        layout.addLayout(search_layout)

        # 市场筛选
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("市场:"))
        self.market_combo = QComboBox()
        self.market_combo.addItems(["全部", "SH", "SZ", "BJ"])
        self.market_combo.currentTextChanged.connect(self.refresh_stock_list)
        filter_layout.addWidget(self.market_combo)

        filter_layout.addWidget(QLabel("周期:"))
        self.filter_period_combo = QComboBox()
        self.filter_period_combo.addItems(["全部", "1d", "1m", "5m", "tick"])
        self.filter_period_combo.currentTextChanged.connect(self.refresh_stock_list)
        filter_layout.addWidget(self.filter_period_combo)

        # 手动刷新。放在筛选栏右侧，保持轻量，不打断左侧面板布局。
        self.refresh_tree_btn = QPushButton("⟳")
        self.refresh_tree_btn.setToolTip("刷新数据列表")
        self.refresh_tree_btn.setFixedSize(32, 32)
        self.refresh_tree_btn.setStyleSheet("""
            QPushButton {
                background-color: #404040;
                color: #E0E0E0;
                border: 1px solid #505050;
                border-radius: 3px;
                font-size: 16px;
                padding: 0;
            }
            QPushButton:hover {
                background-color: #4A4A4A;
                border-color: #606060;
            }
            QPushButton:pressed {
                background-color: #353535;
            }
        """)
        self.refresh_tree_btn.clicked.connect(self.on_refresh_clicked)
        filter_layout.addWidget(self.refresh_tree_btn)
        filter_layout.addStretch()
        layout.addLayout(filter_layout)

        # 股票树
        self.stock_tree = QTreeWidget()
        self.stock_tree.setHeaderLabels(["股票/市场", "记录数"])
        self.stock_tree.setColumnWidth(0, 200)  # 增加宽度以显示股票名称
        self.stock_tree.itemClicked.connect(self.on_stock_selected)
        self.stock_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.stock_tree.customContextMenuRequested.connect(self.show_tree_context_menu)
        layout.addWidget(self.stock_tree)

        # 统计标签
        self.stats_label = QLabel("股票数: 0")
        layout.addWidget(self.stats_label)

        return panel

    def _create_right_panel(self) -> QWidget:
        """创建右侧面板：数据展示"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # 查询条件
        query_group = QGroupBox("查询条件")
        query_layout = QGridLayout(query_group)

        # 股票代码
        query_layout.addWidget(QLabel("股票代码:"), 0, 0)
        self.stock_code_label = QLabel("-")
        self.stock_code_label.setFont(QFont("Arial", 12, QFont.Bold))
        query_layout.addWidget(self.stock_code_label, 0, 1)

        # 周期选择
        query_layout.addWidget(QLabel("周期:"), 0, 2)
        self.period_combo = QComboBox()
        self.period_combo.addItems(["1d", "1m", "5m", "tick"])
        self.period_combo.currentTextChanged.connect(self.on_period_changed)
        query_layout.addWidget(self.period_combo, 0, 3)

        # 开始日期
        query_layout.addWidget(QLabel("开始日期:"), 1, 0)
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDate(QDate.currentDate().addYears(-1))
        query_layout.addWidget(self.start_date, 1, 1)

        # 结束日期
        query_layout.addWidget(QLabel("结束日期:"), 1, 2)
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDate(QDate.currentDate())
        query_layout.addWidget(self.end_date, 1, 3)

        # 查询按钮
        self.query_btn = QPushButton("查询")
        self.query_btn.clicked.connect(self.query_data)
        query_layout.addWidget(self.query_btn, 1, 4)

        # 数据完整性检查按钮
        self.integrity_btn = QPushButton("数据完整性检查")
        self.integrity_btn.clicked.connect(self.check_data_integrity)
        query_layout.addWidget(self.integrity_btn, 1, 5)

        # 导出CSV按钮
        self.export_btn = QPushButton("导出CSV")
        self.export_btn.clicked.connect(self.export_to_csv)
        query_layout.addWidget(self.export_btn, 1, 6)

        # 限制条数
        query_layout.addWidget(QLabel("限制:"), 0, 4)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(100, 10000000)
        self.limit_spin.setValue(10000)
        self.limit_spin.setSingleStep(1000)
        query_layout.addWidget(self.limit_spin, 0, 5, 1, 2)  # 跨2列

        layout.addWidget(query_group)

        # Tab页
        self.tab_widget = QTabWidget()

        # 数据表格Tab
        table_tab = QWidget()
        table_layout = QVBoxLayout(table_tab)

        self.data_table = QTableView()
        self.data_model = PandasModel()
        self.data_table.setModel(self.data_model)
        self.data_table.setAlternatingRowColors(True)
        self.data_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.data_table.setHorizontalHeader(WrapHeaderView(Qt.Horizontal, self.data_table))
        self.data_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table_layout.addWidget(self.data_table)

        # 数据信息
        self.data_info_label = QLabel("加载数据后显示信息")
        table_layout.addWidget(self.data_info_label)

        self.tab_widget.addTab(table_tab, "数据表格")

        # SQL查询Tab
        sql_tab = self._create_sql_tab()
        self.tab_widget.addTab(sql_tab, "SQL查询")

        layout.addWidget(self.tab_widget, 1)

        return panel

    def _create_sql_tab(self) -> QWidget:
        """创建SQL查询Tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # SQL输入框
        layout.addWidget(QLabel("SQL查询 (针对当前选中股票的数据库):"))
        self.sql_edit = QTextEdit()
        self.sql_edit.setMaximumHeight(100)
        self.sql_edit.setPlaceholderText(
            "输入SQL语句，例如:\n"
            "SELECT * FROM kline_1d WHERE close > 10 ORDER BY time DESC LIMIT 100\n"
            "SELECT AVG(close) as avg_price, MAX(high) as max_high FROM kline_1d"
        )
        layout.addWidget(self.sql_edit)

        # 执行按钮
        btn_layout = QHBoxLayout()
        self.exec_sql_btn = QPushButton("执行SQL")
        self.exec_sql_btn.clicked.connect(self.execute_sql)
        btn_layout.addWidget(self.exec_sql_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # SQL结果表格
        self.sql_table = QTableView()
        self.sql_model = PandasModel()
        self.sql_table.setModel(self.sql_model)
        self.sql_table.setAlternatingRowColors(True)
        self.sql_table.setHorizontalHeader(WrapHeaderView(Qt.Horizontal, self.sql_table))
        self.sql_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        layout.addWidget(self.sql_table, 1)

        # SQL结果信息
        self.sql_info_label = QLabel("")
        layout.addWidget(self.sql_info_label)

        return tab

    # ============ 事件处理 ============

    def browse_data_root(self):
        """浏览选择数据目录"""
        if self._reject_while_reindex_active("切换数据目录"):
            return
        path = QFileDialog.getExistingDirectory(
            self, "选择DuckDB数据目录",
            self.path_edit.text() or os.path.expanduser("~")
        )
        if path:
            self.path_edit.setText(path)
            # 自动加载数据目录
            self.load_data_root(path)

    def on_refresh_clicked(self):
        """刷新按钮点击"""
        if self._reject_while_reindex_active("刷新数据目录"):
            return
        if not self.manager:
            scheduled_process = getattr(self, "scheduled_sync_process", None)
            if scheduled_process is not None and scheduled_process.poll() is None:
                QMessageBox.information(
                    self,
                    "连接已释放",
                    "定时补充模块正在独立运行。为避免下一次写入任务发生 DuckDB 文件冲突，"
                    "数据管理模块暂不重新连接。\n\n退出定时补充模块后，再点击刷新即可恢复浏览。"
                )
                return
            if self.data_root and os.path.exists(self.data_root):
                try:
                    self._open_manager(self.data_root, read_only=True)
                except Exception as e:
                    QMessageBox.warning(self, "恢复失败", f"无法重新连接数据目录:\n{e}")
                    return
            else:
                QMessageBox.warning(self, "提示", "请先加载数据目录")
                return

        try:
            # 强制重新初始化元数据库连接
            if self.manager._metadata_conn:
                self.manager._metadata_conn.close()
                self.manager._metadata_conn = None

            # 重新连接
            self.manager._init_metadata_db()

            # 刷新股票列表
            self.refresh_stock_list()

            self.statusBar.showMessage("股票列表已刷新")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"刷新失败:\n{e}")
            import traceback
            traceback.print_exc()

    def _close_current_manager(self, skip_checkpoint: bool = False):
        if not self.manager:
            return
        try:
            if skip_checkpoint and hasattr(self.manager, "close_all_no_checkpoint"):
                self.manager.close_all_no_checkpoint()
            else:
                self.manager.close_all()
        except Exception as e:
            print(f"关闭管理器时出错: {e}")
        finally:
            self.manager = None

    def _open_manager(self, path: str, read_only: bool):
        # 同进程读写模式切换前先释放相反模式的单例，避免 Windows 文件锁冲突。
        if read_only:
            DuckDBManager.close_writable_instances(path)
        else:
            DuckDBManager.close_read_only_instances(path)
        self.manager = DuckDBManager(data_root=path, read_only=read_only)
        self._manager_read_only = bool(read_only)
        return self.manager

    def _ensure_metadata_initialized(self, path: str) -> bool:
        """确保空数据目录也能进入数据管理模块。

        只读浏览模式无法创建 metadata.db。用户刚在设置里新建 DuckDB 路径时，
        目录通常是空的；此时先短暂打开可写 manager 初始化 metadata 和市场子目录，
        再关闭连接，让后续只读浏览正常打开。
        """
        metadata_path = os.path.join(path, 'metadata.db')
        if os.path.exists(metadata_path):
            return False

        DuckDBManager.close_read_only_instances(path)
        init_manager = DuckDBManager(data_root=path, read_only=False)
        try:
            init_manager.close_all()
        finally:
            DuckDBManager.close_writable_instances(path)
        return True

    def _ensure_writable_manager(self):
        """确保当前窗口持有可写连接；失败时尽力恢复只读浏览状态。

        DuckDB 在 Windows 下不允许一个进程以只读配置打开数据库、另一个进程
        同时以读写配置打开。切换写连接前必须先关闭当前只读 manager。旧实现
        一旦写连接创建失败便把 ``self.manager`` 永久留成 ``None``，导致用户
        再点其他导入入口时误报“请先加载数据目录”。这里把模式切换做成可恢复
        操作：保留原始异常，随后尽力恢复只读连接；即使外部写进程也阻止只读
        恢复，后续入口仍可依据 ``data_root`` 重新尝试，不再把连接状态误当成
        数据目录未配置。
        """
        if not self.data_root or not os.path.exists(self.data_root):
            raise ValueError("数据目录无效，请先选择正确的DuckDB数据目录")
        if self.manager and not getattr(self.manager, "read_only", False):
            return self.manager
        self._close_current_manager(skip_checkpoint=True)
        try:
            return self._open_manager(self.data_root, read_only=False)
        except Exception:
            # DuckDBManager.__new__ 可能已登记了一个初始化失败的写实例；创建
            # 只读实例时会先清理它。恢复失败不能覆盖最初、更有诊断价值的锁异常。
            try:
                self._open_manager(self.data_root, read_only=True)
            except Exception as restore_error:
                self.manager = None
                logging.warning(f"写连接失败后恢复只读连接失败: {restore_error}")
            raise

    def _is_reindex_scan_running(self) -> bool:
        thread = getattr(self, "scan_thread", None)
        try:
            return bool(thread is not None and thread.isRunning())
        except RuntimeError:
            self.scan_thread = None
            return False

    def _reject_while_reindex_active(self, operation_name: str) -> bool:
        if not DuckDBViewer._is_reindex_scan_running(self):
            return False
        message = f"元数据索引核验正在进行，暂不能{operation_name}。请等待核验完成或关闭窗口取消。"
        try:
            QMessageBox.information(self, "索引核验进行中", message)
            self.statusBar.showMessage(message)
        except Exception:
            pass
        return True

    def _request_writable_manager(self, operation_name: str = "执行写操作"):
        """为 GUI 写入口统一申请写连接，并处理跨进程 DuckDB 占用。

        返回可写 manager 表示成功，返回 ``None`` 表示用户取消或恢复失败。
        只有用户在弹窗中明确确认后才会结束外部占用进程。
        """
        if DuckDBViewer._reject_while_reindex_active(self, operation_name):
            return None
        try:
            return self._ensure_writable_manager()
        except Exception as error:
            lock_info = self._extract_duckdb_lock_info(error)
            pid = lock_info.get("pid")
            process_path = lock_info.get("process_path")
            file_path = lock_info.get("file_path")

            detail_lines = [f"{operation_name}需要打开 DuckDB 写连接，但当前无法取得写权限。"]
            if file_path:
                detail_lines.append(f"被占用文件: {file_path}")
            if process_path:
                detail_lines.append(f"占用程序: {process_path}")
            if pid:
                detail_lines.append(f"占用 PID: {pid}")

            if pid and pid != os.getpid():
                reply = QMessageBox.question(
                    self,
                    "数据库正在被其他程序使用",
                    "\n".join(detail_lines)
                    + "\n\n可能是研究看板、定时补充或其他看海量化进程仍在读取数据库。"
                    + "\n是否结束该占用进程并重试？\n\n未确认前不会结束任何程序。",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    self.statusBar.showMessage(f"{operation_name}已取消：DuckDB 正被其他程序占用")
                    return None

                ok, kill_message = self._terminate_process_by_pid(pid)
                if not ok:
                    QMessageBox.warning(
                        self,
                        "无法结束占用进程",
                        f"无法结束 PID {pid}:\n{kill_message}",
                    )
                    self.statusBar.showMessage(f"{operation_name}失败：无法释放 DuckDB 占用")
                    return None

                try:
                    time.sleep(0.3)
                    manager = self._ensure_writable_manager()
                    self.statusBar.showMessage(f"已释放占用，正在{operation_name}")
                    return manager
                except Exception as retry_error:
                    QMessageBox.warning(
                        self,
                        "重试失败",
                        f"占用进程已结束，但仍无法打开写连接:\n{retry_error}",
                    )
                    self.statusBar.showMessage(f"{operation_name}失败：DuckDB 写连接重试失败")
                    return None

            QMessageBox.warning(
                self,
                "无法打开写连接",
                "\n".join(detail_lines) + f"\n\n错误详情:\n{error}",
            )
            self.statusBar.showMessage(f"{operation_name}失败：无法打开 DuckDB 写连接")
            return None

    def _on_import_dialog_destroyed(self, attr_name: str):
        """导入窗口关闭后释放写连接，并恢复数据管理的只读浏览模式。"""
        # QApplication.quit() 或父窗口级联销毁时，导入子窗口的 destroyed
        # 信号可能晚于 QTreeWidget/QStatusBar 的 C++ 实例销毁。此时 Python
        # 包装对象仍可能存在，但任何控件访问都会抛 RuntimeError，甚至继续
        # 进入 SIP 造成原生崩溃。销毁阶段只清理引用，不再恢复连接或刷新 UI。
        if _qt_object_is_deleted(self):
            return
        setattr(self, attr_name, None)
        if getattr(self, "_viewer_closing", False):
            return

        # 如果还有其他导入窗口存活，其后台任务可能仍需要写 manager，不能
        # 被当前窗口的 destroyed 信号误切成只读。
        dialog_attrs = (
            "import_dialog",
            "baostock_import_dialog",
            "http_import_dialog",
            "tushare_import_dialog",
            "tencent_import_dialog",
            "ths_import_dialog",
        )
        if any(getattr(self, name, None) is not None for name in dialog_attrs):
            return

        try:
            self._ensure_read_only_manager()
            self.refresh_stock_list()
            _safe_qt_method_call(
                getattr(self, "statusBar", None),
                "showMessage",
                "导入窗口已关闭，数据管理已恢复只读浏览",
            )
        except Exception as error:
            if _qt_object_is_deleted(self):
                return
            # 外部写任务可能恰好在此时接管数据库。数据目录仍然有效，后续点击
            # 刷新或任一导入入口都会重新尝试，不能再误报目录未加载。
            self.manager = None
            logging.warning(f"导入窗口关闭后恢复只读连接失败: {error}")
            _safe_qt_method_call(
                getattr(self, "statusBar", None),
                "showMessage",
                "导入窗口已关闭；数据库暂被其他任务占用，可稍后刷新",
            )

    def _ensure_read_only_manager(self):
        """写操作结束后可切回只读连接，减少对 Codex/CLI 回测的影响。"""
        if not self.data_root or not os.path.exists(self.data_root):
            raise ValueError("数据目录无效，请先选择正确的DuckDB数据目录")
        if self.manager and getattr(self.manager, "read_only", False):
            return self.manager
        self._close_current_manager(skip_checkpoint=True)
        return self._open_manager(self.data_root, read_only=True)

    def load_data_root(self, path: str, read_only: Optional[bool] = None):
        """加载数据目录"""
        if self._reject_while_reindex_active("切换数据目录"):
            return False
        if not os.path.exists(path):
            QMessageBox.warning(self, "错误", f"目录不存在: {path}")
            return

        self.data_root = path
        self.path_edit.setText(path)
        read_only = self.default_read_only if read_only is None else bool(read_only)

        # 先关闭旧的管理器
        self._close_current_manager(skip_checkpoint=True)

        # 等待一小段时间确保文件句柄被释放
        import time
        time.sleep(0.1)

        # 创建新的管理器
        try:
            initialized_empty_dir = False
            if read_only:
                initialized_empty_dir = self._ensure_metadata_initialized(path)

            self._open_manager(path, read_only=read_only)

            # 刷新股票列表
            self.refresh_stock_list()

            mode_text = "只读浏览" if read_only else "读写管理"
            if initialized_empty_dir:
                self.statusBar.showMessage(f"已初始化空DuckDB目录并加载({mode_text}): {path}")
            else:
                self.statusBar.showMessage(f"已加载数据目录({mode_text}): {path}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"加载数据目录失败:\n{e}")
            self.manager = None

    def refresh_stock_list(self):
        """刷新股票列表（优化版本：延迟加载记录数）"""
        if DuckDBViewer._is_reindex_scan_running(self):
            self.statusBar.showMessage("元数据索引核验进行中，完成后会自动刷新股票列表")
            return
        if not self.manager:
            return

        self.stock_tree.clear()

        # 获取筛选条件
        market_filter = self.market_combo.currentText()
        period_filter = self.filter_period_combo.currentText()

        market = None if market_filter == "全部" else market_filter
        period = None if period_filter == "全部" else period_filter

        # 获取股票列表（从元数据库获取）
        stocks = self.manager.get_available_stocks(period=period, market=market)

        # 尝试导入 khQTTools 获取股票名称
        get_stock_name_func = None
        try:
            import sys
            import os
            parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if parent_dir not in sys.path:
                sys.path.insert(0, parent_dir)
            import khQTTools
            get_stock_name_func = khQTTools.get_stock_name
        except Exception:
            pass

        # 按市场分组
        market_groups = {'SH': [], 'SZ': [], 'BJ': []}
        for stock in stocks:
            if '.' in stock:
                _, m = stock.split('.')
                if m in market_groups:
                    market_groups[m].append(stock)

        # 填充树
        search_text = self.search_edit.text().strip().upper()
        total_count = 0

        for market_name, stock_list in market_groups.items():
            if market != None and market != market_name:
                continue

            if not stock_list:
                continue

            # 过滤搜索（搜索股票代码和名称）
            if search_text:
                filtered_list = []
                for s in stock_list:
                    if search_text in s.upper():
                        filtered_list.append(s)
                    elif get_stock_name_func:
                        name = get_stock_name_func(s)
                        if name and search_text in name.upper():
                            filtered_list.append(s)
                stock_list = filtered_list

            if not stock_list:
                continue

            market_item = QTreeWidgetItem([f"{market_name} ({len(stock_list)}只)", ""])
            market_item.setExpanded(True)

            for stock_code in sorted(stock_list):
                # 获取股票名称
                stock_name = get_stock_name_func(stock_code) if get_stock_name_func else ''
                # 显示格式: 股票代码 名称
                display_text = f"{stock_code} {stock_name}" if stock_name else stock_code
                stock_item = QTreeWidgetItem([display_text, "点击查看"])
                stock_item.setData(0, Qt.UserRole, stock_code)
                market_item.addChild(stock_item)
                total_count += 1

            self.stock_tree.addTopLevelItem(market_item)

        scope = "当前显示" if (market is not None or period is not None or search_text) else "可浏览"
        self.stats_label.setText(
            f"{scope}股票数: {total_count}（来自元数据索引；物理文件口径见“统计信息”）"
        )

    def filter_stock_list(self):
        """筛选股票列表"""
        self.refresh_stock_list()

    @staticmethod
    def _preferred_period_from_counts(counts: dict) -> str:
        """按显示优先级选择该股票当前最适合展示的周期。"""
        for period in ("1d", "1m", "5m", "tick"):
            try:
                if int(counts.get(period, 0) or 0) > 0:
                    return period
            except Exception:
                continue
        return "1d"

    def on_stock_selected(self, item: QTreeWidgetItem, column: int):
        """股票选中事件"""
        if self._reject_while_reindex_active("查询股票数据"):
            return
        stock_code = item.data(0, Qt.UserRole)
        if stock_code:
            self.current_stock = stock_code
            # 尝试显示股票名称（形如: 110074.SH (某ETF)），若失败则回退显示代码
            try:
                import khQTTools
                display_text = khQTTools.get_stock_display(stock_code)
            except Exception:
                display_text = stock_code
            self.stock_code_label.setText(display_text)

            # 延迟加载：选中时更新该股票的记录数
            try:
                stock_db = self.manager.get_stock_db(stock_code)
                counts = stock_db.get_all_counts()
                count_str = (
                    f"1d:{counts.get('1d', 0)} "
                    f"1m:{counts.get('1m', 0)} "
                    f"5m:{counts.get('5m', 0)} "
                    f"tick:{counts.get('tick', 0)}"
                )
                item.setText(1, count_str)
                preferred_period = self._preferred_period_from_counts(counts)
                if self.period_combo.currentText() != preferred_period:
                    self.period_combo.blockSignals(True)
                    self.period_combo.setCurrentText(preferred_period)
                    self.period_combo.blockSignals(False)
                self.current_period = preferred_period
            except Exception:
                item.setText(1, "加载失败")

            self.query_data()

    def on_period_changed(self, period: str):
        """周期改变"""
        self.current_period = period
        if self.current_stock:
            self.query_data()

    def query_data(self):
        """查询数据"""
        if self._reject_while_reindex_active("查询行情数据"):
            return
        if not self.manager or not self.current_stock:
            return

        start = self.start_date.date().toString("yyyyMMdd")
        end = self.end_date.date().toString("yyyyMMdd")
        period = self.period_combo.currentText()

        # 在状态栏也显示带名称的显示文本（若可用）
        try:
            import khQTTools
            display_text = khQTTools.get_stock_display(self.current_stock)
        except Exception:
            display_text = self.current_stock
        self.statusBar.showMessage(f"正在查询 {display_text} {period} 数据...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        # 使用线程加载数据
        self.load_thread = DataLoadThread(
            self.manager, self.current_stock, period, start, end
        )
        self.load_thread.finished.connect(self.on_data_loaded)
        self.load_thread.error.connect(self.on_load_error)
        self.load_thread.progress.connect(self.progress_bar.setValue)
        self.load_thread.start()

    def on_data_loaded(self, df: pd.DataFrame):
        """数据加载完成"""
        self.progress_bar.setVisible(False)

        # 限制显示条数
        limit = self.limit_spin.value()
        display_df = df.tail(limit) if len(df) > limit else df

        self.data_model.update_data(display_df)

        # 更新信息
        info = f"共 {len(df)} 条记录"
        if len(df) > limit:
            info += f" (显示最近 {limit} 条)"

        if len(df) > 0:
            info += f" | 时间范围: {df['time'].min()} ~ {df['time'].max()}"

        self.data_info_label.setText(info)
        self.statusBar.showMessage(f"查询完成: {len(df)} 条记录")

    def on_load_error(self, error: str):
        """数据加载错误"""
        self.progress_bar.setVisible(False)
        QMessageBox.warning(self, "查询错误", error)
        self.statusBar.showMessage(f"查询失败: {error}")

    def check_data_integrity(self):
        """检查数据完整性"""
        if self._reject_while_reindex_active("检查数据完整性"):
            return
        if not self.manager or not self.current_stock:
            QMessageBox.warning(self, "提示", "请先选择股票并查询数据")
            return
        
        start = self.start_date.date().toString("yyyyMMdd")
        end = self.end_date.date().toString("yyyyMMdd")
        period = self.period_combo.currentText()
        
        # 转换日期格式为YYYY-MM-DD
        start_date_str = self.start_date.date().toString("yyyy-MM-dd")
        end_date_str = self.end_date.date().toString("yyyy-MM-dd")
        
        # 打开数据完整性检查对话框
        dialog = DataIntegrityDialog(
            self.manager, self.current_stock, period,
            start_date_str, end_date_str, self
        )
        dialog.exec_()

    def execute_sql(self):
        """执行SQL查询"""
        if self._reject_while_reindex_active("执行 SQL 查询"):
            return
        if not self.manager or not self.current_stock:
            QMessageBox.warning(self, "提示", "请先选择一只股票")
            return

        sql = self.sql_edit.toPlainText().strip()
        if not sql:
            return

        try:
            stock_db = self.manager.get_stock_db(self.current_stock)
            df = stock_db.execute_sql(sql)

            self.sql_model.update_data(df)
            self.sql_info_label.setText(f"查询结果: {len(df)} 行, {len(df.columns)} 列")

        except Exception as e:
            QMessageBox.warning(self, "SQL执行错误", str(e))
            self.sql_info_label.setText(f"错误: {e}")

    def show_tree_context_menu(self, pos):
        """显示右键菜单"""
        item = self.stock_tree.itemAt(pos)
        if not item:
            return

        stock_code = item.data(0, Qt.UserRole)
        if not stock_code:
            return

        menu = QMenu(self)

        # 查看详情
        view_action = menu.addAction("查看数据")
        view_action.triggered.connect(lambda: self.on_stock_selected(item, 0))

        # 导出
        export_action = menu.addAction("导出CSV")
        export_action.triggered.connect(lambda: self.export_stock_data(stock_code))

        menu.addSeparator()

        # 删除（谨慎操作）
        delete_action = menu.addAction("删除数据")
        delete_action.triggered.connect(lambda: self.delete_stock_data(stock_code))

        menu.exec_(self.stock_tree.mapToGlobal(pos))

    def export_stock_data(self, stock_code: str):
        """导出单只股票数据"""
        if not self.manager:
            return

        period = self.period_combo.currentText()

        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出CSV",
            f"{stock_code}_{period}.csv",
            "CSV文件 (*.csv)"
        )

        if file_path:
            df = self.manager.get_kline_data(stock_code, period)
            df.to_csv(file_path, index=False, encoding='utf-8-sig')
            QMessageBox.information(self, "导出成功", f"已导出 {len(df)} 条记录到:\n{file_path}")

    def export_to_csv(self):
        """导出当前显示的数据"""
        if self.data_model._df is None or len(self.data_model._df) == 0:
            QMessageBox.warning(self, "提示", "没有数据可导出")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出CSV",
            f"{self.current_stock}_{self.current_period}.csv",
            "CSV文件 (*.csv)"
        )

        if file_path:
            self.data_model._df.to_csv(file_path, index=False, encoding='utf-8-sig')
            QMessageBox.information(self, "导出成功",
                                   f"已导出 {len(self.data_model._df)} 条记录到:\n{file_path}")

    def delete_stock_data(self, stock_code: str):
        """删除股票数据"""
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除 {stock_code} 的所有数据吗?\n此操作不可恢复!",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            try:
                manager = self._request_writable_manager(f"删除 {stock_code} 数据")
                if manager is None:
                    return
                # 获取数据库路径并删除文件
                config = manager.config
                db_path = config.get_db_path(stock_code)

                if os.path.exists(db_path):
                    # 先关闭连接
                    if stock_code in manager._stock_dbs:
                        manager._stock_dbs[stock_code].close()
                        del manager._stock_dbs[stock_code]

                    # 删除数据库文件
                    os.remove(db_path)

                # 删除元数据库中的记录
                manager.delete_stock_metadata(stock_code)

                # 从树形列表中移除该项
                self._remove_stock_from_tree(stock_code)

                QMessageBox.information(self, "删除成功", f"已删除 {stock_code} 的数据")

            except Exception as e:
                QMessageBox.warning(self, "删除失败", str(e))
                import traceback
                traceback.print_exc()
            finally:
                # 删除是短写操作，结束后立即恢复只读，避免数据管理窗口继续
                # 阻塞研究看板、CLI 或其他只读进程。
                try:
                    self._ensure_read_only_manager()
                except Exception as restore_error:
                    logging.warning(f"删除操作后恢复只读连接失败: {restore_error}")

    def _remove_stock_from_tree(self, stock_code: str):
        """从树形列表中移除股票项

        Args:
            stock_code: 股票代码
        """
        # 遍历所有市场节点
        for i in range(self.stock_tree.topLevelItemCount()):
            market_item = self.stock_tree.topLevelItem(i)

            # 遍历市场节点下的所有股票
            for j in range(market_item.childCount()):
                stock_item = market_item.child(j)
                item_code = stock_item.data(0, Qt.UserRole)

                if item_code == stock_code:
                    # 找到了，删除该项
                    market_item.removeChild(stock_item)

                    # 如果市场节点下没有股票了，更新显示
                    if market_item.childCount() == 0:
                        market_item.setText(0, f"{market_item.text(0).split('(')[0]} (0)")
                    else:
                        # 更新市场节点的计数
                        market_name = market_item.text(0).split('(')[0].strip()
                        market_item.setText(0, f"{market_name} ({market_item.childCount()})")

                    return

    def scan_data_directory(self):
        """后台只读核验候选库；确有行情时才申请写连接同步。"""
        if self.scan_thread is not None and self.scan_thread.isRunning():
            QMessageBox.information(self, "索引核验", "索引核验正在进行，请稍候。")
            return
        if not self.manager:
            QMessageBox.warning(self, "提示", "请先加载数据目录")
            return
        blockers = self._local_database_release_blockers()
        if blockers:
            QMessageBox.warning(
                self,
                "暂不能核验索引",
                "以下数据任务仍在运行：" + "、".join(blockers) + "。请先正常结束任务。",
            )
            return
        reply = QMessageBox.question(
            self,
            "核验并同步元数据索引",
            "系统将先在后台只读核验未纳入索引的数据库文件。\n\n"
            "空行情库和非证券文件会跳过；只有发现真实行情时，才会先备份 "
            "metadata.db，再以单个事务补充索引。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            manager = self._ensure_read_only_manager()
        except Exception as exc:
            QMessageBox.warning(self, "无法开始核验", str(exc))
            return

        self.statusBar.showMessage("正在只读核验未纳入索引的数据库...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.reindex_action.setEnabled(False)
        self.scan_thread = ScanThread(manager)
        self.scan_thread.progress.connect(self.progress_bar.setValue)
        self.scan_thread.finished.connect(self._on_scan_thread_done)
        self.scan_thread.start()

    @staticmethod
    def _format_reindex_summary(summary: Dict[str, Any]) -> str:
        return (
            f"核验文件: {int(summary.get('candidate_files', 0) or 0)}\n"
            f"整库未入索引候选: {int(summary.get('unindexed_candidates', 0) or 0)}\n"
            f"已有证券周期标志候选: {int(summary.get('period_flag_candidates', 0) or 0)}\n"
            f"新增索引: {int(summary.get('updated_stocks', 0) or 0)}\n"
            f"有行情待补库: {int(summary.get('valid_data_files', 0) or 0)}\n"
            f"空行情库跳过: {int(summary.get('empty_data_files', 0) or 0)}\n"
            "无缺失周期数据跳过: "
            f"{int(summary.get('no_missing_period_data_files', 0) or 0)}\n"
            f"非证券库跳过: {int(summary.get('non_stock_files', 0) or 0)}\n"
            f"读取失败: {int(summary.get('failed_files', 0) or 0)}"
        )

    def on_scan_finished(self, summary):
        """只读核验完成；仅对完整且有数据的结果执行备份和事务写入。"""
        self.progress_bar.setVisible(False)
        if self._pending_close_after_scan or summary.get("cancelled"):
            self.statusBar.showMessage("索引核验已取消，metadata.db 未修改")
            return

        final_summary = dict(summary)
        if int(summary.get("failed_files", 0) or 0) > 0:
            self._last_reindex_summary = final_summary
            details = self._format_reindex_summary(final_summary)
            failures = "\n".join(summary.get("failures", [])[:5])
            QMessageBox.warning(
                self,
                "索引核验未提交",
                details
                + "\n\n存在无法读取的候选库，为避免部分提交，metadata.db 未修改。"
                + (f"\n\n失败样例:\n{failures}" if failures else ""),
            )
            self.statusBar.showMessage("索引核验存在读取失败，未修改 metadata.db")
            return

        if int(summary.get("valid_data_files", 0) or 0) > 0:
            manager = self._request_writable_manager("同步元数据索引")
            if manager is None:
                self._last_reindex_summary = final_summary
                return
            try:
                final_summary = manager.apply_metadata_reindex(summary)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "索引同步失败",
                    f"只读核验已经完成，但写入失败：\n{exc}\n\nmetadata.db 事务未提交。",
                )
                self.statusBar.showMessage("索引写入失败，metadata.db 事务未提交")
                return
            finally:
                try:
                    self._ensure_read_only_manager()
                except Exception:
                    pass

        self._last_reindex_summary = final_summary
        try:
            self._ensure_read_only_manager()
        except Exception:
            pass
        self.refresh_stock_list()
        summary_text = self._format_reindex_summary(final_summary)
        if final_summary.get("backup_path"):
            summary_text += f"\n\n元数据备份: {final_summary['backup_path']}"
        if int(final_summary.get("updated_stocks", 0) or 0) > 0:
            title = "索引同步完成"
            self.statusBar.showMessage("索引核验与同步完成")
        else:
            title = "索引核验完成"
            summary_text += "\n\n没有发现漏索引的有效行情库，metadata.db 未修改。"
            self.statusBar.showMessage("索引核验完成：没有有效行情库需要补索引")
        QMessageBox.information(self, title, summary_text)

    def on_scan_error(self, error: str):
        """核验线程异常。"""
        self.progress_bar.setVisible(False)
        if not self._pending_close_after_scan:
            QMessageBox.warning(self, "索引核验错误", error)
            self.statusBar.showMessage("索引核验失败，metadata.db 未修改")

    def _on_scan_thread_done(self):
        thread = self.scan_thread
        summary = getattr(thread, "result", None) if thread is not None else None
        error_message = getattr(thread, "error_message", "") if thread is not None else ""
        self.scan_thread = None
        if thread is not None:
            thread.deleteLater()
        self.reindex_action.setEnabled(True)
        if self._pending_close_after_scan:
            self._pending_close_after_scan = False
            QTimer.singleShot(0, self.close)
        elif error_message:
            self.on_scan_error(error_message)
        elif summary is not None:
            self.on_scan_finished(summary)

    def show_statistics(self):
        """显示统计信息"""
        if self._reject_while_reindex_active("读取统计信息"):
            return
        if not self.manager:
            QMessageBox.warning(self, "提示", "请先加载数据目录")
            return

        stats = self.manager.get_statistics()

        msg = f"""数据目录统计信息

数据路径: {stats['data_root']}
可浏览股票数（元数据索引）: {stats['indexed_stocks']}
物理 .db 文件总数: {stats['total_database_files']}
六位证券数据库文件: {stats['total_stocks']}
未纳入索引的六位文件: {stats['unverified_unindexed_database_files']}
周期标志核验候选证券库: {stats.get('period_flag_candidate_files', 0)}
非证券数据库文件: {stats['non_stock_database_files']}
仅有元数据但文件缺失: {stats['metadata_only_stocks']}
总大小: {stats['total_size_mb']} MB

各市场:
"""
        for market, info in stats['markets'].items():
            msg += (
                f"  {market}: 可浏览 {info.get('indexed_stocks', 0)} 只 / "
                f"证券库 {info['stocks']} 个 / .db文件 {info.get('database_files', info['stocks'])} 个, "
                f"{info['size_mb']} MB\n"
            )

        msg += f"""
各周期股票数:
  日线(1d): {stats['by_period']['1d']}
  1分钟(1m): {stats['by_period']['1m']}
  5分钟(5m): {stats['by_period']['5m']}
  Tick: {stats['by_period']['tick']}

说明:
  左侧股票树与“可浏览股票数”均使用 metadata.db 索引。
  “未纳入索引的六位文件”可能是空行情库，核验后仍会保留，不等于漏数据。
  “周期标志核验候选”只表示至少一个周期标志为否，不代表该周期存在行情。
  请用工具栏“核验索引”做只读确认。
  核验只会把确有行情的证券库写入索引，空库和非证券文件不会污染股票树。
"""

        if self._last_reindex_summary:
            msg += "\n本次会话最近一次核验:\n" + self._format_reindex_summary(
                self._last_reindex_summary
            ) + "\n"

        QMessageBox.information(self, "统计信息", msg)

    def run_wal_repair(self):
        if self._reject_while_reindex_active("执行 WAL 修复"):
            return
        if not self.manager:
            QMessageBox.warning(self, "提示", "请先加载数据目录")
            return

        confirm = QMessageBox.question(
            self,
            "确认修复",
            "将检测并修复全部数据库的WAL错误，是否继续？",
            QMessageBox.Yes | QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return

        if self.wal_repair_thread:
            try:
                if self.wal_repair_thread.isRunning():
                    QMessageBox.information(self, "提示", "修复任务正在进行")
                    return
            except Exception:
                pass

        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.statusBar.showMessage("正在执行WAL修复...")

        self.wal_repair_thread = WalRepairThread(self.manager.data_root)
        self.wal_repair_thread.finished.connect(self._on_wal_repair_finished)
        self.wal_repair_thread.error.connect(self._on_wal_repair_error)
        self.wal_repair_thread.start()

    def _on_wal_repair_finished(self, result: dict):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setVisible(False)
        self.statusBar.showMessage("WAL修复完成")

        msg = (
            f"总计: {result.get('total', 0)}\n"
            f"正常: {result.get('ok', 0)}\n"
            f"修复: {result.get('fixed', 0)}\n"
            f"重建: {result.get('rebuilt', 0)}\n"
            f"失败: {len(result.get('failed', []))}\n"
            f"删除WAL/SHM: {result.get('removed_sidecars', 0)}"
        )

        if result.get("rebuilt", 0) > 0:
            msg += f"\n隔离目录: {result.get('quarantine_root', '')}"

        QMessageBox.information(self, "WAL修复结果", msg)

    def _on_wal_repair_error(self, error: str):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setVisible(False)
        self.statusBar.showMessage("WAL修复失败")
        QMessageBox.warning(self, "WAL修复失败", error)

    def _extract_duckdb_lock_info(self, error: Exception):
        """从DuckDB文件锁错误中提取占用进程信息"""
        import re

        msg = str(error) if error is not None else ""
        pid = None
        process_path = None
        file_path = None

        file_match = re.search(
            r'(?:Cannot open file|Could not set lock on file)\s+"([^"]+)"',
            msg,
            flags=re.IGNORECASE,
        )
        if file_match:
            file_path = file_match.group(1)

        pid_match = re.search(r'\(PID\s+(\d+)\)', msg)
        if pid_match:
            try:
                pid = int(pid_match.group(1))
            except Exception:
                pid = None

        proc_match = re.search(
            r'(?:open in|held in)\s+(.+?)\s*\(PID',
            msg,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if proc_match:
            process_path = " ".join(proc_match.group(1).split())

        return {
            "message": msg,
            "pid": pid,
            "process_path": process_path,
            "file_path": file_path
        }

    def _terminate_process_by_pid(
        self,
        pid: int,
        expected_create_time: Optional[float] = None,
    ):
        """结束精确PID；提供启动时间时先防止PID复用误杀。"""
        import subprocess

        if pid <= 0:
            return False, "无效的PID"
        if pid == os.getpid():
            return False, "拒绝结束当前GUI进程"

        if expected_create_time is not None:
            identity, identity_error = inspect_process_identity(pid)
            if identity is None:
                return False, f"无法复核进程身份，进程可能已经退出: {identity_error}"
            actual_create_time = identity.create_time
            if actual_create_time is None or abs(actual_create_time - expected_create_time) > 0.01:
                return False, "PID 已被系统复用，已拒绝结束新进程"

        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                text=True,
                errors="replace",
                shell=False
            )
            if result.returncode == 0:
                msg = (result.stdout or "").strip() or f"已结束进程 PID {pid}"
                logging.info("已按用户确认结束 DuckDB 占用进程 PID %s", pid)
                return True, msg

            err_msg = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()
            return False, err_msg or f"结束进程失败 (PID {pid})"
        except Exception as e:
            return False, str(e)

    def _local_database_release_blockers(self) -> List[str]:
        """返回当前窗口中不能直接断开数据库的活动模块。"""
        blockers = []
        if DuckDBViewer._is_reindex_scan_running(self):
            blockers.append("元数据索引核验")
        try:
            load_thread = getattr(self, "load_thread", None)
            if load_thread is not None and load_thread.isRunning():
                blockers.append("行情数据查询")
        except RuntimeError:
            self.load_thread = None
        for attr_name, display_name in (
            ("import_dialog", "miniQMT 导入"),
            ("baostock_import_dialog", "BaoStock 导入"),
            ("tushare_import_dialog", "Tushare 导入"),
            ("tencent_import_dialog", "tx导入"),
            ("ths_import_dialog", "同花顺导入"),
            ("http_import_dialog", "桥接数据导入"),
        ):
            try:
                if getattr(self, attr_name, None) is not None:
                    blockers.append(display_name)
            except RuntimeError:
                setattr(self, attr_name, None)
        try:
            if self.wal_repair_thread is not None and self.wal_repair_thread.isRunning():
                blockers.append("WAL 修复")
        except RuntimeError:
            self.wal_repair_thread = None
        try:
            scheduled_process = getattr(self, "scheduled_sync_process", None)
            if scheduled_process is not None and scheduled_process.poll() is None:
                blockers.append("定时补充独立进程")
        except (AttributeError, OSError):
            pass

        # 桌面回测在主GUI进程的QThread内运行。此时系统句柄只能看到同一个PID，
        # 无法把“数据管理连接”和“回测连接”分开；必须拒绝一键重置单例。
        try:
            for window in QApplication.topLevelWidgets():
                if window is self:
                    continue
                strategy_thread = getattr(window, "strategy_thread", None)
                if strategy_thread is not None and strategy_thread.isRunning():
                    blockers.append("桌面回测/策略运行")
                    break
        except (AttributeError, RuntimeError):
            pass
        return blockers

    def _release_local_duckdb_connections(self):
        """安全释放当前GUI进程持有的连接，不自动重新连接。"""
        blockers = self._local_database_release_blockers()
        if blockers:
            return (
                False,
                "以下数据任务或窗口仍在使用当前连接："
                + "、".join(blockers)
                + "。请先正常停止并关闭这些模块，避免中断写库。",
            )

        release_errors = []
        try:
            self._close_current_manager(skip_checkpoint=True)
        except Exception as exc:
            release_errors.append(f"关闭当前管理器失败: {exc}")

        try:
            DuckDBManager.reset_instance()
        except Exception as exc:
            release_errors.append(f"重置DuckDB单例失败: {exc}")

        try:
            try:
                from . import xtdata_adapter as _xt_adapter
            except ImportError:
                import xtdata_adapter as _xt_adapter
            if hasattr(_xt_adapter, "reset_manager"):
                _xt_adapter.reset_manager()
        except Exception as exc:
            release_errors.append(f"重置xtdata_adapter失败: {exc}")

        try:
            from khDataSource import get_data_source_manager

            data_source_manager = get_data_source_manager()
            if data_source_manager is not None:
                adapter = getattr(data_source_manager, "_duckdb_adapter", None)
                if adapter is not None and hasattr(adapter, "reset_manager"):
                    adapter.reset_manager()
        except Exception as exc:
            release_errors.append(f"重置数据源DuckDB适配器失败: {exc}")

        try:
            import gc

            gc.collect()
            time.sleep(0.2)
        except Exception as exc:
            release_errors.append(f"等待文件句柄释放失败: {exc}")

        if release_errors:
            message = (
                "未能确认当前数据管理进程的 DuckDB 连接已全部释放。"
                "为避免误判，请勿继续强制结束外部进程；可关闭数据管理窗口后重试。"
                "\n\n失败步骤：\n"
            ) + "\n".join(
                f"- {item}" for item in release_errors
            )
            logging.warning(
                "数据管理释放本进程DuckDB连接失败，errors=%s detail=%s",
                len(release_errors),
                "; ".join(release_errors),
            )
            return False, message

        message = "已释放当前数据管理进程持有的 DuckDB 连接。"
        logging.info("数据管理已释放本进程DuckDB连接")
        return True, message

    def _reopen_manager_after_release(self):
        """释放连接后重建管理器并刷新列表"""
        if not self.data_root or not os.path.exists(self.data_root):
            raise ValueError("数据目录无效，请先选择正确的DuckDB数据目录")

        self._open_manager(self.data_root, read_only=True)
        self.refresh_stock_list()

    def release_duckdb_occupancy(self):
        """释放DuckDB占用（先释放本进程连接，必要时可结束外部占用进程）"""
        self.statusBar.showMessage("正在释放DuckDB占用...")

        released, release_message = self._release_local_duckdb_connections()
        if not released:
            QMessageBox.warning(self, "无法释放连接", release_message)
            self.statusBar.showMessage("DuckDB占用释放已取消")
            return

        try:
            self._reopen_manager_after_release()
            msg = release_message + "\n\n已重新加载数据目录。"
            QMessageBox.information(self, "完成", msg)
            self.statusBar.showMessage("DuckDB占用已释放")
            return
        except Exception as reopen_error:
            lock_info = self._extract_duckdb_lock_info(reopen_error)
            pid = lock_info.get("pid")
            process_path = lock_info.get("process_path")
            file_path = lock_info.get("file_path")

            detail_lines = ["释放本进程连接后，仍无法重新打开DuckDB。"]
            if file_path:
                detail_lines.append(f"被锁文件: {file_path}")
            if process_path:
                detail_lines.append(f"占用进程: {process_path}")
            if pid:
                detail_lines.append(f"占用PID: {pid}")
            detail_lines.append(release_message)

            if pid and pid != os.getpid():
                reply = QMessageBox.question(
                    self,
                    "检测到外部占用",
                    "\n".join(detail_lines) + "\n\n是否尝试结束该占用进程并自动重试？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No
                )
                if reply == QMessageBox.Yes:
                    ok, kill_msg = self._terminate_process_by_pid(pid)
                    if not ok:
                        QMessageBox.warning(
                            self,
                            "结束进程失败",
                            f"无法结束占用进程 (PID {pid}):\n{kill_msg}"
                        )
                        self.statusBar.showMessage("DuckDB占用释放失败")
                        return

                    try:
                        import time
                        time.sleep(0.3)
                        self._reopen_manager_after_release()
                        QMessageBox.information(
                            self,
                            "完成",
                            f"已结束占用进程并重新加载数据目录。\n\n{kill_msg}"
                        )
                        self.statusBar.showMessage("DuckDB占用已释放")
                        return
                    except Exception as retry_error:
                        QMessageBox.warning(
                            self,
                            "重试失败",
                            f"结束进程后重试仍失败:\n{retry_error}"
                        )
                        self.statusBar.showMessage("DuckDB占用释放失败")
                        return

            QMessageBox.warning(
                self,
                "释放失败",
                "\n".join(detail_lines) + f"\n\n错误详情:\n{reopen_error}"
            )
            self.statusBar.showMessage("DuckDB占用释放失败")

    def open_scheduled_sync(self):
        """以独立进程打开定时数据补充窗口。

        定时补充是长驻任务，不能绑定在数据管理窗口的 Qt parent 或 DuckDBManager
        生命周期上。否则关闭数据管理模块/主界面时，会连带关闭定时补充窗口或释放其
        数据库连接。这里统一拉起一个独立 GUI 进程，让它自行管理托盘、退出和连接。
        """
        if self._reject_while_reindex_active("打开定时补充"):
            return
        try:
            loading_dialog = getattr(self, "_scheduled_sync_loading_dialog", None)
            if loading_dialog is not None and loading_dialog.isVisible():
                loading_dialog.raise_()
                loading_dialog.activateWindow()
                return

            # 已经从当前数据管理窗口启动过且仍在运行时，避免重复打开多个定时任务。
            process = getattr(self, "scheduled_sync_process", None)
            if process is not None and process.poll() is None:
                QMessageBox.information(
                    self,
                    "定时补充已运行",
                    "定时补充模块已经在独立窗口中运行。\n\n"
                    "它不会随数据管理模块关闭而退出，如需结束请在定时补充窗口或托盘菜单中退出。"
                )
                return

            data_root = self.data_root
            if not data_root and self.manager:
                data_root = getattr(self.manager, "data_root", None)
            if not data_root:
                QMessageBox.warning(self, "提示", "请先加载 DuckDB 数据目录")
                return

            data_root = os.path.abspath(data_root)
            os.makedirs(data_root, exist_ok=True)

            parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

            launcher = windowed_python_executable()
            if getattr(sys, "frozen", False):
                # 打包模式：主 exe 已在 GUIkhQuant.main() 中处理 --scheduled-sync。
                command = [launcher, "--scheduled-sync", data_root]
            else:
                # 源码模式：通过 GUIkhQuant.py 进入同一套启动逻辑。
                gui_entry = os.path.join(parent_dir, "GUIkhQuant.py")
                if not os.path.exists(gui_entry):
                    QMessageBox.warning(self, "错误", f"找不到主程序入口:\n{gui_entry}")
                    return
                command = [launcher, gui_entry, "--scheduled-sync", data_root]

            creationflags = 0
            if sys.platform.startswith("win"):
                creationflags = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                )

            # 独立模块真正执行时需要以写模式打开数据库。启动子进程前先释放
            # 数据管理窗口持有的只读 metadata/个股连接，避免跨进程 DuckDB 冲突。
            manager_was_open = self.manager is not None
            if manager_was_open:
                self._close_current_manager(skip_checkpoint=True)

            desktop = QDesktopWidget()
            screen_index = desktop.screenNumber(self)
            screen_center = desktop.availableGeometry(screen_index).center()
            command.extend([
                "--screen-center",
                f"{screen_center.x()},{screen_center.y()}",
            ])

            ready_file = os.path.join(
                tempfile.gettempdir(),
                f"khquant_scheduled_sync_{os.getpid()}_{uuid.uuid4().hex}.ready",
            )
            command.extend(["--ready-file", ready_file])
            self._scheduled_sync_ready_file = ready_file
            self._scheduled_sync_loading_dialog = ScheduledSyncStartupDialog(
                self, getattr(self, "font_scale", 1.0)
            )
            self._scheduled_sync_loading_dialog.show_centered()
            QApplication.processEvents()

            try:
                self.scheduled_sync_process = subprocess.Popen(
                    command,
                    cwd=parent_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    creationflags=creationflags,
                )
            except Exception as e:
                if manager_was_open:
                    try:
                        self._open_manager(data_root, read_only=True)
                        self.refresh_stock_list()
                    except Exception as restore_error:
                        logging.warning(f"定时补充启动失败后恢复数据管理连接失败: {restore_error}")
                self._finish_scheduled_sync_startup()
                QMessageBox.warning(self, "错误", f"无法启动定时补充模块:\n{e}")
                return

            self._scheduled_sync_start_deadline = time.monotonic() + 30.0
            self._scheduled_sync_start_timer = QTimer(self)
            self._scheduled_sync_start_timer.setInterval(100)
            self._scheduled_sync_start_timer.timeout.connect(
                self._poll_scheduled_sync_startup
            )
            self._scheduled_sync_start_timer.start()

            self.statusBar.showMessage(
                "定时补充模块已独立启动；数据管理连接已释放，退出定时模块后可点击刷新恢复"
            )

        except Exception as e:
            import traceback
            self._finish_scheduled_sync_startup()
            QMessageBox.critical(self, "错误", f"启动定时补充窗口失败:\n{e}\n{traceback.format_exc()}")

    def _poll_scheduled_sync_startup(self):
        """等待独立进程确认窗口已经进入事件循环。"""
        ready_file = getattr(self, "_scheduled_sync_ready_file", "")
        if ready_file and os.path.exists(ready_file):
            self._finish_scheduled_sync_startup()
            self.statusBar.showMessage(
                "定时补充模块已打开；数据管理连接已释放，退出定时模块后可点击刷新恢复"
            )
            return

        process = getattr(self, "scheduled_sync_process", None)
        if process is not None and process.poll() is not None:
            self._finish_scheduled_sync_startup()
            QMessageBox.warning(self, "启动失败", "定时补充进程已提前退出，请查看运行日志。")
            return

        if time.monotonic() >= getattr(self, "_scheduled_sync_start_deadline", 0.0):
            self._finish_scheduled_sync_startup()
            QMessageBox.warning(
                self,
                "启动较慢",
                "定时补充模块暂未返回就绪状态。进程可能仍在后台加载，请稍后查看任务栏。",
            )

    def _finish_scheduled_sync_startup(self):
        """关闭启动提示并清理一次性就绪文件。"""
        timer = getattr(self, "_scheduled_sync_start_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
            self._scheduled_sync_start_timer = None

        dialog = getattr(self, "_scheduled_sync_loading_dialog", None)
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
            self._scheduled_sync_loading_dialog = None

        ready_file = getattr(self, "_scheduled_sync_ready_file", "")
        if ready_file:
            try:
                os.remove(ready_file)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logging.debug("清理定时补充就绪文件失败: %s", exc)
        self._scheduled_sync_ready_file = ""

    def show_import_dialog(self, source: object = None):
        """显示统一历史行情导入对话框。

        ``source`` 仅用于入口预选来源；用户仍可在窗口的来源下拉框中
        切换。无参数调用保持原有 miniQMT 入口行为，桥接工具栏按钮则
        通过 :meth:`show_bridge_import_dialog` 显式预选 ``QMT_NATIVE``。
        """
        requested_source = source
        if requested_source is None:
            operation_name = "打开 miniQMT 导入"
        else:
            try:
                requested_source = _canonical_history_source(requested_source)
            except Exception:
                # 让 MiniQMTImportDialog 自己显示配置错误；这里不把无效
                # 入口参数静默改成 miniQMT。
                requested_source = source
            if requested_source == QMT_NATIVE:
                operation_name = "打开大QMT原生桥导入"
            elif requested_source == MINIQMT:
                operation_name = "打开 miniQMT 导入"
            else:
                operation_name = "打开历史行情导入"

        manager = self._request_writable_manager(operation_name)
        if manager is None:
            return

        # 如果对话框已存在且可见，激活它
        if self.import_dialog:
            try:
                if self.import_dialog.isVisible():
                    # 两个工具栏入口共用一个现代窗口。若窗口尚未开始
                    # 导入，桥接入口可即时把来源切到 QMT_NATIVE；运行中
                    # 不改动线程已经锁定的来源，避免界面选择与后台任务
                    # 不一致。
                    if requested_source is not None:
                        running = getattr(self.import_dialog, "is_import_running", None)
                        try:
                            is_running = bool(running()) if callable(running) else False
                        except Exception:
                            is_running = False
                        if not is_running:
                            self._select_history_import_source(
                                self.import_dialog, requested_source
                            )
                    self.import_dialog.raise_()
                    self.import_dialog.activateWindow()
                    return
            except RuntimeError:
                # 对话框已被删除，清除引用
                self.import_dialog = None

        # 创建新的导入对话框
        dialog_kwargs = {}
        if requested_source is not None:
            dialog_kwargs["history_import_source"] = requested_source
        # 普通数据管理入口只呈现 MiniQMT。开发源码中保留原生桥模式，
        # 由专用内部入口显式传入来源时才构建对应控件。
        dialog_kwargs["allow_native_source"] = requested_source == QMT_NATIVE
        self.import_dialog = MiniQMTImportDialog(manager, self, **dialog_kwargs)
        # 连接destroyed信号，在对话框被删除时清除引用
        self.import_dialog.destroyed.connect(
            lambda _obj=None, owner=self:
            _dispatch_import_dialog_destroyed(owner, "import_dialog")
        )
        if hasattr(self.import_dialog, "apply_ui_scale"):
            self.import_dialog.apply_ui_scale(get_ui_font_scale())
        self.import_dialog.show()

        # 导入完成后会自动刷新列表（在对话框的finished信号中处理）

    @staticmethod
    def _select_history_import_source(dialog, source: object) -> bool:
        """在已打开的统一导入窗口中预选历史数据源。

        通过控件触发已有的 ``_on_history_source_changed``，确保 QSettings、
        全局配置、tick 复权控件和来源探测同时更新。返回值表示是否找到
        可切换的来源；旧版/测试替身没有下拉框时不会抛异常。
        """
        try:
            canonical = _canonical_history_source(source)
        except Exception:
            return False
        combo = getattr(dialog, "history_source_combo", None)
        if combo is None:
            combo = getattr(dialog, "history_import_source_combo", None)
        if combo is not None:
            try:
                index = combo.findData(canonical)
                if index < 0:
                    return False
                if combo.currentIndex() != index:
                    combo.setCurrentIndex(index)
                return True
            except Exception:
                return False
        # 兼容极旧的统一窗口替身：只在没有控件时更新属性，不尝试写
        # 持久化设置，以免把半初始化对象标记成已切换。
        try:
            if hasattr(dialog, "history_source"):
                dialog.history_source = canonical
                dialog.source = canonical
                return True
        except Exception:
            pass
        return False

    def show_bridge_import_dialog(self):
        """显示统一桥接导入窗口，并默认预选大 QMT 原生桥。"""
        return self.show_import_dialog(source=QMT_NATIVE)

    def show_baostock_import_dialog(self):
        manager = self._request_writable_manager("打开 BaoStock 导入")
        if manager is None:
            return

        if self.baostock_import_dialog:
            try:
                if self.baostock_import_dialog.isVisible():
                    self.baostock_import_dialog.raise_()
                    self.baostock_import_dialog.activateWindow()
                    return
            except RuntimeError:
                self.baostock_import_dialog = None

        self.baostock_import_dialog = BaoStockImportDialog(manager, self)
        self.baostock_import_dialog.destroyed.connect(
            lambda _obj=None, owner=self:
            _dispatch_import_dialog_destroyed(owner, "baostock_import_dialog")
        )
        if hasattr(self.baostock_import_dialog, "apply_ui_scale"):
            self.baostock_import_dialog.apply_ui_scale(get_ui_font_scale())
        self.baostock_import_dialog.show()

    def show_legacy_http_import_dialog(self):
        """显示旧版 HTTP miniQMT 桥接对话框（显式 legacy 入口）。

        该入口保留给仍依赖 HTTP 服务地址/API key 的旧部署；普通“桥接
        导入”按钮不再调用它。
        """
        manager = self._request_writable_manager("打开桥接数据导入")
        if manager is None:
            return

        if self.http_import_dialog:
            try:
                if self.http_import_dialog.isVisible():
                    self.http_import_dialog.raise_()
                    self.http_import_dialog.activateWindow()
                    return
            except RuntimeError:
                self.http_import_dialog = None

        self.http_import_dialog = HttpBridgeImportDialog(manager, self)
        self.http_import_dialog.destroyed.connect(
            lambda _obj=None, owner=self:
            _dispatch_import_dialog_destroyed(owner, "http_import_dialog")
        )
        if hasattr(self.http_import_dialog, "apply_ui_scale"):
            self.http_import_dialog.apply_ui_scale(get_ui_font_scale())
        self.http_import_dialog.show()

    def show_http_import_dialog(self):
        """兼容旧调用名，显式打开 legacy HTTP 桥接窗口。"""
        # 使用类方法调用保留对历史嵌入方/单元测试中最小 viewer 替身的
        # 兼容性；真实 QMainWindow 实例的行为与直接 self 调用相同。
        return DuckDBViewer.show_legacy_http_import_dialog(self)

    def show_tushare_import_dialog(self):
        """显示Tushare导入对话框"""
        # 检查 token 是否已配置
        if not load_tushare_settings().token:
            QMessageBox.warning(
                self, "未配置Token",
                "请先在【软件设置 → Tushare设置】中填写 Tushare Token，然后再使用此功能。"
            )
            return
        manager = self._request_writable_manager("打开 Tushare 导入")
        if manager is None:
            return

        if self.tushare_import_dialog:
            try:
                if self.tushare_import_dialog.isVisible():
                    self.tushare_import_dialog.raise_()
                    self.tushare_import_dialog.activateWindow()
                    return
            except RuntimeError:
                self.tushare_import_dialog = None

        self.tushare_import_dialog = TushareImportDialog(manager, self)
        self.tushare_import_dialog.destroyed.connect(
            lambda _obj=None, owner=self:
            _dispatch_import_dialog_destroyed(owner, "tushare_import_dialog")
        )
        if hasattr(self.tushare_import_dialog, "apply_ui_scale"):
            self.tushare_import_dialog.apply_ui_scale(get_ui_font_scale())
        self.tushare_import_dialog.show()

    def show_tencent_import_dialog(self):
        """Open the network-source dialog without opening a database writer."""
        if self.tencent_import_dialog is not None:
            self.tencent_import_dialog.show()
            self.tencent_import_dialog.raise_()
            self.tencent_import_dialog.activateWindow()
            return
        from duckdb_storage.tencent_dialog import TencentImportDialog
        self.tencent_import_dialog = TencentImportDialog(self.data_root, self)
        self.tencent_import_dialog.destroyed.connect(
            lambda _obj=None, owner=self:
            _dispatch_import_dialog_destroyed(owner, "tencent_import_dialog")
        )
        self.tencent_import_dialog.show()

    def show_ths_import_dialog(self):
        """打开同花顺（扶摇开放平台）数据导入对话框。"""
        if self.ths_import_dialog is not None:
            self.ths_import_dialog.show()
            self.ths_import_dialog.raise_()
            self.ths_import_dialog.activateWindow()
            return
        import importlib
        import duckdb_storage.ths_dialog as ths_dialog_mod
        importlib.reload(ths_dialog_mod)
        self.ths_import_dialog = ths_dialog_mod.THSImportDialog(self.data_root, self)
        self.ths_import_dialog.destroyed.connect(
            lambda _obj=None, owner=self:
            _dispatch_import_dialog_destroyed(owner, "ths_import_dialog")
        )
        self.ths_import_dialog.show()

    def _prepare_tencent_import(self, root):
        # Do not close a shared manager while a query/backtest/other importer
        # is using it. Each worker creates and closes its own write connections.
        blockers = [name for name in self._local_database_release_blockers() if name != "tx导入"]
        for window in QApplication.topLevelWidgets():
            if isinstance(window, DuckDBViewer) and window is not self and window.manager is not None:
                if os.path.normcase(os.path.abspath(window.data_root or '.')) == os.path.normcase(os.path.abspath(root)):
                    blockers.append("同目录的另一个数据管理窗口")
        if blockers:
            QMessageBox.warning(self, "暂不能开始导入", "请先结束或关闭：" + "、".join(blockers))
            return False
        self._close_current_manager(skip_checkpoint=True)
        DuckDBManager.close_instances(root)
        self._tencent_disabled_widgets = [self.centralWidget(), *self.findChildren(QToolBar)]
        self._tencent_enabled_states = [widget.isEnabled() for widget in self._tencent_disabled_widgets]
        for widget in self._tencent_disabled_widgets:
            widget.setEnabled(False)
        return True

    def _finish_tencent_import(self, root):
        for widget, enabled in zip(getattr(self, "_tencent_disabled_widgets", []),
                                   getattr(self, "_tencent_enabled_states", [])):
            widget.setEnabled(enabled)
        self._tencent_disabled_widgets = []
        if self._viewer_closing:
            return
        if self.data_root and os.path.isdir(self.data_root):
            try:
                self._open_manager(self.data_root, read_only=True)
                self.refresh_stock_list()
            except Exception as exc:
                self.manager = None
                self.statusBar.showMessage(f"tx任务已结束；恢复只读浏览失败，可稍后刷新：{exc}")
                return
        self.statusBar.showMessage(f"tx任务已结束，保存目录：{root}")

    def _prepare_ths_import(self, root):
        blockers = [name for name in self._local_database_release_blockers() if name != "同花顺导入"]
        for window in QApplication.topLevelWidgets():
            if isinstance(window, DuckDBViewer) and window is not self and window.manager is not None:
                if os.path.normcase(os.path.abspath(window.data_root or '.')) == os.path.normcase(os.path.abspath(root)):
                    blockers.append("同目录的另一个数据管理窗口")
        if blockers:
            QMessageBox.warning(self, "暂不能开始导入", "请先结束或关闭：" + "、".join(blockers))
            return False
        self._close_current_manager(skip_checkpoint=True)
        DuckDBManager.close_instances(root)
        self._ths_disabled_widgets = [self.centralWidget(), *self.findChildren(QToolBar)]
        self._ths_enabled_states = [widget.isEnabled() for widget in self._ths_disabled_widgets]
        for widget in self._ths_disabled_widgets:
            widget.setEnabled(False)
        return True

    def _finish_ths_import(self, root):
        for widget, enabled in zip(getattr(self, "_ths_disabled_widgets", []),
                                   getattr(self, "_ths_enabled_states", [])):
            widget.setEnabled(enabled)
        self._ths_disabled_widgets = []
        if self._viewer_closing:
            return
        if self.data_root and os.path.isdir(self.data_root):
            try:
                self._open_manager(self.data_root, read_only=True)
                self.refresh_stock_list()
            except Exception as exc:
                self.manager = None
                self.statusBar.showMessage(f"同花顺任务已结束；恢复只读浏览失败，可稍后刷新：{exc}")
                return
        self.statusBar.showMessage(f"同花顺任务已结束，保存目录：{root}")

    def closeEvent(self, event):
        """窗口关闭事件"""
        self._viewer_closing = True
        scan_thread = getattr(self, "scan_thread", None)
        if scan_thread is not None and scan_thread.isRunning():
            self._pending_close_after_scan = True
            scan_thread.requestInterruption()
            self.reindex_action.setEnabled(False)
            self.statusBar.showMessage(
                "正在取消只读索引核验，完成后将自动关闭；metadata.db 不会写入"
            )
            event.ignore()
            return
        if self._pending_close_after_tushare_stop:
            event.ignore()
            return
        running_dialogs = []
        for attr_name, display_name in (
            ("import_dialog", "miniQMT"),
            ("baostock_import_dialog", "BaoStock"),
            ("tushare_import_dialog", "Tushare"),
            ("tencent_import_dialog", "tx"),
            ("ths_import_dialog", "同花顺"),
        ):
            dialog = getattr(self, attr_name, None)
            if dialog is None:
                continue
            try:
                if dialog.is_import_running():
                    running_dialogs.append((attr_name, display_name, dialog))
            except RuntimeError:
                setattr(self, attr_name, None)

        if running_dialogs:
            names = "、".join(item[1] for item in running_dialogs)
            reply = QMessageBox.question(
                self,
                "确认关闭",
                f"{names} 数据任务正在进行中，确定要关闭数据管理模块吗？\n"
                "系统会先停止领取新任务，等待当前写入完成并收尾数据库，再关闭窗口。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self._pending_close_after_tushare_stop = True
                self._pending_import_dialogs_to_close = {
                    item[0] for item in running_dialogs
                }
                for attr_name, _display_name, dialog in running_dialogs:
                    dialog.destroyed.connect(
                        lambda _obj=None, name=attr_name:
                        self._finish_close_after_import_stop(name)
                    )
                    dialog.request_close_after_stop()
                event.ignore()
                return

            self._viewer_closing = False
            event.ignore()
            return

        self._finish_scheduled_sync_startup()
        if self.manager:
            self.manager.close_all()
        event.accept()

    def _finish_close_after_tushare_stop(self):
        """兼容旧调用：等待导入线程安全退出后，再关闭主窗口。"""
        self._finish_close_after_import_stop("tushare_import_dialog")

    def _finish_close_after_import_stop(self, attr_name: str):
        """等待所有活动导入窗口完成数据库收尾后，再关闭主窗口。"""
        pending = getattr(self, "_pending_import_dialogs_to_close", set())
        pending.discard(attr_name)
        self._pending_import_dialogs_to_close = pending
        if pending:
            return
        self._pending_close_after_tushare_stop = False
        QTimer.singleShot(0, self.close)


# ============================================================
# MiniQMT数据导入对话框
# ============================================================

class ImportThread(QThread):
    """数据导入线程"""
    progress = pyqtSignal(int, int, int)  # (百分比, 已完成数, 总数)
    status = pyqtSignal(str)
    finished = pyqtSignal(dict)  # 返回结果统计
    error = pyqtSignal(str)

    # K线数据字段（完整）
    KLINE_FIELDS = [
        'time', 'open', 'high', 'low', 'close', 'volume', 'amount',
        'settelementPrice', 'openInterest', 'preClose', 'suspendFlag'
    ]

    # Tick数据字段（完整）
    TICK_FIELDS = [
        'time', 'lastPrice', 'open', 'high', 'low', 'lastClose',
        'amount', 'volume', 'pvolume', 'stockStatus', 'openInt',
        'lastSettlementPrice', 'askPrice', 'bidPrice', 'askVol', 'bidVol',
        'transactionNum'
    ]

    def __init__(self, manager: DuckDBManager, stocks: List[str], periods: List[str],
                 start_date: str, end_date: str, dividend_types: Optional[List[str]] = None,
                 source: str = MINIQMT, bridge_dir: Optional[str] = None,
                 max_task_retries: int = DEFAULT_HISTORY_MAX_TASK_RETRIES,
                 retry_backoff: Optional[List[float]] = None,
                 instance_generation: Optional[str] = None,
                 native_profile: Optional[str] = None,
                 profile: Optional[str] = None,
                 max_inflight: Optional[int] = None,
                 batch_size: Optional[int] = None,
                 span_rows: Optional[int] = None,
                 span_bytes: Optional[int] = None,
                 mode: Optional[str] = None,
                 cache_strategy: Optional[str] = None,
                 cancel_after: Optional[float] = None):
        super().__init__()
        self.manager = manager
        self.stocks = stocks
        self.periods = periods  # 改为列表，支持多周期
        self.start_date = start_date
        self.end_date = end_date
        self.dividend_types = dividend_types
        self.source = _canonical_history_source(source)
        self.bridge_dir = str(bridge_dir) if bridge_dir else None
        self.max_task_retries, self.retry_backoff = _coerce_retry_settings(
            max_task_retries, retry_backoff
        )
        self.instance_generation = _normalise_generation_aliases(instance_generation)
        # Keep the direct/native compatibility path on the same execution
        # contract as MultiProcessImporter.  These attributes are intentionally
        # public so embedding callers can override them without changing the
        # historical constructor positional layout.
        self.force = False
        self.allow_gaps = _default_allow_gaps_for_source(self.source)
        self.local_only = False
        self.idempotent = True
        self.max_attempts = DEFAULT_NATIVE_MAX_ATTEMPTS if self.source == QMT_NATIVE else None
        self._native_execution = normalize_native_execution_options(
            {
                "native_profile": native_profile,
                "profile": profile,
                "max_inflight": max_inflight,
                "batch_size": batch_size,
                "span_rows": span_rows,
                "span_bytes": span_bytes,
                "mode": mode,
                "cache_strategy": cache_strategy,
                "cancel_after": cancel_after,
            },
            strict=False,
        )
        for _key, _value in self._native_execution.items():
            setattr(self, _key, _value)
        # Keep the legacy default incremental, but let an explicitly selected
        # native backfill mode derive the matching ``False`` flag.  Passing
        # the old unconditional ``True`` here made this direct compatibility
        # thread discard ``mode=historical-backfill`` and the bridge rejected
        # the contradictory request.
        self.incrementally = not (
            self.source == QMT_NATIVE
            and self.mode == "historical-backfill"
        )
        self._is_running = True
        self._cancel_timer = None

    def stop(self):
        self._is_running = False
        timer = getattr(self, "_cancel_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        # The direct/native compatibility path may be blocked in wait_job;
        # request cancellation before the loop observes the flag.  MiniQMT's
        # historical stop semantics remain unchanged.
        service = getattr(self, "_native_service", None)
        if service is not None:
            adapter = getattr(service, "adapter", service)
            _cancel_history_runner(adapter, timeout=2.0)

    def _run_native(self):
        """QThread 直连原生桥的兼容路径（旧单线程导入入口）。"""
        from duckdb_storage.history_adapters import HistoryImportService
        import pandas as pd

        results = {
            'success': 0,
            'failed': 0,
            'empty': 0,
            'total_records': 0,
            # Native bundle diagnostics are deliberately additive so old GUI
            # consumers that only read the four historical counters remain
            # compatible.  ``bundle_gaps`` contains only bounded metadata.
            'bundle_incomplete': 0,
            'bundle_gaps': [],
        }
        # 传入显式 bridge_dir 与重试参数；该服务只在任务线程中实例化，
        # 不会在对话框启动时创建 request 文件或连接客户端。
        retry_options = {
            "max_attempts": self.max_task_retries + 1,
        }
        # RetryPolicy 是指数退避模型，使用首个间隔作为基准；多进程导入
        # 仍会使用精确的 30/120/300 秒序列。
        if self.retry_backoff:
            retry_options["base_delay_seconds"] = self.retry_backoff[0]
            if len(self.retry_backoff) > 1 and self.retry_backoff[0] > 0:
                retry_options["backoff_factor"] = self.retry_backoff[1] / self.retry_backoff[0]
        service_options = {"retry": retry_options}
        if self.bridge_dir:
            service_options["bridge_dir"] = self.bridge_dir
        service = HistoryImportService(self.source, **service_options)
        self._native_service = service

        # ``cancel_after`` is a GUI safety valve.  Use a daemon timer so a
        # blocked bridge wait is interrupted even when the QThread event loop
        # cannot process a queued stop signal.
        if self.source == QMT_NATIVE and self.cancel_after is not None:
            import threading

            def _deadline_cancel():
                self._is_running = False
                adapter = getattr(service, "adapter", service)
                _cancel_history_runner(adapter, timeout=2.0)

            self._cancel_timer = threading.Timer(float(self.cancel_after), _deadline_cancel)
            self._cancel_timer.daemon = True
            self._cancel_timer.start()

        pending_native_ack_groups = {}

        def _invoke_ack(method, first_arg):
            try:
                import inspect
                signature = inspect.signature(method)
            except (TypeError, ValueError):
                signature = None
            accepts_kwargs = bool(
                signature is not None
                and any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )
            ack_kwargs = {
                "remove_results": True,
                "instance_generation": self.instance_generation or None,
            }
            if signature is not None and not accepts_kwargs:
                ack_kwargs = {
                    name: value
                    for name, value in ack_kwargs.items()
                    if name in signature.parameters
                }
                # Bind before the sole invocation.  Never retry a body
                # TypeError after a potentially destructive ACK.
                signature.bind(first_arg, **ack_kwargs)
            return method(first_arg, **ack_kwargs)

        def _ack_bundle_result(bundle_value):
            """ACK only complete bridge views after DuckDB accepts the rows.

            ``NativeBundleResult.job_ids`` is an identity/diagnostic surface,
            not a completion proof.  In particular, a bundle may contain
            verified rows for one adjustment while another view is partial or
            still waiting for a retry.  Reusing the strict selector shared by
            the manager and CLI keeps the GUI from deleting those recoverable
            result files.  Import the helper lazily so the viewer remains
            usable in frozen/legacy installations where ``khDataSource`` is
            not importable; in that case fail closed and let TTL cleanup do
            the eventual housekeeping.
            """
            try:
                from khDataSource import _native_successful_job_ids

                ids = _native_successful_job_ids(bundle_value)
            except Exception as ack_gate_error:
                self.status.emit(
                    f"⚠ 原生桥结果未通过 ACK 完整性检查: {ack_gate_error}"
                )
                return
            adapter = getattr(service, "adapter", None)
            client = getattr(adapter, "client", None)
            if client is None and adapter is not None:
                try:
                    client = adapter._client()
                except Exception:
                    client = None
            acknowledge = getattr(client, "acknowledge_job", None)
            acknowledge_many = getattr(client, "acknowledge_jobs", None)
            if not callable(acknowledge) and not callable(acknowledge_many):
                return
            unique_ids = list(dict.fromkeys(
                str(value) for value in ids if str(value).strip()
            ))
            if callable(acknowledge_many) and unique_ids:
                group = pending_native_ack_groups.setdefault(
                    id(client), {"client": client, "job_ids": []}
                )
                for job_id in unique_ids:
                    if job_id not in group["job_ids"]:
                        group["job_ids"].append(job_id)
                if len(group["job_ids"]) >= 256:
                    _flush_pending_native_acks()
                return
            if not callable(acknowledge):
                return
            for job_id in unique_ids:
                try:
                    response = _invoke_ack(acknowledge, job_id)
                    if response is False or (
                        isinstance(response, Mapping) and response.get("ok") is False
                    ):
                        raise RuntimeError("native ACK 被桥接端拒绝")
                except Exception as ack_error:
                    self.status.emit(f"⚠ 原生桥任务 {job_id} ACK 清理失败: {ack_error}")

        def _flush_pending_native_acks():
            groups = list(pending_native_ack_groups.values())
            pending_native_ack_groups.clear()
            for group in groups:
                client = group["client"]
                job_ids = list(group["job_ids"])
                acknowledge_many = getattr(client, "acknowledge_jobs", None)
                if not callable(acknowledge_many) or not job_ids:
                    continue
                try:
                    response = _invoke_ack(acknowledge_many, job_ids)
                    outcomes = response.get("jobs") if isinstance(response, Mapping) else None
                    if isinstance(outcomes, (list, tuple)):
                        by_id = {
                            str(item.get("job_id") or "").strip(): item
                            for item in outcomes
                            if isinstance(item, Mapping) and item.get("job_id")
                        }
                        for job_id in job_ids:
                            item = by_id.get(job_id)
                            if item is None or item.get("ok") is False:
                                detail = item or {}
                                reason = (
                                    detail.get("error_message")
                                    or detail.get("error")
                                    or detail.get("error_code")
                                    or "未返回结果"
                                )
                                self.status.emit(
                                    f"⚠ 原生桥任务 {job_id} ACK 清理失败: {reason}"
                                )
                    elif response is False or (
                        isinstance(response, Mapping) and response.get("ok") is False
                    ):
                        self.status.emit("⚠ 原生桥批量 ACK 被桥接端拒绝")
                except Exception as ack_error:
                    # Do not replay a possibly partially destructive call.
                    self.status.emit(f"⚠ 原生桥批量 ACK 清理失败: {ack_error}")

        total_tasks = len(self.stocks) * len(self.periods)
        completed = 0
        for period in self.periods:
            for stock in self.stocks:
                if not self._is_running:
                    self.status.emit("导入已取消")
                    self.finished.emit({**results, 'cancelled': True})
                    service.close()
                    self._native_service = None
                    return
                try:
                    period_value = str(getattr(period, "value", period)).strip().lower()
                    # Tick has no adjustment semantics.  The service enforces
                    # that contract strictly, so strip any bar selections
                    # before constructing the multi-adjustment request when a
                    # GUI task mixes tick and bar periods.
                    requested = [] if period_value == "tick" else (self.dividend_types or [])
                    if period_value == "tick":
                        # 原生桥 tick 只能读取近一个月且单次最多 31 个
                        # 自然日；旧入口也必须套用同一前门禁，不能因绕过
                        # Full/Custom 扫描而把超限请求送进 worker。
                        ranges = _tick_safe_date_ranges(
                            self.start_date,
                            self.end_date,
                        )
                        if not ranges:
                            self.status.emit(f"○ {stock} {period} 日期范围无效，跳过")
                            results['empty'] += 1
                            completed += 1
                            self.progress.emit(
                                int(completed * 100 / max(1, total_tasks)),
                                completed,
                                total_tasks,
                            )
                            continue
                    else:
                        ranges = ((self.start_date, self.end_date),)

                    # 每个安全分段独立拼接复权列，再按行拼接，避免把第一个
                    # 分段作为 left frame 导致后续分段被静默丢弃。
                    frame_chunks = []
                    fetched_bundles = []
                    strict_bundle_incomplete = False
                    task_bundle_incomplete = False
                    for range_start, range_end in ranges:
                        # All native entry points use the same one-download /
                        # multi-view coordinator.  The former direct
                        # ``service.fetch`` call submitted one job per
                        # adjustment and could be 5x slower (and race on the
                        # QMT cache).  Pure long-range mode deliberately has
                        # no legacy per-view fallback.
                        fetched = service.fetch_bundle(
                            [stock], period_value, range_start, range_end,
                            adjustments=['none'] + list(requested),
                            incrementally=self.incrementally,
                            force=self.force,
                            allow_gaps=self.allow_gaps,
                            idempotent=self.idempotent,
                            local_only=self.local_only,
                            max_attempts=(
                                self.max_attempts
                                if self.max_attempts is not None
                                else DEFAULT_NATIVE_MAX_ATTEMPTS
                            ),
                            instance_generation=self.instance_generation,
                            profile=self.profile,
                            mode=(
                                self.mode
                                if self.mode in ("tail-sync", "historical-backfill")
                                else (
                                    "tail-sync"
                                    if self.incrementally is not False
                                    else "historical-backfill"
                                )
                            ),
                            native_profile=self.native_profile,
                            max_inflight=self.max_inflight,
                            batch_size=self.batch_size,
                            span_rows=self.span_rows,
                            span_bytes=self.span_bytes,
                            cache_strategy=self.cache_strategy,
                        )
                        fetched_bundles.append(fetched)
                        bundle_complete, bundle_diagnostics = _native_bundle_completion_status(
                            fetched
                        )
                        if not bundle_complete:
                            task_bundle_incomplete = True
                            diagnostics = dict(bundle_diagnostics or {})
                            if diagnostics.get("gaps"):
                                # Keep the result payload bounded even when a
                                # compatibility producer sends a huge gap
                                # list.  Each item is already metadata-only.
                                for gap in diagnostics["gaps"][:16]:
                                    if gap not in results['bundle_gaps']:
                                        results['bundle_gaps'].append(gap)
                            reason = str(
                                diagnostics.get("reason")
                                or diagnostics.get("state")
                                or "聚合状态未完成"
                            )[:128]
                            if not self.allow_gaps:
                                # Strict imports must not write rows from a
                                # partial outer bundle.  Keep the fetched
                                # bundle in memory for diagnostics, but skip
                                # decoding/ACKing it and stop requesting later
                                # ranges for this stock-period.
                                strict_bundle_incomplete = True
                                self.status.emit(
                                    f"✗ {stock} {period_value} {range_start}~{range_end} "
                                    f"原生桥 bundle 不完整，拒绝写入 ({reason})"
                                )
                                break
                            self.status.emit(
                                f"⚠ {stock} {period_value} {range_start}~{range_end} "
                                f"bundle 部分完成，仅写入已验证行并保留缺口 ({reason})"
                            )
                        self.status.emit(
                            f"原生桥阶段完成: {stock} {period_value} {range_start}~{range_end}"
                        )
                        chunk = pd.DataFrame()
                        # Decode both the new NativeBundleResult and the
                        # compact mapping returned by a compatibility bridge.
                        bundle_item = None
                        by_code = getattr(fetched, "by_code", None)
                        if by_code is None and isinstance(fetched, Mapping):
                            by_code = fetched.get("by_code")
                        if isinstance(by_code, Mapping):
                            bundle_item = by_code.get(stock)
                            if bundle_item is None:
                                bundle_item = next(
                                    (value for key, value in by_code.items()
                                     if str(key).strip().upper() == str(stock).strip().upper()),
                                    None,
                                )
                        values = None
                        if isinstance(bundle_item, Mapping):
                            values = bundle_item.get("results")
                            if not isinstance(values, Mapping):
                                values = {
                                    name: bundle_item.get(name)
                                    for name in ("none", *list(requested))
                                    if bundle_item.get(name) is not None
                                }
                        if not isinstance(values, Mapping):
                            # Very old service implementations may still
                            # return a list of HistoryResult objects.  Keep a
                            # read-only decoder for that shape; no new request
                            # is issued here.
                            values = {}
                            old_items = fetched if isinstance(fetched, list) else [fetched]
                            for item in old_items:
                                if not getattr(item, "ok", False):
                                    continue
                                name = str(getattr(item, "adjustment", "none") or "none")
                                values[name] = getattr(item, "rows", None)

                        for adjustment_name in ("none", *list(requested)):
                            value = values.get(adjustment_name) if isinstance(values, Mapping) else None
                            if value is None:
                                continue
                            if isinstance(value, pd.DataFrame):
                                part = value.copy()
                            else:
                                try:
                                    part = pd.DataFrame(value)
                                except Exception:
                                    continue
                            part = part.drop(columns=['code', 'stock_code'], errors='ignore')
                            if adjustment_name != "none":
                                part = part.rename(columns={
                                    field: f"{field}_{adjustment_name}"
                                    for field in ("open", "high", "low", "close")
                                    if field in part.columns
                                })
                            if part.empty:
                                continue
                            if chunk.empty:
                                chunk = part
                            elif 'time' in chunk.columns and 'time' in part.columns:
                                chunk = chunk.merge(
                                    part, on='time', how='left',
                                    suffixes=('', '_dup'),
                                )
                                chunk = chunk.drop(
                                    columns=[c for c in chunk.columns if c.endswith('_dup')],
                                    errors='ignore',
                                )
                        if not chunk.empty:
                            frame_chunks.append(chunk)
                    if strict_bundle_incomplete:
                        results['failed'] += 1
                        results['bundle_incomplete'] += 1
                        completed += 1
                        self.progress.emit(
                            int(completed * 100 / max(1, total_tasks)),
                            completed,
                            total_tasks,
                        )
                        continue
                    if task_bundle_incomplete:
                        results['bundle_incomplete'] += 1
                    frame = (
                        pd.concat(frame_chunks, ignore_index=True)
                        if frame_chunks else pd.DataFrame()
                    )
                    if frame.empty:
                        # An empty decode is not a durable DuckDB commit.  Do
                        # not ACK/clean result files here: a transient decode
                        # or a partial bundle must remain recoverable via TTL.
                        results['empty'] += 1
                        self.status.emit(f"○ {stock} {period} 无数据")
                    else:
                        # 使用规范化周期；调用方可能传入 Period.TICK 枚举，
                        # manager 的 tick 分派只识别其 wire value。
                        if period_value == "tick":
                            _validate_tick_frame_retention(frame)
                            # Tick 有独立的写入契约（唯一时间戳、保留窗和
                            # 增量追加）。优先调用显式接口；旧的 manager
                            # 替身没有该方法时才回退到兼容的总入口。
                            save_tick = getattr(self.manager, "save_tick_data", None)
                            if callable(save_tick):
                                saved = save_tick(
                                    frame,
                                    stock,
                                    append_missing_only=(
                                        bool(self.incrementally) and not bool(self.force)
                                    ),
                                )
                            else:
                                saved = self.manager.save_kline_data(
                                    frame, stock, period_value, 'none'
                                )
                        else:
                            saved = self.manager.save_kline_data(
                                frame, stock, period_value, 'none'
                            )
                        # Validate the write return before acknowledging.  A
                        # malformed/None return must not be interpreted as a
                        # committed transaction and must leave IPC artifacts
                        # available for repair.  A zero count is not enough
                        # evidence to ACK: custom writers may have filtered
                        # every row or returned a physical no-op.  The bundled
                        # writer reports its logical input count for
                        # skip-unchanged imports, so supported idempotent
                        # imports still cross the positive-count fence below.
                        try:
                            saved_count = int(saved)
                        except (TypeError, ValueError, OverflowError) as exc:
                            raise RuntimeError(
                                "DuckDB 写入未返回有效记录数，保留原生桥结果"
                            ) from exc
                        if saved_count < 0:
                            raise RuntimeError(
                                "DuckDB 写入返回负记录数，保留原生桥结果"
                            )
                        # Only acknowledge after the write call returns a
                        # positive durable count.  A zero count can mean that
                        # a custom writer filtered every row (or returned a
                        # physical no-op); it is not proof that this native
                        # result was safely consumed.  Keep the bridge/IPC
                        # artifacts for retry/TTL recovery in that case.
                        if saved_count > 0:
                            for bundle_value in fetched_bundles:
                                _ack_bundle_result(bundle_value)
                        if task_bundle_incomplete:
                            results.setdefault('partial', 0)
                            results['partial'] += 1
                        if saved_count > 0:
                            results['success'] += 1
                            results['total_records'] += saved_count
                            self.status.emit(f"✓ {stock} {period} 导入 {saved_count} 条数据")
                        else:
                            results['empty'] += 1
                except Exception as exc:
                    results['failed'] += 1
                    self.status.emit(f"✗ {stock} {period} 失败: {exc}")
                completed += 1
                self.progress.emit(int(completed * 100 / max(1, total_tasks)), completed, total_tasks)
        _flush_pending_native_acks()
        timer = getattr(self, "_cancel_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
            self._cancel_timer = None
        service.close()
        self._native_service = None
        self.finished.emit(results)

    def run(self):
        # 在方法开头导入 datetime，避免作用域问题
        from datetime import datetime, timedelta

        results = {'success': 0, 'failed': 0, 'empty': 0, 'total_records': 0}

        if self.source == QMT_NATIVE:
            try:
                self._run_native()
            except Exception as exc:
                self.error.emit(f"原生 QMT 导入异常: {exc}")
            finally:
                timer = getattr(self, "_cancel_timer", None)
                if timer is not None:
                    try:
                        timer.cancel()
                    except Exception:
                        pass
                    self._cancel_timer = None
                service = getattr(self, "_native_service", None)
                if service is not None:
                    try:
                        service.close()
                    except Exception:
                        pass
                    self._native_service = None
            return

        try:
            # 尝试导入xtquant
            from xtquant import xtdata
        except ImportError:
            self.error.emit("无法导入xtquant模块，请确保已安装MiniQMT")
            return

        # ========== 新增：检查并下载 000300.SH 数据 ==========
        try:
            self.status.emit("检查基准指数 000300.SH 数据...")

            # 检查是否已有 000300.SH 数据
            benchmark_code = '000300.SH'
            has_benchmark = False

            try:
                # 查询数据库中是否有 000300.SH 的数据
                stocks = self.manager.get_available_stocks()
                has_benchmark = benchmark_code in stocks  # 修复：stocks 是字符串列表
                self.status.emit(f"当前数据库中有 {len(stocks)} 只股票")
            except Exception as e:
                self.status.emit(f"查询数据库失败: {e}")
                import traceback
                traceback.print_exc()

            if not has_benchmark:
                self.status.emit("未找到基准指数数据，正在下载 000300.SH 近20年日线数据...")

                # 计算20年前的日期
                end_date = datetime.now()
                start_date = end_date - timedelta(days=365*20)

                start_str = start_date.strftime("%Y%m%d")
                end_str = end_date.strftime("%Y%m%d")

                # 下载数据
                xtdata.download_history_data(
                    benchmark_code,
                    period='1d',
                    start_time=start_str,
                    end_time=end_str,
                    incrementally=True
                )

                # 获取不复权数据
                data_none = xtdata.get_local_data(
                    field_list=[],
                    stock_list=[benchmark_code],
                    period='1d',
                    start_time=start_str,
                    end_time=end_str,
                    dividend_type='none',
                    fill_data=True
                )

                if benchmark_code in data_none and data_none[benchmark_code] is not None and len(data_none[benchmark_code]) > 0:
                    df = data_none[benchmark_code].copy()

                    dividend_types = self.dividend_types
                    if dividend_types is None:
                        dividend_types = ['front', 'back', 'front_ratio', 'back_ratio']

                    for div_type in dividend_types:
                        try:
                            data_adj = xtdata.get_local_data(
                                field_list=['time', 'open', 'high', 'low', 'close'],
                                stock_list=[benchmark_code],
                                period='1d',
                                start_time=start_str,
                                end_time=end_str,
                                dividend_type=div_type,
                                fill_data=True
                            )

                            if benchmark_code in data_adj and data_adj[benchmark_code] is not None:
                                df_adj = data_adj[benchmark_code]

                                # 重命名复权字段
                                suffix = div_type
                                rename_map = {
                                    'open': f'open_{suffix}',
                                    'high': f'high_{suffix}',
                                    'low': f'low_{suffix}',
                                    'close': f'close_{suffix}'
                                }
                                df_adj = df_adj.rename(columns=rename_map)
                                adj_columns = list(rename_map.values())
                                if df_adj.empty:
                                    df_adj = pd.DataFrame(index=df.index, columns=adj_columns)
                                else:
                                    df_adj = df_adj[adj_columns]

                                # 合并到主 DataFrame
                                df = df.merge(df_adj, left_index=True, right_index=True, how='left')

                                del data_adj, df_adj
                        except Exception as e:
                            self.status.emit(f"获取 000300.SH {div_type} 复权数据失败: {e}")

                    # 保存到DuckDB
                    records = self.manager.save_kline_data(df, benchmark_code, '1d', 'none')
                    self.status.emit(f"已成功下载并保存 000300.SH 数据，共 {records} 条记录")

                    del df, data_none
                else:
                    self.status.emit("警告：000300.SH 数据下载失败，请手动补充")
            else:
                self.status.emit("基准指数 000300.SH 数据已存在")

        except Exception as e:
            self.status.emit(f"检查/下载 000300.SH 数据时出错: {e}")
            import traceback
            traceback.print_exc()
        # ========== 基准指数检查结束 ==========

        total_stocks = len(self.stocks)
        total_tasks = total_stocks * len(self.periods)  # 总任务数 = 股票数 × 周期数
        completed_tasks = 0

        # 遍历每个周期
        for period in self.periods:
            if not self._is_running:
                self.status.emit("导入已取消")
                break

            self.status.emit(f"开始导入 {period} 周期数据...")

            # 根据周期选择字段列表
            if period == 'tick':
                field_list = self.TICK_FIELDS
            else:
                field_list = self.KLINE_FIELDS

            # 遍历每只股票
            for i, stock in enumerate(self.stocks):
                if not self._is_running:
                    self.status.emit("导入已取消")
                    break

                try:
                    period_value = str(getattr(period, "value", period)).strip().lower()
                    if period_value == "tick":
                        # 旧的单线程入口同样遵守 tick 近一个月保留窗；
                        # 按自然日拆分后再拼接，避免一次请求超出原生桥上限。
                        tick_ranges = _tick_safe_date_ranges(
                            self.start_date,
                            self.end_date,
                        )
                        if not tick_ranges:
                            results['empty'] += 1
                            self.status.emit(f"○ {stock} tick 日期范围无效，跳过")
                        else:
                            tick_frames = []
                            for tick_start, tick_end in tick_ranges:
                                xtdata.download_history_data(
                                    stock,
                                    period='tick',
                                    start_time=tick_start,
                                    end_time=tick_end,
                                    incrementally=True,
                                )
                                tick_data = xtdata.get_local_data(
                                    field_list=[],
                                    stock_list=[stock],
                                    period='tick',
                                    start_time=tick_start,
                                    end_time=tick_end,
                                    dividend_type='none',
                                    fill_data=True,
                                )
                                part = tick_data.get(stock) if isinstance(tick_data, dict) else None
                                if part is not None and len(part) > 0:
                                    tick_frames.append(part.copy())
                            df_tick = (
                                pd.concat(tick_frames, ignore_index=True)
                                if tick_frames else pd.DataFrame()
                            )
                            if df_tick.empty:
                                results['empty'] += 1
                                self.status.emit(f"○ {stock} tick 无数据")
                            else:
                                _validate_tick_frame_retention(df_tick)
                                records = self.manager.save_tick_data(
                                    df_tick,
                                    stock,
                                )
                                if records > 0:
                                    results['success'] += 1
                                    results['total_records'] += records
                                    self.status.emit(f"✓ {stock} tick 导入 {records} 条数据")
                                else:
                                    results['empty'] += 1
                                    self.status.emit(f"○ {stock} tick 无新数据")
                            del df_tick
                        self.manager.cleanup_connections_aggressive()
                        completed_tasks += 1
                        self.progress.emit(
                            int(completed_tasks * 100 / max(1, total_tasks)),
                            completed_tasks,
                            total_tasks,
                        )
                        continue

                    # 下载数据到本地
                    xtdata.download_history_data(
                        stock,
                        period=period_value,
                        start_time=self.start_date,
                        end_time=self.end_date,
                        incrementally=True
                    )

                    # 获取不复权数据（基础数据）
                    data_none = xtdata.get_local_data(
                        field_list=[],  # 空列表表示获取所有字段
                        stock_list=[stock],
                        period=period_value,
                        start_time=self.start_date,
                        end_time=self.end_date,
                        dividend_type='none',
                        fill_data=True
                    )

                    if stock in data_none and data_none[stock] is not None and len(data_none[stock]) > 0:
                        df = data_none[stock].copy()

                        dividend_types = self.dividend_types
                        if dividend_types is None:
                            dividend_types = ['front', 'back', 'front_ratio', 'back_ratio']

                        for div_type in dividend_types:
                            try:
                                data_adj = xtdata.get_local_data(
                                    field_list=['time', 'open', 'high', 'low', 'close'],
                                    stock_list=[stock],
                                    period=period_value,
                                    start_time=self.start_date,
                                    end_time=self.end_date,
                                    dividend_type=div_type,
                                    fill_data=True
                                )

                                if stock in data_adj and data_adj[stock] is not None:
                                    df_adj = data_adj[stock]

                                    # 重命名复权字段
                                    suffix = div_type
                                    rename_map = {
                                        'open': f'open_{suffix}',
                                        'high': f'high_{suffix}',
                                        'low': f'low_{suffix}',
                                        'close': f'close_{suffix}'
                                    }
                                    df_adj = df_adj.rename(columns=rename_map)

                                    # 只保留复权字段
                                    adj_columns = list(rename_map.values())
                                    if df_adj.empty:
                                        df_adj = pd.DataFrame(index=df.index, columns=adj_columns)
                                    else:
                                        df_adj = df_adj[adj_columns]

                                    # 合并到主 DataFrame
                                    df = df.merge(df_adj, left_index=True, right_index=True, how='left')

                                    # 释放临时数据
                                    del data_adj, df_adj
                            except Exception as e:
                                # 如果某种复权方式获取失败，跳过
                                self.status.emit(f"获取 {stock} {div_type} 复权数据失败: {e}")

                        # 保存到DuckDB（包含所有复权字段）
                        records = self.manager.save_kline_data(
                            df, stock, period_value, 'none'
                        )

                        if records > 0:
                            results['success'] += 1
                            results['total_records'] += records
                            self.status.emit(f"✓ {stock} {period} 导入 {records} 条数据")
                        else:
                            results['empty'] += 1
                            self.status.emit(f"○ {stock} {period} 无新数据")

                        # 显式释放DataFrame内存
                        del df
                    else:
                        results['empty'] += 1
                        self.status.emit(f"○ {stock} {period} 无数据")

                    # 显式释放data变量内存
                    del data_none

                    # 每只股票处理完后立即清理数据库连接
                    self.manager.cleanup_connections_aggressive()

                except Exception as e:
                    results['failed'] += 1
                    self.status.emit(f"✗ {stock} {period} 失败: {e}")

                completed_tasks += 1
                self.progress.emit(int((completed_tasks / total_tasks) * 100), completed_tasks, total_tasks)

            # 每处理完一个周期后，彻底清理内存
            self.manager.cleanup_connections_aggressive()
            import gc
            gc.collect()

        # 导入完成后，彻底清理所有连接
        self.manager.cleanup_connections_aggressive()
        import gc
        gc.collect()

        self.finished.emit(results)


class ImportThreadMP(QThread):
    """
    多进程数据导入线程

    与 ImportThread 的区别：
    1. 数据下载在独立进程中执行，不会阻塞主线程
    2. 支持任务超时，避免无限等待
    3. 支持强制取消，即使 xtdata API 卡住也能中断
    """
    progress = pyqtSignal(int, int, int)  # (百分比, 已完成数, 总数)
    status = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, manager: DuckDBManager, stocks: List[str], periods: List[str],
                 start_date: str, end_date: str, timeout_per_task: float = 120.0,
                 dividend_types: Optional[List[str]] = None, source: str = MINIQMT,
                 bridge_dir: Optional[str] = None,
                 max_task_retries: int = DEFAULT_HISTORY_MAX_TASK_RETRIES,
                 retry_backoff: Optional[List[float]] = None,
                 instance_generation: Optional[str] = None,
                 native_profile: Optional[str] = None,
                 profile: Optional[str] = None,
                 max_inflight: Optional[int] = None,
                 batch_size: Optional[int] = None,
                 span_rows: Optional[int] = None,
                 span_bytes: Optional[int] = None,
                 mode: Optional[str] = None,
                 cache_strategy: Optional[str] = None,
                 cancel_after: Optional[float] = None):
        super().__init__()
        self.manager = manager
        self.stocks = stocks
        self.periods = periods
        self.start_date = start_date
        self.end_date = end_date
        self.timeout_per_task = timeout_per_task
        self.dividend_types = dividend_types
        self.source = _canonical_history_source(source)
        self.bridge_dir = str(bridge_dir) if bridge_dir else None
        self.max_task_retries, self.retry_backoff = _coerce_retry_settings(
            max_task_retries, retry_backoff
        )
        self.instance_generation = _normalise_generation_aliases(instance_generation)
        self._native_execution = normalize_native_execution_options(
            {
                "native_profile": native_profile,
                "profile": profile,
                "max_inflight": max_inflight,
                "batch_size": batch_size,
                "span_rows": span_rows,
                "span_bytes": span_bytes,
                "mode": mode,
                "cache_strategy": cache_strategy,
                "cancel_after": cancel_after,
            },
            strict=False,
        )
        for _key, _value in self._native_execution.items():
            setattr(self, _key, _value)
        self._is_running = True
        self._importer: Optional[MultiProcessImporter] = None
        self._cancel_timer = None
        # Big QMT may legitimately have a trailing empty trading day while
        # earlier segments are already complete.  Preserve those verified
        # rows in the interactive GUI; MiniQMT keeps the strict legacy policy.
        self.allow_gaps = _default_allow_gaps_for_source(self.source)
        self.workers_requested, self.workers_effective = _effective_history_workers(
            self.source, 4, default=4
        )

    def stop(self):
        """停止导入"""
        self._is_running = False
        if self._importer:
            _cancel_history_runner(self._importer, timeout=2.0)
        timer = getattr(self, "_cancel_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def run(self):
        # 在方法开头导入 datetime，避免作用域问题
        from datetime import datetime, timedelta

        results = {'success': 0, 'failed': 0, 'empty': 0, 'total_records': 0}

        try:
            # 基准指数也必须走可终止子进程；不能在QThread里直接调用可能阻塞的XtData接口。
            benchmark_importer = None
            try:
                self.status.emit("检查基准指数 000300.SH 数据...")
                benchmark_code = '000300.SH'
                available = self.manager.get_available_stocks()
                self.status.emit(f"当前数据库中有 {len(available)} 只股票")
                if benchmark_code in available:
                    self.status.emit("基准指数 000300.SH 数据已存在")
                elif self._is_running:
                    self.status.emit("未找到基准指数数据，正在下载 000300.SH 日线数据...")
                    end_date = datetime.now()
                    if self.source == QMT_NATIVE:
                        start_date = end_date - timedelta(days=3650 - 1)
                    else:
                        start_date = end_date - timedelta(days=365 * 20)
                    benchmark_importer = MultiProcessImporter(
                        num_workers=1,
                        timeout_per_task=self.timeout_per_task,
                        max_task_retries=self.max_task_retries,
                        retry_backoff=self.retry_backoff,
                        source=self.source,
                        bridge_dir=self.bridge_dir,
                    )
                    _configure_history_importer_defaults(
                        benchmark_importer,
                        self.source,
                        instance_generation=self.instance_generation,
**_native_options_for_source(self.source, _native_options_from_owner(self)),
                        cancel_after=getattr(self, "cancel_after", None),
                    )
                    benchmark_importer.start()
                    benchmark_importer.add_task(
                        benchmark_code,
                        '1d',
                        start_date.strftime("%Y%m%d"),
                        end_date.strftime("%Y%m%d"),
                        self.dividend_types,
                    )
                    benchmark_result = None
                    while self._is_running and not benchmark_importer.is_done():
                        benchmark_result = benchmark_importer.get_result(timeout=0.2)
                        for event in benchmark_importer.get_all_progress():
                            if event.get('type') == 'retry':
                                self.status.emit(
                                    f"基准指数连接异常，{event.get('delay', 0):g}秒后进行"
                                    f"第{event.get('attempt')}次重试"
                                )
                        if benchmark_result:
                            break
                    if benchmark_result and benchmark_result.get('success'):
                        df_dict = benchmark_result.get('df_dict')
                        df = dict_to_dataframe(df_dict) if df_dict else pd.DataFrame()
                        if df is not None and not df.empty:
                            records = self.manager.save_kline_data(
                                df, benchmark_code, '1d', 'none'
                            )
                            self.status.emit(
                                f"已成功下载并保存 000300.SH 数据，共 {records} 条记录"
                            )
                        else:
                            self.status.emit("警告：000300.SH 未返回有效数据，请稍后重试")
                    elif self._is_running:
                        self.status.emit(
                            "警告：000300.SH 数据下载失败，请确认MiniQMT连接后重试"
                        )
            except _SkipQmtBenchmark:
                self.status.emit("大QMT原生桥模式：跳过 MiniQMT/xtquant 基准指数检查")
            except Exception as e:
                self.status.emit(f"检查/下载 000300.SH 数据时出错: {e}")
            finally:
                if benchmark_importer is not None:
                    try:
                        benchmark_importer.force_stop()
                    except Exception:
                        pass
            # ========== 基准指数检查结束 ==========

            # 创建多进程导入器。原生桥由 QMT 端单消费者串行服务，
            # 因此把用户请求的 worker 数明确收敛为 1；MiniQMT 仍保持
            # 历史的 4 进程行为。
            # Native tasks may be intentionally prefetched so the one worker
            # can combine many codes into a bridge bundle.  Restarting that
            # worker while descriptors are queued would discard tasks already
            # counted by ``task_index``; its file-backed transport also makes
            # the old MiniQMT cache-reclamation restart unnecessary.
            RESTART_INTERVAL = (
                1_000_000_000 if self.source == QMT_NATIVE else 200
            )
            self._importer = MultiProcessImporter(
                num_workers=self.workers_effective,
                timeout_per_task=self.timeout_per_task,
                max_task_retries=self.max_task_retries,
                retry_backoff=self.retry_backoff,
                source=self.source,
                bridge_dir=self.bridge_dir,
**_native_options_for_source(self.source, _native_options_from_owner(self)),
            )
            _configure_history_importer_defaults(
                self._importer,
                self.source,
                instance_generation=self.instance_generation,
**_native_options_for_source(self.source, _native_options_from_owner(self)),
                cancel_after=getattr(self, "cancel_after", None),
            )
            self._importer.start()
            if self.source == QMT_NATIVE and self.cancel_after is not None:
                import threading

                def _deadline_cancel_importer():
                    self._is_running = False
                    _cancel_history_runner(self._importer, timeout=2.0)

                self._cancel_timer = threading.Timer(
                    float(self.cancel_after), _deadline_cancel_importer
                )
                self._cancel_timer.daemon = True
                self._cancel_timer.start()
            tasks_since_restart = 0

            if self.source == QMT_NATIVE and self.workers_requested != self.workers_effective:
                self.status.emit(
                    f"原生大QMT桥为单消费者：请求 {self.workers_requested} 个进程，"
                    f"实际使用 {self.workers_effective} 个"
                )
            else:
                self.status.emit(
                    f"多进程导入器已启动（{self.workers_effective} 个进程）..."
                )

            # 计算总任务数。tick 先在主线程裁到近一个月并按 31 个自然日
            # 分段，避免把一个超限范围交给 worker/native bridge。
            all_tasks = []
            skipped_tick_units = 0
            for period in self.periods:
                period_value = str(getattr(period, "value", period)).strip().lower()
                for stock in self.stocks:
                    if period_value == "tick":
                        ranges = _tick_safe_date_ranges(
                            self.start_date,
                            self.end_date,
                        )
                        if not ranges:
                            skipped_tick_units += 1
                            self.status.emit(
                                f"○ {stock} tick 日期范围无效，跳过"
                            )
                            continue
                    else:
                        ranges = ((self.start_date, self.end_date),)
                    all_tasks.extend(
                        (stock, period_value, range_start, range_end)
                        for range_start, range_end in ranges
                    )
            total_tasks = len(all_tasks) + skipped_tick_units
            # 被保留窗过滤的逻辑单元视为“明确跳过”，计入进度/空结果，
            # 但绝不提交给 worker。
            completed_count = skipped_tick_units
            if skipped_tick_units:
                results['empty'] += skipped_tick_units
                self.progress.emit(
                    int(completed_count * 100 / max(1, total_tasks)),
                    completed_count,
                    total_tasks,
                )

            # v3.1.6: 改为边添加边处理，而不是先全部添加再处理
            # 这样可以更好地控制内存和支持周期性重启
            task_index = 0

            queue_target = _history_prefetch_limit(
                self.source, len(all_tasks), self.workers_effective
            )

            while (task_index < len(all_tasks) or not self._importer.is_done()) and self._is_running:
                # 添加任务（保持队列中有少量任务，避免堆积）
                while (
                    task_index < len(all_tasks)
                    and self._importer.pending_tasks < queue_target
                ):
                    stock, period, task_start, task_end = all_tasks[task_index]
                    self._importer.add_task(
                        stock, period, task_start, task_end,
                        _task_dividend_types(period, self.dividend_types),
                    )
                    task_index += 1

                # 处理进度信息
                for p in self._importer.get_all_progress():
                    if p['type'] == 'status':
                        self.status.emit(p['msg'])
                    elif p['type'] == 'fatal':
                        self.error.emit(p['msg'])
                        self._importer.force_stop()
                        return
                    elif p['type'] == 'error':
                        self.status.emit(f"下载失败: {p['stock']} {p['period']} - {p['error']}")
                    elif p['type'] == 'retry':
                        self.status.emit(
                            f"↻ {p.get('stock')} {p.get('period')} 连接异常，"
                            f"{p.get('delay', 0):g}秒后进行第{p.get('attempt')}次重试"
                        )
                    elif p['type'] == 'native_phase':
                        self.status.emit(
                            f"原生桥 {p.get('stock', '')} {p.get('period', '')}: "
                            f"{p.get('phase', '处理中')}"
                        )
                    elif p['type'] == 'native_generation':
                        self.status.emit(
                            (
                                "原生桥已重启，自动切换到新实例并继续下载"
                                if p.get('generation_changed') else
                                "原生桥代次已同步，继续下载"
                            )
                        )
                    elif p['type'] == 'executor_started':
                        self.status.emit(
                            f"下载工作进程实际并发: {p.get('workers_effective', self.workers_effective)}"
                        )

                # 获取下载结果
                result = self._importer.get_result(timeout=0.2)

                if result:
                    completed_count += 1
                    tasks_since_restart += 1

                    # Keep a second gate at the GUI write boundary.  A
                    # frozen/third-party worker can return ``success=True``
                    # with an aggregate ``partial`` marker even though the
                    # bundled worker now rejects that shape before decode.
                    # Never persist it unless the caller explicitly opted
                    # into ``allow_gaps``.
                    if (
                        self.source == QMT_NATIVE
                        and result.get('success')
                        and not bool(getattr(self, 'allow_gaps', False))
                        and not bool(_native_result_completion_gate(result))
                    ):
                        results['failed'] += 1
                        self.status.emit(
                            f"✗ {result.get('stock', '')} {result.get('period', '')} "
                            "原生桥 bundle 未完成或含缺口，拒绝写入"
                        )
                        _finalize_native_worker_result(
                            result,
                            runner=self._importer,
                            committed=False,
                            status_callback=self.status.emit,
                        )
                        progress_pct = int((completed_count / total_tasks) * 100)
                        self.progress.emit(progress_pct, completed_count, total_tasks)
                        continue

                    if result['success']:
                        df_dict = result.get('df_dict')
                        stock = result['stock']
                        period = result['period']
                        records = result.get('records', 0)

                        if df_dict is not None and records > 0:
                            try:
                                # 将字典转换回 DataFrame
                                df = dict_to_dataframe(df_dict)

                                # 保存到 DuckDB。多进程 worker/旧客户端可能
                                # 忽略请求边界，因此在最终写入点再做一次 tick
                                # 保留窗校验，避免把不可再补的历史行落库。
                                period_value = str(
                                    getattr(period, "value", period)
                                ).strip().lower()
                                if period_value == "tick":
                                    _validate_tick_frame_retention(df)
                                    save_tick = getattr(
                                        self.manager, "save_tick_data", None
                                    )
                                    if callable(save_tick):
                                        importer = getattr(self, "_importer", None)
                                        force_tick = bool(
                                            getattr(importer, "default_force", False)
                                        )
                                        saved_records = save_tick(
                                            df,
                                            stock,
                                            append_missing_only=(
                                                self.source == QMT_NATIVE and not force_tick
                                            ),
                                        )
                                    else:
                                        saved_records = self.manager.save_kline_data(
                                            df, stock, period_value, 'none'
                                        )
                                else:
                                    saved_records = self.manager.save_kline_data(
                                        df, stock, period_value, 'none'
                                    )

                                if saved_records > 0:
                                    results['success'] += 1
                                    results['total_records'] += saved_records
                                    self.status.emit(f"✓ {stock} {period} 导入 {saved_records} 条数据")
                                else:
                                    results['empty'] += 1
                                    self.status.emit(f"○ {stock} {period} 无新数据")

                                # The frame has been consumed (a zero-row
                                # save is also a deliberate terminal consume),
                                # so release the native bridge job and the
                                # file-backed IPC payload now.  Failed writes
                                # intentionally skip this hook for recovery.
                                if self.source == QMT_NATIVE:
                                    _finalize_native_worker_result(
                                        result,
                                        frame=df,
                                        runner=self._importer,
                                        # A zero/invalid write count is not a
                                        # durable import; leave native/IPC
                                        # artifacts for recovery.
                                        committed=bool(saved_records > 0),
                                        status_callback=self.status.emit,
                                    )
                                del df
                                del df_dict
                            except Exception as e:
                                quality_code = str(
                                    getattr(e, "code", "") or ""
                                ).upper()
                                if quality_code in {
                                    "DUPLICATE_TIMESTAMP",
                                    "DATA_QUALITY",
                                    "OUT_OF_RETENTION",
                                }:
                                    results.setdefault("quality_errors", []).append({
                                        "stock": stock,
                                        "period": str(
                                            getattr(period, "value", period)
                                        ).strip().lower(),
                                        "code": quality_code,
                                        "error": str(e),
                                    })
                                    results["skipped"] = int(
                                        results.get("skipped", 0) or 0
                                    ) + 1
                                    self.status.emit(
                                        f"⚠ {stock} {str(getattr(period, 'value', period)).strip().lower()} 数据质量拒绝: {e}"
                                    )
                                else:
                                    results['failed'] += 1
                                    self.status.emit(f"✗ {stock} {period} 保存失败: {e}")
                        else:
                            results['empty'] += 1
                            is_legal_empty = _is_native_legal_empty(result, self.source)
                            if is_legal_empty:
                                self.status.emit(f"○ {result['stock']} {result['period']} 无数据 (历史停牌/无交易)")
                            else:
                                self.status.emit(f"○ {result['stock']} {result['period']} 无数据")
                            if self.source == QMT_NATIVE:
                                _finalize_native_worker_result(
                                    result,
                                    committed=bool(is_legal_empty),
                                    runner=self._importer,
                                    status_callback=self.status.emit,
                                )
                    else:
                        results['failed'] += 1
                        self.status.emit(f"✗ {result['stock']} {result['period']} 下载失败: {result.get('error', '未知错误')}")

                    # 更新进度 - 每完成一只就刷新
                    progress_pct = int((completed_count / total_tasks) * 100)
                    self.progress.emit(progress_pct, completed_count, total_tasks)

                    # v3.1.6: 减少清理频率，从每5个改为每20个
                    if completed_count % 20 == 0:
                        self.manager.cleanup_connections_aggressive()
                        import gc
                        gc.collect()

                    # v3.1.6: 周期性重启工作进程，释放 xtdata 内部缓存
                    if tasks_since_restart >= RESTART_INTERVAL and self._is_running:
                        self.status.emit(f"[内存优化] 已处理 {tasks_since_restart} 个任务，重启工作进程...")
                        self._importer.stop()
                        import gc
                        gc.collect()
                        self._importer = MultiProcessImporter(
                            num_workers=self.workers_effective,
                            timeout_per_task=self.timeout_per_task,
                            max_task_retries=self.max_task_retries,
                            retry_backoff=self.retry_backoff,
                            source=self.source,
                            bridge_dir=self.bridge_dir,
**_native_options_for_source(self.source, _native_options_from_owner(self)),
                        )
                        _configure_history_importer_defaults(
                            self._importer,
                            self.source,
                            instance_generation=self.instance_generation,
**_native_options_for_source(self.source, _native_options_from_owner(self)),
                            cancel_after=getattr(self, "cancel_after", None),
                        )
                        self._importer.start()
                        tasks_since_restart = 0

                # 让 Qt 事件循环有机会处理
                QThread.msleep(10)  # 减少等待时间，提高响应速度

            # 导入完成后清理
            self._importer.stop()
            self.manager.cleanup_connections_aggressive()
            import gc
            gc.collect()

            if self._is_running:
                self.finished.emit(results)
            else:
                self.status.emit("导入已取消")

        except Exception as e:
            import traceback
            self.error.emit(f"导入异常: {e}\n{traceback.format_exc()}")
        finally:
            timer = getattr(self, "_cancel_timer", None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
                self._cancel_timer = None
            if self._importer:
                self._importer.force_stop()


def fit_dialog_height_to_content(dialog, tab_widget=None, max_height=1200):
    """把对话框高度撑到当前页内容所需高度（受屏幕可用高度限制）。

    MiniQMT / BaoStock 两个导入对话框把"开始/停止/关闭"按钮放在可滚动页里，
    窗口按 800x600 的最小尺寸打开时按钮落在可视区之外，用户在默认窗口里
    找不到开始按钮。这里在窗口显示后按内容需要的高度撑一次；屏幕放不下时
    仍然可以滚动，不会把窗口顶出屏幕。
    """
    try:
        page = tab_widget.currentWidget() if tab_widget is not None else None
        scroll = page if isinstance(page, QScrollArea) else None
        if scroll is None and page is not None:
            scroll = page.findChild(QScrollArea)
        if scroll is None or scroll.widget() is None:
            return
        # 视口之外的部分（状态栏、标签栏、边距）维持原样，只补内容差额
        deficit = scroll.widget().sizeHint().height() - scroll.viewport().height()
        if deficit <= 0:
            return
        available = QApplication.desktop().availableGeometry(dialog)
        target = min(dialog.height() + deficit + 8, max_height, available.height() - 60)
        if target <= dialog.height():
            return
        dialog.resize(dialog.width(), int(target))
        frame = dialog.frameGeometry()
        if frame.bottom() > available.bottom():
            dialog.move(dialog.x(), max(available.top(), available.bottom() - frame.height()))
    except Exception:
        pass


class MiniQMTImportDialog(QDialog):
    """MiniQMT数据导入对话框 - 包含常规导入和全量增量补充功能"""

    # 进度文件名（用于断点续传）
    PROGRESS_FILE = 'full_increment_progress.json'
    BAOSTOCK_DAILY_INDICATORS = (
        'turn', 'pctChg', 'peTTM', 'psTTM', 'pcfNcfTTM', 'pbMRQ', 'isST'
    )

    def __init__(self, manager: DuckDBManager, parent=None,
                 bridge_dir: Optional[str] = None,
                 max_task_retries: Optional[int] = None,
                 retry_backoff: Optional[List[float]] = None,
                 history_import_source: object = None,
                 source: object = None,
                 native_profile: Optional[str] = None,
                 profile: Optional[str] = None,
                 max_inflight: Optional[int] = None,
                 batch_size: Optional[int] = None,
                 span_rows: Optional[int] = None,
                 span_bytes: Optional[int] = None,
                 mode: Optional[str] = None,
                 cache_strategy: Optional[str] = None,
                 cancel_after: Optional[float] = None,
                 allow_native_source: bool = False):
        super().__init__(parent)
        self.manager = manager
        self.allow_native_source = bool(allow_native_source)
        self.history_settings = QSettings('KHQuant', 'HistoryImport')
        scheduled_settings = QSettings('KHQuant', 'ScheduledDataSync')
        raw_global_settings = {}
        try:
            import kh_settings as _kh_settings

            raw_global_settings = _kh_settings._read_raw_settings()
            if not isinstance(raw_global_settings, dict):
                raw_global_settings = {}
        except Exception:
            raw_global_settings = {}

        explicit_source = history_import_source if history_import_source is not None else source
        configured_source = explicit_source
        if configured_source is None:
            configured_source = self.history_settings.value(HISTORY_IMPORT_SOURCE_KEY, None)
        if configured_source is None or not str(configured_source).strip():
            configured_source = raw_global_settings.get(HISTORY_IMPORT_SOURCE_KEY, MINIQMT)
        self._history_source_config_error = ""
        try:
            self.history_source = _canonical_history_source(configured_source)
        except Exception as exc:
            # legacy/unknown 配置必须留在界面上等待用户明确选择，不能悄悄
            # 改回 MiniQMT 后继续执行。
            self.history_source = str(configured_source or "").strip().lower()
            self._history_source_config_error = str(exc)
        if not self.allow_native_source:
            self.history_source = MINIQMT
            self._history_source_config_error = ""

        configured_bridge_dir = bridge_dir
        if configured_bridge_dir is None or not str(configured_bridge_dir).strip():
            configured_bridge_dir = self.history_settings.value(HISTORY_BRIDGE_DIR_KEY, None)
        if configured_bridge_dir is None or not str(configured_bridge_dir).strip():
            configured_bridge_dir = scheduled_settings.value(HISTORY_BRIDGE_DIR_KEY, None)
        if configured_bridge_dir is None or not str(configured_bridge_dir).strip():
            configured_bridge_dir = (
                raw_global_settings.get("qmt_bridge_dir")
                or raw_global_settings.get(HISTORY_BRIDGE_DIR_KEY)
                or None
            )
        self.bridge_dir = (
            str(configured_bridge_dir).strip() if configured_bridge_dir is not None
            and str(configured_bridge_dir).strip() else None
        )

        configured_retries = max_task_retries
        if configured_retries is None:
            configured_retries = self.history_settings.value(HISTORY_MAX_RETRIES_KEY, None)
        if configured_retries is None:
            configured_retries = scheduled_settings.value(
                HISTORY_MAX_RETRIES_KEY, DEFAULT_HISTORY_MAX_TASK_RETRIES
            )
        configured_backoff = retry_backoff
        if configured_backoff is None:
            configured_backoff = self.history_settings.value(HISTORY_RETRY_BACKOFF_KEY, None)
        if configured_backoff is None:
            configured_backoff = scheduled_settings.value(
                HISTORY_RETRY_BACKOFF_KEY,
                json.dumps(list(DEFAULT_HISTORY_RETRY_BACKOFF)),
            )
        self.max_task_retries, self.retry_backoff = _coerce_retry_settings(
            configured_retries, configured_backoff
        )
        # Native execution controls use the same precedence as source/bridge:
        # explicit constructor kwargs > HistoryImport settings > scheduled
        # settings > global CLI settings > safe defaults.  They are retained
        # even when MiniQMT is selected so switching source is lossless, but
        # are only sent to the native importer at execution time.
        explicit_native = {
            "native_profile": native_profile,
            "profile": profile,
            "max_inflight": max_inflight,
            "batch_size": batch_size,
            "span_rows": span_rows,
            "span_bytes": span_bytes,
            "mode": mode,
            "cache_strategy": cache_strategy,
            "cancel_after": cancel_after,
        }
        history_native = read_qsettings_options(self.history_settings, complete=False)
        scheduled_native = read_qsettings_options(scheduled_settings, complete=False)
        # 调度窗口的 mode 属于追尾同步，不能泄漏到手动历史补数窗口。
        scheduled_native.pop("mode", None)
        global_native = {
            key: raw_global_settings.get(key)
            for key in NATIVE_EXECUTION_OPTION_KEYS
            if key != "mode" and key in raw_global_settings
        }
        global_native["mode"] = raw_global_settings.get(
            "qmt_native_history_mode", "historical-backfill"
        )
        # Keep explicit ``None`` as “not provided” so persisted values can be
        # recovered; a caller can reset a value by passing the canonical safe
        # default explicitly.
        self._native_execution_explicit = {
            key for key, value in explicit_native.items()
            if value is not None and value != ""
        }
        self._native_execution = merge_native_execution_options(
            global_native,
            scheduled_native,
            history_native,
            explicit_native,
        )
        for _key, _value in self._native_execution.items():
            setattr(self, _key, _value)
        self.source = self.history_source
        self._bridge_dir_explicit = bridge_dir is not None and bool(str(bridge_dir).strip())
        self.history_source_probe = None
        self.history_source_probe_details = {}
        self.history_source_available: Optional[bool] = None
        self.import_thread = None
        self.full_increment_thread = None
        self.full_import_local_thread = None
        self.custom_increment_thread = None  # 自定义增量线程
        self.indicator_thread = None
        self._indicator_pipeline_context = None
        self._pipeline_stop_requested = False
        self._close_after_stop = False
        self._close_poll_scheduled = False
        self.completed_tasks = set()
        self.custom_completed_tasks = set()  # 自定义增量已完成任务
        self.countdown_timer = None
        self.font_scale = get_ui_font_scale()
        self._base_style_raw = None

        self.setWindowTitle(
            "历史行情导入（MiniQMT / 大QMT原生桥）"
            if self.allow_native_source else "MiniQMT历史行情导入"
        )
        # 长页面已由选项卡内滚动区承载，因此允许窗口在小屏幕上收缩；
        # 默认仍尽量给日期和复权选项足够横向空间。
        self.setMinimumSize(800, 600)

        # 设置窗口属性 - 确保关闭时不影响主程序
        self.setAttribute(Qt.WA_DeleteOnClose, True)  # 关闭时删除对象
        self.setWindowFlags(self.windowFlags() | Qt.Window)  # 设置为独立窗口

        # 设置Windows暗色标题栏
        self._set_dark_titlebar()

        # 设置暗色主题样式（与主界面一致）
        self._base_style_raw = """
            QDialog, QWidget {
                background-color: #333333;
                color: #e8e8e8;
                font-family: "Microsoft YaHei UI";
            }
            QLabel {
                color: #e8e8e8;
            }
            QLineEdit, QTextEdit {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px;
            }
            QPushButton {
                background-color: #0078d4;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-family: "Microsoft YaHei UI";
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:pressed {
                background-color: #005a9e;
            }
            QPushButton:disabled {
                background-color: #555555;
                color: #888888;
            }
            QComboBox {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px;
            }
            QComboBox QAbstractItemView {
                background-color: #3c3c3c;
                color: #e8e8e8;
                selection-background-color: #0078d4;
            }
            QCheckBox {
                color: #e8e8e8;
                min-height: 24px;
                spacing: 6px;
                padding: 2px 0;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 1px solid #555555;
                border-radius: 3px;
                background-color: #3c3c3c;
            }
            QCheckBox::indicator:checked {
                background-color: #0078d4;
                border-color: #0078d4;
            }
            QRadioButton {
                color: #e8e8e8;
            }
            QRadioButton::indicator {
                width: 18px;
                height: 18px;
                border: 1px solid #555555;
                border-radius: 9px;
                background-color: #3c3c3c;
            }
            QRadioButton::indicator:checked {
                background-color: #0078d4;
                border-color: #0078d4;
            }
            QGroupBox {
                border: 1px solid #555555;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
                color: #e8e8e8;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QProgressBar {
                background-color: #3c3c3c;
                border: 1px solid #555555;
                border-radius: 3px;
                text-align: center;
                color: #e8e8e8;
            }
            QProgressBar::chunk {
                background-color: #0078d4;
            }
            QTabWidget::pane {
                border: 1px solid #555555;
                background-color: #333333;
            }
            QTabBar::tab {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                padding: 8px 16px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background-color: #0078d4;
            }
            QTabBar::tab:hover {
                background-color: #404040;
            }
            QSpinBox, QDateEdit {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px;
            }
            QFrame {
                background-color: #333333;
                color: #e8e8e8;
            }
        """
        _ui_font = get_preferred_ui_font_family() or "Microsoft YaHei UI"
        if _ui_font != "Microsoft YaHei UI":
            self._base_style_raw = self._base_style_raw.replace('"Microsoft YaHei UI"', f'"{_ui_font}"')
        self.setStyleSheet(self._scale_stylesheet(self._base_style_raw, self.font_scale))

        self.init_ui()
        # 规范化并记录当前来源/桥接参数；非法旧配置由界面保留并提示，
        # 不在这里自动替换成另一数据源。
        self._persist_history_import_config()
        try:
            available = QApplication.desktop().availableGeometry(self)
            self.resize(
                max(self.minimumWidth(), min(1160, available.width() - 80)),
                max(self.minimumHeight(), min(820, available.height() - 80)),
            )
        except Exception:
            self.resize(1100, 780)
        self.check_resume()
        self.apply_ui_scale(self.font_scale)
        self._content_height_fitted = False

        # 初始化xtquant状态检查定时器
        self.xtquant_status_timer = QTimer(self)
        self.xtquant_status_timer.timeout.connect(self.check_xtquant_connection)
        self.xtquant_status_timer.start(5000)  # 每5秒检查一次
        self.history_source_timer = QTimer(self)
        self.history_source_timer.timeout.connect(self.check_history_source)
        self.history_source_timer.start(5000)
        
        # 初始检查
        self._update_source_specific_ui()
        self.check_xtquant_connection()
        self.check_history_source()

    def _set_dark_titlebar(self):
        """设置Windows暗色标题栏"""
        try:
            import platform
            if platform.system() == "Windows":
                from ctypes import windll, c_int, byref, sizeof
                from ctypes.wintypes import DWORD
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                DWMWA_CAPTION_COLOR = 35
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_USE_IMMERSIVE_DARK_MODE,
                    byref(c_int(2)),
                    sizeof(c_int)
                )
                caption_color = DWORD(0x333333)
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_CAPTION_COLOR,
                    byref(caption_color),
                    sizeof(caption_color)
                )
        except Exception as e:
            import logging
            logging.debug(f"设置暗色标题栏失败: {str(e)}")

    def _scale_stylesheet(self, style: str, scale: float) -> str:
        if not style:
            return style
        import re

        def repl(match):
            value = float(match.group(1))
            unit = match.group(2)
            scaled = max(6, int(round(value * float(scale))))
            return f"font-size: {scaled}{unit}"

        return re.sub(
            r"font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(px|pt)",
            repl,
            style,
            flags=re.IGNORECASE
        )

    def _set_scaled_stylesheet(self, widget: QWidget, style: str):
        widget.setProperty("ui_base_stylesheet", style)
        widget.setStyleSheet(self._scale_stylesheet(style, self.font_scale))

    def _set_scaled_font(self, widget: QWidget, base_pt: int):
        widget.setProperty("ui_base_font_pt", base_pt)
        font = widget.font()
        font.setPointSize(max(6, int(round(base_pt * self.font_scale))))
        widget.setFont(font)

    def apply_ui_scale(self, scale=None):
        if scale is None:
            scale = get_ui_font_scale()
        self.font_scale = scale
        if self._base_style_raw:
            self.setStyleSheet(self._scale_stylesheet(self._base_style_raw, self.font_scale))
        try:
            for child in self.findChildren(QWidget):
                base_ss = child.property("ui_base_stylesheet")
                if base_ss:
                    child.setStyleSheet(self._scale_stylesheet(base_ss, self.font_scale))
                base_pt = child.property("ui_base_font_pt")
                if base_pt:
                    font = child.font()
                    font.setPointSize(max(6, int(round(float(base_pt) * self.font_scale))))
                    child.setFont(font)
        except Exception:
            pass

    def showEvent(self, event):
        """首次显示后把高度撑到内容所需，避免底部按钮落在可视区外。"""
        super().showEvent(event)
        if getattr(self, "_content_height_fitted", False):
            return
        self._content_height_fitted = True
        QTimer.singleShot(0, lambda: fit_dialog_height_to_content(self, self.tab_widget))

    def init_ui(self):
        layout = QVBoxLayout(self)

        # ========== 顶部状态栏 ==========
        status_bar_widget = QWidget()
        status_bar_widget.setStyleSheet("""
            QWidget {
                background-color: #2b2b2b;
                border-bottom: 2px solid #404040;
            }
        """)
        status_bar_layout = QHBoxLayout(status_bar_widget)
        status_bar_layout.setContentsMargins(15, 10, 15, 10)
        status_bar_layout.setSpacing(10)
        
        # 连接状态标签
        conn_label = QLabel("xtquant连接状态:")
        self._set_scaled_stylesheet(conn_label, """
            color: #e8e8e8;
            font-weight: bold;
            font-size: 14px;
            padding: 0px;
            margin: 0px;
        """)
        status_bar_layout.addWidget(conn_label)
        
        # 创建状态指示灯容器（确保指示灯有足够的显示空间）
        indicator_container = QWidget()
        indicator_container.setFixedSize(24, 24)
        indicator_layout = QHBoxLayout(indicator_container)
        indicator_layout.setContentsMargins(0, 0, 0, 0)
        indicator_layout.setAlignment(Qt.AlignCenter)
        
        # 创建状态指示灯
        self.xtquant_indicator = QLabel()
        self.xtquant_indicator.setFixedSize(20, 20)
        self.xtquant_indicator.setToolTip("xtquant连接状态")
        indicator_layout.addWidget(self.xtquant_indicator)
        
        status_bar_layout.addWidget(indicator_container)
        
        # 添加状态文字标签
        self.xtquant_status_text = QLabel("检查中...")
        self._set_scaled_stylesheet(self.xtquant_status_text, """
            color: #b0b0b0;
            font-size: 13px;
            padding: 0px;
            margin: 0px;
        """)
        status_bar_layout.addWidget(self.xtquant_status_text)

        # 历史行情来源选择：只保存 canonical 值，不自动在两个客户端之间
        # fallback。实际是否打开/登录由下方状态检查和开始任务前探测共同确认。
        self.history_source_combo = QComboBox(self)
        self.history_import_source_combo = self.history_source_combo
        self.history_source_combo.addItem("MiniQMT（xtdata）", MINIQMT)
        if self.allow_native_source:
            self.history_source_combo.addItem("大QMT（原生桥）", QMT_NATIVE)
        current_index = self.history_source_combo.findData(self.history_source)
        self.history_source_combo.setCurrentIndex(current_index if current_index >= 0 else -1)
        self.history_source_combo.currentIndexChanged.connect(self._on_history_source_changed)
        self.history_source_status = QLabel("检查中…", self)
        self._set_scaled_stylesheet(self.history_source_status, "color: #b0b0b0; font-size: 12px;")
        if self.allow_native_source:
            source_label = QLabel("历史行情源:")
            self._set_scaled_stylesheet(source_label, "color: #e8e8e8; font-weight: bold; font-size: 13px;")
            status_bar_layout.addWidget(source_label)
            self.history_source_combo.setToolTip(
                "明确选择历史行情来源；不会因为未启动而自动切换或启动另一客户端"
            )
            status_bar_layout.addWidget(self.history_source_combo)
            status_bar_layout.addWidget(self.history_source_status)
            self.qmt_bridge_sync_btn = QPushButton("同步大QMT高速桥")
            self.qmt_bridge_sync_btn.setToolTip(
                "备份大QMT中的旧桥脚本并同步 KhQuant 随包最新版"
            )
            self.qmt_bridge_sync_btn.clicked.connect(self._sync_qmt_native_bridge)
            status_bar_layout.addWidget(self.qmt_bridge_sync_btn)
        else:
            self.history_source_combo.setVisible(False)
            self.history_source_status.setVisible(False)
        
        status_bar_layout.addStretch()
        
        layout.addWidget(status_bar_widget)

        # 创建选项卡
        self.tab_widget = QTabWidget()
        layout.addWidget(self.tab_widget)

        # 两个页面内容较长；在系统字号倍率较大或小屏幕上必须允许滚动，
        # 否则底部的开始/停止按钮会被挤到屏幕外且无法操作。
        # ===== 选项卡1：全量增量补充（放在首位） =====
        full_tab = QWidget()
        full_layout = QVBoxLayout(full_tab)
        self._init_full_increment_tab(full_layout)
        self.full_scroll_area = QScrollArea()
        self.full_scroll_area.setWidgetResizable(True)
        self.full_scroll_area.setFrameShape(QFrame.NoFrame)
        self.full_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.full_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.full_scroll_area.setWidget(full_tab)
        self.tab_widget.addTab(self.full_scroll_area, "全量增量补充")

        # ===== 选项卡2：自定义补充数据 =====
        normal_tab = QWidget()
        normal_layout = QVBoxLayout(normal_tab)
        self._init_normal_import_tab(normal_layout)
        self.normal_scroll_area = QScrollArea()
        self.normal_scroll_area.setWidgetResizable(True)
        self.normal_scroll_area.setFrameShape(QFrame.NoFrame)
        self.normal_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.normal_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.normal_scroll_area.setWidget(normal_tab)
        self.tab_widget.addTab(self.normal_scroll_area, "自定义补充数据")
        for prefix in ("custom", "full"):
            # Tick 单独选择时复权必须锁定为 none；Tick 与 bar 混选时，
            # bar 的复权选项仍然可用。因此所有周期复选框变化都要重新计算。
            for period in ("1d", "1m", "5m", "tick"):
                period_check = getattr(self, f"{prefix}_period_{period}_check", None)
                if period_check is not None:
                    period_check.toggled.connect(
                        lambda _checked, owner=self: owner._apply_tick_adjustment_policy()
                    )
        self._apply_tick_adjustment_policy()

    @classmethod
    def _build_baostock_indicator_request(
        cls,
        enabled: bool,
        stocks: List[str],
        periods_config: Dict[str, tuple],
    ) -> Optional[dict]:
        """构建日线指标请求；BaoStock 指标仅适用于沪深 A 股。"""
        if not enabled or '1d' not in periods_config:
            return None

        eligible = []
        unsupported = []
        seen = set()
        sh_prefixes = ('600', '601', '603', '605', '688', '689')
        sz_prefixes = ('000', '001', '002', '003', '300', '301')
        for raw_code in stocks or []:
            code = str(raw_code).strip().upper()
            if not code or code in seen:
                continue
            seen.add(code)
            number, dot, market = code.partition('.')
            supported = bool(
                dot
                and number.isdigit()
                and len(number) == 6
                and (
                    (market == 'SH' and number.startswith(sh_prefixes))
                    or (market == 'SZ' and number.startswith(sz_prefixes))
                )
            )
            (eligible if supported else unsupported).append(code)

        start, end = periods_config['1d']

        def to_dash(value):
            text = str(value).strip().replace('-', '')
            return datetime.strptime(text, '%Y%m%d').strftime('%Y-%m-%d')

        return {
            'stocks': eligible,
            'unsupported_stocks': unsupported,
            'start_date': to_dash(start),
            'end_date': to_dash(end),
            'indicators': list(cls.BAOSTOCK_DAILY_INDICATORS),
        }

    def _prepare_indicator_pipeline(
        self,
        scope: str,
        enabled: bool,
        stocks: List[str],
        periods_config: Dict[str, tuple],
    ):
        self._pipeline_stop_requested = False
        self._indicator_pipeline_context = {
            'scope': scope,
            'enabled': bool(enabled),
            'stocks': list(stocks or []),
            'periods_config': dict(periods_config),
            'market_results': None,
            'request': None,
        }

    def _is_any_import_running(self) -> bool:
        for name in (
            'import_thread', 'full_increment_thread', 'full_import_local_thread',
            'custom_increment_thread', 'indicator_thread'
        ):
            thread = getattr(self, name, None)
            if thread is not None and thread.isRunning():
                return True
        return False

    def is_import_running(self) -> bool:
        """供父窗口统一判断 miniQMT 导入任务是否仍在运行。"""
        try:
            return self._is_any_import_running()
        except RuntimeError:
            return False

    def request_close_after_stop(self):
        """停止领取新任务，等待写库与元数据收尾完成后自动关闭。"""
        if not self.is_import_running():
            self.close()
            return
        self._close_after_stop = True
        # 关闭流程中不再进行周期性来源探测；探测本身只读，但会制造无用
        # 日志/状态写入并可能与 Qt 对象销毁竞态。
        for timer_name in ("xtquant_status_timer", "history_source_timer"):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                try:
                    timer.stop()
                except Exception:
                    pass
        self._set_pipeline_start_buttons_enabled(False)
        self.stop_btn.setEnabled(False)
        self.full_stop_btn.setEnabled(False)
        context_scope = (self._indicator_pipeline_context or {}).get('scope')
        full_running = bool(
            (self.full_increment_thread and self.full_increment_thread.isRunning())
            or (self.full_import_local_thread and self.full_import_local_thread.isRunning())
        )
        scope = context_scope or ('full' if full_running else 'custom')
        self._pipeline_log(
            scope,
            "正在安全停止：等待当前写入、元数据刷新和连接释放后关闭窗口...",
        )
        self._stop_all_running_threads()
        self._schedule_close_when_idle()

    def _set_pipeline_start_buttons_enabled(self, enabled: bool):
        for name in ('start_btn', 'full_start_btn'):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(enabled)
        if hasattr(self, 'full_import_local_btn'):
            is_native = getattr(self, "history_source", None) == QMT_NATIVE
            self.full_import_local_btn.setEnabled(bool(enabled) and not is_native)
        # 来源在任务创建时会被绑定到后台线程；运行中允许用户改下拉框会
        # 只改变持久化配置，却不会改变已经提交的任务，容易造成“界面显示
        # 与实际下载端不一致”。因此把来源选择器与启动按钮作为同一组
        # 运行态控件锁定，任务结束/失败后再恢复。
        source_combo = getattr(self, 'history_source_combo', None)
        if source_combo is None:
            source_combo = getattr(self, 'history_import_source_combo', None)
        if source_combo is not None:
            try:
                source_combo.setEnabled(bool(enabled))
            except Exception:
                pass

    def _pipeline_log(self, scope: str, message: str):
        (self.log if scope == 'custom' else self.full_log)(message)

    def _set_indicator_stage_ui(self, scope: str, current: int = 0, total: int = 0):
        text = f"BaoStock日线指标 {current}/{total}" if total else "正在启动BaoStock日线指标..."
        if scope == 'custom':
            self.custom_phase_label.setText("阶段: 补充日线指标")
            self.import_status_label.setText(text)
            self.progress_bar.setValue(int(current * 100 / total) if total else 0)
            self.import_countdown_label.setText("")
        else:
            self.full_phase_label.setText("阶段: 补充日线指标")
            self.full_status_label.setText(text)
            self.full_progress_bar.setValue(int(current * 100 / total) if total else 0)
            self.full_countdown_label.setText("")

    def _continue_after_market_stage(
        self,
        scope: str,
        market_results: dict,
        resolved_stocks: Optional[List[str]] = None,
    ):
        context = self._indicator_pipeline_context
        if not context or context.get('scope') != scope:
            context = {
                'scope': scope,
                'enabled': False,
                'stocks': [],
                'periods_config': {},
            }
            self._indicator_pipeline_context = context
        if resolved_stocks is not None:
            context['stocks'] = list(resolved_stocks)
        context['market_results'] = dict(market_results or {})

        if market_results.get('cancelled') or self._pipeline_stop_requested:
            self._finalize_indicator_pipeline(scope, market_results, None)
            return

        request = self._build_baostock_indicator_request(
            context.get('enabled', False),
            context.get('stocks', []),
            context.get('periods_config', {}),
        )
        context['request'] = request
        if request is None:
            self._finalize_indicator_pipeline(scope, market_results, None)
            return

        unsupported = request.get('unsupported_stocks', [])
        if unsupported:
            self._pipeline_log(
                scope,
                f"BaoStock日线指标仅覆盖沪深A股，已跳过 {len(unsupported)} 个不支持的证券",
            )
        if not request.get('stocks'):
            indicator_results = {
                'success': 0,
                'failed': 0,
                'empty': len(unsupported),
                'total_records': 0,
                'cancelled': False,
            }
            self._finalize_indicator_pipeline(scope, market_results, indicator_results)
            return

        self._start_baostock_indicator_stage(scope, request)

    def _start_baostock_indicator_stage(self, scope: str, request: dict):
        try:
            data_root = getattr(self.manager, 'data_root', None) or os.getcwd()
            tracker = get_baostock_request_tracker(data_root)
            current_count = tracker.get_count()
            total = len(request['stocks'])
            if tracker.is_limit_reached():
                self._pipeline_log(scope, tracker.limit_message)
                context = self._indicator_pipeline_context or {}
                self._finalize_indicator_pipeline(
                    scope,
                    context.get('market_results') or {},
                    {
                        'success': 0,
                        'failed': 0,
                        'empty': 0,
                        'total_records': 0,
                        'unprocessed': total,
                        'cancelled': False,
                        'limit_reached': True,
                    },
                )
                return
            self._pipeline_log(
                scope,
                f"开始补充日线指标：{total} 只股票，数据由 BaoStock 提供；"
                f"预计 {total} 次请求，今日已使用 {current_count}/{tracker.daily_limit}",
            )
            self._set_indicator_stage_ui(scope, 0, total)
            self._set_pipeline_start_buttons_enabled(False)
            if scope == 'custom':
                self.stop_btn.setEnabled(True)
            else:
                self.full_stop_btn.setEnabled(True)

            self.indicator_thread = BaoStockIndicatorImportThread(
                self.manager,
                request['stocks'],
                request['start_date'],
                request['end_date'],
                request['indicators'],
                request_tracker=tracker,
            )
            self.indicator_thread.progress.connect(self.on_baostock_indicator_progress)
            self.indicator_thread.status.connect(
                lambda message, current_scope=scope: self._pipeline_log(current_scope, message)
            )
            self.indicator_thread.finished.connect(self.on_baostock_indicator_finished)
            self.indicator_thread.error.connect(self.on_baostock_indicator_error)
            self.indicator_thread.start()
        except Exception as exc:
            self.indicator_thread = None
            self._pipeline_log(scope, f"BaoStock指标任务启动失败: {exc}")
            context = self._indicator_pipeline_context or {}
            self._finalize_indicator_pipeline(
                scope,
                context.get('market_results') or {},
                {
                    'success': 0,
                    'failed': len(request.get('stocks', [])),
                    'empty': 0,
                    'total_records': 0,
                    'unprocessed': 0,
                    'cancelled': False,
                    'error': str(exc),
                },
            )

    def on_baostock_indicator_progress(self, percent: int, current: int, total: int):
        context = self._indicator_pipeline_context or {}
        scope = context.get('scope', 'custom')
        self._set_indicator_stage_ui(scope, current, total)

    def on_baostock_indicator_finished(self, results: dict):
        context = self._indicator_pipeline_context or {}
        scope = context.get('scope', 'custom')
        market_results = context.get('market_results') or {}
        self._pipeline_log(scope, "=" * 40)
        if results.get('cancelled'):
            self._pipeline_log(scope, "BaoStock日线指标补充已停止")
        elif results.get('limit_reached'):
            self._pipeline_log(scope, "BaoStock日线指标达到请求上限，未全部完成")
        else:
            self._pipeline_log(scope, "BaoStock日线指标补充完成")
        self._pipeline_log(scope, f"成功: {results.get('success', 0)} 只")
        self._pipeline_log(scope, f"无匹配数据: {results.get('empty', 0)} 只")
        self._pipeline_log(scope, f"失败: {results.get('failed', 0)} 只")
        if results.get('unprocessed', 0):
            self._pipeline_log(scope, f"未处理: {results.get('unprocessed', 0)} 只")
        self._pipeline_log(scope, f"更新日线: {results.get('total_records', 0)} 条")
        if results.get('limit_reached'):
            self._pipeline_log(scope, "BaoStock请求已达到当日上限，剩余指标未补充")
        self._pipeline_log(scope, "=" * 40)
        self._finalize_indicator_pipeline(scope, market_results, results)

    def on_baostock_indicator_error(self, error: str):
        context = self._indicator_pipeline_context or {}
        scope = context.get('scope', 'custom')
        market_results = context.get('market_results') or {}
        request = context.get('request') or {}
        self._pipeline_log(scope, f"BaoStock指标补充失败: {error}")
        results = {
            'success': 0,
            'failed': len(request.get('stocks', [])),
            'empty': 0,
            'total_records': 0,
            'unprocessed': 0,
            'cancelled': False,
            'error': error,
        }
        self._finalize_indicator_pipeline(scope, market_results, results)

    def _finalize_indicator_pipeline(
        self,
        scope: str,
        market_results: dict,
        indicator_results: Optional[dict],
    ):
        stopped = bool(
            self._pipeline_stop_requested
            or market_results.get('cancelled')
            or (indicator_results or {}).get('cancelled')
        )
        indicator_issue = bool(
            indicator_results is not None
            and (
                indicator_results.get('error')
                or indicator_results.get('limit_reached')
                or indicator_results.get('failed', 0) > 0
                or indicator_results.get('unprocessed', 0) > 0
            )
        )
        self._set_pipeline_start_buttons_enabled(True)
        self.stop_btn.setEnabled(False)
        self.full_stop_btn.setEnabled(False)

        if stopped:
            phase_text = "阶段: 已停止"
            status_text = "已停止"
        elif indicator_issue:
            phase_text = "阶段: 部分完成"
            status_text = (
                f"{_history_source_display_name(self.history_source)}行情完成，"
                "BaoStock指标未全部完成"
            )
        else:
            phase_text = "阶段: 完成"
            status_text = "完成"

        if scope == 'custom':
            self.custom_phase_label.setText(phase_text)
            self.import_status_label.setText(status_text)
            self.import_countdown_label.setText("")
        else:
            self.full_phase_label.setText(phase_text)
            self.full_status_label.setText(status_text)
            self.full_countdown_label.setText("")

        has_changes = bool(
            market_results.get('total_records', 0)
            or (indicator_results or {}).get('total_records', 0)
        )
        parent = self.parent()
        parent_closing = bool(
            parent
            and (
                getattr(parent, '_viewer_closing', False)
                or getattr(parent, '_pending_close_after_tushare_stop', False)
            )
        )
        if has_changes and not parent_closing and parent:
            refreshed = False
            if hasattr(parent, 'on_refresh_clicked'):
                try:
                    parent.on_refresh_clicked()
                    refreshed = True
                except Exception as exc:
                    self._pipeline_log(scope, f"自动刷新列表失败: {exc}")
            elif hasattr(parent, 'refresh_stock_list'):
                try:
                    parent.refresh_stock_list()
                    refreshed = True
                except Exception as exc:
                    self._pipeline_log(scope, f"自动刷新列表失败: {exc}")
            if refreshed:
                self._pipeline_log(scope, "股票列表已自动刷新")

        if not stopped and not self._close_after_stop:
            lines = [
                f"{_history_source_display_name(self.history_source)}行情成功: "
                f"{market_results.get('success', 0)}",
                f"行情记录数: {market_results.get('total_records', 0)}",
            ]
            lock_skipped = market_results.get('lock_skipped', [])
            if lock_skipped:
                lines.append(f"数据库占用跳过: {len(lock_skipped)}")
                preview = ", ".join(
                    f"{item.get('stock')} {item.get('period')}"
                    for item in lock_skipped[:8]
                )
                lines.append(f"未补充清单: {preview}")
                if len(lock_skipped) > 8:
                    lines.append(f"另有 {len(lock_skipped) - 8} 项，详见运行日志")
            if indicator_results is not None:
                lines.extend([
                    f"BaoStock指标成功: {indicator_results.get('success', 0)}",
                    f"指标更新日线: {indicator_results.get('total_records', 0)}",
                    f"指标失败: {indicator_results.get('failed', 0)}",
                ])
                if indicator_results.get('unprocessed', 0):
                    lines.append(f"指标未处理: {indicator_results.get('unprocessed', 0)}")
                lines.append("指标来源: BaoStock")
            if indicator_issue:
                QMessageBox.warning(self, "行情完成，指标未全部完成", "\n".join(lines))
            else:
                QMessageBox.information(self, "处理完成", "\n".join(lines))

        self._indicator_pipeline_context = None
        self._pipeline_stop_requested = False
        if self._close_after_stop:
            self._schedule_close_when_idle()

    def _init_normal_import_tab(self, layout):
        """初始化常规导入选项卡"""
        # ========== 股票池选择 ==========
        stock_group = QGroupBox("股票池选择")
        stock_layout = QVBoxLayout(stock_group)

        # 选择方式
        method_layout = QHBoxLayout()
        self.method_group = QButtonGroup(self)

        self.preset_radio = QRadioButton("预设板块")
        self.preset_radio.setChecked(True)
        self.method_group.addButton(self.preset_radio)
        method_layout.addWidget(self.preset_radio)

        self.file_radio = QRadioButton("从文件导入")
        self.method_group.addButton(self.file_radio)
        method_layout.addWidget(self.file_radio)

        self.manual_radio = QRadioButton("手动输入")
        self.method_group.addButton(self.manual_radio)
        method_layout.addWidget(self.manual_radio)

        method_layout.addStretch()
        stock_layout.addLayout(method_layout)

        # 预设板块选择
        self.preset_frame = QFrame()
        preset_layout = QGridLayout(self.preset_frame)
        preset_layout.setContentsMargins(0, 0, 0, 0)

        self.preset_checks = {}
        presets = [
            ('沪深A股', 'all_a'),
            ('上证A股', 'sh_a'),
            ('深证A股', 'sz_a'),
            ('沪深300', 'hs300'),
            ('上证50', 'sz50'),
            ('中证500', 'zz500'),
            ('创业板', 'cyb'),
            ('科创板', 'kcb'),
            ('沪深ETF', 'hs_etf'),
            ('沪深场内基金（含ETF/LOF）', 'hs_fund'),
            ('沪深转债', 'hs_convertible_bonds'),
            ('T0型ETF', 't0_etf'),
            ('常用指数', 'common_index'),
        ]

        for i, (name, key) in enumerate(presets):
            cb = QCheckBox(name)
            self.preset_checks[key] = cb
            preset_layout.addWidget(cb, i // 4, i % 4)

        stock_layout.addWidget(self.preset_frame)

        # 文件选择
        self.file_frame = QFrame()
        file_layout = QHBoxLayout(self.file_frame)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_path_edit = QLineEdit()
        self.file_path_edit.setPlaceholderText("选择股票列表CSV文件...")
        file_layout.addWidget(self.file_path_edit)
        self.browse_file_btn = QPushButton("浏览...")
        self.browse_file_btn.clicked.connect(self.browse_stock_file)
        file_layout.addWidget(self.browse_file_btn)
        self.file_frame.setVisible(False)
        stock_layout.addWidget(self.file_frame)

        # 手动输入
        self.manual_frame = QFrame()
        manual_layout = QVBoxLayout(self.manual_frame)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.addWidget(QLabel(
            "输入股票代码（每行一个，支持纯数字或带市场后缀；退市证券请选择其历史交易区间）:"
        ))
        self.manual_edit = QTextEdit()
        self.manual_edit.setMaximumHeight(100)
        self.manual_edit.setPlaceholderText(
            "000001.SZ 或 000001\n600001.SH 或 600001（历史证券）\n300750"
        )
        manual_layout.addWidget(self.manual_edit)
        self.manual_frame.setVisible(False)
        stock_layout.addWidget(self.manual_frame)

        # 连接信号
        self.preset_radio.toggled.connect(self.on_method_changed)
        self.file_radio.toggled.connect(self.on_method_changed)
        self.manual_radio.toggled.connect(self.on_method_changed)

        layout.addWidget(stock_group)

        # ========== 数据周期设置 ==========
        period_group = QGroupBox("数据周期设置")
        period_layout = QGridLayout(period_group)

        today = QDate.currentDate()

        # 日线 - 10年
        self.custom_period_1d_check = QCheckBox("日线 (1d)")
        self.custom_period_1d_check.setChecked(True)
        period_layout.addWidget(self.custom_period_1d_check, 0, 0)
        period_layout.addWidget(QLabel("开始:"), 0, 1)
        self.custom_period_1d_start = QDateEdit()
        self.custom_period_1d_start.setCalendarPopup(True)
        self.custom_period_1d_start.setDate(today.addYears(-10))
        period_layout.addWidget(self.custom_period_1d_start, 0, 2)
        period_layout.addWidget(QLabel("结束:"), 0, 3)
        self.custom_period_1d_end = QDateEdit()
        self.custom_period_1d_end.setCalendarPopup(True)
        self.custom_period_1d_end.setDate(today)
        period_layout.addWidget(self.custom_period_1d_end, 0, 4)

        # 1分钟 - 1年
        self.custom_period_1m_check = QCheckBox("1分钟 (1m)")
        self.custom_period_1m_check.setChecked(True)
        period_layout.addWidget(self.custom_period_1m_check, 1, 0)
        period_layout.addWidget(QLabel("开始:"), 1, 1)
        self.custom_period_1m_start = QDateEdit()
        self.custom_period_1m_start.setCalendarPopup(True)
        self.custom_period_1m_start.setDate(today.addYears(-1))
        period_layout.addWidget(self.custom_period_1m_start, 1, 2)
        period_layout.addWidget(QLabel("结束:"), 1, 3)
        self.custom_period_1m_end = QDateEdit()
        self.custom_period_1m_end.setCalendarPopup(True)
        self.custom_period_1m_end.setDate(today)
        period_layout.addWidget(self.custom_period_1m_end, 1, 4)

        # 5分钟 - 1年
        self.custom_period_5m_check = QCheckBox("5分钟 (5m)")
        self.custom_period_5m_check.setChecked(True)
        period_layout.addWidget(self.custom_period_5m_check, 2, 0)
        period_layout.addWidget(QLabel("开始:"), 2, 1)
        self.custom_period_5m_start = QDateEdit()
        self.custom_period_5m_start.setCalendarPopup(True)
        self.custom_period_5m_start.setDate(today.addYears(-1))
        period_layout.addWidget(self.custom_period_5m_start, 2, 2)
        period_layout.addWidget(QLabel("结束:"), 2, 3)
        self.custom_period_5m_end = QDateEdit()
        self.custom_period_5m_end.setCalendarPopup(True)
        self.custom_period_5m_end.setDate(today)
        period_layout.addWidget(self.custom_period_5m_end, 2, 4)

        # Tick - 1个月
        self.custom_period_tick_check = QCheckBox("Tick数据")
        self.custom_period_tick_check.setChecked(False)  # 默认不选，数据量太大
        period_layout.addWidget(self.custom_period_tick_check, 3, 0)
        period_layout.addWidget(QLabel("开始:"), 3, 1)
        self.custom_period_tick_start = QDateEdit()
        self.custom_period_tick_start.setCalendarPopup(True)
        self.custom_period_tick_start.setDate(today.addMonths(-1))
        period_layout.addWidget(self.custom_period_tick_start, 3, 2)
        period_layout.addWidget(QLabel("结束:"), 3, 3)
        self.custom_period_tick_end = QDateEdit()
        self.custom_period_tick_end.setCalendarPopup(True)
        self.custom_period_tick_end.setDate(today)
        period_layout.addWidget(self.custom_period_tick_end, 3, 4)

        self.custom_force_overwrite_check = QCheckBox("强制覆写已有数据（禁用增量补充）")
        self.custom_force_overwrite_check.setChecked(False)
        period_layout.addWidget(self.custom_force_overwrite_check, 4, 0, 1, 5)

        self.custom_baostock_indicators_check = QCheckBox(
            "同时补充日线指标（数据由 BaoStock 提供）"
        )
        self.custom_baostock_indicators_check.setChecked(False)
        self.custom_baostock_indicators_check.setToolTip(
            "行情完成后，由BaoStock补充换手率、涨跌幅、估值和是否ST；"
            "仅更新已有日线，会增加网络请求及耗时。"
        )
        custom_indicator_layout = QHBoxLayout()
        custom_indicator_layout.setContentsMargins(0, 0, 0, 0)
        custom_indicator_layout.setSpacing(10)
        custom_indicator_layout.addWidget(self.custom_baostock_indicators_check)
        self.custom_baostock_indicators_detail_label = QLabel(
            "包含：换手率、涨跌幅、PE、PS、PCF、PB、是否ST"
        )
        self._set_scaled_stylesheet(
            self.custom_baostock_indicators_detail_label,
            "color: #9aa0a6; font-size: 12px;",
        )
        custom_indicator_layout.addWidget(self.custom_baostock_indicators_detail_label)
        custom_indicator_layout.addStretch()
        period_layout.addLayout(custom_indicator_layout, 5, 0, 1, 5)
        self.custom_period_1d_check.toggled.connect(
            self.custom_baostock_indicators_check.setEnabled
        )
        self.custom_period_1d_check.toggled.connect(
            self.custom_baostock_indicators_detail_label.setEnabled
        )

        layout.addWidget(period_group)

        dividend_group = QGroupBox("复权方式")
        dividend_layout = QVBoxLayout(dividend_group)
        dividend_checks_layout = QHBoxLayout()
        self.custom_dividend_none_check = QCheckBox("不复权")
        self.custom_dividend_front_check = QCheckBox("前复权")
        self.custom_dividend_back_check = QCheckBox("后复权")
        self.custom_dividend_front_ratio_check = QCheckBox("等比前复权")
        self.custom_dividend_back_ratio_check = QCheckBox("等比后复权")
        self.custom_dividend_none_check.setChecked(True)
        self.custom_dividend_front_check.setChecked(True)
        self.custom_dividend_back_check.setChecked(True)
        self.custom_dividend_front_ratio_check.setChecked(False)
        self.custom_dividend_back_ratio_check.setChecked(False)
        dividend_checks_layout.addWidget(self.custom_dividend_none_check)
        dividend_checks_layout.addWidget(self.custom_dividend_front_check)
        dividend_checks_layout.addWidget(self.custom_dividend_back_check)
        dividend_checks_layout.addWidget(self.custom_dividend_front_ratio_check)
        dividend_checks_layout.addWidget(self.custom_dividend_back_ratio_check)
        dividend_checks_layout.addStretch()
        custom_dividend_tip = QLabel("提示：不同复权无法增量下载，需勾选“强制覆写”后重新下载所需复权。")
        custom_dividend_tip.setWordWrap(True)
        custom_dividend_tip.setStyleSheet("color: #8a8a8a;")
        dividend_layout.addLayout(dividend_checks_layout)
        dividend_layout.addWidget(custom_dividend_tip)
        layout.addWidget(dividend_group)

        # ========== 进度信息 ==========
        progress_group = QGroupBox("进度信息")
        progress_layout = QVBoxLayout(progress_group)

        # 进度条
        self.progress_bar = QProgressBar()
        progress_layout.addWidget(self.progress_bar)

        # 状态和倒计时
        status_layout = QHBoxLayout()
        self.import_status_label = QLabel("就绪")
        status_layout.addWidget(self.import_status_label)
        status_layout.addStretch()
        self.import_countdown_label = QLabel("")
        self.import_countdown_label.setStyleSheet("color: #1976D2; font-weight: bold;")
        status_layout.addWidget(self.import_countdown_label)
        progress_layout.addLayout(status_layout)

        # 统计信息
        stats_layout = QHBoxLayout()
        self.custom_phase_label = QLabel("阶段: -")
        stats_layout.addWidget(self.custom_phase_label)
        self.custom_tasks_label = QLabel("任务: 0")
        stats_layout.addWidget(self.custom_tasks_label)
        self.custom_completed_label = QLabel("已完成: 0")
        stats_layout.addWidget(self.custom_completed_label)
        stats_layout.addStretch()
        progress_layout.addLayout(stats_layout)

        layout.addWidget(progress_group)

        # ========== 日志 ==========
        log_group = QGroupBox("执行日志")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        _configure_download_log_widget(self.log_text)
        log_layout.addWidget(self.log_text)
        layout.addWidget(log_group)

        # ========== 按钮 ==========
        btn_layout = QHBoxLayout()

        self.start_btn = QPushButton("开始扫描并补充")
        _ui_font = get_preferred_ui_font_family() or "Microsoft YaHei UI"
        self.start_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #4CAF50;
                color: #ffffff;
                font-family: "{_ui_font}";
                font-weight: normal;
                padding: 8px 16px;
            }}
            QPushButton:disabled {{
                background-color: #555555;
                color: #888888;
            }}
        """)
        self.start_btn.clicked.connect(self.start_import)
        btn_layout.addWidget(self.start_btn)

        self.custom_dl_workers_label = QLabel("下载进程数:")
        btn_layout.addWidget(self.custom_dl_workers_label)
        self.custom_dl_workers_spin = QSpinBox()
        self.custom_dl_workers_spin.setRange(1, 8)
        self.custom_dl_workers_spin.setValue(2)
        self.custom_dl_workers_spin.setToolTip("扫描补充时的并行下载进程数，建议2~4，过多可能导致miniQMT连接冲突")
        self.custom_dl_workers_spin.setFixedWidth(60)
        btn_layout.addWidget(self.custom_dl_workers_spin)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_import)
        btn_layout.addWidget(self.stop_btn)

        btn_layout.addStretch()

        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

    def _init_full_increment_tab(self, layout):
        """初始化全量增量补充选项卡"""
        # ========== 说明信息 ==========
        info_group = QGroupBox("功能说明")
        info_layout = QVBoxLayout(info_group)
        info_label = QLabel(
            "一键扫描所有预设板块的股票数据，自动检测缺失的数据段并进行补充下载。\n"
            "支持断点续传，中断后可继续上次进度。\n\n"
            "预设板块: 沪深A股、上证A股、深证A股、沪深300、上证50、中证500、创业板、科创板、沪深ETF、沪深场内基金（含ETF/LOF）、沪深转债、T0型ETF、指数"
        )
        info_label.setWordWrap(True)
        self._set_scaled_stylesheet(info_label, "color: #e8e8e8; font-size: 14px; line-height: 1.5;")
        self._set_scaled_font(info_label, 11)
        info_layout.addWidget(info_label)
        layout.addWidget(info_group)

        # ========== 周期设置 ==========
        period_group = QGroupBox("数据周期设置")
        period_layout = QGridLayout(period_group)

        today = QDate.currentDate()

        # 日线 - 10年
        self.full_period_1d_check = QCheckBox("日线 (1d)")
        self.full_period_1d_check.setChecked(True)
        period_layout.addWidget(self.full_period_1d_check, 0, 0)
        period_layout.addWidget(QLabel("开始:"), 0, 1)
        self.full_period_1d_start = QDateEdit()
        self.full_period_1d_start.setCalendarPopup(True)
        self.full_period_1d_start.setDate(today.addYears(-10))
        period_layout.addWidget(self.full_period_1d_start, 0, 2)
        period_layout.addWidget(QLabel("结束:"), 0, 3)
        self.full_period_1d_end = QDateEdit()
        self.full_period_1d_end.setCalendarPopup(True)
        self.full_period_1d_end.setDate(today)
        period_layout.addWidget(self.full_period_1d_end, 0, 4)

        # 1分钟 - 1年
        self.full_period_1m_check = QCheckBox("1分钟 (1m)")
        self.full_period_1m_check.setChecked(True)
        period_layout.addWidget(self.full_period_1m_check, 1, 0)
        period_layout.addWidget(QLabel("开始:"), 1, 1)
        self.full_period_1m_start = QDateEdit()
        self.full_period_1m_start.setCalendarPopup(True)
        self.full_period_1m_start.setDate(today.addYears(-1))
        period_layout.addWidget(self.full_period_1m_start, 1, 2)
        period_layout.addWidget(QLabel("结束:"), 1, 3)
        self.full_period_1m_end = QDateEdit()
        self.full_period_1m_end.setCalendarPopup(True)
        self.full_period_1m_end.setDate(today)
        period_layout.addWidget(self.full_period_1m_end, 1, 4)

        # 5分钟 - 1年
        self.full_period_5m_check = QCheckBox("5分钟 (5m)")
        self.full_period_5m_check.setChecked(True)
        period_layout.addWidget(self.full_period_5m_check, 2, 0)
        period_layout.addWidget(QLabel("开始:"), 2, 1)
        self.full_period_5m_start = QDateEdit()
        self.full_period_5m_start.setCalendarPopup(True)
        self.full_period_5m_start.setDate(today.addYears(-1))
        period_layout.addWidget(self.full_period_5m_start, 2, 2)
        period_layout.addWidget(QLabel("结束:"), 2, 3)
        self.full_period_5m_end = QDateEdit()
        self.full_period_5m_end.setCalendarPopup(True)
        self.full_period_5m_end.setDate(today)
        period_layout.addWidget(self.full_period_5m_end, 2, 4)

        # Tick - 1个月
        self.full_period_tick_check = QCheckBox("Tick数据")
        self.full_period_tick_check.setChecked(False)  # 默认不选，数据量太大
        period_layout.addWidget(self.full_period_tick_check, 3, 0)
        period_layout.addWidget(QLabel("开始:"), 3, 1)
        self.full_period_tick_start = QDateEdit()
        self.full_period_tick_start.setCalendarPopup(True)
        self.full_period_tick_start.setDate(today.addMonths(-1))
        period_layout.addWidget(self.full_period_tick_start, 3, 2)
        period_layout.addWidget(QLabel("结束:"), 3, 3)
        self.full_period_tick_end = QDateEdit()
        self.full_period_tick_end.setCalendarPopup(True)
        self.full_period_tick_end.setDate(today)
        period_layout.addWidget(self.full_period_tick_end, 3, 4)

        self.full_force_overwrite_check = QCheckBox("强制覆写已有数据（禁用增量补充）")
        self.full_force_overwrite_check.setChecked(False)
        period_layout.addWidget(self.full_force_overwrite_check, 4, 0, 1, 5)

        self.full_baostock_indicators_check = QCheckBox(
            "同时补充日线指标（数据由 BaoStock 提供）"
        )
        self.full_baostock_indicators_check.setChecked(False)
        self.full_baostock_indicators_check.setToolTip(
            "行情完成后，由BaoStock补充换手率、涨跌幅、估值和是否ST；"
            "仅更新已有日线，会增加网络请求及耗时。"
        )
        full_indicator_layout = QHBoxLayout()
        full_indicator_layout.setContentsMargins(0, 0, 0, 0)
        full_indicator_layout.setSpacing(10)
        full_indicator_layout.addWidget(self.full_baostock_indicators_check)
        self.full_baostock_indicators_detail_label = QLabel(
            "包含：换手率、涨跌幅、PE、PS、PCF、PB、是否ST"
        )
        self._set_scaled_stylesheet(
            self.full_baostock_indicators_detail_label,
            "color: #9aa0a6; font-size: 12px;",
        )
        full_indicator_layout.addWidget(self.full_baostock_indicators_detail_label)
        full_indicator_layout.addStretch()
        period_layout.addLayout(full_indicator_layout, 5, 0, 1, 5)
        self.full_period_1d_check.toggled.connect(
            self.full_baostock_indicators_check.setEnabled
        )
        self.full_period_1d_check.toggled.connect(
            self.full_baostock_indicators_detail_label.setEnabled
        )

        layout.addWidget(period_group)

        dividend_group = QGroupBox("复权方式")
        dividend_layout = QVBoxLayout(dividend_group)
        dividend_checks_layout = QHBoxLayout()
        self.full_dividend_none_check = QCheckBox("不复权")
        self.full_dividend_front_check = QCheckBox("前复权")
        self.full_dividend_back_check = QCheckBox("后复权")
        self.full_dividend_front_ratio_check = QCheckBox("等比前复权")
        self.full_dividend_back_ratio_check = QCheckBox("等比后复权")
        self.full_dividend_none_check.setChecked(True)
        self.full_dividend_front_check.setChecked(True)
        self.full_dividend_back_check.setChecked(True)
        self.full_dividend_front_ratio_check.setChecked(False)
        self.full_dividend_back_ratio_check.setChecked(False)
        dividend_checks_layout.addWidget(self.full_dividend_none_check)
        dividend_checks_layout.addWidget(self.full_dividend_front_check)
        dividend_checks_layout.addWidget(self.full_dividend_back_check)
        dividend_checks_layout.addWidget(self.full_dividend_front_ratio_check)
        dividend_checks_layout.addWidget(self.full_dividend_back_ratio_check)
        dividend_checks_layout.addStretch()
        full_dividend_tip = QLabel("提示：不同复权无法增量下载，需勾选“强制覆写”后重新下载所需复权。")
        full_dividend_tip.setWordWrap(True)
        full_dividend_tip.setStyleSheet("color: #8a8a8a;")
        dividend_layout.addLayout(dividend_checks_layout)
        dividend_layout.addWidget(full_dividend_tip)
        layout.addWidget(dividend_group)

        # ========== 进度信息 ==========
        progress_group = QGroupBox("进度信息")
        progress_layout = QVBoxLayout(progress_group)

        # 进度条
        self.full_progress_bar = QProgressBar()
        progress_layout.addWidget(self.full_progress_bar)

        # 状态和倒计时
        status_layout = QHBoxLayout()
        self.full_status_label = QLabel("就绪")
        status_layout.addWidget(self.full_status_label)
        status_layout.addStretch()
        self.full_countdown_label = QLabel("")
        self.full_countdown_label.setStyleSheet("color: #1976D2; font-weight: bold;")
        status_layout.addWidget(self.full_countdown_label)
        progress_layout.addLayout(status_layout)

        # 统计信息
        stats_layout = QHBoxLayout()
        self.full_phase_label = QLabel("阶段: -")
        stats_layout.addWidget(self.full_phase_label)
        self.full_tasks_label = QLabel("任务: 0")
        stats_layout.addWidget(self.full_tasks_label)
        self.full_completed_label = QLabel("已完成: 0")
        stats_layout.addWidget(self.full_completed_label)
        stats_layout.addStretch()
        progress_layout.addLayout(stats_layout)

        layout.addWidget(progress_group)

        # ========== 日志 ==========
        log_group = QGroupBox("执行日志")
        log_layout = QVBoxLayout(log_group)
        self.full_log_text = QTextEdit()
        self.full_log_text.setReadOnly(True)
        _configure_download_log_widget(self.full_log_text)
        log_layout.addWidget(self.full_log_text)
        layout.addWidget(log_group)

        # ========== 按钮 ==========
        btn_layout = QHBoxLayout()

        self.full_start_btn = QPushButton("开始扫描并补充")
        _ui_font = get_preferred_ui_font_family() or "Microsoft YaHei UI"
        self.full_start_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #4CAF50;
                color: #ffffff;
                font-family: "{_ui_font}";
                font-weight: normal;
                padding: 8px 16px;
            }}
            QPushButton:disabled {{
                background-color: #555555;
                color: #888888;
            }}
        """)
        self.full_start_btn.clicked.connect(self.start_full_increment)
        btn_layout.addWidget(self.full_start_btn)

        self.full_dl_workers_label = QLabel("下载进程数:")
        self.full_dl_workers_label.setStyleSheet("color: #4CAF50; font-weight: bold;")
        btn_layout.addWidget(self.full_dl_workers_label)
        self.full_dl_workers_spin = QSpinBox()
        self.full_dl_workers_spin.setRange(1, 8)
        self.full_dl_workers_spin.setValue(2)
        self.full_dl_workers_spin.setToolTip("扫描补充时的并行下载进程数，建议2~4，过多可能导致miniQMT连接冲突")
        self.full_dl_workers_spin.setFixedWidth(60)
        btn_layout.addWidget(self.full_dl_workers_spin)

        self.full_import_local_btn = QPushButton("从本地miniQMT数据导入")
        self.full_import_local_btn.setToolTip("无需从miniQMT下载新数据，直接从本地miniQMT数据库读取并导入。\n根据当前勾选的周期、时间段与复权方式，使用 get_local_data 读取本地数据，效率更高，适用于已在miniQMT本地有历史数据的用户。")
        _ui_font = get_preferred_ui_font_family() or "Microsoft YaHei UI"
        self.full_import_local_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #2196F3;
                color: #ffffff;
                font-family: "{_ui_font}";
                font-weight: normal;
                padding: 8px 16px;
            }}
            QPushButton:disabled {{
                background-color: #555555;
                color: #888888;
            }}
        """)
        self.full_import_local_btn.clicked.connect(self.start_full_import_local)
        btn_layout.addWidget(self.full_import_local_btn)

        local_workers_label = QLabel("线程数:")
        local_workers_label.setStyleSheet("color: #2196F3; font-weight: bold;")
        btn_layout.addWidget(local_workers_label)
        self.full_local_workers_spin = QSpinBox()
        self.full_local_workers_spin.setRange(1, 32)
        self.full_local_workers_spin.setValue(8)
        self.full_local_workers_spin.setToolTip("本地导入的并行线程数，建议4~16")
        self.full_local_workers_spin.setFixedWidth(60)
        btn_layout.addWidget(self.full_local_workers_spin)

        self.full_stop_btn = QPushButton("停止")
        self.full_stop_btn.setEnabled(False)
        self.full_stop_btn.clicked.connect(self.stop_full_increment)
        btn_layout.addWidget(self.full_stop_btn)

        btn_layout.addStretch()

        self.full_close_btn = QPushButton("关闭")
        self.full_close_btn.clicked.connect(self.close)
        btn_layout.addWidget(self.full_close_btn)

        layout.addLayout(btn_layout)

    def on_method_changed(self):
        """切换股票选择方式"""
        self.preset_frame.setVisible(self.preset_radio.isChecked())
        self.file_frame.setVisible(self.file_radio.isChecked())
        self.manual_frame.setVisible(self.manual_radio.isChecked())

    def browse_stock_file(self):
        """浏览股票文件"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择股票列表文件",
            "", "CSV文件 (*.csv);;所有文件 (*.*)"
        )
        if file_path:
            self.file_path_edit.setText(file_path)

    def get_stock_list(self) -> List[str]:
        """获取股票列表"""
        stocks = []

        if self.preset_radio.isChecked():
            # 从预设板块获取
            legacy_dirs = [
                os.path.join(os.path.dirname(os.path.dirname(__file__)), 'stock_lists'),
                os.path.join(os.path.dirname(__file__), 'stock_lists'),
            ]

            # 板块文件映射
            preset_files = {
                'all_a': '沪深A股_股票列表.csv',
                'sh_a': '上证A股_股票列表.csv',
                'sz_a': '深证A股_股票列表.csv',
                'hs300': '沪深300成分股_股票列表.csv',
                'sz50': '上证50成分股_股票列表.csv',
                'zz500': '中证500成分股_股票列表.csv',
                'cyb': '创业板_股票列表.csv',
                'kcb': '科创板_股票列表.csv',
                'hs_etf': '沪深ETF_成分股列表.csv',
                'hs_fund': '沪深基金_列表.csv',
                'hs_convertible_bonds': '沪深转债_列表.csv',
                't0_etf': 'T0型ETF.csv',
                'common_index': '指数_股票列表.csv',
            }

            for key, cb in self.preset_checks.items():
                if cb.isChecked() and key in preset_files:
                    file_path = _resolve_preset_stock_pool_file(
                        preset_files[key], legacy_dirs
                    )
                    if file_path:
                        stocks.extend(self._read_stock_file(file_path))
                    else:
                        # 文件不存在时记录警告
                        logging.warning(f"股票列表文件不存在: {preset_files[key]}")

        elif self.file_radio.isChecked():
            # 从文件获取
            file_path = self.file_path_edit.text().strip()
            if file_path and os.path.exists(file_path):
                stocks = self._read_stock_file(file_path)

        elif self.manual_radio.isChecked():
            # 手动输入
            text = self.manual_edit.toPlainText().strip()
            for line in text.split('\n'):
                code = line.strip()
                if code:
                    # 自动补充市场后缀
                    normalized_code = self._normalize_stock_code_with_market(code)
                    stocks.append(normalized_code)

        # 去重
        return list(set(stocks))

    def _normalize_stock_code_with_market(self, code: str) -> str:
        return _normalize_market_security_code(code)

    def _read_stock_file(self, file_path: str) -> List[str]:
        """读取股票文件"""
        stocks = []
        try:
            # 首先尝试不使用header读取（假设文件没有表头）
            df = pd.read_csv(file_path, dtype=str, header=None, encoding='utf-8-sig')

            # 检查第一行第一列是否像股票代码（包含.SH或.SZ）
            if len(df) > 0 and len(df.columns) > 0:
                first_cell = str(df.iloc[0, 0])
                if '.SH' in first_cell or '.SZ' in first_cell or '.BJ' in first_cell:
                    # 第一行是数据，使用无表头模式
                    raw_stocks = df.iloc[:, 0].dropna().tolist()
                else:
                    # 第一行可能是表头，重新读取并寻找代码列
                    df = pd.read_csv(file_path, dtype=str, encoding='utf-8-sig')
                    raw_stocks = []
                    for col in df.columns:
                        if '代码' in col or 'code' in col.lower():
                            raw_stocks.extend(df[col].dropna().tolist())
                            break
                    else:
                        # 没有找到代码列，使用第一列
                        if len(df.columns) > 0:
                            raw_stocks.extend(df.iloc[:, 0].dropna().tolist())

                # 对所有读取的股票代码进行规范化处理
                for code in raw_stocks:
                    normalized_code = self._normalize_stock_code_with_market(code)
                    stocks.append(normalized_code)

        except Exception as e:
            self.log(f"读取文件失败: {e}")
        return stocks

    def log(self, message: str):
        """添加日志"""
        _append_download_log(self.log_text, f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def start_import(self):
        """开始自定义增量补充"""
        if self._is_any_import_running():
            QMessageBox.warning(self, "提示", "已有数据任务正在运行，请等待完成或先停止当前任务")
            return
        # 立即禁用按钮，防止重复点击
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        try:
            stocks = self.get_stock_list()

            if not stocks:
                QMessageBox.warning(self, "提示", "请选择要导入的股票")
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                return

            # 获取周期配置
            periods_config = self.get_custom_periods_config()
            if not periods_config:
                QMessageBox.warning(self, "提示", "请至少选择一个数据周期")
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                return
            if not self._validate_history_ranges(periods_config):
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                return
            available, reason, source_details = self._history_source_probe()
            if not available:
                QMessageBox.warning(
                    self,
                    "历史行情源未就绪",
                    f"当前选择：{self.history_source}\n{reason}\n"
                    "请先打开并登录对应客户端/桥接后重试；不会自动切换数据源。",
                )
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                return
            self._persist_history_import_config()

            self._prepare_indicator_pipeline(
                'custom',
                self.custom_baostock_indicators_check.isChecked(),
                stocks,
                periods_config,
            )
            self._set_pipeline_start_buttons_enabled(False)

            self.log(f"准备扫描并补充 {len(stocks)} 只股票的数据")
            if self.manual_radio.isChecked():
                self.log("手动代码将直接下载，不按当前上市股票池过滤；退市证券请将日期设为其历史交易期")
            self.log(f"选中周期: {', '.join(periods_config.keys())}")
            for period, (start, end) in periods_config.items():
                self.log(f"  {period}: {start} ~ {end}")

            # 更新UI状态
            self.progress_bar.setValue(0)
            self.custom_phase_label.setText("阶段: 扫描中")
            self.import_status_label.setText("正在启动...")
            self.import_countdown_label.setText("")
            self.custom_tasks_label.setText("任务: 0")
            self.custom_completed_label.setText("已完成: 0")

            # 重置计时器
            self._scan_start_time = None

            force_overwrite = self.custom_force_overwrite_check.isChecked()
            if force_overwrite:
                self.clear_custom_progress()
            else:
                self.load_custom_progress()

            dividend_types = self._get_selected_dividend_types(
                self.custom_dividend_none_check,
                self.custom_dividend_front_check,
                self.custom_dividend_back_check,
                self.custom_dividend_front_ratio_check,
                self.custom_dividend_back_ratio_check
            )

            num_dl_workers = 1 if self.history_source == QMT_NATIVE else self.custom_dl_workers_spin.value()
            self.custom_increment_thread = CustomIncrementThread(
                self.manager, stocks, periods_config, self.custom_completed_tasks, force_overwrite, dividend_types,
                num_dl_workers=num_dl_workers, source=self.history_source,
                bridge_dir=self.bridge_dir,
                max_task_retries=self.max_task_retries,
                retry_backoff=self.retry_backoff,
                instance_generation=(
                    _history_instance_generation(source_details)
                    if self.history_source == QMT_NATIVE else None
                ),
                **self.native_execution_options(),
            )
            self.custom_increment_thread.scan_progress.connect(self.on_custom_scan_progress)
            self.custom_increment_thread.scan_log.connect(self.log)
            self.custom_increment_thread.scan_finished.connect(self.on_custom_scan_finished)
            self.custom_increment_thread.download_progress.connect(self.on_custom_download_progress)
            self.custom_increment_thread.download_log.connect(self.log)
            self.custom_increment_thread.task_completed.connect(self.on_custom_task_completed)
            self.custom_increment_thread.lock_conflict.connect(
                lambda info: self.on_increment_lock_conflict("custom", info)
            )
            self.custom_increment_thread.enable_lock_prompt()
            self.custom_increment_thread.finished.connect(self.on_custom_finished)
            self.custom_increment_thread.error.connect(self.on_custom_error)
            self.custom_increment_thread.start()  #需要用start()启动
        except Exception as e:
            # 发生异常时恢复按钮状态
            self._set_pipeline_start_buttons_enabled(True)
            self.stop_btn.setEnabled(False)
            self._indicator_pipeline_context = None
            self.log(f"启动失败: {e}")
            QMessageBox.warning(self, "错误", f"启动失败: {e}")

    def stop_import(self):
        """停止自定义增量补充"""
        context = self._indicator_pipeline_context or {}
        if (
            context.get('scope') == 'custom'
            and self.indicator_thread
            and self.indicator_thread.isRunning()
        ):
            self._pipeline_stop_requested = True
            self.indicator_thread.stop()
            self.stop_btn.setEnabled(False)
            self.custom_phase_label.setText("阶段: 正在停止指标补充")
            self.import_status_label.setText("等待当前BaoStock请求完成...")
            self.log("正在停止BaoStock指标补充，当前请求结束后停止...")
            return

        if hasattr(self, 'custom_increment_thread') and self.custom_increment_thread and self.custom_increment_thread.isRunning():
            self._pipeline_stop_requested = True
            self.custom_increment_thread.stop()
            self.stop_btn.setEnabled(False)
            self.log("正在停止...")
            self.save_custom_progress(force=True)
            self.custom_phase_label.setText("阶段: 正在停止")
            self.import_status_label.setText("等待当前任务结束...")
            self.import_countdown_label.setText("")
            self.log("停止请求已发送，等待当前任务结束...")

    def on_import_finished(self, results: dict):
        """导入完成"""
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

        self.log("=" * 40)
        self.log(f"导入完成!")
        self.log(f"成功: {results['success']} 只")
        self.log(f"空数据: {results['empty']} 只")
        self.log(f"失败: {results['failed']} 只")
        self.log(f"总记录数: {results['total_records']}")
        self.log("=" * 40)

        # 导入完成后，通知父窗口刷新列表
        if self.parent() and hasattr(self.parent(), 'on_refresh_clicked'):
            try:
                self.parent().on_refresh_clicked()
                self.log("股票列表已自动刷新")
            except Exception as e:
                self.log(f"自动刷新列表失败: {e}")

        QMessageBox.information(
            self, "导入完成",
            f"成功导入 {results['success']} 只股票\n"
            f"共 {results['total_records']} 条记录"
        )
        # 清除 ETA 与状态
        try:
            self._import_start_time = None
            self.import_countdown_label.setText("")
            self.import_status_label.setText("完成")
        except Exception:
            pass

    def on_import_progress(self, value: int, completed: int = 0, total: int = 0):
        """处理常规导入的进度回调，计算并显示 ETA"""
        try:
            # 更新进度条
            self.progress_bar.setValue(value)

            # 更新进度文本显示 (已完成/总数)
            if total > 0:
                self.import_status_label.setText(f"进度: {completed}/{total}")

            # 如果尚未记录开始时间，则不计算
            if not hasattr(self, '_import_start_time') or self._import_start_time is None:
                return

            # 当进度大于0时根据百分比估算剩余时间
            if value > 0:
                elapsed = (datetime.now() - self._import_start_time).total_seconds()
                if elapsed > 0 and value > 0:
                    eta_seconds = int(elapsed * (100 - value) / value)
                    self._set_import_countdown(eta_seconds, "导入")
                else:
                    self.import_countdown_label.setText("估算中...")
            else:
                self.import_countdown_label.setText("估算中...")
        except Exception as e:
            logging.debug(f"on_import_progress 异常: {e}")

    def on_import_status(self, message: str):
        """处理导入状态更新，输出到日志窗口"""
        try:
            # 输出到日志窗口
            self.log(message)
        except Exception as e:
            logging.debug(f"on_import_status 异常: {e}")

    def _set_import_countdown(self, eta_seconds: int, phase: str):
        """内部：根据秒数设置导入 ETA 文本"""
        try:
            if eta_seconds <= 0:
                self.import_countdown_label.setText("")
                return
            hours = eta_seconds // 3600
            minutes = (eta_seconds % 3600) // 60
            seconds = eta_seconds % 60
            if hours > 0:
                text = f"{phase}剩余: {hours}时{minutes}分{seconds}秒"
            elif minutes > 0:
                text = f"{phase}剩余: {minutes}分{seconds}秒"
            else:
                text = f"{phase}剩余: {seconds}秒"
            self.import_countdown_label.setText(text)
        except Exception:
            pass

    def on_import_error(self, error: str):
        """导入错误"""
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.log(f"错误: {error}")
        QMessageBox.warning(self, "导入错误", error)

    def _show_scan_report_non_modal(self, report, attr_name: str):
        """显示扫描报告但不阻塞导入线程/停止按钮。

        旧实现使用 ``QDialog.exec_``，扫描完成后会进入一个嵌套事件循环；
        用户看到报告时无法可靠点击主窗口的停止按钮，原生 QMT 请求也就
        只能等到桥接端超时。报告本身只读，因此使用普通 ``show``，并保留
        一个实例避免被 Python/Qt 提前回收。
        """
        if not report:
            return
        previous = getattr(self, attr_name, None)
        if previous is not None:
            try:
                previous.close()
                previous.deleteLater()
            except Exception:
                pass
        try:
            dialog = ScanReportDialog(report, self)
            dialog.setAttribute(Qt.WA_DeleteOnClose, True)
            setattr(self, attr_name, dialog)

            def _clear_report_dialog(*_args):
                if getattr(self, attr_name, None) is dialog:
                    setattr(self, attr_name, None)

            dialog.finished.connect(_clear_report_dialog)
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
        except Exception as exc:
            # 报告是辅助信息，创建失败不能影响后续下载。
            logging.debug("显示扫描报告失败: %s", exc)

    # ========== 自定义增量补充相关方法 ==========

    def on_custom_scan_progress(self, current: int, total: int, message: str, found_tasks: int):
        """自定义扫描进度更新"""
        # 第一次调用时设置开始时间
        if not hasattr(self, '_scan_start_time') or self._scan_start_time is None:
            self._scan_start_time = datetime.now()
        if not _should_update_download_ui(self, '_last_custom_scan_progress_ui_ts', current, total):
            return
        percent = int(current * 100 / total) if total > 0 else 0
        self.progress_bar.setValue(percent)
        self.import_status_label.setText(message)
        self.custom_tasks_label.setText(f"任务: {found_tasks}")

        # 计算扫描预计剩余时间
        if current > 0:
            elapsed = (datetime.now() - self._scan_start_time).total_seconds()
            if elapsed > 0:
                avg_time = elapsed / current
                eta_seconds = int(avg_time * (total - current))
                self._update_custom_countdown(eta_seconds, "扫描")

    def on_custom_scan_finished(self, total_tasks: int, total_missing: int, report: dict):
        """自定义扫描完成"""
        self.custom_tasks_label.setText(f"任务: {total_tasks}")
        self.custom_phase_label.setText("阶段: 下载中")
        self.progress_bar.setValue(0)
        self._download_start_time = datetime.now()

        # 保存报告供后续使用
        self._last_custom_scan_report = report

        # 弹出可视化报告对话框
        if report and total_tasks > 0:
            self._show_scan_report_non_modal(report, '_custom_scan_report_dialog')

    def on_custom_download_progress(self, current: int, total: int, message: str, eta_seconds: int):
        """自定义下载进度更新"""
        if not _should_update_download_ui(self, '_last_custom_download_progress_ui_ts', current, total):
            return
        percent = int(current * 100 / total) if total > 0 else 0
        self.progress_bar.setValue(percent)
        self.import_status_label.setText(message)
        self._update_custom_countdown(eta_seconds, "下载")

    def _update_custom_countdown(self, eta_seconds: int, phase: str):
        """更新自定义补充的倒计时显示"""
        if eta_seconds > 0:
            hours = eta_seconds // 3600
            minutes = (eta_seconds % 3600) // 60
            seconds = eta_seconds % 60
            if hours > 0:
                self.import_countdown_label.setText(f"预计剩余: {hours}小时{minutes}分{seconds}秒")
            elif minutes > 0:
                self.import_countdown_label.setText(f"预计剩余: {minutes}分{seconds}秒")
            else:
                self.import_countdown_label.setText(f"预计剩余: {seconds}秒")
        else:
            self.import_countdown_label.setText("")

    def on_custom_task_completed(self, task: dict):
        """自定义单个任务完成"""
        task_key = task.get('_progress_key') or _increment_task_key(
            task,
            _task_dividend_types(
                task.get('period'),
                getattr(self.custom_increment_thread, 'dividend_types', None),
            ),
            getattr(self.custom_increment_thread, 'force_overwrite', False),
            getattr(self.custom_increment_thread, 'source', self.history_source),
        )
        self.custom_completed_tasks.add(task_key)
        self.custom_completed_label.setText(f"已完成: {len(self.custom_completed_tasks)}")
        self.save_custom_progress()

    def _should_save_progress_snapshot(self, prefix: str, count: int, force: bool = False) -> bool:
        """进度文件保存节流：避免任务越多越频繁重写巨大 JSON。"""
        if force:
            return True
        import time
        now = time.time()
        last_count = getattr(self, f'_{prefix}_progress_last_count', -1)
        last_ts = getattr(self, f'_{prefix}_progress_last_ts', 0.0)
        if last_count < 0:
            setattr(self, f'_{prefix}_progress_last_count', count)
            setattr(self, f'_{prefix}_progress_last_ts', now)
            return True
        # 大任务下每 50 个任务或至少 8 秒保存一次，避免 O(n^2) 文件写入拖慢下载。
        return (count - last_count) >= 50 or (now - last_ts) >= 8.0

    def _mark_progress_snapshot_saved(self, prefix: str, count: int):
        import time
        setattr(self, f'_{prefix}_progress_last_count', count)
        setattr(self, f'_{prefix}_progress_last_ts', time.time())

    def _atomic_write_progress_json(self, progress_file: str, data: dict):
        """先写临时文件再原子替换，避免程序中断时留下半截 JSON。"""
        import json
        import os
        tmp_file = f"{progress_file}.tmp"
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
            f.write('\n')
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp_file, progress_file)

    def _quarantine_bad_progress_file(self, progress_file: str, log_func, error: Exception):
        """坏进度文件自动隔离，避免每次打开模块都重复报错。"""
        import os
        from datetime import datetime
        try:
            bad_file = f"{progress_file}.bad_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            os.replace(progress_file, bad_file)
            log_func(f"加载进度失败: {error}，已将损坏进度文件改名为 {os.path.basename(bad_file)}")
        except Exception as move_err:
            log_func(f"加载进度失败: {error}；损坏文件改名失败: {move_err}")

    def on_custom_finished(self, results: dict):
        """自定义增量补充完成"""
        cancelled = bool(results.get('cancelled'))
        # ``finished`` is delivered to the dialog's Qt GUI thread.  Keep the
        # widget update here (rather than touching a progress bar from the
        # worker thread) and make the no-task/last-task outcome explicit.
        if not cancelled:
            self.progress_bar.setValue(100)
        self._persist_history_import_config({
            "status": "cancelled" if cancelled else "completed",
            "result": dict(results or {}),
        })
        self.custom_phase_label.setText("阶段: 已停止" if cancelled else "阶段: 行情完成")
        source_name = _history_source_display_name(self.history_source)
        self.import_status_label.setText(
            "已停止" if cancelled else f"{source_name}行情完成"
        )
        self.import_countdown_label.setText("")

        self.log("=" * 40)
        self.log("补充已停止" if cancelled else "补充完成!")
        self.log(f"成功: {results['success']} 个任务")
        self.log(f"跳过: {results.get('skipped', 0)} 个任务")
        self.log(f"失败: {results['failed']} 个任务")
        self.log(f"总记录数: {results['total_records']}")
        lock_skipped = results.get('lock_skipped', [])
        if lock_skipped:
            self.log(f"因数据库占用跳过: {len(lock_skipped)} 个任务")
            for item in lock_skipped:
                self.log(f"  - {item.get('stock')} {item.get('period')} (PID {item.get('pid') or '未知'})")
        self.log("=" * 40)

        if cancelled:
            self.save_custom_progress(force=True)
        else:
            self.clear_custom_progress()
        self._continue_after_market_stage('custom', results)

    def on_custom_error(self, error: str):
        """自定义增量补充错误"""
        self._persist_history_import_config({"status": "error", "error": str(error)})
        self._set_pipeline_start_buttons_enabled(True)
        self.stop_btn.setEnabled(False)
        self._indicator_pipeline_context = None
        self._pipeline_stop_requested = False
        self.custom_phase_label.setText("阶段: 错误")
        self.import_status_label.setText("错误")
        self.import_countdown_label.setText("")
        self.log(f"错误: {error}")
        if self._close_after_stop:
            self._schedule_close_when_idle()
        else:
            QMessageBox.warning(self, "补充错误", error)

    def load_custom_progress(self):
        """加载自定义补充的进度（断点续传）"""
        if not hasattr(self, 'custom_completed_tasks'):
            self.custom_completed_tasks = set()

        progress_file = os.path.join(self.manager.data_root, 'custom_increment_progress.json')
        if os.path.exists(progress_file):
            try:
                with open(progress_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    raw_tasks = set(data.get('completed_tasks', []))
                    self.custom_completed_tasks = {
                        key for key in raw_tasks
                        if isinstance(key, str) and key.startswith('v2|')
                    }
                    discarded = len(raw_tasks) - len(self.custom_completed_tasks)
                    if discarded:
                        self.log(
                            f"旧版断点记录缺少复权/覆写配置，已安全失效 {discarded} 项"
                        )
                    self.log(f"已加载 {len(self.custom_completed_tasks)} 个已完成任务（断点续传）")
            except Exception as e:
                self._quarantine_bad_progress_file(progress_file, self.log, e)
                self.custom_completed_tasks = set()
        else:
            self.custom_completed_tasks = set()
        self.custom_completed_label.setText(f"已完成: {len(self.custom_completed_tasks)}")

    def save_custom_progress(self, force: bool = False):
        """保存自定义补充的进度"""
        if not hasattr(self, 'custom_completed_tasks'):
            return

        progress_file = os.path.join(self.manager.data_root, 'custom_increment_progress.json')
        count = len(self.custom_completed_tasks)
        if not self._should_save_progress_snapshot('custom', count, force=force):
            return
        try:
            data = {
                'version': 2,
                # QThread 可能同时补充/校验任务键；先用 C 层 set.copy()
                # 获取稳定快照，避免 JSON 序列化期间集合大小变化。
                'completed_tasks': sorted(self.custom_completed_tasks.copy()),
                'save_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            self._atomic_write_progress_json(progress_file, data)
            self._mark_progress_snapshot_saved('custom', count)
            if force or count % 500 == 0:
                self.log(f"进度已保存（{count} 个任务）")
        except Exception as e:
            self.log(f"保存进度失败: {e}")

    def clear_custom_progress(self):
        """清除自定义补充的进度文件"""
        progress_file = os.path.join(self.manager.data_root, 'custom_increment_progress.json')
        if os.path.exists(progress_file):
            try:
                os.remove(progress_file)
            except Exception:
                pass
        if hasattr(self, 'custom_completed_tasks'):
            self.custom_completed_tasks.clear()

    # ========== 全量增量补充相关方法 ==========

    def get_full_periods_config(self) -> Dict[str, tuple]:
        """获取全量增量补充的周期配置"""
        config = {}
        if self.full_period_1d_check.isChecked():
            config['1d'] = (
                self.full_period_1d_start.date().toString("yyyyMMdd"),
                self.full_period_1d_end.date().toString("yyyyMMdd")
            )
        if self.full_period_1m_check.isChecked():
            config['1m'] = (
                self.full_period_1m_start.date().toString("yyyyMMdd"),
                self.full_period_1m_end.date().toString("yyyyMMdd")
            )
        if self.full_period_5m_check.isChecked():
            config['5m'] = (
                self.full_period_5m_start.date().toString("yyyyMMdd"),
                self.full_period_5m_end.date().toString("yyyyMMdd")
            )
        if self.full_period_tick_check.isChecked():
            config['tick'] = (
                self.full_period_tick_start.date().toString("yyyyMMdd"),
                self.full_period_tick_end.date().toString("yyyyMMdd")
            )
        return config

    def get_custom_periods_config(self) -> Dict[str, tuple]:
        """获取自定义补充的周期配置"""
        config = {}
        if self.custom_period_1d_check.isChecked():
            config['1d'] = (
                self.custom_period_1d_start.date().toString("yyyyMMdd"),
                self.custom_period_1d_end.date().toString("yyyyMMdd")
            )
        if self.custom_period_1m_check.isChecked():
            config['1m'] = (
                self.custom_period_1m_start.date().toString("yyyyMMdd"),
                self.custom_period_1m_end.date().toString("yyyyMMdd")
            )
        if self.custom_period_5m_check.isChecked():
            config['5m'] = (
                self.custom_period_5m_start.date().toString("yyyyMMdd"),
                self.custom_period_5m_end.date().toString("yyyyMMdd")
            )
        if self.custom_period_tick_check.isChecked():
            config['tick'] = (
                self.custom_period_tick_start.date().toString("yyyyMMdd"),
                self.custom_period_tick_end.date().toString("yyyyMMdd")
            )
        return config

    def _get_selected_dividend_types(
        self,
        none_check: QCheckBox,
        front_check: QCheckBox,
        back_check: QCheckBox,
        front_ratio_check: QCheckBox,
        back_ratio_check: QCheckBox
    ):
        selected = []
        if front_check.isChecked():
            selected.append('front')
        if back_check.isChecked():
            selected.append('back')
        if front_ratio_check.isChecked():
            selected.append('front_ratio')
        if back_ratio_check.isChecked():
            selected.append('back_ratio')
        return selected

    def start_full_increment(self):
        if self._is_any_import_running():
            QMessageBox.warning(self, "提示", "已有数据任务正在运行，请等待完成或先停止当前任务")
            return
        periods_config = self.get_full_periods_config()
        if not periods_config:
            QMessageBox.warning(self, "提示", "请至少选择一个数据周期")
            return
        if not self._validate_history_ranges(periods_config):
            return
        available, reason, source_details = self._history_source_probe()
        if not available:
            QMessageBox.warning(
                self, "历史行情源未就绪",
                f"当前选择：{self.history_source}\n{reason}\n"
                "请先打开并登录对应客户端/桥接后重试；不会自动切换数据源。",
            )
            return
        self._persist_history_import_config()

        self._prepare_indicator_pipeline(
            'full',
            self.full_baostock_indicators_check.isChecked(),
            [],
            periods_config,
        )

        # 禁用按钮
        self._set_pipeline_start_buttons_enabled(False)
        self.full_stop_btn.setEnabled(True)
        self.full_progress_bar.setValue(0)
        self.full_phase_label.setText("阶段: 扫描中")
        self.full_status_label.setText("正在启动...")
        self.full_countdown_label.setText("")
        self.full_tasks_label.setText("任务: 0")

        # 重置计时器
        self._scan_start_time = None

        force_overwrite = self.full_force_overwrite_check.isChecked()
        if force_overwrite:
            self.clear_full_progress()

        dividend_types = self._get_selected_dividend_types(
            self.full_dividend_none_check,
            self.full_dividend_front_check,
            self.full_dividend_back_check,
            self.full_dividend_front_ratio_check,
            self.full_dividend_back_ratio_check
        )

        try:
            num_dl_workers = 1 if self.history_source == QMT_NATIVE else self.full_dl_workers_spin.value()
            self.full_increment_thread = FullIncrementThread(
                self.manager, periods_config, self.completed_tasks, force_overwrite, dividend_types,
                num_dl_workers=num_dl_workers, source=self.history_source,
                bridge_dir=self.bridge_dir,
                max_task_retries=self.max_task_retries,
                retry_backoff=self.retry_backoff,
                instance_generation=(
                    _history_instance_generation(source_details)
                    if self.history_source == QMT_NATIVE else None
                ),
                **self.native_execution_options(),
            )
            self.full_increment_thread.scan_progress.connect(self.on_full_scan_progress)
            self.full_increment_thread.scan_log.connect(self.full_log)
            self.full_increment_thread.scan_finished.connect(self.on_full_scan_finished)
            self.full_increment_thread.download_progress.connect(self.on_full_download_progress)
            self.full_increment_thread.download_log.connect(self.full_log)
            self.full_increment_thread.task_completed.connect(self.on_full_task_completed)
            self.full_increment_thread.lock_conflict.connect(
                lambda info: self.on_increment_lock_conflict("full", info)
            )
            self.full_increment_thread.enable_lock_prompt()
            self.full_increment_thread.finished.connect(self.on_full_finished)
            self.full_increment_thread.error.connect(self.on_full_error)
            self.full_increment_thread.start()
        except Exception as exc:
            self.full_increment_thread = None
            self.on_full_error(f"启动失败: {exc}")

    def start_full_import_local(self):
        try:
            self.history_source = _canonical_history_source(self.history_source)
            self.source = self.history_source
        except Exception as exc:
            QMessageBox.warning(
                self,
                "历史行情源无效",
                f"{exc}\n请在来源下拉框中明确选择 MiniQMT 或大QMT原生桥。",
            )
            return
        if self.history_source == QMT_NATIVE:
            QMessageBox.information(
                self,
                "原生大QMT不支持本地缓存按钮",
                "当前选择为大QMT原生桥。请使用“开始扫描并补充”，"
                "它会通过原生桥读取/下载，不会调用 MiniQMT 本地缓存。",
            )
            return
        if self._is_any_import_running():
            QMessageBox.warning(self, "提示", "已有数据任务正在运行，请等待完成或先停止当前任务")
            return
        periods_config = self.get_full_periods_config()
        if not periods_config:
            QMessageBox.warning(self, "提示", "请至少选择一个数据周期")
            return
        self._prepare_indicator_pipeline(
            'full_local',
            self.full_baostock_indicators_check.isChecked(),
            [],
            periods_config,
        )
        self.full_log(f"选中周期: {', '.join(periods_config.keys())}")
        for period, (start, end) in periods_config.items():
            self.full_log(f"周期范围: {period} {start}~{end}")
        dividend_types = self._get_selected_dividend_types(
            self.full_dividend_none_check,
            self.full_dividend_front_check,
            self.full_dividend_back_check,
            self.full_dividend_front_ratio_check,
            self.full_dividend_back_ratio_check
        )
        self._set_pipeline_start_buttons_enabled(False)
        self.full_stop_btn.setEnabled(True)
        self.full_progress_bar.setValue(0)
        self.full_phase_label.setText("阶段: 本地导入")
        self.full_status_label.setText("正在启动...")
        self.full_countdown_label.setText("")
        self.full_tasks_label.setText("任务: 0")
        self.full_completed_label.setText("已完成: 0")

        try:
            num_workers = self.full_local_workers_spin.value()
            self.full_import_local_thread = LocalMiniQMTImportThread(
                self.manager, periods_config, dividend_types, num_workers=num_workers,
                source=self.history_source,
                bridge_dir=self.bridge_dir,
                max_task_retries=self.max_task_retries,
                retry_backoff=self.retry_backoff,
            )
            self.full_import_local_thread.progress.connect(self.on_full_local_progress)
            self.full_import_local_thread.log.connect(self.full_log)
            self.full_import_local_thread.finished.connect(self.on_full_local_finished)
            self.full_import_local_thread.error.connect(self.on_full_error)
            self.full_import_local_thread.start()
        except Exception as exc:
            self.full_import_local_thread = None
            self.on_full_error(f"启动失败: {exc}")

    def stop_full_increment(self):
        context = self._indicator_pipeline_context or {}
        if (
            context.get('scope') in ('full', 'full_local')
            and self.indicator_thread
            and self.indicator_thread.isRunning()
        ):
            self._pipeline_stop_requested = True
            self.indicator_thread.stop()
            self.full_stop_btn.setEnabled(False)
            self.full_phase_label.setText("阶段: 正在停止指标补充")
            self.full_status_label.setText("等待当前BaoStock请求完成...")
            self.full_log("正在停止BaoStock指标补充，当前请求结束后停止...")
            return

        if self.full_increment_thread and self.full_increment_thread.isRunning():
            self._pipeline_stop_requested = True
            self.full_increment_thread.stop()
            self.full_stop_btn.setEnabled(False)
            self.full_log("正在停止...")
            self.save_full_progress(force=True)
            self.full_phase_label.setText("阶段: 正在停止")
            self.full_status_label.setText("等待当前任务结束...")
            self.full_countdown_label.setText("")
            self.full_log("停止请求已发送，等待当前任务结束...")
            return

        if self.full_import_local_thread and self.full_import_local_thread.isRunning():
            self._pipeline_stop_requested = True
            self.full_import_local_thread.stop()
            self.full_stop_btn.setEnabled(False)
            self.full_log("正在停止...")
            self.full_phase_label.setText("阶段: 正在停止")
            self.full_status_label.setText("等待当前任务结束...")
            self.full_countdown_label.setText("")
            self.full_log("停止请求已发送，等待当前任务结束...")

    def full_log(self, message: str):
        """添加全量增量补充日志"""
        _append_download_log(self.full_log_text, f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def on_full_scan_progress(self, current: int, total: int, message: str, found_tasks: int):
        """扫描进度更新"""
        # 第一次调用时设置开始时间
        if not hasattr(self, '_scan_start_time') or self._scan_start_time is None:
            self._scan_start_time = datetime.now()
        if not _should_update_download_ui(self, '_last_full_scan_progress_ui_ts', current, total):
            return
        percent = int(current * 100 / total) if total > 0 else 0
        self.full_progress_bar.setValue(percent)
        self.full_status_label.setText(message)
        self.full_tasks_label.setText(f"任务: {found_tasks}")

        # 计算扫描预计剩余时间
        if current > 0:
            elapsed = (datetime.now() - self._scan_start_time).total_seconds()
            if elapsed > 0:
                avg_time = elapsed / current
                eta_seconds = int(avg_time * (total - current))
                self._update_countdown(eta_seconds, "扫描")

    def on_full_scan_finished(self, total_tasks: int, total_missing: int, report: dict):
        """扫描完成"""
        self.full_tasks_label.setText(f"任务: {total_tasks}")
        self.full_phase_label.setText("阶段: 下载中")
        self.full_progress_bar.setValue(0)
        self._download_start_time = datetime.now()

        # 保存报告供后续使用
        self._last_scan_report = report

        # 弹出可视化报告对话框
        if report and total_tasks > 0:
            self._show_scan_report_non_modal(report, '_full_scan_report_dialog')

    def on_full_download_progress(self, current: int, total: int, message: str, eta_seconds: int):
        """下载进度更新"""
        if not _should_update_download_ui(self, '_last_full_download_progress_ui_ts', current, total):
            return
        percent = int(current * 100 / total) if total > 0 else 0
        self.full_progress_bar.setValue(percent)
        self.full_status_label.setText(message)
        self._update_countdown(eta_seconds, "下载")

    def _update_countdown(self, eta_seconds: int, phase: str):
        """更新倒计时显示"""
        if eta_seconds <= 0:
            self.full_countdown_label.setText("")
            return

        hours = eta_seconds // 3600
        minutes = (eta_seconds % 3600) // 60
        seconds = eta_seconds % 60

        if hours > 0:
            countdown_text = f"{phase}剩余: {hours}时{minutes}分{seconds}秒"
        elif minutes > 0:
            countdown_text = f"{phase}剩余: {minutes}分{seconds}秒"
        else:
            countdown_text = f"{phase}剩余: {seconds}秒"

        self.full_countdown_label.setText(countdown_text)

    def on_full_task_completed(self, task: dict):
        """单个任务完成"""
        task_key = task.get('_progress_key') or _increment_task_key(
            task,
            _task_dividend_types(
                task.get('period'),
                getattr(self.full_increment_thread, 'dividend_types', None),
            ),
            getattr(self.full_increment_thread, 'force_overwrite', False),
            getattr(self.full_increment_thread, 'source', self.history_source),
        )
        self.completed_tasks.add(task_key)
        self.full_completed_label.setText(f"已完成: {len(self.completed_tasks)}")
        self.save_full_progress()

    def on_increment_lock_conflict(self, scope: str, info: dict):
        """自动重试耗尽后，在主线程询问如何处理单只股票文件。"""
        thread = (
            self.custom_increment_thread if scope == "custom"
            else self.full_increment_thread
        )
        if thread is None:
            return
        stock = info.get("stock") or "未知证券"
        period = info.get("period") or "未知周期"
        pid = info.get("pid")
        process = info.get("process") or "其他进程"
        path = info.get("db_path") or ""

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("数据库文件正在使用")
        box.setText(f"{stock}（{period}）暂时无法写入")
        detail_lines = [
            "系统已自动重试 5 次，但文件仍被其他任务占用。",
            f"占用进程：{process}" + (f"（PID {pid}）" if pid else ""),
        ]
        if path:
            detail_lines.append(f"文件：{path}")
        detail_lines.append("跳过后，本任务结束时会列入未补充清单，且不会记为已完成。")
        box.setInformativeText("\n".join(detail_lines))
        retry_button = box.addButton("继续重试", QMessageBox.AcceptRole)
        skip_button = box.addButton("先跳过", QMessageBox.ActionRole)
        abort_button = box.addButton("停止任务", QMessageBox.RejectRole)
        box.setDefaultButton(skip_button)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is retry_button:
            thread.set_lock_resolution("retry")
        elif clicked is abort_button:
            thread.set_lock_resolution("abort")
        else:
            thread.set_lock_resolution("skip")

    def on_full_finished(self, results: dict):
        cancelled = bool(results.get('cancelled'))
        # ``finished`` is delivered to the dialog's Qt GUI thread.  This also
        # covers a scan that found no missing tasks, for which no download
        # progress signal is emitted.
        if not cancelled:
            self.full_progress_bar.setValue(100)
        self._persist_history_import_config({
            "status": "cancelled" if cancelled else "completed",
            "result": dict(results or {}),
        })
        self.full_phase_label.setText("阶段: 已停止" if cancelled else "阶段: 行情完成")
        self.full_countdown_label.setText("")

        self.full_log("=" * 50)
        self.full_log("全量增量补充已停止" if cancelled else "全量增量补充完成!")
        self.full_log(f"成功: {results['success']}")
        self.full_log(f"失败: {results['failed']}")
        self.full_log(f"跳过: {results['skipped']}")
        self.full_log(f"总记录数: {results['total_records']}")
        lock_skipped = results.get('lock_skipped', [])
        if lock_skipped:
            self.full_log(f"因数据库占用跳过: {len(lock_skipped)}")
            for item in lock_skipped:
                self.full_log(f"  - {item.get('stock')} {item.get('period')} (PID {item.get('pid') or '未知'})")
        self.full_log("=" * 50)

        source_name = _history_source_display_name(self.history_source)
        self.full_status_label.setText(
            "已停止" if cancelled else f"{source_name}行情完成"
        )

        if cancelled:
            self.save_full_progress(force=True)
        else:
            self.clear_full_progress()
        resolved_stocks = getattr(self.full_increment_thread, 'resolved_stocks', [])
        self._continue_after_market_stage('full', results, resolved_stocks)

    def on_full_error(self, error: str):
        self._persist_history_import_config({"status": "error", "error": str(error)})
        self._set_pipeline_start_buttons_enabled(True)
        self.full_stop_btn.setEnabled(False)
        self._indicator_pipeline_context = None
        self._pipeline_stop_requested = False
        self.full_phase_label.setText("阶段: 错误")
        self.full_status_label.setText("错误")
        self.full_log(f"错误: {error}")
        self.full_countdown_label.setText("")
        if self._close_after_stop:
            self._schedule_close_when_idle()
        else:
            QMessageBox.warning(self, "错误", error)

    def on_full_local_progress(self, current: int, total: int, message: str, eta_seconds: int):
        if not _should_update_download_ui(self, '_last_full_local_progress_ui_ts', current, total):
            return
        percent = int(current * 100 / total) if total > 0 else 0
        self.full_progress_bar.setValue(percent)
        self.full_status_label.setText(message)
        self._update_countdown(eta_seconds, "本地导入")

    def on_full_local_finished(self, results: dict):
        self._persist_history_import_config({
            "status": "cancelled" if results.get('cancelled') else "completed",
            "result": dict(results or {}),
            "local_only": True,
        })
        self.full_countdown_label.setText("")
        if results.get('cancelled'):
            self.full_phase_label.setText("阶段: 已停止")
            self.full_status_label.setText("已停止")
            self.full_log("本地导入已停止")
        else:
            self.full_phase_label.setText("阶段: 行情完成")
            self.full_status_label.setText(
                f"{_history_source_display_name(self.history_source)}本地行情完成"
            )
            self.full_log("=" * 50)
            self.full_log("本地导入完成!")
            self.full_log(f"成功: {results.get('success', 0)}")
            self.full_log(f"失败: {results.get('failed', 0)}")
            self.full_log(f"总记录数: {results.get('total_records', 0)}")
            self.full_log("=" * 50)
        resolved_stocks = getattr(self.full_import_local_thread, 'resolved_stocks', [])
        self._continue_after_market_stage('full_local', results, resolved_stocks)

    def get_progress_file_path(self) -> str:
        """获取进度文件路径"""
        return os.path.join(self.manager.data_root, self.PROGRESS_FILE)

    def save_full_progress(self, force: bool = False):
        """保存进度"""
        count = len(self.completed_tasks)
        if not self._should_save_progress_snapshot('full', count, force=force):
            return
        progress_data = {
            'version': 2,
            'last_update': datetime.now().isoformat(),
            'periods_config': self.get_full_periods_config(),
            'completed_tasks': sorted(self.completed_tasks.copy())
        }
        try:
            self._atomic_write_progress_json(self.get_progress_file_path(), progress_data)
            self._mark_progress_snapshot_saved('full', count)
            if force or count % 500 == 0:
                self.full_log(f"进度已保存（{count} 个任务）")
        except Exception as e:
            self.full_log(f"保存进度失败: {e}")

    def load_full_progress(self) -> bool:
        """加载进度"""
        import json
        progress_file = self.get_progress_file_path()
        if not os.path.exists(progress_file):
            return False

        try:
            with open(progress_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            raw_tasks = set(data.get('completed_tasks', []))
            self.completed_tasks = {
                key for key in raw_tasks
                if isinstance(key, str) and key.startswith('v2|')
            }
            discarded = len(raw_tasks) - len(self.completed_tasks)
            if discarded:
                self.full_log(
                    f"旧版断点记录缺少复权/覆写配置，已安全失效 {discarded} 项"
                )
            return len(self.completed_tasks) > 0
        except Exception as e:
            self._quarantine_bad_progress_file(progress_file, self.full_log, e)
            return False

    def clear_full_progress(self):
        """清除进度文件"""
        try:
            progress_file = self.get_progress_file_path()
            if os.path.exists(progress_file):
                os.remove(progress_file)
            self.completed_tasks.clear()
        except Exception:
            pass

    def check_xtquant_connection(self):
        """检查xtquant连接状态"""
        try:
            selected_source = _canonical_history_source(
                getattr(self, "history_source", MINIQMT)
            )
        except Exception as exc:
            # 配置非法时不把 xtquant 的状态伪装成可用来源；保留错误态，
            # 等用户在下拉框中明确迁移/选择。
            self.update_xtquant_indicator(
                "gray",
                f"历史行情源配置无效: {exc}",
                "来源无效",
            )
            self.check_history_source()
            return
        # 原生大 QMT 模式：探测原生桥连接状态并驱动指示灯
        if selected_source == QMT_NATIVE:
            try:
                from kh_qmt_native_bridge.detector import detect_native_bridge, BridgeState
                probe = detect_native_bridge(bridge_dir=self.bridge_dir)
                if probe.state in {BridgeState.READY, "ready"}:
                    pid_str = f" (PID {probe.pid})" if probe.pid else ""
                    self.update_xtquant_indicator(
                        "green",
                        f"大QMT原生桥已连接，策略正常运行{pid_str}",
                        "大QMT已连接",
                    )
                elif probe.state in {BridgeState.BUSY, "busy"}:
                    self.update_xtquant_indicator(
                        "yellow",
                        "大QMT原生桥正忙碌处理中",
                        "大QMT处理中",
                    )
                else:
                    state_desc = probe.state or "未启动"
                    self.update_xtquant_indicator(
                        "red",
                        f"大QMT原生桥未就绪（状态: {state_desc}），请在QMT中启动策略",
                        "大QMT未就绪",
                    )
            except Exception as exc:
                self.update_xtquant_indicator(
                    "red",
                    f"大QMT原生桥探测异常: {exc}",
                    "大QMT未连接",
                )
            self.check_history_source()
            return
        try:
            # 尝试导入khQTTools模块
            if khQTTools is None:
                self.update_xtquant_indicator("red", "khQTTools模块未导入", "模块未导入")
                return

            # 检查xtquant连接
            if hasattr(khQTTools, '_check_xtquant_connection'):
                is_connected = khQTTools._check_xtquant_connection()
                if is_connected:
                    self.update_xtquant_indicator("green", "xtquant已成功连接，可以正常使用", "MiniQMT已连接")
                else:
                    self.update_xtquant_indicator("red", "xtquant未连接，请检查MiniQMT是否启动并登录", "MiniQMT未连接")
            else:
                # 如果没有检查函数，尝试导入xtdata来判断
                try:
                    from xtquant import xtdata
                    # 尝试获取股票列表来测试连接
                    try:
                        test_list = xtdata.get_stock_list_in_sector('沪深A股')
                        if test_list and len(test_list) > 0:
                            self.update_xtquant_indicator("green", "xtquant已成功连接，可以正常使用", "MiniQMT已连接")
                        else:
                            self.update_xtquant_indicator("red", "xtquant未连接，请检查MiniQMT是否启动并登录", "MiniQMT未连接")
                    except:
                        self.update_xtquant_indicator("red", "xtquant未连接，请检查MiniQMT是否启动并登录", "MiniQMT未连接")
                except ImportError:
                    self.update_xtquant_indicator("red", "xtquant模块未安装", "MiniQMT未安装")
        except Exception as e:
            logging.error(f"检查xtquant连接状态时出错: {str(e)}")
            self.update_xtquant_indicator("red", f"状态检查失败: {str(e)}", "检查失败")

        # 来源选择器独立检查；xtquant 状态不能代表大 QMT 原生桥状态。
        self.check_history_source()

    def _on_history_source_changed(self, index: int):
        """切换历史来源，不启动客户端、不清理另一客户端进程。"""
        combo = getattr(self, "history_source_combo", None)
        value = combo.itemData(index) if combo is not None and index >= 0 else None
        previous = self.history_source
        try:
            self.history_source = _canonical_history_source(value, default=previous)
        except Exception as exc:
            self.history_source = previous
            self._history_source_config_error = str(exc)
            self.history_source_status.setText(f"来源无效: {exc}")
            if combo is not None:
                combo.blockSignals(True)
                old_index = combo.findData(previous)
                combo.setCurrentIndex(old_index if old_index >= 0 else -1)
                combo.blockSignals(False)
            return
        self.source = self.history_source
        self._history_source_config_error = ""
        # tick 只允许不复权；选择来源时立即更新控件，避免用户先勾选复权
        # 后在后台生成非法请求。
        self._apply_tick_adjustment_policy()
        try:
            import kh_settings as _kh_settings
            cfg = _kh_settings.load()
            cfg["history_import_source"] = self.history_source
            _kh_settings.save(cfg)
        except Exception:
            # GUI 选择不应因设置文件只读而阻断本次任务；窗口本地设置仍
            # 保存 canonical 值，下一次启动会再次尝试同步全局配置。
            pass
        try:
            self.history_settings.setValue(HISTORY_IMPORT_SOURCE_KEY, self.history_source)
            self.history_settings.sync()
        except Exception:
            pass
        # 这里曾额外写一份 QSettings('KhQuant', 'KhQuant')，但全工程没有任何
        # 读取点，纯属只写存储。真正的全局真相源是 cli/settings.json，由下面
        # 的 _persist_history_import_config() 负责同步。
        self._persist_history_import_config()
        self._update_source_specific_ui()
        self.check_history_source()
        self.check_xtquant_connection()

    def _update_source_specific_ui(self):
        """根据当前历史行情源动态更新相关控件状态与提示。"""
        is_native = getattr(self, "history_source", None) == QMT_NATIVE
        if hasattr(self, 'full_import_local_btn'):
            if is_native:
                self.full_import_local_btn.setEnabled(False)
                self.full_import_local_btn.setToolTip(
                    "当前选择为大QMT原生桥，直接使用【开始扫描并补充】即可。\n"
                    "如需从本地MiniQMT缓存导入，请将历史行情源切换为MiniQMT。"
                )
            else:
                self.full_import_local_btn.setEnabled(True)
                self.full_import_local_btn.setToolTip(
                    "无需从miniQMT下载新数据，直接从本地miniQMT数据库读取并导入。\n"
                    "根据当前勾选的周期、时间段与复权方式，使用 get_local_data 读取本地数据，"
                    "效率更高，适用于已在miniQMT本地有历史数据的用户。"
                )
        if hasattr(self, 'full_local_workers_spin'):
            self.full_local_workers_spin.setEnabled(not is_native)

        # 联动并发数控件与文案：大 QMT 原生桥为单通道序列化传输，内部单通道吞吐极高，自动锁定 1 并发
        native_workers_tip = (
            "大QMT原生桥采用高性能内存级管道序列化传输，内部单通道吞吐极高，"
            "自动锁定1并发避免多进程管道冲突。"
        )
        mini_workers_tip = "扫描补充时的并行下载进程数，建议2~4，过多可能导致miniQMT连接冲突"

        if hasattr(self, 'custom_dl_workers_spin'):
            self.custom_dl_workers_spin.setEnabled(not is_native)
            self.custom_dl_workers_spin.setToolTip(
                native_workers_tip if is_native else mini_workers_tip
            )
        if hasattr(self, 'custom_dl_workers_label'):
            self.custom_dl_workers_label.setText(
                "并发数（锁定1）:" if is_native else "下载进程数:"
            )

        if hasattr(self, 'full_dl_workers_spin'):
            self.full_dl_workers_spin.setEnabled(not is_native)
            self.full_dl_workers_spin.setToolTip(
                native_workers_tip if is_native else mini_workers_tip
            )
        if hasattr(self, 'full_dl_workers_label'):
            self.full_dl_workers_label.setText(
                "并发数（锁定1）:" if is_native else "下载进程数:"
            )

        if hasattr(self, 'qmt_bridge_sync_btn'):
            self.qmt_bridge_sync_btn.setEnabled(is_native)
            self.qmt_bridge_sync_btn.setToolTip(
                "备份大QMT中的旧桥脚本并同步 KhQuant 随包最新版" if is_native
                else "同步大QMT高速桥仅在大QMT原生模式下生效"
            )

    def _sync_qmt_native_bridge(self):
        """一键部署 KhQuant 随包的大 QMT 原生高速桥。"""
        try:
            from kh_qmt_native_bridge.deployment import sync_qmt_bridge

            result = sync_qmt_bridge(bridge_dir=self.bridge_dir)
        except Exception as exc:
            QMessageBox.critical(self, "同步失败", str(exc))
            return
        changed = "、".join(result.changed_files) or "无（已是最新）"
        message = f"桥脚本已同步到：\n{result.qmt_python_dir}\n\n更新：{changed}"
        if result.backup_dir:
            message += f"\n旧脚本备份：{result.backup_dir}"
        runtime_info = dict(getattr(result, "realtime_runtime", None) or {})
        if runtime_info.get("ready"):
            runtime_line = f"\n实时行情桥运行目录：{runtime_info.get('version')}"
            if runtime_info.get("activated"):
                runtime_line += "（本次已安装/激活）"
            if runtime_info.get("update_available"):
                runtime_line += f"；随包新版 {runtime_info.get('packaged_version')} 未自动切换"
            message += runtime_line
        elif runtime_info:
            message += f"\n实时行情桥运行目录未就绪：{runtime_info.get('error') or '未知原因'}"
        if result.restart_required:
            message += "\n\n请在大QMT中停止并重新启动桥接策略，使新版本生效。"
        QMessageBox.information(self, "同步完成", message)
        self.check_history_source()
        self.check_xtquant_connection()

    def native_execution_options(self) -> dict:
        """Return the current canonical native execution controls.

        This public helper is intentionally UI-independent so callers that
        embed the dialog can pass one stable dictionary to Full/Custom/CLI
        entry points without reaching into individual widget state.
        """
        self._native_execution = normalize_native_execution_options(
            {
                key: getattr(self, key, None)
                for key in NATIVE_EXECUTION_OPTION_KEYS
            },
            strict=False,
        )
        return dict(self._native_execution)

    def set_native_execution_options(self, **kwargs) -> dict:
        """Update native controls programmatically and persist them.

        The visible dialog currently exposes the safe defaults implicitly;
        this setter gives advanced/automation callers a stable way to select
        turbo/batching values while preserving backwards compatibility with
        the existing widget layout.
        """
        current = self.native_execution_options()
        for key in NATIVE_EXECUTION_OPTION_KEYS:
            if key in kwargs and kwargs[key] is not None:
                current[key] = kwargs[key]
        self._native_execution = normalize_native_execution_options(current, strict=True)
        for key, value in self._native_execution.items():
            setattr(self, key, value)
        self._persist_history_import_config()
        return dict(self._native_execution)

    def _persist_history_import_config(self, last_run: Optional[dict] = None) -> bool:
        """保存当前历史导入来源、桥接目录、重试策略和原生执行选项。"""
        try:
            source = _canonical_history_source(self.history_source)
        except Exception:
            # 非法旧配置必须留在错误态，不能借保存动作静默改成 MiniQMT。
            return False
        self.history_source = source
        self.source = source
        try:
            self.history_settings.setValue(HISTORY_IMPORT_SOURCE_KEY, source)
            self.history_settings.setValue(HISTORY_BRIDGE_DIR_KEY, self.bridge_dir or "")
            self.history_settings.setValue(HISTORY_MAX_RETRIES_KEY, int(self.max_task_retries))
            self.history_settings.setValue(
                HISTORY_RETRY_BACKOFF_KEY,
                json.dumps(list(self.retry_backoff), ensure_ascii=False),
            )
            write_qsettings_options(
                self.history_settings,
                self.native_execution_options(),
            )
            if last_run is not None:
                payload = dict(last_run)
                payload.setdefault("source", source)
                payload.setdefault("bridge_dir", self.bridge_dir)
                payload.setdefault("recorded_at", datetime.now().isoformat(timespec="seconds"))
                self.history_settings.setValue(
                    HISTORY_LAST_RUN_STATUS_KEY,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                )
            self.history_settings.sync()
        except Exception as exc:
            logging.debug("保存历史导入窗口配置失败: %s", exc)
        # 同步全局配置时只写 canonical source/bridge；失败不阻断当前任务。
        try:
            import kh_settings as _kh_settings
            cfg = _kh_settings.load()
            cfg[HISTORY_IMPORT_SOURCE_KEY] = source
            cfg["qmt_bridge_dir"] = self.bridge_dir or ""
            cfg[HISTORY_BRIDGE_DIR_KEY] = self.bridge_dir or ""
            cfg[HISTORY_MAX_RETRIES_KEY] = int(self.max_task_retries)
            cfg[HISTORY_RETRY_BACKOFF_KEY] = list(self.retry_backoff)
            native_values = self.native_execution_options()
            cfg.update({
                key: value for key, value in native_values.items()
                if key != "mode"
            })
            cfg["qmt_native_history_mode"] = (
                native_values.get("mode") or "historical-backfill"
            )
            _kh_settings.save(cfg)
        except Exception as exc:
            logging.debug("同步历史导入全局配置失败: %s", exc)
        return True

    def _apply_tick_adjustment_policy(self):
        """让两个 tab 的 Tick 复权选项始终固定为 none。"""
        # 仅在 Tick 单独选中时暂时禁用复权勾选；Tick 与 bar 混选时必须
        # 保留 bar 的复权选项，任务提交层会对 tick 单独传 []。
        for prefix in ("custom", "full"):
            tick = getattr(self, f"{prefix}_period_tick_check", None)
            bar_checks = [
                getattr(self, f"{prefix}_period_1d_check", None),
                getattr(self, f"{prefix}_period_1m_check", None),
                getattr(self, f"{prefix}_period_5m_check", None),
            ]
            bars_selected = any(cb is not None and cb.isChecked() for cb in bar_checks)
            checks = [
                getattr(self, f"{prefix}_dividend_front_check", None),
                getattr(self, f"{prefix}_dividend_back_check", None),
                getattr(self, f"{prefix}_dividend_front_ratio_check", None),
                getattr(self, f"{prefix}_dividend_back_ratio_check", None),
            ]
            saved_key = f"_{prefix}_tick_adjustments_saved"
            if tick is not None and tick.isChecked() and not bars_selected:
                if not hasattr(self, saved_key):
                    setattr(self, saved_key, [cb.isChecked() for cb in checks if cb is not None])
                for checkbox in checks:
                    if checkbox is not None:
                        checkbox.setChecked(False)
                        checkbox.setEnabled(False)
                none_check = getattr(self, f"{prefix}_dividend_none_check", None)
                if none_check is not None:
                    none_check.setChecked(True)
            else:
                for checkbox in checks:
                    if checkbox is not None:
                        checkbox.setEnabled(True)
                saved = getattr(self, saved_key, None)
                if bars_selected and saved is not None:
                    for checkbox, checked in zip(checks, saved):
                        if checkbox is not None:
                            checkbox.setChecked(bool(checked))
                    try:
                        delattr(self, saved_key)
                    except Exception:
                        pass

    def _history_source_probe(self):
        """返回 (可用, 文本, 详细状态)，不自动切源。"""
        source = self.history_source
        try:
            source = _canonical_history_source(source)
            self.history_source = source
            self.source = source
            from duckdb_storage.history_adapters import create_history_adapter
            adapter_kwargs = {}
            if source == QMT_NATIVE and self.bridge_dir:
                adapter_kwargs["bridge_dir"] = self.bridge_dir
            adapter = create_history_adapter(source, **adapter_kwargs)
            try:
                result = adapter.probe()
            finally:
                adapter.close()
            details = _history_probe_details(result)
            details.setdefault("source", source)
            details["read_only_probe"] = True
            if source == MINIQMT:
                details.update(_probe_miniqmt_processes())
            # 原生桥只能以 detector 的 bridge_ready 作为前门禁；文件可读、
            # endpoint_up 或进程存在都不足以启动下载。
            available = _history_probe_ready(result, source)
            message = str(
                details.get("message")
                or ("已就绪" if available else "不可用")
            )
            self.history_source_probe = result
            self.history_source_probe_details = details
            self.history_source_available = available
            return available, message, details
        except Exception as exc:
            details = {
                "source": source,
                "available": False,
                "read_only_probe": True,
                "error_code": getattr(exc, "code", type(exc).__name__),
                "message": str(exc),
            }
            self.history_source_probe = details
            self.history_source_probe_details = details
            self.history_source_available = False
            return False, str(exc), details

    def check_history_source(self):
        label = getattr(self, "history_source_status", None)
        if label is None:
            return
        available, message, details = self._history_source_probe()
        try:
            source = _canonical_history_source(self.history_source)
        except Exception:
            source = None
        if source is None:
            text = f"历史行情源无效（{message}）"
        elif source == QMT_NATIVE:
            state = details.get("state") if isinstance(details, dict) else None
            age = details.get("heartbeat_age_seconds") if isinstance(details, dict) else None
            text = "大QMT桥就绪" if available else f"大QMT桥未就绪（{state or message}）"
            if age is not None:
                try:
                    text += f" · 心跳 {float(age):.1f}s"
                except (TypeError, ValueError):
                    pass
        elif source == MINIQMT:
            process_open = details.get("process_open") if isinstance(details, dict) else None
            if available and process_open is False:
                text = "MiniQMT API可用；未发现客户端进程（仅提示）"
            else:
                text = "MiniQMT API可用" if available else f"MiniQMT不可用（{message}）"
        label.setText(text)
        label.setStyleSheet(
            f"color: {'#4CAF50' if available else '#F44336'}; font-size: 12px;"
        )
        try:
            self.history_settings.setValue(
                HISTORY_SOURCE_STATUS_KEY,
                json.dumps(details, ensure_ascii=False, sort_keys=True, default=str),
            )
            self.history_settings.sync()
        except Exception:
            pass
        return available

    def _validate_history_ranges(self, periods_config: Dict[str, tuple]) -> bool:
        """开始任务前做严格范围校验；超限只告知并停止，不静默截断。"""
        try:
            source = _canonical_history_source(self.history_source)
            self.history_source = source
            self.source = source
            from duckdb_storage.history_sources import HistoryRequest
            from datetime import date
            for period, (start, end) in periods_config.items():
                if period == "tick":
                    # tick 周期由后台自动按 31 天分片下载，不限制跨度与历史起始日；若无数据自然不入库
                    from duckdb_storage.incremental import normalize_date8
                    s8, e8 = normalize_date8(start), normalize_date8(end)
                    if s8 > e8:
                        raise ValueError(f"开始日期 {s8} 晚于结束日期 {e8}")
                    if datetime.strptime(e8, "%Y%m%d").date() > date.today():
                        raise ValueError("不能请求未来日期")
                    continue
                request = HistoryRequest(
                    codes=["000001.SZ"], period=period, start=start, end=end,
                    adjustment="none", source=source,
                )
                request.validate(today=date.today())
            return True
        except Exception as exc:
            QMessageBox.warning(self, "日期范围不可用", str(exc))
            return False
    
    def update_xtquant_indicator(self, color, tooltip, status_text=""):
        """更新xtquant连接状态指示器"""
        try:
            # 创建20x20的圆形指示灯
            pixmap = QPixmap(20, 20)
            pixmap.fill(Qt.transparent)
            
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            
            # 设置颜色和样式
            if color == "green":
                painter.setBrush(QColor("#4CAF50"))
                painter.setPen(QColor("#2E7D32"))
            elif color == "yellow":
                painter.setBrush(QColor("#FFC107"))
                painter.setPen(QColor("#F57F17"))
            elif color == "gray":
                painter.setBrush(QColor("#9E9E9E"))
                painter.setPen(QColor("#616161"))
            else:
                painter.setBrush(QColor("#F44336"))
                painter.setPen(QColor("#C62828"))
            
            # 绘制圆形，留1像素边框
            painter.drawEllipse(1, 1, 18, 18)
            painter.end()
            
            self.xtquant_indicator.setPixmap(pixmap)
            self.xtquant_indicator.setToolTip(tooltip)
            
            # 更新状态文字
            if hasattr(self, 'xtquant_status_text'):
                if not status_text:
                    status_text = "已连接" if color == "green" else (
                        "检查中" if color in ("yellow", "gray") else "未连接"
                    )
                
                text_color = {
                    "green": "#4CAF50",
                    "yellow": "#FFC107",
                    "gray": "#9E9E9E",
                }.get(color, "#F44336")
                self.xtquant_status_text.setText(status_text)
                self._set_scaled_stylesheet(self.xtquant_status_text, f"""
                    color: {text_color};
                    font-size: 13px;
                    font-weight: bold;
                    padding: 0px;
                    margin: 0px;
                """)
            
        except Exception as e:
            logging.error(f"更新xtquant指示器时出错: {str(e)}")
    
    def check_resume(self):
        """检查是否有可恢复的进度"""
        if self.load_full_progress():
            # 全量增量补充现在是第一个选项卡（索引0）
            self.tab_widget.setCurrentIndex(0)

            reply = QMessageBox.question(
                self, "恢复进度",
                f"发现上次未完成的全量增量补充任务 ({len(self.completed_tasks)} 个已完成)。\n"
                "是否继续上次的进度？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )
            if reply == QMessageBox.Yes:
                self.full_completed_label.setText(f"已完成: {len(self.completed_tasks)}")
                self.full_log(f"已恢复进度: {len(self.completed_tasks)} 个任务已完成")
            else:
                self.completed_tasks.clear()
                self.clear_full_progress()

    def _stop_all_running_threads(self):
        self._pipeline_stop_requested = True
        for name in (
            'import_thread', 'full_increment_thread', 'full_import_local_thread',
            'custom_increment_thread', 'indicator_thread'
        ):
            thread = getattr(self, name, None)
            if thread is not None and thread.isRunning() and hasattr(thread, 'stop'):
                try:
                    thread.stop()
                except Exception:
                    pass
        if self.full_increment_thread and self.full_increment_thread.isRunning():
            self.save_full_progress(force=True)
        if self.custom_increment_thread and self.custom_increment_thread.isRunning():
            self.save_custom_progress(force=True)

    def _schedule_close_when_idle(self):
        if self._close_poll_scheduled:
            return
        self._close_poll_scheduled = True
        QTimer.singleShot(100, self._close_when_idle)

    def _close_when_idle(self):
        self._close_poll_scheduled = False
        if self._is_any_import_running():
            self._schedule_close_when_idle()
            return
        self.close()

    def closeEvent(self, event):
        """关闭事件"""
        if self._is_any_import_running():
            if not self._close_after_stop:
                reply = QMessageBox.question(
                    self, "确认关闭",
                    "任务正在进行中，确定要关闭吗？\n"
                    "系统会先停止领取新任务，等待当前写入、元数据刷新和数据库连接收尾后再关闭。",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    event.ignore()
                    return
                self.request_close_after_stop()
            event.ignore()
            return

        if hasattr(self, 'xtquant_status_timer') and self.xtquant_status_timer:
            self.xtquant_status_timer.stop()
        if hasattr(self, 'history_source_timer') and self.history_source_timer:
            self.history_source_timer.stop()
        if hasattr(self, 'manager') and self.manager:
            try:
                self.manager.close_all()
                logging.info("ImportDialog: DuckDB 连接已关闭")
            except Exception as e:
                logging.warning(f"ImportDialog: 关闭 DuckDB 连接时出错: {e}")
        event.accept()


class BaoStockRequestTracker:
    def __init__(self, data_root: str, daily_limit: int = 30000, display_limit: int = 30000):
        self.data_root = data_root
        self.daily_limit = daily_limit
        self.display_limit = display_limit
        self.file_path = os.path.join(self.data_root, "baostock_request_usage.json")
        self._lock = Lock()
        self._date = QDate.currentDate().toString("yyyyMMdd")
        self._count = 0
        self._last_saved_count = 0
        self.limit_message = f"已达到BaoStock当日请求上限{self.daily_limit}次，已停止下载"
        self._load()

    def _load(self):
        if not os.path.exists(self.file_path):
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            date_value = str(data.get("date", ""))
            count_value = int(data.get("count", 0))
            if date_value == self._date:
                self._count = max(0, count_value)
                self._last_saved_count = self._count
        except Exception:
            pass

    def _save(self):
        try:
            data = {"date": self._date, "count": self._count}
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._last_saved_count = self._count
        except Exception:
            pass

    def _reset_if_new_day(self):
        current = QDate.currentDate().toString("yyyyMMdd")
        if current != self._date:
            self._date = current
            self._count = 0
            self._last_saved_count = 0
            self._save()

    def get_count(self) -> int:
        with self._lock:
            self._reset_if_new_day()
            return self._count

    def is_limit_reached(self) -> bool:
        with self._lock:
            self._reset_if_new_day()
            return self._count >= self.daily_limit

    def consume(self, n: int = 1) -> bool:
        with self._lock:
            self._reset_if_new_day()
            if self._count + n > self.daily_limit:
                return False
            self._count += n
            if self._count - self._last_saved_count >= 20 or self._count == self.daily_limit:
                self._save()
            return True

    def flush(self):
        """立即持久化请求计数，避免少于20次的短任务丢失计数。"""
        with self._lock:
            self._reset_if_new_day()
            if self._count != self._last_saved_count:
                self._save()


_BAOSTOCK_REQUEST_TRACKERS = {}
_BAOSTOCK_REQUEST_TRACKERS_LOCK = Lock()


def get_baostock_request_tracker(data_root: str) -> BaoStockRequestTracker:
    """同一数据目录复用一个 BaoStock 请求计数器。"""
    key = os.path.normcase(os.path.abspath(data_root or os.getcwd()))
    with _BAOSTOCK_REQUEST_TRACKERS_LOCK:
        tracker = _BAOSTOCK_REQUEST_TRACKERS.get(key)
        if tracker is None:
            tracker = BaoStockRequestTracker(key)
            _BAOSTOCK_REQUEST_TRACKERS[key] = tracker
        return tracker


class BaoStockImportThread(QThread):
    progress = pyqtSignal(int, int, int)
    status = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    conflict = pyqtSignal(str, str, int, int)
    request_count = pyqtSignal(int)

    def __init__(
        self,
        manager: DuckDBManager,
        stocks: List[str],
        periods_config: Dict[str, dict],
        request_tracker=None,
        num_workers: int = 2,
        timeout_per_task: float = 120.0,
        force_overwrite: bool = False,
        include_front_adjusted: bool = True,
        include_back_adjusted: bool = True,
    ):
        super().__init__()
        self.manager = manager
        self.stocks = stocks
        self.periods_config = periods_config
        self._is_running = True
        self._mutex = QMutex()
        self._wait = QWaitCondition()
        self._pending_resolution = None
        self._pending_apply_all = False
        self._default_resolution = None
        self.request_tracker = request_tracker
        self.num_workers = max(1, int(num_workers))
        self.timeout_per_task = float(timeout_per_task)
        self.force_overwrite = bool(force_overwrite)
        self.include_front_adjusted = bool(include_front_adjusted)
        self.include_back_adjusted = bool(include_back_adjusted)
        self._mp_importer = None

    def stop(self):
        self._is_running = False
        self._mutex.lock()
        self._wait.wakeAll()
        self._mutex.unlock()
        try:
            if self._mp_importer is not None and getattr(self._mp_importer, "is_running", False):
                self._mp_importer.force_stop()
        except Exception:
            pass

    def set_conflict_resolution(self, resolution: str, apply_all: bool):
        self._mutex.lock()
        self._pending_resolution = resolution
        self._pending_apply_all = apply_all
        self._wait.wakeAll()
        self._mutex.unlock()

    def _to_baostock_code(self, stock_code: str) -> str:
        code = stock_code.strip().upper()
        if '.' in code:
            num, market = code.split('.')
            if market in ('SH', 'SZ', 'BJ'):
                return f"{market.lower()}.{num}"
        return code.lower()

    @staticmethod
    def _to_baostock_date(value: str) -> str:
        from .incremental import normalize_date8

        date8 = normalize_date8(value)
        return f"{date8[:4]}-{date8[4:6]}-{date8[6:]}"

    def _build_incremental_plan(
        self,
        stock_code: str,
        period: str,
        start_date: str,
        end_date: str,
        *,
        include_adjusted: bool = True,
        include_front_adjusted: bool = True,
        include_back_adjusted: bool = True,
    ):
        from .incremental import (
            RAW_COMPLETENESS_COLUMNS,
            build_incremental_plan,
            required_price_columns,
        )

        return build_incremental_plan(
            self.manager,
            stock_code,
            period,
            start_date,
            end_date,
            # raw、前复权、后复权可由调用方独立规划；这样既能识别旧库
            # 缺失的派生列，也不会因单一复权缺口重复写入完整 raw。
            required_columns=(
                required_price_columns(
                    front=include_front_adjusted,
                    back=include_back_adjusted,
                )
                if include_adjusted else RAW_COMPLETENESS_COLUMNS
            ),
        )

    def _adjustment_refresh_range(
        self,
        stock_code: str,
        period: str,
        requested_start: str,
        requested_end: str,
    ) -> Tuple[str, str]:
        """把前复权刷新范围扩展到库内该周期的全部 raw 历史。"""

        from .incremental import normalize_date8

        start8 = normalize_date8(requested_start)
        end8 = normalize_date8(requested_end)
        getter = getattr(self.manager, "get_kline_data_range", None)
        if not callable(getter):
            return start8, end8
        try:
            local_start, local_end = getter(stock_code, period)
            if local_start is not None:
                start8 = min(start8, normalize_date8(str(local_start)))
            if local_end is not None:
                end8 = max(end8, normalize_date8(str(local_end)))
            return start8, end8
        finally:
            try:
                self.manager.close_stock_connection(
                    stock_code, skip_checkpoint=True,
                )
            except Exception:
                pass

    def _validate_adjustment_refresh_frame(
        self,
        frame: pd.DataFrame,
        stock_code: str,
        period: str,
        start_date: str,
        end_date: str,
        columns: List[str],
    ) -> Optional[int]:
        """确认一次前复权结果完整覆盖库内 raw 后再原子更新。

        这一步阻止 BaoStock 全历史请求半途截断或部分空值时把一部分旧行
        刷成新基准、另一部分仍留在旧基准。旧测试替身没有读取接口时保持
        兼容；正式 DuckDBManager 始终执行严格比对。
        """

        getter = getattr(self.manager, "get_kline_data", None)
        if not callable(getter):
            return
        missing_columns = [column for column in columns if column not in frame.columns]
        if missing_columns:
            raise RuntimeError(
                f"前复权返回缺少字段: {', '.join(missing_columns)}"
            )
        local = getter(
            stock_code,
            period,
            start_time=start_date,
            end_time=end_date,
            fields=["time"],
        )
        if local is None or local.empty or "time" not in local.columns:
            raise RuntimeError("本地 raw 行情为空，拒绝应用前复权刷新")

        if "time" not in frame.columns:
            raise RuntimeError("前复权返回缺少 time")
        local_times = pd.to_datetime(local["time"], errors="coerce")
        source_times_all = pd.to_datetime(frame["time"], errors="coerce")
        if local_times.isna().any() or source_times_all.isna().any():
            raise RuntimeError("前复权刷新包含无效时间，已拒绝应用")
        import math

        numeric_columns = frame[columns].apply(pd.to_numeric, errors="coerce")
        invalid_columns = [
            column for column in columns
            if numeric_columns[column].isna().any()
            or not numeric_columns[column].map(math.isfinite).all()
        ]
        if invalid_columns:
            raise RuntimeError(
                "前复权刷新包含空值或无效数值: "
                + ", ".join(invalid_columns)
            )
        valid_source = frame.copy()
        source_times = pd.to_datetime(valid_source["time"], errors="coerce")
        if period == "1d":
            local_keys = set(local_times.dt.strftime("%Y%m%d"))
            source_keys = set(source_times.dt.strftime("%Y%m%d"))
            all_source_keys = source_times_all.dt.strftime("%Y%m%d")
        else:
            local_keys = set(local_times.astype("int64").tolist())
            source_keys = set(source_times.astype("int64").tolist())
            all_source_keys = source_times_all.astype("int64")
        if all_source_keys.duplicated().any():
            raise RuntimeError("前复权刷新返回重复行情时间，已拒绝应用")
        missing = local_keys - source_keys
        unexpected = source_keys - local_keys
        if missing or unexpected:
            raise RuntimeError(
                f"前复权结果未覆盖本地 {len(missing)} 个行情时间点，"
                f"并包含 {len(unexpected)} 个本地 raw 之外的时间点；"
                "已拒绝部分刷新以避免混用两套基准"
            )
        return len(local_keys)

    def _mark_adjustment_columns_pending(
        self,
        stock_code: str,
        period: str,
        columns: List[str],
    ) -> int:
        """清空无法证明一致的复权列，让下一次增量必然重新补齐。"""

        clearer = getattr(self.manager, "clear_kline_columns", None)
        if not callable(clearer) or not columns:
            return 0
        return int(clearer(stock_code, period, list(columns)) or 0)

    # 说明：_fetch_kline/_prepare_base_df/_merge_adjusted 已迁移至子进程 worker 中执行，
    # 主线程仅负责冲突处理与写入 DuckDB。

    def _get_existing_times(self, stock_db, period: str, start_time: datetime, end_time: datetime) -> List[datetime]:
        table_name = stock_db.PERIOD_TABLE_MAP.get(period)
        if not table_name:
            return []
        try:
            tables = stock_db.conn.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_name = ?
            """, [table_name]).fetchall()
            if not tables:
                return []
            rows = stock_db.conn.execute(
                f"SELECT time FROM {table_name} WHERE time >= ? AND time <= ?",
                [start_time, end_time]
            ).fetchall()
            return [row[0] for row in rows]
        except Exception:
            return []

    def _resolve_conflict(self, stock: str, period: str, existing_count: int, new_count: int) -> str:
        if self._default_resolution:
            return self._default_resolution
        self._mutex.lock()
        self._pending_resolution = None
        self._pending_apply_all = False
        self._mutex.unlock()
        self.conflict.emit(stock, period, existing_count, new_count)
        self._mutex.lock()
        while self._pending_resolution is None and self._is_running:
            self._wait.wait(self._mutex)
        resolution = self._pending_resolution or 'skip'
        apply_all = self._pending_apply_all
        self._mutex.unlock()
        if apply_all:
            self._default_resolution = resolution
        return resolution

    def _ensure_benchmark_incremental(self, imported_records: list, results: dict):
        """按缺口补充 000300.SH；指数每段只消耗一次 BaoStock 请求。"""

        import baostock as bs
        import pandas as pd
        from datetime import datetime, timedelta

        benchmark_code = '000300.SH'
        end_date = datetime.now()
        start_date = end_date - timedelta(days=365 * 20)
        start_text = start_date.strftime("%Y-%m-%d")
        end_text = end_date.strftime("%Y-%m-%d")
        self.status.emit("检查基准指数 000300.SH 增量缺口...")

        try:
            plan = self._build_incremental_plan(
                benchmark_code, '1d', start_text, end_text,
            )
        finally:
            try:
                self.manager.close_stock_connection(
                    benchmark_code, skip_checkpoint=True,
                )
            except Exception:
                pass

        from .incremental import full_range, group_missing_trade_dates

        anomalous = set(plan.anomalous_dates)
        normal_unresolved = (
            set(plan.missing_dates) | set(plan.partial_dates)
        ) - anomalous
        download_specs = [
            (*full_range('1d', group_start, group_end), False)
            for group_start, group_end in group_missing_trade_dates(
                normal_unresolved, plan.expected_dates,
            )
        ]
        download_specs.extend(
            (*full_range('1d', date8, date8), True)
            for date8 in sorted(anomalous)
        )

        if not download_specs:
            results['benchmark_up_to_date'] = True
            self.status.emit("基准指数 000300.SH 已是最新")
            return

        self.status.emit(
            f"000300.SH 发现 {len(download_specs)} 段缺口，正在增量补充..."
        )
        login_result = bs.login()
        if login_result.error_code != '0':
            message = f"BaoStock 登录失败，无法补充 000300.SH: {login_result.error_msg}"
            results['benchmark_unresolved'] = list(plan.unresolved_dates)
            self.status.emit(f"警告：{message}")
            return

        try:
            for range_start, range_end, overwrite_range in download_specs:
                if not self._is_running:
                    break
                if self.request_tracker and not self.request_tracker.consume(1):
                    results['benchmark_unresolved'] = list(plan.unresolved_dates)
                    raise RuntimeError(self.request_tracker.limit_message)
                if self.request_tracker:
                    self.request_count.emit(self.request_tracker.get_count())
                start_dash = self._to_baostock_date(range_start)
                end_dash = self._to_baostock_date(range_end)
                rs = bs.query_history_k_data_plus(
                    "sh.000300",
                    "date,code,open,high,low,close,preclose,volume,amount,pctChg",
                    start_date=start_dash,
                    end_date=end_dash,
                    frequency="d",
                )
                if rs.error_code != '0':
                    raise RuntimeError(rs.error_msg or rs.error_code)
                rows = []
                while (rs.error_code == '0') & rs.next():
                    rows.append(rs.get_row_data())
                if not rows:
                    results['benchmark_empty'] = int(
                        results.get('benchmark_empty', 0) or 0
                    ) + 1
                    self.status.emit(
                        f"警告：000300.SH {start_dash} ~ {end_dash} 返回空行情"
                    )
                    continue

                frame = pd.DataFrame(rows, columns=rs.fields)
                frame["time"] = (
                    pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="coerce")
                    + pd.Timedelta(hours=9, minutes=30)
                )
                if "preclose" in frame.columns:
                    frame["preClose"] = frame["preclose"]
                for column in (
                    "open", "high", "low", "close", "preClose", "volume", "amount"
                ):
                    if column in frame.columns:
                        frame[column] = pd.to_numeric(frame[column], errors="coerce")
                for suffix in ('front', 'back', 'front_ratio', 'back_ratio'):
                    for field in ('open', 'high', 'low', 'close'):
                        if field in frame.columns:
                            frame[f'{field}_{suffix}'] = frame[field]
                keep = [
                    "time", "open", "high", "low", "close", "preClose",
                    "volume", "amount",
                ] + [
                    f"{field}_{suffix}"
                    for suffix in ('front', 'back', 'front_ratio', 'back_ratio')
                    for field in ('open', 'high', 'low', 'close')
                ]
                frame = frame[[column for column in keep if column in frame.columns]]
                frame = frame.dropna(subset=["time"]).sort_values("time")
                if frame.empty:
                    continue
                if overwrite_range:
                    from .incremental import validate_overwrite_frame

                    validate_overwrite_frame(
                        frame,
                        '1d',
                        [
                            date8 for date8 in plan.expected_dates
                            if range_start <= date8 <= range_end
                        ],
                    )
                # BaoStock 成交量为股，与主导入路径一样在入口统一换算为手；
                # 放在所有筛选之后，保证写库的 frame 带着单位标记。
                from .units import normalize_kline_units

                frame = normalize_kline_units(frame, source_volume_unit="shares")
                saved = self.manager.save_kline_data(
                    frame,
                    benchmark_code,
                    '1d',
                    'none',
                    skip_metadata=True,
                    overwrite=overwrite_range,
                    merge_missing=not overwrite_range,
                    **(
                        {"overwrite_trade_dates": True}
                        if overwrite_range else {}
                    ),
                )
                if saved > 0:
                    imported_records.append((benchmark_code, '1d', int(saved)))
                self.manager.close_stock_connection(
                    benchmark_code, skip_checkpoint=True,
                )
        finally:
            try:
                bs.logout()
            except Exception:
                pass
            try:
                self.manager.close_stock_connection(
                    benchmark_code, skip_checkpoint=True,
                )
            except Exception:
                pass

        try:
            remaining = self._build_incremental_plan(
                benchmark_code, '1d', start_text, end_text,
            )
            results['benchmark_unresolved'] = list(remaining.unresolved_dates)
            if remaining.unresolved_dates:
                self.status.emit(
                    f"警告：000300.SH 仍有 {len(remaining.unresolved_dates)} 个待核验交易日"
                )
            else:
                self.status.emit("基准指数 000300.SH 增量补充完成")
        finally:
            try:
                self.manager.close_stock_connection(
                    benchmark_code, skip_checkpoint=True,
                )
            except Exception:
                pass

    def run(self):
        results = {
            'success': 0,
            'failed': 0,
            'empty': 0,
            'up_to_date': 0,
            'total_records': 0,
            'cancelled': False,
            'planned_ranges': 0,
            'unresolved': [],
            'benchmark_unresolved': [],
            'benchmark_error': '',
            'benchmark_empty': 0,
            'adjustment_failed': 0,
            'limit_reached': False,
        }
        limit_reached = False
        imported_records = []
        short_lock_enabled = False
        try:
            try:
                if hasattr(self.manager, 'enable_short_lock_write'):
                    self.manager.enable_short_lock_write()
                    short_lock_enabled = True
            except Exception as exc:
                self.status.emit(f"短锁写入模式启用失败，将继续使用普通模式: {exc}")

            try:
                self._ensure_benchmark_incremental(imported_records, results)
            except Exception as e:
                results['benchmark_error'] = str(e)
                self.status.emit(f"检查/补充 000300.SH 基准时出错: {e}")

            completed_tasks = 0

            # 下载前先扫描本地覆盖；默认只提交缺失/部分交易日区间。
            tasks = []
            planned_stock_periods = {}
            for period, config in self.periods_config.items():
                if not self._is_running:
                    break
                start_date = config["start"]
                end_date = config["end"]
                for stock_code in self.stocks:
                    if not self._is_running:
                        break
                    key = (stock_code, period)
                    first_task_index = len(tasks)
                    raw_plan = None
                    front_plan = None
                    back_plan = None
                    if self.force_overwrite:
                        from .incremental import expected_trade_dates, normalize_date8

                        force_expected, ignored_open = expected_trade_dates(
                            start_date, end_date,
                        )
                        if not force_expected:
                            results['up_to_date'] += 1
                            if ignored_open:
                                self.status.emit(
                                    f"{stock_code} {period} 仅包含尚未收盘/未来交易日，"
                                    "未执行强制覆写"
                                )
                            continue
                        range_specs = [(start_date, end_date, True)]
                        plan_expected_dates = force_expected
                    else:
                        self.status.emit(f"扫描 {stock_code} {period} 增量缺口...")
                        try:
                            # raw、用户选中的前/后复权分别规划。前复权缺口只需
                            # 全历史复权任务，不能为了补派生列重复写 raw。
                            raw_plan = self._build_incremental_plan(
                                stock_code,
                                period,
                                start_date,
                                end_date,
                                include_adjusted=False,
                            )
                            if self._is_running and self.include_back_adjusted:
                                back_plan = self._build_incremental_plan(
                                    stock_code,
                                    period,
                                    start_date,
                                    end_date,
                                    include_front_adjusted=False,
                                    include_back_adjusted=True,
                                )
                            if self._is_running and self.include_front_adjusted:
                                front_plan = self._build_incremental_plan(
                                    stock_code,
                                    period,
                                    start_date,
                                    end_date,
                                    include_front_adjusted=True,
                                    include_back_adjusted=False,
                                )
                            # 后复权被选择时，以 raw+back 的覆盖结果规划普通
                            # 下载区间；否则只要求 raw 完整。
                            plan = back_plan or raw_plan
                        finally:
                            try:
                                self.manager.close_stock_connection(
                                    stock_code, skip_checkpoint=True,
                                )
                            except Exception:
                                pass
                        if not self._is_running:
                            break
                        from .incremental import (
                            full_range,
                            group_missing_trade_dates,
                            normalize_date8,
                        )

                        anomalous = set(plan.anomalous_dates)
                        normal_unresolved = (
                            set(plan.missing_dates) | set(plan.partial_dates)
                        ) - anomalous
                        normal_groups = group_missing_trade_dates(
                            normal_unresolved, plan.expected_dates,
                        )
                        range_specs = [
                            (*full_range(period, group_start, group_end), False)
                            for group_start, group_end in normal_groups
                        ]
                        # 超额时间戳无法靠 INSERT/MERGE 清理，逐日覆写才能恢复到
                        # 数据源返回的规范条数；只覆写异常日，不扩大到相邻正常日。
                        range_specs.extend(
                            (*full_range(period, date8, date8), True)
                            for date8 in sorted(anomalous)
                        )
                        if plan.partial_dates:
                            self.status.emit(
                                f"{stock_code} {period} 有 {len(plan.partial_dates)} 个部分交易日，"
                                "将按整日请求并安全合并"
                            )
                        if plan.anomalous_dates:
                            self.status.emit(
                                f"{stock_code} {period} 有 {len(plan.anomalous_dates)} 个条数异常交易日，"
                                "将逐日修复覆写"
                            )
                        if plan.ignored_open_dates:
                            self.status.emit(
                                f"{stock_code} {period} 暂不核验尚未收盘/未来交易日"
                            )
                        plan_expected_dates = plan.expected_dates

                    raw_unresolved_dates = set()
                    if raw_plan is not None:
                        raw_unresolved_dates.update(raw_plan.missing_dates)
                        raw_unresolved_dates.update(raw_plan.partial_dates)
                    front_needs_download = bool(
                        self.include_front_adjusted
                        and (
                            self.force_overwrite
                            or (
                                front_plan is not None
                                and front_plan.needs_download
                            )
                        )
                    )
                    planned_stock_periods[key] = (start_date, end_date)
                    for range_start, range_end, overwrite_range in range_specs:
                        range_start8 = normalize_date8(range_start)
                        range_end8 = normalize_date8(range_end)
                        expected_for_range = [
                            date8 for date8 in plan_expected_dates
                            if range_start8 <= date8 <= range_end8
                        ]
                        raw_change_expected = bool(
                            self.force_overwrite
                            or raw_unresolved_dates.intersection(
                                expected_for_range
                            )
                        )
                        do_front = bool(
                            self.include_front_adjusted and raw_change_expected
                        )
                        do_back = bool(self.include_back_adjusted)
                        tasks.append({
                            "stock_code": stock_code,
                            "bs_code": self._to_baostock_code(stock_code),
                            "period": period,
                            "start_date": self._to_baostock_date(range_start),
                            "end_date": self._to_baostock_date(range_end),
                            "overwrite_range": overwrite_range,
                            "expected_dates": expected_for_range,
                            "task_kind": "kline",
                            # 纯后复权缺口只请求 raw+后复权用于填列；raw
                            # 已完整，不触碰前复权，也不触发任何失效操作。
                            "do_front": do_front,
                            "do_back": do_back,
                            "raw_change_expected": raw_change_expected,
                            "request_cost": 1 + int(do_front) + int(do_back),
                        })
                    has_local_raw = bool(
                        raw_plan is not None
                        and (
                            raw_plan.complete_dates
                            or raw_plan.partial_dates
                        )
                    )
                    if range_specs or (
                        front_needs_download and has_local_raw
                    ):
                        from .incremental import normalize_date8

                        refresh_start, refresh_end = self._adjustment_refresh_range(
                            stock_code, period, start_date, end_date,
                        )
                        selected_start = normalize_date8(start_date)
                        selected_end = normalize_date8(end_date)
                        refresh_expanded = (
                            refresh_start != selected_start
                            or refresh_end != selected_end
                        )
                        needs_basis_refresh = bool(
                            self.include_front_adjusted
                            and (
                                (self.force_overwrite and refresh_expanded)
                                or (
                                    not self.force_overwrite
                                    and front_needs_download
                                    and (has_local_raw or refresh_expanded)
                                )
                            )
                        )
                    else:
                        needs_basis_refresh = False
                    if needs_basis_refresh:
                        # 前复权以最新复权因子为基准。增量新增日期时，如果只
                        # 写新行，旧行会保留上一次基准并形成混合口径。因此在
                        # 已有历史数据的情况下，用一次请求刷新库内完整区间。
                        if refresh_expanded:
                            self.status.emit(
                                f"{stock_code} {period} 前复权刷新范围已扩展到本地全部历史 "
                                f"{refresh_start} ~ {refresh_end}"
                            )
                        # 新缺口先只保存 raw + 后复权。前复权必须等全历史结果
                        # 通过覆盖校验后再一次性更新；全量请求失败时新行保持
                        # NULL，而不是与旧历史混用两套基准。
                        for pending_task in tasks[first_task_index:]:
                            if (
                                pending_task.get("task_kind") == "kline"
                                and pending_task.get("raw_change_expected", True)
                            ):
                                pending_task["do_front"] = False
                                pending_task["request_cost"] = (
                                    1 + int(bool(pending_task.get("do_back", False)))
                                )
                        tasks.append({
                            "stock_code": stock_code,
                            "bs_code": self._to_baostock_code(stock_code),
                            "period": period,
                            "start_date": self._to_baostock_date(refresh_start),
                            "end_date": self._to_baostock_date(refresh_end),
                            "task_kind": "adjustment",
                            "adjustflag": "2",
                            "suffix": "front",
                            "columns": [
                                "open_front", "high_front",
                                "low_front", "close_front",
                            ],
                            "request_cost": 1,
                        })
                    if not range_specs and not needs_basis_refresh:
                        planned_stock_periods.pop(key, None)
                        results['up_to_date'] += 1

            # 所有 raw 区间必须先落库，随后才能校验并应用全历史前复权。
            # 多进程任务仅靠 append 顺序不能形成依赖，显式设置全局阶段屏障。
            tasks.sort(
                key=lambda item: 1 if item.get("task_kind") == "adjustment" else 0
            )
            raw_task_count = sum(
                1 for item in tasks if item.get("task_kind") != "adjustment"
            )
            raw_completed = 0
            total_tasks = len(tasks)
            results['planned_ranges'] = total_tasks

            if not tasks:
                compact_metadata = _coalesce_metadata_records(imported_records)
                if compact_metadata:
                    try:
                        self.manager.batch_update_metadata(compact_metadata)
                    except Exception as exc:
                        self.status.emit(f"警告：基准已写入，但元数据刷新失败: {exc}")
                try:
                    self.manager.close_metadata_connection()
                except Exception:
                    pass
                if short_lock_enabled and hasattr(self.manager, 'disable_short_lock_write'):
                    self.manager.disable_short_lock_write()
                    short_lock_enabled = False
                if not self._is_running:
                    results["cancelled"] = True
                    self.status.emit("BaoStock 增量扫描已停止，未提交行情请求")
                else:
                    self.status.emit("所选 BaoStock 行情均已是最新，无需发起行情请求")
                self.finished.emit(results)
                return

            self.status.emit(f"使用 {self.num_workers} 个进程并行下载 BaoStock 数据...")

            try:
                from .baostock_import_worker import (
                    MultiProcessBaoStockImporter,
                    sanitize_requested_adjustments,
                )
            except ImportError:
                pending_count = sum(
                    1 for task in tasks
                    if task.get("task_kind") == "adjustment"
                )
                results["adjustment_failed"] += pending_count
                results["failed"] += max(1, pending_count)
                self.error.emit("未找到 baostock_import_worker 模块，请确保文件存在。你可以使用 miniQMT 接口进行数据导入，或者更新到最新版本。")
                self.finished.emit(results)
                return

            importer = MultiProcessBaoStockImporter(
                num_workers=self.num_workers,
                timeout_per_task=self.timeout_per_task,
                max_task_retries=2,
                retry_backoff=(2.0, 5.0),
            )
            self._mp_importer = importer
            importer.start()

            next_submit = 0
            in_flight = 0
            id_to_task = {}

            def _consume_requests_for_task(task: dict) -> bool:
                request_cost = int(task.get("request_cost", 3) or 3)
                if not self.request_tracker:
                    return True
                if not self.request_tracker.consume(request_cost):
                    return False
                try:
                    self.request_count.emit(self.request_tracker.get_count())
                except Exception:
                    pass
                return True

            def _submit_task(task: dict) -> int:
                if task.get("task_kind") == "adjustment":
                    return importer.add_adjustment_task(
                        task["stock_code"],
                        task["bs_code"],
                        task["period"],
                        task["start_date"],
                        task["end_date"],
                        adjustflag=task.get("adjustflag", "2"),
                        suffix=task.get("suffix", "front"),
                    )
                task_options = {}
                if task.get("do_front") is False:
                    task_options["do_front"] = False
                if task.get("do_back") is False:
                    task_options["do_back"] = False
                return importer.add_task(
                    task["stock_code"],
                    task["bs_code"],
                    task["period"],
                    task["start_date"],
                    task["end_date"],
                    **task_options,
                )

            def _next_task_ready() -> bool:
                if next_submit >= len(tasks):
                    return False
                return not (
                    tasks[next_submit].get("task_kind") == "adjustment"
                    and raw_completed < raw_task_count
                )

            # 预提交填满管道
            while (
                in_flight < self.num_workers
                and _next_task_ready()
                and self._is_running
            ):
                t = tasks[next_submit]
                if not _consume_requests_for_task(t):
                    limit_reached = True
                    results['limit_reached'] = True
                    self._is_running = False
                    self.error.emit(self.request_tracker.limit_message)
                    break
                tid = _submit_task(t)
                id_to_task[tid] = t
                next_submit += 1
                in_flight += 1

            # 主循环：收结果 → 写库 → 提交下一个
            while completed_tasks < total_tasks and in_flight > 0 and self._is_running:
                result = importer.get_result(timeout=0.5)
                for event in importer.get_all_progress():
                    if event.get("type") == "retry":
                        retry_cost = int(event.get("request_cost", 3) or 3)
                        if self.request_tracker and not self.request_tracker.consume(retry_cost):
                            limit_reached = True
                            results['limit_reached'] = True
                            self._is_running = False
                            self.error.emit(self.request_tracker.limit_message)
                            break
                        if self.request_tracker:
                            self.request_count.emit(self.request_tracker.get_count())
                        self.status.emit(
                            f"↻ {event.get('stock_code')} {event.get('period')} "
                            f"连接或登录异常，{event.get('delay', 0):g}秒后进行"
                            f"第{event.get('attempt')}次重试"
                        )
                    elif event.get("type") == "fatal":
                        self.status.emit(str(event.get("msg") or "BaoStock工作进程异常"))
                if not self._is_running:
                    break
                if not result:
                    continue

                tid = result.get("task_id")
                orig = id_to_task.pop(tid, None)
                stock_code = (orig or {}).get("stock_code") or result.get("stock_code") or result.get("stock_code", "")
                period = (orig or {}).get("period") or result.get("period", "")

                try:
                    if result.get("success"):
                        df_dict = result.get("df_dict")
                        if df_dict:
                            df = dict_to_dataframe(df_dict)
                        else:
                            df = pd.DataFrame()

                        task_kind = (orig or {}).get("task_kind", "kline")
                        if df is None or df.empty:
                            results["empty"] += 1
                            if task_kind == "adjustment":
                                results["adjustment_failed"] += 1
                                self.status.emit(
                                    f"○ {stock_code} {period} 前复权返回空数据；"
                                    "raw 已保留，前复权全历史保持待补状态"
                                )
                            else:
                                self.status.emit(f"○ {stock_code} {period} 无数据")
                        else:
                            force = bool((orig or {}).get("overwrite_range", False))
                            adjustment_errors = list(
                                result.get("adjustment_errors") or []
                            )
                            if task_kind != "adjustment":
                                requested_suffixes = []
                                if bool((orig or {}).get("do_front", True)):
                                    requested_suffixes.append("front")
                                if bool((orig or {}).get("do_back", True)):
                                    requested_suffixes.append("back")
                                df, defensive_errors = sanitize_requested_adjustments(
                                    df,
                                    requested_suffixes,
                                )
                                adjustment_errors.extend(
                                    message for message in defensive_errors
                                    if message not in adjustment_errors
                                )
                            if task_kind == "adjustment":
                                columns = list((orig or {}).get("columns") or [])
                                expected_updates = self._validate_adjustment_refresh_frame(
                                    df,
                                    stock_code,
                                    period,
                                    str((orig or {}).get("start_date") or ""),
                                    str((orig or {}).get("end_date") or ""),
                                    columns,
                                )
                                records = self.manager.update_kline_columns(
                                    df,
                                    stock_code,
                                    period,
                                    columns,
                                )
                                if (
                                    expected_updates is not None
                                    and int(records or 0) != expected_updates
                                ):
                                    raise RuntimeError(
                                        f"数据库仅匹配 {int(records or 0)}/"
                                        f"{expected_updates} 行，前复权刷新未完整"
                                    )
                            else:
                                if force:
                                    from .incremental import (
                                        required_price_columns,
                                        validate_overwrite_frame,
                                    )

                                    if adjustment_errors:
                                        raise RuntimeError(
                                            "复权返回不完整，拒绝整日覆写，原数据未改动："
                                            + "；".join(adjustment_errors)
                                        )

                                    validate_overwrite_frame(
                                        df,
                                        period,
                                        list((orig or {}).get("expected_dates") or []),
                                        required_columns=required_price_columns(
                                            front=bool((orig or {}).get("do_front", True)),
                                            back=bool((orig or {}).get("do_back", True)),
                                        ),
                                    )
                                from .incremental import (
                                    FRONT_ADJUSTMENT_COLUMNS,
                                    adjustment_columns_to_invalidate,
                                )

                                records = self.manager.save_kline_data(
                                    df,
                                    stock_code,
                                    period,
                                    "none",
                                    skip_metadata=True,
                                    overwrite=force,
                                    merge_missing=not force,
                                    invalidate_adjustment_columns=(
                                        adjustment_columns_to_invalidate(
                                            front_valid=bool(
                                                (orig or {}).get("do_front", True)
                                                and all(
                                                    f"{field}_front" in df.columns
                                                    for field in ("open", "high", "low", "close")
                                                )
                                            ),
                                            back_valid=bool(
                                                (orig or {}).get("do_back", True)
                                                and all(
                                                    f"{field}_back" in df.columns
                                                    for field in ("open", "high", "low", "close")
                                                )
                                            ),
                                        )
                                        if bool(
                                            (orig or {}).get(
                                                "raw_change_expected", True
                                            )
                                        ) else ()
                                    ),
                                    invalidate_adjustment_columns_full_history=(
                                        FRONT_ADJUSTMENT_COLUMNS
                                        if (
                                            bool(
                                                (orig or {}).get(
                                                    "raw_change_expected", True
                                                )
                                            )
                                            and not bool(
                                                (orig or {}).get("do_front", True)
                                            )
                                        )
                                        else ()
                                    ),
                                    **(
                                        {"overwrite_trade_dates": True}
                                        if force else {}
                                    ),
                                )
                            if records > 0:
                                imported_records.append((stock_code, period, int(records)))
                                results["success"] += 1
                                results["total_records"] += records
                                action = (
                                    "刷新前复权口径" if task_kind == "adjustment"
                                    else "覆写" if force else "增量处理"
                                )
                                self.status.emit(
                                    f"✓ {stock_code} {period} {action} {records} 条数据"
                                )
                            else:
                                results["empty"] += 1
                                self.status.emit(f"○ {stock_code} {period} 无新数据")

                            if adjustment_errors:
                                results["adjustment_failed"] += 1
                                self.status.emit(
                                    f"警告：{stock_code} {period} raw 已保存，"
                                    "对应旧复权已随 raw 同事务失效，但复权未完整："
                                    + "；".join(adjustment_errors)
                                )

                        try:
                            del df
                        except Exception:
                            pass
                    else:
                        results["failed"] += 1
                        err = result.get("error", "未知错误")
                        if (orig or {}).get("task_kind") == "adjustment":
                            results["adjustment_failed"] += 1
                        self.status.emit(f"✗ {stock_code} {period} 失败: {err}")
                except Exception as e:
                    results["failed"] += 1
                    if (orig or {}).get("task_kind") == "adjustment":
                        results["adjustment_failed"] += 1
                    self.status.emit(f"✗ {stock_code} {period} 失败: {e}")
                finally:
                    try:
                        self.manager.close_stock_connection(
                            stock_code, skip_checkpoint=True
                        )
                    except Exception:
                        pass

                completed_tasks += 1
                in_flight -= 1
                if (orig or {}).get("task_kind", "kline") != "adjustment":
                    raw_completed += 1
                self.progress.emit(int((completed_tasks / total_tasks) * 100) if total_tasks else 100, completed_tasks, total_tasks)

                # 提交下一个保持管道满
                while (
                    in_flight < self.num_workers
                    and _next_task_ready()
                    and self._is_running
                ):
                    t = tasks[next_submit]
                    if not _consume_requests_for_task(t):
                        limit_reached = True
                        results['limit_reached'] = True
                        self._is_running = False
                        self.error.emit(self.request_tracker.limit_message)
                        break
                    tid2 = _submit_task(t)
                    id_to_task[tid2] = t
                    next_submit += 1
                    in_flight += 1

                if completed_tasks % 20 == 0:
                    self.manager.cleanup_connections_aggressive(skip_checkpoint=True)

            try:
                importer.stop()
            except Exception:
                pass

            self._mp_importer = None

            # 下载完成后重新读取数据库覆盖情况。接口返回空、停牌/未上市、进程中断
            # 等情况都不能仅凭“请求成功”宣称数据完整，因此单独保留待核验清单。
            if self._is_running:
                unresolved_items = []
                unresolved_total = 0
                post_scan_errors = []
                for (stock_code, period), (start_date, end_date) in planned_stock_periods.items():
                    try:
                        remaining = self._build_incremental_plan(
                            stock_code, period, start_date, end_date,
                            include_front_adjusted=self.include_front_adjusted,
                            include_back_adjusted=self.include_back_adjusted,
                        )
                        count = len(remaining.unresolved_dates)
                        if count:
                            unresolved_total += count
                            unresolved_items.append({
                                "stock": stock_code,
                                "period": period,
                                "count": count,
                                "preview": list(remaining.unresolved_dates[:10]),
                            })
                    except Exception as exc:
                        post_scan_errors.append(f"{stock_code} {period}: {exc}")
                    finally:
                        try:
                            self.manager.close_stock_connection(
                                stock_code, skip_checkpoint=True,
                            )
                        except Exception:
                            pass
                results["unresolved"] = unresolved_items
                results["unresolved_count"] = unresolved_total
                results["post_scan_errors"] = post_scan_errors
                if unresolved_total:
                    self.status.emit(
                        f"警告：导入后仍有 {unresolved_total} 个交易日待核验；"
                        "可能是停牌、未上市、数据源未返回或下载未完成"
                    )
                if post_scan_errors:
                    self.status.emit(
                        f"警告：{len(post_scan_errors)} 个股票周期未能完成导入后复核"
                    )

            compact_metadata = _coalesce_metadata_records(imported_records)
            if compact_metadata:
                try:
                    updated = self.manager.batch_update_metadata(compact_metadata)
                    self.status.emit(f"元数据刷新完成: {updated}/{len(compact_metadata)}")
                    imported_records.clear()
                except Exception as exc:
                    self.status.emit(f"警告：行情已写入，但元数据刷新失败: {exc}")
            try:
                self.manager.close_metadata_connection()
            except Exception:
                pass
            if short_lock_enabled and hasattr(self.manager, 'disable_short_lock_write'):
                self.manager.disable_short_lock_write()
            short_lock_enabled = False
            results['cancelled'] = not self._is_running and not limit_reached
            results['limit_reached'] = bool(limit_reached)
            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            try:
                if self._mp_importer is not None:
                    self._mp_importer.force_stop()
            except Exception:
                pass
            self._mp_importer = None
            # 异常路径也尽力让已经提交的行情在数据树中可见。
            if imported_records:
                try:
                    self.manager.batch_update_metadata(
                        _coalesce_metadata_records(imported_records)
                    )
                except Exception as exc:
                    self.status.emit(f"警告：异常收尾时元数据刷新失败: {exc}")
            try:
                self.manager.close_metadata_connection()
            except Exception:
                pass
            if short_lock_enabled and hasattr(self.manager, 'disable_short_lock_write'):
                try:
                    self.manager.disable_short_lock_write()
                except Exception:
                    pass
            if self.request_tracker:
                try:
                    self.request_tracker.flush()
                except Exception:
                    pass


class BaoStockIndicatorImportThread(QThread):
    progress = pyqtSignal(int, int, int)
    status = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    request_count = pyqtSignal(int)

    # 一次 BaoStock 请求中字段多少不增加请求次数。始终补齐整组指标并以
    # tradestatus 作为“该交易日已成功请求过指标”的稳定证据，避免合法的
    # 空估值字段（如亏损公司的 peTTM）被反复误判为缺失。
    ALL_INDICATORS = (
        'turn', 'tradestatus', 'pctChg', 'peTTM', 'pbMRQ',
        'psTTM', 'pcfNcfTTM', 'isST',
    )

    def __init__(
        self,
        manager: DuckDBManager,
        stocks: List[str],
        start_date: str,
        end_date: str,
        indicators: List[str],
        request_tracker=None,
        num_workers: int = 1,
        timeout_per_task: float = 60.0,
        force_refresh: bool = False,
    ):
        super().__init__()
        self.manager = manager
        self.stocks = stocks
        self.start_date = start_date
        self.end_date = end_date
        self.indicators = list(indicators or [])
        self.fetch_indicators = list(self.ALL_INDICATORS)
        self._is_running = True
        self.request_tracker = request_tracker
        self.num_workers = max(1, int(num_workers))
        self.timeout_per_task = max(5.0, float(timeout_per_task))
        self.force_refresh = bool(force_refresh)
        self._mp_importer = None

    def stop(self):
        self._is_running = False
        importer = self._mp_importer
        if importer is not None:
            try:
                importer.force_stop()
            except Exception:
                pass

    def _to_baostock_code(self, stock_code: str) -> str:
        code = stock_code.strip().upper()
        if '.' in code:
            num, market = code.split('.')
            if market in ('SH', 'SZ', 'BJ'):
                return f"{market.lower()}.{num}"
        return code.lower()

    @staticmethod
    def _to_baostock_date(date8: str) -> str:
        text = str(date8).replace('-', '')[:8]
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"

    def _indicator_download_ranges(self, stock_code: str):
        """只返回本地已有日线中尚无指标导入证据的日期区间。"""

        from datetime import time as datetime_time
        from .incremental import group_missing_trade_dates, normalize_date8

        coverage = self.manager.get_existing_date_completeness(
            stock_code,
            '1d',
            required_columns=['tradestatus'],
            raise_on_error=True,
            start_date=self.start_date,
            end_date=self.end_date,
        )
        now = datetime.now()
        today8 = now.strftime('%Y%m%d')
        local_dates = []
        for raw_date in sorted(coverage):
            date8 = normalize_date8(raw_date)
            if date8 > today8:
                continue
            if date8 == today8 and now.time() < datetime_time(15, 15):
                continue
            local_dates.append(date8)
        if not local_dates:
            return [], 0
        if self.force_refresh:
            groups = ((local_dates[0], local_dates[-1]),)
        else:
            missing = [
                date8 for date8 in local_dates
                if int((coverage.get(date8) or {}).get('valid_rows', 0) or 0) <= 0
            ]
            groups = group_missing_trade_dates(missing, local_dates)
        return [
            (self._to_baostock_date(start), self._to_baostock_date(end))
            for start, end in groups
        ], len(local_dates)

    def _fetch_indicators(self, bs, stock: str) -> pd.DataFrame:
        if self.request_tracker:
            if not self.request_tracker.consume(1):
                raise RuntimeError(self.request_tracker.limit_message)
            self.request_count.emit(self.request_tracker.get_count())
        fields = ['date', 'code'] + self.indicators
        rs = bs.query_history_k_data_plus(
            stock,
            ','.join(fields),
            start_date=self.start_date,
            end_date=self.end_date,
            frequency="d",
            adjustflag="3"
        )

        if rs.error_code != '0':
            raise RuntimeError(rs.error_msg or rs.error_code)

        data_list = []
        while (rs.error_code == '0') & rs.next():
            data_list.append(rs.get_row_data())

        if not data_list:
            return pd.DataFrame()

        df = pd.DataFrame(data_list, columns=rs.fields)
        df['time'] = pd.to_datetime(df['date'], format="%Y-%m-%d", errors='coerce')
        df = df.dropna(subset=['time'])
        for col in self.indicators:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        if 'isST' in df.columns:
            df['isST'] = pd.to_numeric(df['isST'], errors='coerce')
        keep_cols = ['time'] + [c for c in self.indicators if c in df.columns]
        return df[keep_cols]

    def _merge_with_existing(self, stock_code: str, indicator_df: pd.DataFrame) -> pd.DataFrame:
        """兼容旧调用：仅以已有日线为左表，不引入指标独有日期。"""
        base_df = self.manager.get_kline_data(
            stock_code,
            '1d',
            self.start_date,
            self.end_date
        )
        if base_df is None or base_df.empty:
            return pd.DataFrame()
        base_df = base_df.copy()
        if 'time' not in base_df.columns:
            return pd.DataFrame()
        base_df['time'] = pd.to_datetime(base_df['time'], errors='coerce')
        indicator_df = indicator_df.copy()
        indicator_df['time'] = pd.to_datetime(indicator_df['time'], errors='coerce')
        base_df = base_df.dropna(subset=['time']).set_index('time')
        indicator_df = indicator_df.dropna(subset=['time']).set_index('time')
        indicator_cols = [c for c in self.indicators if c in indicator_df.columns]
        for col in indicator_cols:
            if col not in base_df.columns:
                base_df[col] = pd.NA
        indicator_aligned = indicator_df[indicator_cols].reindex(base_df.index)
        for col in indicator_cols:
            base_df[col] = indicator_aligned[col].combine_first(base_df[col])
        merged = base_df.reset_index()
        merged = merged.dropna(subset=['time'])
        merged = merged.sort_values('time')
        merged = merged.reset_index(drop=True)
        return merged

    def run(self):
        results = {
            'success': 0,
            'failed': 0,
            'empty': 0,
            'up_to_date': 0,
            'no_local_daily': 0,
            'total_records': 0,
            'unprocessed': 0,
            'cancelled': False,
            'limit_reached': False,
            'unresolved_count': 0,
        }
        limit_reached = False
        importer = None
        try:
            try:
                from .baostock_import_worker import MultiProcessBaoStockImporter
            except ImportError:
                try:
                    # 兼容直接运行 viewer.py 的源码调试方式。
                    from baostock_import_worker import MultiProcessBaoStockImporter
                except ImportError:
                    self.error.emit(
                        "未找到BaoStock多进程下载模块，请更新到最新版本。"
                    )
                    return

            tasks = []
            planned_stocks = set()
            for stock_code in self.stocks:
                try:
                    ranges, local_count = self._indicator_download_ranges(stock_code)
                    if local_count <= 0:
                        results['no_local_daily'] += 1
                        self.status.emit(f"○ {stock_code} 没有可匹配的本地日线，跳过指标请求")
                    elif not ranges:
                        results['up_to_date'] += 1
                    else:
                        planned_stocks.add(stock_code)
                        for range_start, range_end in ranges:
                            tasks.append((stock_code, range_start, range_end))
                except Exception as exc:
                    results['failed'] += 1
                    self.status.emit(f"✗ {stock_code} 指标增量扫描失败: {exc}")
                finally:
                    try:
                        self.manager.close_stock_connection(
                            stock_code, skip_checkpoint=True,
                        )
                    except Exception:
                        pass

            total_tasks = len(tasks)
            completed_tasks = 0
            if total_tasks <= 0:
                if results['failed']:
                    self.status.emit(
                        f"指标增量扫描结束，但有 {results['failed']} 只股票扫描失败"
                    )
                elif results['no_local_daily'] and not results['up_to_date']:
                    self.status.emit("所选股票没有可匹配的本地日线，未发起指标请求")
                else:
                    self.status.emit("所选股票的 BaoStock 指标均已补齐，无需发起请求")
                self.finished.emit(results)
                return

            importer = MultiProcessBaoStockImporter(
                num_workers=min(self.num_workers, total_tasks),
                timeout_per_task=self.timeout_per_task,
                max_task_retries=2,
                retry_backoff=(2.0, 5.0),
            )
            self._mp_importer = importer
            importer.start()
            next_submit = 0
            in_flight = 0
            id_to_task = {}

            def consume_request() -> bool:
                if not self.request_tracker:
                    return True
                if not self.request_tracker.consume(1):
                    return False
                self.request_count.emit(self.request_tracker.get_count())
                return True

            def submit_next() -> bool:
                nonlocal next_submit, in_flight, limit_reached
                if next_submit >= total_tasks or not self._is_running:
                    return False
                if not consume_request():
                    limit_reached = True
                    self._is_running = False
                    self.status.emit(self.request_tracker.limit_message)
                    return False
                stock_code, range_start, range_end = tasks[next_submit]
                task_id = importer.add_indicator_task(
                    stock_code,
                    self._to_baostock_code(stock_code),
                    range_start,
                    range_end,
                    self.fetch_indicators,
                )
                id_to_task[task_id] = (stock_code, range_start, range_end)
                next_submit += 1
                in_flight += 1
                return True

            for _ in range(min(self.num_workers, total_tasks)):
                if not submit_next():
                    break

            while in_flight > 0 and self._is_running:
                result = importer.get_result(timeout=0.25)
                for event in importer.get_all_progress():
                    event_type = event.get('type')
                    if event_type == 'retry':
                        if not consume_request():
                            limit_reached = True
                            self._is_running = False
                            self.status.emit(self.request_tracker.limit_message)
                            break
                        self.status.emit(
                            f"↻ {event.get('stock_code')} 网络或登录异常，"
                            f"{event.get('delay', 0):g}秒后进行第{event.get('attempt')}次重试"
                        )
                    elif event_type == 'fatal':
                        self.status.emit(str(event.get('msg') or 'BaoStock工作进程异常'))
                if not self._is_running:
                    break
                if not result:
                    continue

                task_id = int(result.get('task_id', 0) or 0)
                task = id_to_task.pop(task_id, None)
                stock_code = (task or ('', '', ''))[0] or result.get('stock_code', '')
                indicator_df = None
                try:
                    if result.get('success'):
                        df_dict = result.get('df_dict')
                        indicator_df = dict_to_dataframe(df_dict) if df_dict else pd.DataFrame()
                        if indicator_df is None or indicator_df.empty:
                            results['empty'] += 1
                            self.status.emit(f"○ {stock_code} 无指标数据")
                        else:
                            records = self.manager.update_daily_indicators(
                                indicator_df,
                                stock_code,
                                columns=self.fetch_indicators,
                            )
                            if records <= 0:
                                results['empty'] += 1
                                self.status.emit(f"○ {stock_code} 无匹配的本地日线，已跳过指标")
                            else:
                                results['success'] += 1
                                results['total_records'] += records
                                self.status.emit(f"✓ {stock_code} 指标更新 {records} 条日线")
                    else:
                        results['failed'] += 1
                        self.status.emit(
                            f"✗ {stock_code} 失败（已重试）: "
                            f"{result.get('error', '未知错误')}"
                        )
                except Exception as exc:
                    results['failed'] += 1
                    self.status.emit(f"✗ {stock_code} 写入失败: {exc}")
                finally:
                    if indicator_df is not None:
                        del indicator_df
                    try:
                        self.manager.close_stock_connection(
                            stock_code, skip_checkpoint=True
                        )
                    except Exception:
                        pass

                completed_tasks += 1
                in_flight -= 1
                percent = int((completed_tasks / total_tasks) * 100)
                self.progress.emit(percent, completed_tasks, total_tasks)
                submit_next()

            if importer is not None:
                importer.stop(timeout=2.0)
                importer = None
                self._mp_importer = None
            if self._is_running and not self.force_refresh:
                unresolved_count = 0
                for stock_code in planned_stocks:
                    try:
                        ranges, _ = self._indicator_download_ranges(stock_code)
                        if ranges:
                            # 复核清单按范围展示即可；精确日期仍可由下一次扫描恢复。
                            unresolved_count += len(ranges)
                    except Exception as exc:
                        unresolved_count += 1
                        self.status.emit(f"警告：{stock_code} 指标导入后复核失败: {exc}")
                    finally:
                        try:
                            self.manager.close_stock_connection(
                                stock_code, skip_checkpoint=True,
                            )
                        except Exception:
                            pass
                results['unresolved_count'] = unresolved_count
                if unresolved_count:
                    self.status.emit(
                        f"警告：仍有 {unresolved_count} 个指标缺口区间待核验"
                    )
            processed = results['success'] + results['failed'] + results['empty']
            results['unprocessed'] = max(0, total_tasks - processed)
            results['cancelled'] = not self._is_running and not limit_reached
            results['limit_reached'] = limit_reached
            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            if importer is not None:
                try:
                    importer.force_stop()
                except Exception:
                    pass
            self._mp_importer = None
            if self.request_tracker:
                try:
                    self.request_tracker.flush()
                except Exception:
                    pass


class BaoStockImportDialog(QDialog):
    def __init__(self, manager: DuckDBManager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.import_thread = None
        self.indicator_thread = None
        self.font_scale = get_ui_font_scale()
        self._base_style_raw = None
        self._import_start_time = None
        self._indicator_start_time = None
        self._close_after_stop = False
        self._close_poll_scheduled = False

        self.setWindowTitle("从BaoStock导入数据")
        self.setMinimumSize(800, 700)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowFlags(self.windowFlags() | Qt.Window)
        self._set_dark_titlebar()

        self._base_style_raw = """
            QDialog, QWidget {
                background-color: #333333;
                color: #e8e8e8;
                font-family: "Microsoft YaHei UI";
            }
            QLabel {
                color: #e8e8e8;
            }
            QLineEdit, QTextEdit {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px;
            }
            QPushButton {
                background-color: #0078d4;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-family: "Microsoft YaHei UI";
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:pressed {
                background-color: #005a9e;
            }
            QPushButton:disabled {
                background-color: #555555;
                color: #888888;
            }
            QComboBox {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px;
            }
            QComboBox QAbstractItemView {
                background-color: #3c3c3c;
                color: #e8e8e8;
                selection-background-color: #0078d4;
            }
            QCheckBox {
                color: #e8e8e8;
                min-height: 24px;
                spacing: 6px;
                padding: 2px 0;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 1px solid #555555;
                border-radius: 3px;
                background-color: #3c3c3c;
            }
            QCheckBox::indicator:checked {
                background-color: #0078d4;
                border-color: #0078d4;
            }
            QRadioButton {
                color: #e8e8e8;
            }
            QRadioButton::indicator {
                width: 18px;
                height: 18px;
                border: 1px solid #555555;
                border-radius: 9px;
                background-color: #3c3c3c;
            }
            QRadioButton::indicator:checked {
                background-color: #0078d4;
                border-color: #0078d4;
            }
            QGroupBox {
                border: 1px solid #555555;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
                color: #e8e8e8;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QProgressBar {
                background-color: #3c3c3c;
                border: 1px solid #555555;
                border-radius: 3px;
                text-align: center;
                color: #e8e8e8;
            }
            QProgressBar::chunk {
                background-color: #0078d4;
            }
            QSpinBox, QDateEdit {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px;
            }
            QFrame {
                background-color: #333333;
                color: #e8e8e8;
            }
        """
        _ui_font = get_preferred_ui_font_family() or "Microsoft YaHei UI"
        if _ui_font != "Microsoft YaHei UI":
            self._base_style_raw = self._base_style_raw.replace('"Microsoft YaHei UI"', f'"{_ui_font}"')
        self.setStyleSheet(self._scale_stylesheet(self._base_style_raw, self.font_scale))
        data_root = getattr(self.manager, "data_root", None) or os.getcwd()
        self._request_tracker = get_baostock_request_tracker(data_root)
        self.init_ui()
        self.apply_ui_scale(self.font_scale)
        self._refresh_request_usage_label()

    def _set_dark_titlebar(self):
        try:
            import platform
            if platform.system() == "Windows":
                from ctypes import windll, c_int, byref, sizeof
                from ctypes.wintypes import DWORD
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                DWMWA_CAPTION_COLOR = 35
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_USE_IMMERSIVE_DARK_MODE,
                    byref(c_int(2)),
                    sizeof(c_int)
                )
                caption_color = DWORD(0x333333)
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_CAPTION_COLOR,
                    byref(caption_color),
                    sizeof(caption_color)
                )
        except Exception:
            pass

    def _scale_stylesheet(self, style: str, scale: float) -> str:
        if not style:
            return style
        import re

        def repl(match):
            value = float(match.group(1))
            unit = match.group(2)
            scaled = max(6, int(round(value * float(scale))))
            return f"font-size: {scaled}{unit}"

        return re.sub(
            r"font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(px|pt)",
            repl,
            style,
            flags=re.IGNORECASE
        )

    def _set_scaled_stylesheet(self, widget: QWidget, style: str):
        widget.setProperty("ui_base_stylesheet", style)
        widget.setStyleSheet(self._scale_stylesheet(style, self.font_scale))

    def _set_scaled_font(self, widget: QWidget, base_pt: int):
        widget.setProperty("ui_base_font_pt", base_pt)
        font = widget.font()
        font.setPointSize(max(6, int(round(base_pt * self.font_scale))))
        widget.setFont(font)

    def apply_ui_scale(self, scale=None):
        if scale is None:
            scale = get_ui_font_scale()
        self.font_scale = scale
        if self._base_style_raw:
            self.setStyleSheet(self._scale_stylesheet(self._base_style_raw, self.font_scale))
        try:
            for child in self.findChildren(QWidget):
                base_ss = child.property("ui_base_stylesheet")
                if base_ss:
                    child.setStyleSheet(self._scale_stylesheet(base_ss, self.font_scale))
                base_pt = child.property("ui_base_font_pt")
                if base_pt:
                    font = child.font()
                    font.setPointSize(max(6, int(round(float(base_pt) * self.font_scale))))
                    child.setFont(font)
            # 复选框/单选框高度需随字号缩放，否则放大字体后指示器与文字会被裁切
            for box in self.findChildren(QCheckBox) + self.findChildren(QRadioButton):
                row_h = max(QFontMetrics(box.font()).height() + 10, 24)
                box.setMinimumHeight(row_h)
        except Exception:
            pass

    def showEvent(self, event):
        """首次显示后把高度撑到内容所需，避免底部按钮落在可视区外。"""
        super().showEvent(event)
        if getattr(self, "_content_height_fitted", False):
            return
        self._content_height_fitted = True
        QTimer.singleShot(0, lambda: fit_dialog_height_to_content(self, getattr(self, "tabs", None)))

    def init_ui(self):
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.kline_tab = QWidget()
        self.indicator_tab = QWidget()
        self.tabs.addTab(self.kline_tab, "行情数据")
        self.tabs.addTab(self.indicator_tab, "指标下载")
        layout.addWidget(self.tabs)
        self._build_kline_tab()
        self._build_indicator_tab()

    def _build_kline_tab(self):
        # 与指标页一致，用滚动区域包裹，避免高分屏放大字号后分组被压扁
        outer_layout = QVBoxLayout(self.kline_tab)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        layout = QVBoxLayout(content)

        stock_group = QGroupBox("股票池选择")
        stock_layout = QVBoxLayout(stock_group)

        method_layout = QHBoxLayout()
        self.method_group = QButtonGroup(self)
        self.preset_radio = QRadioButton("预设板块")
        self.preset_radio.setChecked(True)
        self.method_group.addButton(self.preset_radio)
        method_layout.addWidget(self.preset_radio)

        self.file_radio = QRadioButton("从文件导入")
        self.method_group.addButton(self.file_radio)
        method_layout.addWidget(self.file_radio)

        self.manual_radio = QRadioButton("手动输入")
        self.method_group.addButton(self.manual_radio)
        method_layout.addWidget(self.manual_radio)

        method_layout.addStretch()
        stock_layout.addLayout(method_layout)

        self.preset_frame = QFrame()
        preset_layout = QGridLayout(self.preset_frame)
        preset_layout.setContentsMargins(0, 0, 0, 0)
        self.preset_checks = {}
        presets = [
            ('沪深A股', 'all_a'),
            ('上证A股', 'sh_a'),
            ('深证A股', 'sz_a'),
            ('沪深300', 'hs300'),
            ('上证50', 'sz50'),
            ('中证500', 'zz500'),
            ('创业板', 'cyb'),
            ('科创板', 'kcb'),
            ('沪深ETF', 'hs_etf'),
            ('沪深场内基金（含ETF/LOF）', 'hs_fund'),
            ('沪深转债', 'hs_convertible_bonds'),
            ('T0型ETF', 't0_etf'),
            ('常用指数', 'common_index'),
        ]
        for i, (name, key) in enumerate(presets):
            cb = QCheckBox(name)
            self.preset_checks[key] = cb
            preset_layout.addWidget(cb, i // 4, i % 4)

        stock_layout.addWidget(self.preset_frame)

        self.file_frame = QFrame()
        file_layout = QHBoxLayout(self.file_frame)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_path_edit = QLineEdit()
        self.file_path_edit.setPlaceholderText("选择股票列表CSV文件...")
        file_layout.addWidget(self.file_path_edit)
        self.browse_file_btn = QPushButton("浏览...")
        self.browse_file_btn.clicked.connect(self.browse_stock_file)
        file_layout.addWidget(self.browse_file_btn)
        self.file_frame.setVisible(False)
        stock_layout.addWidget(self.file_frame)

        self.manual_frame = QFrame()
        manual_layout = QVBoxLayout(self.manual_frame)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.addWidget(QLabel("输入股票代码（每行一个，支持纯数字或带市场后缀）:"))
        self.manual_edit = QTextEdit()
        self.manual_edit.setMaximumHeight(100)
        self.manual_edit.setPlaceholderText("000001.SZ 或 000001\n600000.SH 或 600000\n300750")
        manual_layout.addWidget(self.manual_edit)
        self.manual_frame.setVisible(False)
        stock_layout.addWidget(self.manual_frame)

        self.preset_radio.toggled.connect(self.on_method_changed)
        self.file_radio.toggled.connect(self.on_method_changed)
        self.manual_radio.toggled.connect(self.on_method_changed)

        layout.addWidget(stock_group)

        period_group = QGroupBox("数据周期设置")
        period_layout = QGridLayout(period_group)
        today = QDate.currentDate()

        self.period_1d_check = QCheckBox("日线 (1d)")
        self.period_1d_check.setChecked(True)
        period_layout.addWidget(self.period_1d_check, 0, 0)
        period_layout.addWidget(QLabel("开始:"), 0, 1)
        self.period_1d_start = QDateEdit()
        self.period_1d_start.setCalendarPopup(True)
        self.period_1d_start.setDate(today.addYears(-10))
        period_layout.addWidget(self.period_1d_start, 0, 2)
        period_layout.addWidget(QLabel("结束:"), 0, 3)
        self.period_1d_end = QDateEdit()
        self.period_1d_end.setCalendarPopup(True)
        self.period_1d_end.setDate(today)
        period_layout.addWidget(self.period_1d_end, 0, 4)

        self.period_5m_check = QCheckBox("5分钟 (5m)")
        self.period_5m_check.setChecked(True)
        period_layout.addWidget(self.period_5m_check, 1, 0)
        period_layout.addWidget(QLabel("开始:"), 1, 1)
        self.period_5m_start = QDateEdit()
        self.period_5m_start.setCalendarPopup(True)
        self.period_5m_start.setDate(today.addYears(-1))
        period_layout.addWidget(self.period_5m_start, 1, 2)
        period_layout.addWidget(QLabel("结束:"), 1, 3)
        self.period_5m_end = QDateEdit()
        self.period_5m_end.setCalendarPopup(True)
        self.period_5m_end.setDate(today)
        period_layout.addWidget(self.period_5m_end, 1, 4)

        layout.addWidget(period_group)

        adjustment_group = QGroupBox("价格字段（原始价必存，复权价可选）")
        adjustment_layout = QHBoxLayout(adjustment_group)
        self.adjustment_none_check = QCheckBox("原始价（必存）")
        self.adjustment_front_check = QCheckBox("前复权")
        self.adjustment_back_check = QCheckBox("后复权")
        self.adjustment_none_check.setChecked(True)
        self.adjustment_none_check.setEnabled(False)
        self.adjustment_none_check.setToolTip(
            "DuckDB 以不复权行情作为基础字段，导入时始终保存。"
        )
        # 保留原窗口默认同时下载前/后复权的行为，用户现在可以按需取消。
        self.adjustment_front_check.setChecked(True)
        self.adjustment_back_check.setChecked(True)
        self.adjustment_front_check.setToolTip(
            "保存 open_front/high_front/low_front/close_front；"
            "增量补充时可能刷新本地完整历史，以保持同一复权基准。"
        )
        self.adjustment_back_check.setToolTip(
            "保存 open_back/high_back/low_back/close_back。"
        )
        adjustment_layout.addWidget(self.adjustment_none_check)
        adjustment_layout.addWidget(self.adjustment_front_check)
        adjustment_layout.addWidget(self.adjustment_back_check)
        adjustment_layout.addStretch()
        layout.addWidget(adjustment_group)

        mode_group = QGroupBox("导入方式")
        mode_layout = QVBoxLayout(mode_group)
        mode_hint = QLabel(
            "默认增量：先核验本地每个交易日，只请求缺失或条数不完整的区间；"
            "尚未收盘的当天不会被误判为缺失。"
        )
        mode_hint.setWordWrap(True)
        mode_layout.addWidget(mode_hint)
        self.force_overwrite_check = QCheckBox(
            "强制覆写已有数据（跳过增量检测，重新下载所选完整区间）"
        )
        self.force_overwrite_check.setChecked(False)
        self.force_overwrite_check.setToolTip(
            "仅在确认本地历史数据需要整体重建时使用；默认无需勾选。"
        )
        mode_layout.addWidget(self.force_overwrite_check)
        layout.addWidget(mode_group)

        progress_group = QGroupBox("进度信息")
        progress_layout = QVBoxLayout(progress_group)
        self.progress_bar = QProgressBar()
        progress_layout.addWidget(self.progress_bar)
        usage_layout = QHBoxLayout()
        self.request_usage_label = QLabel("软件每日BaoStock请求上限3万次；当前：0/30000")
        usage_layout.addWidget(self.request_usage_label)
        usage_layout.addStretch()
        progress_layout.addLayout(usage_layout)

        status_layout = QHBoxLayout()
        self.import_status_label = QLabel("就绪")
        status_layout.addWidget(self.import_status_label)
        self.import_eta_label = QLabel("预计剩余：--")
        status_layout.addWidget(self.import_eta_label)
        status_layout.addStretch()
        progress_layout.addLayout(status_layout)

        layout.addWidget(progress_group)

        log_group = QGroupBox("执行日志")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        _configure_download_log_widget(self.log_text)
        log_layout.addWidget(self.log_text)
        layout.addWidget(log_group)

        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始导入")
        self.start_btn.clicked.connect(self.start_import)
        btn_layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_import)
        btn_layout.addWidget(self.stop_btn)

        btn_layout.addWidget(QLabel("下载进程数:"))
        self.baostock_workers_spin = QSpinBox()
        self.baostock_workers_spin.setRange(1, 8)
        self.baostock_workers_spin.setValue(2)
        self.baostock_workers_spin.setToolTip("BaoStock 并行下载进程数。建议 2~4；过多可能导致网络/请求上限消耗更快。")
        self.baostock_workers_spin.setFixedWidth(60)
        btn_layout.addWidget(self.baostock_workers_spin)

        btn_layout.addStretch()
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

    def _build_indicator_tab(self):
        # 指标页分组较多，整体高度可能超过窗口，用滚动区域包裹避免分组被压扁
        outer_layout = QVBoxLayout(self.indicator_tab)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        layout = QVBoxLayout(content)

        stock_group = QGroupBox("股票池选择")
        stock_layout = QVBoxLayout(stock_group)

        method_layout = QHBoxLayout()
        self.indicator_method_group = QButtonGroup(self)
        self.indicator_preset_radio = QRadioButton("预设板块")
        self.indicator_preset_radio.setChecked(True)
        self.indicator_method_group.addButton(self.indicator_preset_radio)
        method_layout.addWidget(self.indicator_preset_radio)

        self.indicator_file_radio = QRadioButton("从文件导入")
        self.indicator_method_group.addButton(self.indicator_file_radio)
        method_layout.addWidget(self.indicator_file_radio)

        self.indicator_manual_radio = QRadioButton("手动输入")
        self.indicator_method_group.addButton(self.indicator_manual_radio)
        method_layout.addWidget(self.indicator_manual_radio)

        method_layout.addStretch()
        stock_layout.addLayout(method_layout)

        self.indicator_preset_frame = QFrame()
        preset_layout = QGridLayout(self.indicator_preset_frame)
        preset_layout.setContentsMargins(0, 0, 0, 0)
        self.indicator_preset_checks = {}
        presets = [
            ('沪深A股', 'all_a'),
            ('上证A股', 'sh_a'),
            ('深证A股', 'sz_a'),
            ('沪深300', 'hs300'),
            ('上证50', 'sz50'),
            ('中证500', 'zz500'),
            ('创业板', 'cyb'),
            ('科创板', 'kcb'),
            ('沪深ETF', 'hs_etf'),
            ('沪深场内基金（含ETF/LOF）', 'hs_fund'),
            ('沪深转债', 'hs_convertible_bonds'),
            ('T0型ETF', 't0_etf'),
            ('常用指数', 'common_index'),
        ]
        for i, (name, key) in enumerate(presets):
            cb = QCheckBox(name)
            self.indicator_preset_checks[key] = cb
            preset_layout.addWidget(cb, i // 4, i % 4)

        stock_layout.addWidget(self.indicator_preset_frame)

        self.indicator_file_frame = QFrame()
        file_layout = QHBoxLayout(self.indicator_file_frame)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.indicator_file_path_edit = QLineEdit()
        self.indicator_file_path_edit.setPlaceholderText("选择股票列表CSV文件...")
        file_layout.addWidget(self.indicator_file_path_edit)
        self.indicator_browse_file_btn = QPushButton("浏览...")
        self.indicator_browse_file_btn.clicked.connect(self.browse_indicator_stock_file)
        file_layout.addWidget(self.indicator_browse_file_btn)
        self.indicator_file_frame.setVisible(False)
        stock_layout.addWidget(self.indicator_file_frame)

        self.indicator_manual_frame = QFrame()
        manual_layout = QVBoxLayout(self.indicator_manual_frame)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.addWidget(QLabel("输入股票代码（每行一个，支持纯数字或带市场后缀）:"))
        self.indicator_manual_edit = QTextEdit()
        self.indicator_manual_edit.setMaximumHeight(100)
        self.indicator_manual_edit.setPlaceholderText("000001.SZ 或 000001\n600000.SH 或 600000\n300750")
        manual_layout.addWidget(self.indicator_manual_edit)
        self.indicator_manual_frame.setVisible(False)
        stock_layout.addWidget(self.indicator_manual_frame)

        self.indicator_preset_radio.toggled.connect(self.on_indicator_method_changed)
        self.indicator_file_radio.toggled.connect(self.on_indicator_method_changed)
        self.indicator_manual_radio.toggled.connect(self.on_indicator_method_changed)

        layout.addWidget(stock_group)

        indicator_group = QGroupBox("指标字段（每次请求统一补齐全部字段）")
        indicator_layout = QGridLayout(indicator_group)
        self.indicator_checks = {}
        indicators = [
            ('turn', '换手率'),
            ('tradestatus', '交易状态'),
            ('pctChg', '涨跌幅'),
            ('peTTM', '滚动市盈率'),
            ('psTTM', '滚动市销率'),
            ('pcfNcfTTM', '滚动市现率'),
            ('pbMRQ', '市净率'),
            ('isST', '是否ST'),
        ]
        for i, (field, label) in enumerate(indicators):
            cb = QCheckBox(f"{label} ({field})")
            cb.setChecked(True)
            cb.setEnabled(False)
            cb.setToolTip("BaoStock 同一次请求可返回整组指标，统一补齐能提供可靠的增量完成证据。")
            self.indicator_checks[field] = cb
            indicator_layout.addWidget(cb, i // 2, i % 2)
        layout.addWidget(indicator_group)

        date_group = QGroupBox("日期范围")
        date_layout = QHBoxLayout(date_group)
        today = QDate.currentDate()
        date_layout.addWidget(QLabel("开始:"))
        self.indicator_start = QDateEdit()
        self.indicator_start.setCalendarPopup(True)
        self.indicator_start.setDate(today.addYears(-10))
        date_layout.addWidget(self.indicator_start)
        date_layout.addWidget(QLabel("结束:"))
        self.indicator_end = QDateEdit()
        self.indicator_end.setCalendarPopup(True)
        self.indicator_end.setDate(today)
        date_layout.addWidget(self.indicator_end)
        layout.addWidget(date_group)

        self.indicator_force_refresh_check = QCheckBox(
            "强制重新请求所选日期范围（默认仅补本地日线中尚未导入的指标）"
        )
        self.indicator_force_refresh_check.setChecked(False)
        self.indicator_force_refresh_check.setToolTip(
            "默认无需勾选；仅在确认历史指标需要整体刷新时使用。"
        )
        layout.addWidget(self.indicator_force_refresh_check)

        progress_group = QGroupBox("进度信息")
        progress_layout = QVBoxLayout(progress_group)
        self.indicator_progress_bar = QProgressBar()
        progress_layout.addWidget(self.indicator_progress_bar)
        usage_layout2 = QHBoxLayout()
        self.indicator_request_usage_label = QLabel("软件每日BaoStock请求上限3万次；当前：0/30000（指标请求也计入）")
        usage_layout2.addWidget(self.indicator_request_usage_label)
        usage_layout2.addStretch()
        progress_layout.addLayout(usage_layout2)

        status_layout = QHBoxLayout()
        self.indicator_status_label = QLabel("就绪")
        status_layout.addWidget(self.indicator_status_label)
        self.indicator_eta_label = QLabel("预计剩余：--")
        status_layout.addWidget(self.indicator_eta_label)
        status_layout.addStretch()
        progress_layout.addLayout(status_layout)

        layout.addWidget(progress_group)

        log_group = QGroupBox("执行日志")
        log_layout = QVBoxLayout(log_group)
        self.indicator_log_text = QTextEdit()
        self.indicator_log_text.setReadOnly(True)
        _configure_download_log_widget(self.indicator_log_text)
        log_layout.addWidget(self.indicator_log_text)
        layout.addWidget(log_group)

        btn_layout = QHBoxLayout()
        self.indicator_start_btn = QPushButton("开始下载")
        self.indicator_start_btn.clicked.connect(self.start_indicator_import)
        btn_layout.addWidget(self.indicator_start_btn)

        self.indicator_stop_btn = QPushButton("停止")
        self.indicator_stop_btn.setEnabled(False)
        self.indicator_stop_btn.clicked.connect(self.stop_indicator_import)
        btn_layout.addWidget(self.indicator_stop_btn)

        btn_layout.addStretch()
        self.indicator_close_btn = QPushButton("关闭")
        self.indicator_close_btn.clicked.connect(self.close)
        btn_layout.addWidget(self.indicator_close_btn)

        layout.addLayout(btn_layout)

    def on_method_changed(self):
        self.preset_frame.setVisible(self.preset_radio.isChecked())
        self.file_frame.setVisible(self.file_radio.isChecked())
        self.manual_frame.setVisible(self.manual_radio.isChecked())

    def on_indicator_method_changed(self):
        self.indicator_preset_frame.setVisible(self.indicator_preset_radio.isChecked())
        self.indicator_file_frame.setVisible(self.indicator_file_radio.isChecked())
        self.indicator_manual_frame.setVisible(self.indicator_manual_radio.isChecked())

    def browse_stock_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择股票列表文件",
            "", "CSV文件 (*.csv);;所有文件 (*.*)"
        )
        if file_path:
            self.file_path_edit.setText(file_path)

    def browse_indicator_stock_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择股票列表文件",
            "", "CSV文件 (*.csv);;所有文件 (*.*)"
        )
        if file_path:
            self.indicator_file_path_edit.setText(file_path)

    def _normalize_stock_code_with_market(self, code: str) -> str:
        return _normalize_market_security_code(code)

    def _read_stock_file(self, file_path: str) -> List[str]:
        stocks = []
        try:
            df = pd.read_csv(file_path, dtype=str, header=None, encoding='utf-8-sig')
            if len(df) > 0 and len(df.columns) > 0:
                first_cell = str(df.iloc[0, 0])
                if '.SH' in first_cell or '.SZ' in first_cell or '.BJ' in first_cell:
                    raw_stocks = df.iloc[:, 0].dropna().tolist()
                else:
                    df = pd.read_csv(file_path, dtype=str, encoding='utf-8-sig')
                    raw_stocks = []
                    for col in df.columns:
                        if '代码' in col or 'code' in col.lower():
                            raw_stocks.extend(df[col].dropna().tolist())
                            break
                    else:
                        if len(df.columns) > 0:
                            raw_stocks.extend(df.iloc[:, 0].dropna().tolist())
                for code in raw_stocks:
                    normalized_code = self._normalize_stock_code_with_market(code)
                    stocks.append(normalized_code)
        except Exception as e:
            self.log(f"读取文件失败: {e}")
        return stocks

    def get_stock_list(self) -> List[str]:
        stocks = []
        if self.preset_radio.isChecked():
            legacy_dirs = [
                os.path.join(os.path.dirname(os.path.dirname(__file__)), 'stock_lists'),
                os.path.join(os.path.dirname(__file__), 'stock_lists'),
            ]
            preset_files = {
                'all_a': '沪深A股_股票列表.csv',
                'sh_a': '上证A股_股票列表.csv',
                'sz_a': '深证A股_股票列表.csv',
                'hs300': '沪深300成分股_股票列表.csv',
                'sz50': '上证50成分股_股票列表.csv',
                'zz500': '中证500成分股_股票列表.csv',
                'cyb': '创业板_股票列表.csv',
                'kcb': '科创板_股票列表.csv',
                'hs_etf': '沪深ETF_成分股列表.csv',
                'hs_fund': '沪深基金_列表.csv',
                'hs_convertible_bonds': '沪深转债_列表.csv',
                't0_etf': 'T0型ETF.csv',
                'common_index': '指数_股票列表.csv',
            }
            for key, cb in self.preset_checks.items():
                if cb.isChecked() and key in preset_files:
                    file_path = _resolve_preset_stock_pool_file(
                        preset_files[key], legacy_dirs
                    )
                    if file_path:
                        stocks.extend(self._read_stock_file(file_path))
        elif self.file_radio.isChecked():
            file_path = self.file_path_edit.text().strip()
            if file_path and os.path.exists(file_path):
                stocks = self._read_stock_file(file_path)
        elif self.manual_radio.isChecked():
            text = self.manual_edit.toPlainText().strip()
            for line in text.split('\n'):
                code = line.strip()
                if code:
                    normalized_code = self._normalize_stock_code_with_market(code)
                    stocks.append(normalized_code)
        return list(set(stocks))

    def get_indicator_stock_list(self) -> List[str]:
        stocks = []
        if self.indicator_preset_radio.isChecked():
            legacy_dirs = [
                os.path.join(os.path.dirname(os.path.dirname(__file__)), 'stock_lists'),
                os.path.join(os.path.dirname(__file__), 'stock_lists'),
            ]
            preset_files = {
                'all_a': '沪深A股_股票列表.csv',
                'sh_a': '上证A股_股票列表.csv',
                'sz_a': '深证A股_股票列表.csv',
                'hs300': '沪深300成分股_股票列表.csv',
                'sz50': '上证50成分股_股票列表.csv',
                'zz500': '中证500成分股_股票列表.csv',
                'cyb': '创业板_股票列表.csv',
                'kcb': '科创板_股票列表.csv',
                'hs_etf': '沪深ETF_成分股列表.csv',
                'hs_fund': '沪深基金_列表.csv',
                'hs_convertible_bonds': '沪深转债_列表.csv',
                't0_etf': 'T0型ETF.csv',
                'common_index': '指数_股票列表.csv',
            }
            for key, cb in self.indicator_preset_checks.items():
                if cb.isChecked() and key in preset_files:
                    file_path = _resolve_preset_stock_pool_file(
                        preset_files[key], legacy_dirs
                    )
                    if file_path:
                        stocks.extend(self._read_stock_file(file_path))
        elif self.indicator_file_radio.isChecked():
            file_path = self.indicator_file_path_edit.text().strip()
            if file_path and os.path.exists(file_path):
                stocks = self._read_stock_file(file_path)
        elif self.indicator_manual_radio.isChecked():
            text = self.indicator_manual_edit.toPlainText().strip()
            for line in text.split('\n'):
                code = line.strip()
                if code:
                    normalized_code = self._normalize_stock_code_with_market(code)
                    stocks.append(normalized_code)
        return list(set(stocks))

    def get_periods_config(self) -> Dict[str, dict]:
        periods = {}
        if self.period_1d_check.isChecked():
            periods['1d'] = {
                'start': self.period_1d_start.date().toString("yyyy-MM-dd"),
                'end': self.period_1d_end.date().toString("yyyy-MM-dd")
            }
        if self.period_5m_check.isChecked():
            periods['5m'] = {
                'start': self.period_5m_start.date().toString("yyyy-MM-dd"),
                'end': self.period_5m_end.date().toString("yyyy-MM-dd")
            }
        return periods

    def get_adjustments_config(self) -> Tuple[bool, bool]:
        """返回用户选择的（前复权, 后复权）；原始价始终保存。"""
        return (
            bool(self.adjustment_front_check.isChecked()),
            bool(self.adjustment_back_check.isChecked()),
        )

    def log(self, message: str):
        _append_download_log(self.log_text, f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def start_import(self):
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        try:
            self._import_start_time = datetime.now()
            self.import_eta_label.setText("预计剩余：--")
            if self._request_tracker.is_limit_reached():
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                QMessageBox.warning(self, "提示", self._request_tracker.limit_message)
                return
            try:
                import baostock as bs
            except ImportError:
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                QMessageBox.warning(
                    self,
                    "导入错误",
                    "无法导入baostock模块，请先安装BaoStock。\n\n安装命令：pip install baostock"
                )
                return
            stocks = self.get_stock_list()
            if not stocks:
                QMessageBox.warning(self, "提示", "请选择要导入的股票")
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                return
            periods_config = self.get_periods_config()
            if not periods_config:
                QMessageBox.warning(self, "提示", "请至少选择一个数据周期")
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                return

            include_front_adjusted, include_back_adjusted = (
                self.get_adjustments_config()
            )
            invalid_periods = [
                period for period, config in periods_config.items()
                if config["start"] > config["end"]
            ]
            if invalid_periods:
                QMessageBox.warning(
                    self,
                    "提示",
                    f"以下周期的开始日期晚于结束日期：{', '.join(invalid_periods)}",
                )
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                return

            self._refresh_request_usage_label()
            num_workers = 2
            try:
                if hasattr(self, "baostock_workers_spin") and self.baostock_workers_spin:
                    num_workers = int(self.baostock_workers_spin.value())
            except Exception:
                num_workers = 2

            self.import_thread = BaoStockImportThread(
                self.manager,
                stocks,
                periods_config,
                self._request_tracker,
                num_workers=num_workers,
                timeout_per_task=120.0,
                force_overwrite=self.force_overwrite_check.isChecked(),
                include_front_adjusted=include_front_adjusted,
                include_back_adjusted=include_back_adjusted,
            )
            self.import_thread.progress.connect(self.on_progress)
            self.import_thread.status.connect(self.on_status)
            self.import_thread.finished.connect(self.on_finished)
            self.import_thread.error.connect(self.on_error)
            self.import_thread.conflict.connect(self.on_conflict)
            self.import_thread.request_count.connect(self.on_request_count_changed)
            self.import_thread.start()
            adjustment_labels = ["原始价"]
            if include_front_adjusted:
                adjustment_labels.append("前复权")
            if include_back_adjusted:
                adjustment_labels.append("后复权")
            self.log(
                f"开始导入 {len(stocks)} 只股票；价格字段："
                + "、".join(adjustment_labels)
            )
        except Exception as e:
            self.on_error(str(e))

    def get_selected_indicators(self) -> List[str]:
        return [key for key, cb in self.indicator_checks.items() if cb.isChecked()]

    def start_indicator_import(self):
        self.indicator_start_btn.setEnabled(False)
        self.indicator_stop_btn.setEnabled(True)
        try:
            self._indicator_start_time = datetime.now()
            self.indicator_eta_label.setText("预计剩余：--")
            if self._request_tracker.is_limit_reached():
                self.indicator_start_btn.setEnabled(True)
                self.indicator_stop_btn.setEnabled(False)
                QMessageBox.warning(self, "提示", self._request_tracker.limit_message)
                return
            try:
                import baostock as bs
            except ImportError:
                self.indicator_start_btn.setEnabled(True)
                self.indicator_stop_btn.setEnabled(False)
                QMessageBox.warning(
                    self,
                    "导入错误",
                    "无法导入baostock模块，请先安装BaoStock。\n\n安装命令：pip install baostock"
                )
                return
            stocks = self.get_indicator_stock_list()
            if not stocks:
                QMessageBox.warning(self, "提示", "请选择要导入的股票")
                self.indicator_start_btn.setEnabled(True)
                self.indicator_stop_btn.setEnabled(False)
                return
            indicators = self.get_selected_indicators()
            if not indicators:
                QMessageBox.warning(self, "提示", "请至少选择一个指标")
                self.indicator_start_btn.setEnabled(True)
                self.indicator_stop_btn.setEnabled(False)
                return
            start_date = self.indicator_start.date().toString("yyyy-MM-dd")
            end_date = self.indicator_end.date().toString("yyyy-MM-dd")
            if self.indicator_start.date() > self.indicator_end.date():
                QMessageBox.warning(self, "提示", "指标开始日期不能晚于结束日期")
                self.indicator_start_btn.setEnabled(True)
                self.indicator_stop_btn.setEnabled(False)
                return
            self._refresh_request_usage_label()
            self.indicator_thread = BaoStockIndicatorImportThread(
                self.manager,
                stocks,
                start_date,
                end_date,
                indicators,
                self._request_tracker,
                force_refresh=self.indicator_force_refresh_check.isChecked(),
            )
            self.indicator_thread.progress.connect(self.on_indicator_progress)
            self.indicator_thread.status.connect(self.on_indicator_status)
            self.indicator_thread.finished.connect(self.on_indicator_finished)
            self.indicator_thread.error.connect(self.on_indicator_error)
            self.indicator_thread.request_count.connect(self.on_request_count_changed)
            self.indicator_thread.start()
            self.indicator_log(f"开始下载 {len(stocks)} 只股票指标")
        except Exception as e:
            self.on_indicator_error(str(e))

    def stop_indicator_import(self):
        if self.indicator_thread and self.indicator_thread.isRunning():
            self.indicator_thread.stop()
        self.indicator_stop_btn.setEnabled(False)
        self.indicator_eta_label.setText("预计剩余：--")
        self.indicator_log("已发送停止请求，等待当前写入和数据库收尾...")

    def _refresh_request_usage_label(self):
        count = self._request_tracker.get_count()
        total = self._request_tracker.display_limit
        self.request_usage_label.setText(f"软件每日BaoStock请求上限3万次；当前：{count}/{total}")
        self.indicator_request_usage_label.setText(f"软件每日BaoStock请求上限3万次；当前：{count}/{total}（指标请求也计入）")

    def on_request_count_changed(self, count: int):
        self._refresh_request_usage_label()

    def stop_import(self):
        if self.import_thread and self.import_thread.isRunning():
            self.import_thread.stop()
        self.stop_btn.setEnabled(False)
        self.import_eta_label.setText("预计剩余：--")
        self.log("已发送停止请求，等待当前写入和数据库收尾...")

    def on_progress(self, percent: int, completed: int, total: int):
        if not _should_update_download_ui(self, '_last_baostock_progress_ui_ts', completed, total):
            return
        self.progress_bar.setValue(percent)
        self.import_status_label.setText(f"进度: {completed}/{total}")
        self.import_eta_label.setText(self._estimate_eta_text(self._import_start_time, completed, total))

    def on_status(self, message: str):
        self.import_status_label.setText(message)
        self.log(message)

    def on_indicator_progress(self, percent: int, completed: int, total: int):
        if not _should_update_download_ui(self, '_last_baostock_indicator_progress_ui_ts', completed, total):
            return
        self.indicator_progress_bar.setValue(percent)
        self.indicator_status_label.setText(f"进度: {completed}/{total}")
        self.indicator_eta_label.setText(self._estimate_eta_text(self._indicator_start_time, completed, total))

    def on_indicator_status(self, message: str):
        self.indicator_status_label.setText(message)
        self.indicator_log(message)

    def on_conflict(self, stock: str, period: str, existing_count: int, new_count: int):
        msg = QMessageBox(self)
        msg.setWindowTitle("数据已存在")
        msg.setText(
            f"{stock} {period} 已存在 {existing_count} 条记录，准备导入 {new_count} 条。\n请选择处理方式:"
        )
        overwrite_btn = msg.addButton("覆盖", QMessageBox.AcceptRole)
        skip_btn = msg.addButton("跳过", QMessageBox.RejectRole)
        msg.setDefaultButton(skip_btn)
        checkbox = QCheckBox("后续都采用相同方式处理")
        msg.setCheckBox(checkbox)
        msg.exec_()
        chosen = msg.clickedButton()
        resolution = "overwrite" if chosen == overwrite_btn else "skip"
        apply_all = checkbox.isChecked()
        if self.import_thread:
            self.import_thread.set_conflict_resolution(resolution, apply_all)

    def on_finished(self, results: dict):
        cancelled = bool(results.get('cancelled'))
        unresolved_count = int(results.get('unresolved_count', 0) or 0)
        benchmark_unresolved = results.get('benchmark_unresolved') or []
        benchmark_error = str(results.get('benchmark_error') or '')
        benchmark_empty = int(results.get('benchmark_empty', 0) or 0)
        post_scan_errors = results.get('post_scan_errors') or []
        failed_count = int(results.get('failed', 0) or 0)
        empty_count = int(results.get('empty', 0) or 0)
        adjustment_failed = int(results.get('adjustment_failed', 0) or 0)
        limit_reached = bool(results.get('limit_reached'))
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if not cancelled:
            self.progress_bar.setValue(100)
        self.import_eta_label.setText("预计剩余：--" if cancelled else "预计剩余：00:00:00")
        self.log("导入已安全停止" if cancelled else "导入完成")
        self._refresh_parent_after_changes(results)
        if self._close_after_stop:
            self._schedule_close_when_idle()
            return
        if cancelled:
            title = "导入已停止"
        elif (
            adjustment_failed
            or limit_reached
            or failed_count
            or empty_count
            or unresolved_count
            or benchmark_unresolved
            or benchmark_error
            or benchmark_empty
            or post_scan_errors
        ):
            title = "导入完成（仍有待核验项）"
        else:
            title = "导入完成"
        summary = (
            f"已是最新: {results.get('up_to_date', 0)}\n"
            f"成功区间: {results.get('success', 0)}\n"
            f"失败区间: {results.get('failed', 0)}\n"
            f"无数据区间: {results.get('empty', 0)}\n"
            f"处理记录数: {results.get('total_records', 0)}"
        )
        if unresolved_count:
            summary += (
                f"\n待核验交易日: {unresolved_count}"
                "（可能停牌、未上市、数据源未返回或下载未完成）"
            )
        if benchmark_unresolved:
            summary += f"\n基准待核验交易日: {len(benchmark_unresolved)}"
        if benchmark_empty:
            summary += f"\n基准空返区间: {benchmark_empty}"
        if benchmark_error:
            summary += f"\n基准检查失败: {benchmark_error}"
        if post_scan_errors:
            summary += f"\n复核失败的股票周期: {len(post_scan_errors)}"
        if adjustment_failed:
            summary += f"\n复权未完整的区间: {adjustment_failed}"
        if limit_reached:
            summary += "\nBaoStock 请求保护上限已触发，未完成项保持待补"
        if (
            adjustment_failed
            or limit_reached
            or failed_count
            or empty_count
            or unresolved_count
            or benchmark_unresolved
            or benchmark_error
            or benchmark_empty
            or post_scan_errors
        ):
            QMessageBox.warning(self, title, summary)
        else:
            QMessageBox.information(self, title, summary)

    def on_indicator_finished(self, results: dict):
        cancelled = bool(results.get('cancelled'))
        unresolved_count = int(results.get('unresolved_count', 0) or 0)
        failed_count = int(results.get('failed', 0) or 0)
        self.indicator_start_btn.setEnabled(True)
        self.indicator_stop_btn.setEnabled(False)
        if not cancelled:
            self.indicator_progress_bar.setValue(100)
        self.indicator_eta_label.setText("预计剩余：--" if cancelled else "预计剩余：00:00:00")
        self.indicator_log("下载已安全停止" if cancelled else "下载完成")
        self._refresh_parent_after_changes(results)
        if self._close_after_stop:
            self._schedule_close_when_idle()
            return
        title = (
            "下载已停止" if cancelled
            else "下载完成（仍有待核验项）" if failed_count or unresolved_count
            else "下载完成"
        )
        summary = (
            f"已是最新: {results.get('up_to_date', 0)}\n"
            f"无本地日线: {results.get('no_local_daily', 0)}\n"
            f"成功区间: {results.get('success', 0)}\n"
            f"失败: {results.get('failed', 0)}\n"
            f"无数据区间: {results.get('empty', 0)}\n"
            f"更新日线数: {results.get('total_records', 0)}"
        )
        if unresolved_count:
            summary += f"\n待核验指标缺口区间: {unresolved_count}"
        if failed_count or unresolved_count:
            QMessageBox.warning(self, title, summary)
        else:
            QMessageBox.information(self, title, summary)

    def on_error(self, error: str):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.import_eta_label.setText("预计剩余：--")
        if self._close_after_stop:
            self._schedule_close_when_idle()
        else:
            QMessageBox.warning(self, "导入错误", error)

    def on_indicator_error(self, error: str):
        self.indicator_start_btn.setEnabled(True)
        self.indicator_stop_btn.setEnabled(False)
        self.indicator_eta_label.setText("预计剩余：--")
        if self._close_after_stop:
            self._schedule_close_when_idle()
        else:
            QMessageBox.warning(self, "导入错误", error)

    def _refresh_parent_after_changes(self, results: dict):
        if not results.get('total_records', 0):
            return
        parent = self.parent()
        parent_closing = bool(
            parent
            and (
                getattr(parent, '_viewer_closing', False)
                or getattr(parent, '_pending_close_after_tushare_stop', False)
            )
        )
        if parent_closing or parent is None:
            return
        try:
            if hasattr(parent, 'on_refresh_clicked'):
                parent.on_refresh_clicked()
            elif hasattr(parent, 'refresh_stock_list'):
                parent.refresh_stock_list()
        except Exception:
            pass

    def is_import_running(self) -> bool:
        try:
            return bool(
                (self.import_thread and self.import_thread.isRunning())
                or (self.indicator_thread and self.indicator_thread.isRunning())
            )
        except RuntimeError:
            return False

    def request_close_after_stop(self):
        """停止领取新任务，待线程和数据库收尾完成后自动关闭。"""
        if not self.is_import_running():
            self.close()
            return
        self._close_after_stop = True
        for button in (
            self.start_btn, self.stop_btn, self.close_btn,
            self.indicator_start_btn, self.indicator_stop_btn,
            self.indicator_close_btn,
        ):
            button.setEnabled(False)
        if self.import_thread and self.import_thread.isRunning():
            self.import_thread.stop()
            self.import_status_label.setText("正在安全停止并收尾数据库...")
            self.log("正在安全停止：等待当前写入和数据库连接收尾后关闭窗口...")
        if self.indicator_thread and self.indicator_thread.isRunning():
            self.indicator_thread.stop()
            self.indicator_status_label.setText("正在安全停止并收尾数据库...")
            self.indicator_log("正在安全停止：等待当前写入和数据库连接收尾后关闭窗口...")
        self._schedule_close_when_idle()

    def _schedule_close_when_idle(self):
        if self._close_poll_scheduled:
            return
        self._close_poll_scheduled = True
        QTimer.singleShot(100, self._close_when_idle)

    def _close_when_idle(self):
        self._close_poll_scheduled = False
        if self.is_import_running():
            self._schedule_close_when_idle()
            return
        self.close()

    def _estimate_eta_text(self, start_time: Optional[datetime], completed: int, total: int) -> str:
        if not start_time or completed <= 0 or total <= 0:
            return "预计剩余：--"
        elapsed = (datetime.now() - start_time).total_seconds()
        if elapsed <= 0:
            return "预计剩余：--"
        remaining_tasks = max(0, total - completed)
        if remaining_tasks == 0:
            return "预计剩余：00:00:00"
        seconds = int(round(elapsed * remaining_tasks / max(1, completed)))
        seconds = max(0, seconds)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"预计剩余：{hours:02d}:{minutes:02d}:{secs:02d}"

    def closeEvent(self, event):
        if self.is_import_running():
            if not self._close_after_stop:
                reply = QMessageBox.question(
                    self, "确认关闭",
                    "任务正在进行中，确定要关闭吗？\n"
                    "系统会先停止领取新任务，等待当前写入和数据库连接收尾后再关闭。",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    event.ignore()
                    return
                self.request_close_after_stop()
            event.ignore()
            self._schedule_close_when_idle()
            return
        event.accept()

    def indicator_log(self, message: str):
        _append_download_log(self.indicator_log_text, f"[{datetime.now().strftime('%H:%M:%S')}] {message}")


# ============================================================
# 扫描报告对话框
# ============================================================

class ScanReportDialog(QDialog):
    """扫描报告可视化对话框"""

    def __init__(self, report: dict, parent=None):
        super().__init__(parent)
        self.report = report
        self.setWindowTitle("扫描报告")
        self.setMinimumSize(900, 700)

        # 设置Windows暗色标题栏
        self._set_dark_titlebar()

        # 设置暗色主题样式（与主界面一致）
        self.setStyleSheet("""
            QDialog, QWidget {
                background-color: #333333;
                color: #e8e8e8;
                font-family: "Microsoft YaHei UI";
            }
            QLabel {
                color: #e8e8e8;
            }
            QPushButton {
                background-color: #0078d4;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-family: "Microsoft YaHei UI";
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QTableView {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                gridline-color: #555555;
                alternate-background-color: #404040;
            }
            QTableView::item:selected {
                background-color: #0078d4;
            }
            QHeaderView::section {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #555555;
                padding: 5px;
            }
            QTabWidget::pane {
                border: 1px solid #555555;
                background-color: #333333;
            }
            QTabBar::tab {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                padding: 8px 16px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background-color: #0078d4;
            }
            QTabBar::tab:hover {
                background-color: #404040;
            }
        """)

        self.init_ui()

    def _set_dark_titlebar(self):
        """设置Windows暗色标题栏"""
        try:
            import platform
            if platform.system() == "Windows":
                from ctypes import windll, c_int, byref, sizeof
                from ctypes.wintypes import DWORD
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                DWMWA_CAPTION_COLOR = 35
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_USE_IMMERSIVE_DARK_MODE,
                    byref(c_int(2)),
                    sizeof(c_int)
                )
                caption_color = DWORD(0x333333)
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_CAPTION_COLOR,
                    byref(caption_color),
                    sizeof(caption_color)
                )
        except Exception as e:
            import logging
            logging.debug(f"设置暗色标题栏失败: {str(e)}")

    def init_ui(self):
        """初始化UI"""
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)

        # 创建标签页
        tab_widget = QTabWidget()

        # 标签1: 总体概览
        tab_widget.addTab(self._create_summary_tab(), "总体概览")

        # 标签2: 按周期统计
        tab_widget.addTab(self._create_period_tab(), "按周期统计")

        # 标签3: 按市场统计
        tab_widget.addTab(self._create_market_tab(), "按市场统计")

        # 标签4: 缺失最多的股票
        tab_widget.addTab(self._create_top_stocks_tab(), "缺失最多股票")

        layout.addWidget(tab_widget)

        # 底部按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        export_btn = QPushButton("导出报告")
        export_btn.clicked.connect(self.export_report)
        btn_layout.addWidget(export_btn)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

    def _create_summary_tab(self) -> QWidget:
        """创建总体概览标签页"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(15)

        summary = self.report.get('summary', {})

        # 统计卡片区域
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(15)

        # 股票统计卡片
        stock_card = self._create_stat_card(
            "股票统计",
            [
                ("扫描总数", f"{summary.get('total_stocks', 0):,}"),
                ("有本地数据", f"{summary.get('stocks_with_local_data', 0):,}"),
                ("无本地数据", f"{summary.get('stocks_without_local_data', 0):,}"),
            ],
            "#3498db"
        )
        cards_layout.addWidget(stock_card)

        # 任务统计卡片
        task_card = self._create_stat_card(
            "任务统计",
            [
                ("下载任务数", f"{summary.get('total_tasks', 0):,}"),
                ("缺失总天数", f"{summary.get('total_missing_days', 0):,}"),
            ],
            "#e74c3c"
        )
        cards_layout.addWidget(task_card)

        # 数据完整度卡片
        total_stocks = summary.get('total_stocks', 0)
        with_data = summary.get('stocks_with_local_data', 0)
        completeness = (with_data / total_stocks * 100) if total_stocks > 0 else 0

        complete_card = self._create_stat_card(
            "数据完整度",
            [
                ("本地覆盖率", f"{completeness:.1f}%"),
                ("需补充股票", f"{total_stocks - with_data:,}"),
            ],
            "#27ae60"
        )
        cards_layout.addWidget(complete_card)

        layout.addLayout(cards_layout)

        # 饼图: 有数据 vs 无数据
        chart_layout = QHBoxLayout()

        # 左侧饼图
        fig1 = Figure(figsize=(4, 3), dpi=100)
        canvas1 = FigureCanvas(fig1)
        ax1 = fig1.add_subplot(111)

        with_data = summary.get('stocks_with_local_data', 0)
        without_data = summary.get('stocks_without_local_data', 0)

        if with_data > 0 or without_data > 0:
            sizes = [with_data, without_data]
            labels = [f'有本地数据\n({with_data})', f'无本地数据\n({without_data})']
            colors = ['#27ae60', '#e74c3c']
            ax1.pie(sizes, labels=labels, colors=colors, autopct='%1.1f%%', startangle=90)
            ax1.set_title('股票本地数据分布')
        else:
            ax1.text(0.5, 0.5, '无数据', ha='center', va='center')

        fig1.tight_layout()
        chart_layout.addWidget(canvas1)

        # 右侧饼图: 按市场分布
        fig2 = Figure(figsize=(4, 3), dpi=100)
        canvas2 = FigureCanvas(fig2)
        ax2 = fig2.add_subplot(111)

        by_market = self.report.get('by_market', {})
        if by_market:
            markets = list(by_market.keys())
            counts = [by_market[m]['stock_count'] for m in markets]
            colors = ['#3498db', '#e74c3c', '#f39c12', '#9b59b6', '#1abc9c'][:len(markets)]
            labels = [f'{m}\n({c})' for m, c in zip(markets, counts)]
            ax2.pie(counts, labels=labels, colors=colors, autopct='%1.1f%%', startangle=90)
            ax2.set_title('按市场分布（涉及缺失的股票）')
        else:
            ax2.text(0.5, 0.5, '无数据', ha='center', va='center')

        fig2.tight_layout()
        chart_layout.addWidget(canvas2)

        layout.addLayout(chart_layout)

        layout.addStretch()
        return widget

    def _create_stat_card(self, title: str, items: list, color: str) -> QFrame:
        """创建统计卡片"""
        card = QFrame()
        card.setFrameStyle(QFrame.StyledPanel | QFrame.Raised)
        card.setStyleSheet(f"""
            QFrame {{
                background-color: white;
                border: 2px solid {color};
                border-radius: 10px;
                padding: 10px;
            }}
        """)

        layout = QVBoxLayout(card)

        # 标题
        title_label = QLabel(title)
        title_label.setStyleSheet(f"""
            font-size: 14px;
            font-weight: bold;
            color: {color};
            padding-bottom: 5px;
            border-bottom: 1px solid {color};
        """)
        layout.addWidget(title_label)

        # 数据项
        for label, value in items:
            item_layout = QHBoxLayout()
            label_widget = QLabel(label)
            label_widget.setStyleSheet("color: #666; font-size: 12px;")
            value_widget = QLabel(str(value))
            value_widget.setStyleSheet("font-size: 16px; font-weight: bold; color: #333;")
            value_widget.setAlignment(Qt.AlignRight)
            item_layout.addWidget(label_widget)
            item_layout.addWidget(value_widget)
            layout.addLayout(item_layout)

        return card

    def _create_period_tab(self) -> QWidget:
        """创建按周期统计标签页"""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        by_period = self.report.get('by_period', {})

        if not by_period:
            layout.addWidget(QLabel("暂无数据"))
            return widget

        # 表格
        table = QTableView()
        model = PeriodStatsModel(by_period)
        table.setModel(model)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setAlternatingRowColors(True)
        table.setStyleSheet("""
            QTableView {
                gridline-color: #ddd;
                font-size: 12px;
            }
            QTableView::item {
                padding: 8px;
            }
            QHeaderView::section {
                background-color: #3498db;
                color: white;
                padding: 8px;
                font-weight: bold;
            }
        """)
        layout.addWidget(table)

        # 柱状图
        fig = Figure(figsize=(8, 3), dpi=100)
        canvas = FigureCanvas(fig)
        ax = fig.add_subplot(111)

        periods = list(by_period.keys())
        missing_days = [by_period[p]['missing_days'] for p in periods]
        task_counts = [by_period[p]['task_count'] for p in periods]

        x = range(len(periods))
        width = 0.35

        bars1 = ax.bar([i - width/2 for i in x], missing_days, width, label='缺失天数', color='#e74c3c')
        bars2 = ax.bar([i + width/2 for i in x], task_counts, width, label='任务数', color='#3498db')

        ax.set_ylabel('数量')
        ax.set_title('各周期缺失情况对比')
        ax.set_xticks(x)
        ax.set_xticklabels(periods)
        ax.legend()

        # 添加数值标签
        for bar in bars1:
            height = bar.get_height()
            ax.annotate(f'{int(height):,}',
                        xy=(bar.get_x() + bar.get_width() / 2, height),
                        xytext=(0, 3), textcoords="offset points",
                        ha='center', va='bottom', fontsize=8)

        fig.tight_layout()
        layout.addWidget(canvas)

        return widget

    def _create_market_tab(self) -> QWidget:
        """创建按市场统计标签页"""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        by_market = self.report.get('by_market', {})

        if not by_market:
            layout.addWidget(QLabel("暂无数据"))
            return widget

        # 表格
        table = QTableView()
        model = MarketStatsModel(by_market)
        table.setModel(model)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setAlternatingRowColors(True)
        table.setStyleSheet("""
            QTableView {
                gridline-color: #ddd;
                font-size: 12px;
            }
            QTableView::item {
                padding: 8px;
            }
            QHeaderView::section {
                background-color: #27ae60;
                color: white;
                padding: 8px;
                font-weight: bold;
            }
        """)
        layout.addWidget(table)

        # 柱状图
        fig = Figure(figsize=(8, 3), dpi=100)
        canvas = FigureCanvas(fig)
        ax = fig.add_subplot(111)

        markets = list(by_market.keys())
        stock_counts = [by_market[m]['stock_count'] for m in markets]
        missing_days = [by_market[m]['missing_days'] for m in markets]

        x = range(len(markets))
        width = 0.35

        bars1 = ax.bar([i - width/2 for i in x], stock_counts, width, label='涉及股票数', color='#27ae60')
        bars2 = ax.bar([i + width/2 for i in x], missing_days, width, label='缺失天数', color='#e74c3c')

        ax.set_ylabel('数量')
        ax.set_title('各市场缺失情况对比')
        ax.set_xticks(x)
        ax.set_xticklabels(markets)
        ax.legend()

        for bar in bars1:
            height = bar.get_height()
            ax.annotate(f'{int(height):,}',
                        xy=(bar.get_x() + bar.get_width() / 2, height),
                        xytext=(0, 3), textcoords="offset points",
                        ha='center', va='bottom', fontsize=8)

        fig.tight_layout()
        layout.addWidget(canvas)

        return widget

    def _create_top_stocks_tab(self) -> QWidget:
        """创建缺失最多股票标签页"""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        by_stock = self.report.get('by_stock', {})

        if not by_stock:
            layout.addWidget(QLabel("所有股票数据完整，无缺失"))
            return widget

        # 提示
        hint_label = QLabel(f"共 {len(self.report.get('incomplete_stocks', []))} 只股票存在缺失，以下显示缺失最多的 {len(by_stock)} 只:")
        hint_label.setStyleSheet("color: #666; margin-bottom: 10px;")
        layout.addWidget(hint_label)

        # 表格
        table = QTableView()
        model = TopStocksModel(by_stock)
        table.setModel(model)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setAlternatingRowColors(True)
        table.setStyleSheet("""
            QTableView {
                gridline-color: #ddd;
                font-size: 12px;
            }
            QTableView::item {
                padding: 8px;
            }
            QHeaderView::section {
                background-color: #e74c3c;
                color: white;
                padding: 8px;
                font-weight: bold;
            }
        """)
        layout.addWidget(table)

        # 水平柱状图
        fig = Figure(figsize=(8, 4), dpi=100)
        canvas = FigureCanvas(fig)
        ax = fig.add_subplot(111)

        stocks = list(by_stock.keys())[:10]  # 只显示前10
        missing = [by_stock[s]['total_missing'] for s in stocks]

        y_pos = range(len(stocks))
        bars = ax.barh(y_pos, missing, color='#e74c3c')
        ax.set_yticks(y_pos)
        ax.set_yticklabels(stocks)
        ax.invert_yaxis()  # 最大的在上面
        ax.set_xlabel('缺失天数')
        ax.set_title('缺失最多的股票 (Top 10)')

        for i, bar in enumerate(bars):
            width = bar.get_width()
            ax.annotate(f'{int(width):,}',
                        xy=(width, bar.get_y() + bar.get_height()/2),
                        xytext=(3, 0), textcoords="offset points",
                        ha='left', va='center', fontsize=9)

        fig.tight_layout()
        layout.addWidget(canvas)

        return widget

    def export_report(self):
        """导出报告到文件"""
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出报告", f"scan_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "文本文件 (*.txt);;所有文件 (*)"
        )

        if not file_path:
            return

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write("=" * 60 + "\n")
                f.write("扫描报告\n")
                f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 60 + "\n\n")

                # 总体统计
                summary = self.report.get('summary', {})
                f.write("【总体统计】\n")
                f.write(f"  扫描股票总数: {summary.get('total_stocks', 0):,}\n")
                f.write(f"  有本地数据: {summary.get('stocks_with_local_data', 0):,}\n")
                f.write(f"  无本地数据: {summary.get('stocks_without_local_data', 0):,}\n")
                f.write(f"  下载任务数: {summary.get('total_tasks', 0):,}\n")
                f.write(f"  缺失总天数: {summary.get('total_missing_days', 0):,}\n\n")

                # 按周期统计
                by_period = self.report.get('by_period', {})
                if by_period:
                    f.write("【按周期统计】\n")
                    for period, stats in by_period.items():
                        f.write(f"  [{period}]\n")
                        f.write(f"    涉及股票: {stats['stock_count']:,}\n")
                        f.write(f"    任务数: {stats['task_count']:,}\n")
                        f.write(f"    缺失天数: {stats['missing_days']:,}\n")
                        f.write(f"    日期范围: {stats['date_range']}\n")
                    f.write("\n")

                # 按市场统计
                by_market = self.report.get('by_market', {})
                if by_market:
                    f.write("【按市场统计】\n")
                    for market, stats in by_market.items():
                        f.write(f"  [{market}] 股票: {stats['stock_count']:,}, ")
                        f.write(f"任务: {stats['task_count']:,}, ")
                        f.write(f"缺失: {stats['missing_days']:,} 天\n")
                    f.write("\n")

                # 缺失最多的股票
                by_stock = self.report.get('by_stock', {})
                if by_stock:
                    f.write("【缺失最多的股票】\n")
                    for i, (stock, info) in enumerate(by_stock.items(), 1):
                        periods_str = ", ".join([f"{p}:{d['missing_days']}天" for p, d in info['periods'].items()])
                        f.write(f"  {i:2d}. {stock}: 共缺失 {info['total_missing']:,} 天 ({periods_str})\n")

            QMessageBox.information(self, "导出成功", f"报告已导出到:\n{file_path}")

        except Exception as e:
            QMessageBox.warning(self, "导出失败", f"导出报告失败: {e}")


class PeriodStatsModel(QAbstractTableModel):
    """按周期统计的表格模型"""

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = data
        self._periods = list(data.keys())
        self._headers = ['周期', '涉及股票数', '任务数', '缺失天数', '日期范围']

    def rowCount(self, parent=QModelIndex()):
        return len(self._periods)

    def columnCount(self, parent=QModelIndex()):
        return len(self._headers)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        if role == Qt.DisplayRole:
            period = self._periods[index.row()]
            stats = self._data[period]
            col = index.column()

            if col == 0:
                return period
            elif col == 1:
                return f"{stats['stock_count']:,}"
            elif col == 2:
                return f"{stats['task_count']:,}"
            elif col == 3:
                return f"{stats['missing_days']:,}"
            elif col == 4:
                return stats['date_range']

        elif role == Qt.TextAlignmentRole:
            if index.column() in [1, 2, 3]:
                return Qt.AlignRight | Qt.AlignVCenter
            return Qt.AlignLeft | Qt.AlignVCenter

        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self._headers[section]
        return None


class MarketStatsModel(QAbstractTableModel):
    """按市场统计的表格模型"""

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = data
        self._markets = list(data.keys())
        self._headers = ['市场', '涉及股票数', '任务数', '缺失天数']

    def rowCount(self, parent=QModelIndex()):
        return len(self._markets)

    def columnCount(self, parent=QModelIndex()):
        return len(self._headers)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        if role == Qt.DisplayRole:
            market = self._markets[index.row()]
            stats = self._data[market]
            col = index.column()

            if col == 0:
                return market
            elif col == 1:
                return f"{stats['stock_count']:,}"
            elif col == 2:
                return f"{stats['task_count']:,}"
            elif col == 3:
                return f"{stats['missing_days']:,}"

        elif role == Qt.TextAlignmentRole:
            if index.column() in [1, 2, 3]:
                return Qt.AlignRight | Qt.AlignVCenter
            return Qt.AlignLeft | Qt.AlignVCenter

        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self._headers[section]
        return None


class TopStocksModel(QAbstractTableModel):
    """缺失最多股票的表格模型"""

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self._data = data
        self._stocks = list(data.keys())
        self._headers = ['排名', '股票代码', '总缺失天数', '各周期缺失详情']

    def rowCount(self, parent=QModelIndex()):
        return len(self._stocks)

    def columnCount(self, parent=QModelIndex()):
        return len(self._headers)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        if role == Qt.DisplayRole:
            stock = self._stocks[index.row()]
            info = self._data[stock]
            col = index.column()

            if col == 0:
                return str(index.row() + 1)
            elif col == 1:
                return stock
            elif col == 2:
                return f"{info['total_missing']:,}"
            elif col == 3:
                return ", ".join([f"{p}:{d['missing_days']}天" for p, d in info['periods'].items()])

        elif role == Qt.TextAlignmentRole:
            if index.column() in [0, 2]:
                return Qt.AlignRight | Qt.AlignVCenter
            return Qt.AlignLeft | Qt.AlignVCenter

        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self._headers[section]
        return None


# ============================================================
# 数据完整性检查对话框
# ============================================================

class DataIntegrityDialog(QDialog):
    """数据完整性检查对话框"""
    
    def __init__(self, manager: DuckDBManager, stock_code: str, period: str,
                 start_date: str, end_date: str, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.stock_code = stock_code
        self.period = period
        self.start_date = start_date
        self.end_date = end_date
        self.rect_info = {}  # 存储方块信息，用于鼠标悬停

        self.setWindowTitle(f"数据完整性检查 - {stock_code} ({period})")
        self.setMinimumSize(800, 600)

        # 设置Windows暗色标题栏
        self._set_dark_titlebar()

        # 设置暗色主题样式（与主界面一致）
        self.setStyleSheet("""
            QDialog, QWidget {
                background-color: #333333;
                color: #e8e8e8;
                font-family: "Microsoft YaHei UI";
            }
            QLabel {
                color: #e8e8e8;
            }
            QPushButton {
                background-color: #0078d4;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-family: "Microsoft YaHei UI";
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QFrame {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 5px;
            }
        """)

        self.init_ui()
        self.check_integrity()

    def _set_dark_titlebar(self):
        """设置Windows暗色标题栏"""
        try:
            import platform
            if platform.system() == "Windows":
                from ctypes import windll, c_int, byref, sizeof
                from ctypes.wintypes import DWORD
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                DWMWA_CAPTION_COLOR = 35
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_USE_IMMERSIVE_DARK_MODE,
                    byref(c_int(2)),
                    sizeof(c_int)
                )
                caption_color = DWORD(0x333333)
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_CAPTION_COLOR,
                    byref(caption_color),
                    sizeof(caption_color)
                )
        except Exception as e:
            import logging
            logging.debug(f"设置暗色标题栏失败: {str(e)}")
    
    def init_ui(self):
        """初始化UI"""
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)

        # 标题区域 - 暗色主题
        title_frame = QFrame()
        title_frame.setStyleSheet("""
            QFrame {
                background-color: #3c3c3c;
                border-radius: 5px;
                padding: 10px;
            }
        """)
        title_layout = QVBoxLayout(title_frame)
        title_layout.setContentsMargins(10, 10, 10, 10)

        # 信息标签 - 使用更大的字体和更好的样式
        info_label = QLabel(f"📊 股票: <b>{self.stock_code}</b> | 周期: <b>{self.period}</b> | "
                          f"时间范围: <b>{self.start_date}</b> ~ <b>{self.end_date}</b>")
        info_label.setFont(QFont("Microsoft YaHei", 10))
        info_label.setStyleSheet("color: #e8e8e8;")
        title_layout.addWidget(info_label)
        layout.addWidget(title_frame)

        # 图例 - 使用暗色主题样式
        legend_frame = QFrame()
        legend_frame.setStyleSheet("""
            QFrame {
                background-color: #3c3c3c;
                border: 1px solid #555555;
                border-radius: 5px;
                padding: 8px;
            }
        """)
        legend_layout = QHBoxLayout(legend_frame)
        legend_layout.setContentsMargins(10, 5, 10, 5)
        legend_layout.addWidget(QLabel("<b>图例:</b>"))
        legend_layout.addSpacing(10)

        legend_items = [
            ("完整数据", "#4CAF50", "#2E7D32"),  # 绿色系
            ("部分数据", "#FF9800", "#F57C00"),  # 橙色系
            ("无数据", "#5a5a5a", "#404040")      # 暗灰色系
        ]

        for text, color, border_color in legend_items:
            # 颜色方块
            color_label = QLabel()
            color_label.setStyleSheet(f"""
                QLabel {{
                    background-color: {color};
                    border: 2px solid {border_color};
                    border-radius: 3px;
                    min-width: 24px;
                    max-width: 24px;
                    min-height: 24px;
                    max-height: 24px;
                }}
            """)
            legend_layout.addWidget(color_label)

            # 文字标签
            text_label = QLabel(text)
            text_label.setFont(QFont("Microsoft YaHei", 9))
            text_label.setStyleSheet("color: #e8e8e8;")
            legend_layout.addWidget(text_label)
            legend_layout.addSpacing(15)

        legend_layout.addStretch()
        layout.addWidget(legend_frame)

        # Matplotlib图表 - 暗色背景
        self.figure = Figure(figsize=(11, 7))
        self.figure.patch.set_facecolor('#333333')
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setStyleSheet("background-color: #333333; border: 1px solid #404040; border-radius: 5px;")
        layout.addWidget(self.canvas)
        
        # 统计信息 - 使用暗色主题样式
        stats_frame = QFrame()
        stats_frame.setStyleSheet("""
            QFrame {
                background-color: #3c3c3c;
                border: 1px solid #555555;
                border-radius: 5px;
                padding: 10px;
            }
        """)
        stats_layout = QHBoxLayout(stats_frame)
        stats_layout.setContentsMargins(15, 8, 15, 8)
        self.stats_label = QLabel("正在检查...")
        self.stats_label.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
        self.stats_label.setStyleSheet("color: #e8e8e8;")
        stats_layout.addWidget(self.stats_label)
        stats_layout.addStretch()
        layout.addWidget(stats_frame)

        # 关闭按钮 - 使用暗色主题样式
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setFont(QFont("Microsoft YaHei", 9))
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 25px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:pressed {
                background-color: #005a9e;
            }
        """)
        close_btn.clicked.connect(self.close)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)
    
    def check_integrity(self):
        """检查数据完整性"""
        try:
            # 获取交易日列表
            if khQTTools is None:
                QMessageBox.warning(self, "错误", "无法导入khQTTools模块")
                return

            # 使用 _get_trade_days_list 方法（接收datetime对象）
            # 支持两种日期格式: YYYYMMDD 或 YYYY-MM-DD
            try:
                start_dt = datetime.strptime(self.start_date, '%Y%m%d')
            except ValueError:
                start_dt = datetime.strptime(self.start_date, '%Y-%m-%d')

            try:
                end_dt = datetime.strptime(self.end_date, '%Y%m%d')
            except ValueError:
                end_dt = datetime.strptime(self.end_date, '%Y-%m-%d')

            trade_days_dt = khQTTools._get_trade_days_list(start_dt, end_dt)

            # 转换为字符串列表
            trade_days = [dt.strftime('%Y-%m-%d') for dt in trade_days_dt]
            
            if not trade_days:
                QMessageBox.warning(self, "错误", "无法获取交易日列表")
                return
            
            # 获取实际数据
            df = self.manager.get_kline_data(
                self.stock_code, self.period,
                self.start_date, self.end_date
            )
            
            if df.empty:
                QMessageBox.warning(self, "提示", "该时间段内无数据")
                return
            
            # 转换时间列为日期
            if 'time' in df.columns:
                df['date'] = pd.to_datetime(df['time']).dt.date
            else:
                QMessageBox.warning(self, "错误", "数据中缺少time列")
                return
            
            # 根据周期判断每个交易日的数据完整性
            integrity_status = {}
            
            if self.period == '1d':
                # 日线数据：每个交易日应该有1条数据
                for trade_day_str in trade_days:
                    trade_day = datetime.strptime(trade_day_str, '%Y-%m-%d').date()
                    day_data = df[df['date'] == trade_day]
                    
                    if len(day_data) == 0:
                        integrity_status[trade_day_str] = 'none'  # 无数据
                    elif len(day_data) >= 1:
                        integrity_status[trade_day_str] = 'complete'  # 完整数据
                    else:
                        integrity_status[trade_day_str] = 'partial'  # 部分数据
            
            elif self.period in ['1m', '5m']:
                # 分钟数据：需要判断是否有足够的K线
                # 正常交易日应该有240条1分钟数据（4小时 * 60分钟）
                # 或者48条5分钟数据（4小时 * 12个5分钟）
                expected_count = 240 if self.period == '1m' else 48
                
                for trade_day_str in trade_days:
                    trade_day = datetime.strptime(trade_day_str, '%Y-%m-%d').date()
                    day_data = df[df['date'] == trade_day]
                    
                    if len(day_data) == 0:
                        integrity_status[trade_day_str] = 'none'
                    elif len(day_data) >= expected_count * 0.95:  # 95%以上认为完整
                        integrity_status[trade_day_str] = 'complete'
                    else:
                        integrity_status[trade_day_str] = 'partial'
            
            elif self.period == 'tick':
                # Tick数据：需要判断是否有足够的数据
                # 正常交易日应该有4700条以上的tick数据
                expected_count = 4700
                # 以95%作为完整标准
                complete_threshold = int(expected_count * 0.95)  # 4465条
                
                for trade_day_str in trade_days:
                    trade_day = datetime.strptime(trade_day_str, '%Y-%m-%d').date()
                    day_data = df[df['date'] == trade_day]
                    
                    if len(day_data) == 0:
                        integrity_status[trade_day_str] = 'none'
                    elif len(day_data) >= complete_threshold:  # 达到4700的95%（4465条）认为完整
                        integrity_status[trade_day_str] = 'complete'
                    else:
                        integrity_status[trade_day_str] = 'partial'
            
            else:
                # 其他周期：简单判断有数据即可
                for trade_day_str in trade_days:
                    trade_day = datetime.strptime(trade_day_str, '%Y-%m-%d').date()
                    day_data = df[df['date'] == trade_day]
                    
                    if len(day_data) == 0:
                        integrity_status[trade_day_str] = 'none'
                    elif len(day_data) > 0:
                        integrity_status[trade_day_str] = 'complete'
                    else:
                        integrity_status[trade_day_str] = 'partial'
            
            # 绘制方块矩阵图
            self.plot_integrity_matrix(trade_days, integrity_status)
            
            # 更新统计信息 - 使用更美观的格式
            complete_count = sum(1 for v in integrity_status.values() if v == 'complete')
            partial_count = sum(1 for v in integrity_status.values() if v == 'partial')
            none_count = sum(1 for v in integrity_status.values() if v == 'none')
            total_count = len(trade_days)
            
            # 计算百分比
            complete_pct = (complete_count / total_count * 100) if total_count > 0 else 0
            partial_pct = (partial_count / total_count * 100) if total_count > 0 else 0
            none_pct = (none_count / total_count * 100) if total_count > 0 else 0
            
            stats_text = (f"📈 统计: "
                         f"<span style='color: #4CAF50; font-weight: bold;'>完整 {complete_count} 天 ({complete_pct:.1f}%)</span> | "
                         f"<span style='color: #FF9800; font-weight: bold;'>部分 {partial_count} 天 ({partial_pct:.1f}%)</span> | "
                         f"<span style='color: #757575; font-weight: bold;'>缺失 {none_count} 天 ({none_pct:.1f}%)</span> | "
                         f"<span style='color: #2196F3; font-weight: bold;'>总计 {total_count} 个交易日</span>")
            self.stats_label.setText(stats_text)
            
        except Exception as e:
            QMessageBox.warning(self, "错误", f"检查数据完整性时出错: {str(e)}")
            import traceback
            traceback.print_exc()
    
    def plot_integrity_matrix(self, trade_days: List[str], integrity_status: Dict[str, str]):
        """绘制数据完整性方块矩阵图"""
        self.figure.clear()
        ax = self.figure.add_subplot(111)

        # 颜色映射 - 暗色主题
        color_map = {
            'complete': '#4CAF50',  # 柔和的绿色
            'partial': '#FF9800',   # 柔和的橙色
            'none': '#5a5a5a'       # 暗灰色（适合暗色主题）
        }
        
        # 计算矩阵大小（尽量接近正方形）
        total_days = len(trade_days)
        cols = int(np.ceil(np.sqrt(total_days)))
        rows = int(np.ceil(total_days / cols))
        
        # 存储每个方块的位置和对应的日期信息
        self.rect_info = {}  # {(row, col): {'date': date_str, 'status': status}}
        
        # 绘制彩色方块
        for row in range(rows):
            for col in range(cols):
                idx = row * cols + col
                if idx < len(trade_days):
                    trade_day = trade_days[idx]
                    status = integrity_status.get(trade_day, 'none')
                    color = color_map.get(status, '#FFFFFF')
                    
                    # 根据状态设置边框颜色
                    edge_color = {
                        'complete': '#2E7D32',  # 深绿色边框
                        'partial': '#F57C00',   # 深橙色边框
                        'none': '#BDBDBD'       # 灰色边框
                    }.get(status, '#CCCCCC')
                    
                    rect = Rectangle((col - 0.5, row - 0.5), 1, 1,
                                    facecolor=color,
                                    edgecolor=edge_color, 
                                    linewidth=1.2,
                                    alpha=0.9)
                    ax.add_patch(rect)
                    
                    # 存储方块信息
                    self.rect_info[(row, col)] = {
                        'date': trade_day,
                        'status': status
                    }
        
        ax.set_xlim(-0.5, cols - 0.5)
        ax.set_ylim(-0.5, rows - 0.5)
        ax.set_xticks([])
        ax.set_yticks([])

        # 设置标题样式 - 暗色主题
        ax.set_title(f"股票数据完整性 ({self.stock_code} - {self.period})",
                    fontsize=14, fontweight='bold', pad=15, color='#e8e8e8')

        # 设置背景色 - 暗色主题
        ax.set_facecolor('#333333')

        # 反转Y轴，使第一个交易日显示在左上角（从下往上排列）
        ax.invert_yaxis()

        # 添加鼠标悬停提示 - 暗色主题样式
        self.annot = ax.annotate("", xy=(0,0), xytext=(20,20), textcoords="offset points",
                                bbox=dict(boxstyle="round,pad=0.8",
                                         facecolor='#3c3c3c',
                                         edgecolor='#0078d4',
                                         linewidth=2,
                                         alpha=0.95),
                                arrowprops=dict(arrowstyle="->",
                                               connectionstyle="arc3,rad=0.3",
                                               color='#0078d4',
                                               lw=2))
        self.annot.set_visible(False)
        
        # 连接鼠标移动事件
        self.canvas.mpl_connect("motion_notify_event", self.on_hover)
        
        self.canvas.draw()
    
    def on_hover(self, event):
        """鼠标悬停事件处理"""
        if event.inaxes is None or event.inaxes != self.figure.axes[0]:
            self.annot.set_visible(False)
            self.canvas.draw_idle()
            return
        
        if event.xdata is None or event.ydata is None:
            return
        
        # 获取鼠标位置对应的行列
        # 由于使用了invert_yaxis()，Y轴已反转
        # 绘制时row从0到rows-1，Y坐标从rows-0.5到-0.5（反转后）
        # 所以需要从反转后的坐标计算row
        col = int(round(event.xdata + 0.5))
        # 计算row：由于Y轴反转，需要从顶部计算
        # 反转后Y坐标范围：顶部是rows-0.5，底部是-0.5
        # row = rows - 1 - int(round(event.ydata + 0.5))
        # 更简单的方法：直接使用反转后的坐标
        row = int(round(event.ydata + 0.5))
        
        # 检查是否有对应的方块
        if (row, col) in self.rect_info:
            info = self.rect_info[(row, col)]
            date_str = info['date']
            status = info['status']
            
            # 状态中文描述和颜色
            status_info = {
                'complete': ('完整数据', '#4CAF50'),
                'partial': ('部分数据', '#FF9800'),
                'none': ('无数据', '#757575')
            }.get(status, ('未知', '#757575'))
            
            status_text, status_color = status_info
            
            # 格式化日期显示（添加星期）
            try:
                date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                weekday = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][date_obj.weekday()]
                date_display = f"{date_str} ({weekday})"
            except:
                date_display = date_str
            
            # 显示提示信息 - 使用更美观的格式
            self.annot.xy = (event.xdata, event.ydata)
            
            # 使用纯文本格式（matplotlib不支持HTML）
            text = f"📅 日期: {date_display}\n📊 状态: {status_text}"
            self.annot.set_text(text)
            self.annot.set_fontsize(10)
            self.annot.set_fontfamily('Microsoft YaHei')
            self.annot.set_visible(True)
        else:
            self.annot.set_visible(False)

        self.canvas.draw_idle()


# ============================================================
# 全量增量数据补充线程（扫描+下载一体化）
# ============================================================

class _DuckDBLockDecisionMixin:
    """让下载线程在自动重试耗尽后等待界面给出重试/跳过/停止决定。"""

    def _init_lock_decision(self):
        self._lock_decision_event = Event()
        self._lock_decision_guard = Lock()
        self._lock_decision = None
        self._lock_prompt_enabled = False

    def enable_lock_prompt(self):
        self._lock_prompt_enabled = True

    def set_lock_resolution(self, resolution: str):
        value = str(resolution or "skip").strip().lower()
        if value not in ("retry", "skip", "abort"):
            value = "skip"
        with self._lock_decision_guard:
            self._lock_decision = value
            self._lock_decision_event.set()

    def _wake_lock_decision_for_stop(self):
        self.set_lock_resolution("abort")

    def _request_lock_resolution(self, info: dict) -> str:
        # 非GUI调用不能永久等待；自动跳过并在最终汇总中明确列出。
        if not self._lock_prompt_enabled:
            return "skip"
        with self._lock_decision_guard:
            self._lock_decision = None
            self._lock_decision_event.clear()
        self.lock_conflict.emit(info)
        while not self._stop_flag:
            if self._lock_decision_event.wait(0.2):
                break
        with self._lock_decision_guard:
            return self._lock_decision or ("abort" if self._stop_flag else "skip")

    def _save_download_frame(self, df, stock: str, period: str, results: dict):
        """返回 (saved|skip|abort, 写入条数)。"""
        while not self._stop_flag:
            try:
                native_source = getattr(self, "source", MINIQMT) == QMT_NATIVE
                force_overwrite = bool(getattr(self, "force_overwrite", False))
                period_value = str(getattr(period, "value", period)).strip().lower()
                if period_value == "tick":
                    # 即使 worker/旧客户端错误返回窗口外数据，写库前仍做
                    # 最后一层门禁；否则下一次扫描会把旧数据永久视为本地
                    # 覆盖，且无法再从当前 QMT 补回。
                    _validate_tick_frame_retention(df)
                    saved = self.manager.save_tick_data(
                        df,
                        stock,
                        skip_metadata=True,
                        append_missing_only=(native_source and not force_overwrite),
                    )
                else:
                    if native_source:
                        saved = self.manager.save_kline_data(
                            df,
                            stock,
                            period,
                            skip_metadata=True,
                            overwrite=force_overwrite,
                            merge_missing=not force_overwrite,
                            overwrite_trade_dates=force_overwrite,
                        )
                    else:
                        # Preserve the established MiniQMT incremental write
                        # semantics for existing callers, but pass overwrite_trade_dates
                        # when force_overwrite is explicitly requested.
                        saved = self.manager.save_kline_data(
                            df,
                            stock,
                            period,
                            skip_metadata=True,
                            overwrite=force_overwrite,
                            overwrite_trade_dates=force_overwrite,
                        )
                return "saved", saved
            except Exception as exc:
                quality_code = str(getattr(exc, "code", "") or "").upper()
                if quality_code in {
                    "DUPLICATE_TIMESTAMP",
                    "DATA_QUALITY",
                    "OUT_OF_RETENTION",
                }:
                    results.setdefault("quality_errors", []).append({
                        "stock": stock, "period": period,
                        "code": quality_code,
                        "error": str(exc),
                    })
                    self.download_log.emit(f"  数据质量拒绝: {stock} {period} - {exc}")
                    results["skipped"] = int(results.get("skipped", 0) or 0) + 1
                    return "skip", 0
                if not is_duckdb_lock_error(exc):
                    raise
                info = parse_duckdb_lock_error(
                    exc,
                    stock_code=stock,
                    period=period,
                    operation="write",
                    attempts=5,
                )
                self.download_log.emit(
                    f"  数据库占用: {stock} {period}，自动重试仍未释放"
                )
                decision = self._request_lock_resolution(info)
                if decision == "retry":
                    self.download_log.emit(f"  用户选择继续重试: {stock} {period}")
                    try:
                        self.manager.close_stock_connection(stock, skip_checkpoint=True)
                    except Exception:
                        pass
                    continue
                if decision == "skip":
                    results.setdefault("lock_skipped", []).append(info)
                    results["skipped"] = int(results.get("skipped", 0)) + 1
                    self.download_log.emit(f"  已跳过占用文件: {stock} {period}")
                    return "skip", 0
                self._stop_flag = True
                self.download_log.emit("  用户选择停止任务")
                return "abort", 0
        return "abort", 0

    def _refresh_download_metadata(self, records) -> int:
        """批量修复/刷新本轮可见性，避免每个缺口任务重复更新元数据库。"""
        compact = _coalesce_metadata_records(records)
        if not compact:
            return 0
        try:
            updated = self.manager.batch_update_metadata(compact)
            self.download_log.emit(f"元数据刷新完成: {updated}/{len(compact)}")
            return updated
        except Exception as meta_err:
            self.download_log.emit(
                f"⚠ 数据已写入，但元数据刷新失败: {meta_err}。可稍后扫描修复元数据。"
            )
            return 0
        finally:
            try:
                self.manager.close_metadata_connection()
            except Exception:
                pass


class FullIncrementThread(_DuckDBLockDecisionMixin, QThread):
    """
    全量增量数据补充线程
    合并了扫描和下载功能，一键完成
    """
    # 信号定义
    scan_progress = pyqtSignal(int, int, str, int)  # (当前, 总数, 消息, 已发现任务数)
    scan_log = pyqtSignal(str)  # 扫描日志（显示详细缺失信息）
    scan_finished = pyqtSignal(int, int, dict)  # (总任务数, 总缺失天数, 详细报告字典)
    download_progress = pyqtSignal(int, int, str, int)  # (当前, 总数, 消息, 预计剩余秒数)
    download_log = pyqtSignal(str)  # 下载日志
    task_completed = pyqtSignal(dict)  # 单个任务完成
    finished = pyqtSignal(dict)  # 最终结果统计
    error = pyqtSignal(str)
    lock_conflict = pyqtSignal(dict)

    # 预设板块文件列表
    PRESET_SECTORS = [
        ('沪深A股', '沪深A股_股票列表.csv'),
        ('上证A股', '上证A股_股票列表.csv'),
        ('深证A股', '深证A股_股票列表.csv'),
        ('沪深300', '沪深300成分股_股票列表.csv'),
        ('上证50', '上证50成分股_股票列表.csv'),
        ('中证500', '中证500成分股_股票列表.csv'),
        ('创业板', '创业板_股票列表.csv'),
        ('科创板', '科创板_股票列表.csv'),
        ('沪深ETF', '沪深ETF_成分股列表.csv'),
        ('沪深场内基金（含ETF/LOF）', '沪深基金_列表.csv'),
        ('沪深转债', '沪深转债_列表.csv'),
        ('T0型ETF', 'T0型ETF.csv'),
        ('指数', '指数_股票列表.csv'),
    ]

    # 进度文件名
    PROGRESS_FILE = 'full_increment_progress.json'

    def __init__(self, manager: DuckDBManager, periods_config: Dict[str, tuple],
                 completed_tasks: set = None, force_overwrite: bool = False,
                 dividend_types: Optional[List[str]] = None, num_dl_workers: int = 2,
                 parent=None, source: str = MINIQMT,
                 bridge_dir: Optional[str] = None,
                 max_task_retries: int = DEFAULT_HISTORY_MAX_TASK_RETRIES,
                 retry_backoff: Optional[List[float]] = None,
                 instance_generation: Optional[str] = None,
                 native_profile: Optional[str] = None,
                 profile: Optional[str] = None,
                 max_inflight: Optional[int] = None,
                 batch_size: Optional[int] = None,
                 span_rows: Optional[int] = None,
                 span_bytes: Optional[int] = None,
                 mode: Optional[str] = None,
                 cache_strategy: Optional[str] = None,
                 cancel_after: Optional[float] = None):
        super().__init__(parent)
        self.manager = manager
        self.periods_config = periods_config  # {'1d': ('20150101', '20251221'), ...}
        self.completed_tasks = completed_tasks if completed_tasks is not None else set()
        self.force_overwrite = force_overwrite
        self.dividend_types = dividend_types
        self.num_dl_workers = num_dl_workers
        self.source = _canonical_history_source(source)
        self.bridge_dir = str(bridge_dir) if bridge_dir else None
        self.max_task_retries, self.retry_backoff = _coerce_retry_settings(
            max_task_retries, retry_backoff
        )
        self.instance_generation = _normalise_generation_aliases(instance_generation)
        self._native_execution = normalize_native_execution_options(
            {
                "native_profile": native_profile,
                "profile": profile,
                "max_inflight": max_inflight,
                "batch_size": batch_size,
                "span_rows": span_rows,
                "span_bytes": span_bytes,
                "mode": mode,
                "cache_strategy": cache_strategy,
                "cancel_after": cancel_after,
            },
            strict=False,
        )
        for _key, _value in self._native_execution.items():
            setattr(self, _key, _value)
        self._stop_flag = False
        self._cancel_timer = None
        self.workers_requested, self.workers_effective = _effective_history_workers(
            self.source, self.num_dl_workers, default=2
        )
        self.allow_gaps = _default_allow_gaps_for_source(self.source)
        self._active_importer = None
        self.tasks = []
        self.start_time = None
        # Timings are kept separate so native progress text can distinguish
        # DuckDB/coverage scanning from the actual bridge download phase.
        self._scan_start_time = None
        self._download_start_time = None
        self._scan_lock = None  # 扫描结果锁
        self.resolved_stocks = []
        self._init_lock_decision()

    def stop(self):
        self._stop_flag = True
        self._wake_lock_decision_for_stop()
        importer = getattr(self, "_active_importer", None)
        if importer is not None:
            _cancel_history_runner(importer, timeout=2.0)
        timer = getattr(self, "_cancel_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def collect_all_stocks(self) -> List[str]:
        """收集所有预设板块的股票"""
        all_stocks = set()

        legacy_dirs = [
            os.path.join(os.path.dirname(os.path.dirname(__file__)), 'stock_lists'),
            os.path.join(os.path.dirname(__file__), 'stock_lists'),
        ]

        for sector_name, filename in self.PRESET_SECTORS:
            file_path = _resolve_preset_stock_pool_file(filename, legacy_dirs)

            if file_path:
                # v3.1.6: 内存优化 - 使用 try-finally 确保 DataFrame 释放
                df = None
                try:
                    # 首先尝试不使用header读取（假设文件没有表头）
                    df = pd.read_csv(file_path, dtype=str, header=None, encoding='utf-8-sig')

                    # 检查第一行第一列是否像股票代码（包含.SH或.SZ）
                    if len(df) > 0 and len(df.columns) > 0:
                        first_cell = str(df.iloc[0, 0])
                        if '.SH' in first_cell or '.SZ' in first_cell or '.BJ' in first_cell:
                            # 第一行是数据，使用无表头模式
                            stocks = df.iloc[:, 0].dropna().tolist()
                            all_stocks.update(stocks)
                        else:
                            # v3.1.6: 避免重复读取文件 - 直接使用已读取的数据
                            # 将第一行作为表头处理
                            df.columns = df.iloc[0]
                            df = df.iloc[1:]  # 移除第一行（现在是表头）

                            # 寻找代码列
                            found_col = None
                            for col in df.columns:
                                col_str = str(col) if col is not None else ''
                                if '代码' in col_str or 'code' in col_str.lower():
                                    found_col = col
                                    break

                            if found_col is not None:
                                all_stocks.update(df[found_col].dropna().tolist())
                            elif len(df.columns) > 0:
                                # 没有找到代码列，使用第一列
                                all_stocks.update(df.iloc[:, 0].dropna().tolist())
                except Exception as e:
                    self.scan_log.emit(f"读取 {filename} 失败: {e}")
                finally:
                    # v3.1.6: 显式释放 DataFrame
                    if df is not None:
                        del df
            else:
                fetched_stocks = []
                try:
                    import khQTTools
                    if hasattr(khQTTools, 'get_stock_list_from_qmt_native') and getattr(self, "source", None) == QMT_NATIVE:
                        fetched_stocks = khQTTools.get_stock_list_from_qmt_native(sector_name)
                    if not fetched_stocks and hasattr(khQTTools, 'get_stock_list'):
                        fetched_stocks = khQTTools.get_stock_list(sector_name)
                except Exception:
                    fetched_stocks = []
                if fetched_stocks:
                    all_stocks.update(fetched_stocks)
                    self.scan_log.emit(f"本地未找到 {filename}，已通过接口动态获取板块【{sector_name}】{len(fetched_stocks)}只股票")
                    try:
                        save_dir = legacy_dirs[0]
                        os.makedirs(save_dir, exist_ok=True)
                        save_path = os.path.join(save_dir, filename)
                        pd.DataFrame(list(fetched_stocks)).to_csv(save_path, index=False, header=False, encoding='utf-8-sig')
                    except Exception:
                        pass
                else:
                    self.scan_log.emit(f"未找到文件: {filename}")

        return list(all_stocks)

    def _generate_trade_days_cache(self) -> Dict[str, set]:
        """预先生成所有需要的交易日缓存"""
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from khQTTools import is_trade_day
        from datetime import datetime, timedelta

        cache = {}
        sorted_cache = {}  # 同时缓存排序后的列表
        for period, (target_start, target_end) in self.periods_config.items():
            cache_key = f"{target_start}_{target_end}"
            if cache_key not in cache:
                start_dt = datetime.strptime(target_start, '%Y%m%d')
                end_dt = datetime.strptime(target_end, '%Y%m%d')

                all_trade_days = set()
                current = start_dt
                while current <= end_dt:
                    date_str = current.strftime('%Y%m%d')
                    if is_trade_day(date_str):
                        all_trade_days.add(date_str)
                    current += timedelta(days=1)

                cache[cache_key] = all_trade_days
                sorted_cache[cache_key] = sorted(all_trade_days)  # 预排序

        self._sorted_trade_days_cache = sorted_cache  # 保存排序缓存
        return cache

    @staticmethod
    def _native_metadata_date(value) -> str:
        """Normalize official QMT listing dates to ``YYYYMMDD``."""
        if value is None:
            return ""
        text = str(value).strip().replace("-", "").replace("/", "")
        digits = "".join(ch for ch in text[:32] if ch.isdigit())
        if len(digits) < 8:
            return ""
        candidate = digits[:8]
        try:
            datetime.strptime(candidate, "%Y%m%d")
        except ValueError:
            return ""
        return candidate

    def _native_open_dates(self, stocks: List[str]) -> Dict[str, str]:
        """Read official QMT listing dates once for a full scan, best effort."""
        if self.source != QMT_NATIVE or not stocks:
            return {}
        try:
            from kh_qmt_native_bridge.client import QmtNativeClient

            client = QmtNativeClient(
                bridge_dir=self.bridge_dir,
                expected_generation=self.instance_generation or None,
                request_timeout=5.0,
            )
            # QMT 对单次合约元数据请求有数量上限（通常约 2,000）。
            # 全量股票池不能因一次 oversized 请求失败而退回慢扫描；
            # 分块仍走官方接口，并允许单块失败时保留其原扫描任务。
            details = {}
            for offset in range(0, len(stocks), 1000):
                batch = stocks[offset:offset + 1000]
                try:
                    part = client.instrument_metadata(batch, timeout=5.0)
                except Exception as batch_exc:
                    self.scan_log.emit(
                        f"官方合约元数据分块 {offset + 1}-{offset + len(batch)} 不可用，保留该块扫描任务: {batch_exc}"
                    )
                    continue
                if isinstance(part, Mapping):
                    details.update(part)
        except Exception as exc:
            self.scan_log.emit(f"官方合约元数据不可用，保留原扫描任务: {exc}")
            return {}
        result = {}
        for code, detail in (details or {}).items():
            if not isinstance(detail, Mapping):
                continue
            open_date = self._native_metadata_date(
                detail.get("OpenDate") or detail.get("open_date")
            )
            if open_date:
                result[str(code).strip().upper()] = open_date
        self.scan_log.emit(
            f"已读取官方 QMT 合约元数据: {len(result)}/{len(stocks)} 只股票含上市日期"
        )
        return result

    def _scan_single_stock(self, stock: str, trade_days_cache: Dict[str, set]) -> List[dict]:
        """扫描单只股票的缺失数据"""
        if self._stop_flag:
            return []

        tasks = []
        try:
            # 批量获取所有周期的已有日期
            existing_dates = self.manager.get_existing_dates_batch(stock, list(self.periods_config.keys()))

            for period, (target_start, target_end) in self.periods_config.items():
                cache_key = f"{target_start}_{target_end}"
                all_trade_days = trade_days_cache.get(cache_key, set())
                existing = existing_dates.get(period, set())
                period_key = str(getattr(period, "value", period)).strip().lower()

                if period_key == "tick":
                    # 旧回退路径不能仅按“日期存在”判断 tick 完整；只
                    # 接受达到行数/字段阈值的日期，其余（含 partial）继续
                    # 作为缺口重取，并套用一个月保留窗。
                    all_trade_days = _tick_scan_retained_dates(all_trade_days)
                    existing = _tick_scan_complete_dates(
                        self.manager,
                        stock,
                        target_start,
                        target_end,
                    )
                else:
                    # 若主扫描器暂时不可用，仍需沿用其复权列门禁；否则
                    # 旧库中仅有 raw/front/back 的日期会把 ratio 缺口吞掉。
                    adjusted_existing = _fallback_kline_complete_dates(
                        self.manager,
                        stock,
                        period,
                        target_start,
                        target_end,
                        self.dividend_types,
                    )
                    if adjusted_existing is not None:
                        existing = adjusted_existing

                # 计算缺失日期
                missing = all_trade_days - existing
                if missing:
                    missing_dates = sorted(missing)

                    # 使用简化的分组逻辑（传入已排序的existing）
                    if period_key == "tick":
                        groups = _tick_scan_groups(
                            missing_dates,
                            all_trade_days,
                        )
                    else:
                        sorted_existing = sorted(existing) if existing else []
                        groups = self._group_missing_dates_fast2(
                            missing_dates,
                            sorted_existing,
                        )

                    for group_start, group_end, group_count in groups:
                        tasks.append({
                            'stock': stock,
                            'period': period,
                            'start': group_start,
                            'end': group_end,
                            'missing_days': group_count
                        })
        except Exception:
            pass  # 忽略单个股票的错误

        return tasks

    def _group_missing_dates_fast2(self, missing_dates: List[str], sorted_existing: List[str]) -> List[tuple]:
        """
        快速版缺失日期分组（性能优化版 - 接收已排序的列表）

        Args:
            missing_dates: 已排序的缺失日期列表
            sorted_existing: 已排序的已有数据日期列表

        Returns:
            [(start, end, count), ...] 分组结果
        """
        if not missing_dates:
            return []

        if not sorted_existing:
            # 没有已存在数据，所有缺失日期合并为一个任务
            return [(missing_dates[0], missing_dates[-1], len(missing_dates))]

        import bisect

        groups = []
        group_start = missing_dates[0]
        group_count = 1

        for i in range(1, len(missing_dates)):
            prev_date = missing_dates[i - 1]
            curr_date = missing_dates[i]

            # 使用二分查找检查是否有已存在数据在 prev_date 和 curr_date 之间
            idx = bisect.bisect_right(sorted_existing, prev_date)
            has_existing_between = (idx < len(sorted_existing) and sorted_existing[idx] < curr_date)

            if has_existing_between:
                # 需要分开，保存当前组
                groups.append((group_start, prev_date, group_count))
                # 开始新组
                group_start = curr_date
                group_count = 1
            else:
                group_count += 1

        # 保存最后一组
        groups.append((group_start, missing_dates[-1], group_count))

        return groups

    def _group_missing_dates(self, missing_dates: List[str], existing_dates: set, all_trade_days: set) -> List[tuple]:
        """
        将缺失日期按连续性分组

        规则：如果两个缺失日期之间有"已存在数据的交易日"，则分开成两个任务
              如果中间只有非交易日，则合并成一个任务

        Args:
            missing_dates: 已排序的缺失日期列表
            existing_dates: 已有数据的日期集合
            all_trade_days: 所有交易日集合

        Returns:
            [(start, end, count), ...] 分组结果
        """
        if not missing_dates:
            return []

        # 将所有交易日排序，用于快速判断两个日期之间是否有已存在数据
        sorted_trade_days = sorted(all_trade_days)

        groups = []
        group_start = missing_dates[0]
        group_dates = [missing_dates[0]]

        for i in range(1, len(missing_dates)):
            prev_date = missing_dates[i - 1]
            curr_date = missing_dates[i]

            # 检查 prev_date 和 curr_date 之间是否有已存在数据的交易日
            has_existing_between = False
            for trade_day in sorted_trade_days:
                if trade_day > prev_date and trade_day < curr_date:
                    # 这个交易日在两个缺失日期之间
                    if trade_day in existing_dates:
                        has_existing_between = True
                        break
                elif trade_day >= curr_date:
                    break

            if has_existing_between:
                # 需要分开，保存当前组
                groups.append((group_start, group_dates[-1], len(group_dates)))
                # 开始新组
                group_start = curr_date
                group_dates = [curr_date]
            else:
                # 不需要分开，继续当前组
                group_dates.append(curr_date)

        # 保存最后一组
        groups.append((group_start, group_dates[-1], len(group_dates)))

        return groups

    def _group_missing_dates_fast(self, missing_dates: List[str], existing_dates: set) -> List[tuple]:
        """
        快速版缺失日期分组（优化性能）

        简化规则：如果两个相邻的缺失日期之间有已存在数据的交易日，则分开

        Args:
            missing_dates: 已排序的缺失日期列表
            existing_dates: 已有数据的日期集合

        Returns:
            [(start, end, count), ...] 分组结果
        """
        if not missing_dates:
            return []

        if not existing_dates:
            # 没有已存在数据，所有缺失日期合并为一个任务
            return [(missing_dates[0], missing_dates[-1], len(missing_dates))]

        # 将 existing_dates 排序，用于二分查找
        import bisect
        sorted_existing = sorted(existing_dates)

        groups = []
        group_start = missing_dates[0]
        group_count = 1

        for i in range(1, len(missing_dates)):
            prev_date = missing_dates[i - 1]
            curr_date = missing_dates[i]

            # 使用二分查找检查是否有已存在数据在 prev_date 和 curr_date 之间
            # bisect_right 返回第一个大于 prev_date 的位置
            idx = bisect.bisect_right(sorted_existing, prev_date)
            has_existing_between = (idx < len(sorted_existing) and sorted_existing[idx] < curr_date)

            if has_existing_between:
                # 需要分开，保存当前组
                groups.append((group_start, prev_date, group_count))
                # 开始新组
                group_start = curr_date
                group_count = 1
            else:
                group_count += 1

        # 保存最后一组
        groups.append((group_start, missing_dates[-1], group_count))

        return groups

    def _generate_scan_report(self, tasks: List[dict], stocks_total: int,
                               stocks_with_db: int, stocks_without_db: int) -> dict:
        """
        生成详细的扫描报告

        Args:
            tasks: 扫描出的任务列表
            stocks_total: 总股票数
            stocks_with_db: 有本地数据的股票数
            stocks_without_db: 无本地数据的股票数

        Returns:
            详细报告字典
        """
        report = {
            'summary': {
                'total_stocks': stocks_total,
                'stocks_with_local_data': stocks_with_db,
                'stocks_without_local_data': stocks_without_db,
                'total_tasks': len(tasks),
                'total_missing_days': sum(t['missing_days'] for t in tasks),
            },
            'by_period': {},      # 按周期统计
            'by_market': {},      # 按市场统计
            'by_stock': {},       # 按股票统计
            'complete_stocks': [],  # 数据完整的股票列表
            'incomplete_stocks': [],  # 数据不完整的股票列表
        }

        # 收集所有涉及的股票
        stocks_in_tasks = set()

        # 按周期统计
        period_stats = {}
        for task in tasks:
            period = task['period']
            stock = task['stock']
            stocks_in_tasks.add(stock)

            if period not in period_stats:
                period_stats[period] = {
                    'task_count': 0,
                    'missing_days': 0,
                    'stocks': set(),
                    'date_range': {'min': None, 'max': None}
                }

            period_stats[period]['task_count'] += 1
            period_stats[period]['missing_days'] += task['missing_days']
            period_stats[period]['stocks'].add(stock)

            # 更新日期范围
            if period_stats[period]['date_range']['min'] is None or task['start'] < period_stats[period]['date_range']['min']:
                period_stats[period]['date_range']['min'] = task['start']
            if period_stats[period]['date_range']['max'] is None or task['end'] > period_stats[period]['date_range']['max']:
                period_stats[period]['date_range']['max'] = task['end']

        # 转换为可序列化格式
        for period, stats in period_stats.items():
            report['by_period'][period] = {
                'task_count': stats['task_count'],
                'missing_days': stats['missing_days'],
                'stock_count': len(stats['stocks']),
                'date_range': f"{stats['date_range']['min']} ~ {stats['date_range']['max']}"
            }

        # 按市场统计
        market_stats = {}
        for task in tasks:
            stock = task['stock']
            market = stock.split('.')[-1] if '.' in stock else 'Unknown'

            if market not in market_stats:
                market_stats[market] = {
                    'task_count': 0,
                    'missing_days': 0,
                    'stocks': set()
                }

            market_stats[market]['task_count'] += 1
            market_stats[market]['missing_days'] += task['missing_days']
            market_stats[market]['stocks'].add(stock)

        for market, stats in market_stats.items():
            report['by_market'][market] = {
                'task_count': stats['task_count'],
                'missing_days': stats['missing_days'],
                'stock_count': len(stats['stocks'])
            }

        # 按股票统计（前20个缺失最多的）
        stock_missing = {}
        for task in tasks:
            stock = task['stock']
            if stock not in stock_missing:
                stock_missing[stock] = {'periods': {}, 'total_missing': 0}

            period = task['period']
            stock_missing[stock]['periods'][period] = {
                'missing_days': task['missing_days'],
                'date_range': f"{task['start']} ~ {task['end']}"
            }
            stock_missing[stock]['total_missing'] += task['missing_days']

        # 排序取前20
        sorted_stocks = sorted(stock_missing.items(), key=lambda x: x[1]['total_missing'], reverse=True)
        report['by_stock'] = {k: v for k, v in sorted_stocks[:20]}

        # 完整 vs 不完整的股票
        report['incomplete_stocks'] = list(stocks_in_tasks)
        # 注意：complete_stocks 需要从外部传入，这里暂时留空

        return report

    def _log_scan_report(self, report: dict):
        """将扫描报告输出到日志"""
        summary = report['summary']

        self.scan_log.emit("")
        self.scan_log.emit("=" * 50)
        self.scan_log.emit("【扫描报告】")
        self.scan_log.emit("=" * 50)

        # 总体统计
        self.scan_log.emit("")
        self.scan_log.emit("▶ 总体统计:")
        self.scan_log.emit(f"  • 扫描股票总数: {summary['total_stocks']}")
        self.scan_log.emit(f"  • 有本地数据: {summary['stocks_with_local_data']} 只")
        self.scan_log.emit(f"  • 无本地数据: {summary['stocks_without_local_data']} 只")
        self.scan_log.emit(f"  • 生成任务数: {summary['total_tasks']}")
        self.scan_log.emit(f"  • 缺失总天数: {summary['total_missing_days']}")

        # 按周期统计
        if report['by_period']:
            self.scan_log.emit("")
            self.scan_log.emit("▶ 按周期统计:")
            for period, stats in report['by_period'].items():
                self.scan_log.emit(f"  [{period}]")
                self.scan_log.emit(f"    • 涉及股票: {stats['stock_count']} 只")
                self.scan_log.emit(f"    • 任务数: {stats['task_count']}")
                self.scan_log.emit(f"    • 缺失天数: {stats['missing_days']}")
                self.scan_log.emit(f"    • 日期范围: {stats['date_range']}")

        # 按市场统计
        if report['by_market']:
            self.scan_log.emit("")
            self.scan_log.emit("▶ 按市场统计:")
            for market, stats in report['by_market'].items():
                self.scan_log.emit(f"  [{market}] 股票: {stats['stock_count']} 只, 任务: {stats['task_count']}, 缺失: {stats['missing_days']} 天")

        # 缺失最多的股票
        if report['by_stock']:
            self.scan_log.emit("")
            self.scan_log.emit("▶ 缺失最多的股票 (Top 20):")
            for i, (stock, info) in enumerate(report['by_stock'].items(), 1):
                periods_str = ", ".join([f"{p}:{d['missing_days']}天" for p, d in info['periods'].items()])
                self.scan_log.emit(f"  {i:2d}. {stock}: 共缺失 {info['total_missing']} 天 ({periods_str})")

        self.scan_log.emit("")
        self.scan_log.emit("=" * 50)

    def _parallel_scan(self, stocks: List[str], trade_days_cache: Dict[str, set] = None) -> tuple:
        """
        扫描股票缺失数据（使用khQTTools中的check_duckdb_data_integrity函数）

        Args:
            stocks: 股票列表
            trade_days_cache: 交易日缓存（已废弃，保留参数仅为兼容性）

        Returns:
            (tasks, scan_stats) - 任务列表和扫描统计信息
        """
        # 扫描前确保释放所有数据库连接，防止多线程直接 duckdb.connect 发生文件锁冲突
        if hasattr(self.manager, 'close_all_no_checkpoint'):
            self.manager.close_all_no_checkpoint()
        elif hasattr(self.manager, 'close_all'):
            self.manager.close_all()

        # 获取数据目录
        data_root = self.manager.data_root

        # 预先获取已存在的数据库文件列表（用于统计）
        existing_dbs = set()
        for market in ['SH', 'SZ', 'BJ']:
            market_dir = os.path.join(data_root, market)
            if os.path.exists(market_dir):
                for f in os.listdir(market_dir):
                    if f.endswith('.db'):
                        code = f[:-3]
                        existing_dbs.add(f"{code}.{market}")

        self.scan_log.emit(f"本地已有 {len(existing_dbs)} 个数据库文件")

        # 统计：有本地数据的股票 vs 无本地数据的股票
        stocks_sorted = sorted(stocks, key=lambda x: (x.split('.')[-1] if '.' in x else 'SZ', x))
        stocks_with_db = [s for s in stocks_sorted if s in existing_dbs]
        stocks_without_db = [s for s in stocks_sorted if s not in existing_dbs]

        self.scan_log.emit(f"需扫描: {len(stocks_with_db)} 只（有本地数据）, {len(stocks_without_db)} 只（无本地数据）")

        # 获取周期列表和日期范围
        periods = list(self.periods_config.keys())

        # 找出所有周期的最早开始日期和最晚结束日期
        all_starts = [v[0] for v in self.periods_config.values()]
        all_ends = [v[1] for v in self.periods_config.values()]
        min_start = min(all_starts)
        max_end = max(all_ends)

        # 进度回调函数
        scan_progress_count = [0]  # 用列表封装以便在回调中修改
        total_stocks = len(stocks_sorted)

        def progress_callback(current, total, message, task_count):
            scan_progress_count[0] = current
            self.scan_progress.emit(current, total_stocks, message, task_count)

        # 停止标志检查函数
        def stop_flag_check():
            return self._stop_flag

        # 使用khQTTools中的check_duckdb_data_integrity函数
        try:
            native_open_dates = self._native_open_dates(stocks_sorted)
            result = khQTTools.check_duckdb_data_integrity(
                stock_list=stocks_sorted,
                periods=periods,
                start_date=min_start,
                end_date=max_end,
                # 每个周期只扫描其实际日期窗口；大 QMT 的只读完整性
                # 检查可提高并发，MiniQMT 不传入这些优化参数，保持原路径。
                period_ranges=(
                    self.periods_config if self.source == QMT_NATIVE else None
                ),
                scan_workers=(
                    # 首次扫描的主要成本是逐文件建立只读 DuckDB 连接。
                    # 原生大 QMT 的扫描只读且按股票库隔离，适度提高并发
                    # 可明显缩短连接/表结构探测阶段；完整性函数内部仍有
                    # 上限保护，避免无限制创建线程。
                    min(16, max(8, (os.cpu_count() or 8)))
                    if self.source == QMT_NATIVE else None
                ),
                duckdb_data_path=data_root,
                progress_callback=progress_callback,
                stop_flag=stop_flag_check,
                # 扫描必须与下载任务使用同一复权集合；否则 raw 完整但
                # 选中的前/后复权列缺失时会被误判为无需补充。tick
                # 周期由完整性函数忽略该参数并固定为不复权。
                dividend_type=_integrity_dividend_types(self.dividend_types),
            )

            # 从结果中提取任务列表
            all_tasks = result.get('missing_tasks', [])

            # 过滤：只保留在periods_config中配置的周期对应日期范围内的任务。
            # 大 QMT 的全量扫描必须同时应用官方 OpenDate：上市日前没有
            # 官方 K 线，这类区间属于合法空段，不应再提交给异步下载器并
            # 在 GUI 中显示为失败。跨越上市日的任务从 OpenDate 开始请求，
            # 保留上市后的真实缺口。
            filtered_tasks = []
            prelisting_dropped = 0
            prelisting_clamped = 0
            for task in all_tasks:
                period = task['period']
                if period not in self.periods_config:
                    continue
                period_start, period_end = self.periods_config[period]
                task_start = task['start']
                task_end = task['end']
                open_date = native_open_dates.get(
                    str(task.get('stock') or '').strip().upper(), ''
                )
                if open_date:
                    if task_end < open_date:
                        prelisting_dropped += 1
                        continue
                    if task_start < open_date:
                        task_start = open_date
                        prelisting_clamped += 1

                # 检查任务日期是否在配置的范围内
                if task_start >= period_start and task_end <= period_end:
                    filtered_tasks.append({
                        **task, 'start': task_start, 'end': task_end,
                    })
                elif task_start <= period_end and task_end >= period_start:
                    # 部分重叠，调整日期范围
                    new_start = max(task_start, period_start)
                    new_end = min(task_end, period_end)
                    if new_start <= new_end:
                        # 重新计算缺失天数（近似）
                        original_days = task['missing_days']
                        original_span = _calendar_day_span(task_start, task_end)
                        new_span = _calendar_day_span(new_start, new_end)
                        if original_span > 0:
                            new_missing_days = max(1, int(original_days * new_span / original_span))
                        else:
                            new_missing_days = original_days
                        filtered_tasks.append({
                            'stock': task['stock'],
                            'period': period,
                            'start': new_start,
                            'end': new_end,
                            'missing_days': new_missing_days
                        })

            all_tasks = filtered_tasks
            if prelisting_dropped or prelisting_clamped:
                self.scan_log.emit(
                    f"上市前合法空段优化: 丢弃 {prelisting_dropped} 个任务，"
                    f"裁剪 {prelisting_clamped} 个任务"
                )
            self.scan_log.emit(f"扫描完成，共发现 {len(all_tasks)} 个下载任务")

        except Exception as e:
            self.scan_log.emit(f"使用check_duckdb_data_integrity扫描失败: {e}，回退到旧方法")
            import traceback
            traceback.print_exc()
            # 回退到原来的扫描逻辑（需要生成交易日缓存）
            if trade_days_cache is None or not trade_days_cache:
                self.scan_log.emit("生成交易日缓存用于回退扫描...")
                trade_days_cache = self._generate_trade_days_cache()
            all_tasks = self._parallel_scan_fallback(stocks_sorted, trade_days_cache, existing_dbs)

        # 返回任务和统计信息
        scan_stats = {
            'stocks_total': len(stocks_sorted),
            'stocks_with_db': len(stocks_with_db),
            'stocks_without_db': len(stocks_without_db),
        }
        return all_tasks, scan_stats

    def _parallel_scan_fallback(self, stocks_sorted: List[str], trade_days_cache: Dict[str, set], existing_dbs: set) -> List[dict]:
        """
        回退扫描方法（当khQTTools函数不可用时使用）
        """
        all_tasks = []
        total = len(stocks_sorted)
        batch_size = 100

        # 统计：有本地数据的股票 vs 无本地数据的股票
        stocks_with_db = [s for s in stocks_sorted if s in existing_dbs]
        stocks_without_db = [s for s in stocks_sorted if s not in existing_dbs]

        # 1. 先处理无本地数据的股票（直接标记为全部缺失，无需打开数据库）
        for stock in stocks_without_db:
            if self._stop_flag:
                break
            for period, (target_start, target_end) in self.periods_config.items():
                cache_key = f"{target_start}_{target_end}"
                all_trade_days = trade_days_cache.get(cache_key, set())
                period_key = str(getattr(period, "value", period)).strip().lower()
                if period_key == "tick":
                    all_trade_days = _tick_scan_retained_dates(all_trade_days)
                if not all_trade_days:
                    continue
                if period_key == "tick":
                    groups = _tick_scan_groups(
                        all_trade_days,
                        all_trade_days,
                    )
                else:
                    # 保留旧回退路径的区间边界（用户配置可能包含周末），
                    # 仅 tick 使用按交易日拆分的安全边界。
                    groups = [(target_start, target_end, len(all_trade_days))]
                for group_start, group_end, group_count in groups:
                    all_tasks.append({
                        'stock': stock,
                        'period': period,
                        'start': group_start,
                        'end': group_end,
                        'missing_days': group_count,
                    })

        self.scan_log.emit(f"无本地数据股票处理完成，已添加 {len(all_tasks)} 个任务")

        # 2. 扫描有本地数据的股票
        for i in range(0, len(stocks_with_db), batch_size):
            if self._stop_flag:
                break

            batch = stocks_with_db[i:i + batch_size]
            batch_tasks = []

            for stock in batch:
                if self._stop_flag:
                    break
                tasks = self._scan_single_stock(stock, trade_days_cache)
                batch_tasks.extend(tasks)

            all_tasks.extend(batch_tasks)

            current = min(i + len(batch), len(stocks_with_db))
            self.scan_progress.emit(
                len(stocks_without_db) + current, total,
                f"已扫描 {current}/{len(stocks_with_db)} (有数据)",
                len(all_tasks)
            )

            if (i + batch_size) % 500 == 0 or current == len(stocks_with_db):
                self.scan_log.emit(f"进度: {current}/{len(stocks_with_db)}, 共发现 {len(all_tasks)} 个任务")

        self.scan_log.emit(f"扫描完成，共发现 {len(all_tasks)} 个下载任务")
        return all_tasks

    def run(self):
        # 在方法开头导入 datetime，避免作用域问题
        from datetime import datetime, timedelta
        short_lock_enabled = False
        importer = None
        self._scan_start_time = datetime.now()
        resume_metadata_pairs = {
            pair
            for pair in (_progress_key_stock_period(key) for key in self.completed_tasks)
            if pair is not None
        }

        try:
            # ========== 新增：检查并下载 000300.SH 数据 ==========
            try:
                self.scan_log.emit("检查基准指数 000300.SH 数据...")

                # 检查是否已有 000300.SH 数据
                benchmark_code = '000300.SH'
                has_benchmark = False

                try:
                    # 查询数据库中是否有 000300.SH 的数据
                    stocks = self.manager.get_available_stocks()
                    has_benchmark = benchmark_code in stocks
                    self.scan_log.emit(f"当前数据库中有 {len(stocks)} 只股票")
                except Exception as e:
                    self.scan_log.emit(f"查询数据库失败: {e}")
                    import traceback
                    traceback.print_exc()

                if not has_benchmark:
                    self.scan_log.emit("未找到基准指数数据，正在下载 000300.SH 日线数据...")

                    end_date = datetime.now()
                    if self.source == QMT_NATIVE:
                        start_date = end_date - timedelta(days=3650 - 1)
                    else:
                        start_date = end_date - timedelta(days=365*20)

                    start_str = start_date.strftime("%Y%m%d")
                    end_str = end_date.strftime("%Y%m%d")

                    if self.source == QMT_NATIVE:
                        try:
                            from duckdb_storage.history_adapters import HistoryImportService
                            service = HistoryImportService("qmt_native")
                            try:
                                target_adjustments = list(self.dividend_types or ["none"])
                                if "none" not in target_adjustments:
                                    target_adjustments.append("none")
                                import_res = service.import_to_duckdb(
                                    manager=self.manager,
                                    codes=[benchmark_code],
                                    period="1d",
                                    start=start_str,
                                    end=end_str,
                                    adjustments=target_adjustments,
                                )
                                records = import_res.get("saved", 0)
                                self.scan_log.emit(f"已成功通过大 QMT 原生桥下载并保存 000300.SH 数据，共 {records} 条记录")
                            finally:
                                service.close()
                        except Exception as native_err:
                            self.scan_log.emit(f"原生桥下载 000300.SH 失败: {native_err}")
                        raise _SkipQmtBenchmark()

                    # 尝试导入xtquant
                    from xtquant import xtdata

                    # 下载数据
                    xtdata.download_history_data(
                        benchmark_code,
                        period='1d',
                        start_time=start_str,
                        end_time=end_str,
                        incrementally=True
                    )

                    # 获取不复权数据
                    data_none = xtdata.get_local_data(
                        field_list=[],
                        stock_list=[benchmark_code],
                        period='1d',
                        start_time=start_str,
                        end_time=end_str,
                        dividend_type='none',
                        fill_data=True
                    )

                    if benchmark_code in data_none and data_none[benchmark_code] is not None and len(data_none[benchmark_code]) > 0:
                        df = data_none[benchmark_code].copy()

                        dividend_types = self.dividend_types
                        if dividend_types is None:
                            dividend_types = ['front', 'back', 'front_ratio', 'back_ratio']

                        for div_type in dividend_types:
                            try:
                                data_adj = xtdata.get_local_data(
                                    field_list=['time', 'open', 'high', 'low', 'close'],
                                    stock_list=[benchmark_code],
                                    period='1d',
                                    start_time=start_str,
                                    end_time=end_str,
                                    dividend_type=div_type,
                                    fill_data=True
                                )

                                if benchmark_code in data_adj and data_adj[benchmark_code] is not None:
                                    df_adj = data_adj[benchmark_code]

                                    # 重命名复权字段
                                    suffix = div_type
                                    rename_map = {
                                        'open': f'open_{suffix}',
                                        'high': f'high_{suffix}',
                                        'low': f'low_{suffix}',
                                        'close': f'close_{suffix}'
                                    }
                                    df_adj = df_adj.rename(columns=rename_map)
                                    df_adj = df_adj[list(rename_map.values())]

                                    # 合并到主 DataFrame
                                    df = df.merge(df_adj, left_index=True, right_index=True, how='left')

                                    del data_adj, df_adj
                            except Exception as e:
                                self.scan_log.emit(f"获取 000300.SH {div_type} 复权数据失败: {e}")

                        # 保存到DuckDB
                        records = self.manager.save_kline_data(df, benchmark_code, '1d', 'none')
                        self.scan_log.emit(f"已成功下载并保存 000300.SH 数据，共 {records} 条记录")

                        del df, data_none
                    else:
                        self.scan_log.emit("警告：000300.SH 数据下载失败，请手动补充")
                else:
                    self.scan_log.emit("基准指数 000300.SH 数据已存在")

            except _SkipQmtBenchmark:
                self.scan_log.emit("大QMT原生桥模式：跳过 MiniQMT/xtquant 基准指数检查")
            except Exception as e:
                self.scan_log.emit(f"检查/下载 000300.SH 数据时出错: {e}")
                import traceback
                traceback.print_exc()
            # ========== 基准指数检查结束 ==========

            # ===== 第一阶段：扫描 =====
            self.scan_log.emit("开始收集股票列表...")
            stocks = self.collect_all_stocks()
            self.resolved_stocks = list(stocks)

            if not stocks:
                self.error.emit("未找到任何股票")
                return

            self.scan_log.emit(f"共收集 {len(stocks)} 只股票")
            self.scan_log.emit(f"选中周期: {', '.join(self.periods_config.keys())}")
            self.scan_log.emit("=" * 50)

            if self.force_overwrite:
                self.scan_log.emit("已启用强制覆写，跳过增量扫描，直接生成全量任务")
                data_root = self.manager.data_root
                existing_dbs = set()
                for market in ['SH', 'SZ', 'BJ']:
                    market_dir = os.path.join(data_root, market)
                    if os.path.exists(market_dir):
                        for f in os.listdir(market_dir):
                            if f.endswith('.db'):
                                code = f[:-3]
                                existing_dbs.add(f"{code}.{market}")

                stocks_sorted = sorted(stocks, key=lambda x: (x.split('.')[-1] if '.' in x else 'SZ', x))
                stocks_with_db = [s for s in stocks_sorted if s in existing_dbs]
                stocks_without_db = [s for s in stocks_sorted if s not in existing_dbs]

                trade_days_cache = self._generate_trade_days_cache()
                self.tasks = []
                for stock in stocks_sorted:
                    for period, (target_start, target_end) in self.periods_config.items():
                        cache_key = f"{target_start}_{target_end}"
                        all_trade_days = trade_days_cache.get(cache_key, set())
                        # force_overwrite 不能绕过 QMT tick 的近一个月/31
                        # 自然日请求边界；仍按安全分段生成任务。bar 保持
                        # 历史的单区间覆写行为。
                        for group_start, group_end, group_count in _force_task_groups(
                            period, target_start, target_end, all_trade_days
                        ):
                            self.tasks.append({
                                'stock': stock,
                                'period': getattr(period, "value", period),
                                'start': group_start,
                                'end': group_end,
                                'missing_days': group_count,
                            })

                scan_stats = {
                    'stocks_total': len(stocks_sorted),
                    'stocks_with_db': len(stocks_with_db),
                    'stocks_without_db': len(stocks_without_db),
                }
            else:
                self.scan_log.emit("开始扫描缺失数据（使用优化方法）...")
                self.tasks, scan_stats = self._parallel_scan(stocks, {})

            # 断点键和 worker payload 必须绑定来源；同一时间区间从两个
            # 客户端取得的数据不能互相覆盖/误命中断点。
            for _task in self.tasks:
                _task.setdefault('source', self.source)

            if self._stop_flag:
                self.scan_log.emit("扫描已取消")
                self.finished.emit({
                    'success': 0, 'failed': 0, 'skipped': 0,
                    'total_records': 0, 'cancelled': True,
                })
                return

            # 生成详细报告
            report = self._generate_scan_report(
                self.tasks,
                scan_stats['stocks_total'],
                scan_stats['stocks_with_db'],
                scan_stats['stocks_without_db']
            )

            # 统计结果
            total_missing = sum(t['missing_days'] for t in self.tasks)
            self.scan_log.emit("=" * 50)
            self.scan_log.emit(f"扫描完成: 发现 {len(self.tasks)} 个下载任务, 共缺失 {total_missing} 天数据")

            # 输出详细报告到日志
            self._log_scan_report(report)

            self.scan_finished.emit(len(self.tasks), total_missing, report)

            if not self.tasks:
                self.scan_log.emit("所有数据已完整，无需补充")
                self._refresh_download_metadata(resume_metadata_pairs)
                self.finished.emit({
                    'success': 0, 'failed': 0, 'skipped': 0,
                    'total_records': 0, 'cancelled': False,
                })
                return

            # ===== 第二阶段：下载（流水线并行模式）=====
            self.download_log.emit("=" * 50)
            self.download_log.emit(f"开始下载 {len(self.tasks)} 个任务...")
            self.start_time = datetime.now()
            self._download_start_time = self.start_time

            results = {
                'success': 0, 'failed': 0, 'skipped': 0,
                'total_records': 0, 'cancelled': False,
            }
            imported_records = []
            try:
                if hasattr(self.manager, 'enable_short_lock_write'):
                    self.manager.enable_short_lock_write()
                    short_lock_enabled = True
            except Exception as e:
                self.download_log.emit(f"启用短锁写入模式失败，将使用普通写入模式: {e}")

            # 构建待下载列表（跳过已完成）
            pending_tasks = []
            for task in self.tasks:
                task_key = _increment_task_key(
                    task, _task_dividend_types(task.get('period'), self.dividend_types),
                    self.force_overwrite, self.source
                )
                if task_key in self.completed_tasks:
                    # 当前扫描仍判定为缺失，说明断点记录与真实股票库不一致；
                    # 不能因旧记录跳过，否则会永久留下数据洞。
                    self.completed_tasks.discard(task_key)
                    self.download_log.emit(
                        f"  断点校验失效，重新补充: {task['stock']} {task['period']} "
                        f"{task['start']}~{task['end']}"
                    )
                pending_tasks.append(task)

            total_pending = len(pending_tasks)
            if total_pending == 0:
                self.download_log.emit("所有任务已完成，无需下载")
                self._refresh_download_metadata(resume_metadata_pairs)
                self.finished.emit(results)
                return

            # MiniQMT keeps stock-first ordering to reuse one DuckDB
            # connection.  Native QMT groups compatible period/range tasks
            # so the worker can create one multi-code bundle; the per-stock
            # remaining counter still closes each DB immediately after its
            # final logical task has been written.
            pending_tasks.sort(
                key=lambda item: _history_task_sort_key(item, self.source)
            )
            remaining_by_stock = {}
            for pending in pending_tasks:
                stock = pending.get('stock', '')
                remaining_by_stock[stock] = remaining_by_stock.get(stock, 0) + 1

            def release_stock_when_done(task):
                if not task:
                    return
                stock = task.get('stock', '')
                remaining_by_stock[stock] = max(0, remaining_by_stock.get(stock, 1) - 1)
                if remaining_by_stock[stock] == 0:
                    try:
                        self.manager.close_stock_connection(stock, skip_checkpoint=True)
                    except Exception:
                        pass

            NUM_DL_WORKERS = self.workers_effective
            # Native results are file-backed and its queue may intentionally
            # contain a 64-task bundle window.  Keep that single worker alive
            # instead of periodically breaking the look-ahead queue.
            RESTART_INTERVAL = (
                1_000_000_000 if self.source == QMT_NATIVE else 300
            )
            importer = MultiProcessImporter(
                num_workers=NUM_DL_WORKERS,
                timeout_per_task=120.0,
                max_task_retries=self.max_task_retries,
                retry_backoff=self.retry_backoff,
                source=self.source,
                bridge_dir=self.bridge_dir,
**_native_options_for_source(self.source, _native_options_from_owner(self)),
            )
            self._active_importer = importer
            _configure_history_importer_defaults(
                importer,
                self.source,
                force=self.force_overwrite,
                allow_gaps=self.allow_gaps,
                local_only=False,
                idempotent=True,
                incrementally=not (
                    self.source == QMT_NATIVE
                    and getattr(self, "mode", None) == "historical-backfill"
                ),
                instance_generation=self.instance_generation,
**_native_options_for_source(self.source, _native_options_from_owner(self)),
                cancel_after=getattr(self, "cancel_after", None),
            )
            importer.start()
            if self.source == QMT_NATIVE and self.cancel_after is not None:
                import threading

                def _deadline_cancel_full():
                    self._stop_flag = True
                    _cancel_history_runner(self._active_importer, timeout=2.0)

                self._cancel_timer = threading.Timer(
                    float(self.cancel_after), _deadline_cancel_full
                )
                self._cancel_timer.daemon = True
                self._cancel_timer.start()

            native_progress_stats = _new_native_progress_stats()

            def report_source_events():
                for event in importer.get_all_progress():
                    if self.source == QMT_NATIVE:
                        _consume_native_progress_stats(native_progress_stats, event)
                    if event.get('type') == 'retry':
                        self.download_log.emit(
                            f"  ↻ {event.get('stock')} {event.get('period')} 连接异常，"
                            f"{event.get('delay', 0):g}秒后进行第{event.get('attempt')}次重试"
                        )
                    elif event.get('type') == 'fatal':
                        self.download_log.emit(
                            f"  数据源工作进程异常: {event.get('msg', '未知错误')}"
                        )
                    elif event.get('type') == 'native_phase':
                        self.download_log.emit(
                            f"  原生桥 {event.get('stock', '')} "
                            f"{event.get('period', '')}: {event.get('phase', '处理中')}"
                        )
                    elif event.get('type') == 'native_generation':
                        self.download_log.emit(
                            (
                                "  原生桥已重启，自动切换到新实例并继续下载"
                                if event.get('generation_changed') else
                                "  原生桥代次已同步，继续下载"
                            )
                        )
                    elif event.get('type') == 'executor_started':
                        self.download_log.emit(
                            f"  实际下载并发: {event.get('workers_effective', NUM_DL_WORKERS)}"
                        )

            next_submit = 0
            completed_count = 0
            tasks_since_restart = 0
            id_to_task = {}  # task_id -> task dict

            def emit_download_progress(current_stock="", current_period=""):
                """统一发出任务完成进度（主循环与重启排空路径共用）。"""
                eta_seconds = 0
                if completed_count > 0:
                    elapsed = (datetime.now() - self.start_time).total_seconds()
                    eta_seconds = max(
                        0, int(elapsed * (total_pending - completed_count) / completed_count)
                    )
                progress_message = (
                    f"下载 {current_stock} {current_period} "
                    f"({completed_count}/{total_pending})"
                )
                if self.source == QMT_NATIVE:
                    # Keep the legacy four-argument Qt signal intact while
                    # exposing the bridge-level counters in the visible GUI
                    # status text.  The accumulator is bounded by task id and
                    # never carries rows/manifests through the signal.
                    progress_message += " | " + _format_native_progress_summary(
                        native_progress_stats, eta_seconds
                    )
                    scan_elapsed = 0.0
                    if getattr(self, "_scan_start_time", None) is not None:
                        scan_elapsed = max(
                            0.0,
                            (self.start_time - self._scan_start_time).total_seconds(),
                        )
                    progress_message += " | scan=%.1fs download=%.1fs" % (
                        scan_elapsed,
                        max(0.0, (datetime.now() - self.start_time).total_seconds()),
                    )
                self.download_progress.emit(
                    completed_count,
                    total_pending,
                    progress_message,
                    eta_seconds,
                )

            if self.source == QMT_NATIVE:
                self.download_log.emit(
                    "原生大QMT桥：workers_requested=%d, workers_effective=%d, "
                    "bridge_inflight=1" % (
                        self.workers_requested,
                        NUM_DL_WORKERS,
                    )
                )
            else:
                self.download_log.emit(f"使用 {NUM_DL_WORKERS} 个下载进程并行下载")

            # Native QMT remains one effective worker/in-flight request, but
            # prequeue a bounded descriptor window so that worker-side
            # look-ahead can coalesce compatible stock codes into one bundle.
            prefetch_limit = _history_prefetch_limit(
                self.source, total_pending, NUM_DL_WORKERS
            )
            while next_submit < prefetch_limit and not self._stop_flag:
                t = pending_tasks[next_submit]
                importer.add_task(
                    t['stock'], t['period'], t['start'], t['end'],
                    _task_dividend_types(t.get('period'), self.dividend_types),
                )
                id_to_task[importer._task_id] = t
                next_submit += 1

            # 主循环：收到结果→处理→提交下一个
            while completed_count < next_submit and not self._stop_flag:
                result = importer.get_result(timeout=0.5)
                report_source_events()
                if not result:
                    continue

                # 通过 task_id 匹配原始任务
                tid = result.get('task_id')
                orig_task = id_to_task.pop(tid, None)
                r_stock = result.get('stock', '')
                r_period = result.get('period', '')
                if self.source == QMT_NATIVE:
                    _consume_native_progress_stats(
                        native_progress_stats,
                        {
                            "type": "result",
                            "task_id": tid,
                            "native_job_ids": result.get("native_job_ids"),
                            "native_job_details": result.get("native_job_details"),
                            "records": result.get("records", 0),
                            "transport_bytes": result.get("transport_bytes", 0),
                        },
                    )

                df = None
                df_dict = None
                task_saved = False
                try:
                    native_result_safe = (
                        self.source != QMT_NATIVE
                        or _native_worker_result_write_safe(
                            result,
                            allow_gaps=bool(getattr(self, "allow_gaps", False)),
                        )
                    )
                    if result.get('success') and native_result_safe:
                        df_dict = result.get('df_dict')
                        if _is_native_legal_empty(result, self.source):
                            task_saved = True
                            results['skipped'] = results.get('skipped', 0) + 1
                            self.download_log.emit(f"  跳过: {r_stock} {r_period} (历史停牌/无交易，无需补充)")
                            if self.source == QMT_NATIVE:
                                _finalize_native_worker_result(
                                    result,
                                    frame=None,
                                    runner=importer,
                                    committed=True,
                                    status_callback=self.download_log.emit,
                                )
                        elif df_dict:
                            df = dict_to_dataframe(df_dict)
                            if df is not None and len(df) > 0:
                                save_status, saved = self._save_download_frame(
                                    df, r_stock, r_period, results
                                )
                                if self.source == QMT_NATIVE and save_status == "saved":
                                    _finalize_native_worker_result(
                                        result,
                                        frame=df,
                                        runner=importer,
                                        committed=bool(saved and saved > 0),
                                        status_callback=self.download_log.emit,
                                    )
                                if save_status == "saved" and saved and saved > 0:
                                    imported_records.append((r_stock, r_period, int(saved)))
                                    task_saved = True
                                    results['success'] += 1
                                    results['total_records'] += int(saved)
                                    self.download_log.emit(f"  成功: {r_stock} {r_period} ({saved}条)")
                                elif save_status == "saved":
                                    results['failed'] += 1
                                    self.download_log.emit(f"  未保存: {r_stock} {r_period}")
                            else:
                                results['failed'] += 1
                                self.download_log.emit(f"  空数据: {r_stock} {r_period}")
                                if self.source == QMT_NATIVE:
                                    _finalize_native_worker_result(
                                        result,
                                        frame=df,
                                        runner=importer,
                                        committed=False,
                                        status_callback=self.download_log.emit,
                                    )
                        else:
                            results['failed'] += 1
                            self.download_log.emit(f"  无数据: {r_stock} {r_period}")
                            if self.source == QMT_NATIVE:
                                _finalize_native_worker_result(
                                    result,
                                    committed=False,
                                    runner=importer,
                                    status_callback=self.download_log.emit,
                                )
                    elif result.get('success'):
                        results['failed'] += 1
                        self.download_log.emit(
                            f"  失败: {r_stock} {r_period} - "
                            "原生桥 bundle 未完成或含缺口，拒绝写入"
                        )
                        _finalize_native_worker_result(
                            result,
                            runner=importer,
                            committed=False,
                            status_callback=self.download_log.emit,
                        )
                    else:
                        results['failed'] += 1
                        error_msg = result.get('error', '未知错误')
                        tb = result.get('traceback', '')
                        if tb and 'KeyError' in tb:
                            error_msg = f"数据字段缺失: {error_msg}"
                        elif tb and 'Connection' in tb:
                            error_msg = f"连接错误: {error_msg}"
                        self.download_log.emit(f"  失败: {r_stock} {r_period} - {error_msg}")

                    # 只有真正写入成功的任务才能进入断点完成集合。
                    if task_saved and orig_task:
                        tk = _increment_task_key(
                            orig_task,
                            _task_dividend_types(orig_task.get('period'), self.dividend_types),
                            self.force_overwrite, self.source
                        )
                        self.completed_tasks.add(tk)
                        completed_task = dict(orig_task)
                        completed_task['_progress_key'] = tk
                        self.task_completed.emit(completed_task)
                finally:
                    if df is not None:
                        del df
                    if df_dict is not None:
                        del df_dict
                    release_stock_when_done(orig_task)

                completed_count += 1
                tasks_since_restart += 1

                # 提交下一个任务保持管道满
                if next_submit < total_pending and not self._stop_flag:
                    t = pending_tasks[next_submit]
                    importer.add_task(
                        t['stock'], t['period'], t['start'], t['end'],
                        _task_dividend_types(t.get('period'), self.dividend_types),
                    )
                    id_to_task[importer._task_id] = t
                    next_submit += 1

                # 更新进度和 ETA
                emit_download_progress(r_stock, r_period)

                if completed_count % 20 == 0:
                    self.manager._cleanup_idle_connections(
                        keep_recent=max(8, NUM_DL_WORKERS * 2),
                        skip_checkpoint=True,
                    )
                    import gc
                    gc.collect()

                # 工作进程重启：先排空管道
                if tasks_since_restart >= RESTART_INTERVAL and not self._stop_flag:
                    self.download_log.emit(f"  [内存优化] 已处理 {tasks_since_restart} 个任务，重启工作进程...")
                    # 排空管道中剩余的in-flight任务
                    while completed_count < next_submit and not self._stop_flag:
                        r2 = importer.get_result(timeout=1.0)
                        report_source_events()
                        if r2:
                            tid2 = r2.get('task_id')
                            ot2 = id_to_task.pop(tid2, None)
                            if self.source == QMT_NATIVE:
                                _consume_native_progress_stats(
                                    native_progress_stats,
                                    {
                                        "type": "result",
                                        "task_id": tid2,
                                        "native_job_ids": r2.get("native_job_ids"),
                                        "records": r2.get("records", 0),
                                        "transport_bytes": r2.get("transport_bytes", 0),
                                    },
                                )
                            # 简化处理排空结果
                            df2, dd2 = None, None
                            task2_saved = False
                            try:
                                native_r2_safe = (
                                    self.source != QMT_NATIVE
                                    or _native_worker_result_write_safe(
                                        r2,
                                        allow_gaps=bool(getattr(self, "allow_gaps", False)),
                                    )
                                )
                                if r2.get('success') and native_r2_safe:
                                    rs, rp = r2.get('stock',''), r2.get('period','')
                                    if _is_native_legal_empty(r2, self.source):
                                        task2_saved = True
                                        results['skipped'] = results.get('skipped', 0) + 1
                                        self.download_log.emit(f"  跳过: {rs} {rp} (历史停牌/无交易，无需补充)")
                                        if self.source == QMT_NATIVE:
                                            _finalize_native_worker_result(
                                                r2,
                                                frame=None,
                                                runner=importer,
                                                committed=True,
                                                status_callback=self.download_log.emit,
                                            )
                                    else:
                                        dd2 = r2.get('df_dict')
                                        if dd2:
                                            df2 = dict_to_dataframe(dd2)
                                            if df2 is not None and len(df2) > 0:
                                                save_status2, saved2 = self._save_download_frame(
                                                    df2, rs, rp, results
                                                )
                                                if self.source == QMT_NATIVE and save_status2 == "saved":
                                                    _finalize_native_worker_result(
                                                        r2,
                                                        frame=df2,
                                                        runner=importer,
                                                        committed=bool(saved2 and saved2 > 0),
                                                        status_callback=self.download_log.emit,
                                                    )
                                                if save_status2 == "saved" and saved2 and saved2 > 0:
                                                    imported_records.append((rs, rp, int(saved2)))
                                                    task2_saved = True
                                                    results['success'] += 1; results['total_records'] += int(saved2)
                                                    self.download_log.emit(f"  成功: {rs} {rp} ({saved2}条)")
                                                elif save_status2 == "saved": results['failed'] += 1
                                            else:
                                                results['failed'] += 1
                                                if self.source == QMT_NATIVE:
                                                    _finalize_native_worker_result(
                                                        r2,
                                                        frame=df2,
                                                        runner=importer,
                                                        committed=False,
                                                        status_callback=self.download_log.emit,
                                                    )
                                        else:
                                            results['failed'] += 1
                                            if self.source == QMT_NATIVE:
                                                _finalize_native_worker_result(
                                                    r2,
                                                    runner=importer,
                                                    committed=False,
                                                    status_callback=self.download_log.emit,
                                                )
                                elif r2.get('success'):
                                    results['failed'] += 1
                                    self.download_log.emit(
                                        f"  失败: {r2.get('stock', '')} {r2.get('period', '')} - "
                                        "原生桥 bundle 未完成或含缺口，拒绝写入"
                                    )
                                    _finalize_native_worker_result(
                                        r2,
                                        runner=importer,
                                        committed=False,
                                        status_callback=self.download_log.emit,
                                    )
                                else: results['failed'] += 1
                                if task2_saved and ot2:
                                    tk2 = _increment_task_key(
                                        ot2,
                                        _task_dividend_types(ot2.get('period'), self.dividend_types),
                                        self.force_overwrite, self.source
                                    )
                                    self.completed_tasks.add(tk2)
                                    completed_task2 = dict(ot2)
                                    completed_task2['_progress_key'] = tk2
                                    self.task_completed.emit(completed_task2)
                            finally:
                                del df2, dd2
                                release_stock_when_done(ot2)
                            completed_count += 1
                            # 重启前排空的结果也属于已完成任务；立即
                            # 通知 GUI，避免进度条在排空期间停留旧值。
                            emit_download_progress(r2.get('stock', ''), r2.get('period', ''))
                    importer.stop()
                    import gc; gc.collect()
                    importer = MultiProcessImporter(
                        num_workers=NUM_DL_WORKERS,
                        timeout_per_task=120.0,
                        max_task_retries=self.max_task_retries,
                        retry_backoff=self.retry_backoff,
                        source=self.source,
                        bridge_dir=self.bridge_dir,
**_native_options_for_source(self.source, _native_options_from_owner(self)),
                    )
                    self._active_importer = importer
                    _configure_history_importer_defaults(
                        importer,
                        self.source,
                        force=self.force_overwrite,
                        allow_gaps=self.allow_gaps,
                        local_only=False,
                        idempotent=True,
                        incrementally=not (
                            self.source == QMT_NATIVE
                            and getattr(self, "mode", None) == "historical-backfill"
                        ),
                        instance_generation=self.instance_generation,
**_native_options_for_source(self.source, _native_options_from_owner(self)),
                        cancel_after=getattr(self, "cancel_after", None),
                    )
                    importer.start()
                    tasks_since_restart = 0
                    id_to_task.clear()
                    # 重新填充管道
                    _refill_limit = min(next_submit + NUM_DL_WORKERS, total_pending)
                    while next_submit < _refill_limit and not self._stop_flag:
                        t = pending_tasks[next_submit]
                        importer.add_task(
                            t['stock'], t['period'], t['start'], t['end'],
                            _task_dividend_types(t.get('period'), self.dividend_types),
                        )
                        id_to_task[importer._task_id] = t
                        next_submit += 1
                    self.download_log.emit(f"  [内存优化] 工作进程已重启")

            importer.stop()
            self.manager.cleanup_connections_aggressive(skip_checkpoint=True)
            self._refresh_download_metadata(
                list(imported_records) + list(resume_metadata_pairs)
            )
            results['cancelled'] = self._stop_flag
            self.finished.emit(results)

        except Exception as e:
            import traceback
            self.error.emit(f"执行异常: {e}\n{traceback.format_exc()}")
        finally:
            if importer is not None:
                try:
                    if getattr(importer, 'is_running', False):
                        _cancel_history_runner(importer, timeout=2.0)
                except Exception:
                    pass
            timer = getattr(self, "_cancel_timer", None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
                self._cancel_timer = None
            self._active_importer = None
            if short_lock_enabled and hasattr(self.manager, 'disable_short_lock_write'):
                self.manager.disable_short_lock_write()


class LocalMiniQMTImportThread(QThread):
    progress = pyqtSignal(int, int, str, int)
    log = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    PRESET_SECTORS = [
        ('沪深A股', '沪深A股_股票列表.csv'),
        ('上证A股', '上证A股_股票列表.csv'),
        ('深证A股', '深证A股_股票列表.csv'),
        ('沪深300', '沪深300成分股_股票列表.csv'),
        ('上证50', '上证50成分股_股票列表.csv'),
        ('中证500', '中证500成分股_股票列表.csv'),
        ('创业板', '创业板_股票列表.csv'),
        ('科创板', '科创板_股票列表.csv'),
        ('沪深ETF', '沪深ETF_成分股列表.csv'),
        ('沪深场内基金（含ETF/LOF）', '沪深基金_列表.csv'),
        ('沪深转债', '沪深转债_列表.csv'),
        ('T0型ETF', 'T0型ETF.csv'),
        ('指数', '指数_股票列表.csv'),
    ]

    def __init__(self, manager: DuckDBManager, periods_config: Dict[str, tuple],
                 dividend_types: Optional[List[str]] = None, num_workers: int = 8,
                 parent=None, source: str = MINIQMT,
                 bridge_dir: Optional[str] = None,
                 max_task_retries: int = DEFAULT_HISTORY_MAX_TASK_RETRIES,
                 retry_backoff: Optional[List[float]] = None):
        super().__init__(parent)
        self.manager = manager
        self.periods_config = periods_config
        # Preserve the distinction between omitted (legacy default: all
        # adjustments) and an explicit empty list (raw/unadjusted only).
        # The latter is what the GUI passes when the user clears every
        # adjustment checkbox, and it must remain consistent with the native
        # importer and scheduled-sync paths.
        self.dividend_types = (
            ['none', 'front', 'back', 'front_ratio', 'back_ratio']
            if dividend_types is None else dividend_types
        )
        self.num_workers = num_workers
        self.source = _canonical_history_source(source)
        # LocalMiniQMTImportThread intentionally never uses the native bridge;
        # retain these values for a stable constructor contract and diagnostics.
        self.bridge_dir = str(bridge_dir) if bridge_dir else None
        self.max_task_retries, self.retry_backoff = _coerce_retry_settings(
            max_task_retries, retry_backoff
        )
        self._stop_flag = False
        self.resolved_stocks = []

    def stop(self):
        self._stop_flag = True

    def _resolve_miniqmt_path(self) -> Optional[str]:
        try:
            import kh_settings as settings
            qmt_path = settings.load().get('qmt_path', '')
            if qmt_path:
                data_dir = os.path.join(qmt_path, 'datadir')
                if os.path.exists(data_dir):
                    return data_dir
        except Exception:
            return None
        return None

    def collect_all_stocks(self) -> List[str]:
        all_stocks = set()
        legacy_dirs = [
            os.path.join(os.path.dirname(os.path.dirname(__file__)), 'stock_lists'),
            os.path.join(os.path.dirname(__file__), 'stock_lists'),
        ]
        for sector_name, filename in self.PRESET_SECTORS:
            file_path = _resolve_preset_stock_pool_file(filename, legacy_dirs)
            if file_path:
                df = None
                try:
                    df = pd.read_csv(file_path, dtype=str, header=None, encoding='utf-8-sig')
                    if len(df) > 0 and len(df.columns) > 0:
                        first_cell = str(df.iloc[0, 0])
                        if '.SH' in first_cell or '.SZ' in first_cell or '.BJ' in first_cell:
                            stocks = df.iloc[:, 0].dropna().tolist()
                            all_stocks.update(stocks)
                        else:
                            df.columns = df.iloc[0]
                            df = df.iloc[1:]
                            found_col = None
                            for col in df.columns:
                                col_str = str(col) if col is not None else ''
                                if '代码' in col_str or 'code' in col_str.lower():
                                    found_col = col
                                    break
                            if found_col is not None:
                                all_stocks.update(df[found_col].dropna().tolist())
                            elif len(df.columns) > 0:
                                all_stocks.update(df.iloc[:, 0].dropna().tolist())
                except Exception as e:
                    self.log.emit(f"读取 {filename} 失败: {e}")
                finally:
                    if df is not None:
                        del df
            else:
                fetched_stocks = []
                try:
                    import khQTTools
                    if hasattr(khQTTools, 'get_stock_list_from_qmt_native') and getattr(self, "source", None) == QMT_NATIVE:
                        fetched_stocks = khQTTools.get_stock_list_from_qmt_native(sector_name)
                    if not fetched_stocks and hasattr(khQTTools, 'get_stock_list'):
                        fetched_stocks = khQTTools.get_stock_list(sector_name)
                except Exception:
                    fetched_stocks = []
                if fetched_stocks:
                    all_stocks.update(fetched_stocks)
                    self.log.emit(f"本地未找到 {filename}，已通过接口动态获取板块【{sector_name}】{len(fetched_stocks)}只股票")
                    try:
                        save_dir = legacy_dirs[0]
                        os.makedirs(save_dir, exist_ok=True)
                        save_path = os.path.join(save_dir, filename)
                        pd.DataFrame(list(fetched_stocks)).to_csv(save_path, index=False, header=False, encoding='utf-8-sig')
                    except Exception:
                        pass
                else:
                    self.log.emit(f"未找到文件: {filename}")
        return list(all_stocks)

    def _process_stock_worker(self, importer, code, periods_config, dividend_types):
        """单只股票的处理函数（在线程池工作线程中运行）"""
        result = {'success': 0, 'failed': 0, 'records': 0, 'pairs': [], 'errors': []}
        if self._stop_flag:
            return result
        for period, (start, end) in periods_config.items():
            if self._stop_flag:
                break
            try:
                period_value = str(getattr(period, "value", period)).strip().lower()
                if period_value == "tick":
                    # 本地 MiniQMT 缓存导入也不应把窗口外数据当作可
                    # 补充目标；按安全分段读取，和在线导入保持一致。
                    ranges = _tick_safe_date_ranges(start, end)
                    if not ranges:
                        result['failed'] += 1
                        result['errors'].append(
                            f"{code} tick: 日期范围无效"
                        )
                        continue
                else:
                    ranges = ((start, end),)
                for range_start, range_end in ranges:
                    r = importer.import_stock_data(
                        stock_list=[code],
                        period=period_value,
                        start_time=range_start,
                        end_time=range_end,
                        # tick 的复权在 MiniQMT/QMT 端没有语义；只传 none
                        # 可避免旧 importer 默认再请求四种复权。
                        dividend_types=([] if period_value == "tick" else dividend_types),
                        progress_callback=None,
                        defer_metadata=True,
                    )
                    result['success'] += r.get('success', 0)
                    result['failed'] += r.get('failed', 0)
                    rec = r.get('total_records', 0)
                    result['records'] += rec
                    if rec > 0:
                        result['pairs'].append((code, period_value))
            except Exception as e:
                result['failed'] += 1
                result['errors'].append(f"{code} {period}: {e}")
        return result

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import time
        _t_run_start = time.time()
        if self.source == QMT_NATIVE:
            self.error.emit(
                "原生大QMT不支持本地 MiniQMT 缓存导入，请使用在线扫描并补充"
            )
            return
        try:
            try:
                from .miniQMT_importer import MiniQMTImporter
            except ImportError:
                from duckdb_storage.miniQMT_importer import MiniQMTImporter
        except ImportError as e:
            self.error.emit(f"无法加载miniQMT导入模块: {e}")
            return

        stocks = self.collect_all_stocks()
        self.resolved_stocks = list(stocks)
        if not stocks:
            self.error.emit("未找到任何股票")
            return
        miniqmt_path = self._resolve_miniqmt_path()
        if miniqmt_path:
            self.log.emit(f"使用miniQMT数据路径: {miniqmt_path}")
        else:
            self.error.emit("未配置miniQMT数据路径，请在设置中配置后再使用本地导入")
            return
        self.log.emit(f"DuckDB数据路径: {self.manager.data_root}")
        importer = MiniQMTImporter(self.manager.data_root, miniqmt_path)

        total_success = 0
        total_failed = 0
        total_records = 0
        imported_pairs = []
        period_names = ', '.join(self.periods_config.keys())

        NUM_WORKERS = self.num_workers
        BATCH_SIZE = 100
        total = len(stocks)
        self.log.emit(f"开始本地导入: 周期[{period_names}] 共{total}只股票 ({NUM_WORKERS}线程并行)")
        start_time_all = datetime.now()
        completed_count = 0

        with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
            for batch_start in range(0, total, BATCH_SIZE):
                if self._stop_flag:
                    break
                batch_end = min(batch_start + BATCH_SIZE, total)
                batch_stocks = stocks[batch_start:batch_end]

                # 提交本批所有股票到线程池
                futures = {}
                for code in batch_stocks:
                    if self._stop_flag:
                        break
                    future = executor.submit(
                        self._process_stock_worker,
                        importer, code, self.periods_config, self.dividend_types
                    )
                    futures[future] = code

                # 收集结果（按完成顺序）
                processed_futures = set()
                for future in as_completed(futures):
                    if self._stop_flag:
                        break
                    processed_futures.add(future)
                    code = futures[future]
                    try:
                        result = future.result()
                        total_success += result['success']
                        total_failed += result['failed']
                        total_records += result['records']
                        imported_pairs.extend(result['pairs'])
                        if result['records'] == 0:
                            self.log.emit(f"无数据: {code}")
                        for err in result.get('errors', []):
                            self.log.emit(f"本地导入失败: {err}")
                    except Exception as e:
                        total_failed += 1
                        self.log.emit(f"本地导入异常: {code} - {e}")

                    completed_count += 1
                    # 每处理20只股票后，主动清理连接池（防止连接数累积）
                    if completed_count % 20 == 0:
                        try:
                            importer.manager._cleanup_idle_connections(keep_recent=10, skip_checkpoint=True)
                        except Exception:
                            pass
                    # 更新进度和ETA
                    elapsed = (datetime.now() - start_time_all).total_seconds()
                    avg_time = elapsed / completed_count if completed_count > 0 else 0
                    eta_seconds = int(avg_time * (total - completed_count))
                    self.progress.emit(completed_count, total,
                        f"本地导入 {completed_count}/{total} {code} [{period_names}]",
                        eta_seconds)

                # 如果用户取消，等待剩余future完成并收集结果（确保数据写入）
                if self._stop_flag:
                    for f in futures:
                        if f in processed_futures:
                            continue
                        code = futures[f]
                        try:
                            result = f.result(timeout=30)
                            total_success += result['success']
                            total_failed += result['failed']
                            total_records += result['records']
                            imported_pairs.extend(result['pairs'])
                        except Exception:
                            total_failed += 1
                        completed_count += 1
                    # 停止时：写元数据 + checkpoint确保数据落盘
                    try:
                        if imported_pairs:
                            importer.manager.batch_update_metadata(imported_pairs)
                            imported_pairs = []
                    except Exception:
                        pass
                    try:
                        importer.manager.checkpoint_and_close_all()
                    except Exception:
                        pass
                    break  # 退出batch循环

                # 正常批次完成：写元数据 + 释放连接（防止内存累积）
                try:
                    if imported_pairs:
                        importer.manager.batch_update_metadata(imported_pairs)
                        imported_pairs = []
                except Exception as e:
                    self.log.emit(f"⚠ 数据已写入，但元数据刷新失败: {e}。可稍后扫描修复元数据。")
                try:
                    importer.manager.close_all_no_checkpoint()
                except Exception:
                    pass

        # 导入完成：写剩余元数据 + 最终关闭
        if imported_pairs:
            try:
                importer.manager.batch_update_metadata(imported_pairs)
            except Exception as e:
                self.log.emit(f"⚠ 数据已写入，但元数据刷新失败: {e}。可稍后扫描修复元数据。")
        try:
            importer.manager.checkpoint_and_close_all()
        except Exception:
            pass

        _total_elapsed = time.time() - _t_run_start
        if completed_count > 0:
            self.log.emit(f"[耗时统计] 总耗时: {_total_elapsed:.1f}秒, 处理{completed_count}只股票, 平均每只: {_total_elapsed*1000/completed_count:.0f}ms ({NUM_WORKERS}线程)")

        if not self._stop_flag:
            self.log.emit(f"完成本地导入: 成功{total_success} 失败{total_failed} 记录数{total_records}")

        self.finished.emit({
            'success': total_success,
            'failed': total_failed,
            'total_records': total_records,
            'cancelled': self._stop_flag
        })


class CustomIncrementThread(_DuckDBLockDecisionMixin, QThread):
    """
    自定义增量数据补充线程
    与 FullIncrementThread 类似，但接收外部传入的股票列表
    """
    # 信号定义
    scan_progress = pyqtSignal(int, int, str, int)  # (当前, 总数, 消息, 已发现任务数)
    scan_log = pyqtSignal(str)  # 扫描日志
    scan_finished = pyqtSignal(int, int, dict)  # (总任务数, 总缺失天数, 详细报告字典)
    download_progress = pyqtSignal(int, int, str, int)  # (当前, 总数, 消息, 预计剩余秒数)
    download_log = pyqtSignal(str)  # 下载日志
    task_completed = pyqtSignal(dict)  # 单个任务完成
    finished = pyqtSignal(dict)  # 最终结果统计
    error = pyqtSignal(str)
    lock_conflict = pyqtSignal(dict)

    # 进度文件名
    PROGRESS_FILE = 'custom_increment_progress.json'

    def __init__(self, manager: DuckDBManager, stocks: List[str], periods_config: Dict[str, tuple],
                 completed_tasks: set = None, force_overwrite: bool = False,
                 dividend_types: Optional[List[str]] = None, num_dl_workers: int = 2,
                 parent=None, source: str = MINIQMT,
                 bridge_dir: Optional[str] = None,
                 max_task_retries: int = DEFAULT_HISTORY_MAX_TASK_RETRIES,
                 retry_backoff: Optional[List[float]] = None,
                 instance_generation: Optional[str] = None,
                 native_profile: Optional[str] = None,
                 profile: Optional[str] = None,
                 max_inflight: Optional[int] = None,
                 batch_size: Optional[int] = None,
                 span_rows: Optional[int] = None,
                 span_bytes: Optional[int] = None,
                 mode: Optional[str] = None,
                 cache_strategy: Optional[str] = None,
                 cancel_after: Optional[float] = None):
        super().__init__(parent)
        self.manager = manager
        self.stocks = stocks  # 外部传入的股票列表
        self.periods_config = periods_config  # {'1d': ('20150101', '20251221'), ...}
        self.completed_tasks = completed_tasks if completed_tasks is not None else set()
        self.force_overwrite = force_overwrite
        self.dividend_types = dividend_types
        self.num_dl_workers = num_dl_workers
        self.source = _canonical_history_source(source)
        self.bridge_dir = str(bridge_dir) if bridge_dir else None
        self.max_task_retries, self.retry_backoff = _coerce_retry_settings(
            max_task_retries, retry_backoff
        )
        self.instance_generation = _normalise_generation_aliases(instance_generation)
        self._native_execution = normalize_native_execution_options(
            {
                "native_profile": native_profile,
                "profile": profile,
                "max_inflight": max_inflight,
                "batch_size": batch_size,
                "span_rows": span_rows,
                "span_bytes": span_bytes,
                "mode": mode,
                "cache_strategy": cache_strategy,
                "cancel_after": cancel_after,
            },
            strict=False,
        )
        for _key, _value in self._native_execution.items():
            setattr(self, _key, _value)
        self._stop_flag = False
        self._cancel_timer = None
        self.workers_requested, self.workers_effective = _effective_history_workers(
            self.source, self.num_dl_workers, default=2
        )
        self.allow_gaps = _default_allow_gaps_for_source(self.source)
        self._active_importer = None
        self.tasks = []
        self.start_time = None
        self._scan_start_time = None
        self._download_start_time = None
        self._init_lock_decision()

    def stop(self):
        self._stop_flag = True
        self._wake_lock_decision_for_stop()
        importer = getattr(self, "_active_importer", None)
        if importer is not None:
            _cancel_history_runner(importer, timeout=2.0)
        timer = getattr(self, "_cancel_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _generate_trade_days_cache(self) -> Dict[str, set]:
        """预先生成所有需要的交易日缓存"""
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from khQTTools import is_trade_day
        from datetime import datetime, timedelta

        cache = {}
        sorted_cache = {}
        for period, (target_start, target_end) in self.periods_config.items():
            cache_key = f"{target_start}_{target_end}"
            if cache_key not in cache:
                start_dt = datetime.strptime(target_start, '%Y%m%d')
                end_dt = datetime.strptime(target_end, '%Y%m%d')

                all_trade_days = set()
                current = start_dt
                while current <= end_dt:
                    date_str = current.strftime('%Y%m%d')
                    if is_trade_day(date_str):
                        all_trade_days.add(date_str)
                    current += timedelta(days=1)

                cache[cache_key] = all_trade_days
                sorted_cache[cache_key] = sorted(all_trade_days)

        self._sorted_trade_days_cache = sorted_cache
        return cache

    def _group_missing_dates_fast2(self, missing_dates: List[str], sorted_existing: List[str]) -> List[tuple]:
        """快速版缺失日期分组"""
        if not missing_dates:
            return []

        if not sorted_existing:
            return [(missing_dates[0], missing_dates[-1], len(missing_dates))]

        import bisect

        groups = []
        group_start = missing_dates[0]
        group_count = 1

        for i in range(1, len(missing_dates)):
            prev_date = missing_dates[i - 1]
            curr_date = missing_dates[i]

            idx = bisect.bisect_right(sorted_existing, prev_date)
            has_existing_between = (idx < len(sorted_existing) and sorted_existing[idx] < curr_date)

            if has_existing_between:
                groups.append((group_start, prev_date, group_count))
                group_start = curr_date
                group_count = 1
            else:
                group_count += 1

        groups.append((group_start, missing_dates[-1], group_count))
        return groups

    def _generate_scan_report(self, tasks: List[dict], stocks_total: int,
                               stocks_with_db: int, stocks_without_db: int) -> dict:
        """生成详细的扫描报告"""
        report = {
            'summary': {
                'total_stocks': stocks_total,
                'stocks_with_local_data': stocks_with_db,
                'stocks_without_local_data': stocks_without_db,
                'total_tasks': len(tasks),
                'total_missing_days': sum(t['missing_days'] for t in tasks),
            },
            'by_period': {},
            'by_market': {},
            'by_stock': {},
            'complete_stocks': [],
            'incomplete_stocks': [],
        }

        stocks_in_tasks = set()
        period_stats = {}

        for task in tasks:
            period = task['period']
            stock = task['stock']
            stocks_in_tasks.add(stock)

            if period not in period_stats:
                period_stats[period] = {
                    'task_count': 0,
                    'missing_days': 0,
                    'stocks': set(),
                    'date_range': {'min': None, 'max': None}
                }

            period_stats[period]['task_count'] += 1
            period_stats[period]['missing_days'] += task['missing_days']
            period_stats[period]['stocks'].add(stock)

            if period_stats[period]['date_range']['min'] is None or task['start'] < period_stats[period]['date_range']['min']:
                period_stats[period]['date_range']['min'] = task['start']
            if period_stats[period]['date_range']['max'] is None or task['end'] > period_stats[period]['date_range']['max']:
                period_stats[period]['date_range']['max'] = task['end']

        for period, stats in period_stats.items():
            report['by_period'][period] = {
                'task_count': stats['task_count'],
                'missing_days': stats['missing_days'],
                'stock_count': len(stats['stocks']),
                'date_range': f"{stats['date_range']['min']} ~ {stats['date_range']['max']}"
            }

        # 按市场统计
        market_stats = {}
        for task in tasks:
            stock = task['stock']
            market = stock.split('.')[-1] if '.' in stock else 'Unknown'
            if market not in market_stats:
                market_stats[market] = {'task_count': 0, 'missing_days': 0, 'stocks': set()}
            market_stats[market]['task_count'] += 1
            market_stats[market]['missing_days'] += task['missing_days']
            market_stats[market]['stocks'].add(stock)

        for market, stats in market_stats.items():
            report['by_market'][market] = {
                'task_count': stats['task_count'],
                'missing_days': stats['missing_days'],
                'stock_count': len(stats['stocks'])
            }

        return report

    def _log_scan_report(self, report: dict):
        """输出扫描报告到日志"""
        self.scan_log.emit("=" * 50)
        self.scan_log.emit("扫描报告摘要:")
        summary = report['summary']
        self.scan_log.emit(f"  总股票数: {summary['total_stocks']}")
        self.scan_log.emit(f"  有本地数据: {summary['stocks_with_local_data']}")
        self.scan_log.emit(f"  无本地数据: {summary['stocks_without_local_data']}")
        self.scan_log.emit(f"  总任务数: {summary['total_tasks']}")
        self.scan_log.emit(f"  总缺失天数: {summary['total_missing_days']}")

        if report['by_period']:
            self.scan_log.emit("\n按周期统计:")
            for period, stats in report['by_period'].items():
                self.scan_log.emit(f"  {period}: {stats['task_count']}个任务, {stats['missing_days']}天, {stats['stock_count']}只股票")

        if report['by_market']:
            self.scan_log.emit("\n按市场统计:")
            for market, stats in report['by_market'].items():
                self.scan_log.emit(f"  {market}: {stats['task_count']}个任务, {stats['missing_days']}天, {stats['stock_count']}只股票")

    @staticmethod
    def _native_metadata_date(value) -> str:
        """Normalize official QMT Open/Delist dates to ``YYYYMMDD``."""
        if value is None:
            return ""
        text = str(value).strip().replace("-", "").replace("/", "")
        digits = "".join(ch for ch in text[:32] if ch.isdigit())
        if len(digits) < 8:
            return ""
        candidate = digits[:8]
        try:
            datetime.strptime(candidate, "%Y%m%d")
        except ValueError:
            return ""
        return candidate

    def _native_open_dates(self, stocks: List[str]) -> Dict[str, str]:
        """Read official QMT listing dates once for a scan, best effort."""
        if self.source != QMT_NATIVE or not stocks:
            return {}
        try:
            from kh_qmt_native_bridge.client import QmtNativeClient

            client = QmtNativeClient(
                bridge_dir=self.bridge_dir,
                expected_generation=self.instance_generation or None,
                request_timeout=5.0,
            )
            details = {}
            for offset in range(0, len(stocks), 1000):
                batch = stocks[offset:offset + 1000]
                try:
                    part = client.instrument_metadata(batch, timeout=5.0)
                except Exception as batch_exc:
                    self.scan_log.emit(
                        f"官方合约元数据分块 {offset + 1}-{offset + len(batch)} 不可用，保留该块扫描任务: {batch_exc}"
                    )
                    continue
                if isinstance(part, Mapping):
                    details.update(part)
        except Exception as exc:
            self.scan_log.emit(f"官方合约元数据不可用，保留原扫描任务: {exc}")
            return {}
        result = {}
        for code, detail in (details or {}).items():
            if not isinstance(detail, Mapping):
                continue
            open_date = self._native_metadata_date(
                detail.get("OpenDate") or detail.get("open_date")
            )
            if open_date:
                result[str(code).strip().upper()] = open_date
        self.scan_log.emit(
            f"已读取官方 QMT 合约元数据: {len(result)}/{len(stocks)} 只股票含上市日期"
        )
        return result

    def _parallel_scan(self, stocks: List[str], trade_days_cache: Dict[str, set] = None) -> tuple:
        """扫描股票缺失数据"""
        data_root = self.manager.data_root

        # 预先获取已存在的数据库文件列表
        existing_dbs = set()
        for market in ['SH', 'SZ', 'BJ']:
            market_dir = os.path.join(data_root, market)
            if os.path.exists(market_dir):
                for f in os.listdir(market_dir):
                    if f.endswith('.db'):
                        code = f[:-3]
                        existing_dbs.add(f"{code}.{market}")

        self.scan_log.emit(f"本地已有 {len(existing_dbs)} 个数据库文件")

        stocks_sorted = sorted(stocks, key=lambda x: (x.split('.')[-1] if '.' in x else 'SZ', x))
        stocks_with_db = [s for s in stocks_sorted if s in existing_dbs]
        stocks_without_db = [s for s in stocks_sorted if s not in existing_dbs]

        self.scan_log.emit(f"需扫描: {len(stocks_with_db)} 只（有本地数据）, {len(stocks_without_db)} 只（无本地数据）")

        periods = list(self.periods_config.keys())
        all_starts = [v[0] for v in self.periods_config.values()]
        all_ends = [v[1] for v in self.periods_config.values()]
        min_start = min(all_starts)
        max_end = max(all_ends)

        scan_progress_count = [0]
        total_stocks = len(stocks_sorted)

        def progress_callback(current, total, message, task_count):
            scan_progress_count[0] = current
            self.scan_progress.emit(current, total_stocks, message, task_count)

        def stop_flag_check():
            return self._stop_flag

        try:
            # 读取官方 QMT 合约元数据，用上市日期裁掉上市前的合法空段。
            # 该调用是 best-effort；失败时返回空字典，仍保留原扫描结果。
            native_open_dates = self._native_open_dates(stocks_sorted)
            result = khQTTools.check_duckdb_data_integrity(
                stock_list=stocks_sorted,
                periods=periods,
                start_date=min_start,
                end_date=max_end,
                duckdb_data_path=data_root,
                progress_callback=progress_callback,
                stop_flag=stop_flag_check,
                dividend_type=_integrity_dividend_types(self.dividend_types),
            )

            all_tasks = result.get('missing_tasks', [])

            # 过滤任务；上市日期之前的区间是官方定义的合法空段，不应
            # 提交给异步 downloader 再等待多轮空读。跨越上市日的任务只
            # 从 OpenDate 开始请求，保留真实历史数据范围。
            filtered_tasks = []
            prelisting_dropped = 0
            prelisting_clamped = 0
            for task in all_tasks:
                period = task['period']
                if period in self.periods_config:
                    period_start, period_end = self.periods_config[period]
                    task_start = task['start']
                    task_end = task['end']
                    open_date = native_open_dates.get(
                        str(task.get('stock') or '').strip().upper(), ''
                    )
                    if open_date:
                        if task_end < open_date:
                            prelisting_dropped += 1
                            continue
                        if task_start < open_date:
                            task_start = open_date
                            prelisting_clamped += 1
                    if task_start >= period_start and task_end <= period_end:
                        filtered_tasks.append({**task, 'start': task_start, 'end': task_end})
                    elif task_start <= period_end and task_end >= period_start:
                        new_start = max(task_start, period_start)
                        new_end = min(task_end, period_end)
                        if new_start <= new_end:
                            original_days = task['missing_days']
                            original_span = _calendar_day_span(task_start, task_end)
                            new_span = _calendar_day_span(new_start, new_end)
                            if original_span > 0:
                                new_missing_days = max(1, int(original_days * new_span / original_span))
                            else:
                                new_missing_days = original_days
                            filtered_tasks.append({
                                'stock': task['stock'],
                                'period': period,
                                'start': new_start,
                                'end': new_end,
                                'missing_days': new_missing_days
                            })

            all_tasks = filtered_tasks
            if prelisting_dropped or prelisting_clamped:
                self.scan_log.emit(
                    f"上市前合法空段优化: 丢弃 {prelisting_dropped} 个任务，"
                    f"裁剪 {prelisting_clamped} 个任务"
                )
            self.scan_log.emit(f"扫描完成，共发现 {len(all_tasks)} 个下载任务")

        except Exception as e:
            self.scan_log.emit(f"使用check_duckdb_data_integrity扫描失败: {e}，回退到旧方法")
            import traceback
            traceback.print_exc()
            if trade_days_cache is None or not trade_days_cache:
                self.scan_log.emit("生成交易日缓存用于回退扫描...")
                trade_days_cache = self._generate_trade_days_cache()
            all_tasks = self._parallel_scan_fallback(stocks_sorted, trade_days_cache, existing_dbs)

        scan_stats = {
            'stocks_total': len(stocks_sorted),
            'stocks_with_db': len(stocks_with_db),
            'stocks_without_db': len(stocks_without_db),
        }
        return all_tasks, scan_stats

    def _parallel_scan_fallback(self, stocks_sorted: List[str], trade_days_cache: Dict[str, set], existing_dbs: set) -> List[dict]:
        """回退扫描方法"""
        all_tasks = []
        stocks_with_db = [s for s in stocks_sorted if s in existing_dbs]
        stocks_without_db = [s for s in stocks_sorted if s not in existing_dbs]

        # 处理无本地数据的股票
        for stock in stocks_without_db:
            if self._stop_flag:
                break
            for period, (target_start, target_end) in self.periods_config.items():
                cache_key = f"{target_start}_{target_end}"
                all_trade_days = trade_days_cache.get(cache_key, set())
                period_key = str(getattr(period, "value", period)).strip().lower()
                if period_key == "tick":
                    all_trade_days = _tick_scan_retained_dates(all_trade_days)
                if not all_trade_days:
                    continue
                if period_key == "tick":
                    groups = _tick_scan_groups(
                        all_trade_days,
                        all_trade_days,
                    )
                else:
                    groups = [(target_start, target_end, len(all_trade_days))]
                for group_start, group_end, group_count in groups:
                    all_tasks.append({
                        'stock': stock,
                        'period': period,
                        'start': group_start,
                        'end': group_end,
                        'missing_days': group_count,
                    })

        self.scan_log.emit(f"无本地数据股票处理完成，已添加 {len(all_tasks)} 个任务")

        # 处理有本地数据的股票
        total = len(stocks_with_db)
        for i, stock in enumerate(stocks_with_db):
            if self._stop_flag:
                break

            if (i + 1) % 100 == 0 or i == total - 1:
                self.scan_progress.emit(i + 1, total, f"扫描 {stock}", len(all_tasks))

            try:
                existing_dates = self.manager.get_existing_dates_batch(stock, list(self.periods_config.keys()))

                for period, (target_start, target_end) in self.periods_config.items():
                    cache_key = f"{target_start}_{target_end}"
                    all_trade_days = trade_days_cache.get(cache_key, set())
                    existing = existing_dates.get(period, set())

                    period_key = str(getattr(period, "value", period)).strip().lower()
                    if period_key == "tick":
                        all_trade_days = _tick_scan_retained_dates(all_trade_days)
                        existing = _tick_scan_complete_dates(
                            self.manager,
                            stock,
                            target_start,
                            target_end,
                        )
                    else:
                        # 与主 scanner 保持同一复权完整性协议；回退时也
                        # 不能把 ratio NULL 行当成已经覆盖。
                        adjusted_existing = _fallback_kline_complete_dates(
                            self.manager,
                            stock,
                            period,
                            target_start,
                            target_end,
                            self.dividend_types,
                        )
                        if adjusted_existing is not None:
                            existing = adjusted_existing

                    missing = all_trade_days - existing
                    if missing:
                        missing_dates = sorted(missing)
                        if period_key == "tick":
                            groups = _tick_scan_groups(
                                missing_dates,
                                all_trade_days,
                            )
                        else:
                            sorted_existing = sorted(existing) if existing else []
                            groups = self._group_missing_dates_fast2(
                                missing_dates,
                                sorted_existing,
                            )

                        for group_start, group_end, group_count in groups:
                            all_tasks.append({
                                'stock': stock,
                                'period': period,
                                'start': group_start,
                                'end': group_end,
                                'missing_days': group_count
                            })
            except Exception:
                pass

        return all_tasks

    def run(self):
        from datetime import datetime, timedelta
        short_lock_enabled = False
        importer = None
        self._scan_start_time = datetime.now()
        resume_metadata_pairs = {
            pair
            for pair in (_progress_key_stock_period(key) for key in self.completed_tasks)
            if pair is not None
        }

        try:
            # ========== 新增：检查并下载 000300.SH 数据 ==========
            try:
                self.scan_log.emit("检查基准指数 000300.SH 数据...")

                # 检查是否已有 000300.SH 数据
                benchmark_code = '000300.SH'
                has_benchmark = False

                try:
                    # 查询数据库中是否有 000300.SH 的数据
                    stocks_db = self.manager.get_available_stocks()
                    has_benchmark = benchmark_code in stocks_db
                    self.scan_log.emit(f"当前数据库中有 {len(stocks_db)} 只股票")
                except Exception as e:
                    self.scan_log.emit(f"查询数据库失败: {e}")
                    import traceback
                    traceback.print_exc()

                if not has_benchmark:
                    self.scan_log.emit("未找到基准指数数据，正在下载 000300.SH 日线数据...")

                    end_date = datetime.now()
                    if self.source == QMT_NATIVE:
                        start_date = end_date - timedelta(days=3650 - 1)
                    else:
                        start_date = end_date - timedelta(days=365*20)

                    start_str = start_date.strftime("%Y%m%d")
                    end_str = end_date.strftime("%Y%m%d")

                    if self.source == QMT_NATIVE:
                        try:
                            from duckdb_storage.history_adapters import HistoryImportService
                            service = HistoryImportService("qmt_native")
                            try:
                                target_adjustments = list(self.dividend_types or ["none"])
                                if "none" not in target_adjustments:
                                    target_adjustments.append("none")
                                import_res = service.import_to_duckdb(
                                    manager=self.manager,
                                    codes=[benchmark_code],
                                    period="1d",
                                    start=start_str,
                                    end=end_str,
                                    adjustments=target_adjustments,
                                )
                                records = import_res.get("saved", 0)
                                self.scan_log.emit(f"已成功通过大 QMT 原生桥下载并保存 000300.SH 数据，共 {records} 条记录")
                            finally:
                                service.close()
                        except Exception as native_err:
                            self.scan_log.emit(f"原生桥下载 000300.SH 失败: {native_err}")
                        raise _SkipQmtBenchmark()

                    # 尝试导入xtquant
                    from xtquant import xtdata

                    # 下载数据
                    xtdata.download_history_data(
                        benchmark_code,
                        period='1d',
                        start_time=start_str,
                        end_time=end_str,
                        incrementally=True
                    )

                    # 获取不复权数据
                    data_none = xtdata.get_local_data(
                        field_list=[],
                        stock_list=[benchmark_code],
                        period='1d',
                        start_time=start_str,
                        end_time=end_str,
                        dividend_type='none',
                        fill_data=True
                    )

                    if benchmark_code in data_none and data_none[benchmark_code] is not None and len(data_none[benchmark_code]) > 0:
                        df = data_none[benchmark_code].copy()

                        dividend_types = self.dividend_types
                        if dividend_types is None:
                            dividend_types = ['front', 'back', 'front_ratio', 'back_ratio']

                        for div_type in dividend_types:
                            try:
                                data_adj = xtdata.get_local_data(
                                    field_list=['time', 'open', 'high', 'low', 'close'],
                                    stock_list=[benchmark_code],
                                    period='1d',
                                    start_time=start_str,
                                    end_time=end_str,
                                    dividend_type=div_type,
                                    fill_data=True
                                )

                                if benchmark_code in data_adj and data_adj[benchmark_code] is not None:
                                    df_adj = data_adj[benchmark_code]

                                    # 重命名复权字段
                                    suffix = div_type
                                    rename_map = {
                                        'open': f'open_{suffix}',
                                        'high': f'high_{suffix}',
                                        'low': f'low_{suffix}',
                                        'close': f'close_{suffix}'
                                    }
                                    df_adj = df_adj.rename(columns=rename_map)
                                    df_adj = df_adj[list(rename_map.values())]

                                    # 合并到主 DataFrame
                                    df = df.merge(df_adj, left_index=True, right_index=True, how='left')

                                    del data_adj, df_adj
                            except Exception as e:
                                self.scan_log.emit(f"获取 000300.SH {div_type} 复权数据失败: {e}")

                        # 保存到DuckDB
                        records = self.manager.save_kline_data(df, benchmark_code, '1d', 'none')
                        self.scan_log.emit(f"已成功下载并保存 000300.SH 数据，共 {records} 条记录")

                        del df, data_none
                    else:
                        self.scan_log.emit("警告：000300.SH 数据下载失败，请手动补充")
                else:
                    self.scan_log.emit("基准指数 000300.SH 数据已存在")

            except _SkipQmtBenchmark:
                self.scan_log.emit("大QMT原生桥模式：跳过 MiniQMT/xtquant 基准指数检查")
            except Exception as e:
                self.scan_log.emit(f"检查/下载 000300.SH 数据时出错: {e}")
                import traceback
                traceback.print_exc()
            # ========== 基准指数检查结束 ==========

            # ===== 第一阶段：扫描 =====
            self.scan_log.emit("开始扫描缺失数据...")
            self.scan_log.emit(f"共 {len(self.stocks)} 只股票")
            self.scan_log.emit(f"选中周期: {', '.join(self.periods_config.keys())}")
            self.scan_log.emit("=" * 50)

            if self.force_overwrite:
                self.scan_log.emit("已启用强制覆写，跳过增量扫描，直接生成全量任务")
                data_root = self.manager.data_root
                existing_dbs = set()
                for market in ['SH', 'SZ', 'BJ']:
                    market_dir = os.path.join(data_root, market)
                    if os.path.exists(market_dir):
                        for f in os.listdir(market_dir):
                            if f.endswith('.db'):
                                code = f[:-3]
                                existing_dbs.add(f"{code}.{market}")

                stocks_sorted = sorted(self.stocks, key=lambda x: (x.split('.')[-1] if '.' in x else 'SZ', x))
                stocks_with_db = [s for s in stocks_sorted if s in existing_dbs]
                stocks_without_db = [s for s in stocks_sorted if s not in existing_dbs]

                trade_days_cache = self._generate_trade_days_cache()
                self.tasks = []
                for stock in stocks_sorted:
                    for period, (target_start, target_end) in self.periods_config.items():
                        cache_key = f"{target_start}_{target_end}"
                        all_trade_days = trade_days_cache.get(cache_key, set())
                        # 强制覆写也必须遵守 tick 的保留窗口与单次跨度；
                        # 对 K 线维持历史单区间任务。
                        for group_start, group_end, group_count in _force_task_groups(
                            period, target_start, target_end, all_trade_days
                        ):
                            self.tasks.append({
                                'stock': stock,
                                'period': getattr(period, "value", period),
                                'start': group_start,
                                'end': group_end,
                                'missing_days': group_count,
                            })

                scan_stats = {
                    'stocks_total': len(stocks_sorted),
                    'stocks_with_db': len(stocks_with_db),
                    'stocks_without_db': len(stocks_without_db),
                }
            else:
                self.tasks, scan_stats = self._parallel_scan(self.stocks, {})

            for _task in self.tasks:
                _task.setdefault('source', self.source)

            if self._stop_flag:
                self.scan_log.emit("扫描已取消")
                self.finished.emit({
                    'success': 0, 'failed': 0, 'skipped': 0,
                    'total_records': 0, 'cancelled': True,
                })
                return

            # 生成详细报告
            report = self._generate_scan_report(
                self.tasks,
                scan_stats['stocks_total'],
                scan_stats['stocks_with_db'],
                scan_stats['stocks_without_db']
            )

            total_missing = sum(t['missing_days'] for t in self.tasks)
            self.scan_log.emit("=" * 50)
            self.scan_log.emit(f"扫描完成: 发现 {len(self.tasks)} 个下载任务, 共缺失 {total_missing} 天数据")

            self._log_scan_report(report)
            self.scan_finished.emit(len(self.tasks), total_missing, report)

            if not self.tasks:
                self.scan_log.emit("所有数据已完整，无需补充")
                self._refresh_download_metadata(resume_metadata_pairs)
                self.finished.emit({
                    'success': 0, 'failed': 0, 'skipped': 0,
                    'total_records': 0, 'cancelled': False,
                })
                return

            # ===== 第二阶段：下载（流水线并行模式）=====
            self.download_log.emit("=" * 50)
            self.download_log.emit(f"开始下载 {len(self.tasks)} 个任务...")
            self.start_time = datetime.now()
            self._download_start_time = self.start_time

            results = {
                'success': 0, 'failed': 0, 'skipped': 0,
                'total_records': 0, 'cancelled': False,
            }
            imported_records = []
            try:
                if hasattr(self.manager, 'enable_short_lock_write'):
                    self.manager.enable_short_lock_write()
                    short_lock_enabled = True
            except Exception as e:
                self.download_log.emit(f"启用短锁写入模式失败，将使用普通写入模式: {e}")

            # 构建待下载列表（跳过已完成）
            pending_tasks = []
            for task in self.tasks:
                task_key = _increment_task_key(
                    task, _task_dividend_types(task.get('period'), self.dividend_types),
                    self.force_overwrite, self.source
                )
                if task_key in self.completed_tasks:
                    self.completed_tasks.discard(task_key)
                    self.download_log.emit(
                        f"  断点校验失效，重新补充: {task['stock']} {task['period']} "
                        f"{task['start']}~{task['end']}"
                    )
                pending_tasks.append(task)

            total_pending = len(pending_tasks)
            if total_pending == 0:
                self.download_log.emit("所有任务已完成，无需下载")
                self._refresh_download_metadata(resume_metadata_pairs)
                self.finished.emit(results)
                return

            pending_tasks.sort(
                key=lambda item: _history_task_sort_key(item, self.source)
            )
            remaining_by_stock = {}
            for pending in pending_tasks:
                stock = pending.get('stock', '')
                remaining_by_stock[stock] = remaining_by_stock.get(stock, 0) + 1

            def release_stock_when_done(task):
                if not task:
                    return
                stock = task.get('stock', '')
                remaining_by_stock[stock] = max(0, remaining_by_stock.get(stock, 1) - 1)
                if remaining_by_stock[stock] == 0:
                    try:
                        self.manager.close_stock_connection(stock, skip_checkpoint=True)
                    except Exception:
                        pass

            NUM_DL_WORKERS = self.workers_effective
            RESTART_INTERVAL = (
                1_000_000_000 if self.source == QMT_NATIVE else 300
            )
            importer = MultiProcessImporter(
                num_workers=NUM_DL_WORKERS,
                timeout_per_task=120.0,
                max_task_retries=self.max_task_retries,
                retry_backoff=self.retry_backoff,
                source=self.source,
                bridge_dir=self.bridge_dir,
**_native_options_for_source(self.source, _native_options_from_owner(self)),
            )
            self._active_importer = importer
            _configure_history_importer_defaults(
                importer,
                self.source,
                force=self.force_overwrite,
                allow_gaps=self.allow_gaps,
                local_only=False,
                idempotent=True,
                incrementally=not (
                    self.source == QMT_NATIVE
                    and getattr(self, "mode", None) == "historical-backfill"
                ),
                instance_generation=self.instance_generation,
**_native_options_for_source(self.source, _native_options_from_owner(self)),
                cancel_after=getattr(self, "cancel_after", None),
            )
            importer.start()
            if self.source == QMT_NATIVE and self.cancel_after is not None:
                import threading

                def _deadline_cancel_custom():
                    self._stop_flag = True
                    _cancel_history_runner(self._active_importer, timeout=2.0)

                self._cancel_timer = threading.Timer(
                    float(self.cancel_after), _deadline_cancel_custom
                )
                self._cancel_timer.daemon = True
                self._cancel_timer.start()

            native_progress_stats = _new_native_progress_stats()

            def report_source_events():
                for event in importer.get_all_progress():
                    if self.source == QMT_NATIVE:
                        _consume_native_progress_stats(native_progress_stats, event)
                    if event.get('type') == 'retry':
                        self.download_log.emit(
                            f"  ↻ {event.get('stock')} {event.get('period')} 连接异常，"
                            f"{event.get('delay', 0):g}秒后进行第{event.get('attempt')}次重试"
                        )
                    elif event.get('type') == 'fatal':
                        self.download_log.emit(
                            f"  数据源工作进程异常: {event.get('msg', '未知错误')}"
                        )
                    elif event.get('type') == 'native_phase':
                        self.download_log.emit(
                            f"  原生桥 {event.get('stock', '')} "
                            f"{event.get('period', '')}: {event.get('phase', '处理中')}"
                        )
                    elif event.get('type') == 'native_generation':
                        self.download_log.emit(
                            (
                                "  原生桥已重启，自动切换到新实例并继续下载"
                                if event.get('generation_changed') else
                                "  原生桥代次已同步，继续下载"
                            )
                        )
                    elif event.get('type') == 'executor_started':
                        self.download_log.emit(
                            f"  实际下载并发: {event.get('workers_effective', NUM_DL_WORKERS)}"
                        )

            next_submit = 0
            completed_count = 0
            tasks_since_restart = 0
            id_to_task = {}  # task_id -> task dict

            def emit_download_progress(current_stock="", current_period=""):
                """统一发出任务完成进度（主循环与重启排空路径共用）。"""
                eta_seconds = 0
                if completed_count > 0:
                    elapsed = (datetime.now() - self.start_time).total_seconds()
                    eta_seconds = max(
                        0, int(elapsed * (total_pending - completed_count) / completed_count)
                    )
                progress_message = (
                    f"下载 {current_stock} {current_period} "
                    f"({completed_count}/{total_pending})"
                )
                if self.source == QMT_NATIVE:
                    progress_message += " | " + _format_native_progress_summary(
                        native_progress_stats, eta_seconds
                    )
                    scan_elapsed = 0.0
                    if getattr(self, "_scan_start_time", None) is not None:
                        scan_elapsed = max(
                            0.0,
                            (self.start_time - self._scan_start_time).total_seconds(),
                        )
                    progress_message += " | scan=%.1fs download=%.1fs" % (
                        scan_elapsed,
                        max(0.0, (datetime.now() - self.start_time).total_seconds()),
                    )
                self.download_progress.emit(
                    completed_count,
                    total_pending,
                    progress_message,
                    eta_seconds,
                )

            if self.source == QMT_NATIVE:
                self.download_log.emit(
                    "原生大QMT桥：workers_requested=%d, workers_effective=%d, "
                    "bridge_inflight=1" % (
                        self.workers_requested,
                        NUM_DL_WORKERS,
                    )
                )
            else:
                self.download_log.emit(f"使用 {NUM_DL_WORKERS} 个下载进程并行下载")

            # See FullIncrementThread: native execution is serial, while the
            # bounded queue look-ahead is what enables a multi-code bundle.
            prefetch_limit = _history_prefetch_limit(
                self.source, total_pending, NUM_DL_WORKERS
            )
            while next_submit < prefetch_limit and not self._stop_flag:
                t = pending_tasks[next_submit]
                importer.add_task(
                    t['stock'], t['period'], t['start'], t['end'],
                    _task_dividend_types(t.get('period'), self.dividend_types),
                )
                id_to_task[importer._task_id] = t
                next_submit += 1

            # 主循环：收到结果→处理→提交下一个
            while completed_count < next_submit and not self._stop_flag:
                result = importer.get_result(timeout=0.5)
                report_source_events()
                if not result:
                    continue

                tid = result.get('task_id')
                orig_task = id_to_task.pop(tid, None)
                r_stock = result.get('stock', '')
                r_period = result.get('period', '')
                if self.source == QMT_NATIVE:
                    _consume_native_progress_stats(
                        native_progress_stats,
                        {
                            "type": "result",
                            "task_id": tid,
                            "native_job_ids": result.get("native_job_ids"),
                            "native_job_details": result.get("native_job_details"),
                            "records": result.get("records", 0),
                            "transport_bytes": result.get("transport_bytes", 0),
                        },
                    )

                df = None
                df_dict = None
                task_saved = False
                try:
                    native_result_safe = (
                        self.source != QMT_NATIVE
                        or _native_worker_result_write_safe(
                            result,
                            allow_gaps=bool(getattr(self, "allow_gaps", False)),
                        )
                    )
                    if result.get('success') and native_result_safe:
                        df_dict = result.get('df_dict')
                        if _is_native_legal_empty(result, self.source):
                            task_saved = True
                            results['skipped'] = results.get('skipped', 0) + 1
                            self.download_log.emit(f"  跳过: {r_stock} {r_period} (历史停牌/无交易，无需补充)")
                            if self.source == QMT_NATIVE:
                                _finalize_native_worker_result(
                                    result,
                                    frame=None,
                                    runner=importer,
                                    committed=True,
                                    status_callback=self.download_log.emit,
                                )
                        elif df_dict:
                            df = dict_to_dataframe(df_dict)
                            if df is not None and len(df) > 0:
                                save_status, saved = self._save_download_frame(
                                    df, r_stock, r_period, results
                                )
                                if self.source == QMT_NATIVE and save_status == "saved":
                                    _finalize_native_worker_result(
                                        result,
                                        frame=df,
                                        runner=importer,
                                        committed=bool(saved is not None and saved > 0),
                                        status_callback=self.download_log.emit,
                                    )
                                if save_status == "saved" and saved and saved > 0:
                                    imported_records.append((r_stock, r_period, int(saved)))
                                    task_saved = True
                                    results['success'] += 1
                                    results['total_records'] += int(saved)
                                    self.download_log.emit(f"  成功: {r_stock} {r_period} ({saved}条)")
                                elif save_status == "saved":
                                    results['failed'] += 1
                                    self.download_log.emit(f"  未保存: {r_stock} {r_period}")
                            else:
                                results['failed'] += 1
                                self.download_log.emit(
                                    f"  空数据: {r_stock} {r_period}；请确认所选区间覆盖该证券的交易历史，"
                                    "退市证券需选择退市前日期"
                                )
                                if self.source == QMT_NATIVE:
                                    _finalize_native_worker_result(
                                        result,
                                        frame=df,
                                        runner=importer,
                                        committed=False,
                                        status_callback=self.download_log.emit,
                                    )
                        else:
                            results['failed'] += 1
                            self.download_log.emit(
                                f"  无数据: {r_stock} {r_period}；请确认所选区间覆盖该证券的交易历史，"
                                "退市证券需选择退市前日期"
                            )
                            if self.source == QMT_NATIVE:
                                _finalize_native_worker_result(
                                    result,
                                    committed=False,
                                    runner=importer,
                                    status_callback=self.download_log.emit,
                                )
                    elif result.get('success'):
                        results['failed'] += 1
                        self.download_log.emit(
                            f"  失败: {r_stock} {r_period} - "
                            "原生桥 bundle 未完成或含缺口，拒绝写入"
                        )
                        _finalize_native_worker_result(
                            result,
                            runner=importer,
                            committed=False,
                            status_callback=self.download_log.emit,
                        )
                    else:
                        results['failed'] += 1
                        error_msg = result.get('error', '未知错误')
                        self.download_log.emit(f"  失败: {r_stock} {r_period} - {error_msg}")

                    if task_saved and orig_task:
                        tk = _increment_task_key(
                            orig_task,
                            _task_dividend_types(orig_task.get('period'), self.dividend_types),
                            self.force_overwrite, self.source
                        )
                        self.completed_tasks.add(tk)
                        completed_task = dict(orig_task)
                        completed_task['_progress_key'] = tk
                        self.task_completed.emit(completed_task)
                finally:
                    if df is not None:
                        del df
                    if df_dict is not None:
                        del df_dict
                    release_stock_when_done(orig_task)

                completed_count += 1
                tasks_since_restart += 1

                # 提交下一个任务保持管道满
                if next_submit < total_pending and not self._stop_flag:
                    t = pending_tasks[next_submit]
                    importer.add_task(
                        t['stock'], t['period'], t['start'], t['end'],
                        _task_dividend_types(t.get('period'), self.dividend_types),
                    )
                    id_to_task[importer._task_id] = t
                    next_submit += 1

                # 更新进度和 ETA
                emit_download_progress(r_stock, r_period)

                if completed_count % 20 == 0:
                    self.manager._cleanup_idle_connections(
                        keep_recent=max(8, NUM_DL_WORKERS * 2),
                        skip_checkpoint=True,
                    )
                    import gc
                    gc.collect()

                # 工作进程重启：先排空管道
                if tasks_since_restart >= RESTART_INTERVAL and not self._stop_flag:
                    self.download_log.emit(f"  [内存优化] 已处理 {tasks_since_restart} 个任务，重启工作进程...")
                    while completed_count < next_submit and not self._stop_flag:
                        r2 = importer.get_result(timeout=1.0)
                        report_source_events()
                        if r2:
                            tid2 = r2.get('task_id')
                            ot2 = id_to_task.pop(tid2, None)
                            if self.source == QMT_NATIVE:
                                _consume_native_progress_stats(
                                    native_progress_stats,
                                    {
                                        "type": "result",
                                        "task_id": tid2,
                                        "native_job_ids": r2.get("native_job_ids"),
                                        "records": r2.get("records", 0),
                                        "transport_bytes": r2.get("transport_bytes", 0),
                                    },
                                )
                            df2, dd2 = None, None
                            task2_saved = False
                            try:
                                native_r2_safe = (
                                    self.source != QMT_NATIVE
                                    or _native_worker_result_write_safe(
                                        r2,
                                        allow_gaps=bool(getattr(self, "allow_gaps", False)),
                                    )
                                )
                                if r2.get('success') and native_r2_safe:
                                    rs, rp = r2.get('stock',''), r2.get('period','')
                                    if _is_native_legal_empty(r2, self.source):
                                        task2_saved = True
                                        results['skipped'] = results.get('skipped', 0) + 1
                                        self.download_log.emit(f"  跳过: {rs} {rp} (历史停牌/无交易，无需补充)")
                                        if self.source == QMT_NATIVE:
                                            _finalize_native_worker_result(
                                                r2,
                                                frame=None,
                                                runner=importer,
                                                committed=True,
                                                status_callback=self.download_log.emit,
                                            )
                                    else:
                                        dd2 = r2.get('df_dict')
                                        if dd2:
                                            df2 = dict_to_dataframe(dd2)
                                            if df2 is not None and len(df2) > 0:
                                                save_status2, saved2 = self._save_download_frame(
                                                    df2, rs, rp, results
                                                )
                                                if self.source == QMT_NATIVE and save_status2 == "saved":
                                                    _finalize_native_worker_result(
                                                        r2,
                                                        frame=df2,
                                                        runner=importer,
                                                        committed=bool(saved2 is not None and saved2 > 0),
                                                        status_callback=self.download_log.emit,
                                                    )
                                                if save_status2 == "saved" and saved2 and saved2 > 0:
                                                    imported_records.append((rs, rp, int(saved2)))
                                                    task2_saved = True
                                                    results['success'] += 1; results['total_records'] += int(saved2)
                                                    self.download_log.emit(f"  成功: {rs} {rp} ({saved2}条)")
                                                elif save_status2 == "saved": results['failed'] += 1
                                            else:
                                                results['failed'] += 1
                                                if self.source == QMT_NATIVE:
                                                    _finalize_native_worker_result(
                                                        r2,
                                                        frame=df2,
                                                        runner=importer,
                                                        committed=False,
                                                        status_callback=self.download_log.emit,
                                                    )
                                        else:
                                            results['failed'] += 1
                                            if self.source == QMT_NATIVE:
                                                _finalize_native_worker_result(
                                                    r2,
                                                    runner=importer,
                                                    committed=False,
                                                    status_callback=self.download_log.emit,
                                                )
                                elif r2.get('success'):
                                    results['failed'] += 1
                                    self.download_log.emit(
                                        f"  失败: {r2.get('stock', '')} {r2.get('period', '')} - "
                                        "原生桥 bundle 未完成或含缺口，拒绝写入"
                                    )
                                    _finalize_native_worker_result(
                                        r2,
                                        runner=importer,
                                        committed=False,
                                        status_callback=self.download_log.emit,
                                    )
                                else: results['failed'] += 1
                                if task2_saved and ot2:
                                    tk2 = _increment_task_key(
                                        ot2,
                                        _task_dividend_types(ot2.get('period'), self.dividend_types),
                                        self.force_overwrite, self.source
                                    )
                                    self.completed_tasks.add(tk2)
                                    completed_task2 = dict(ot2)
                                    completed_task2['_progress_key'] = tk2
                                    self.task_completed.emit(completed_task2)
                            finally:
                                del df2, dd2
                                release_stock_when_done(ot2)
                            completed_count += 1
                            # 重启前排空的结果也属于已完成任务；立即
                            # 通知 GUI，避免进度条在排空期间停留旧值。
                            emit_download_progress(r2.get('stock', ''), r2.get('period', ''))
                    importer.stop()
                    import gc; gc.collect()
                    importer = MultiProcessImporter(
                        num_workers=NUM_DL_WORKERS,
                        timeout_per_task=120.0,
                        max_task_retries=self.max_task_retries,
                        retry_backoff=self.retry_backoff,
                        source=self.source,
                        bridge_dir=self.bridge_dir,
**_native_options_for_source(self.source, _native_options_from_owner(self)),
                    )
                    self._active_importer = importer
                    _configure_history_importer_defaults(
                        importer,
                        self.source,
                        force=self.force_overwrite,
                        allow_gaps=self.allow_gaps,
                        local_only=False,
                        idempotent=True,
                        incrementally=not (
                            self.source == QMT_NATIVE
                            and getattr(self, "mode", None) == "historical-backfill"
                        ),
                        instance_generation=self.instance_generation,
**_native_options_for_source(self.source, _native_options_from_owner(self)),
                        cancel_after=getattr(self, "cancel_after", None),
                    )
                    importer.start()
                    tasks_since_restart = 0
                    id_to_task.clear()
                    _refill_limit = min(next_submit + NUM_DL_WORKERS, total_pending)
                    while next_submit < _refill_limit and not self._stop_flag:
                        t = pending_tasks[next_submit]
                        importer.add_task(
                            t['stock'], t['period'], t['start'], t['end'],
                            _task_dividend_types(t.get('period'), self.dividend_types),
                        )
                        id_to_task[importer._task_id] = t
                        next_submit += 1
                    self.download_log.emit(f"  [内存优化] 工作进程已重启")

            importer.stop()
            self.manager.cleanup_connections_aggressive(skip_checkpoint=True)
            self._refresh_download_metadata(
                list(imported_records) + list(resume_metadata_pairs)
            )
            results['cancelled'] = self._stop_flag
            self.finished.emit(results)

        except Exception as e:
            import traceback
            self.error.emit(f"执行异常: {e}\n{traceback.format_exc()}")
        finally:
            if importer is not None:
                try:
                    if getattr(importer, 'is_running', False):
                        _cancel_history_runner(importer, timeout=2.0)
                except Exception:
                    pass
            timer = getattr(self, "_cancel_timer", None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
                self._cancel_timer = None
            self._active_importer = None
            if short_lock_enabled and hasattr(self.manager, 'disable_short_lock_write'):
                self.manager.disable_short_lock_write()


class TushareImportDialog(QDialog):
    """
    Tushare 数据导入对话框

    单页 UI，参照 MiniQMTImportDialog 的"自定义补充数据"风格。
    使用乘法前复权（adj_factor 方式）。
    Token 和代理配置从 QSettings 读取，在【软件设置 → Tushare设置】中设置。
    """

    def __init__(self, manager: DuckDBManager, parent=None):
        super().__init__(parent)
        self.manager      = manager
        self.import_thread = None
        self._close_after_stop = False
        self.font_scale    = get_ui_font_scale()
        self._base_style_raw = None
        self._import_start_time = None

        self.setWindowTitle("从Tushare导入数据")
        self.setMinimumSize(820, 760)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowFlags(self.windowFlags() | Qt.Window)
        self._set_dark_titlebar()

        self._base_style_raw = """
            QDialog, QWidget {
                background-color: #333333;
                color: #e8e8e8;
                font-family: "Microsoft YaHei UI";
            }
            QLabel { color: #e8e8e8; }
            QLineEdit, QTextEdit {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px;
            }
            QPushButton {
                background-color: #0078d4;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-family: "Microsoft YaHei UI";
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover { background-color: #106ebe; }
            QPushButton:pressed { background-color: #005a9e; }
            QPushButton:disabled {
                background-color: #555555;
                color: #888888;
            }
            QCheckBox { color: #e8e8e8; }
            QCheckBox::indicator {
                width: 18px; height: 18px;
                border: 1px solid #555555;
                border-radius: 3px;
                background-color: #3c3c3c;
            }
            QCheckBox::indicator:checked {
                background-color: #0078d4;
                border-color: #0078d4;
            }
            QRadioButton { color: #e8e8e8; }
            QRadioButton::indicator {
                width: 18px; height: 18px;
                border: 1px solid #555555;
                border-radius: 9px;
                background-color: #3c3c3c;
            }
            QRadioButton::indicator:checked {
                background-color: #0078d4;
                border-color: #0078d4;
            }
            QGroupBox {
                border: 1px solid #555555;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
                color: #e8e8e8;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QProgressBar {
                background-color: #3c3c3c;
                border: 1px solid #555555;
                border-radius: 3px;
                text-align: center;
                color: #e8e8e8;
            }
            QProgressBar::chunk { background-color: #0078d4; }
            QSpinBox, QDateEdit {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px;
            }
            QFrame { background-color: #333333; color: #e8e8e8; }
        """
        _ui_font = get_preferred_ui_font_family() or "Microsoft YaHei UI"
        if _ui_font != "Microsoft YaHei UI":
            self._base_style_raw = self._base_style_raw.replace('"Microsoft YaHei UI"', f'"{_ui_font}"')
        self.setStyleSheet(self._scale_stylesheet(self._base_style_raw, self.font_scale))
        self._init_ui()
        self.apply_ui_scale(self.font_scale)

    # ------------------------------------------------------------------
    # 暗色标题栏 / 字号缩放（与其他对话框保持一致）
    # ------------------------------------------------------------------

    def _set_dark_titlebar(self):
        try:
            import platform
            if platform.system() == "Windows":
                from ctypes import windll, c_int, byref, sizeof
                from ctypes.wintypes import DWORD
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                DWMWA_CAPTION_COLOR = 35
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()), DWMWA_USE_IMMERSIVE_DARK_MODE,
                    byref(c_int(2)), sizeof(c_int)
                )
                caption_color = DWORD(0x333333)
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()), DWMWA_CAPTION_COLOR,
                    byref(caption_color), sizeof(caption_color)
                )
        except Exception:
            pass

    def _scale_stylesheet(self, style: str, scale: float) -> str:
        if not style:
            return style
        import re
        def repl(m):
            scaled = max(6, int(round(float(m.group(1)) * float(scale))))
            return f"font-size: {scaled}{m.group(2)}"
        return re.sub(
            r"font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(px|pt)",
            repl, style, flags=re.IGNORECASE
        )

    def apply_ui_scale(self, scale=None):
        if scale is None:
            scale = get_ui_font_scale()
        self.font_scale = scale
        if self._base_style_raw:
            self.setStyleSheet(self._scale_stylesheet(self._base_style_raw, self.font_scale))
        try:
            for child in self.findChildren(QWidget):
                base_ss = child.property("ui_base_stylesheet")
                if base_ss:
                    child.setStyleSheet(self._scale_stylesheet(base_ss, self.font_scale))
                base_pt = child.property("ui_base_font_pt")
                if base_pt:
                    font = child.font()
                    font.setPointSize(max(6, int(round(float(base_pt) * self.font_scale))))
                    child.setFont(font)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _init_ui(self):
        layout = QVBoxLayout(self)

        # ===== 股票池选择 =====
        stock_group = QGroupBox("股票池选择")
        stock_layout = QVBoxLayout(stock_group)

        method_layout = QHBoxLayout()
        self.method_group = QButtonGroup(self)
        self.preset_radio = QRadioButton("预设板块")
        self.preset_radio.setChecked(True)
        self.method_group.addButton(self.preset_radio)
        method_layout.addWidget(self.preset_radio)

        self.file_radio = QRadioButton("从文件导入")
        self.method_group.addButton(self.file_radio)
        method_layout.addWidget(self.file_radio)

        self.manual_radio = QRadioButton("手动输入")
        self.method_group.addButton(self.manual_radio)
        method_layout.addWidget(self.manual_radio)
        method_layout.addStretch()
        stock_layout.addLayout(method_layout)

        # 预设板块
        self.preset_frame = QFrame()
        preset_grid = QGridLayout(self.preset_frame)
        preset_grid.setContentsMargins(0, 0, 0, 0)
        self.preset_checks = {}
        presets = [
            ('沪深A股',   'all_a'),
            ('上证A股',   'sh_a'),
            ('深证A股',   'sz_a'),
            ('沪深300',   'hs300'),
            ('上证50',    'sz50'),
            ('中证500',   'zz500'),
            ('创业板',    'cyb'),
            ('科创板',    'kcb'),
            ('沪深ETF',   'hs_etf'),
            ('沪深场内基金（含ETF/LOF）', 'hs_fund'),
            ('沪深转债',  'hs_convertible_bonds'),
            ('T0型ETF',   't0_etf'),
            ('常用指数',  'common_index'),
        ]
        for i, (name, key) in enumerate(presets):
            cb = QCheckBox(name)
            self.preset_checks[key] = cb
            preset_grid.addWidget(cb, i // 4, i % 4)
        stock_layout.addWidget(self.preset_frame)

        # 文件导入
        self.file_frame = QFrame()
        file_layout = QHBoxLayout(self.file_frame)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_path_edit = QLineEdit()
        self.file_path_edit.setPlaceholderText("选择股票列表CSV文件...")
        file_layout.addWidget(self.file_path_edit)
        self.browse_file_btn = QPushButton("浏览...")
        self.browse_file_btn.clicked.connect(self.browse_stock_file)
        file_layout.addWidget(self.browse_file_btn)
        self.file_frame.setVisible(False)
        stock_layout.addWidget(self.file_frame)

        # 手动输入
        self.manual_frame = QFrame()
        manual_layout = QVBoxLayout(self.manual_frame)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.addWidget(QLabel("输入股票代码（每行一个，支持纯数字或带市场后缀）:"))
        self.manual_edit = QTextEdit()
        self.manual_edit.setMaximumHeight(100)
        self.manual_edit.setPlaceholderText("000001.SZ 或 000001\n600000.SH 或 600000\n300750")
        manual_layout.addWidget(self.manual_edit)
        self.manual_frame.setVisible(False)
        stock_layout.addWidget(self.manual_frame)

        self.preset_radio.toggled.connect(self._on_method_changed)
        self.file_radio.toggled.connect(self._on_method_changed)
        self.manual_radio.toggled.connect(self._on_method_changed)
        layout.addWidget(stock_group)

        # ===== 数据周期设置 =====
        period_group = QGroupBox("数据周期设置")
        period_layout = QGridLayout(period_group)
        today = QDate.currentDate()

        self.period_1d_check = QCheckBox("日线 (1d)")
        self.period_1d_check.setChecked(True)
        period_layout.addWidget(self.period_1d_check, 0, 0)
        period_layout.addWidget(QLabel("开始:"), 0, 1)
        self.period_1d_start = QDateEdit()
        self.period_1d_start.setCalendarPopup(True)
        self.period_1d_start.setDate(today.addYears(-10))
        period_layout.addWidget(self.period_1d_start, 0, 2)
        period_layout.addWidget(QLabel("结束:"), 0, 3)
        self.period_1d_end = QDateEdit()
        self.period_1d_end.setCalendarPopup(True)
        self.period_1d_end.setDate(today)
        period_layout.addWidget(self.period_1d_end, 0, 4)

        self.period_1m_check = QCheckBox("1分钟 (1m)")
        self.period_1m_check.setChecked(False)
        self.period_1m_check.setToolTip(
            "分钟行情请求量较大，且 Tushare 指数接口不支持分钟线；需要时再勾选。"
        )
        period_layout.addWidget(self.period_1m_check, 1, 0)
        period_layout.addWidget(QLabel("开始:"), 1, 1)
        self.period_1m_start = QDateEdit()
        self.period_1m_start.setCalendarPopup(True)
        self.period_1m_start.setDate(today.addYears(-1))
        period_layout.addWidget(self.period_1m_start, 1, 2)
        period_layout.addWidget(QLabel("结束:"), 1, 3)
        self.period_1m_end = QDateEdit()
        self.period_1m_end.setCalendarPopup(True)
        self.period_1m_end.setDate(today)
        period_layout.addWidget(self.period_1m_end, 1, 4)

        self.period_5m_check = QCheckBox("5分钟 (5m)")
        self.period_5m_check.setChecked(False)
        self.period_5m_check.setToolTip(
            "分钟行情请求量较大，且 Tushare 指数接口不支持分钟线；需要时再勾选。"
        )
        period_layout.addWidget(self.period_5m_check, 2, 0)
        period_layout.addWidget(QLabel("开始:"), 2, 1)
        self.period_5m_start = QDateEdit()
        self.period_5m_start.setCalendarPopup(True)
        self.period_5m_start.setDate(today.addYears(-1))
        period_layout.addWidget(self.period_5m_start, 2, 2)
        period_layout.addWidget(QLabel("结束:"), 2, 3)
        self.period_5m_end = QDateEdit()
        self.period_5m_end.setCalendarPopup(True)
        self.period_5m_end.setDate(today)
        period_layout.addWidget(self.period_5m_end, 2, 4)

        layout.addWidget(period_group)

        # ===== 复权方式 =====
        adj_group = QGroupBox(
            "价格字段（原始价为基础必存；复权价需 adj_factor 接口约2000积分）"
        )
        adj_layout = QHBoxLayout(adj_group)
        self.adj_none_check  = QCheckBox("原始价（必存）")
        self.adj_front_check = QCheckBox("前复权")
        self.adj_back_check  = QCheckBox("后复权")
        self.adj_none_check.setChecked(True)
        self.adj_none_check.setEnabled(False)
        self.adj_none_check.setToolTip(
            "DuckDB 同一行以原始行情作为基础字段，前/后复权价存放在附加列中。"
        )
        self.adj_front_check.setChecked(True)
        self.adj_back_check.setChecked(True)
        adj_layout.addWidget(self.adj_none_check)
        adj_layout.addWidget(self.adj_front_check)
        adj_layout.addWidget(self.adj_back_check)
        adj_layout.addStretch()
        layout.addWidget(adj_group)

        # ===== 进度信息 =====
        progress_group = QGroupBox("进度信息")
        progress_layout = QVBoxLayout(progress_group)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("%v/%m (%p%)")
        progress_layout.addWidget(self.progress_bar)
        status_layout = QVBoxLayout()
        self.import_status_label = QLabel("就绪")
        self.import_status_label.setWordWrap(True)
        status_layout.addWidget(self.import_status_label)
        self.import_eta_label = QLabel("预计剩余：--")
        self.import_eta_label.setWordWrap(True)
        status_layout.addWidget(self.import_eta_label)
        progress_layout.addLayout(status_layout)
        layout.addWidget(progress_group)

        # ===== 执行日志 =====
        log_group = QGroupBox("执行日志")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        _configure_download_log_widget(self.log_text)
        log_layout.addWidget(self.log_text)
        layout.addWidget(log_group)

        # ===== 按钮行 =====
        # 覆写选项（默认增量，勾选才强制覆写）
        overwrite_layout = QHBoxLayout()
        self.force_overwrite_check = QCheckBox("强制覆写已有数据（跳过增量检测，重新下载上方设定的全部时间范围）")
        self.force_overwrite_check.setChecked(False)
        overwrite_layout.addWidget(self.force_overwrite_check)
        overwrite_layout.addStretch()
        layout.addLayout(overwrite_layout)

        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始下载")
        self.start_btn.clicked.connect(self.start_import)
        btn_layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_import)
        btn_layout.addWidget(self.stop_btn)

        btn_layout.addStretch()

        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def _on_method_changed(self):
        self.preset_frame.setVisible(self.preset_radio.isChecked())
        self.file_frame.setVisible(self.file_radio.isChecked())
        self.manual_frame.setVisible(self.manual_radio.isChecked())

    def browse_stock_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择股票列表文件", "", "CSV文件 (*.csv);;所有文件 (*.*)"
        )
        if file_path:
            self.file_path_edit.setText(file_path)

    def _normalize_stock_code_with_market(self, code: str) -> str:
        return _normalize_market_security_code(code)

    def _read_stock_file(self, file_path: str) -> List[str]:
        stocks = []
        try:
            # 只读一次（header=None），根据首行内容判断是否有列名行，避免二次 I/O
            df = pd.read_csv(file_path, dtype=str, header=None, encoding='utf-8-sig')
            if df.empty or df.shape[1] == 0:
                return stocks
            first_cell = str(df.iloc[0, 0]).strip()
            if any(x in first_cell for x in ('.SH', '.SZ', '.BJ')):
                # 首行已是带市场后缀的代码，直接取第一列
                raw_stocks = df.iloc[:, 0].dropna().tolist()
            else:
                # 首行是列名；将首行提升为表头，其余为数据
                df.columns = [str(c).strip() for c in df.iloc[0]]
                df = df.iloc[1:].reset_index(drop=True)
                code_col = next(
                    (c for c in df.columns if '代码' in c or 'code' in c.lower()),
                    None,
                )
                raw_stocks = (
                    df[code_col].dropna().tolist() if code_col
                    else df.iloc[:, 0].dropna().tolist()
                )
            for code in raw_stocks:
                stocks.append(self._normalize_stock_code_with_market(code))
        except Exception as e:
            self._append_log(f"[错误] 读取文件失败: {e}")
        return stocks

    def get_stock_list(self) -> List[str]:
        stocks = []
        if self.preset_radio.isChecked():
            legacy_dirs = [
                os.path.join(os.path.dirname(os.path.dirname(__file__)), 'stock_lists'),
                os.path.join(os.path.dirname(__file__), 'stock_lists'),
            ]
            preset_files = {
                'all_a':  '沪深A股_股票列表.csv',
                'sh_a':   '上证A股_股票列表.csv',
                'sz_a':   '深证A股_股票列表.csv',
                'hs300':  '沪深300成分股_股票列表.csv',
                'sz50':   '上证50成分股_股票列表.csv',
                'zz500':  '中证500成分股_股票列表.csv',
                'cyb':    '创业板_股票列表.csv',
                'kcb':    '科创板_股票列表.csv',
                'hs_etf': '沪深ETF_成分股列表.csv',
                'hs_fund': '沪深基金_列表.csv',
                'hs_convertible_bonds': '沪深转债_列表.csv',
                't0_etf': 'T0型ETF.csv',
                'common_index': '指数_股票列表.csv',
            }
            for key, cb in self.preset_checks.items():
                if not cb.isChecked() or key not in preset_files:
                    continue
                fname = preset_files[key]
                file_path = _resolve_preset_stock_pool_file(fname, legacy_dirs)
                if file_path:
                    stocks.extend(self._read_stock_file(file_path))

        elif self.file_radio.isChecked():
            fp = self.file_path_edit.text().strip()
            if fp and os.path.exists(fp):
                stocks = self._read_stock_file(fp)
            else:
                QMessageBox.warning(self, "错误", "请选择有效的股票列表文件")
                return []

        elif self.manual_radio.isChecked():
            text = self.manual_edit.toPlainText().strip()
            for line in text.split('\n'):
                code = line.strip()
                if code:
                    stocks.append(self._normalize_stock_code_with_market(code))

        return list(set(stocks))

    def get_periods_config(self) -> List[dict]:
        """构建传给 TushareImportThread 的 periods 列表。"""
        periods = []
        do_none  = self.adj_none_check.isChecked()
        do_front = self.adj_front_check.isChecked()
        do_back  = self.adj_back_check.isChecked()

        if self.period_1d_check.isChecked():
            periods.append({
                'period':     '1d',
                'start_date': self.period_1d_start.date().toString("yyyyMMdd"),
                'end_date':   self.period_1d_end.date().toString("yyyyMMdd"),
                'adj_none':   do_none,
                'adj_front':  do_front,
                'adj_back':   do_back,
            })
        if self.period_1m_check.isChecked():
            periods.append({
                'period':     '1m',
                'start_date': self.period_1m_start.date().toString("yyyy-MM-dd") + " 09:00:00",
                'end_date':   self.period_1m_end.date().toString("yyyy-MM-dd")   + " 15:00:00",
                'adj_none':   do_none,
                'adj_front':  do_front,
                'adj_back':   do_back,
            })
        if self.period_5m_check.isChecked():
            periods.append({
                'period':     '5m',
                'start_date': self.period_5m_start.date().toString("yyyy-MM-dd") + " 09:00:00",
                'end_date':   self.period_5m_end.date().toString("yyyy-MM-dd")   + " 15:00:00",
                'adj_none':   do_none,
                'adj_front':  do_front,
                'adj_back':   do_back,
            })
        return periods

    def _append_log(self, msg: str):
        _append_download_log(self.log_text, f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    # ------------------------------------------------------------------
    # 导入逻辑
    # ------------------------------------------------------------------

    def _read_token(self) -> str:
        """从 GUI/CLI 共享配置读取并解码 token。"""
        return load_tushare_settings().token

    def _read_proxy(self) -> Tuple[bool, str, str]:
        settings = load_tushare_settings()
        return settings.use_proxy, settings.proxy_url, settings.api_url

    def start_import(self):
        self._close_after_stop = False
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._import_start_time = datetime.now()
        self.import_eta_label.setText("预计剩余：--")

        # 读取 token
        token = self._read_token()
        if not token:
            QMessageBox.warning(self, "错误", "Token 未配置，请在【软件设置 → Tushare设置】中设置。")
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            return

        # 校验股票列表
        stock_list = self.get_stock_list()
        if not stock_list:
            QMessageBox.warning(self, "错误", "股票列表为空，请至少选择或输入一只股票。")
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            return

        # 校验周期
        periods = self.get_periods_config()
        if not periods:
            QMessageBox.warning(self, "错误", "请至少勾选一个数据周期。")
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            return

        date_pairs = []
        if self.period_1d_check.isChecked():
            date_pairs.append(("1d", self.period_1d_start.date(), self.period_1d_end.date()))
        if self.period_1m_check.isChecked():
            date_pairs.append(("1m", self.period_1m_start.date(), self.period_1m_end.date()))
        if self.period_5m_check.isChecked():
            date_pairs.append(("5m", self.period_5m_start.date(), self.period_5m_end.date()))
        invalid_periods = [name for name, start, end in date_pairs if start > end]
        if invalid_periods:
            QMessageBox.warning(
                self,
                "错误",
                f"以下周期的开始日期晚于结束日期：{', '.join(invalid_periods)}",
            )
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            return

        minute_periods = [
            item["period"] for item in periods if item["period"] in ("1m", "5m")
        ]
        if minute_periods:
            from .tushare_importer import is_tushare_index_code

            index_stocks = [code for code in stock_list if is_tushare_index_code(code)]
            if index_stocks:
                preview = "、".join(index_stocks[:5])
                if len(index_stocks) > 5:
                    preview += f" 等 {len(index_stocks)} 只"
                QMessageBox.warning(
                    self,
                    "指数不支持分钟线",
                    "Tushare 当前指数接口只支持日线，不能与 1m/5m 一起提交。\n"
                    f"检测到指数：{preview}\n\n"
                    "请取消分钟周期，或将指数与股票分成两次下载。",
                )
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                return

        # 校验复权（三选一，至少要勾一种）
        if not any([
            self.adj_none_check.isChecked(),
            self.adj_front_check.isChecked(),
            self.adj_back_check.isChecked(),
        ]):
            QMessageBox.warning(self, "错误", "请至少勾选一种复权方式（不复权 / 前复权 / 后复权）。")
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            return

        use_proxy, proxy_url, api_url = self._read_proxy()
        data_root = getattr(self.manager, 'data_root', None) or ''

        self.log_text.clear()
        self.progress_bar.setValue(0)
        self._append_log(f"开始下载，股票 {len(stock_list)} 只，周期 {[p['period'] for p in periods]}")

        # 导入前先释放当前进程已持有的 DuckDB 连接，避免线程启动后写入时撞锁。
        try:
            if hasattr(self.manager, 'close_all_no_checkpoint'):
                self.manager.close_all_no_checkpoint()
            elif hasattr(self.manager, 'close_all'):
                self.manager.close_all()
        except Exception as e:
            self._append_log(f"[警告] 导入前释放数据库连接失败: {e}")

        from .tushare_import_worker import TushareImportThread
        self.import_thread = TushareImportThread(
            token=token,
            use_proxy=use_proxy,
            proxy_url=proxy_url,
            api_url=api_url,
            stock_list=stock_list,
            periods=periods,
            manager=self.manager,
            force_overwrite=self.force_overwrite_check.isChecked(),
            parent=self,
        )
        self.import_thread.progress.connect(self._on_progress)
        self.import_thread.status.connect(self.import_status_label.setText)
        self.import_thread.log.connect(self._append_log)
        self.import_thread.lock_conflict.connect(self._on_lock_conflict)
        self.import_thread.enable_lock_prompt()
        self.import_thread.finished.connect(self._on_finished)
        self.import_thread.start()

    def _on_lock_conflict(self, info: dict):
        """Tushare 自动重试耗尽后，在界面线程询问如何继续。"""
        thread = self.import_thread
        if thread is None:
            return

        stock = info.get("stock") or "元数据索引"
        period = info.get("period") or ""
        operation = info.get("operation") or "数据库操作"
        pid = info.get("pid")
        process = info.get("process") or "其他进程"
        path = info.get("db_path") or ""

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("数据库文件正在使用")
        target = f"{stock}（{period}）" if period else stock
        box.setText(f"{target}暂时无法完成{operation}")
        detail_lines = [
            "系统已自动重试 5 次，但文件仍被其他任务占用。",
            f"占用进程：{process}" + (f"（PID {pid}）" if pid else ""),
        ]
        if path:
            detail_lines.append(f"文件：{path}")
        if operation == "刷新元数据":
            detail_lines.append("跳过不会丢失已写入行情，可稍后通过扫描修复元数据。")
        else:
            detail_lines.append("跳过项会在本次任务结束总结中单独列出。")
        box.setInformativeText("\n".join(detail_lines))
        retry_button = box.addButton("继续重试", QMessageBox.AcceptRole)
        skip_button = box.addButton("先跳过", QMessageBox.ActionRole)
        abort_button = box.addButton("停止任务", QMessageBox.RejectRole)
        box.setDefaultButton(skip_button)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is retry_button:
            thread.set_lock_resolution("retry")
        elif clicked is abort_button:
            thread.set_lock_resolution("abort")
        else:
            thread.set_lock_resolution("skip")

    def is_import_running(self) -> bool:
        """返回后台下载线程是否仍在运行。"""
        try:
            return bool(self.import_thread and self.import_thread.isRunning())
        except RuntimeError:
            self.import_thread = None
            return False

    def request_close_after_stop(self):
        """请求线程停止，并在真正退出后自动关闭窗口。"""
        if not self.is_import_running():
            self.close()
            return

        self._close_after_stop = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.import_status_label.setText("正在停止下载，线程退出后自动关闭...")
        self.import_eta_label.setText("预计剩余：--")
        self._append_log("正在停止下载，等待当前请求结束后自动关闭窗口...")
        try:
            self.import_thread.stop()
        except Exception:
            pass
        self.hide()

    def stop_import(self):
        if self.import_thread and self.import_thread.isRunning():
            self.import_thread.stop()
            self._append_log("已发送停止信号，等待当前任务完成...")
            # 注意：start_btn 由 _on_finished 信号在线程真正结束时重新启用
            # 此处不立即启用，防止旧线程未退出时重复启动
        else:
            self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.import_eta_label.setText("预计剩余：--")

    def closeEvent(self, event):
        """关闭前检查线程状态，防止线程仍在运行时销毁对话框导致崩溃。"""
        if self.is_import_running():
            reply = QMessageBox.question(
                self, "确认关闭",
                "数据下载正在进行中，确定要关闭吗？\n"
                "系统会先停止领取新任务，等待当前请求、元数据刷新和数据库连接收尾后再关闭。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.request_close_after_stop()
                event.ignore()
            else:
                event.ignore()
        else:
            event.accept()

    def _on_progress(self, completed: int, total: int, requests: int = 0, recent_requests: int = 0):
        if not _should_update_download_ui(self, '_last_tushare_progress_ui_ts', completed, total):
            return
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(completed)
            # 更新 ETA 和完成比例
            if self._import_start_time:
                elapsed = (datetime.now() - self._import_start_time).total_seconds()
                
                # 如果运行时间不足1分钟，推算每分钟速率；否则直接使用最近一分钟的真实请求数
                if elapsed < 60 and elapsed > 0:
                    req_per_min = (requests / elapsed) * 60
                else:
                    req_per_min = recent_requests
                
                if completed > 0:
                    eta_sec = elapsed / completed * (total - completed)
                    mins, secs = divmod(int(eta_sec), 60)
                    if mins >= 60:
                        hours, mins = divmod(mins, 60)
                        eta_str = f"{hours}小时{mins}分{secs:02d}秒"
                    else:
                        eta_str = f"{mins}分{secs:02d}秒"
                        
                    self.import_eta_label.setText(f"进度：{completed}/{total} | 请求数：{requests} ({req_per_min:.1f}次/分) | 预计剩余：{eta_str}")
                else:
                    self.import_eta_label.setText(f"进度：0/{total} | 请求数：{requests} ({req_per_min:.1f}次/分) | 预计剩余：计算中...")

    def _on_finished(self, ok: bool, summary: str):
        thread = self.import_thread
        self.import_thread = None
        results = getattr(thread, "last_results", {}) if thread else {}

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.import_eta_label.setText("已完成")
        self._append_log(f"{'[完成]' if ok else '[部分失败]'} {summary}")
        lock_skipped = results.get("lock_skipped", [])
        if lock_skipped:
            self._append_log(f"[汇总] 数据库占用项: {len(lock_skipped)}")
            for item in lock_skipped:
                target = item.get("stock") or "metadata"
                period = item.get("period") or ""
                pid = item.get("pid") or "未知"
                scope = item.get("scope") or "task"
                self._append_log(
                    f"  - {target} {period}，PID {pid}，范围 {scope}"
                )
        errors = results.get("errors", [])
        if errors:
            self._append_log(f"[汇总] 错误/无数据项: {len(errors)}")
            for message in errors[:20]:
                self._append_log(f"  - {message}")
            if len(errors) > 20:
                self._append_log(f"  - 其余 {len(errors) - 20} 项请查看运行日志")
        adjustment_errors = results.get("adjustment_errors", [])
        if adjustment_errors:
            self._append_log(f"[汇总] 复权待补项: {len(adjustment_errors)}")
            for message in adjustment_errors[:20]:
                self._append_log(f"  - {message}")
        unresolved = results.get("unresolved", [])
        if unresolved:
            self._append_log(f"[汇总] 导入后仍待核验: {len(unresolved)} 个任务")
            for item in unresolved[:20]:
                raw_count = len(item.get("raw_dates") or [])
                adjustment_count = len(item.get("adjustment_dates") or [])
                self._append_log(
                    f"  - {item.get('stock', '')} {item.get('period', '')}: "
                    f"raw {raw_count} 日，复权 {adjustment_count} 日"
                )

        has_changes = bool(
            results.get("total_records", 0)
            or results.get("metadata_updated", 0)
        )
        if (
            has_changes
            and self.parent()
            and not (
                getattr(self.parent(), '_viewer_closing', False)
                or getattr(self.parent(), '_pending_close_after_tushare_stop', False)
            )
            and hasattr(self.parent(), "on_refresh_clicked")
        ):
            try:
                self.parent().on_refresh_clicked()
                self._append_log("股票列表已自动刷新")
            except Exception as exc:
                self._append_log(f"[警告] 自动刷新股票列表失败: {exc}")

        display_summary = summary
        if lock_skipped:
            preview = []
            for item in lock_skipped[:8]:
                target = item.get("stock") or "metadata"
                period = item.get("period") or ""
                preview.append(f"{target} {period}".strip())
            display_summary += "\n\n数据库占用跳过：\n" + "\n".join(f"• {x}" for x in preview)
            if len(lock_skipped) > 8:
                display_summary += f"\n• 其余 {len(lock_skipped) - 8} 项请查看日志"
        if self._close_after_stop:
            self._close_after_stop = False
            if thread:
                try:
                    thread.deleteLater()
                except Exception as e:
                    self._append_log(f"线程资源释放失败: {e}")
            QTimer.singleShot(0, self.close)
            return

        if ok:
            QMessageBox.information(self, "完成", display_summary)
        else:
            QMessageBox.warning(self, "部分失败", display_summary)

        if thread:
            try:
                thread.deleteLater()
            except Exception as e:
                self._append_log(f"线程资源释放失败: {e}")


class HttpBridgeImportDialog(QDialog):
    """从 miniQMT 桥接服务导入数据到本地 DuckDB。

    界面对齐"从MiniQMT导入数据"：股票池支持 预设板块/从文件导入/手动输入(支持纯数字)；
    数据周期可多选且各自设置起止日期；复权口径多选，分别写入对应字段。
    """

    _DIVIDENDS = [
        ("不复权", "none"), ("前复权", "front"), ("后复权", "back"),
        ("等比前复权", "front_ratio"), ("等比后复权", "back_ratio"),
    ]
    # 预设板块 -> data/ 下现有股票列表文件
    _PRESETS = [
        ('沪深A股', '沪深A股_股票列表.csv'),
        ('上证A股', '上证A股_股票列表.csv'),
        ('深证A股', '深证A股_股票列表.csv'),
        ('沪深300', '沪深300成分股_股票列表.csv'),
        ('上证50', '上证50成分股_股票列表.csv'),
        ('中证500', '中证500成分股_股票列表.csv'),
        ('创业板', '创业板_股票列表.csv'),
        ('科创板', '科创板_股票列表.csv'),
        ('沪深ETF', '沪深ETF_成分股列表.csv'),
        ('沪深场内基金（含ETF/LOF）', '沪深基金_列表.csv'),
        ('沪深转债', '沪深转债_列表.csv'),
        ('常用指数', '指数_股票列表.csv'),
    ]

    _QSS = """
        QDialog, QWidget { background-color: #333333; color: #e8e8e8; font-family: "Microsoft YaHei UI"; }
        QLabel { color: #e8e8e8; }
        QLineEdit, QTextEdit { background-color: #3c3c3c; color: #e8e8e8;
            border: 1px solid #555555; border-radius: 3px; padding: 4px; }
        QPushButton { background-color: #0078d4; color: #ffffff; border: none;
            border-radius: 4px; padding: 6px 14px; font-family: "Microsoft YaHei UI"; font-weight: bold; font-size: 14px; }
        QPushButton:hover { background-color: #106ebe; }
        QPushButton:disabled { background-color: #555555; color: #888888; }
        QPushButton#secondary { background-color: #4d4d4d; }
        QPushButton#secondary:hover { background-color: #5a5a5a; }
        QPushButton#danger { background-color: #c0392b; }
        QComboBox, QDateEdit { background-color: #3c3c3c; color: #e8e8e8;
            border: 1px solid #555555; border-radius: 3px; padding: 4px; }
        QComboBox QAbstractItemView { background-color: #3c3c3c; color: #e8e8e8;
            selection-background-color: #0078d4; }
        QCheckBox, QRadioButton { color: #e8e8e8; spacing: 6px; padding: 2px 0; }
        QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px;
            border: 1px solid #555555; background-color: #3c3c3c; }
        QCheckBox::indicator { border-radius: 3px; }
        QRadioButton::indicator { border-radius: 8px; }
        QCheckBox::indicator:checked, QRadioButton::indicator:checked {
            background-color: #0078d4; border-color: #0078d4; }
        QGroupBox { border: 1px solid #555555; border-radius: 5px; margin-top: 10px;
            padding-top: 10px; color: #e8e8e8; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
        QProgressBar { background-color: #3c3c3c; border: 1px solid #555555;
            border-radius: 3px; text-align: center; color: #e8e8e8; }
        QProgressBar::chunk { background-color: #0078d4; }
    """

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._viewer = parent
        self.manager = manager
        from qt_settings_bridge import KhQtSettings
        self.settings = KhQtSettings('KHQuant', 'StockAnalyzer')
        self._thread = None
        self.setWindowTitle("从桥接导入数据 - miniQMT 桥接")
        self.setMinimumSize(820, 820)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        _ui_font = get_preferred_ui_font_family() or "Microsoft YaHei UI"
        qss = self._QSS
        if _ui_font != "Microsoft YaHei UI":
            qss = qss.replace('"Microsoft YaHei UI"', f'"{_ui_font}"')
        self.setStyleSheet(qss)
        self._build_ui()
        self._set_dark_titlebar()

    def _set_dark_titlebar(self):
        """Windows 暗色标题栏（与主界面一致）；非 Windows 自动跳过，macOS 用原生标题栏。"""
        try:
            import platform
            if platform.system() != "Windows":
                return
            from ctypes import windll, c_int, byref, sizeof
            from ctypes.wintypes import DWORD
            hwnd = int(self.winId())
            windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, byref(c_int(1)), sizeof(c_int))
            windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, byref(DWORD(0x00333333)), sizeof(DWORD))
        except Exception:
            pass

    # ---------------- UI ----------------
    def _build_ui(self):
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        root = QVBoxLayout(container)
        scroll.setWidget(container)
        outer.addWidget(scroll)

        # ===== 桥接服务配置 =====
        cfg = QGroupBox("桥接服务配置")
        cfgl = QGridLayout(cfg)
        cfgl.addWidget(QLabel("服务地址:"), 0, 0)
        self.url_edit = QLineEdit(self.settings.value('http_base_url', 'http://127.0.0.1:8001'))
        cfgl.addWidget(self.url_edit, 0, 1)
        cfgl.addWidget(QLabel("API Key:"), 1, 0)
        self.key_edit = QLineEdit(self.settings.value('http_api_key', ''))
        cfgl.addWidget(self.key_edit, 1, 1)
        self.test_btn = QPushButton("测试连接")
        self.test_btn.setObjectName("secondary")
        self.test_btn.clicked.connect(self._test_connection)
        cfgl.addWidget(self.test_btn, 0, 2, 2, 1)
        cfgl.setColumnStretch(1, 1)
        root.addWidget(cfg)

        # ===== 股票池选择 =====
        stock_group = QGroupBox("股票池选择")
        stock_layout = QVBoxLayout(stock_group)
        method_layout = QHBoxLayout()
        self.method_group = QButtonGroup(self)
        self.preset_radio = QRadioButton("预设板块")
        self.preset_radio.setChecked(True)
        self.file_radio = QRadioButton("从文件导入")
        self.manual_radio = QRadioButton("手动输入")
        for rb in (self.preset_radio, self.file_radio, self.manual_radio):
            self.method_group.addButton(rb)
            method_layout.addWidget(rb)
            rb.toggled.connect(self.on_method_changed)
        method_layout.addStretch()
        stock_layout.addLayout(method_layout)

        # 预设板块
        self.preset_frame = QFrame()
        preset_layout = QGridLayout(self.preset_frame)
        preset_layout.setContentsMargins(0, 0, 0, 0)
        self.preset_checks = {}
        for i, (name, fn) in enumerate(self._PRESETS):
            cb = QCheckBox(name)
            self.preset_checks[fn] = cb
            preset_layout.addWidget(cb, i // 4, i % 4)
        stock_layout.addWidget(self.preset_frame)

        # 从文件
        self.file_frame = QFrame()
        file_layout = QHBoxLayout(self.file_frame)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_path_edit = QLineEdit()
        self.file_path_edit.setPlaceholderText("选择股票列表CSV文件...")
        file_layout.addWidget(self.file_path_edit)
        browse_btn = QPushButton("浏览...")
        browse_btn.setObjectName("secondary")
        browse_btn.clicked.connect(self.browse_stock_file)
        file_layout.addWidget(browse_btn)
        self.file_frame.setVisible(False)
        stock_layout.addWidget(self.file_frame)

        # 手动输入
        self.manual_frame = QFrame()
        manual_layout = QVBoxLayout(self.manual_frame)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.addWidget(QLabel("输入股票代码（每行一个，支持纯数字或带市场后缀）:"))
        self.manual_edit = QTextEdit()
        self.manual_edit.setMaximumHeight(100)
        self.manual_edit.setPlaceholderText("000001.SZ 或 000001\n600000.SH 或 600000\n300750")
        manual_layout.addWidget(self.manual_edit)
        self.manual_frame.setVisible(False)
        stock_layout.addWidget(self.manual_frame)
        root.addWidget(stock_group)

        # ===== 数据周期设置（多选 + 各自起止）=====
        period_group = QGroupBox("数据周期设置（可多选，分别设置起止日期）")
        pl = QGridLayout(period_group)
        today = QDate.currentDate()
        self.period_rows = {}
        period_defs = [
            ("1d", "日线 (1d)", today.addYears(-3), True),
            ("5m", "5分钟 (5m)", today.addYears(-1), False),
            ("1m", "1分钟 (1m)", today.addYears(-1), False),
            ("tick", "Tick数据", today.addMonths(-1), False),
        ]
        for r, (pid, label, default_start, default_on) in enumerate(period_defs):
            cb = QCheckBox(label)
            cb.setChecked(default_on)
            pl.addWidget(cb, r, 0)
            pl.addWidget(QLabel("开始:"), r, 1)
            ds = QDateEdit()
            ds.setCalendarPopup(True)
            ds.setDate(default_start)
            pl.addWidget(ds, r, 2)
            pl.addWidget(QLabel("结束:"), r, 3)
            de = QDateEdit()
            de.setCalendarPopup(True)
            de.setDate(today)
            pl.addWidget(de, r, 4)
            self.period_rows[pid] = (cb, ds, de)
        self.force_overwrite_check = QCheckBox("强制覆写已有数据（禁用增量补充）")
        self.force_overwrite_check.setChecked(False)
        self.force_overwrite_check.setToolTip(
            "不勾选：增量补充，已存在的K线保持不变，仅补入缺失部分；\n勾选：强制覆写，重新下载并覆盖所选区间已有数据。")
        pl.addWidget(self.force_overwrite_check, len(period_defs), 0, 1, 5)
        root.addWidget(period_group)

        # ===== 复权方式（多选）=====
        divg = QGroupBox("复权方式（可多选，分别写入对应字段）")
        dl = QGridLayout(divg)
        self.div_cbs = {}
        for i, (label, val) in enumerate(self._DIVIDENDS):
            cb = QCheckBox(label)
            if val == 'none':
                cb.setChecked(True)
            self.div_cbs[val] = cb
            dl.addWidget(cb, i // 3, i % 3)
        root.addWidget(divg)

        # ===== 操作 + 进度 =====
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("开始导入")
        self.start_btn.clicked.connect(self.start_import)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_import)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%v / %m")
        root.addWidget(self.progress)
        self.status_label = QLabel("准备就绪")
        self.status_label.setStyleSheet("color: #a0a0a0;")
        root.addWidget(self.status_label)

        # ===== 运行日志 =====
        logg = QGroupBox("运行日志")
        ll = QVBoxLayout(logg)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(150)
        _configure_download_log_widget(self.log_text)
        ll.addWidget(self.log_text)
        root.addWidget(logg)

    # ---------------- 交互 ----------------
    def on_method_changed(self):
        self.preset_frame.setVisible(self.preset_radio.isChecked())
        self.file_frame.setVisible(self.file_radio.isChecked())
        self.manual_frame.setVisible(self.manual_radio.isChecked())

    def browse_stock_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择股票列表文件", "", "CSV文件 (*.csv);;所有文件 (*.*)")
        if path:
            self.file_path_edit.setText(path)

    def log(self, msg):
        _append_download_log(self.log_text, f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def _normalize_code(self, code):
        code = (code or "").strip().upper()
        if '.' in code:
            parts = code.split('.')
            if len(parts) == 2 and parts[1] in ('SH', 'SZ', 'BJ'):
                return code
            if len(parts) == 2 and parts[0] in ('SH', 'SZ', 'BJ') and parts[1].isdigit():
                return f"{parts[1]}.{parts[0]}"
        if code.isdigit() and len(code) == 6:
            f = code[0]
            if f in ('6', '5', '9'):
                return f"{code}.SH"
            if f in ('0', '2', '3'):
                return f"{code}.SZ"
            if f == '1':
                if code.startswith(('11', '113', '118')):
                    return f"{code}.SH"
                if code.startswith(('12', '123', '127', '128')):
                    return f"{code}.SZ"
                return f"{code}.SH"
            if f in ('4', '8'):
                return f"{code}.BJ"
        return code

    def _read_stock_file(self, file_path):
        stocks = []
        try:
            df = pd.read_csv(file_path, dtype=str, header=None, encoding='utf-8-sig')
            if len(df) > 0 and len(df.columns) > 0:
                first_cell = str(df.iloc[0, 0]).strip()
                # 首格是带后缀代码或纯6位数字 -> 整列即数据（无表头）
                looks_like_code = (any(s in first_cell.upper() for s in ('.SH', '.SZ', '.BJ'))
                                   or (first_cell.isdigit() and len(first_cell) == 6))
                if looks_like_code:
                    raw = df.iloc[:, 0].dropna().tolist()
                else:
                    df = pd.read_csv(file_path, dtype=str, encoding='utf-8-sig')
                    raw = []
                    for col in df.columns:
                        if '代码' in col or 'code' in col.lower():
                            raw.extend(df[col].dropna().tolist())
                            break
                    else:
                        raw.extend(df.iloc[:, 0].dropna().tolist())
                for c in raw:
                    stocks.append(self._normalize_code(str(c)))
        except Exception as e:
            self.log(f"读取文件失败: {e}")
        return stocks

    def get_stock_list(self):
        stocks = []
        if self.preset_radio.isChecked():
            for fn, cb in self.preset_checks.items():
                if cb.isChecked():
                    path = _resolve_preset_stock_pool_file(fn)
                    if path:
                        stocks.extend(self._read_stock_file(path))
                    else:
                        self.log(f"列表文件不存在: {fn}")
        elif self.file_radio.isChecked():
            path = self.file_path_edit.text().strip()
            if path and os.path.exists(path):
                stocks = self._read_stock_file(path)
        elif self.manual_radio.isChecked():
            for line in self.manual_edit.toPlainText().strip().split('\n'):
                c = line.strip()
                if c:
                    stocks.append(self._normalize_code(c))
        # 去重保持顺序
        seen, out = set(), []
        for c in stocks:
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def _selected_periods(self):
        specs = []
        for pid, (cb, ds, de) in self.period_rows.items():
            if cb.isChecked():
                specs.append((pid,
                              ds.date().toString('yyyyMMdd'),
                              de.date().toString('yyyyMMdd')))
        return specs

    def _selected_dividends(self):
        dts = [v for v, cb in self.div_cbs.items() if cb.isChecked()]
        return dts or ['none']

    def _make_importer(self):
        self.settings.setValue('http_base_url', self.url_edit.text().strip())
        self.settings.setValue('http_api_key', self.key_edit.text().strip())
        from duckdb_storage.http_importer import HttpImporter
        return HttpImporter(self.url_edit.text().strip(), self.key_edit.text().strip(), self.manager.data_root)

    def _test_connection(self):
        try:
            imp = self._make_importer()
            self.log(f"测试连接：{self.url_edit.text().strip()}")
            ok, msg = imp.test_connection()
            self.log(("连接成功：" if ok else "连接失败：") + str(msg))
            (QMessageBox.information if ok else QMessageBox.warning)(self, "连接测试", str(msg))
        except Exception as e:
            self.log(f"测试连接异常：{e}")
            QMessageBox.critical(self, "错误", str(e))

    # ---------------- 导入 ----------------
    def start_import(self):
        if self._thread is not None and self._thread.isRunning():
            return
        stocks = self.get_stock_list()
        if not stocks:
            QMessageBox.warning(self, "提示", "请先选择股票池 / 文件 / 手动输入股票代码")
            return
        periods = self._selected_periods()
        if not periods:
            QMessageBox.warning(self, "提示", "请至少勾选一个数据周期")
            return
        dts = self._selected_dividends()
        force_overwrite = self.force_overwrite_check.isChecked()
        try:
            importer = self._make_importer()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))
            return

        # 导入前先做连通性检查：未连接则提示并中止，避免空跑出"成功 0/N"
        self.status_label.setText("正在检查桥接服务连接...")
        QApplication.processEvents()
        try:
            ok, msg = importer.test_connection()
        except Exception as e:
            ok, msg = False, str(e)
        if not ok:
            self.status_label.setText("未连接桥接服务")
            self.log(f"连接失败：{msg}")
            QMessageBox.warning(
                self, "无法连接桥接服务",
                f"{msg}\n\n请检查：\n"
                f"1) 服务端是否已点「启动服务」\n"
                f"2) 服务地址与端口是否正确（当前 {self.url_edit.text().strip()}）\n"
                f"3) API Key 是否与服务端一致")
            return
        self.log(f"连接成功：{self.url_edit.text().strip()}")

        total_units = len(stocks) * len(periods)
        self.progress.setRange(0, total_units)
        self.progress.setValue(0)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.log(f"开始导入：{len(stocks)} 只 | 周期 {','.join(p[0] for p in periods)} | "
                 f"复权 {','.join(dts)} | {'强制覆写' if force_overwrite else '增量补充'}")

        self._thread = _BridgeImportThread(importer, stocks, periods, dts, force_overwrite)
        self._thread.progress_sig.connect(self._on_progress)
        self._thread.log_sig.connect(self.log)
        self._thread.done_sig.connect(self._on_done)
        self._thread.start()

    def stop_import(self):
        if self._thread is not None and self._thread.isRunning():
            self._thread.stop()
            self.log("正在停止（当前股票完成后停止）...")
            self.stop_btn.setEnabled(False)

    def _on_progress(self, cur, total, rows, msg):
        if not _should_update_download_ui(self, '_last_http_bridge_progress_ui_ts', cur, total):
            return
        self.progress.setValue(cur)
        self.status_label.setText(f"{msg}  |  进度 {cur}/{total}  累计 {rows:,} 条")

    def _on_done(self, total_ok, total_all, stopped):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        tip = "已停止" if stopped else "导入完成"
        self.status_label.setText(f"{tip}：成功 {total_ok}/{total_all}")
        self.log(f"{tip}：成功 {total_ok}/{total_all}")
        if not stopped:
            QMessageBox.information(self, "导入完成", f"成功导入 {total_ok}/{total_all}（股票×周期）")
        if self._viewer is not None and hasattr(self._viewer, 'refresh_stock_list'):
            try:
                self._viewer.refresh_stock_list()
            except Exception:
                pass


class _BridgeImportThread(QThread):
    """桥接导入后台线程：逐股逐周期导入，进度按 股票×周期 推进并实时累计条数。

    逐股处理使进度条在下载过程中持续前进（而非整周期结束才跳一下），
    且不引入逐条轮询等额外开销——单只的下载工作量与批量相同。
    """
    progress_sig = pyqtSignal(int, int, int, str)   # 已完成单元, 总单元, 累计条数, 消息
    log_sig = pyqtSignal(str)
    done_sig = pyqtSignal(int, int, bool)

    def __init__(self, importer, stocks, period_specs, dividend_types, force_overwrite=True):
        super().__init__()
        self.importer = importer
        self.stocks = stocks
        self.period_specs = period_specs   # [(period, start, end), ...]
        self.dividend_types = dividend_types
        self.force_overwrite = force_overwrite
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        n = len(self.stocks)
        total = n * len(self.period_specs)
        done_units = 0
        total_ok = 0
        total_rows = 0
        metadata_records = []
        manager = self.importer._manager
        short_lock_enabled = False
        try:
            if hasattr(manager, 'enable_short_lock_write'):
                manager.enable_short_lock_write()
                short_lock_enabled = True
            for period, start, end in self.period_specs:
                if self._stop:
                    break
                self.log_sig.emit(f"[{period}] 区间 {start}~{end} 开始（{n} 只）")
                for code in self.stocks:
                    if self._stop:
                        break
                    try:
                        ok, rows = self.importer.import_one(
                            code,
                            period,
                            start,
                            end,
                            dividend_types=self.dividend_types,
                            force_overwrite=self.force_overwrite,
                            defer_metadata=True,
                        )
                    except Exception as e:
                        ok, rows = False, 0
                        self.log_sig.emit(f"[{period}] {code} 异常：{e}")
                    done_units += 1
                    if ok:
                        total_ok += 1
                        total_rows += rows
                        if rows > 0:
                            metadata_records.append((code, period, rows))
                    tag = f"✓{rows}条" if ok else "无数据"
                    self.progress_sig.emit(done_units, total, total_rows, f"[{period}] {code} {tag}")
                self.log_sig.emit(f"[{period}] 完成")
        finally:
            compact = _coalesce_metadata_records(metadata_records)
            if compact:
                try:
                    updated = manager.batch_update_metadata(compact)
                    self.log_sig.emit(f"元数据刷新完成：{updated}/{len(compact)}")
                except Exception as exc:
                    self.log_sig.emit(f"行情已写入，但元数据刷新失败：{exc}")
            try:
                manager.close_metadata_connection()
            except Exception:
                pass
            if short_lock_enabled and hasattr(manager, 'disable_short_lock_write'):
                manager.disable_short_lock_write()
        self.done_sig.emit(total_ok, total, self._stop)


def main():
    """主函数"""
    app = QApplication(sys.argv)

    # 设置样式
    app.setStyle('Fusion')

    # 未获焦点的下拉框/日期框不再吃滚轮，避免滚动对话框时静默改掉下载区间
    install_wheel_guard(app)

    # 获取命令行参数中的数据目录
    data_root = None
    if len(sys.argv) > 1:
        data_root = sys.argv[1]

    viewer = DuckDBViewer(data_root=data_root)
    viewer.show()

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
