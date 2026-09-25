# coding: utf-8
import time
import datetime
import inspect
import traceback
import importlib.util
from typing import Dict, List, Optional, Union, Any
import logging
import sys
import shutil
from types import SimpleNamespace
import threading
import gc
import os

# 开源版不依赖 xtquant：回测记录沿用 xtconstant 的真实数值，账户对象只保存账号信息。
import kh_constants as xtconstant
from kh_constants import StockAccount

from khTrade import KhTradeManager
from khRisk import KhRiskManager
from khQTTools import KhQuTools, determine_pool_type, format_price, round_price, get_price_decimals, check_t0_support, get_t0_details, get_stock_display, is_etf
from khConfig import KhConfig
from khDynamicLoader import DynamicDataLoader
from khDataSource import (
    DataSourceManager,
    get_data_source_manager,
    init_data_source_manager,
    reset_data_source_manager,
)
from khPathUtils import get_backtest_results_dir, resolve_strategy_file
from security_type_utils import is_listed_fund_code
from performance_config import get_performance_config
from duckdb_storage.lock_retry import is_duckdb_lock_error, parse_duckdb_lock_error

import numpy as np
import pandas as pd


class _LazyMarketRow:
    """Lightweight row view for optional current_data construction fast path."""

    __slots__ = ("_df", "_idx", "_col_pos", "empty")

    def __init__(self, df, idx, col_pos):
        self._df = df
        self._idx = idx
        self._col_pos = col_pos
        self.empty = False

    def get(self, key, default=None):
        loc = self._col_pos.get(key)
        if loc is None:
            return default
        try:
            return self._df.iat[self._idx, loc]
        except Exception:
            return default

    def __getitem__(self, key):
        loc = self._col_pos[key]
        return self._df.iat[self._idx, loc]

    def _kh_fast_get_float(self, key):
        loc = None
        if key == "close":
            loc = self._col_pos.get("lastPrice")
        if loc is None:
            loc = self._col_pos.get(key)
        if loc is None:
            return 0.0
        try:
            value = self._df.iat[self._idx, loc]
            result = float(value)
            if np.isnan(result) or np.isinf(result):
                return 0.0
            return result
        except Exception:
            return 0.0

    def __contains__(self, key):
        return key in self._col_pos

    def __len__(self):
        return len(self._col_pos)

    def __bool__(self):
        return True

    def keys(self):
        return self._df.columns

    def items(self):
        for col in self._df.columns:
            yield col, self.get(col)

    def values(self):
        for col in self._df.columns:
            yield self.get(col)

    def to_dict(self):
        return {col: self.get(col) for col in self._df.columns}


class _EmptyMarketRow:
    """Empty mapping compatible with the optional lazy current_data row mode."""

    __slots__ = ("empty",)

    def __init__(self):
        self.empty = True

    def get(self, key, default=None):
        return default

    def __getitem__(self, key):
        raise KeyError(key)

    def __contains__(self, key):
        return False

    def __len__(self):
        return 0

    def __bool__(self):
        return False

    def keys(self):
        return ()

    def items(self):
        return ()

    def values(self):
        return ()

    def to_dict(self):
        return {}


_EMPTY_MARKET_ROW = _EmptyMarketRow()


def _use_searchsorted_time_index(config) -> bool:
    performance_cfg = get_performance_config(config)
    mode = str(performance_cfg.get("time_index_mode", "dict") or "dict").strip().lower()
    return mode in ("searchsorted", "array", "numpy", "np")


def _build_time_index_cache_entry(time_values, use_searchsorted: bool):
    if use_searchsorted:
        time_np = np.asarray(time_values, dtype=np.int64)
        try:
            if len(time_np) > 1 and not bool(np.all(time_np[1:] > time_np[:-1])):
                use_searchsorted = False
        except Exception:
            use_searchsorted = False
    if use_searchsorted:
        return {
            "mode": "searchsorted",
            "time_np": time_np,
        }
    time_idx_map = {}
    for i, tv in enumerate(time_values):
        time_idx_map[tv] = i
    return time_idx_map


def _lookup_time_index(time_idx_map, current_time, is_ms_timestamp):
    if time_idx_map is None:
        return None, "no_cache"

    if isinstance(time_idx_map, dict) and time_idx_map.get("mode") == "searchsorted":
        time_np = time_idx_map.get("time_np")
        if time_np is None or len(time_np) == 0:
            return None, "no_cache"
        try:
            current_value = int(current_time)
            idx = int(np.searchsorted(time_np, current_value, side="left"))
            if idx < len(time_np) and int(time_np[idx]) == current_value:
                return idx, "direct"

            alt_time = current_value // 1000 if is_ms_timestamp else current_value * 1000
            idx = int(np.searchsorted(time_np, alt_time, side="left"))
            if idx < len(time_np) and int(time_np[idx]) == alt_time:
                return idx, "sec_ms"
        except Exception:
            return None, "missing"
        return None, "missing"

    if not time_idx_map:
        return None, "no_cache"

    if current_time in time_idx_map:
        return time_idx_map[current_time], "direct"
    alt_time = current_time // 1000 if is_ms_timestamp else current_time * 1000
    if alt_time in time_idx_map:
        return time_idx_map[alt_time], "sec_ms"
    return None, "missing"


def _build_lazy_col_pos_cache(historical_data_ref):
    cache = {}
    for code, df in historical_data_ref.items():
        try:
            cache[code] = {col: i for i, col in enumerate(df.columns)}
        except Exception:
            cache[code] = {}
    return cache


_FRAMEWORK_RAW_PREFIX = "__kh_raw_"


def _security_price_decimals(stock_code, fallback=2):
    """Return quote precision for one security inside a mixed asset pool."""
    if stock_code and is_listed_fund_code(str(stock_code)):
        return 3
    code = str(stock_code or "").upper()
    if (
        code.endswith((".SH", ".SZ", ".BJ"))
        or code.startswith(("SH.", "SZ.", "BJ."))
        or (len(code) == 6 and code.isdigit())
    ):
        return 2
    return int(fallback)


def _implied_etf_cash_distribution(
    previous_adjusted_close,
    current_adjusted_close,
    previous_raw_close,
    current_raw_close,
    volume,
):
    """Separate cash distribution from an adjusted ETF total return."""
    values = (
        previous_adjusted_close,
        current_adjusted_close,
        previous_raw_close,
        current_raw_close,
        volume,
    )
    try:
        prev_adj, curr_adj, prev_raw, curr_raw, held_volume = map(float, values)
    except (TypeError, ValueError):
        return 0.0
    if (
        not all(np.isfinite(value) for value in (prev_adj, curr_adj, prev_raw, curr_raw, held_volume))
        or prev_adj <= 0
        or prev_raw <= 0
        or curr_raw <= 0
        or held_volume <= 0
    ):
        return 0.0
    total_return_value = prev_raw * (curr_adj / prev_adj)
    distribution_per_unit = total_return_value - curr_raw
    if distribution_per_unit <= 1e-10:
        return 0.0
    return distribution_per_unit * held_volume


def _split_framework_raw_sidecar(df):
    """Remove internal raw OHLC columns before strategy data is constructed."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df, None
    raw_columns = [col for col in df.columns if str(col).startswith(_FRAMEWORK_RAW_PREFIX)]
    if not raw_columns:
        return df, None
    sidecar_columns = (["time"] if "time" in df.columns else []) + raw_columns
    sidecar = df[sidecar_columns].copy(deep=False).rename(
        columns={col: str(col)[len(_FRAMEWORK_RAW_PREFIX):] for col in raw_columns}
    )
    public_df = df.drop(columns=raw_columns)
    return public_df, sidecar


def _split_framework_raw_pack(data):
    """Split private raw columns for a full Parquet-pack cache hit."""
    public_data = {}
    raw_data = {}
    for code, frame in (data or {}).items():
        public_frame, raw_sidecar = _split_framework_raw_sidecar(frame)
        public_data[code] = public_frame
        if raw_sidecar is not None:
            raw_data[code] = raw_sidecar
    return public_data, raw_data


def _build_available_time_set(time_idx_cache, is_ms_timestamp):
    values = set()
    for entry in time_idx_cache.values():
        try:
            if isinstance(entry, dict) and entry.get("mode") == "searchsorted":
                time_np = entry.get("time_np")
                if time_np is None:
                    continue
                for value in time_np:
                    iv = int(value)
                    values.add(iv)
                    values.add(iv * 1000 if not is_ms_timestamp else iv // 1000)
            elif isinstance(entry, dict):
                for value in entry.keys():
                    iv = int(value)
                    values.add(iv)
                    values.add(iv * 1000 if not is_ms_timestamp else iv // 1000)
        except Exception:
            continue
    return values


class _LazyCurrentData:
    """Dict-like current_data that resolves stock rows only when accessed."""

    __slots__ = (
        "_stock_codes", "_stock_set", "_historical_data_ref", "_time_idx_cache",
        "_current_time", "_is_ms_timestamp", "_use_lazy_rows", "_col_pos_cache",
        "_stats", "_extra", "_overrides", "_row_cache",
    )

    def __init__(
        self,
        stock_codes,
        stock_set,
        historical_data_ref,
        time_idx_cache,
        current_time,
        is_ms_timestamp,
        use_lazy_rows,
        col_pos_cache,
        stats=None,
    ):
        self._stock_codes = stock_codes
        self._stock_set = stock_set
        self._historical_data_ref = historical_data_ref
        self._time_idx_cache = time_idx_cache
        self._current_time = current_time
        self._is_ms_timestamp = is_ms_timestamp
        self._use_lazy_rows = use_lazy_rows
        self._col_pos_cache = col_pos_cache
        self._stats = stats
        self._extra = {}
        self._overrides = {}
        self._row_cache = {}

    def _find_idx(self, code):
        time_idx_map = self._time_idx_cache.get(code)
        current_time = self._current_time
        idx, match_kind = _lookup_time_index(time_idx_map, current_time, self._is_ms_timestamp)
        self._bump(match_kind)
        return idx

    def _bump(self, key):
        if self._stats is not None:
            try:
                self._stats[key] += 1
            except Exception:
                pass

    def _row_for(self, code):
        if code in self._overrides:
            return self._overrides[code]
        if code in self._row_cache:
            return self._row_cache[code]
        df = self._historical_data_ref.get(code)
        if df is None:
            self._bump("no_ref")
            row = _EMPTY_MARKET_ROW
        else:
            idx = self._find_idx(code)
            if idx is None:
                row = _EMPTY_MARKET_ROW
            elif self._use_lazy_rows:
                row = _LazyMarketRow(df, idx, self._col_pos_cache.get(code, {}))
            else:
                row = df.iloc[idx]
        self._row_cache[code] = row
        return row

    def has_any_stock_data(self):
        for code in self._stock_codes:
            df = self._historical_data_ref.get(code)
            if df is None:
                continue
            time_idx_map = self._time_idx_cache.get(code)
            idx, _ = _lookup_time_index(time_idx_map, self._current_time, self._is_ms_timestamp)
            if idx is not None:
                return True
        return False

    def empty_stocks(self):
        return [
            code for code in self._stock_codes
            if code in self._historical_data_ref and self._row_for(code) is _EMPTY_MARKET_ROW
        ]

    def get(self, key, default=None):
        if key in self._extra:
            return self._extra[key]
        if key in self._stock_set:
            if key not in self._historical_data_ref and key not in self._overrides:
                return default
            return self._row_for(key)
        if key in self._overrides:
            return self._overrides[key]
        return default

    def __getitem__(self, key):
        if key in self._extra:
            return self._extra[key]
        if key in self._stock_set:
            if key not in self._historical_data_ref and key not in self._overrides:
                raise KeyError(key)
            return self._row_for(key)
        if key in self._overrides:
            return self._overrides[key]
        raise KeyError(key)

    def __setitem__(self, key, value):
        if key in self._stock_set:
            self._overrides[key] = value
            self._row_cache[key] = value
        else:
            self._extra[key] = value

    def __contains__(self, key):
        if key in self._extra or key in self._overrides:
            return True
        return key in self._stock_set and key in self._historical_data_ref

    def __iter__(self):
        seen = set()
        for code in self._stock_codes:
            if code not in self._historical_data_ref and code not in self._overrides:
                continue
            seen.add(code)
            yield code
        for key in self._extra:
            if key not in seen:
                seen.add(key)
                yield key
        for key in self._overrides:
            if key not in seen:
                yield key

    def __len__(self):
        stock_len = sum(
            1 for code in self._stock_codes
            if code in self._historical_data_ref or code in self._overrides
        )
        return stock_len + len([k for k in self._extra if k not in self._stock_set])

    def __bool__(self):
        return True

    def keys(self):
        return list(iter(self))

    def items(self):
        for key in self:
            yield key, self[key]

    def values(self):
        for key in self:
            yield self[key]

    def copy(self):
        return {key: self[key] for key in self}

    def fork(self):
        """Create an isolated callback view without materializing stock rows."""
        cloned = _LazyCurrentData(
            stock_codes=self._stock_codes,
            stock_set=self._stock_set,
            historical_data_ref=self._historical_data_ref,
            time_idx_cache=self._time_idx_cache,
            current_time=self._current_time,
            is_ms_timestamp=self._is_ms_timestamp,
            use_lazy_rows=self._use_lazy_rows,
            col_pos_cache=self._col_pos_cache,
            stats=self._stats,
        )
        cloned._extra.update(self._extra)
        cloned._overrides.update(self._overrides)
        return cloned


def _copy_callback_data(data):
    """Copy strategy callback data while preserving lazy stock-row loading."""
    fork = getattr(data, "fork", None)
    if callable(fork):
        return fork()
    return data.copy() if data else {}


def _callback_time_info(base_time_info, date_text, time_text, is_ms_timestamp):
    """Build a self-consistent clock for pre/post-market callbacks."""
    callback_dt = datetime.datetime.strptime(
        f"{date_text} {time_text}", "%Y-%m-%d %H:%M:%S"
    )
    timestamp = int(callback_dt.timestamp() * (1000 if is_ms_timestamp else 1))
    result = dict(base_time_info or {})
    result.update({
        "date": date_text,
        "time": time_text,
        "datetime": callback_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": timestamp,
        "raw_time": timestamp,
        "_dt": callback_dt,
    })
    return result


def _invoke_strategy_callback(framework, callback, data, set_framework_fn=None):
    """Run every strategy callback with the same framework context contract."""
    khimport = sys.modules.get("khQuantImport")
    previous_framework = getattr(khimport, "_CURRENT_FRAMEWORK", None)
    if set_framework_fn is None:
        try:
            import khQuantImport as khimport
            set_framework_fn = getattr(khimport, "_set_current_framework", None)
        except Exception:
            set_framework_fn = None
    try:
        if callable(set_framework_fn):
            set_framework_fn(framework)
        return callback(data)
    finally:
        if callable(set_framework_fn):
            set_framework_fn(previous_framework)


def _index_strings_to_local_epoch_seconds(index) -> np.ndarray:
    """Convert YYYYMMDDHHMMSS-like index values to local epoch seconds in bulk."""
    idx_as_str = pd.Index(index).astype(str)
    if len(idx_as_str) == 0:
        return np.array([], dtype=np.int64)

    parsed = pd.to_datetime(idx_as_str, format="%Y%m%d%H%M%S", errors="raise")
    raw_seconds = (parsed.astype("int64") // 10**9).astype(np.int64)

    # pandas treats naive timestamps as UTC when converting to int64, while the
    # original code used datetime.timestamp() in the machine's local timezone.
    local_first = int(datetime.datetime.strptime(str(idx_as_str[0]), "%Y%m%d%H%M%S").timestamp())
    raw_first = int(raw_seconds[0])
    if local_first != raw_first:
        raw_seconds = raw_seconds + (local_first - raw_first)
    return raw_seconds


def _index_cache_key(index):
    if len(index) == 0:
        return (0, "", "")
    return (len(index), str(index[0]), str(index[-1]))


def _cached_index_seconds(index, cache: dict) -> np.ndarray:
    key = _index_cache_key(index)
    candidates = cache.get(key)
    if candidates:
        for cached_index, cached_seconds in candidates:
            if cached_index.equals(index):
                return cached_seconds

    seconds = _index_strings_to_local_epoch_seconds(index)
    cache.setdefault(key, []).append((pd.Index(index), seconds))
    return seconds


def _cached_index_time_map(index, cache: dict) -> dict:
    key = _index_cache_key(index)
    candidates = cache.get(key)
    if candidates:
        for cached_index, cached_map in candidates:
            if cached_index.equals(index):
                return cached_map

    seconds = _index_strings_to_local_epoch_seconds(index)
    time_idx_map = dict(zip(seconds.tolist(), range(len(seconds))))
    cache.setdefault(key, []).append((pd.Index(index), time_idx_map))
    return time_idx_map


def _normalize_daily_time_values(values) -> np.ndarray:
    """Normalize daily timestamps to local date-start epoch values."""
    normalized = []
    use_ms = None
    for value in values:
        try:
            if pd.isna(value):
                continue
        except Exception:
            pass

        try:
            if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool):
                int_value = int(value)
                value_is_ms = abs(int_value) > 10_000_000_000
                dt = datetime.datetime.fromtimestamp(int_value / 1000 if value_is_ms else int_value)
                if use_ms is None:
                    use_ms = value_is_ms
            else:
                ts = pd.Timestamp(value)
                if pd.isna(ts):
                    continue
                dt = ts.to_pydatetime()
                if use_ms is None:
                    use_ms = False
            day_start = datetime.datetime.combine(dt.date(), datetime.time())
            factor = 1000 if use_ms else 1
            normalized.append(int(day_start.timestamp() * factor))
        except Exception:
            continue
    return np.array(normalized, dtype=np.int64)

# 简单的GUI类，用于处理日志记录
class DummySignal:
    """虚拟信号类，用于非GUI模式下替代PyQt5信号"""
    def emit(self, *args, **kwargs):
        """空实现，忽略信号发射"""
        pass

class SimpleGUI:
    """简单的GUI类，用于处理日志记录，兼容纯代码运行模式"""
    def __init__(self):
        # 创建虚拟信号对象，避免属性访问错误
        self.progress_signal = DummySignal()
        self._progress_label = ""

    def log_message(self, message, level="INFO"):
        """记录日志消息"""
        print(f"[{level}] {datetime.datetime.now()} - {message}")

    def on_strategy_finished(self):
        """策略完成回调"""
        print(f"[INFO] {datetime.datetime.now()} - 策略执行完成")

    def set_progress_label(self, label: str):
        self._progress_label = label or ""
        if self._progress_label:
            print(f"[INFO] {datetime.datetime.now()} - {self._progress_label}")


# ============================================================
# 撮合引擎相关类
# ============================================================

class OrderStatus:
    """订单状态常量"""
    CREATED = "created"           # 已创建
    PENDING = "pending"           # 挂单中
    PARTIAL_FILLED = "partial"    # 部分成交
    FILLED = "filled"             # 全部成交
    CANCELLED = "cancelled"       # 已撤销
    REJECTED = "rejected"         # 已拒绝
    EXPIRED = "expired"           # 已过期


class PendingOrderManager:
    """挂单管理器

    负责管理限价单、止损单等需要等待成交的订单。
    支持跨Bar挂单、订单过期、部分成交等功能。
    """

    def __init__(self, config: dict = None):
        """初始化挂单管理器

        Args:
            config: 撮合引擎配置
        """
        self.config = config or {}
        self._pending_orders = {}  # {order_id: order_dict}
        self._order_counter = 0
        self._current_date = ""

    def add_order(self, signal: dict, time_info: dict) -> str:
        """添加挂单

        Args:
            signal: 交易信号
            time_info: 当前时间信息

        Returns:
            订单ID
        """
        self._order_counter += 1
        date_str = time_info.get("date", "").replace("-", "")
        order_id = f"P{date_str}{self._order_counter:06d}"

        order = {
            "order_id": order_id,
            "code": signal["code"],
            "action": signal["action"],
            "price": signal["price"],
            "volume": signal["volume"],
            "original_volume": signal["volume"],
            "remaining_volume": signal["volume"],
            "filled_volume": 0,
            "order_type": signal.get("order_type", "limit"),
            "time_in_force": signal.get("time_in_force", "day").lower(),
            "stop_price": signal.get("stop_price"),
            "expire_date": signal.get("expire_date"),
            "max_pending_bars": signal.get("max_pending_bars", 0),
            "bars_pending": 0,
            "status": OrderStatus.PENDING,
            "create_time": time_info.get("timestamp", 0),
            "create_date": time_info.get("date", ""),
            "reason": signal.get("reason", ""),
            "remark": signal.get("remark", ""),
            "fill_price": 0,
            "fill_timestamp": 0
        }

        self._pending_orders[order_id] = order
        print(f"[INFO] 挂单已添加 - {order_id} {signal['action']} {signal['code']} "
              f"限价:{signal['price']:.2f} 数量:{signal['volume']}")

        return order_id

    def check_pending_orders(self, market_data_dict: dict,
                              current_timestamp: int,
                              current_date: str,
                              match_config: dict) -> list:
        """检查挂单是否可成交

        Args:
            market_data_dict: {股票代码: 市场数据}
            current_timestamp: 当前时间戳
            current_date: 当前日期
            match_config: 撮合配置

        Returns:
            可成交的订单列表
        """
        filled_orders = []
        orders_to_remove = []

        for order_id, order in self._pending_orders.items():
            if order["status"] != OrderStatus.PENDING:
                continue

            code = order["code"]

            # 检查是否有该股票的市场数据
            if code not in market_data_dict:
                continue

            market_data = market_data_dict[code]
            if hasattr(market_data, 'to_dict'):
                market_data = market_data.to_dict()
            elif not isinstance(market_data, dict):
                continue

            # 跳过特殊键
            if code.startswith("__"):
                continue

            # 增加挂单Bar计数
            order["bars_pending"] += 1

            # 检查是否超过最大挂单Bar数
            max_bars = order["max_pending_bars"]
            if max_bars > 0 and order["bars_pending"] > max_bars:
                order["status"] = OrderStatus.EXPIRED
                orders_to_remove.append(order_id)
                print(f"[WARNING] 挂单过期(超过{max_bars}Bar) - {order_id} {code}")
                continue

            # 检查GTD过期
            if order["time_in_force"] == "gtd" and order["expire_date"]:
                if current_date > order["expire_date"]:
                    order["status"] = OrderStatus.EXPIRED
                    orders_to_remove.append(order_id)
                    print(f"[WARNING] 挂单过期(GTD) - {order_id} {code}")
                    continue

            # 停牌检测：volume=0 且 high==low==close 视为停牌，跳过
            bar_vol = market_data.get("volume", -1)
            if bar_vol == 0:
                bar_h = market_data.get("high", 0)
                bar_l = market_data.get("low", 0)
                bar_c = market_data.get("close", market_data.get("lastPrice", 0))
                if bar_h > 0 and abs(bar_h - bar_l) < 0.001 and abs(bar_h - bar_c) < 0.001:
                    continue

            # 根据订单类型检查是否可成交
            order_type = order["order_type"]

            if order_type == "limit":
                can_fill, fill_price = self._check_limit_order_fill(order, market_data)
            elif order_type == "stop":
                triggered = self._check_stop_trigger(order, market_data)
                if triggered:
                    # 止损单触发后转市价单，使用当前价成交
                    can_fill = True
                    fill_price = market_data.get("close", market_data.get("lastPrice", order["price"]))
                else:
                    can_fill = False
                    fill_price = 0
            elif order_type == "stop_limit":
                triggered = self._check_stop_trigger(order, market_data)
                if triggered:
                    # 止损限价单触发后转限价单
                    order["order_type"] = "limit"
                    can_fill, fill_price = self._check_limit_order_fill(order, market_data)
                else:
                    can_fill = False
                    fill_price = 0
            else:
                can_fill = False
                fill_price = 0

            if can_fill and fill_price > 0:
                # 涨跌停校验
                if match_config.get("price_limit", {}).get("enabled", True):
                    can_trade = self._check_price_limit_for_pending(order, market_data)
                    if not can_trade:
                        # 涨跌停导致无法成交，继续挂单
                        continue

                # 判断是否是开盘价成交
                bar_open = market_data.get("open", 0)
                is_open_price_fill = (bar_open > 0 and abs(fill_price - bar_open) < 0.001)
                fill_type = "开盘价" if is_open_price_fill else "限价"

                order["fill_price"] = fill_price
                order["fill_timestamp"] = current_timestamp
                order["status"] = OrderStatus.FILLED
                filled_orders.append(order.copy())
                orders_to_remove.append(order_id)
                print(f"[INFO] 挂单成交({fill_type}) - {order_id} {order['action']} {code} "
                      f"委托价:{order['price']:.2f} 成交价:{fill_price:.2f}")

        # 移除已成交或过期的订单
        for order_id in orders_to_remove:
            del self._pending_orders[order_id]

        return filled_orders

    def _check_limit_order_fill(self, order: dict, market_data: dict) -> tuple:
        """检查限价挂单是否可成交

        Args:
            order: 挂单信息
            market_data: 市场数据

        Returns:
            (是否可成交, 成交价格)
        """
        limit_price = order["price"]
        action = order["action"]
        code = order.get("code", "")

        bar_high = market_data.get("high", 0)
        bar_low = market_data.get("low", 0)
        bar_open = market_data.get("open", 0)

        # 调试日志
        logging.warning(f"[挂单检查] _check_limit_order_fill: {code} {action} 限价:{limit_price:.2f} "
              f"Bar数据: open={bar_open}, high={bar_high}, low={bar_low}")

        if bar_high <= 0 or bar_low <= 0:
            logging.warning(f"[挂单检查] Bar数据无效，返回False")
            return False, 0

        if action == "buy":
            # 买入挂单成交逻辑
            # 优先检查：委托价 >= 开盘价 → 按开盘价成交
            if bar_open > 0 and limit_price >= bar_open:
                can_fill = True
                fill_price = bar_open
                logging.warning(f"[挂单检查] 买入-开盘价成交: limit_price({limit_price}) >= bar_open({bar_open}), 成交价: {fill_price}")
            # 委托价 < 开盘价 → 检查是否触及限价
            elif bar_low <= limit_price:
                can_fill = True
                fill_price = limit_price
                logging.warning(f"[挂单检查] 买入-限价成交: bar_low({bar_low}) <= limit_price({limit_price}), 成交价: {fill_price}")
            else:
                can_fill = False
                fill_price = 0
                logging.warning(f"[挂单检查] 买入-不可成交: bar_low({bar_low}) > limit_price({limit_price})")

            if can_fill:
                return True, fill_price
        else:  # sell
            # 卖出挂单成交逻辑
            # 优先检查：委托价 <= 开盘价 → 按开盘价成交
            if bar_open > 0 and limit_price <= bar_open:
                can_fill = True
                fill_price = bar_open
                logging.warning(f"[挂单检查] 卖出-开盘价成交: limit_price({limit_price}) <= bar_open({bar_open}), 成交价: {fill_price}")
            # 委托价 > 开盘价 → 检查是否触及限价
            elif bar_high >= limit_price:
                can_fill = True
                fill_price = limit_price
                logging.warning(f"[挂单检查] 卖出-限价成交: bar_high({bar_high}) >= limit_price({limit_price}), 成交价: {fill_price}")
            else:
                can_fill = False
                fill_price = 0
                logging.warning(f"[挂单检查] 卖出-不可成交: bar_high({bar_high}) < limit_price({limit_price})")

            if can_fill:
                return True, fill_price

        logging.warning(f"[挂单检查] 不可成交，继续挂单")
        return False, 0

    def _check_stop_trigger(self, order: dict, market_data: dict) -> bool:
        """检查止损是否触发

        Args:
            order: 挂单信息
            market_data: 市场数据

        Returns:
            是否触发
        """
        stop_price = order.get("stop_price", 0)
        if stop_price <= 0:
            return False

        current_price = market_data.get("close", market_data.get("lastPrice", 0))
        bar_high = market_data.get("high", current_price)
        bar_low = market_data.get("low", current_price)

        if current_price <= 0:
            return False

        action = order["action"]

        if action == "buy":
            # 买入止损：最高价 >= 触发价
            return bar_high >= stop_price
        else:
            # 卖出止损：最低价 <= 触发价
            return bar_low <= stop_price

    def _get_limit_rate(self, code: str, market_data: dict = None) -> float:
        """根据股票代码和名称判断涨跌停幅度，支持ST股票5%

        Args:
            code: 股票代码
            market_data: 市场数据（可选，用于获取股票名称判断ST）

        Returns:
            涨跌停幅度
        """
        code_prefix = code[:3] if len(code) >= 3 else ""
        code_suffix = code[-2:].upper() if len(code) >= 2 else ""

        # 科创板：20%
        if code_prefix in ["688", "689"]:
            return 0.20
        # 创业板：20%
        if code_prefix in ["300", "301"]:
            return 0.20
        # 北交所：30%
        if code_prefix in ["43", "83", "87"] or code_suffix == "BJ":
            return 0.30

        # ST股票：5%（通过股票名称判断）
        if market_data:
            stock_name = market_data.get("stockName", market_data.get("stock_name", ""))
            if stock_name and ("ST" in stock_name.upper()):
                return 0.05

        # 主板/中小板：10%
        return 0.10

    def _check_price_limit_for_pending(self, order: dict, market_data: dict) -> bool:
        """检查挂单是否受涨跌停限制

        Args:
            order: 挂单信息
            market_data: 市场数据

        Returns:
            是否可交易
        """
        pre_close = market_data.get("preClose", market_data.get("pre_close", 0))
        if pre_close <= 0:
            return True

        code = order["code"]
        action = order["action"]

        limit_rate = self._get_limit_rate(code, market_data)
        limit_up = round(pre_close * (1 + limit_rate), 2)
        limit_down = round(pre_close * (1 - limit_rate), 2)
        limit_down = max(limit_down, 0.01)

        # 检查信号价格是否超出涨跌停范围
        order_price = order.get("price", 0)
        if order_price > 0:
            if action == "buy" and order_price > limit_up + 0.001:
                return False
            if action == "sell" and order_price < limit_down - 0.001:
                return False

        # 用 high/low 辅助判断封板状态（比仅用 close 更准确）
        bar_high = market_data.get("high", 0)
        bar_low = market_data.get("low", 0)
        current_price = market_data.get("close", market_data.get("lastPrice", 0))
        if current_price <= 0:
            return True

        # 一字板检测：开高低收全在涨/跌停价
        bar_open = market_data.get("open", 0)
        if bar_open > 0 and bar_high > 0 and bar_low > 0:
            if (abs(bar_open - limit_up) < 0.01 and abs(bar_high - limit_up) < 0.01
                    and abs(bar_low - limit_up) < 0.01):
                if action == "buy":
                    return False
            if (abs(bar_open - limit_down) < 0.01 and abs(bar_high - limit_down) < 0.01
                    and abs(bar_low - limit_down) < 0.01):
                if action == "sell":
                    return False

        # 收盘封板检测
        is_limit_up = current_price >= limit_up - 0.001
        is_limit_down = current_price <= limit_down + 0.001

        if action == "buy" and is_limit_up:
            return False
        if action == "sell" and is_limit_down:
            return False

        return True

    def expire_day_orders(self, current_date: str):
        """过期当日有效订单

        Args:
            current_date: 当前日期
        """
        orders_to_expire = []

        for order_id, order in self._pending_orders.items():
            if order["status"] != OrderStatus.PENDING:
                continue

            if order["time_in_force"] == "day":
                orders_to_expire.append(order_id)

        for order_id in orders_to_expire:
            order = self._pending_orders[order_id]
            order["status"] = OrderStatus.EXPIRED
            del self._pending_orders[order_id]
            print(f"[INFO] 挂单过期(当日有效) - {order_id} {order['code']}")

    def cancel_order(self, order_id: str) -> bool:
        """撤销挂单

        Args:
            order_id: 订单ID

        Returns:
            是否成功
        """
        if order_id in self._pending_orders:
            order = self._pending_orders[order_id]
            if order["status"] == OrderStatus.PENDING:
                order["status"] = OrderStatus.CANCELLED
                del self._pending_orders[order_id]
                print(f"[INFO] 挂单已撤销 - {order_id} {order['code']}")
                return True
        return False

    def cancel_orders_by_code(self, code: str) -> int:
        """撤销指定股票的所有挂单

        Args:
            code: 股票代码

        Returns:
            撤销数量
        """
        orders_to_cancel = [
            order_id for order_id, order in self._pending_orders.items()
            if order["code"] == code and order["status"] == OrderStatus.PENDING
        ]

        for order_id in orders_to_cancel:
            self.cancel_order(order_id)

        return len(orders_to_cancel)

    def cancel_all_orders(self) -> int:
        """撤销所有挂单

        Returns:
            撤销数量
        """
        orders_to_cancel = [
            order_id for order_id, order in self._pending_orders.items()
            if order["status"] == OrderStatus.PENDING
        ]

        for order_id in orders_to_cancel:
            self.cancel_order(order_id)

        return len(orders_to_cancel)

    def get_pending_orders(self, code: str = None) -> list:
        """获取挂单列表

        Args:
            code: 股票代码（可选）

        Returns:
            挂单列表
        """
        if code:
            return [
                order.copy() for order in self._pending_orders.values()
                if order["code"] == code and order["status"] == OrderStatus.PENDING
            ]
        else:
            return [
                order.copy() for order in self._pending_orders.values()
                if order["status"] == OrderStatus.PENDING
            ]

    def get_pending_summary(self) -> dict:
        """获取挂单汇总

        Returns:
            汇总信息
        """
        active_orders = [o for o in self._pending_orders.values() if o["status"] == OrderStatus.PENDING]

        summary = {
            "total_count": len(active_orders),
            "buy_count": sum(1 for o in active_orders if o["action"] == "buy"),
            "sell_count": sum(1 for o in active_orders if o["action"] == "sell"),
            "total_buy_volume": sum(o["remaining_volume"] for o in active_orders if o["action"] == "buy"),
            "total_sell_volume": sum(o["remaining_volume"] for o in active_orders if o["action"] == "sell"),
        }

        return summary


# 触发器基类
class TriggerBase:
    """触发器基类，定义触发机制的通用接口"""
    
    def __init__(self, framework):
        """初始化触发器
        
        Args:
            framework: KhQuantFramework实例
        """
        self.framework = framework
        
    def initialize(self):
        """初始化触发器"""
        pass
        
    def should_trigger(self, timestamp, data):
        """判断是否应该触发策略
        
        Args:
            timestamp: 当前时间戳
            data: 当前市场数据
            
        Returns:
            bool: 是否触发策略
        """
        return False
        
    def get_data_period(self):
        """获取数据周期，用于数据加载
        
        Returns:
            str: 数据周期，如"tick", "1m", "5m"等
        """
        return "tick"

# Tick触发器
class TickTrigger(TriggerBase):
    """Tick触发器，每个Tick都触发策略"""
    
    def should_trigger(self, timestamp, data):
        """判断是否应该触发策略
        
        Args:
            timestamp: 当前时间戳
            data: 当前市场数据
            
        Returns:
            bool: 是否触发策略
        """
        # Tick触发方式下，每个Tick都触发
        return True
        
    def get_data_period(self):
        """获取数据周期
        
        Returns:
            str: 数据周期
        """
        return "tick"

# K线触发器
class KLineTrigger(TriggerBase):
    """K线触发器，在K线形成时触发策略"""
    
    def __init__(self, framework, period):
        """初始化K线触发器
        
        Args:
            framework: KhQuantFramework实例
            period: K线周期，如"1m", "5m", "1d"等
        """
        super().__init__(framework)
        self.period = period  # "1m", "5m" 或 "1d"
        self.last_trigger_time = {}  # 记录每个股票上次触发时间
        self.last_trigger_date = None  # 记录上次触发的日期（用于日K线）
        
    def should_trigger(self, timestamp, data):
        """判断是否应该触发策略
        
        Args:
            timestamp: 当前时间戳
            data: 当前市场数据
            
        Returns:
            bool: 是否触发策略
        """
        # 优先使用主循环预写好的 datetime，避免重复 fromtimestamp 调用
        current_time = getattr(self.framework, '_current_bar_dt', None)
        if current_time is None:
            if isinstance(timestamp, str):
                try:
                    current_time = datetime.datetime.strptime(timestamp, "%Y%m%d%H%M%S")
                except:
                    current_time = datetime.datetime.now()
            else:
                try:
                    ts = float(timestamp)
                    current_time = datetime.datetime.fromtimestamp(ts / 1000 if ts > 1e10 else ts)
                except:
                    current_time = datetime.datetime.now()
        
        # 对于1分钟K线，在每分钟的开始触发
        if self.period == "1m":
            return current_time.second == 0
            
        # 对于5分钟K线，在每5分钟的开始触发
        elif self.period == "5m":
            return current_time.minute % 5 == 0 and current_time.second == 0
            
        # 对于日K线，每个交易日触发一次
        elif self.period == "1d":
            current_date = current_time.date()
            # 检查是否是新的一天（日K线只需要基于日期判断，无需考虑具体时间）
            if self.last_trigger_date != current_date:
                self.last_trigger_date = current_date
                return True
            return False
            
        return False
        
    def get_data_period(self):
        """获取数据周期
        
        Returns:
            str: 数据周期
        """
        return self.period


# 自定义定时触发器
class CustomTimeTrigger(TriggerBase):
    """自定义定时触发器，在指定的时间点触发策略"""
    
    def __init__(self, framework, custom_times):
        """初始化自定义定时触发器
        
        Args:
            framework: KhQuantFramework实例
            custom_times: 自定义触发时间点列表，格式为["09:30:00", "09:45:00", ...]
        """
        super().__init__(framework)
        # 解析时间字符串为秒数（从午夜开始）
        self.trigger_seconds = []
        for time_str in custom_times:
            h, m, s = map(int, time_str.split(':'))
            seconds = h * 3600 + m * 60 + s
            self.trigger_seconds.append(seconds)
        self.trigger_seconds.sort()
        
    def should_trigger(self, timestamp, data):
        """判断是否应该触发策略
        
        Args:
            timestamp: 当前时间戳
            data: 当前市场数据
            
        Returns:
            bool: 是否触发策略
        """
        # 优先使用主循环预写好的 datetime，避免重复 fromtimestamp 调用
        current_time = getattr(self.framework, '_current_bar_dt', None)
        if current_time is None:
            if isinstance(timestamp, str):
                try:
                    current_time = datetime.datetime.strptime(timestamp, "%Y%m%d%H%M%S")
                except:
                    current_time = datetime.datetime.now()
            else:
                try:
                    ts = float(timestamp)
                    current_time = datetime.datetime.fromtimestamp(ts / 1000 if ts > 1e10 else ts)
                except:
                    current_time = datetime.datetime.now()
        
        # 计算当前时间的秒数（从午夜开始）
        current_seconds = current_time.hour * 3600 + current_time.minute * 60 + current_time.second
        
        # 检查是否接近任一触发时间点（允许5秒误差）
        for trigger_second in self.trigger_seconds:
            if abs(current_seconds - trigger_second) < 5:
                return True
                
        return False
        
    def get_data_period(self):
        """获取数据周期
        
        Returns:
            str: 数据周期
        """
        # 自定义触发使用1秒级数据
        return "1s"

# 触发器工厂
class TriggerFactory:
    """触发器工厂，用于创建不同类型的触发器"""
    
    @staticmethod
    def create_trigger(framework, config):
        """创建触发器
        
        Args:
            framework: KhQuantFramework实例
            config: 配置字典
            
        Returns:
            TriggerBase: 触发器实例
        """
        trigger_type = config.get("backtest", {}).get("trigger", {}).get("type", "tick")
        
        if trigger_type == "tick":
            return TickTrigger(framework)
        elif trigger_type == "1m":
            return KLineTrigger(framework, "1m")
        elif trigger_type == "5m":
            return KLineTrigger(framework, "5m")
        elif trigger_type == "1d":
            return KLineTrigger(framework, "1d")
        elif trigger_type == "custom":
            custom_times = config.get("backtest", {}).get("trigger", {}).get("custom_times", [])
            return CustomTimeTrigger(framework, custom_times)
        else:
            # 默认使用Tick触发
            return TickTrigger(framework)

class MyTraderCallback:
    def __init__(self, gui=None):
        super().__init__()
        self.gui = gui
        self.price_decimals = 2  # 默认价格精度，会在回测开始时根据股票池类型更新
        logging.info("交易回调已初始化")
    
    def set_price_decimals(self, decimals: int):
        """设置价格精度"""
        self.price_decimals = decimals
    
    def on_stock_order(self, order):
        """委托回报推送"""
        try:
            direction_map = {
                xtconstant.STOCK_BUY: '买入',
                xtconstant.STOCK_SELL: '卖出'
            }
            
            status_map = {
                0: '已提交',
                1: '已接受',
                2: '已拒绝',
                3: '已撤销',
                4: '已成交',
                5: '部分成交'
            }
            
            # 格式化时间戳 (从order对象获取, 通常是Unix时间戳)
            formatted_time = "未知"
            order_time_val = getattr(order, 'order_time', None)
            if order_time_val:
                try:
                    timestamp = float(order_time_val)
                    # 检查并转换毫秒级时间戳
                    if timestamp > 1e10:
                        timestamp = timestamp / 1000
                    formatted_time = datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
                except ValueError:
                    # 如果不是数字时间戳，尝试解析字符串
                    try:
                        formatted_time = datetime.datetime.strptime(str(order_time_val), '%Y%m%d%H%M%S').strftime('%Y-%m-%d %H:%M:%S')
                    except:
                        formatted_time = str(order_time_val) # 解析失败则直接显示原始值
                except Exception as e:
                    formatted_time = f"时间转换错误: {e}"
            
            decimals = self.price_decimals
            order_msg = (
                f"委托信息 - "
                f"时间: {formatted_time} | "
                f"股票代码: {get_stock_display(order.stock_code)} | "
                f"方向: {direction_map.get(order.order_type, '未知')} | "
                f"委托价格: {order.price:.{decimals}f} | "
                f"数量: {order.order_volume} | "
                f"委托编号: {order.order_id} | "
                f"原因: {order.status_msg or '策略交易'}"
            )
            
            logging.info("[TRADE] " + str(order_msg))
            print(datetime.datetime.now(), '委托回调', order.order_remark)
            
        except Exception as e:
            logging.error(f"处理委托回报时出错: {str(e)}")

    def on_stock_trade(self, trade):
        """成交回报推送"""
        try:
            direction_map = {
                xtconstant.STOCK_BUY: '买入',
                xtconstant.STOCK_SELL: '卖出'
            }
            
            # 获取实际成交价格
            actual_price = getattr(trade, 'actual_price', trade.traded_price)
            
            # 格式化时间戳 (从trade对象获取, 通常是Unix时间戳)
            formatted_time = "未知"
            traded_time_val = getattr(trade, 'traded_time', None)
            if traded_time_val:
                try:
                    timestamp = float(traded_time_val)
                    # 检查并转换毫秒级时间戳
                    if timestamp > 1e10:
                        timestamp = timestamp / 1000
                    formatted_time = datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
                except ValueError:
                    # 如果不是数字时间戳，尝试解析字符串
                    try:
                        formatted_time = datetime.datetime.strptime(str(traded_time_val), '%Y%m%d%H%M%S').strftime('%Y-%m-%d %H:%M:%S')
                    except:
                        formatted_time = str(traded_time_val) # 解析失败则直接显示原始值
                except Exception as e:
                    formatted_time = f"时间转换错误: {e}"
            
            decimals = self.price_decimals
            trade_msg = (
                f"成交信息 - "
                f"时间: {formatted_time} | "
                f"股票代码: {get_stock_display(trade.stock_code)} | "
                f"方向: {direction_map.get(trade.order_type, '未知')} | "
                f"实际成交价: {actual_price:.{decimals}f} | "
                f"成交数量: {trade.traded_volume} | "
                f"成交金额: {trade.traded_amount:.{decimals}f} | "
                f"成交编号: {trade.traded_id} | "
                f"原因: {trade.order_remark or '策略交易'}"
            )
            
            logging.info("[TRADE] " + str(trade_msg))
            print(datetime.datetime.now(), '成交回调', trade.order_remark)
            
        except Exception as e:
            logging.error(f"处理成交回报时出错: {str(e)}")
    
    def on_order_error(self, order_error):
        """委托错误回报推送"""
        try:
            error_msg = (
                f"委托错误 - "
                f"股票代码: {get_stock_display(order_error.stock_code)} | "
                f"错误代码: {order_error.error_id} | "
                f"错误信息: {order_error.error_msg} | "
                f"备注: {order_error.order_remark}"
            )
            
            level = logging.WARNING if getattr(order_error, "error_id", None) == -1 else logging.ERROR
            logging.log(level, error_msg)
            print(f"委托报错回调 {order_error.order_remark} {order_error.error_msg}")
            
        except Exception as e:
            logging.error(f"处理委托错误时出错: {str(e)}")
    
    def on_cancel_error(self, cancel_error):
        """撤单错误回报推送"""
        try:
            error_msg = (
                f"撤单错误 - "
                f"委托编号: {cancel_error.order_id} | "
                f"错误代码: {cancel_error.error_id} | "
                f"错误信息: {cancel_error.error_msg}"
            )
            
            logging.error(error_msg)
            print(datetime.datetime.now(), sys._getframe().f_code.co_name)
            
        except Exception as e:
            logging.error(f"处理撤单错误时出错: {str(e)}")
            

    def on_stock_position(self, position):
        """持仓变动推送"""
        try:
            # 只记录重要的持仓变动
            decimals = self.price_decimals
            msg = (
                f"持仓变动 - "
                f"股票代码: {get_stock_display(position.stock_code)} | "
                f"持仓数量: {position.volume} | "
                f"最新价格: {getattr(position, 'current_price', 0):.{decimals}f} | "
                f"持仓市值: {getattr(position, 'market_value', 0):.{decimals}f} | "
                f"持仓盈亏: {getattr(position, 'profit', 0):.{decimals}f}"
            )
            logging.info(msg)
        except Exception as e:
            logging.error(f"处理持仓变动时出错: {str(e)}")


    def on_stock_asset(self, asset):
        """资金变动推送"""
        '''
        try:
            decimals = self.price_decimals
            msg = (
                f"资金变动 - "
                f"账户: {asset.account_id} | "
                f"可用资金: {asset.cash:.{decimals}f} | "
                f"总资产: {asset.total_asset:.{decimals}f}"
            )
            logging.info(msg)
            print("资金变动推送on asset callback")
            print(asset.account_id, asset.cash, asset.total_asset)
        except Exception as e:
            logging.error(f"处理资金变动时出错: {str(e)}")
            '''

class KhQuantFramework:
    """量化交易框架主类"""

    def _get_writable_backtest_dir(self) -> str:
        """返回可写的回测结果目录。"""
        return get_backtest_results_dir(create=True)


    def __init__(self, config_path: str, strategy_file: str, trader_callback=None, ui_settings=None):
        """初始化框架
        
        Args:
            config_path: 配置文件路径
            strategy_file: 策略文件路径
            trader_callback: 交易回调函数
            ui_settings: 字典形式传递的界面配置参数
        """
        print(f"[DEBUG] KhQuantFramework.__init__ 开始")
        print(f"[DEBUG] config_path: {config_path}")
        print(f"[DEBUG] strategy_file: {strategy_file}")
        
        self.ui_settings = dict(ui_settings or {})
        if not self.ui_settings.get("duckdb_data_path"):
            try:
                from kh_settings import load as load_cli_settings
                cfg = load_cli_settings()
                if cfg.get("duckdb_data_path"):
                    self.ui_settings["duckdb_data_path"] = cfg.get("duckdb_data_path")
            except Exception:
                pass
        # 开源版只读 DuckDB，调用方传入的其他数据源名一律忽略
        self.ui_settings["backtest_data_source"] = "duckdb"
        
        self.config_path = config_path
        self.config = KhConfig(config_path)
        raw_strategy_file = strategy_file or self.config.config_dict.get("strategy_file", "")
        self.strategy_file = resolve_strategy_file(
            raw_strategy_file,
            config_path=self.config_path,
            extra_base_dirs=[os.path.dirname(os.path.abspath(__file__))],
        )
        self.is_running = False  # 运行状态标识
        self.save_results_on_stop = True  # 停止时是否保存结果（默认True保持向后兼容）
        self.external_stop_requested = False
        self._duckdb_lock_skipped = []
        self.account = None  # 账户对象
        self.strategy_module = None  # 策略模块
        self.trade_mgr = KhTradeManager(self.config)  # 交易管理器
        self.risk_mgr = KhRiskManager(self.config)  # 风险管理器
        self.tools = KhQuTools()  # 工具类
        self.backtest_records = {}  # 回测记录
        self.daily_price_cache = {}  # 日线价格缓存，用于存储所有股票的日线数据
        self._cached_benchmark_close = {}  # 基准指数收盘价缓存
        self._benchmark_initial_price = None  # 基准初始价格（回测第一天）

        backtest_config = self.config.config_dict.get("backtest", {})
        self._etf_total_return_enabled = bool(
            backtest_config.get("etf_total_return_accounting", False)
        )
        if self._etf_total_return_enabled and self.config.kline_period != "1d":
            raise ValueError("etf_total_return_accounting currently requires 1d data")
        self._etf_total_return_state = {}
        self._etf_raw_time_idx_cache = {}
        self._etf_distribution_total = 0.0
        self._etf_distribution_today = 0.0
        self._etf_distribution_date = None
        self._etf_total_return_fallbacks = 0

        # T+0交易模式标识（默认关闭，在run()中根据股票池判断）
        self.t0_mode = False
        
        # 添加运行时间记录变量
        self.start_time = None  # 策略开始运行时间
        self.end_time = None    # 策略结束运行时间
        self.total_runtime = 0  # 总运行时间（秒）
        
        # 添加简单的GUI属性用于日志记录
        self.gui = SimpleGUI()
        
        # 加载策略模块
        print(f"[DEBUG] 准备加载策略模块: {self.strategy_file}")
        try:
            self.strategy_module = self.load_strategy(self.strategy_file)
            print(f"[DEBUG] 策略模块加载成功")
        except Exception as e:
            print(f"[DEBUG] 策略模块加载失败: {str(e)}")
            import traceback
            traceback.print_exc()
            raise
        
        # 当前运行模式
        self.run_mode = self.config.run_mode
        
        self.trader_callback = trader_callback  # 保存交易回调函数
        
        # 创建触发器
        self.trigger = TriggerFactory.create_trigger(self, self.config.config_dict)
        trigger_config = self.config.config_dict.get("backtest", {}).get("trigger", {})
        try:
            self.daily_trigger_cap = int(trigger_config.get("daily_trigger_cap", 1) or 1)
        except Exception:
            self.daily_trigger_cap = 1
        if self.daily_trigger_cap < 1:
            self.daily_trigger_cap = 1
        self.daily_trigger_date = None
        self.daily_trigger_count = 0
        self._daily_trigger_requested = False
        
        # 初始化各个模块
        self.trade_mgr = KhTradeManager(self.config)
        self.risk_mgr = KhRiskManager(self.config) 
        self.tools = KhQuTools()
        
        # 交易账户
        self.account = None
        # 交易回调
        self.callback = None
        
        # 初始化交易管理器
        self.trade_mgr = KhTradeManager(self.config, self)
        
        # 清除可能存在的历史数据缓存，确保每次运行都是干净的状态
        if hasattr(self, 'historical_data_ref'):
            delattr(self, 'historical_data_ref')
        if hasattr(self, 'time_field_cache'):
            delattr(self, 'time_field_cache')
        if hasattr(self, 'time_idx_cache'):
            delattr(self, 'time_idx_cache')
        
        # 初始化风控管理器
        self.risk_mgr = KhRiskManager(self.config)

        # ===== 撮合引擎相关属性 =====
        # v3.1.2: 撮合引擎始终启用，不再可选
        self.match_engine_enabled = True  # 保留此字段以兼容旧代码，始终为True

        # 从传入的配置读取成交量限制配置
        volume_limit_enabled = self.ui_settings.get('volume_limit_enabled', False)
        participation_rate = self.ui_settings.get('participation_rate', 0.1)
        allow_partial_fill = self.ui_settings.get('allow_partial_fill', True)

        self._match_config = {
            "price_limit": {
                "enabled": True,  # 涨跌停校验始终启用
                "reject_on_limit": True
            },
            "volume_limit": {
                "enabled": volume_limit_enabled,
                "participation_rate": participation_rate,
                "allow_partial_fill": allow_partial_fill,
                "min_volume": 100  # 最小成交量（手）
            }
        }

        # 初始化挂单管理器（始终创建）
        self.pending_order_mgr = PendingOrderManager(self._match_config)

        # 预初始化 record_results 用到的缓存字典，避免每次调用做 hasattr 守卫检查
        self._cached_timestamp   = {}
        self._cached_trade_days  = {}
        self._cached_time_points = {}
        self._cached_daily_times = {}

        # 缓存日志信息，稍后在 run() 方法中输出（此时 trader_callback 可能还未完全初始化）
        self._pending_log_messages = []
        volume_status = "启用" if self._match_config["volume_limit"]["enabled"] else "关闭"
        participation_rate_pct = self._match_config["volume_limit"]["participation_rate"] * 100
        self._pending_log_messages.append(f"撮合引擎配置 - 成交量限制: {volume_status}")
        if self._match_config["volume_limit"]["enabled"]:
            self._pending_log_messages.append(f"  市场参与率: {participation_rate_pct:.1f}%")
            allow_partial = "是" if self._match_config["volume_limit"]["allow_partial_fill"] else "否"
            self._pending_log_messages.append(f"  允许部分成交: {allow_partial}")

        # 初始化数据源管理器（从设置中读取配置）
        self._init_data_source()

    def _init_data_source(self):
        """初始化数据源管理器（开源版只读 DuckDB）"""
        duckdb_path = self.ui_settings.get('duckdb_data_path', '')
        explicit_duckdb_path = bool(str(duckdb_path).strip()) if duckdb_path is not None else False

        try:
            if duckdb_path:
                duckdb_path = os.path.abspath(os.path.expandvars(os.path.expanduser(str(duckdb_path).strip())))

            if not duckdb_path:
                from kh_platform import default_duckdb_dir
                duckdb_path = default_duckdb_dir()
                self._log(f"警告: 未设置DuckDB数据路径，将使用默认路径: {duckdb_path}", "WARNING")
            elif not os.path.exists(duckdb_path):
                raise FileNotFoundError(f"DuckDB数据路径不存在: {duckdb_path}")

            self.data_source_mgr = init_data_source_manager(
                data_source='duckdb',
                duckdb_path=duckdb_path,
            )
            self._log(f"数据源初始化完成: {self.data_source_mgr.source_name}", "INFO")

        except Exception as e:
            self._log(f"初始化数据源失败: {str(e)}", "WARNING")
            if explicit_duckdb_path:
                raise RuntimeError(
                    f"DuckDB数据源初始化失败，请检查已设置的数据目录: {duckdb_path}"
                ) from e
            from kh_platform import default_duckdb_dir
            self.data_source_mgr = init_data_source_manager(
                data_source='duckdb',
                duckdb_path=default_duckdb_dir()
            )
            self._log("已切换到默认 DuckDB 数据目录", "INFO")

    def _cleanup_runtime_state(self, stage: str):
        """清理跨回测残留的全局缓存和 DuckDB 连接池。"""
        try:
            import khQTTools as _khqt
            if hasattr(_khqt, "clear_khDuckDB_cache"):
                _khqt.clear_khDuckDB_cache()
            if hasattr(_khqt, "clear_khHistory_cache"):
                _khqt.clear_khHistory_cache()
        except Exception:
            pass

        for attr_name in ["historical_data_ref", "time_field_cache", "time_idx_cache", "all_times"]:
            if hasattr(self, attr_name):
                try:
                    delattr(self, attr_name)
                except Exception:
                    pass

        try:
            if getattr(getattr(self, "data_source_mgr", None), "data_source", "") == "duckdb":
                duckdb_path = getattr(self.data_source_mgr, "duckdb_path", None)
                if stage == "run_end":
                    reset_data_source_manager()
                else:
                    from duckdb_storage import xtdata_adapter
                    xtdata_adapter.reset_manager()
                    xtdata_adapter.set_data_root(duckdb_path)
        except Exception:
            pass

        try:
            gc.collect()
        except Exception:
            pass

    def _log(self, message, level="INFO"):
        """使用标准logging输出日志"""
        if level == "INFO":
            logging.info(message)
        elif level == "WARNING":
            logging.warning(message)
        elif level == "ERROR":
            logging.error(message)
        else:
            logging.log(logging.INFO, f"[{level}] {message}")

    def _extract_strategy_error_context(self, error: Exception) -> Dict[str, Any]:
        """从策略异常栈中提取股票、时间等上下文，便于生成用户可读提示。"""
        context: Dict[str, Any] = {}
        tb = getattr(error, "__traceback__", None)
        while tb:
            local_vars = tb.tb_frame.f_locals
            for name in ("sc", "stock_code", "code"):
                value = local_vars.get(name)
                if value and "stock_code" not in context:
                    context["stock_code"] = str(value)
            for name in ("dt", "current_time", "current_date_str"):
                value = local_vars.get(name)
                if value and "strategy_time" not in context:
                    context["strategy_time"] = str(value)
            tb = tb.tb_next
        return context

    def _format_strategy_key_error(
        self,
        error: KeyError,
        current_data: Dict,
        time_info: Dict,
    ) -> Optional[str]:
        """将常见行情字段 KeyError 转成缺数据提示。"""
        if not error.args:
            return None

        field = str(error.args[0]).strip("'\"")
        market_fields = {
            "open", "high", "low", "close", "volume", "amount",
            "preClose", "lastPrice", "time", "datetime",
        }
        if field not in market_fields:
            return None

        ctx = self._extract_strategy_error_context(error)
        stock_code = ctx.get("stock_code")
        if not stock_code:
            empty_codes = []
            for code, value in current_data.items():
                if str(code).startswith("__"):
                    continue
                if hasattr(value, "empty") and value.empty:
                    empty_codes.append(code)
            if empty_codes:
                stock_code = empty_codes[0]

        current_time_str = (
            ctx.get("strategy_time")
            or time_info.get("datetime")
            or str(time_info.get("timestamp", ""))
        )
        period = self.config.config_dict.get("data", {}).get("kline_period", "")
        dividend_type = self.config.config_dict.get("data", {}).get("dividend_type", "")

        stock_hint = f"\n疑似股票: {stock_code}" if stock_code else ""
        return (
            f"策略访问行情字段 '{field}' 失败，疑似该股票在当前回测时间缺少对应历史数据或字段。"
            f"\n当前时间: {current_time_str}{stock_hint}"
            f"\n数据周期: {period or '未配置'}，复权口径: {dividend_type or '未配置'}"
            "\n建议检查数据管理模块中该股票/周期的数据是否覆盖回测区间；"
            "如果策略调用了 khHistory，也建议在策略中同时判断 DataFrame 是否为空以及字段是否存在。"
        )

    def _should_log(self):
        """检查是否应该输出日志（用于性能优化）

        始终返回True，保持兼容性
        """
        return True

    def _cache_should_log(self):
        """在回测开始时缓存日志开关状态（保持兼容性）"""
        pass

    def _rebuild_data_cache(self, chunk_data: Dict):
        """重建数据缓存（用于动态加载模式分段切换时）

        Args:
            chunk_data: 当前分段的数据 {股票代码: DataFrame}
        """
        # 清除旧缓存
        self.historical_data_ref = {}
        self.time_field_cache = {}
        self.time_idx_cache = {}
        index_time_map_cache = {}
        use_searchsorted_time_index = _use_searchsorted_time_index(self.config)

        # 构建新缓存
        for code, df in chunk_data.items():
            if not isinstance(df, pd.DataFrame):
                continue

            # 找到时间字段
            time_field_found = False
            for field in ['time', 'timestamp', 'date', 'datetime']:
                if field in df.columns:
                    self.time_field_cache[code] = field
                    self.historical_data_ref[code] = df

                    # 构建时间索引映射
                    period_for_cache = str(
                        self.config.config_dict.get("data", {}).get("kline_period", "")
                    ).lower()
                    time_values = (
                        _normalize_daily_time_values(df[field].values)
                        if period_for_cache == "1d" else df[field].values
                    )
                    self.time_idx_cache[code] = _build_time_index_cache_entry(
                        time_values,
                        use_searchsorted_time_index,
                    )
                    time_field_found = True
                    break

            # 如果没有找到时间字段，尝试使用索引（适配 DuckDB 5m/1m 数据）
            if not time_field_found:
                if isinstance(df.index, (pd.Index, pd.RangeIndex)) and len(df.index) > 0:
                    try:
                        first_idx = str(df.index[0])
                        if len(first_idx) == 14 and first_idx.isdigit():
                            # 字符串格式的时间索引
                            self.time_field_cache[code] = '__index__'
                            self.historical_data_ref[code] = df

                            # 构建索引映射
                            if use_searchsorted_time_index:
                                self.time_idx_cache[code] = _build_time_index_cache_entry(
                                    _cached_index_seconds(df.index, index_time_map_cache),
                                    True,
                                )
                            else:
                                time_idx_map = _cached_index_time_map(df.index, index_time_map_cache)
                                self.time_idx_cache[code] = time_idx_map
                    except Exception:
                        pass  # 忽略错误，跳过该股票

    def _reset_daily_trigger_state(self, date_str: str):
        if self.daily_trigger_date != date_str:
            self.daily_trigger_date = date_str
            self.daily_trigger_count = 0
            self._daily_trigger_requested = False

    def request_next_daily_trigger(self) -> bool:
        trigger_type = self.config.config_dict.get("backtest", {}).get("trigger", {}).get("type", "tick")
        if trigger_type != "1d":
            return False
        if self.daily_trigger_count >= self.daily_trigger_cap:
            return False
        self._daily_trigger_requested = True
        return True

    def load_strategy(self, strategy_file: str):
        """动态加载策略模块

        Args:
            strategy_file: 策略文件路径

        Returns:
            module: 策略模块
        """
        print(f"[DEBUG] load_strategy 被调用，参数: {strategy_file}")
        import importlib.util
        import sys
        import os

        # 获取策略文件的绝对路径
        strategy_file = resolve_strategy_file(
            strategy_file,
            config_path=getattr(self, "config_path", None),
            extra_base_dirs=[os.path.dirname(os.path.abspath(__file__))],
        )
        self.strategy_file = strategy_file
        print(f"[DEBUG] 策略文件绝对路径: {strategy_file}")

        # 使用策略文件的实际文件名作为模块名（不含.py扩展名）
        # 这样debugpy可以正确识别模块
        module_name = os.path.splitext(os.path.basename(strategy_file))[0]
        print(f"[DEBUG] 模块名: {module_name}")

        # ⭐ 将策略所在目录添加到 sys.path（确保能导入同目录的库文件）
        strategy_dir = os.path.dirname(strategy_file)
        if strategy_dir not in sys.path:
            sys.path.insert(0, strategy_dir)
            print(f"[DEBUG] 已将策略目录添加到sys.path: {strategy_dir}")

        # 创建模块规范
        spec = importlib.util.spec_from_file_location(module_name, strategy_file)
        print(f"[DEBUG] spec 创建成功")

        # 创建模块对象
        strategy_module = importlib.util.module_from_spec(spec)
        print(f"[DEBUG] 模块对象创建成功")

        # 将模块添加到sys.modules，这样debugpy可以找到它
        # 这是让VSCode断点生效的关键！
        sys.modules[module_name] = strategy_module
        print(f"[DEBUG] 模块已添加到sys.modules: {module_name}")

        # 确保模块的__file__属性指向正确的源文件
        strategy_module.__file__ = strategy_file
        print(f"[DEBUG] 模块__file__属性: {strategy_module.__file__}")

        # 执行模块代码
        print(f"[DEBUG] 准备执行模块代码")
        spec.loader.exec_module(strategy_module)
        print(f"[DEBUG] 模块代码执行完成")

        return strategy_module
        
    def init_trader_and_account(self):
        """初始化交易接口和账户"""
        # 固定为回测模式，只进行虚拟账户初始化
        self._init_virtual_account()
        # 在回测模式下也设置回调
        if self.trader_callback:
            self.trade_mgr.callback = self.trader_callback
        
    def _init_virtual_account(self):
        """初始化虚拟账户"""
        # 创建虚拟账户对象
        self.account = StockAccount(
            self.config.account_id,
            self.config.account_type
        )

        # 获取基准合约并标准化格式（支持 sh.000300 和 000300.SH 两种格式）
        import khQTTools
        original_benchmark = self.config.config_dict["backtest"].get("benchmark", "000300.SH")
        # 使用标准化函数统一转换为标准格式
        self.benchmark = khQTTools.normalize_stock_code(original_benchmark)

        # 更新配置字典中的基准指数代码
        self.config.config_dict["backtest"]["benchmark"] = self.benchmark
        
        # 从回测配置中获取初始资金
        init_capital = self.config.config_dict["backtest"]["init_capital"]
        
        # 初始化资产字典
        self.trade_mgr.assets = {
            "account_type": xtconstant.SECURITY_ACCOUNT,
            "account_id": self.config.account_id,
            "cash": init_capital,
            "frozen_cash": 0.0,
            "market_value": 0.0,
            "total_asset": init_capital,
            "benchmark": self.benchmark
        }
        
        # 初始化持仓字典
        self.trade_mgr.positions = {}  # 初始持仓为空
        
        # 初始化委托字典
        self.trade_mgr.orders = {}  # 初始委托为空
        
        # 初始化成交字典
        self.trade_mgr.trades = {}  # 初始成交为空
        
        print(f"虚拟账户初始化完成: {self.config.account_id}")
        print(f"初始资产: {self.trade_mgr.assets}")
        print(f"基准合约: {self.benchmark}")
        
        
    def init_data(self):
        """初始化行情数据。开源版只读本地 DuckDB，数据由用户事先下载，这里无需下载。"""
        logging.info("使用DuckDB本地数据源，跳过数据下载")
            

    def run(self):
        """启动框架"""
        self.start_time = time.time()
        start_datetime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        logging.info(f"策略开始运行时间: {start_datetime}")
        logging.info("开始初始化交易接口和数据...")
        
        try:
            self._cleanup_runtime_state(stage="run_start")
            # 初始化
            init_start = time.time()
            self.init_trader_and_account() # 初始化交易接口和账户
            init_time = time.time() - init_start
            
            logging.info(f"交易接口初始化耗时: {init_time:.2f}秒")
            
            # 初始化缓存
            self.daily_price_cache = {}
            self._cached_benchmark_close = {}
            self._benchmark_initial_price = None  # 重置基准初始价格

            # 直接从设置界面读取是否初始化数据的配置
            init_data_enabled = self.ui_settings.get('init_data_enabled', True)
            
            logging.info(f"数据初始化设置: {'启用' if init_data_enabled else '禁用'}")
            
            if init_data_enabled:
                data_init_start = time.time()
                logging.info("开始初始化行情数据...")
                self.init_data() # 初始化行情数据
                data_init_time = time.time() - data_init_start
                
                logging.info(f"数据初始化耗时: {data_init_time:.2f}秒")
            else:
                logging.info("跳过数据初始化（根据设置禁用）")
            
            # 读取股票列表
            stock_list_start = time.time()
            stock_codes = self.get_stock_list()
            stock_list_time = time.time() - stock_list_start
            
            logging.info(f"股票列表加载耗时: {stock_list_time:.2f}秒")
            
            # 判断股票池类型并设置价格精度
            self.pool_type, self.price_decimals = determine_pool_type(stock_codes)
            # 将精度设置传递给交易管理器
            self.trade_mgr.set_price_decimals(self.price_decimals)
            # 将精度设置传递给回调对象（用于日志格式化）
            if self.trader_callback and hasattr(self.trader_callback, 'set_price_decimals'):
                self.trader_callback.set_price_decimals(self.price_decimals)
            
            # 记录股票池类型信息
            pool_type_names = {
                'stock_only': '纯股票',
                'etf_only': '纯场内基金',  # 兼容历史键名，包含 ETF/LOF 等
                'mixed': '股票+ETF混合'
            }
            logging.info(f"股票池类型: {pool_type_names.get(self.pool_type, self.pool_type)}, "
                f"价格精度: {self.price_decimals}位小数")
            
            # ==================== T+0模式检验 ====================
            t0_support_type, self.t0_mode = check_t0_support(stock_codes)
            
            # 将T+0模式设置传递给交易管理器
            self.trade_mgr.set_t0_mode(self.t0_mode)
            
            if t0_support_type == 'all_t0':
                # 全部支持T+0，进入T+0模式
                logging.info("T+0交易模式已启用 - 股票池中全部为T0型ETF，支持当日买入当日卖出")
                if self.trader_callback:
                    # 通知GUI更新显示（使用try-except防止跨线程调用导致崩溃）
                    try:
                        cb = self.ui_settings.get('set_t0_mode_display_callback')
                        if cb:
                            cb(True)
                    except Exception as e:
                        logging.warning(f"更新T+0模式显示时出错: {e}")
            elif t0_support_type == 'mixed':
                # 混合池，弹窗提醒
                t0_details = get_t0_details(stock_codes)
                warning_msg = (
                    f"股票池中包含混合品种：\n"
                    f"- 支持T+0的ETF: {t0_details['t0_count']}只\n"
                    f"- 不支持T+0的品种: {len(t0_details['non_t0_stocks'])}只\n\n"
                    f"系统将使用T+1模式运行。如需使用T+0模式，请确保股票池中只包含T0型ETF。"
                )
                logging.warning(f"T+0模式未启用：股票池包含{t0_details['t0_count']}只T0型ETF和{len(t0_details['non_t0_stocks'])}只非T0品种")
                if self.trader_callback:
                    # 显示警告弹窗（使用try-except防止跨线程调用导致崩溃）
                    try:
                        cb = self.ui_settings.get('show_t0_warning_callback')
                        if cb:
                            cb(warning_msg)
                    except Exception as e:
                        logging.warning(f"显示T+0警告弹窗时出错: {e}")
            else:
                # 全部不支持T+0，正常T+1模式
                logging.info("交易模式: T+1（标准A股交易规则）")
            
            # 准备初始化数据结构，包含时间、账户、持仓、股票池等信息
            init_data = {
                "__current_time__": {
                    "timestamp": int(time.time()),
                    "datetime": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "date": datetime.datetime.now().strftime("%Y-%m-%d"),
                    "time": datetime.datetime.now().strftime("%H:%M:%S")
                },
                "__account__": self.trade_mgr.assets,
                "__positions__": self.trade_mgr.positions,
                "__stock_list__": stock_codes,
                "__framework__": self
            }
            
            # 调用策略初始化函数，并传递完整数据结构
            strategy_init_start = time.time()
            import khQuantImport as _khimport_init
            _set_framework_init_fn = getattr(_khimport_init, "_set_current_framework", None)
            try:
                if callable(_set_framework_init_fn):
                    _set_framework_init_fn(self)
                self.strategy_module.init(stock_codes, init_data)
            finally:
                if callable(_set_framework_init_fn):
                    _set_framework_init_fn(None)
            strategy_init_time = time.time() - strategy_init_start
            
            logging.info(f"策略初始化耗时: {strategy_init_time:.2f}秒")
            
            self.is_running = True
            
            preprocess_time = time.time() - self.start_time
            logging.info(f"预处理阶段总耗时: {preprocess_time:.2f}秒")
            logging.info("开始执行策略主逻辑...")
            
            # 固定运行回测模式
            strategy_start = time.time()
            self._run_backtest()
            strategy_time = time.time() - strategy_start
            
            logging.info(f"策略主逻辑执行耗时: {strategy_time:.2f}秒")
            self.is_running = False

        except Exception as e:
            error_msg = "框架运行异常: " + str(e)
            logging.error(error_msg, exc_info=True)
            # 调用错误回调函数
            logging.error(error_msg)
            raise  # 重新抛出异常
            
        finally:
            self.end_time = time.time()
            self.total_runtime = self.end_time - self.start_time
            end_datetime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            logging.info(f"策略结束运行时间: {end_datetime}")
            logging.info(f"策略总运行时长: {self.total_runtime:.2f}秒")
            
            hours = int(self.total_runtime // 3600)
            minutes = int((self.total_runtime % 3600) // 60)
            seconds = self.total_runtime % 60
            
            if hours > 0:
                logging.info(f"策略运行时长: {hours}小时{minutes}分钟{seconds:.2f}秒")
            elif minutes > 0:
                logging.info(f"策略运行时长: {minutes}分钟{seconds:.2f}秒")
            else:
                logging.info(f"策略运行时长: {seconds:.2f}秒")
            
            self.stop()
            self._cleanup_runtime_state(stage="run_end")

    def get_stock_list(self):
        """获取股票列表"""
        stock_codes = []
        try:
            # 优先从配置文件中的stock_list读取
            stock_codes = self.config.get_stock_list()
            if stock_codes:
                logging.info(f"从配置文件读取到 {len(stock_codes)} 支股票")
            else:
                # 兼容性处理：尝试从stock_list_file文件读取（如果存在）
                stock_list_file = self.config.config_dict["data"].get("stock_list_file", "")
                if stock_list_file and os.path.exists(stock_list_file):
                    with open(stock_list_file, 'r', encoding='utf-8') as f:
                        stock_codes = [line.strip() for line in f if line.strip()]
                        logging.info(f"从兼容文件 {stock_list_file} 读取到 {len(stock_codes)} 支股票")
                        # 将读取到的股票列表保存到配置文件中
                        self.config.update_stock_list(stock_codes)
                        self.config.save_config()
                else:
                    # 如果都没有，使用默认股票
                    stock_codes = ["000001.SZ"]
                    logging.warning("股票列表为空，使用默认股票: 000001.SZ")

        except Exception as e:
            # 出现异常时使用默认股票
            stock_codes = ["000001.SZ"]
            logging.error(f"读取股票列表出错: {str(e)}，使用默认股票: 000001.SZ")
        
        return stock_codes

    # ============================================================
    # 撮合引擎相关方法
    # ============================================================

    def _init_match_engine(self):
        """初始化撮合引擎（已废弃，保留以兼容旧代码）

        v3.1.2: 撮合引擎初始化已移至 __init__ 方法中，此方法保留为空函数以兼容可能的调用。
        """
        # 检查配置文件中是否仍包含 match_engine 配置
        match_config = self.config.config_dict.get("backtest", {}).get("match_engine", {})
        if match_config:
            self._log("[警告] .kh配置文件中不应包含 match_engine 配置", "WARNING")
            self._log("[警告] 撮合引擎始终启用，成交量限制请在GUI【设置】中配置", "WARNING")

        # 保留空实现，不做任何操作
        pass


    def _check_volume_limit(self, signal: dict, market_data: dict) -> tuple:
        """成交量限制校验

        Args:
            signal: 交易信号
            market_data: 当前市场数据

        Returns:
            (实际可成交数量, 是否部分成交, 原因)
        """
        volume_config = self._match_config.get("volume_limit", {})
        if not volume_config.get("enabled", False):
            return signal["volume"], False, ""

        requested_volume = signal["volume"]

        # 获取当前Bar成交量
        bar_volume = market_data.get("volume", 0)
        if bar_volume <= 0:
            # 成交量为0可能是停牌，辅助判断：high==low==close 视为停牌
            bar_h = market_data.get("high", 0)
            bar_l = market_data.get("low", 0)
            bar_c = market_data.get("close", market_data.get("lastPrice", 0))
            if bar_h > 0 and abs(bar_h - bar_l) < 0.001 and abs(bar_h - bar_c) < 0.001:
                return 0, False, "疑似停牌（成交量为0且价格无波动），禁止交易"
            return requested_volume, False, "无成交量数据，不限制"

        # 参与率限制
        participation_rate = volume_config.get("participation_rate", 0.1)
        max_by_rate = int(bar_volume * participation_rate)

        # 最小成交量保障
        min_volume = volume_config.get("min_volume", 100)
        max_by_rate = max(max_by_rate, min_volume)

        # 单笔最大限制
        max_single = volume_config.get("max_single_order", float('inf'))

        # 取最严格的限制
        max_allowed = min(max_by_rate, max_single)

        # 取整到100的倍数
        max_allowed = (max_allowed // 100) * 100

        if max_allowed < 100:
            max_allowed = 100  # 最小100股

        if requested_volume <= max_allowed:
            return requested_volume, False, ""

        # 是否允许部分成交
        allow_partial = volume_config.get("allow_partial_fill", True)
        if allow_partial:
            actual_volume = max_allowed
            reason = (f"成交量受限: 请求{requested_volume}股, "
                      f"Bar成交量{bar_volume}股, 参与率{participation_rate*100:.1f}%, "
                      f"实际成交{actual_volume}股")
            return actual_volume, True, reason
        else:
            # 不允许部分成交，全部拒绝
            return 0, False, f"成交量超限且不允许部分成交: 请求{requested_volume}股, 最大允许{max_allowed}股"

    def _check_limit_order(self, signal: dict, market_data: dict) -> tuple:
        """检查限价单是否可立即成交

        Args:
            signal: 交易信号
            market_data: 当前市场数据

        Returns:
            (是否可成交, 成交价格)
        """
        limit_price = signal["price"]
        action = signal["action"]
        code = signal.get("code", "")

        # 获取Bar数据（用 hasattr 替代 isinstance，避免多余的类型注册表查询）
        if hasattr(market_data, 'to_dict'):
            market_data = market_data.to_dict()

        bar_high = market_data.get("high", 0)
        bar_low = market_data.get("low", 0)
        bar_open = market_data.get("open", 0)

        # 调试日志
        logging.debug(f"[撮合DEBUG] _check_limit_order: {code} {action} 限价:{limit_price:.2f} "
              f"Bar数据: open={bar_open}, high={bar_high}, low={bar_low}")

        if bar_high <= 0 or bar_low <= 0:
            logging.debug(f"[撮合DEBUG] Bar数据无效，返回False")
            return False, 0

        if action == "buy":
            # 买入挂单：当前Bar最低价 <= 限价 则可成交
            can_fill = bar_low <= limit_price
            logging.debug(f"[撮合DEBUG] 买入判断: bar_low({bar_low}) <= limit_price({limit_price}) = {can_fill}")
            if can_fill:
                # 成交价逻辑：开盘价 <= 限价时以开盘价成交，否则以限价成交
                if bar_open > 0 and bar_open <= limit_price:
                    fill_price = bar_open
                else:
                    fill_price = limit_price
                logging.debug(f"[撮合DEBUG] 可成交，成交价: {fill_price}")
                return True, fill_price
        else:  # sell
            # 卖出挂单：当前Bar最高价 >= 限价 则可成交
            can_fill = bar_high >= limit_price
            logging.debug(f"[撮合DEBUG] 卖出判断: bar_high({bar_high}) >= limit_price({limit_price}) = {can_fill}")
            if can_fill:
                if bar_open > 0 and bar_open >= limit_price:
                    fill_price = bar_open
                else:
                    fill_price = limit_price
                logging.debug(f"[撮合DEBUG] 可成交，成交价: {fill_price}")
                return True, fill_price

        logging.debug(f"[撮合DEBUG] 不可成交，将挂单")
        return False, 0

    def _check_stop_trigger(self, signal: dict, market_data: dict) -> bool:
        """检查止损是否触发

        Args:
            signal: 交易信号
            market_data: 当前市场数据

        Returns:
            是否触发
        """
        stop_price = signal.get("stop_price", 0)
        if stop_price <= 0:
            return False

        if hasattr(market_data, 'to_dict'):
            market_data = market_data.to_dict()

        current_price = market_data.get("close", market_data.get("lastPrice", 0))
        bar_high = market_data.get("high", current_price)
        bar_low = market_data.get("low", current_price)

        if current_price <= 0:
            return False

        action = signal["action"]

        if action == "buy":
            # 买入止损：最高价 >= 触发价
            return bar_high >= stop_price
        else:
            # 卖出止损：最低价 <= 触发价
            return bar_low <= stop_price

    def _add_to_pending(self, signal: dict, time_info: dict):
        """将信号加入挂单队列

        Args:
            signal: 交易信号
            time_info: 时间信息
        """
        if self.pending_order_mgr:
            order_id = self.pending_order_mgr.add_order(signal, time_info)
            logging.info(f"限价单已加入挂单队列 - {order_id} {signal['action']} {signal['code']} "
                f"限价:{signal['price']:.2f} 数量:{signal['volume']}")

    def _handle_partial_fill(self, signal: dict, actual_volume: int,
                              market_data: dict, time_info: dict):
        """处理部分成交

        Args:
            signal: 原始交易信号
            actual_volume: 实际可成交数量
            market_data: 市场数据
            time_info: 时间信息
        """
        original_volume = signal["volume"]
        remaining_volume = original_volume - actual_volume

        # 1. 执行可成交部分
        partial_signal = signal.copy()
        partial_signal["volume"] = actual_volume
        partial_signal["remark"] = f"{signal.get('remark', '')} [部分成交 {actual_volume}/{original_volume}]"
        self.trade_mgr.process_signals([partial_signal])

        # 2. 剩余部分的处理取决于订单类型
        order_type = signal.get("order_type", "market").lower()

        if remaining_volume >= 100:  # 剩余数量需至少100股
            remaining_signal = signal.copy()
            remaining_signal["volume"] = remaining_volume
            remaining_signal["_is_remaining"] = True  # 标记为剩余订单

            if order_type == "market":
                # 市价单：剩余部分在下一Bar继续尝试成交
                self._add_to_pending(remaining_signal, time_info)
            elif order_type == "limit":
                # 限价单：剩余部分继续挂单
                self._add_to_pending(remaining_signal, time_info)

            logging.info(f"部分成交 - {signal['code']} 成交{actual_volume}股, "
                         f"剩余{remaining_volume}股已加入挂单队列")

    def _raw_market_row(self, code, timestamp):
        """Read an aligned unadjusted OHLC row from the private sidecar."""
        raw_refs = getattr(self, "_khhistory_raw_data_ref", None) or {}
        frame = raw_refs.get(code)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return None
        try:
            current_value = int(float(timestamp))
        except (TypeError, ValueError):
            return None

        time_index = self._etf_raw_time_idx_cache.get(code)
        if time_index is None:
            if "time" not in frame.columns:
                return None
            time_index = _build_time_index_cache_entry(
                frame["time"].to_numpy(),
                use_searchsorted=True,
            )
            self._etf_raw_time_idx_cache[code] = time_index
        index, _ = _lookup_time_index(
            time_index,
            current_value,
            getattr(self, "_is_ms_timestamp", False),
        )
        if index is None or index < 0 or index >= len(frame):
            return None
        return frame.iloc[index]

    def _execution_market_data_for_code(self, code, public_data, timestamp):
        """Use raw ETF OHLC for matching while keeping adjusted data for signals."""
        if not self._etf_total_return_enabled or not is_etf(code):
            return public_data
        raw_row = self._raw_market_row(code, timestamp)
        if raw_row is None:
            self._etf_total_return_fallbacks += 1
            raise RuntimeError(f"ETF raw sidecar is missing for {code} at {timestamp}")
        if hasattr(public_data, "to_dict"):
            merged = public_data.to_dict()
        elif isinstance(public_data, dict):
            merged = dict(public_data)
        else:
            merged = {}
        raw_values = raw_row.to_dict() if hasattr(raw_row, "to_dict") else dict(raw_row)
        for field in ("open", "high", "low", "close"):
            value = raw_values.get(field)
            if value is not None and pd.notna(value):
                merged[field] = value
        if "lastPrice" in merged and pd.notna(raw_values.get("close")):
            merged["lastPrice"] = raw_values["close"]
        return merged

    def _prepare_signal_price(self, signal, timestamp, market_data=None):
        """Normalize one signal to the security's execution price basis and precision."""
        code = signal.get("code", "")
        decimals = _security_price_decimals(code, self.price_decimals)
        if (
            self._etf_total_return_enabled
            and is_etf(code)
            and not signal.get("_framework_etf_raw_price")
        ):
            public_row = (market_data or {}).get(code, {})
            raw_row = self._raw_market_row(code, timestamp)
            if raw_row is None:
                self._etf_total_return_fallbacks += 1
                raise RuntimeError(f"ETF raw sidecar is missing for {code} at {timestamp}")
            public_close = public_row.get("close") if hasattr(public_row, "get") else None
            raw_close = raw_row.get("close") if hasattr(raw_row, "get") else None
            if (
                public_close is None
                or raw_close is None
                or pd.isna(public_close)
                or pd.isna(raw_close)
                or float(public_close) <= 0
                or float(raw_close) <= 0
            ):
                self._etf_total_return_fallbacks += 1
                raise RuntimeError(f"ETF adjustment factor is unavailable for {code} at {timestamp}")
            factor = float(raw_close) / float(public_close)
            for field in ("price", "stop_price"):
                value = signal.get(field)
                if value is not None and pd.notna(value):
                    signal[field] = float(value) * factor
            signal["_framework_etf_raw_price"] = True
        if "price" in signal:
            signal["price"] = round(float(signal["price"]), decimals)
        if signal.get("stop_price") is not None:
            signal["stop_price"] = round(float(signal["stop_price"]), decimals)
        return signal

    def _pending_execution_market_data(self, current_data, timestamp):
        """Replace adjusted rows only for ETFs that currently have pending orders."""
        if not self._etf_total_return_enabled or not self.pending_order_mgr:
            return current_data
        pending = getattr(self.pending_order_mgr, "_pending_orders", {})
        etf_codes = {
            order.get("code")
            for order in pending.values()
            if order.get("code") and is_etf(order.get("code"))
        }
        if not etf_codes:
            return current_data
        matching_data = dict(current_data)
        for code in etf_codes:
            matching_data[code] = self._execution_market_data_for_code(
                code,
                current_data.get(code, {}),
                timestamp,
            )
        return matching_data

    def _portfolio_mark_price(self, code, timestamp, fallback):
        if not self._etf_total_return_enabled or not is_etf(code):
            return fallback
        raw_row = self._raw_market_row(code, timestamp)
        raw_close = raw_row.get("close") if raw_row is not None else None
        if raw_close is not None and pd.notna(raw_close) and float(raw_close) > 0:
            return float(raw_close)
        self._etf_total_return_fallbacks += 1
        logging.error("ETF raw close is missing for %s at %s", code, timestamp)
        return fallback

    def _apply_etf_total_return_distributions(self, current_date, timestamp, data):
        """Post ETF cash distributions implied by adjusted and raw close series."""
        if not self._etf_total_return_enabled:
            return
        date_key = str(current_date)
        if self._etf_distribution_date != date_key:
            self._etf_distribution_date = date_key
            self._etf_distribution_today = 0.0

        positions = self.trade_mgr.positions
        codes = set(self._etf_total_return_state)
        codes.update(code for code in positions if is_etf(code))
        max_ratio = float(
            self.config.config_dict.get("backtest", {}).get(
                "etf_distribution_max_ratio", 0.20
            )
        )
        for code in sorted(codes):
            public_row = data.get(code, {})
            adjusted_close = public_row.get("close") if hasattr(public_row, "get") else None
            raw_row = self._raw_market_row(code, timestamp)
            raw_close = raw_row.get("close") if raw_row is not None else None
            if (
                adjusted_close is None
                or raw_close is None
                or pd.isna(adjusted_close)
                or pd.isna(raw_close)
                or float(adjusted_close) <= 0
                or float(raw_close) <= 0
            ):
                if positions.get(code, {}).get("volume", 0) > 0:
                    self._etf_total_return_fallbacks += 1
                continue

            previous = self._etf_total_return_state.get(code)
            if previous and previous.get("date") != date_key:
                held_volume = float(previous.get("volume", 0) or 0)
                distribution = _implied_etf_cash_distribution(
                    previous.get("adjusted_close"),
                    adjusted_close,
                    previous.get("raw_close"),
                    raw_close,
                    held_volume,
                )
                previous_raw = float(previous.get("raw_close", 0) or 0)
                per_unit = distribution / held_volume if held_volume > 0 else 0.0
                if distribution > 0 and per_unit <= previous_raw * max_ratio:
                    self.trade_mgr.assets["cash"] += distribution
                    self._etf_distribution_today += distribution
                    self._etf_distribution_total += distribution
                elif distribution > 0:
                    self._etf_total_return_fallbacks += 1
                    logging.error(
                        "ETF implied distribution rejected as implausible: %s %.6f/unit",
                        code,
                        per_unit,
                    )

            self._etf_total_return_state[code] = {
                "date": date_key,
                "adjusted_close": float(adjusted_close),
                "raw_close": float(raw_close),
                "volume": float(positions.get(code, {}).get("volume", 0) or 0),
            }

    def _process_signals_with_match_engine(self, signals: list,
                                            current_data: dict,
                                            time_info: dict):
        """使用撮合引擎处理信号

        Args:
            signals: 交易信号列表
            current_data: 当前市场数据
            time_info: 时间信息
        """
        # 把当前成交日期喂给交易管理器，供过户费按日期分段（见 khTrade._transfer_fee_by_date）
        if self.trade_mgr is not None and isinstance(time_info, dict):
            self.trade_mgr.current_backtest_date = time_info.get("date")
        _t_todict = _t_volcheck = _t_process = 0.0
        for signal in signals:
            code = signal["code"]

            # 获取该股票的市场数据（current_data 中的值始终是 pd.Series，用 hasattr 替代 isinstance）
            _t0 = time.time()
            stock_data = current_data.get(code, {})
            stock_data = self._execution_market_data_for_code(
                code,
                stock_data,
                time_info.get("timestamp"),
            )
            if hasattr(stock_data, 'to_dict'):
                stock_data = stock_data.to_dict()
            elif not isinstance(stock_data, dict):
                stock_data = {}
            _t_todict += time.time() - _t0

            # 0. 停牌检测：volume=0 且价格无波动视为停牌
            bar_vol = stock_data.get("volume", -1)
            if bar_vol == 0:
                bar_h = stock_data.get("high", 0)
                bar_l = stock_data.get("low", 0)
                bar_c = stock_data.get("close", stock_data.get("lastPrice", 0))
                if bar_h > 0 and abs(bar_h - bar_l) < 0.001 and abs(bar_h - bar_c) < 0.001:
                    logging.warning(f"订单被拒绝: {code} 疑似停牌（成交量为0且价格无波动）")
                    continue

            # 1. 获取订单类型（默认market）
            order_type = signal.get("order_type", "market").lower()

            # 2. 市价单：直接成交（保持现有行为，完全不校验）
            if order_type == "market":
                # 检查成交量限制（如果启用）
                _t1 = time.time()
                actual_volume, is_partial, reason = self._check_volume_limit(signal, stock_data)
                _t_volcheck += time.time() - _t1

                if actual_volume <= 0:
                    # 成交量限制导致无法成交
                    logging.warning(f"订单被拒绝: {reason}")
                    continue

                _t2 = time.time()
                if is_partial:
                    # 部分成交
                    self._handle_partial_fill(signal, actual_volume, stock_data, time_info)
                else:
                    # 全部成交
                    self.trade_mgr.process_signals([signal])
                _t_process += time.time() - _t2
                continue

            # 3. 涨跌停校验（限价单、止损单等需要校验）
            price_limit_config = self._match_config.get("price_limit", {})
            if price_limit_config.get("enabled", True):
                can_trade, reject_reason = self.trade_mgr._check_price_limit(signal, stock_data)
                if not can_trade:
                    self.trade_mgr._log_order_reject(signal, reject_reason)
                    continue

            # 4. 限价单处理
            if order_type == "limit":
                can_fill, fill_price = self._check_limit_order(signal, stock_data)
                if can_fill:
                    # 可立即成交
                    signal["price"] = fill_price

                    # 检查成交量限制
                    actual_volume, is_partial, reason = self._check_volume_limit(signal, stock_data)
                    if actual_volume <= 0:
                        logging.warning(f"订单被拒绝: {reason}")
                        continue

                    if is_partial:
                        self._handle_partial_fill(signal, actual_volume, stock_data, time_info)
                    else:
                        self.trade_mgr.process_signals([signal])
                else:
                    # 加入挂单队列
                    self._add_to_pending(signal, time_info)
                continue

            # 5. 止损单/止损限价单
            if order_type in ["stop", "stop_limit"]:
                triggered = self._check_stop_trigger(signal, stock_data)
                if triggered:
                    if order_type == "stop":
                        # 转市价单
                        actual_volume, is_partial, reason = self._check_volume_limit(signal, stock_data)
                        if actual_volume <= 0:
                            logging.warning(f"订单被拒绝: {reason}")
                            continue

                        if is_partial:
                            self._handle_partial_fill(signal, actual_volume, stock_data, time_info)
                        else:
                            self.trade_mgr.process_signals([signal])
                    else:  # stop_limit
                        # 转限价单处理
                        can_fill, fill_price = self._check_limit_order(signal, stock_data)
                        if can_fill:
                            signal["price"] = fill_price
                            actual_volume, is_partial, reason = self._check_volume_limit(signal, stock_data)
                            if actual_volume <= 0:
                                logging.warning(f"订单被拒绝: {reason}")
                                continue

                            if is_partial:
                                self._handle_partial_fill(signal, actual_volume, stock_data, time_info)
                            else:
                                self.trade_mgr.process_signals([signal])
                        else:
                            self._add_to_pending(signal, time_info)
                else:
                    # 挂单等待触发
                    self._add_to_pending(signal, time_info)
                continue

            # 6. 未知订单类型，按市价单处理
            self.trade_mgr.process_signals([signal])

        # 子计时诊断（仅 DEBUG 模式输出）
        if signals and logging.getLogger().isEnabledFor(logging.DEBUG) and (_t_todict + _t_volcheck + _t_process) > 0.001:
            logging.debug(
                f"[交易诊断] 本次信号={len(signals)}个 | "
                f"数据转换={_t_todict*1000:.1f}ms | "
                f"成交量校验={_t_volcheck*1000:.1f}ms | "
                f"下单处理={_t_process*1000:.1f}ms"
            )

    def _check_period_consistency(self):
        """检查数据周期和触发周期的一致性"""
        try:
            # 获取数据设置中的周期
            data_period = self.config.kline_period
            
            # 获取触发器类型
            trigger_type = self.config.config_dict.get("backtest", {}).get("trigger", {}).get("type", "tick")
            
            # 如果是自定义触发，则不需要检查
            if trigger_type == "custom":
                return
            
            # 定义周期映射关系
            period_consistency_map = {
                "tick": "tick",
                "1m": "1m", 
                "5m": "5m",
                "1d": "1d"
            }
            
            # 获取触发器对应的期望数据周期
            expected_data_period = period_consistency_map.get(trigger_type, "tick")
            
            # 检查是否一致
            if data_period != expected_data_period:
                # 构建提醒消息
                trigger_type_names = {
                    "tick": "Tick触发",
                    "1m": "1分钟K线触发",
                    "5m": "5分钟K线触发", 
                    "1d": "日K线触发"
                }
                
                data_period_names = {
                    "tick": "tick数据",
                    "1m": "1分钟K线",
                    "5m": "5分钟K线",
                    "1d": "日K线"
                }
                
                trigger_name = trigger_type_names.get(trigger_type, trigger_type)
                data_name = data_period_names.get(data_period, data_period)
                expected_name = data_period_names.get(expected_data_period, expected_data_period)
                
                message = f"""数据周期与触发类型不匹配！

当前配置：
• 数据设置周期：{data_name}
• 触发类型：{trigger_name}

建议配置：
• 数据设置周期：{expected_name}
• 触发类型：{trigger_name}

不匹配可能导致：
- 性能问题（数据精度过高或过低）
- 触发精度问题（错过关键时间点）
- 策略执行异常

是否继续运行回测？"""

                # 使用配置中传入的回调函数（如果存在）
                confirm_cb = self.ui_settings.get('confirm_callback')
                if confirm_cb:
                    if not confirm_cb("周期不匹配警告", message):
                        logging.warning("用户取消运行：数据周期与触发类型不匹配")
                        self.is_running = False
                        return
                    else:
                        logging.warning(f"警告：继续运行不匹配配置 - 数据周期:{data_name}, 触发类型:{trigger_name}")
                else:
                    # 没有回调的情况，直接在日志中记录警告
                    logging.warning(f"警告：数据周期({data_period})与触发类型({trigger_type})不匹配")
                    
        except Exception as e:
            # 检查过程中出现异常，记录但不影响回测继续运行
            logging.warning(f"周期一致性检查时出错: {str(e)}")
            print(f"周期一致性检查时出错: {str(e)}")
        
    def _run_backtest(self):
        """回测模式"""
        try:
            # ===== 性能剖析：阶段级墙钟计时（仅统计，不改变回测逻辑/结果）=====
            # _phase_times: 互斥的顶层阶段（回测初始化→基准→行情加载→索引→预处理→主循环）
            # _phase_sub:   某阶段内部的细分（如行情加载里的纯 DB 读取），单独打印不计入合计
            self._phase_times = {}
            self._phase_sub = {}
            _bt_wall_start = time.time()
            _phase_mark = [time.time()]

            def _mark_phase(name):
                now = time.time()
                self._phase_times[name] = self._phase_times.get(name, 0.0) + (now - _phase_mark[0])
                _phase_mark[0] = now

            # 重置全局 DuckDB 读取统计（区分框架批量加载 vs khHistory 两条读取路径）
            try:
                from khDataSource import reset_read_stats as _reset_read_stats
                _reset_read_stats()
            except Exception:
                pass
            try:
                from duckdb_storage.stock_db import (
                    set_duckdb_order_mode as _set_duckdb_order_mode,
                    set_duckdb_read_ensure_tables as _set_duckdb_read_ensure_tables,
                )
                from duckdb_storage.xtdata_adapter import (
                    set_max_backtest_connections as _set_duckdb_max_connections,
                    set_batch_read_workers as _set_duckdb_batch_read_workers,
                )
                _perf_cfg = get_performance_config(self.config)
                _set_duckdb_order_mode(_perf_cfg.get("duckdb_order_mode", "verify_after_fetch"))
                _set_duckdb_read_ensure_tables(_perf_cfg.get("duckdb_read_ensure_tables", True))
                _set_duckdb_max_connections(max(int(_perf_cfg.get("duckdb_load_max_connections", 800) or 800), 800))  # 有界连接池下限800: 防旧默认100致全A(4752股)连接淘汰churn把分段加载拖成假死; 小池子cache只装实际用到的, 高cap无害
                _set_duckdb_batch_read_workers(_perf_cfg.get("duckdb_parallel_read_workers", 1))
                self._framework_raw_duckdb_load = str(_perf_cfg.get("framework_raw_duckdb_load", "true")).lower() in ("1", "true", "yes", "on")
                _mem_decision = _perf_cfg.get("memory_profile_decision", {}) or {}
                logging.info(
                    "Memory profile: requested=%s effective=%s source=%s total=%sMB available=%sMB estimated_rows=%s estimated_working_set=%sMB",
                    _perf_cfg.get("memory_profile", "auto"),
                    _perf_cfg.get("memory_profile_effective", _perf_cfg.get("memory_profile", "auto")),
                    _mem_decision.get("source", ""),
                    _mem_decision.get("total_mem_mb", 0),
                    _mem_decision.get("available_mem_mb", 0),
                    _mem_decision.get("estimated_rows", ""),
                    _mem_decision.get("estimated_working_set_mb", ""),
                )
                if _perf_cfg.get("memory_profile_retry_from"):
                    logging.warning(
                        "Memory profile retry: from=%s to=%s attempt=%s reason=%s",
                        _perf_cfg.get("memory_profile_retry_from"),
                        _perf_cfg.get("memory_profile_effective", _perf_cfg.get("memory_profile", "")),
                        _perf_cfg.get("memory_profile_retry_attempt", ""),
                        _perf_cfg.get("memory_profile_retry_reason", ""),
                    )
            except Exception:
                self._framework_raw_duckdb_load = False
                pass

            # 检查数据周期和触发周期的一致性
            self._check_period_consistency()
            
            # 初始化回测记录字典
            self.backtest_records = {
                'trades': [],  # 交易记录
                'daily_stats': [],  # 每日统计数据
                'benchmark_data': [],  # 基准指数数据
                'start_time': self.config.backtest_start,
                'end_time': self.config.backtest_end,
                'init_capital': self.config.config_dict["backtest"]["init_capital"]
            }
            self._etf_total_return_state = {}
            self._etf_raw_time_idx_cache = {}
            self._etf_distribution_total = 0.0
            self._etf_distribution_today = 0.0
            self._etf_distribution_date = None
            self._etf_total_return_fallbacks = 0

            # 缓存日志开关状态，避免在回测循环中重复检查
            self._cache_should_log()

            # ===== 初始化撮合引擎 =====
            self._init_match_engine()

            # 输出在 __init__ 中缓存的撮合引擎配置日志
            if hasattr(self, '_pending_log_messages') and self._pending_log_messages:
                for msg in self._pending_log_messages:
                    self._log(msg, "INFO")
                self._pending_log_messages = []  # 清空缓存

            logging.info("开始回测...")

            # 获取股票列表
            stock_codes = self.get_stock_list()

            # 单独处理基准指数数据
            benchmark_code = self.config.config_dict["backtest"]["benchmark"]

            # 获取策略文件名（不含路径和扩展名）
            strategy_file = self.config.config_dict.get("strategy_file", "")
            strategy_name = os.path.splitext(os.path.basename(strategy_file))[0] if strategy_file else "unknown"
            
            self._log(f"策略文件路径: {strategy_file}", "INFO")
            self._log(f"解析的策略名称: {strategy_name}", "INFO")

            # 生成回测时间戳
            backtest_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # 生成回测目录名（使用策略名称、回测时间范围和时间戳）
            # 为了避免调试器和中文路径的问题，直接使用ASCII安全的名称
            try:
                # 直接使用ASCII安全的策略名
                import hashlib
                strategy_hash = hashlib.md5(strategy_name.encode('utf-8')).hexdigest()[:8]
                backtest_dir_name = f"strategy_{strategy_hash}_{self.config.backtest_start}_{self.config.backtest_end}_{backtest_timestamp}"
                self._log(f"使用安全目录名: {backtest_dir_name} (原始策略名: {strategy_name})", "INFO")
            except Exception as e:
                self._log(f"生成目录名时出错: {str(e)}", "ERROR")
                # 使用默认名称
                backtest_dir_name = f"unknown_{self.config.backtest_start}_{self.config.backtest_end}_{backtest_timestamp}"
                self._log(f"使用默认目录名: {backtest_dir_name}", "INFO")

            # 确保backtest_results基础目录存在
            base_results_dir = self._get_writable_backtest_dir()
            self._log(f"检查基础目录是否存在: {base_results_dir}", "INFO")
            try:
                if not os.path.exists(base_results_dir):
                    os.makedirs(base_results_dir, exist_ok=True)
                    self._log(f"创建基础回测目录: {base_results_dir}", "INFO")
                else:
                    self._log(f"基础目录已存在: {base_results_dir}", "INFO")
            except Exception as e:
                self._log(f"检查/创建基础目录时出错: {str(e)}", "ERROR")
                raise
            
            # 构建回测结果目录路径
            backtest_dir = os.path.join(
                base_results_dir,
                backtest_dir_name
            )
            self._log(f"完整回测结果目录路径: {os.path.abspath(backtest_dir)}", "INFO")
            
            # 尝试规范化路径，处理可能的编码问题
            try:
                backtest_dir = os.path.normpath(backtest_dir)
                self._log(f"规范化后的路径: {backtest_dir}", "INFO")
            except Exception as e:
                self._log(f"路径规范化失败: {str(e)}", "ERROR")

            # 确保目录存在，增强错误处理
            try:
                # 强制使用绝对路径以增加稳健性
                if not os.path.isabs(backtest_dir):
                    backtest_dir = os.path.abspath(backtest_dir)
                    self._log(f"已将回测目录转换为绝对路径: {backtest_dir}", "INFO")

                self._log(f"准备检查目录是否存在: {backtest_dir}", "INFO")
                
                # 检测是否在调试模式下
                import sys
                # 优先使用环境变量检测，这在编辑器调试模块中更可靠
                is_debugging = os.environ.get('KHQUANT_DEBUG_MODE') == '1' or (hasattr(sys, 'gettrace') and sys.gettrace() is not None)
                
                if is_debugging:
                    self._log(f"检测到调试模式，使用传统方法创建目录", "INFO")
                    # 在调试模式下，直接尝试创建目录，不检查是否存在
                    try:
                        # 使用exist_ok=True，如果目录已存在也不会报错
                        os.makedirs(backtest_dir, exist_ok=True)
                        self._log(f"已使用 os.makedirs 创建目录（或目录已存在）", "INFO")
                        time.sleep(0.2)
                    except Exception as e:
                        self._log(f"创建目录时出错: {str(e)}", "ERROR")
                        # 尝试使用绝对路径
                        abs_backtest_dir = os.path.abspath(backtest_dir)
                        self._log(f"尝试使用绝对路径: {abs_backtest_dir}", "INFO")
                        os.makedirs(abs_backtest_dir, exist_ok=True)
                        backtest_dir = abs_backtest_dir
                else:
                    # 非调试模式下可以使用pathlib
                    from pathlib import Path
                    backtest_path = Path(backtest_dir)
                    self._log(f"使用 pathlib.Path 处理路径: {backtest_path}", "INFO")
                    
                    if not backtest_path.exists():
                        self._log(f"目录不存在，尝试创建: {backtest_path}", "INFO")
                        backtest_path.mkdir(parents=True, exist_ok=True)
                        self._log(f"已使用 Path.mkdir 创建目录", "INFO")
                        time.sleep(0.2)
                    else:
                        self._log(f"目录已存在: {backtest_path}", "INFO")
                    
                    # 将路径转回字符串格式供后续使用
                    backtest_dir = str(backtest_path)
                
                # 再次验证目录是否存在（调试模式下跳过）
                if not is_debugging:
                    if not os.path.exists(backtest_dir):
                        self._log(f"目录创建后仍然不存在！", "ERROR")
                        raise FileNotFoundError(f"无法创建或访问回测结果目录: {backtest_dir}")
                    self._log(f"回测结果目录确认存在: {backtest_dir}", "INFO")
                else:
                    self._log(f"调试模式下跳过目录存在性验证", "INFO")
                    self._log(f"假定回测结果目录已创建: {backtest_dir}", "INFO")
                    
            except Exception as e:
                error_msg = f"创建回测结果目录时失败: {str(e)}"
                self._log(error_msg, "ERROR")
                raise Exception(error_msg)

            _mark_phase("1.回测初始化(目录/撮合/股票池)")
            benchmark_file = os.path.join(backtest_dir, "benchmark.csv")
            # 前推10天获取前一交易日数据，使报告基准净值曲线与 GUI 一致
            _bench_early_start = (pd.to_datetime(self.config.backtest_start, format='%Y%m%d') - pd.Timedelta(days=10)).strftime('%Y%m%d')

            if not os.path.exists(benchmark_file):
                logging.info(f"开始获取基准指数 {benchmark_code} 的每日数据")

                try:
                    benchmark_data = self.data_source_mgr.get_market_data_ex(
                        field_list=['time', 'close'],
                        stock_list=[benchmark_code],
                        period='1d',
                        start_time=_bench_early_start,
                        end_time=self.config.backtest_end,
                        fill_data=True,
                    )
                    
                    if self.trader_callback:
                        logging.info(f"获取到的数据结构: {benchmark_data.keys()}")
                        if benchmark_code in benchmark_data:
                            logging.info(f"数据字段: {benchmark_data[benchmark_code].columns.tolist()}")
                    
                    if benchmark_data and benchmark_code in benchmark_data:
                        df = benchmark_data[benchmark_code]
                        
                        logging.info(f"基准数据形状: {df.shape}")
                        
                        # 确保有时间和收盘价列
                        if 'time' in df.columns and 'close' in df.columns:
                            # 转换时间戳为日期
                            # ms时间戳是UTC瞬时, 直接按UTC取日会把中国本地日偏移1天(同 low_memory 那类时区bug),
                            # 导致 benchmark cache 键错位、daily_stats 基准线前移1天。转中国时区后再取日。
                            try:
                                df['date'] = pd.to_datetime(df['time'], unit='ms', utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
                            except:
                                # 如果转换失败，尝试其他单位
                                try:
                                    df['date'] = pd.to_datetime(df['time'], unit='s', utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
                                except:
                                    try:
                                        df['date'] = pd.to_datetime(df['time'])
                                    except Exception as e:
                                        logging.error(f"时间戳转换失败: {str(e)}")
                            
                            # 选择需要的列并保存
                            if 'date' in df.columns:
                                result_df = df[['date', 'close']].copy()
                                
                                if len(result_df) > 0:
                                    # 确保保存目录存在
                                    os.makedirs(os.path.dirname(benchmark_file), exist_ok=True)
                                    result_df.to_csv(benchmark_file, index=False)
                                    logging.info(f"基准指数数据已保存到 {benchmark_file}, 共 {len(result_df)} 条记录")
                                        
                                    # 预先缓存所有基准指数数据，提高性能
                                    for _, row in result_df.iterrows():
                                        date_str = row['date'].strftime('%Y%m%d')
                                        cache_key = f"benchmark_{date_str}_{benchmark_code}"
                                        self._cached_benchmark_close[cache_key] = row['close']
                                    
                                    logging.info(f"已预缓存 {len(self._cached_benchmark_close)} 条基准指数数据")
                except Exception as e:
                    logging.error(f"获取和保存基准指数数据失败: {str(e)}")
                    logging.error(f"获取和保存基准指数数据失败: {str(e)}", exc_info=True)
            
            # 获取数据周期
            data_period = self.trigger.get_data_period()

            _mark_phase("2.基准数据加载")
            # ===== 动态数据加载初始化 =====
            # 获取动态加载配置
            dynamic_load_settings = self.config.get_dynamic_load_settings()
            self.dynamic_loader_enabled = dynamic_load_settings['enabled']
            self.dynamic_loader = None
            self._last_chunk_idx = -1

            # 获取数据字段列表
            field_list = self.config.config_dict["data"]["fields"]
            if "time" not in field_list:
                field_list = ["time"] + field_list
            if "close" not in field_list:
                field_list.append("close")
            performance_cfg_for_load = get_performance_config(self.config)
            data_load_start_time = self.config.backtest_start
            try:
                preload_days = int(performance_cfg_for_load.get("framework_history_preload_days", 0) or 0)
            except Exception:
                preload_days = 0
            if preload_days > 0:
                # 用户显式配置: 按日历日预读(向后兼容)
                try:
                    data_load_start_time = (
                        pd.to_datetime(self.config.backtest_start, format="%Y%m%d")
                        - pd.Timedelta(days=preload_days)
                    ).strftime("%Y%m%d")
                    logging.info(
                        "framework history preload enabled: load_start=%s backtest_start=%s days=%s",
                        data_load_start_time,
                        self.config.backtest_start,
                        preload_days,
                    )
                except Exception:
                    data_load_start_time = self.config.backtest_start
            else:
                # #17: 分钟线首段(chunk-0)在策略首次 khHistory 之前加载, 观测回看未知。
                # 给一个默认"交易日"预读兜底(复用真实交易日历, 跨春节等长假也稳);
                # 后续分段由 khDynamicLoader 按 khHistory 实际回看交易日数自适应覆盖。
                try:
                    _kp_preload = str(self.config.config_dict.get("data", {}).get("kline_period", "")).lower()
                except Exception:
                    _kp_preload = ""
                if _kp_preload in ("1m", "5m"):
                    try:
                        from khQTTools import trade_day_n_before
                        data_load_start_time = trade_day_n_before(self.config.backtest_start, 5)
                        logging.info(
                            "framework #17 首段默认交易日预读: load_start=%s backtest_start=%s 交易日=5",
                            data_load_start_time,
                            self.config.backtest_start,
                        )
                    except Exception:
                        data_load_start_time = self.config.backtest_start

            # 对于自定义定时触发，确定实际的数据周期
            period = data_period
            if isinstance(self.trigger, CustomTimeTrigger):
                all_whole_minutes = True
                for seconds in self.trigger.trigger_seconds:
                    seconds_part = seconds % 60
                    if seconds_part != 0:
                        all_whole_minutes = False
                        break
                if all_whole_minutes:
                    period = "1m"
                else:
                    period = "tick"

            if self.dynamic_loader_enabled:
                self._khhistory_full_load_start_time = None
                logging.info(f"启用动态数据加载模式: 分段大小={dynamic_load_settings['chunk_size']}天")

                # 创建动态加载器
                self.dynamic_loader = DynamicDataLoader(
                    config=self.config,
                    stock_codes=stock_codes,
                    data_period=period,
                    start_date=self.config.backtest_start,
                    end_date=self.config.backtest_end,
                    load_start_date=data_load_start_time,
                    enabled=True,
                    chunk_size=dynamic_load_settings['chunk_size'],
                    field_list=field_list,
                    dividend_type=self.config.config_dict["data"]["dividend_type"],
                    tools=self.tools,
                    callback=self.trader_callback,
                    lock_decision_callback=self.ui_settings.get("duckdb_lock_decision_callback"),
                    lock_skip_callback=self._duckdb_lock_skipped.append,
                )

                # 获取所有时间点（用于主循环）
                # 注意：get_all_times() 会加载并立即释放各分段来获取时间点
                all_times = self.dynamic_loader.get_all_times()

                # 不预先加载任何数据，在回测循环中按需加载
                historical_data = {}  # 初始为空，将在循环中动态加载

                logging.info(f"动态加载器初始化完成: 总分段数={len(self.dynamic_loader.time_segments)}, "
                    f"将在回测过程中按需加载数据")
            else:
                # ===== 传统模式：一次性加载所有数据 =====
                self._khhistory_full_load_start_time = data_load_start_time
                logging.info("使用传统数据加载模式（一次性加载）")

                # 一次性加载所有股票的历史数据
                historical_data = {}
                # Adjusted framework loads already query raw OHLC columns from
                # DuckDB.  Keep them in an internal sidecar so khHistory can
                # serve fq="none" without a second full-market database pass.
                self._khhistory_raw_data_ref = {}
                total_stocks = len(stock_codes)
                
                # 初始化加载进度显示
                # 设置进度条标签为"加载数据进度"
                cb = self.ui_settings.get('set_progress_label_callback')
                if cb:
                    cb("加载数据进度")
                logging.info(f"开始批量加载{total_stocks}只股票的历史数据...")
                cb = self.ui_settings.get('update_progress_callback')
                if cb:
                    cb(0)
                
                # 可选 Parquet cache pack：默认关闭。只在框架 raw DuckDB 快路下启用，
                # 命中后跳过逐批打开单股票 DuckDB 文件；未命中或失败自动回退原路径。
                parquet_pack_used = False
                parquet_pack_mode = str(performance_cfg_for_load.get("parquet_cache_pack", "off") or "off").strip().lower()
                if (
                    parquet_pack_mode not in ("", "0", "false", "off", "none", "disabled")
                    and not isinstance(self.trigger, CustomTimeTrigger)
                    and getattr(self, "_framework_raw_duckdb_load", False)
                    and getattr(self.data_source_mgr, "data_source", "") == "duckdb"
                ):
                    try:
                        from duckdb_storage.parquet_cache_pack import load_or_build_framework_raw_pack

                        _pq_t0 = time.time()
                        parquet_data, parquet_info = load_or_build_framework_raw_pack(
                            field_list=field_list,
                            stock_list=stock_codes,
                            period=period,
                            start_time=data_load_start_time,
                            end_time=self.config.backtest_end,
                            dividend_type=self.config.config_dict.get("data", {}).get("dividend_type", "none"),
                            data_root=getattr(self.data_source_mgr, "duckdb_path", ""),
                            performance_cfg=performance_cfg_for_load,
                        )
                        self._phase_sub["Parquet cache pack"] = (
                            self._phase_sub.get("Parquet cache pack", 0.0) + (time.time() - _pq_t0)
                        )
                        logging.info(
                            "Parquet cache pack: mode=%s status=%s rows=%s stocks=%s bytes=%s dir=%s",
                            parquet_info.get("mode"),
                            parquet_info.get("status"),
                            parquet_info.get("rows", 0),
                            parquet_info.get("stock_count", 0),
                            parquet_info.get("bytes", 0),
                            parquet_info.get("pack_dir", ""),
                        )
                        if parquet_data is not None:
                            historical_data, self._khhistory_raw_data_ref = (
                                _split_framework_raw_pack(parquet_data)
                            )
                            parquet_pack_used = True
                            logging.info(
                                "Parquet cache pack命中: 已加载%d/%d只股票，原始价侧车%d只，跳过DuckDB批量加载",
                                len(historical_data),
                                total_stocks,
                                len(self._khhistory_raw_data_ref),
                            )
                    except Exception as e:
                        logging.warning(f"Parquet cache pack读取失败，回退DuckDB批量加载: {str(e)}")

                # ===== 批量加载优化：分批加载，提升速度 =====
                # 每批加载的股票数量（可通过 performance.duckdb_load_batch_size 调整）。
                # 未配置或配置为 auto 时使用稳定批次，避免过大批次在单股票 DuckDB 小库上放大 I/O 抖动；
                # 用户显式写入数值时保持原值，便于针对不同磁盘和数据形态回退对比。
                if parquet_pack_used:
                    batch_size = max(1, total_stocks or 1)
                    total_batches = 0
                else:
                    raw_batch_size = performance_cfg_for_load.get("duckdb_load_batch_size", "auto")
                    try:
                        raw_batch_text = str(raw_batch_size).strip().lower()
                    except Exception:
                        raw_batch_text = "auto"
                    if raw_batch_size is None or raw_batch_text in ("", "auto", "default"):
                        batch_size = min(total_stocks or 1, 250)
                    else:
                        try:
                            batch_size = int(raw_batch_size or 250)
                        except Exception:
                            batch_size = min(total_stocks or 1, 250)
                    batch_size = max(1, min(batch_size, total_stocks or 1))
                    total_batches = (total_stocks + batch_size - 1) // batch_size  # 向上取整

                for batch_idx in range(total_batches):
                    if not self.is_running:
                        break
                    
                    # 计算当前批次的股票范围
                    start_idx = batch_idx * batch_size
                    end_idx = min(start_idx + batch_size, total_stocks)
                    batch_codes = stock_codes[start_idx:end_idx]
                    batch_count = len(batch_codes)
                    
                    # 计算并显示加载进度
                    progress = ((batch_idx + 1) / total_batches) * 100
                    cb = self.ui_settings.get('update_progress_callback')
                    if cb:
                        cb(int(progress))
                    
                    logging.info(f"批量加载第{batch_idx + 1}/{total_batches}批 ({batch_count}只股票, {period}周期)...")
                    
                    # 根据触发器的数据周期批量加载数据
                    # period已在上面确定
                    try:
                        _db_t0 = time.time()
                        if getattr(self, "_framework_raw_duckdb_load", False) and getattr(self.data_source_mgr, "data_source", "") == "duckdb":
                            data = self.data_source_mgr.get_market_data_ex_framework_raw(
                                field_list=field_list,
                                stock_list=batch_codes,
                                period=period,
                                start_time=data_load_start_time,
                                end_time=self.config.backtest_end,
                                dividend_type=self.config.config_dict["data"]["dividend_type"],
                                fill_data=True,
                                preserve_raw_ohlc=True,
                            )
                        else:
                            data = self.data_source_mgr.get_market_data_ex(
                                field_list=field_list,
                                stock_list=batch_codes,  # 批量加载多只股票
                                period=period,
                                start_time=data_load_start_time,
                                end_time=self.config.backtest_end,
                                dividend_type=self.config.config_dict["data"]["dividend_type"],
                                fill_data=True,
                            )
                        self._phase_sub["行情DB读取(get_market_data_ex)"] = \
                            self._phase_sub.get("行情DB读取(get_market_data_ex)", 0.0) + (time.time() - _db_t0)
                    except Exception as e:
                        logging.warning(f"批次{batch_idx + 1}加载失败: {str(e)}，尝试逐个加载...")
                        # 如果批量加载失败，回退到逐个加载
                        data = {}
                        for code in batch_codes:
                            while self.is_running:
                                try:
                                    if (
                                        getattr(self, "_framework_raw_duckdb_load", False)
                                        and getattr(self.data_source_mgr, "data_source", "") == "duckdb"
                                    ):
                                        single_data = self.data_source_mgr.get_market_data_ex_framework_raw(
                                            field_list=field_list,
                                            stock_list=[code],
                                            period=period,
                                            start_time=data_load_start_time,
                                            end_time=self.config.backtest_end,
                                            dividend_type=self.config.config_dict["data"]["dividend_type"],
                                            fill_data=True,
                                            preserve_raw_ohlc=True,
                                        )
                                    else:
                                        single_data = self.data_source_mgr.get_market_data_ex(
                                            field_list=field_list,
                                            stock_list=[code],
                                            period=period,
                                            start_time=data_load_start_time,
                                            end_time=self.config.backtest_end,
                                            dividend_type=self.config.config_dict["data"]["dividend_type"],
                                            fill_data=True,
                                        )
                                    if single_data:
                                        data.update(single_data)
                                    break
                                except Exception as e2:
                                    if not is_duckdb_lock_error(e2):
                                        logging.error(f"股票{code}加载失败: {str(e2)}")
                                        break
                                    info = parse_duckdb_lock_error(
                                        e2,
                                        stock_code=code,
                                        period=period,
                                        operation="read",
                                        attempts=5,
                                    )
                                    decision_cb = self.ui_settings.get("duckdb_lock_decision_callback")
                                    decision = "skip"
                                    if callable(decision_cb):
                                        try:
                                            decision = str(decision_cb(info) or "skip").lower()
                                        except Exception as callback_error:
                                            logging.warning(f"数据库占用处理回调失败: {callback_error}")
                                    if decision == "retry":
                                        logging.warning(f"用户选择继续重试读取: {code} {period}")
                                        continue
                                    if decision == "abort":
                                        raise RuntimeError(
                                            f"用户停止回测：数据库文件持续被占用 {code} {period}"
                                        ) from e2
                                    self._duckdb_lock_skipped.append(info)
                                    logging.warning(
                                        "数据库占用，已跳过本次回测数据: %s %s (PID %s)",
                                        code, period, info.get("pid") or "未知",
                                    )
                                    break
                    
                    # 处理批量加载的数据
                    if data:
                        for code in batch_codes:
                            if code in data:
                                public_df, raw_sidecar = _split_framework_raw_sidecar(data[code])
                                # 判断是否为自定义时间触发
                                if isinstance(self.trigger, CustomTimeTrigger):
                                    # 对于自定义时间触发，只保留触发时间点附近的数据
                                    df = public_df
                                    if 'time' in df.columns:
                                        # 获取所有时间戳
                                        all_timestamps = df['time'].values
                                        # 转换为秒级时间戳进行比较
                                        filtered_rows = []

                                        for ts in all_timestamps:
                                            # 转换时间戳为秒级
                                            ts_seconds = float(ts) / 1000 if float(ts) > 1e10 else float(ts)
                                            ts_dt = datetime.datetime.fromtimestamp(ts_seconds)

                                            # 计算当前时间点的秒数（从午夜开始）
                                            current_seconds = ts_dt.hour * 3600 + ts_dt.minute * 60 + ts_dt.second

                                            # 检查是否接近任一触发时间点（允许1秒误差）
                                            for trigger_second in self.trigger.trigger_seconds:
                                                if abs(current_seconds - trigger_second) <= 1:
                                                    filtered_rows.append(ts)
                                                    break

                                        # 只保留触发时间点附近的数据
                                        if filtered_rows:
                                            filtered_df = df[df['time'].isin(filtered_rows)]
                                            historical_data[code] = filtered_df
                                            if raw_sidecar is not None and 'time' in raw_sidecar.columns:
                                                self._khhistory_raw_data_ref[code] = raw_sidecar[
                                                    raw_sidecar['time'].isin(filtered_rows)
                                                ]
                                        else:
                                            # 如果没有找到匹配的时间点，仍然保存原始数据
                                            historical_data[code] = df
                                            if raw_sidecar is not None:
                                                self._khhistory_raw_data_ref[code] = raw_sidecar
                                    else:
                                        # 如果没有time列，使用原始数据
                                        historical_data[code] = public_df
                                        if raw_sidecar is not None:
                                            self._khhistory_raw_data_ref[code] = raw_sidecar
                                else:
                                    # 非自定义时间触发，直接存储DataFrame
                                    historical_data[code] = public_df
                                    if raw_sidecar is not None:
                                        self._khhistory_raw_data_ref[code] = raw_sidecar

                # 加载完成，显示100%进度
                cb = self.ui_settings.get('update_progress_callback')
                if cb:
                    cb(100)
                logging.info(f"数据加载完成: 已成功加载{len(historical_data)}/{total_stocks}只股票")
                if self._duckdb_lock_skipped:
                    logging.warning(
                        "本次回测因数据库占用跳过%d项: %s",
                        len(self._duckdb_lock_skipped),
                        ", ".join(
                            f"{item.get('stock')} {item.get('period')}"
                            for item in self._duckdb_lock_skipped
                        ),
                    )

                if not self.is_running:
                    logging.warning("回测被中止")
                    return

                # #13 tick(全量路径): tick 表 time 是 TIMESTAMP→读出为 datetime; kline 读取路径已转毫秒整数,
                # tick 没转。全量路径下游 _is_ms_timestamp=int(all_times[0]) 假定毫秒整数 → datetime 直接崩
                # (只"省内存"档因走 khDynamicLoader.load_chunk 已归一而幸免)。在此把 datetime time 列统一转
                # 毫秒(上海时区, 与 load_chunk 同口径), all_times 与逐 bar 查表都用归一后的列 → 三档一致。
                for _df in historical_data.values():
                    if isinstance(_df, pd.DataFrame) and 'time' in _df.columns and len(_df) and \
                            pd.api.types.is_datetime64_any_dtype(_df['time']):
                        _df['time'] = _df['time'].dt.tz_localize('Asia/Shanghai').astype('int64') // 1_000_000

                # 获取所有时间点（传统模式）
                all_times = []

                # 对于自定义时间触发，使用不同的方式获取时间点
                if isinstance(self.trigger, CustomTimeTrigger):
                    # 获取回测日期范围内的所有交易日
                    start_date = datetime.datetime.strptime(self.config.backtest_start, "%Y%m%d").date()
                    end_date = datetime.datetime.strptime(self.config.backtest_end, "%Y%m%d").date()

                    # 获取交易日历（使用KhQuTools的真实交易日判断）
                    current_date = start_date
                    trading_days = []
                    while current_date <= end_date:
                        # 使用KhQuTools判断是否为真实交易日（排除节假日）
                        date_str = current_date.strftime("%Y-%m-%d")
                        if self.tools.is_trade_day(date_str):
                            trading_days.append(current_date)
                        current_date += datetime.timedelta(days=1)

                    logging.info(f"回测期间共有{len(trading_days)}个交易日")

                    # 为每个交易日生成自定义触发时间点
                    for day in trading_days:
                        for seconds in self.trigger.trigger_seconds:
                            # 将秒数转换为时分秒
                            h = seconds // 3600
                            m = (seconds % 3600) // 60
                            s = seconds % 60

                            # 创建完整的datetime对象
                            dt = datetime.datetime.combine(day, datetime.time(h, m, s))

                            # 转换为时间戳（秒级）
                            timestamp = int(dt.timestamp())
                            all_times.append(timestamp)

                    logging.info(f"自定义时间触发模式：生成了{len(all_times)}个时间点")
                else:
                    # 非自定义时间触发模式，使用原来的方式获取时间点
                    index_seconds_cache = {}
                    for code, df in historical_data.items():
                        logging.debug(f"处理{code}的数据...")

                        # 检查数据结构
                        if isinstance(df, pd.DataFrame):
                            # DataFrame结构处理
                            if 'time' in df.columns:
                                times = (
                                    _normalize_daily_time_values(df['time'].values)
                                    if period == "1d" else df['time'].values
                                )
                                all_times.extend(times.tolist() if hasattr(times, "tolist") else times)
                                logging.debug(f"从{code}的DataFrame中提取了{len(times)}个时间点")
                            else:
                                # 尝试使用其他可能的时间字段
                                time_field = None
                                for field in ['timestamp', 'date', 'datetime']:
                                    if field in df.columns:
                                        time_field = field
                                        break

                                if time_field is None:
                                    # 如果没有找到任何时间字段，尝试使用索引
                                    if isinstance(df.index, pd.DatetimeIndex):
                                        times = (
                                            _normalize_daily_time_values(df.index)
                                            if period == "1d" else df.index.astype(np.int64) // 10**9
                                        )  # 转换为秒级时间戳
                                        all_times.extend(times.tolist() if hasattr(times, "tolist") else times)
                                        logging.debug(f"从{code}的DataFrame索引中提取了{len(times)}个时间点")
                                    elif isinstance(df.index, (pd.Index, pd.RangeIndex)) and len(df.index) > 0:
                                        # 尝试将字符串索引转换为时间戳（适配 DuckDB 5m/1m 数据）
                                        # 格式如 '20241227093000' (YYYYMMDDHHmmss)
                                        try:
                                            first_idx = str(df.index[0])
                                            if len(first_idx) == 14 and first_idx.isdigit():
                                                # 字符串格式的时间索引
                                                times = _cached_index_seconds(df.index, index_seconds_cache)
                                                all_times.extend(times.tolist())
                                                logging.debug(f"从{code}的DataFrame字符串索引中提取了{len(times)}个时间点")
                                            else:
                                                # 尝试作为毫秒时间戳处理
                                                if len(first_idx) == 13 and first_idx.isdigit():
                                                    # QMT常见的时间戳是13位（毫秒）
                                                    times = df.index.values.astype(np.int64)
                                                    all_times.extend(times)
                                                    logging.debug(f"从{code}的DataFrame毫秒索引中提取了{len(times)}个时间点")
                                                else:
                                                    # 如果不是14位或13位，可能已经是普通时间戳或者其他格式
                                                    # 退回到直接提取values
                                                    times = df.index.values
                                                    all_times.extend(times)
                                                    logging.debug(f"从{code}的DataFrame索引直接提取了{len(times)}个时间点")
                                        except Exception as e:
                                            logging.error(f"警告: {code}的索引转换提示: {str(e)}，直接提取索引值")
                                            times = df.index.values
                                            all_times.extend(times)
                                    else:
                                        # 如果没有找到任何时间字段，跳过这个股票
                                        logging.error(f"错误: {code}的数据中没有找到任何时间字段，跳过该股票")
                                        continue
                                else:
                                    times = (
                                        _normalize_daily_time_values(df[time_field].values)
                                        if period == "1d" else df[time_field].values
                                    )
                                    all_times.extend(times.tolist() if hasattr(times, "tolist") else times)
                                    logging.debug(f"从{code}的DataFrame {time_field}列中提取了{len(times)}个时间点")
                        else:
                            # 处理其他数据结构
                            logging.warning(f"警告: {code}的数据结构异常: {type(df)}")
                            continue

            # 去重并排序时间点（两种模式通用）
            all_times = sorted(list(set(all_times)))
            # 回测区间裁剪(恒裁): all_times 必须只含 [backtest_start, backtest_end]。为 khHistory 回看而载入
            # 的预热段时间点若不裁掉, 会被回测循环当回测段交易(全量/standard 路径 preload=0 时尤甚)。
            # 此裁剪原先被 `preload_days>0` 误门控, 导致 preload=0 的 standard 回测交易了 start 之前的预热段;
            # 改为恒裁修正。动态分段路径 all_times 已是 [start,end], 此处对它是 no-op。
            if all_times:
                try:
                    _bt_start_dt = datetime.datetime.strptime(self.config.backtest_start, "%Y%m%d")
                    _bt_end_dt = datetime.datetime.strptime(self.config.backtest_end, "%Y%m%d") + datetime.timedelta(days=1)
                    _filtered_times = []
                    for _ts in all_times:
                        _ts_int = int(_ts)
                        _dt = datetime.datetime.fromtimestamp(_ts_int / 1000 if _ts_int > 1e10 else _ts_int)
                        if _bt_start_dt <= _dt < _bt_end_dt:
                            _filtered_times.append(_ts)
                    if len(_filtered_times) != len(all_times):
                        logging.info(
                            "framework backtest-range time filter: all_times %s -> %s (裁掉预热段/越界点)",
                            len(all_times),
                            len(_filtered_times),
                        )
                    all_times = _filtered_times
                except Exception as _filter_exc:
                    logging.warning("framework backtest-range time filter failed: %s", _filter_exc)
            # ===== 数据加载结束 =====
            _mark_phase("3.行情数据加载(全量载入内存)")

            if len(all_times) == 0:
                raise RuntimeError("没有找到任何有效的时间点，无法进行回测")

            logging.info(f"共找到{len(all_times)}个时间点")
            logging.info(f"第一个时间点: {all_times[0]}")
            logging.info(f"最后一个时间点: {all_times[-1]}")

            # 保存所有时间点到实例变量，供record_results使用
            self.all_times = all_times

            # 一次性判断时间戳精度，存为实例变量供其他方法（如 record_results）共享
            self._is_ms_timestamp = bool(int(all_times[0]) > 1e10)

            total_times = len(all_times)
            processed_times = 0

            # 计算进度显示增量（至少为1，最多为总数/100向上取整）
            if total_times > 100:
                progress_increment = max(1, int(total_times / 100))
            else:
                # 如果时间点太少，则每处理一个点都显示一次进度
                progress_increment = 1

            # 显示开始进度
            cb = self.ui_settings.get('set_progress_label_callback')
            if cb:
                cb("回测进度")
            logging.info("回测进度: 0.00%")
            # 强制发送0%进度信号，确保进度条立即显示
            cb = self.ui_settings.get('update_progress_callback')
            if cb:
                cb(0)
            # 注意：不要在子线程中调用 QApplication.processEvents()
            # 这会导致GUI线程阻塞和潜在的线程安全问题

            # 预先构建数据缓存（避免在循环中重复构建）
            # 注意：动态加载模式下，缓存会在第一次加载数据时构建
            if not hasattr(self, 'historical_data_ref'):
                if self.dynamic_loader_enabled:
                    # 动态加载模式：初始化空缓存，在第一次数据加载时构建
                    self.historical_data_ref = {}
                    self.time_field_cache = {}
                    self.time_idx_cache = {}
                    logging.info("动态加载模式：数据缓存将在首次加载时构建")
                elif historical_data:
                    # 传统模式：预先构建缓存
                    logging.info("正在构建数据缓存...")

                    # 创建包含原始DataFrame引用的字典
                    self.historical_data_ref = {}

                    # 创建时间字段到索引的映射，用于快速查找
                    self.time_field_cache = {}
                    self.time_idx_cache = {}
                    index_time_map_cache = {}
                    use_searchsorted_time_index = _use_searchsorted_time_index(self.config)
                    if use_searchsorted_time_index:
                        logging.info("time_index_mode=searchsorted: skip per-row Python time-index dict")

                    # 创建基于当前时间点的数据引用
                    for code, df in historical_data.items():
                        # 找到时间字段
                        time_field_found = False
                        for field in ['time', 'timestamp', 'date', 'datetime']:
                            if field in df.columns:
                                self.time_field_cache[code] = field
                                # 保存原始DataFrame引用
                                self.historical_data_ref[code] = df

                                # 预先创建时间值到索引的映射（只计算一次）
                                time_values = (
                                    _normalize_daily_time_values(df[field].values)
                                    if period == "1d" else df[field].values
                                )
                                self.time_idx_cache[code] = _build_time_index_cache_entry(
                                    time_values,
                                    use_searchsorted_time_index,
                                )
                                time_field_found = True
                                break

                        # 如果没有找到时间字段，尝试使用索引（适配 DuckDB 5m/1m 数据）
                        if not time_field_found:
                            if isinstance(df.index, (pd.Index, pd.RangeIndex)) and len(df.index) > 0:
                                try:
                                    first_idx = str(df.index[0])
                                    if len(first_idx) == 14 and first_idx.isdigit():
                                        # 字符串格式的时间索引，使用 '__index__' 作为虚拟字段名
                                        self.time_field_cache[code] = '__index__'
                                        self.historical_data_ref[code] = df

                                        # 构建索引映射（将字符串索引转换为时间戳）
                                        if use_searchsorted_time_index:
                                            self.time_idx_cache[code] = _build_time_index_cache_entry(
                                                _cached_index_seconds(df.index, index_time_map_cache),
                                                True,
                                            )
                                        else:
                                            time_idx_map = _cached_index_time_map(df.index, index_time_map_cache)
                                            self.time_idx_cache[code] = time_idx_map
                                        time_field_found = True
                                except Exception as e:
                                    logging.warning(f"警告: {code}的索引转换失败: {str(e)}")
                    
                    logging.info("数据缓存构建完成")
            
            _mark_phase("4.时间索引构建(time_idx_cache)")
            # 按时间顺序模拟
            current_date = None
            day_start_time = None
            day_data = {}
            
            # 获取盘前盘后回调设置
            pre_market_enabled = self.config.config_dict.get("market_callback", {}).get("pre_market_enabled", False)
            pre_market_time = self.config.config_dict.get("market_callback", {}).get("pre_market_time", "08:30:00")
            post_market_enabled = self.config.config_dict.get("market_callback", {}).get("post_market_enabled", False)
            post_market_time = self.config.config_dict.get("market_callback", {}).get("post_market_time", "15:30:00")
            
            if pre_market_enabled and self.trader_callback:
                logging.info(f"已启用盘前回调，将在每个交易日 {pre_market_time} 执行")
                # 检查策略是否实现了盘前回调方法
                if not hasattr(self.strategy_module, 'khPreMarket'):
                    logging.warning("警告: 策略模块未实现 khPreMarket 方法，盘前回调将不会执行")
            if post_market_enabled and self.trader_callback:
                logging.info(f"已启用盘后回调，将在每个交易日 {post_market_time} 执行")
                # 检查策略是否实现了盘后回调方法
                if not hasattr(self.strategy_module, 'khPostMarket'):
                    logging.warning("警告: 策略模块未实现 khPostMarket 方法，盘后回调将不会执行")
            
            # 获取唯一的交易日列表
            trading_days = set()
            for time_point in all_times:
                try:
                    timestamp = int(time_point)
                    dt = datetime.datetime.fromtimestamp(timestamp / 1000 if self._is_ms_timestamp else timestamp)
                    trading_days.add(dt.strftime("%Y-%m-%d"))
                except:
                    pass
            
            trading_days = sorted(list(trading_days))
            logging.info(f"回测期间共有 {len(trading_days)} 个交易日")
            
            time_stats = {
                "构造数据": 0,
                "构造时间信息": 0,
                "检查新日期": 0,
                "盘后回调": 0,
                "盘前回调": 0,
                "触发器检查": 0,
                "风控检查": 0,
                "策略处理": 0,
                "处理信号": 0,
                "交易指令": 0,
                "记录结果": 0,
                "总时间": 0
            }
            
            # 循环前预处理：将时间点统一转为 int，避免循环内每次做类型转换
            all_times = [int(t) for t in all_times]
            self.all_times = all_times  # 同步更新实例变量

            # 缓存循环外常量，避免每次迭代做字典查找或 isinstance 判断
            _progress_cb = self.ui_settings.get('update_progress_callback')
            self._is_custom_trigger = isinstance(self.trigger, CustomTimeTrigger)
            import khQuantImport as _khimport  # 移出循环，避免每次迭代触发 sys.modules 查找
            # trigger_type 在整个回测中不变，循环外读一次
            trigger_type = self.config.config_dict.get("backtest", {}).get("trigger", {}).get("type", "tick")
            # strategy_module 在整个回测中不变，hasattr 结果缓存
            _has_pre_market  = hasattr(self.strategy_module, 'khPreMarket')
            _has_post_market = hasattr(self.strategy_module, 'khPostMarket')

            # ── 进阶优化：预计算全量 time_info 查找表 ──────────────────────────────
            # 阈值：超过 50 万个时间点（约 1 年 Tick 量级）时不预计算，避免内存过大
            _TIME_LOOKUP_THRESHOLD = 500_000
            if len(all_times) <= _TIME_LOOKUP_THRESHOLD:
                def _build_time_info(ts):
                    try:
                        dt = datetime.datetime.fromtimestamp(ts / 1000 if self._is_ms_timestamp else ts)
                        return {
                            "timestamp": ts,
                            "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
                            "date": dt.strftime("%Y-%m-%d"),
                            "time": dt.strftime("%H:%M:%S"),
                            "raw_time": ts,
                            "_dt": dt,   # 供触发器直接使用，跳过 fromtimestamp
                        }
                    except Exception:
                        s = str(ts)
                        return {"timestamp": ts, "datetime": s, "date": s, "time": s, "raw_time": ts, "_dt": None}
                _time_info_lookup = {ts: _build_time_info(ts) for ts in all_times}
                logging.info(f"时间信息查找表已预计算，共 {len(_time_info_lookup)} 条，循环内无需再做时间格式化")
                # 同步预填 _cached_daily_times：每个交易日仅在此一次性完成，
                # 避免 record_results 每天首次调用时对全量 all_times 做 fromtimestamp
                _date_bucket: dict = {}
                for ts, info in _time_info_lookup.items():
                    _dt_obj = info.get("_dt")
                    if _dt_obj is not None:
                        _date_bucket.setdefault(_dt_obj.date(), []).append(ts)
                for _d, _ts_list in _date_bucket.items():
                    self._cached_daily_times[f"daily_times_{_d}"] = sorted(_ts_list)
                logging.info(f"daily_times 索引已预计算，共 {len(_date_bucket)} 个交易日")
            else:
                _time_info_lookup = None
                logging.info(f"时间点数量 {len(all_times)} 超过阈值 {_TIME_LOOKUP_THRESHOLD}，使用逐步计算模式")
                # 大数据集同样一次性预填，比每天触发全量扫描省得多
                _date_bucket2: dict = {}
                _ts_div2 = 1000 if self._is_ms_timestamp else 1
                for ts in all_times:
                    try:
                        _dt_obj = datetime.datetime.fromtimestamp(ts / _ts_div2)
                        _date_bucket2.setdefault(_dt_obj.date(), []).append(ts)
                    except Exception:
                        pass
                for _d, _ts_list in _date_bucket2.items():
                    self._cached_daily_times[f"daily_times_{_d}"] = sorted(_ts_list)
                logging.info(f"daily_times 索引已预计算（大数据集），共 {len(_date_bucket2)} 个交易日")
            # ────────────────────────────────────────────────────────────────────────

            _mark_phase("5.循环前预处理(time_info查找表)")
            performance_cfg = get_performance_config(self.config)
            current_data_row_mode = str(performance_cfg.get("current_data_row_mode", "series")).lower()
            use_lazy_current_rows = current_data_row_mode in ("lazy", "mapping", "row_view")
            current_data_container_mode = str(performance_cfg.get("current_data_container_mode", "dict")).lower()
            use_lazy_current_data_container = current_data_container_mode in ("lazy", "mapping", "on", "true", "1")
            stock_codes_set = set(stock_codes) if use_lazy_current_data_container else None
            lazy_col_pos_cache = {}
            available_time_set = None
            if use_lazy_current_rows:
                lazy_col_pos_cache = _build_lazy_col_pos_cache(self.historical_data_ref)
                logging.info("current_data lazy row mode enabled: %s stocks", len(lazy_col_pos_cache))
            if use_lazy_current_data_container:
                available_time_set = _build_available_time_set(self.time_idx_cache, self._is_ms_timestamp)
            try:
                from duckdb_storage.xtdata_adapter import set_max_backtest_connections as _set_duckdb_max_connections
                _set_duckdb_max_connections(max(int(performance_cfg.get("duckdb_max_connections", 800) or 800), 800))  # 有界连接池下限800(同上)
            except Exception:
                pass
            empty_data_log_mode = str(performance_cfg.get("empty_data_log_mode", "summary")).lower()
            empty_data_summary = {
                "all_empty_bars": 0,
                "partial_empty_bars": 0,
                "partial_empty_stock_events": 0,
                "max_empty_stocks": 0,
                "sample_all_empty_time": None,
                "sample_partial_empty_time": None,
                "sample_empty_stocks": [],
            }
            current_data_match_stats = {
                "direct": 0,
                "sec_ms": 0,
                "missing": 0,
                "no_ref": 0,
                "no_cache": 0,
            }

            for current_time in all_times:
                loop_start_time = time.time()
                if not self.is_running:
                    logging.warning("回测被中止")
                    break

                # ===== 动态数据加载：检查是否需要切换分段 =====
                if self.dynamic_loader_enabled and self.dynamic_loader:
                    # 获取当前时间所属分段
                    chunk_idx = self.dynamic_loader.get_chunk_for_time(current_time)

                    # 如果分段发生变化，需要加载新分段并重建缓存
                    if chunk_idx >= 0 and chunk_idx != self._last_chunk_idx:
                        # 加载新分段
                        current_chunk_data = self.dynamic_loader.ensure_data_loaded(current_time)

                        if current_chunk_data:
                            # 重建数据缓存
                            self._rebuild_data_cache(current_chunk_data)
                            if use_lazy_current_rows:
                                lazy_col_pos_cache = _build_lazy_col_pos_cache(self.historical_data_ref)
                            if use_lazy_current_data_container:
                                available_time_set = _build_available_time_set(self.time_idx_cache, self._is_ms_timestamp)
                            self._last_chunk_idx = chunk_idx

                            # 输出分段切换信息
                            if self.trader_callback:
                                mem_stats = self.dynamic_loader.get_memory_stats()
                                logging.info(f"切换到分段 {chunk_idx + 1}/{mem_stats['total_chunks']}, "
                                    f"当前内存: {mem_stats['total_mb']:.2f}MB")
                # ===== 动态数据加载结束 =====

                processed_times += 1
                # 根据计算的增量显示进度，但确保前几次都显示
                should_show_progress = False
                if processed_times <= 5:  # 前5次都显示
                    should_show_progress = True
                elif processed_times % progress_increment == 0:  # 按增量显示
                    should_show_progress = True
                elif processed_times == total_times:  # 最后一次也显示
                    should_show_progress = True
                
                if should_show_progress:
                    progress = (processed_times / total_times) * 100
                    if _progress_cb:
                        _progress_cb(int(progress))
                    # 只在需要输出日志时才记录进度文本
                    if self._should_log():
                        logging.info(f"回测进度: {progress:.2f}%")
                
                data_start_time = time.time()
                # time_info 在下方统一构造，此处仅初始化空字典
                current_data = {}
                empty_stocks = []
                stock_data_empty = True
                _current_data_stock_iter = stock_codes
                if use_lazy_current_data_container:
                    current_data = _LazyCurrentData(
                        stock_codes,
                        stock_codes_set,
                        self.historical_data_ref,
                        self.time_idx_cache,
                        current_time,
                        self._is_ms_timestamp,
                        use_lazy_current_rows,
                        lazy_col_pos_cache,
                        current_data_match_stats,
                    )
                    if available_time_set is not None:
                        try:
                            _ct = int(current_time)
                            stock_data_empty = _ct not in available_time_set
                        except Exception:
                            stock_data_empty = not current_data.has_any_stock_data()
                    else:
                        stock_data_empty = not current_data.has_any_stock_data()
                    _current_data_stock_iter = ()
                
                # 直接添加数据引用，而不是转换为字典
                # 按照stock_codes的顺序遍历，确保传递给策略的数据顺序一致
                for code in _current_data_stock_iter:
                    if code not in self.historical_data_ref:
                        current_data_match_stats["no_ref"] += 1
                        continue
                    if code in self.time_field_cache and code in self.time_idx_cache:
                        time_field = self.time_field_cache[code]
                        time_idx_map = self.time_idx_cache[code]
                        df = self.historical_data_ref[code]

                        idx, match_kind = _lookup_time_index(time_idx_map, current_time, self._is_ms_timestamp)
                        current_data_match_stats[match_kind] += 1
                        if idx is not None:
                            # 直接存储行引用，而不是转换为字典
                            current_data[code] = (
                                _LazyMarketRow(df, idx, lazy_col_pos_cache.get(code, {}))
                                if use_lazy_current_rows else df.iloc[idx]
                            )
                            stock_data_empty = False
                        else:
                            # 没有匹配的数据，存储空Series
                            current_data[code] = _EMPTY_MARKET_ROW if use_lazy_current_rows else pd.Series({})
                            empty_stocks.append(code)
                    else:
                        current_data_match_stats["no_cache"] += 1
                        # 没有时间字段的情况
                        current_data[code] = _EMPTY_MARKET_ROW if use_lazy_current_rows else pd.Series({})
                        empty_stocks.append(code)
                
                time_stats["构造数据"] += time.time() - data_start_time
                
                # 添加日志，显示第一个股票的数据示例（仅在需要输出日志时执行）
                if processed_times == 1 and self.trader_callback and current_data and self._should_log():
                    # 获取第一个股票代码
                    first_stock = None
                    for code in current_data:
                        if code != "__current_time__":
                            first_stock = code
                            break

                    if first_stock:
                        sample_data = current_data[first_stock]
                        logging.info(f"数据样例 - 股票: {first_stock}, 字段: {list(sample_data.keys())}")
                        # 打印每个字段的值（最多显示5个字段）
                        sample_str = ""
                        count = 0
                        for key, value in sample_data.items():
                            if count < 5:
                                sample_str += f"{key}: {value}, "
                                count += 1
                        if sample_str:
                            logging.info(f"部分字段值: {sample_str[:-2]}")
                
                # 构造时间信息：优先从预计算查找表取，超阈值时逐步计算
                time_info_start = time.time()
                if _time_info_lookup is not None:
                    time_info = _time_info_lookup[current_time]
                else:
                    try:
                        dt = datetime.datetime.fromtimestamp(current_time / 1000 if self._is_ms_timestamp else current_time)
                        time_info = {
                            "timestamp": current_time,
                            "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
                            "date": dt.strftime("%Y-%m-%d"),
                            "time": dt.strftime("%H:%M:%S"),
                            "raw_time": current_time,
                            "_dt": dt,   # 供触发器直接使用，跳过 fromtimestamp
                        }
                    except Exception:
                        s = str(current_time)
                        time_info = {"timestamp": current_time, "datetime": s, "date": s, "time": s, "raw_time": current_time, "_dt": None}

                current_data["__current_time__"] = time_info
                # 触发器直接从实例变量拿 datetime，免去每次 fromtimestamp 调用
                self._current_bar_dt = time_info.get("_dt")
                time_stats["构造时间信息"] += time.time() - time_info_start
                # 直接赋值，避免创建临时字典再 update
                current_data["__account__"] = self.trade_mgr.assets
                current_data["__positions__"] = self.trade_mgr.positions
                current_data["__stock_list__"] = stock_codes
                
                # 检查是否是新的一天
                new_day_start = time.time()
                if current_date != time_info["date"]:
                    # 如果有前一天的数据，执行盘后回调
                    post_market_start = time.time()
                    if current_date is not None and post_market_enabled and _has_post_market:
                        # 执行盘后回调
                        try:
                            logging.info(f"执行盘后回调 - 日期: {current_date}")
                            
                            # 设置时间信息为盘后时间
                            post_time_info = _callback_time_info(
                                time_info,
                                current_date,
                                post_market_time,
                                self._is_ms_timestamp,
                            )
                            
                            # 使用最后一个时间点的数据或创建一个完整的数据结构
                            post_data = _copy_callback_data(day_data)
                            post_data["__current_time__"] = post_time_info
                            
                            # 添加账户和持仓信息到数据字典
                            post_data["__account__"] = self.trade_mgr.assets
                            post_data["__positions__"] = self.trade_mgr.positions
                            post_data["__stock_list__"] = stock_codes
                            
                            # 添加框架实例到数据字典
                            post_data["__framework__"] = self
                            
                            # 执行盘后回调
                            post_signals = _invoke_strategy_callback(
                                self,
                                self.strategy_module.khPostMarket,
                                post_data,
                                getattr(_khimport, "_set_current_framework", None),
                            )
                            
                            # 处理盘后回调产生的信号
                            if post_signals:
                                # 计算次日日期（盘后订单的过期日期）
                                current_dt = datetime.datetime.strptime(current_date, "%Y-%m-%d")
                                next_day = current_dt + datetime.timedelta(days=1)
                                next_day_str = next_day.strftime("%Y-%m-%d")

                                for signal in post_signals:
                                    self._prepare_signal_price(
                                        signal,
                                        time_info["timestamp"],
                                        post_data,
                                    )
                                    signal['timestamp'] = time_info["timestamp"]
                                    # 标记为盘后信号
                                    signal['signal_source'] = 'post_market'
                                    signal['source_time'] = post_time_info["datetime"]
                                    # 设置挂单类型为限价单
                                    if 'order_type' not in signal:
                                        signal['order_type'] = 'limit'
                                    # 盘后订单：持续一个交易日（到次日收盘）
                                    if 'time_in_force' not in signal:
                                        signal['time_in_force'] = 'gtd'
                                    if 'expire_date' not in signal:
                                        signal['expire_date'] = next_day_str

                                # 转为挂单而非立即成交
                                for signal in post_signals:
                                    order_id = self.pending_order_mgr.add_order(signal, post_time_info)
                                    logging.info(f"盘后下单已转为挂单 - {order_id} {signal['action']} {signal['code']} "
                                        f"委托价:{signal['price']:.2f} 数量:{signal['volume']} (有效至:{signal.get('expire_date')} 收盘)")
                        except Exception as e:
                            logging.error(f"执行盘后回调时出错: {str(e)}")
                    _post_elapsed = time.time() - post_market_start
                    time_stats["盘后回调"] += _post_elapsed
                    # ===== 日终处理：过期当日有效挂单 =====
                    if self.pending_order_mgr:
                        self.pending_order_mgr.expire_day_orders(current_date)

                    # 更新当前日期
                    current_date = time_info["date"]
                    day_start_time = time_info["timestamp"]
                    day_data = current_data
                    if trigger_type == "1d":
                        self._reset_daily_trigger_state(current_date)

                    # T+1模式下，新交易日开始时将所有持仓的can_use_volume更新为volume
                    # 这样昨天买入的股票今天就可以卖出了
                    if not self.trade_mgr.t0_mode:
                        for code, pos in self.trade_mgr.positions.items():
                            if pos.get("volume", 0) > 0:
                                pos["can_use_volume"] = pos["volume"]
                        logging.info(f"新交易日 {current_date}: 已更新持仓可用数量 (T+1结算)")

                    # 检查是否需要执行盘前回调
                    pre_market_start = time.time()
                    if pre_market_enabled and _has_pre_market:
                        # 执行盘前回调
                        try:
                            logging.info(f"执行盘前回调 - 日期: {current_date}")
                            
                            # 设置时间信息为盘前时间
                            pre_time_info = _callback_time_info(
                                time_info,
                                current_date,
                                pre_market_time,
                                self._is_ms_timestamp,
                            )
                            
                            # 使用当前时间点的数据或创建一个完整的数据结构
                            pre_data = _copy_callback_data(current_data)
                            pre_data["__current_time__"] = pre_time_info
                            
                            # 确保包含账户和持仓信息
                            pre_data["__account__"] = self.trade_mgr.assets
                            pre_data["__positions__"] = self.trade_mgr.positions
                            pre_data["__stock_list__"] = stock_codes
                            
                            # 添加框架实例到数据字典
                            pre_data["__framework__"] = self
                            
                            # 执行盘前回调
                            pre_signals = _invoke_strategy_callback(
                                self,
                                self.strategy_module.khPreMarket,
                                pre_data,
                                getattr(_khimport, "_set_current_framework", None),
                            )
                            
                            # 处理盘前回调产生的信号
                            if pre_signals:
                                for signal in pre_signals:
                                    self._prepare_signal_price(
                                        signal,
                                        time_info["timestamp"],
                                        pre_data,
                                    )
                                    signal['timestamp'] = time_info["timestamp"]
                                    # 标记为盘前信号
                                    signal['signal_source'] = 'pre_market'
                                    signal['source_time'] = pre_time_info["datetime"]
                                    # 设置挂单类型为限价单
                                    if 'order_type' not in signal:
                                        signal['order_type'] = 'limit'
                                    # 设置有效期为当日有效
                                    if 'time_in_force' not in signal:
                                        signal['time_in_force'] = 'day'

                                # 转为挂单而非立即成交
                                for signal in pre_signals:
                                    order_id = self.pending_order_mgr.add_order(signal, pre_time_info)
                                    logging.info(f"盘前下单已转为挂单 - {order_id} {signal['action']} {signal['code']} "
                                        f"委托价:{signal['price']:.2f} 数量:{signal['volume']}")
                        except Exception as e:
                            logging.error(f"执行盘前回调时出错: {str(e)}")
                    _pre_elapsed = time.time() - pre_market_start
                    time_stats["盘前回调"] += _pre_elapsed
                else:
                    _post_elapsed = 0.0
                    _pre_elapsed = 0.0
                    # 更新当天的数据
                    day_data = current_data
                # 检查新日期的纯行政耗时：总耗时减去已单独计时的盘后/盘前，避免双重计入
                time_stats["检查新日期"] += time.time() - new_day_start - _post_elapsed - _pre_elapsed

                # ===== 撮合引擎：检查挂单是否可成交 =====
                pending_fill_signals = []  # 存储挂单成交信号
                if self.pending_order_mgr:
                    pending_market_data = self._pending_execution_market_data(
                        current_data,
                        time_info["timestamp"],
                    )
                    filled_orders = self.pending_order_mgr.check_pending_orders(
                        pending_market_data,
                        time_info["timestamp"],
                        time_info["date"],
                        self._match_config
                    )
                    # 执行挂单成交
                    for order in filled_orders:
                        fill_executed = self.trade_mgr.execute_pending_fill(
                            order,
                            pending_market_data,
                        )

                        # 构造成交信号，用于后续record_results记录
                        if fill_executed:
                            fill_signal = {
                                "code": order["code"],
                                "action": order["action"],
                                "price": order["fill_price"],
                                "actual_price": order["fill_price"],  # 实际成交价
                                "volume": order["remaining_volume"],
                                "reason": f"{order.get('reason', '')} [挂单成交]",
                                "remark": order.get("remark", ""),
                                "timestamp": order.get("fill_timestamp", 0)  # 使用成交时间戳
                            }
                            pending_fill_signals.append(fill_signal)

                # 使用触发器判断是否应该触发策略
                trigger_start = time.time()
                if not self.trigger.should_trigger(current_time, current_data):
                    time_stats["触发器检查"] += time.time() - trigger_start
                    continue
                time_stats["触发器检查"] += time.time() - trigger_start
                
                # 风控检查
                risk_start = time.time()
                if not self.risk_mgr.check_risk(current_data):
                    time_stats["风控检查"] += time.time() - risk_start
                    continue
                time_stats["风控检查"] += time.time() - risk_start
                
                # 检查是否是交易日（time_info 已在当前作用域，无需二次查字典）
                current_date_str = time_info["date"]
                if current_date_str and not self.tools.is_trade_day(current_date_str):
                    # 如果不是交易日，跳过策略调用
                    continue
                
                # 添加框架实例到数据字典
                current_data["__framework__"] = self
                
                # 如果所有股票数据都为空，记录错误并跳过策略调用
                if stock_data_empty:
                    current_time_str = time_info["datetime"]
                    if use_lazy_current_data_container and not empty_stocks:
                        empty_stocks = current_data.empty_stocks()
                    empty_data_summary["all_empty_bars"] += 1
                    if empty_data_summary["sample_all_empty_time"] is None:
                        empty_data_summary["sample_all_empty_time"] = current_time_str
                    if self.trader_callback and (
                        empty_data_log_mode == "verbose"
                        or empty_data_summary["all_empty_bars"] <= 3
                        or empty_data_summary["all_empty_bars"] % 100 == 0
                    ):
                        logging.warning(f"警告: 时间点 {current_time_str} 的所有股票数据为空，跳过策略调用")
                        if empty_stocks:
                            logging.warning(f"空数据股票列表: {', '.join(empty_stocks[:10])}" +
                                (f" 等{len(empty_stocks)}只股票" if len(empty_stocks) > 10 else ""))
                    continue
                
                # 如果有部分股票数据为空，记录警告但继续执行
                if empty_stocks:
                    current_time_str = time_info["datetime"]
                    empty_data_summary["partial_empty_bars"] += 1
                    empty_data_summary["partial_empty_stock_events"] += len(empty_stocks)
                    empty_data_summary["max_empty_stocks"] = max(
                        empty_data_summary["max_empty_stocks"], len(empty_stocks)
                    )
                    if empty_data_summary["sample_partial_empty_time"] is None:
                        empty_data_summary["sample_partial_empty_time"] = current_time_str
                        empty_data_summary["sample_empty_stocks"] = empty_stocks[:10]
                    if (
                        empty_data_log_mode == "verbose"
                        or empty_data_summary["partial_empty_bars"] <= 3
                        or empty_data_summary["partial_empty_bars"] % 100 == 0
                    ):
                        logging.warning(f"警告: 时间点 {current_time_str} 有 {len(empty_stocks)} 只股票数据为空: {', '.join(empty_stocks[:5])}" +
                            (f" 等" if len(empty_stocks) > 5 else ""))
                
                _set_framework_fn = getattr(_khimport, "_set_current_framework", None)
                while True:
                    if trigger_type == "1d":
                        if self.daily_trigger_count >= self.daily_trigger_cap:
                            break
                        self.daily_trigger_count += 1
                        self._daily_trigger_requested = False
                    strategy_start = time.time()
                    try:
                        if callable(_set_framework_fn):
                            _set_framework_fn(self)
                        try:
                            signals = self.strategy_module.khHandlebar(current_data)
                        except KeyError as e:
                            friendly_message = self._format_strategy_key_error(e, current_data, time_info)
                            if friendly_message:
                                raise RuntimeError(friendly_message) from e
                            raise
                    finally:
                        if callable(_set_framework_fn):
                            _set_framework_fn(None)
                    time_stats["策略处理"] += time.time() - strategy_start
                    
                    # 处理信号中的价格精度
                    signal_process_start = time.time()
                    if signals:
                        for signal in signals:
                            self._prepare_signal_price(
                                signal,
                                time_info["timestamp"],
                                current_data,
                            )
                            # 添加当前回测时间戳
                            signal['timestamp'] = current_time
                    time_stats["处理信号"] += time.time() - signal_process_start
                    
                    # 发送交易指令
                    trade_start = time.time()
                    if signals:
                        time_stats.setdefault("_信号总数", 0)
                        time_stats["_信号总数"] += len(signals)
                        # 使用撮合引擎处理信号
                        self._process_signals_with_match_engine(signals, current_data, time_info)
                    time_stats["交易指令"] += time.time() - trade_start
                    
                    # 记录结果（合并策略信号和挂单成交信号）
                    record_start = time.time()
                    all_signals = (signals or []) + (pending_fill_signals or [])
                    self.record_results(current_time, current_data, all_signals, time_info)
                    time_stats["记录结果"] += time.time() - record_start
                    
                    time_stats["总时间"] += time.time() - loop_start_time
                    
                    if trigger_type == "1d" and self._daily_trigger_requested and self.daily_trigger_count < self.daily_trigger_cap:
                        continue
                    break
            
            _mark_phase("6.主循环(逐bar)")
            logging.info(
                "current_data match stats: direct=%s sec_ms=%s missing=%s no_ref=%s no_cache=%s",
                current_data_match_stats["direct"],
                current_data_match_stats["sec_ms"],
                current_data_match_stats["missing"],
                current_data_match_stats["no_ref"],
                current_data_match_stats["no_cache"],
            )
            if empty_data_summary["all_empty_bars"] or empty_data_summary["partial_empty_bars"]:
                logging.warning(
                    "【空数据汇总】全空bar=%s；部分空bar=%s；累计空股票事件=%s；单bar最大空股票数=%s；"
                    "首个全空时间=%s；首个部分空时间=%s；样例股票=%s",
                    empty_data_summary["all_empty_bars"],
                    empty_data_summary["partial_empty_bars"],
                    empty_data_summary["partial_empty_stock_events"],
                    empty_data_summary["max_empty_stocks"],
                    empty_data_summary["sample_all_empty_time"],
                    empty_data_summary["sample_partial_empty_time"],
                    ", ".join(empty_data_summary["sample_empty_stocks"][:10]),
                )
            if self.trader_callback:
                total_time = time_stats["总时间"]
                if total_time > 0:
                    logging.info("回测各部分执行时间统计:")
                    for key, value in time_stats.items():
                        if key != "总时间" and not key.startswith("_"):
                            percentage = (value / total_time) * 100
                            logging.info(f"{key}: {value:.4f}秒 ({percentage:.2f}%)")
                    # 兜底：显示未被任何计时器覆盖的循环开销（进度更新、动态加载检查等）
                    accounted = sum(v for k, v in time_stats.items() if k != "总时间" and not k.startswith("_"))
                    other_overhead = total_time - accounted
                    if other_overhead > 0.001:
                        logging.info(f"其它开销: {other_overhead:.4f}秒 ({other_overhead / total_time * 100:.2f}%)")
                    # 显示信号统计（用于诊断"交易指令"耗时）
                    sig_count = time_stats.get("_信号总数", 0)
                    if sig_count > 0:
                        trade_time = time_stats.get("交易指令", 0)
                        logging.info(f"交易信号统计: 共处理 {sig_count} 个信号，平均每信号耗时 {trade_time/sig_count*1000:.1f}ms")
                    strategy_time = time_stats.get("策略处理", 0)
                    framework_time = max(0.0, total_time - strategy_time)
                    if strategy_time > 0:
                        logging.info(f"策略回调耗时: {strategy_time:.4f}秒 ({strategy_time / total_time * 100:.2f}%)")
                    logging.info(f"框架总耗时: {framework_time:.4f}秒 ({framework_time / total_time * 100:.2f}%)")
                    max_key = None
                    max_value = -1.0
                    for key, value in time_stats.items():
                        if key == "总时间" or key.startswith("_"):
                            continue
                        if value > max_value:
                            max_value = value
                            max_key = key
                    if max_key:
                        logging.info(f"耗时最长: {max_key} {max_value:.4f}秒 ({max_value / total_time * 100:.2f}%)")
                    logging.info(f"总执行时间: {total_time:.4f}秒")

            # ===== 性能剖析：阶段级墙钟汇总（始终打印，便于定位耗时大头）=====
            try:
                _bt_wall_total = time.time() - _bt_wall_start
                logging.info("=" * 60)
                logging.info("【性能剖析】_run_backtest 阶段级墙钟耗时:")
                _ph_accounted = 0.0
                for _pname in sorted(self._phase_times.keys()):
                    _pval = self._phase_times[_pname]
                    _pct = (_pval / _bt_wall_total * 100) if _bt_wall_total > 0 else 0.0
                    logging.info(f"  {_pname:<32s} {_pval:9.2f}s ({_pct:5.1f}%)")
                    _ph_accounted += _pval
                _ph_other = _bt_wall_total - _ph_accounted
                if abs(_ph_other) > 0.01:
                    logging.info(f"  {'(其它未计阶段)':<32s} {_ph_other:9.2f}s ({_ph_other / _bt_wall_total * 100 if _bt_wall_total>0 else 0:5.1f}%)")
                if self._phase_sub:
                    logging.info("  ── 细分(子项，已含在上面对应阶段内) ──")
                    for _sname, _sval in self._phase_sub.items():
                        logging.info(f"    └{_sname:<30s} {_sval:9.2f}s")
                try:
                    import khQTTools as _khqt_stats
                    if hasattr(_khqt_stats, "get_khHistory_cache_stats"):
                        _hs = _khqt_stats.get_khHistory_cache_stats()
                        logging.info("  ── khHistory 窗口缓存统计 ──")
                        logging.info(
                            "    entries=%s hits=%s misses=%s extensions=%s rows=%s memory=%.2fMB "
                            "mem_hits=%s mem_misses=%s prelisting_hits=%s partial_history_hits=%s "
                            "full_prefetch=%s current_day_prefetch=%s fallback=%s",
                            int(_hs.get("entries", 0)),
                            int(_hs.get("hits", 0)),
                            int(_hs.get("misses", 0)),
                            int(_hs.get("extensions", 0)),
                            int(_hs.get("rows_cached", 0)),
                            float(_hs.get("bytes_cached", 0)) / (1024 * 1024),
                            int(_hs.get("memory_fastpath_hits", 0)),
                            int(_hs.get("memory_fastpath_misses", 0)),
                            int(_hs.get("memory_fastpath_prelisting_hits", 0)),
                            int(_hs.get("memory_fastpath_partial_history_hits", 0)),
                            int(_hs.get("full_prefetch_reads", 0)),
                            int(_hs.get("current_day_prefetch_reads", 0)),
                            int(_hs.get("fallback_reads", 0)),
                        )
                except Exception:
                    pass
                logging.info(f"  {'_run_backtest 墙钟合计':<32s} {_bt_wall_total:9.2f}s")
                # 全局 DuckDB 读取统计：含框架批量加载 + khHistory + 基准，三条路径合计
                try:
                    from khDataSource import get_read_stats as _get_read_stats
                    _rs = _get_read_stats()
                    _load_db = self._phase_sub.get("行情DB读取(get_market_data_ex)", 0.0)
                    _khist_db = max(0.0, _rs["seconds"] - _load_db)
                    logging.info("  ── DuckDB 读取分解(get_market_data_ex 全局) ──")
                    logging.info(f"    {'读取总耗时':<28s} {_rs['seconds']:9.2f}s  调用{int(_rs['calls'])}次 累计{int(_rs['stocks'])}股次")
                    logging.info(f"    {'└框架批量加载(载入内存)':<28s} {_load_db:9.2f}s")
                    logging.info(f"    {'└khHistory等其它读取':<28s} {_khist_db:9.2f}s")
                except Exception:
                    pass
                logging.info("=" * 60)
            except Exception as _e:
                logging.warning(f"阶段计时汇总输出失败: {_e}")

            # 处理最后一天的盘后回调
            if current_date is not None and post_market_enabled and _has_post_market:
                try:
                    logging.info(f"执行最后一天的盘后回调 - 日期: {current_date}")
                    
                    # 设置时间信息为盘后时间
                    base_time_info = day_data.get("__current_time__", {}) if day_data else {}
                    time_info = _callback_time_info(
                        base_time_info,
                        current_date,
                        post_market_time,
                        self._is_ms_timestamp,
                    )
                    
                    # 使用最后一个时间点的数据或创建一个完整的数据结构
                    post_data = _copy_callback_data(day_data)
                    post_data["__current_time__"] = time_info
                    
                    # 添加账户和持仓信息到数据字典
                    post_data["__account__"] = self.trade_mgr.assets
                    post_data["__positions__"] = self.trade_mgr.positions
                    post_data["__stock_list__"] = self.get_stock_list()
                    
                    # 添加框架实例到数据字典
                    post_data["__framework__"] = self
                    
                    # 执行盘后回调
                    post_signals = _invoke_strategy_callback(
                        self,
                        self.strategy_module.khPostMarket,
                        post_data,
                        getattr(_khimport, "_set_current_framework", None),
                    )
                    
                    # 处理盘后回调产生的信号
                    if post_signals:
                        for signal in post_signals:
                            self._prepare_signal_price(
                                signal,
                                time_info["timestamp"],
                                post_data,
                            )
                            signal['timestamp'] = time_info["timestamp"]
                        
                        # 发送交易指令
                        self.trade_mgr.process_signals(post_signals)
                except Exception as e:
                    logging.error(f"执行最后一天的盘后回调时出错: {str(e)}")
                
            # 回测完成后停止策略并更新状态
            self.is_running = False
            logging.info("回测进度: 100.00%")
            logging.info("回测完成")

            # ===== 清理动态加载器 =====
            if self.dynamic_loader_enabled and self.dynamic_loader:
                self.dynamic_loader.cleanup()
                logging.info("动态数据加载器已清理")
            # ===== 清理结束 =====

            # 检查是否应该保存回测记录
            # 如果用户点击停止并且启用了"停止后直接退出"，则跳过保存
            if not self.save_results_on_stop:
                logging.info("已跳过保存回测记录（停止后直接退出模式）")
                return  # 不保存结果，直接返回

            # 在回测完成后保存回测记录
            try:
                lbl_cb = self.ui_settings.get('set_progress_label_callback')
                if lbl_cb:
                    lbl_cb("回测收尾: 准备保存")
                prog_cb = self.ui_settings.get('update_progress_callback')
                if prog_cb:
                    prog_cb(0)

                post_total_steps = 6
                post_step = 0

                def _update_post_progress(message: str):
                    nonlocal post_step
                    post_step += 1
                    lbl_cb = self.ui_settings.get('set_progress_label_callback')
                    if lbl_cb:
                        lbl_cb(f"回测收尾: {message}")
                    prog_cb = self.ui_settings.get('update_progress_callback')
                    if prog_cb:
                        prog_cb(int(post_step * 100 / post_total_steps))

                # 获取策略文件名（不含路径和扩展名）
                strategy_file = self.config.config_dict.get("strategy_file", "")
                strategy_name = os.path.splitext(os.path.basename(strategy_file))[0] if strategy_file else "unknown"
                
                # 生成回测时间戳
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                
                # 创建当前回测的子目录（包含策略名）
                backtest_dir = os.path.join(
                    self._get_writable_backtest_dir(),
                    backtest_dir_name
                )

                # 创建新目录（由于包含时间戳，目录名唯一，无需删除）
                os.makedirs(backtest_dir, exist_ok=True)
                _update_post_progress("创建回测结果目录")

                def _safe_to_csv(df: pd.DataFrame, target_path: str, desc: str):
                    """处理共享冲突时的容错写入"""
                    for attempt in range(1, 4):
                        try:
                            df.to_csv(target_path, index=False, encoding='utf-8-sig')
                            return
                        except PermissionError:
                            wait_time = 0.2 * attempt
                            logging.warning(f"{desc}保存失败（文件被占用），{wait_time:.1f}s后重试({attempt}/3)")
                            time.sleep(wait_time)
                        except Exception as e:
                            self._log(f"{desc}保存失败: {e}", "ERROR")
                            raise

                # 保存交易记录
                trades_df = pd.DataFrame(self.backtest_records['trades'])
                if len(trades_df) == 0:
                    trades_df = pd.DataFrame(columns=[
                        'datetime', 'code', 'action', 'price', 'volume', 'amount',
                        'commission', 'stamp_tax', 'transfer_fee', 'flow_fee',
                        'total_asset', 'cash', 'market_value'
                    ])
                    logging.warning("回测期间没有产生交易记录")
                _safe_to_csv(trades_df, os.path.join(backtest_dir, "trades.csv"), "交易记录")
                _update_post_progress("保存交易记录")

                # 保存每日统计数据
                daily_stats_df = pd.DataFrame(self.backtest_records['daily_stats'])
                if len(daily_stats_df) == 0:
                    daily_stats_df = pd.DataFrame(columns=[
                        'date', 'total_asset', 'cash', 'market_value', 
                        'daily_return', 'benchmark_close', 'positions'
                    ])
                    logging.warning("回测期间没有产生每日统计数据")
                _safe_to_csv(daily_stats_df, os.path.join(backtest_dir, "daily_stats.csv"), "每日统计数据")
                _update_post_progress("保存每日统计")

                # 保存回测汇总指标
                try:
                    if len(daily_stats_df) >= 2:
                        init_capital = self.backtest_records['init_capital']
                        final_capital = daily_stats_df['total_asset'].iloc[-1]

                        first_date = pd.to_datetime(daily_stats_df['date'].iloc[0]).strftime('%Y-%m-%d')
                        last_date = pd.to_datetime(daily_stats_df['date'].iloc[-1]).strftime('%Y-%m-%d')
                        trade_days = self.tools.get_trade_days_count(first_date, last_date)
                        if trade_days <= 0:
                            trade_days = (pd.to_datetime(last_date) - pd.to_datetime(first_date)).days

                        # 计算总收益率
                        total_return = (final_capital - init_capital) / init_capital * 100 if init_capital > 0 else 0

                        # 计算年化收益率: ((1+R)^(250/n)-1)*100%
                        if trade_days > 0 and init_capital > 0:
                            total_return_decimal = (final_capital / init_capital) - 1
                            annual_return = (pow(1 + total_return_decimal, 250/trade_days) - 1) * 100
                        else:
                            annual_return = 0

                        # 计算最大回撤
                        cummax = daily_stats_df['total_asset'].cummax()
                        drawdown = (cummax - daily_stats_df['total_asset']) / cummax * 100
                        max_drawdown = drawdown.max()

                        # 保存汇总指标
                        summary = {
                            'init_capital': init_capital,
                            'final_capital': final_capital,
                            'total_return': total_return,
                            'annual_return': annual_return,
                            'max_drawdown': max_drawdown,
                            'trade_days': trade_days,
                            'duckdb_lock_skipped_count': len(self._duckdb_lock_skipped),
                            'duckdb_lock_skipped': '; '.join(
                                f"{item.get('stock')} {item.get('period')}"
                                for item in self._duckdb_lock_skipped
                            ),
                        }
                        _safe_to_csv(pd.DataFrame([summary]), os.path.join(backtest_dir, "summary.csv"), "回测汇总")
                        if self._duckdb_lock_skipped:
                            _safe_to_csv(
                                pd.DataFrame(self._duckdb_lock_skipped),
                                os.path.join(backtest_dir, "duckdb_lock_skips.csv"),
                                "数据库占用跳过清单",
                            )
                except Exception as e:
                    logging.warning(f"保存回测汇总指标时出错: {str(e)}")
                _update_post_progress("保存回测汇总")

                # 保存基准指数数据（前推10天以包含前一交易日收盘价，供报告计算基准起点）
                benchmark_code = self.config.config_dict["backtest"]["benchmark"]
                _bench_early = (pd.to_datetime(self.config.backtest_start, format='%Y%m%d') - pd.Timedelta(days=10)).strftime('%Y%m%d')
                try:
                    benchmark_data = self.data_source_mgr.get_market_data(
                        field_list=['close'],
                        stock_list=[benchmark_code],
                        period='1d',
                        start_time=_bench_early,
                        end_time=self.config.backtest_end,
                        fill_data=True,
                    )
                    
                    logging.info(f"基准数据获取结果: {benchmark_data.keys()}")

                    if benchmark_data and 'close' in benchmark_data and len(benchmark_data['close']) > 0:
                        closes = benchmark_data['close'].values[0]  # 获取收盘价数据
                        if len(closes) > 0:
                            # 直接从benchmark_data中获取日期数据
                            # 获取交易日期索引
                            if 'date' in benchmark_data:
                                dates = benchmark_data['date'][0]  # 使用benchmark_data中的日期
                            elif hasattr(benchmark_data, 'index') and benchmark_data.index is not None and not isinstance(benchmark_data.index, pd.RangeIndex):
                                dates = benchmark_data.index  # 有些情况下日期可能在索引中
                            elif hasattr(benchmark_data['close'], 'columns') and len(benchmark_data['close'].columns) > 0:
                                # 从columns中获取日期（日期作为列名出现的情况）
                                date_cols = [col for col in benchmark_data['close'].columns if str(col).isdigit()]
                                if date_cols:
                                    # 将列名转换为日期对象
                                    dates = pd.to_datetime(date_cols, format='%Y%m%d')
                                    # 确保closes的顺序与dates匹配
                                    closes = np.array([benchmark_data['close'].iloc[0][col] for col in date_cols])
                                else:
                                    # 如果没有日期数据，才使用日期范围（不推荐）
                                    logging.warning("警告：基准数据中没有日期信息，将使用日期范围替代，可能不准确")
                                    dates = pd.date_range(
                                        start=pd.to_datetime(self.config.backtest_start, format='%Y%m%d'),
                                        end=pd.to_datetime(self.config.backtest_end, format='%Y%m%d'),
                                        freq='B'  # 使用工作日频率
                                    )
                                
                                # 创建包含日期和收盘价的DataFrame
                                df = pd.DataFrame({
                                    'date': dates,
                                    'close': closes
                                })
                                
                                # 保存到benchmark.csv
                                benchmark_file = os.path.join(backtest_dir, "benchmark.csv")
                                _safe_to_csv(df, benchmark_file, "基准数据")
                                
                                logging.info(f"基准指数数据已保存到 {benchmark_file}, 共 {len(df)} 条记录")
                        else:
                            logging.warning(f"基准指数 {benchmark_code} 收盘价数据为空")
                    else:
                        logging.warning(f"基准指数 {benchmark_code} 数据获取失败")
                except Exception as e:
                    logging.error(f"获取基准指数数据时出错: {str(e)}")
                    logging.error(f"获取基准指数数据时出错: {str(e)}", exc_info=True)
                _update_post_progress("保存基准数据")
                
                # 保存策略文件副本
                strategy_file_path = getattr(self, "strategy_file", "") or resolve_strategy_file(
                    self.config.config_dict.get("strategy_file", ""),
                    config_path=getattr(self.config, "config_path", None),
                    extra_base_dirs=[os.path.dirname(os.path.abspath(__file__))],
                )
                if strategy_file_path and os.path.exists(strategy_file_path):
                    try:
                        # 保存.py文件
                        strategy_filename = os.path.basename(strategy_file_path)
                        strategy_backup_path = os.path.join(backtest_dir, strategy_filename)
                        shutil.copy2(strategy_file_path, strategy_backup_path)
                        
                        # 查找并保存对应的.kh文件
                        kh_file_path = os.path.splitext(strategy_file_path)[0] + ".kh"
                        if os.path.exists(kh_file_path):
                            kh_filename = os.path.basename(kh_file_path)
                            kh_backup_path = os.path.join(backtest_dir, kh_filename)
                            shutil.copy2(kh_file_path, kh_backup_path)
                            logging.info(f"策略配置文件已保存: {kh_filename}")
                        
                        logging.info(f"策略文件已保存: {strategy_filename}")
                            
                    except Exception as e:
                        logging.error(f"保存策略文件时出错: {str(e)}")
                        logging.error(f"保存策略文件时出错: {str(e)}", exc_info=True)
                
                # 保存完整的配置文件副本
                try:
                    config_file_path = getattr(self.config, 'config_path', None)
                    if config_file_path and os.path.exists(config_file_path):
                        config_filename = os.path.basename(config_file_path)
                        config_backup_path = os.path.join(backtest_dir, f"full_{config_filename}")
                        shutil.copy2(config_file_path, config_backup_path)
                        logging.info(f"完整配置文件已保存: full_{config_filename}")
                except Exception as e:
                    logging.error(f"保存完整配置文件时出错: {str(e)}")
                    logging.error(f"保存完整配置文件时出错: {str(e)}", exc_info=True)
                
                # 保存回测配置信息
                config_info = {
                    'start_time': self.backtest_records['start_time'],
                    'end_time': self.backtest_records['end_time'],
                    'init_capital': self.backtest_records['init_capital'],
                    'benchmark': self.config.config_dict["backtest"]["benchmark"],
                    'strategy_file': self.config.config_dict.get("strategy_file", ""),  # 从配置字典中获取策略文件路径
                    'actual_start_time': datetime.datetime.fromtimestamp(self.start_time).strftime("%Y-%m-%d %H:%M:%S") if self.start_time else "",
                    'actual_end_time': datetime.datetime.fromtimestamp(self.end_time).strftime("%Y-%m-%d %H:%M:%S") if self.end_time else "",
                    'total_runtime_seconds': self.total_runtime,
                    'total_runtime_formatted': self._format_runtime(self.total_runtime),
                    # 保存股票池信息
                    'stock_list': ','.join(self.get_stock_list()) if hasattr(self, 'get_stock_list') else '',
                    'min_volume': self.config.config_dict["backtest"].get("min_volume", 100),
                    'kline_period': self.config.config_dict["data"].get("kline_period", "1m"),
                    'dividend_type': self.config.config_dict["data"].get("dividend_type", "front"),
                    # 历史 HTML 报告须回读与本次回测一致的行情库，不能依赖之后可能
                    # 被用户切换的全局设置。
                    'duckdb_data_path': str(getattr(self.data_source_mgr, 'duckdb_path', '') or ''),
                }
                try:
                    _perf_cfg_save = self.config.config_dict.get("performance", {}) or {}
                    _mem_decision_save = _perf_cfg_save.get("memory_profile_decision", {}) or {}
                    config_info.update({
                        'memory_profile': _perf_cfg_save.get("memory_profile", ""),
                        'memory_profile_effective': _perf_cfg_save.get("memory_profile_effective", ""),
                        'memory_profile_source': _mem_decision_save.get("source", ""),
                        'memory_profile_estimated_rows': _mem_decision_save.get("estimated_rows", ""),
                        'memory_profile_estimated_working_set_mb': _mem_decision_save.get("estimated_working_set_mb", ""),
                        'memory_profile_retry_from': _perf_cfg_save.get("memory_profile_retry_from", ""),
                        'memory_profile_retry_attempt': _perf_cfg_save.get("memory_profile_retry_attempt", ""),
                    })
                except Exception:
                    pass
                _safe_to_csv(pd.DataFrame([config_info]), os.path.join(backtest_dir, "config.csv"), "配置数据")
                _update_post_progress("保存配置与收尾")

                
                logging.info(f"回测记录已保存到目录: {backtest_dir}")
                # 记录回测总耗时
                logging.info(f"回测总耗时: {self._format_runtime(self.total_runtime)}")
                
                # 调用显示结果窗口回调（如果提供）
                show_result_cb = self.ui_settings.get('show_backtest_result_callback')
                if show_result_cb:
                    show_result_cb(backtest_dir)
                
            except Exception as e:
                logging.error(f"保存回测记录时出错: {str(e)}")
                logging.error(f"保存回测记录时出错: {str(e)}", exc_info=True)
                
        except Exception as e:
            error_msg = "回测运行异常: " + str(e)
            logging.error(error_msg, exc_info=True)
            # 调用错误回调函数
            if self.trader_callback:
                logging.error(error_msg)
                import traceback
                logging.error(f"错误详情:\n{traceback.format_exc()}")
            raise  # 重新抛出异常

    def record_results(self, timestamp, data, signals, time_info=None):
        """记录回测结果
        
        Args:
            timestamp:  当前时间戳（int）
            data:       当前市场数据
            signals:    交易信号列表
            time_info:  由主循环预计算的时间信息字典（有则直接使用，跳过时间转换）
        """
        try:
            # 1. 时间戳处理：优先使用主循环传入的预计算结果
            if time_info is not None:
                _dt_obj = time_info.get("_dt")
                if _dt_obj is not None:
                    current_time = _dt_obj
                    current_date = _dt_obj.date()
                else:
                    current_time = datetime.datetime.strptime(time_info["datetime"], "%Y-%m-%d %H:%M:%S")
                    current_date = datetime.datetime.strptime(time_info["date"], "%Y-%m-%d").date()
                current_ts_seconds = timestamp / 1000 if self._is_ms_timestamp else timestamp
            elif isinstance(timestamp, str):
                if self._cached_timestamp.get('str') == timestamp:
                    current_time      = self._cached_timestamp['datetime']
                    current_date      = self._cached_timestamp['date']
                    current_ts_seconds = self._cached_timestamp['ts_seconds']
                else:
                    current_time      = datetime.datetime.strptime(timestamp, "%Y%m%d%H%M%S")
                    current_date      = current_time.date()
                    current_ts_seconds = current_time.timestamp()
                    self._cached_timestamp = {'str': timestamp, 'datetime': current_time,
                                              'date': current_date, 'ts_seconds': current_ts_seconds}
            else:
                ts_seconds = timestamp / 1000 if self._is_ms_timestamp else float(timestamp)
                if abs(self._cached_timestamp.get('ts_seconds', -1e18) - ts_seconds) < 0.1:
                    current_time      = self._cached_timestamp['datetime']
                    current_date      = self._cached_timestamp['date']
                    current_ts_seconds = self._cached_timestamp['ts_seconds']
                else:
                    current_time      = datetime.datetime.fromtimestamp(ts_seconds)
                    current_date      = current_time.date()
                    current_ts_seconds = ts_seconds
                    self._cached_timestamp = {'ts_seconds': ts_seconds, 'datetime': current_time,
                                              'date': current_date}
            
            # 2. 交易日检查优化 - 使用缓存避免重复查询（缓存字典已在 __init__ 初始化）
            cache_key = f"trade_day_{current_date}"
            if cache_key in self._cached_trade_days:
                is_trading_day = self._cached_trade_days[cache_key]
            else:
                try:
                    date_str = time_info["date"] if time_info is not None else current_date.strftime("%Y-%m-%d")
                    is_trading_day = self.tools.is_trade_day(date_str)
                    # 缓存结果
                    self._cached_trade_days[cache_key] = is_trading_day
                except Exception as e:
                    logging.warning(f"检查交易日失败: {str(e)}")
                    is_trading_day = True  # 出错默认为交易日

            if is_trading_day:
                self._apply_etf_total_return_distributions(
                    current_date,
                    timestamp,
                    data,
                )
                    
            # 3. 持仓更新优化 - 预先获取并缓存持仓列表
            positions = self.trade_mgr.positions
            position_codes = list(positions.keys())
            
            # 4. 非交易日处理优化
            if not is_trading_day:
                # 非交易日情况下，不更新持仓市值
                # 只记录每日统计数据，使用前一个交易日的市值数据
                total_market_value = 0.0
                for code, position in positions.items():
                    # 使用已记录的市值，不从当天数据获取
                    if 'market_value' in position and position['market_value'] > 0:
                        total_market_value += position['market_value']
            else:
                # 5. 交易日市值计算优化 - 批量获取价格并一次性更新
                # 预先创建价格字典并批量填充
                prices = {}
                
                # 一次性从数据中提取所有价格
                # 注意：停牌/缺数据时 close、current_price 可能为 pandas NA，
                # 直接用 `NA > 0` 比较会抛 TypeError，故每步都先用 pd.notna 过滤，
                # 且只把“有效（非NA）”价格写入 prices，避免 NA 渗入 current_price 反复触发。
                for code in position_codes:
                    price_val = None
                    # 先检查lastPrice判断是否是tick数据（tick数据的close字段值为nan）
                    if code in data:
                        d = data[code]
                        if 'lastPrice' in d and pd.notna(d['lastPrice']):
                            # Tick数据：优先使用lastPrice字段
                            price_val = d['lastPrice']
                        elif 'close' in d and pd.notna(d['close']):
                            # K线数据：使用close字段
                            price_val = d['close']
                    # data 中无有效价格时，回退到持仓里的现价/成本价
                    if price_val is None and code in positions:
                        cp = positions[code].get('current_price')
                        if pd.notna(cp) and cp > 0:
                            price_val = cp
                        elif 'avg_price' in positions[code]:
                            price_val = positions[code]['avg_price']
                    price_val = self._portfolio_mark_price(code, timestamp, price_val)
                    if price_val is not None and pd.notna(price_val):
                        prices[code] = price_val
                
                # 一次性计算所有持仓的市值和盈亏
                total_market_value = 0.0
                for code in position_codes:
                    if code in prices:
                        position = positions[code]
                        current_price = prices[code]
                        volume = position['volume']
                        avg_price = position['avg_price']
                        
                        # 计算市值和盈亏
                        market_value = current_price * volume
                        position['market_value'] = market_value
                        position['current_price'] = current_price
                        position['profit'] = (current_price - avg_price) * volume
                        position['profit_ratio'] = (current_price - avg_price) / avg_price if avg_price != 0 else 0
                        
                        total_market_value += market_value
            
            # 6. 资产更新优化
            assets = self.trade_mgr.assets
            old_total_asset = assets.get('total_asset', 0)
            old_market_value = assets.get('market_value', 0)
            
            # 非交易日且没有交易信号时，市值保持不变
            if not is_trading_day and not signals and old_market_value > 0:
                total_market_value = old_market_value
            
            # 更新资产信息
            assets['market_value'] = total_market_value
            assets['total_asset'] = assets['cash'] + total_market_value
            
            # 只在资产变化显著时触发回调，减少不必要的回调
            if abs(assets['total_asset'] - old_total_asset) > 0.01 and self.trader_callback:
                self.trader_callback.on_stock_asset(SimpleNamespace(**assets))
            
            # 7. 交易信号处理优化
            if signals:
                trade_mgr = self.trade_mgr
                # 预先创建信号记录列表以避免多次append
                signal_records = []
                
                # 提前获取资产数据
                total_asset = assets['total_asset']
                cash = assets['cash']
                market_value = assets['market_value']
                
                # 只记录已成交的信号（有 actual_price 字段）。若策略显式配置为
                # 零费用，组件总额为零，旧版按占比分摊时会产生 0/0 并丢失全部成交记录。
                # 此处一次性计算组件，同时保留撮合端给出的总成本作为最终口径。
                for signal in signals:
                    if 'actual_price' not in signal:
                        continue
                    price = signal.get('actual_price', signal['price'])
                    volume = signal['volume']
                    commission = trade_mgr.calculate_commission(price, volume)
                    stamp_tax = trade_mgr.calculate_stamp_tax(
                        price,
                        volume,
                        signal['action'],
                        signal['code'],
                    )
                    transfer_fee = trade_mgr.calculate_transfer_fee(signal['code'], price, volume)
                    flow_fee = trade_mgr.calculate_flow_fee()
                    component_total = commission + stamp_tax + transfer_fee + flow_fee
                    if 'trade_cost' in signal:
                        recorded_total = float(signal.get('trade_cost') or 0.0)
                        if component_total > 0:
                            scale = recorded_total / component_total
                            commission *= scale
                            stamp_tax *= scale
                            transfer_fee *= scale
                            flow_fee *= scale
                        elif recorded_total != 0:
                            # 无法从零组件推导来源时，仍让四列之和与撮合总成本一致。
                            commission = recorded_total
                    signal_records.append({
                        # 优先使用 signal 中的 timestamp（挂单成交时间），否则使用当前时间。
                        'datetime': (
                            datetime.datetime.fromtimestamp(
                                signal['timestamp'] / 1000 if signal['timestamp'] > 1e10 else signal['timestamp']
                            )
                            if 'timestamp' in signal and signal['timestamp'] > 0
                            else current_time
                        ),
                        'code': signal['code'],
                        'action': signal['action'],
                        'price': price,
                        'volume': volume,
                        'amount': price * volume,
                        'commission': commission,
                        'stamp_tax': stamp_tax,
                        'transfer_fee': transfer_fee,
                        'flow_fee': flow_fee,
                        'total_asset': total_asset,
                        'cash': cash,
                        'market_value': market_value,
                    })
                self.backtest_records['trades'].extend(signal_records)
            
            # 8. 最后时间点判断优化
            # 使用函数字典替代if-else判断
            is_last_time_point = False
            trigger_type = self.config.config_dict.get("backtest", {}).get("trigger", {}).get("type", "tick")
            
            if trigger_type == "1d":
                # 日K线回测每个交易日只触发一次，不应再按日内最后时间点判定
                is_last_time_point = True
            elif self._is_custom_trigger:
                # 对于自定义时间触发，使用缓存优化
                trigger_seconds = self.trigger.trigger_seconds
                if trigger_seconds:
                    # 缓存当天的触发时间点
                    cache_key = f"time_points_{current_date}"
                    if cache_key not in self._cached_time_points:
                        # 获取当天所有触发时间点并缓存
                        today_times = []
                        max_trigger_second = max(trigger_seconds)
                        
                        # 使用列表推导式优化循环
                        today_times = [
                            int(datetime.datetime.combine(current_date, datetime.time(
                                seconds // 3600,
                                (seconds % 3600) // 60,
                                seconds % 60
                            )).timestamp())
                            for seconds in trigger_seconds
                        ]
                        
                        # 缓存计算结果
                        self._cached_time_points[cache_key] = {
                            'times': sorted(today_times),
                            'max_second': max_trigger_second
                        }
                    
                    # 使用缓存数据
                    max_trigger_second = self._cached_time_points[cache_key]['max_second']
                    
                    # 计算当前时间点的秒数(使用已有变量避免重复计算)
                    current_ts_dt = current_time
                    current_seconds = current_ts_dt.hour * 3600 + current_ts_dt.minute * 60 + current_ts_dt.second
                    
                    # 检查是否是当天最后一个触发点
                    is_last_time_point = abs(current_seconds - max_trigger_second) < 0.1
            else:
                # 非自定义时间触发，使用all_times缓存优化
                cache_key = f"daily_times_{current_date}"
                if cache_key not in self._cached_daily_times:
                    # 获取当天的时间点，使用生成器表达式优化
                    today_times = []
                    
                    # 使用列表推导式和异常处理优化
                    _ts_div = 1000 if self._is_ms_timestamp else 1
                    try:
                        today_times = [
                            t for t in self.all_times
                            if datetime.datetime.fromtimestamp(float(t) / _ts_div).date() == current_date
                        ]
                    except Exception:
                        # 出错时使用传统循环方式作为备选
                        for t in self.all_times:
                            try:
                                t_time = datetime.datetime.fromtimestamp(float(t) / _ts_div)
                                
                                if t_time.date() == current_date:
                                    today_times.append(t)
                            except Exception:
                                continue
                    
                    # 缓存结果
                    self._cached_daily_times[cache_key] = sorted(today_times)
                
                # 使用缓存的时间点
                today_times = self._cached_daily_times[cache_key]
                
                # 检查是否是最后时间点
                if today_times:
                    # 直接使用数值比较，避免创建新的datetime对象
                    last_time = float(today_times[-1])
                    last_time_seconds = last_time / 1000 if self._is_ms_timestamp else last_time
                    is_last_time_point = abs(last_time_seconds - current_ts_seconds) < 0.1
            
            # 9. 每日统计记录优化 - 只在最后时间点记录
            if is_last_time_point and is_trading_day:
                # 传入字符串形式的日期，省掉方法内的 isinstance 类型判断
                date_str_for_stats = time_info["date"] if time_info is not None else (
                    current_date if isinstance(current_date, str) else current_date.strftime("%Y-%m-%d")
                )
                self._record_daily_stats(date_str_for_stats, current_time, data)
            
        except Exception as e:
            logging.error(f"记录回测结果时出错: {str(e)}")
            logging.error(f"记录回测结果时出错: {str(e)}", exc_info=True)
    
    def _record_daily_stats(self, current_date, current_time, data):
        """记录每日统计数据（从record_results中分离出来的功能）
        
        Args:
            current_date: 当前日期
            current_time: 当前时间对象
            data: 市场数据
        """
        # 获取必要的资产数据
        assets = self.trade_mgr.assets
        cash = assets['cash']
        
        # current_date 由调用方保证已是字符串（传入 time_info["date"] 或 str(date_obj)）
        date_str = current_date
        
        # 重新计算一天结束时的市值
        positions = self.trade_mgr.positions
        position_codes = list(positions.keys())
        day_end_market_value = 0.0
        
        # 转换日期为YYYYMMDD格式，用于获取日线数据
        yyyymmdd_date = date_str.replace('-', '') if '-' in date_str else date_str

        # 批量获取收盘价：优先从当前 data 中提取（零成本），仅对缺失的股票才调用 API
        daily_prices = {}
        if position_codes:
            # 第一步：从当前触发数据中提取价格（覆盖绝大多数场景，无需任何 I/O）
            missing_codes = []
            for code in position_codes:
                if self._etf_total_return_enabled and is_etf(code):
                    price = positions[code].get('current_price')
                    if price is not None and pd.notna(price) and price > 0:
                        daily_prices[code] = price
                        continue
                if code in data:
                    price = data[code].get('lastPrice') or data[code].get('close')
                    if price and price > 0:
                        daily_prices[code] = price
                        continue
                missing_codes.append(code)

            # 第二步：仅对在 data 中找不到价格的股票走缓存/API
            if missing_codes:
                cache_date_key = f"daily_prices_{yyyymmdd_date}"
                if cache_date_key in self.daily_price_cache:
                    cached = self.daily_price_cache[cache_date_key]
                    for code in missing_codes:
                        if code in cached:
                            daily_prices[code] = cached[code]
                else:
                    try:
                        _daily_kwargs = {
                            "field_list": ['close'],
                            "stock_list": missing_codes,
                            "period": '1d',
                            "start_time": yyyymmdd_date,
                            "end_time": yyyymmdd_date,
                            "dividend_type": self.config.config_dict["data"].get(
                                "dividend_type", "none"
                            ),
                            "fill_data": True,
                        }
                        daily_data = self.data_source_mgr.get_market_data(
                            **_daily_kwargs
                        )
                        api_prices = {}
                        if daily_data is not None and isinstance(daily_data, dict) and 'close' in daily_data:
                            close_data = daily_data['close']
                            if isinstance(close_data, pd.DataFrame):
                                valid_codes = [c for c in missing_codes if c in close_data.index]
                                if valid_codes:
                                    latest_date = close_data.columns[-1]
                                    api_prices = {
                                        c: close_data.loc[c, latest_date]
                                        for c in valid_codes
                                        if close_data.loc[c, latest_date] is not None and close_data.loc[c, latest_date] > 0
                                    }
                        daily_prices.update(api_prices)
                        self.daily_price_cache[cache_date_key] = api_prices
                    except Exception as e:
                        logging.error(f"获取日线数据失败: {e}")
        
        # 批量计算持仓市值
        for code in position_codes:
            # 优先使用日线收盘价
            if code in daily_prices and daily_prices[code] > 0:
                current_price = daily_prices[code]
            # 备选方案：使用触发数据中的价格
            # 先检查lastPrice判断是否是tick数据（tick数据的close字段值为nan）
            elif code in data and 'lastPrice' in data[code]:
                # Tick数据：优先使用lastPrice字段
                current_price = data[code]['lastPrice']
            elif code in data and 'close' in data[code]:
                # K线数据：使用close字段
                current_price = data[code]['close']
            # 备选方案：使用持仓记录的价格
            elif 'current_price' in positions[code] and positions[code]['current_price'] > 0:
                current_price = positions[code]['current_price']
            # 最后备选：使用持仓均价
            else:
                current_price = positions[code]['avg_price']
            
            # 计算市值
            volume = positions[code]['volume']
            market_value = current_price * volume
            day_end_market_value += market_value
            
            # 更新持仓信息
            positions[code]['current_price'] = current_price
            positions[code]['market_value'] = market_value
            
            # 计算盈亏
            avg_price = positions[code]['avg_price']
            positions[code]['profit'] = (current_price - avg_price) * volume
            positions[code]['profit_ratio'] = (current_price - avg_price) / avg_price if avg_price > 0 else 0
        
        # 计算总资产
        total_asset = cash + day_end_market_value
        
        # 获取基准指数收盘价 - 使用缓存优化
        benchmark_code = self.config.config_dict["backtest"]["benchmark"]
        benchmark_close = None
        
        # 使用缓存避免重复获取基准数据
        cache_key = f"benchmark_{yyyymmdd_date}_{benchmark_code}"
        if cache_key in self._cached_benchmark_close:
            benchmark_close = self._cached_benchmark_close[cache_key]
        else:
            try:
                # 备选：使用触发数据中的价格
                # 先检查lastPrice判断是否是tick数据（tick数据的close字段值为nan）
                if benchmark_code in data:
                    if 'lastPrice' in data[benchmark_code]:
                        # Tick数据：优先使用lastPrice字段
                        benchmark_close = data[benchmark_code]['lastPrice']
                        # 缓存结果
                        self._cached_benchmark_close[cache_key] = benchmark_close
                    elif 'close' in data[benchmark_code]:
                        # K线数据：使用close字段
                        benchmark_close = data[benchmark_code]['close']
                        # 缓存结果
                        self._cached_benchmark_close[cache_key] = benchmark_close
            except Exception as e:
                logging.error(f"获取基准指数数据失败: {e}")
                # 备选方案：先检查lastPrice判断是否是tick数据
                if benchmark_code in data:
                    if 'lastPrice' in data[benchmark_code]:
                        benchmark_close = data[benchmark_code]['lastPrice']
                    elif 'close' in data[benchmark_code]:
                        benchmark_close = data[benchmark_code]['close']
        
        # 计算当日收益率
        daily_stats = self.backtest_records['daily_stats']
        if daily_stats:
            prev_asset = daily_stats[-1]['total_asset']
            daily_return = (total_asset - prev_asset) / prev_asset if prev_asset != 0 else 0
        else:
            init_capital = self.backtest_records['init_capital']
            daily_return = (total_asset - init_capital) / init_capital if init_capital != 0 else 0

        # 计算基准净值（不是收益率！）
        benchmark_nav = 1.0  # 默认净值为1
        if benchmark_close is not None and benchmark_close > 0:
            # 如果是第一天，记录基准初始价格
            if self._benchmark_initial_price is None:
                self._benchmark_initial_price = benchmark_close
                benchmark_nav = 1.0  # 第一天净值为1
            else:
                # 计算净值：当前价格 / 初始价格
                benchmark_nav = benchmark_close / self._benchmark_initial_price

        # 创建持仓快照
        positions_snapshot = {
            code: {
                'volume': pos['volume'],
                'price': pos['current_price'],
                'avg_price': pos['avg_price'],
                'market_value': pos['market_value'],
                'profit': pos['profit'],
                'profit_ratio': pos['profit_ratio']
            }
            for code, pos in self.trade_mgr.positions.items()
        }

        # 记录每日统计数据
        daily_stat = {
            'date': current_date,
            'total_asset': total_asset,
            'cash': cash,
            'market_value': day_end_market_value,
            'daily_return': daily_return,
            'benchmark_close': benchmark_close,
            'benchmark_nav': benchmark_nav,  # 修改：基准净值（而非收益率）
            'positions': positions_snapshot
        }
        if self._etf_total_return_enabled:
            daily_stat.update({
                'etf_cash_distribution': self._etf_distribution_today,
                'etf_cash_distribution_cumulative': self._etf_distribution_total,
                'etf_total_return_fallbacks': self._etf_total_return_fallbacks,
            })
        self.backtest_records['daily_stats'].append(daily_stat)

        # 记录基准指数数据
        if benchmark_close is not None:
            self.backtest_records['benchmark_data'].append({
                'date': current_date,
                'close': benchmark_close,
                'nav': benchmark_nav  # 修改：基准净值（而非收益率）
            })
        
        # 输出日志（性能优化：检查是否需要输出）
        if self.trader_callback and self._should_log():
            decimals = self.price_decimals
            # 当日成交汇总（避免逐笔 logging 拖慢回测）
            trade_mgr = getattr(self, 'trade_mgr', None)
            daily_trades = getattr(trade_mgr, '_daily_trade_count', 0) if trade_mgr else 0
            if trade_mgr:
                trade_mgr._daily_trade_count = 0  # 重置计数
            trade_suffix = f" | 今日成交: {daily_trades}笔" if daily_trades > 0 else ""
            logging.info(f"每日统计 - 日期: {daily_stat['date']} | "
                f"总资产: {total_asset:.{decimals}f} | "
                f"日收益率: {daily_return*100:.2f}% | "
                f"基准净值: {benchmark_nav:.4f} | "
                f"持仓市值: {day_end_market_value:.{decimals}f} | "
                f"可用资金: {cash:.{decimals}f}{trade_suffix}")


    def stop(self):
        """停止框架"""
        self.is_running = False
        
        # 记录结束时间（如果还没有记录的话）
        if self.end_time is None:
            self.end_time = time.time()
            if self.start_time is not None:
                self.total_runtime = self.end_time - self.start_time
                
                # 记录停止日志
                if self.trader_callback:
                    end_datetime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    logging.info(f"策略手动停止时间: {end_datetime}")
                    logging.info(f"策略总运行时长: {self._format_runtime(self.total_runtime)}")
        
            
        
    def log_error(self, msg: str):
        """错误日志"""
        print(f"[ERROR] {datetime.datetime.now()} - {msg}")

    def _format_runtime(self, seconds):
        """格式化运行时间"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        seconds = int(seconds % 60)
        return f"{hours}小时{minutes}分钟{seconds}秒"
    
    def on_stock_position(self, position):
        """持仓变动回调"""
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            try:
                decimals = self.price_decimals
                logging.debug(
                    f"[TRADE] 持仓变动 - {position.stock_code} | "
                    f"数量:{position.volume} | 均价:{position.avg_price:.{decimals}f}"
                )
            except Exception as e:
                logging.error(f"处理持仓变动回调时出错: {e}")
    
    def on_order_error(self, error):
        """委托错误回调"""
        try:
            error_msg = (
                f"委托错误 - "
                f"股票代码: {error.stock_code} | "
                f"错误代码: {error.error_id} | "
                f"错误信息: {error.error_msg} | "
                f"备注: {error.order_remark}"
            )
            level = logging.WARNING if getattr(error, "error_id", None) == -1 else logging.ERROR
            logging.log(level, f"[TRADE] {error_msg}")
            
            tag = "WARNING" if getattr(error, "error_id", None) == -1 else "ERROR"
            print(f"[{tag}] 委托错误: {error.error_msg}")
        except Exception as e:
            print(f"处理委托错误回调时出错: {str(e)}")
    
    def on_stock_order(self, order):
        """委托回报回调"""
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            try:
                logging.debug(
                    f"[TRADE] 委托回报 - {order.stock_code} | "
                    f"编号:{getattr(order, 'order_id', 'N/A')} | "
                    f"价格:{getattr(order, 'price', 'N/A')} | "
                    f"数量:{getattr(order, 'order_volume', 'N/A')}"
                )
            except Exception as e:
                logging.error(f"处理委托回报时出错: {e}")
    
    def on_stock_trade(self, trade):
        """成交回报回调"""
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            try:
                logging.debug(
                    f"[TRADE] 成交回报 - {trade.stock_code} | "
                    f"价格:{getattr(trade, 'traded_price', 'N/A')} | "
                    f"数量:{getattr(trade, 'traded_volume', 'N/A')}"
                )
            except Exception as e:
                logging.error(f"处理成交回报时出错: {e}")
    
    def on_stock_asset(self, asset):
        """资产变动回调"""
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            try:
                logging.debug(
                    f"[TRADE] 资产变动 - 总资产:{getattr(asset, 'total_asset', 'N/A')} | "
                    f"现金:{getattr(asset, 'cash', 'N/A')}"
                )
            except Exception as e:
                logging.error(f"处理资产变动时出错: {e}")
