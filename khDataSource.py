# coding: utf-8
"""
数据源管理器 - 统一管理回测数据获取

开源版只有一种数据源：本地 DuckDB 数据库（需要先用 baostock / tushare 下载数据）。
CS 版的 miniQMT（xtdata）与大QMT原生桥已移除；旧配置里存的任何数据源名都按
DuckDB 处理，并记一条警告。

使用方法：
    from khDataSource import DataSourceManager

    # 创建管理器
    ds = DataSourceManager(data_source='duckdb', duckdb_path='D:/khData')

    # 获取数据（与 xtdata 接口形式一致）
    data = ds.get_market_data_ex(
        field_list=['close'],
        stock_list=['000001.SZ'],
        period='1d',
        start_time='20240101',
        end_time='20241231'
    )
"""

import logging
import os
import time as _time
import inspect
from typing import List, Dict, Optional, Any
import pandas as pd


# ===== 性能剖析：get_market_data_ex 全局读取统计 =====
# 跨所有调用方累计（框架批量加载 + khHistory + 基准），用于区分各读取路径耗时。
# 仅统计，不影响功能。
_READ_STATS = {"calls": 0, "stocks": 0, "seconds": 0.0}


class DataSourceError(ValueError):
    """稳定的数据源配置错误。

    ``code`` is intentionally kept as a plain string so GUI callers can
    expose the same machine-readable reason without importing a particular
    adapter implementation.
    """

    def __init__(self, message: str, code: str = "UNSUPPORTED_SOURCE"):
        super().__init__(message)
        self.code = str(code)


_warned_sources = set()


def _normalize_data_source(value: object, *, default: str = "duckdb") -> str:
    """开源版只读 DuckDB；其他数据源名（多来自 V2.1 / CS 的旧配置）按 duckdb 处理。"""
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        value = getattr(value, "value")
    raw = str(value if value is not None else default).strip().lower()
    if raw and raw != "duckdb" and raw not in _warned_sources:
        _warned_sources.add(raw)
        logging.warning(f"开源版只支持 DuckDB 数据源，配置中的数据源「{raw}」已按 DuckDB 处理")
    return "duckdb"


def _invoke_progress_callback(
    callback: object,
    payload: object,
    *,
    index: Optional[int] = None,
    total: Optional[int] = None,
) -> None:
    """Invoke a history-progress callback exactly once.

    Older callers generally accept one mapping argument, while a few legacy
    integrations use ``callback(index, total)``.  It is tempting to call the
    former and retry the latter when a :class:`TypeError` is raised, but that
    exception can originate *inside* the callback after it has already
    mutated UI/state.  Inspect the signature first and only choose a shape
    that can bind; any exception raised by the callback body is propagated
    without a second invocation.

    If a callable's signature is opaque (for example some C extensions), use
    the documented one-argument mapping form once.  This deliberately favors
    at-most-once side effects over guessing an alternate signature.
    """

    if not callable(callback):
        return
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        callback(payload)
        return

    candidates = [(payload,)]
    if index is not None and total is not None:
        candidates.append((index, total))
    selected = None
    for args in candidates:
        try:
            signature.bind(*args)
        except TypeError:
            continue
        selected = args
        break
    if selected is None:
        # Preserve the historical one-argument attempt so an incompatible
        # callback reports its own TypeError; do not guess or retry.
        callback(payload)
        return
    callback(*selected)


def reset_read_stats():
    """重置全局读取统计（回测开始时调用）。"""
    _READ_STATS["calls"] = 0
    _READ_STATS["stocks"] = 0
    _READ_STATS["seconds"] = 0.0


def get_read_stats() -> Dict[str, float]:
    """返回全局读取统计快照。"""
    return dict(_READ_STATS)


class DataSourceManager:
    """数据源管理器 - 提供统一的数据获取接口（DuckDB）。"""

    def __init__(self, data_source: str = 'duckdb', duckdb_path: str = None):
        """初始化数据源管理器

        Args:
            data_source: 保留参数以兼容旧调用；开源版一律使用 DuckDB。
            duckdb_path: DuckDB 数据根目录。
        """
        self.data_source = _normalize_data_source(data_source)
        self.duckdb_path = self._normalize_duckdb_path(duckdb_path) if duckdb_path else duckdb_path
        self._duckdb_adapter = None
        self.last_error = ""
        self.last_error_code = ""
        if not duckdb_path:
            raise ValueError("使用DuckDB数据源时必须提供duckdb_path参数")
        self._init_duckdb()

    @staticmethod
    def _normalize_duckdb_path(path: str) -> str:
        """展开并规范化 DuckDB 数据根目录路径。"""
        return os.path.abspath(os.path.expandvars(os.path.expanduser(str(path).strip())))

    def _record_error(self, error: object) -> None:
        self.last_error = str(error or "")
        self.last_error_code = str(getattr(error, "code", "SOURCE_ERROR") or "SOURCE_ERROR")

    def clear_error(self) -> None:
        """Clear the last soft-failure marker after a successful operation."""
        self.last_error = ""
        self.last_error_code = ""

    def get_last_error(self) -> dict[str, str]:
        """Return a stable diagnostic snapshot for bool-returning APIs."""
        return {"code": self.last_error_code, "message": self.last_error}

    def _init_duckdb(self):
        """初始化DuckDB适配器"""
        try:
            from duckdb_storage import xtdata_adapter
            if not os.path.isdir(self.duckdb_path):
                raise FileNotFoundError(f"DuckDB数据根目录不存在: {self.duckdb_path}")
            # 设置数据根目录
            xtdata_adapter.set_data_root(self.duckdb_path)
            self._duckdb_adapter = xtdata_adapter
            logging.info(f"数据源初始化完成: DuckDB ({self.duckdb_path})")
        except ImportError as e:
            logging.error(f"导入duckdb_storage失败: {e}")
            raise ImportError("无法导入duckdb_storage模块")
        except Exception as e:
            logging.error(f"初始化DuckDB适配器失败: {e}")
            raise

    @property
    def source_name(self) -> str:
        """返回当前数据源名称"""
        return f"DuckDB ({self.duckdb_path})"

    def get_market_data_ex(
        self,
        field_list: List[str] = [],
        stock_list: List[str] = [],
        period: str = '1d',
        start_time: str = '',
        end_time: str = '',
        count: int = -1,
        dividend_type: str = 'none',
        fill_data: bool = True,
    ) -> Dict[str, pd.DataFrame]:
        """获取市场数据（扩展版）

        与 xtdata.get_market_data_ex() 接口形式一致

        Args:
            field_list: 字段列表，如 ['open', 'high', 'low', 'close', 'volume']
            stock_list: 股票代码列表，如 ['000001.SZ', '600000.SH']
            period: 数据周期，'tick', '1m', '5m', '1d' 等
            start_time: 开始时间，格式 'YYYYMMDD' 或 'YYYYMMDDHHmmss'
            end_time: 结束时间，格式同上
            count: 数据条数，-1 表示不限制
            dividend_type: 复权类型，'none', 'front', 'back', 'front_ratio', 'back_ratio'
            fill_data: 是否填充数据

        Returns:
            Dict[str, pd.DataFrame]: {股票代码: DataFrame}
        """
        # 性能剖析：累计本次读取的调用数/股票数/耗时（含框架加载与 khHistory 两条路径）
        _t0 = _time.time()
        try:
            return self._duckdb_adapter.get_market_data_ex(
                field_list=field_list,
                stock_list=stock_list,
                period=period,
                start_time=start_time,
                end_time=end_time,
                count=count,
                dividend_type=dividend_type,
                fill_data=fill_data
            )
        except Exception as exc:
            # Read APIs intentionally re-raise so callers can handle the
            # original exception, but retain a machine-readable diagnostic.
            self._record_error(exc)
            raise
        finally:
            _READ_STATS["calls"] += 1
            _READ_STATS["stocks"] += len(stock_list) if stock_list else 0
            _READ_STATS["seconds"] += _time.time() - _t0

    def get_market_data_ex_framework_raw(
        self,
        field_list: List[str] = [],
        stock_list: List[str] = [],
        period: str = '1d',
        start_time: str = '',
        end_time: str = '',
        count: int = -1,
        dividend_type: str = 'none',
        fill_data: bool = True,
        preserve_raw_ohlc: bool = False,
    ) -> Dict[str, pd.DataFrame]:
        """Framework internal fast path with ``time`` kept as local-epoch seconds."""
        _t0 = _time.time()
        try:
            return self._duckdb_adapter.get_market_data_ex_framework_raw(
                field_list=field_list,
                stock_list=stock_list,
                period=period,
                start_time=start_time,
                end_time=end_time,
                count=count,
                dividend_type=dividend_type,
                fill_data=fill_data,
                preserve_raw_ohlc=preserve_raw_ohlc,
            )
        finally:
            _READ_STATS["calls"] += 1
            _READ_STATS["stocks"] += len(stock_list) if stock_list else 0
            _READ_STATS["seconds"] += _time.time() - _t0

    def get_market_data(
        self,
        field_list: List[str] = [],
        stock_list: List[str] = [],
        period: str = '1d',
        start_time: str = '',
        end_time: str = '',
        count: int = -1,
        dividend_type: str = 'none',
        fill_data: bool = True,
    ) -> Dict[str, pd.DataFrame]:
        """获取市场数据（透视表格式）

        与 xtdata.get_market_data() 接口形式一致

        Returns:
            Dict[str, pd.DataFrame]: {字段名: DataFrame}，每个DataFrame的行是股票，列是时间
        """
        return self._duckdb_adapter.get_market_data(
            field_list=field_list,
            stock_list=stock_list,
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            fill_data=fill_data
        )

    def get_local_data(
        self,
        field_list: List[str] = [],
        stock_list: List[str] = [],
        period: str = '1d',
        start_time: str = '',
        end_time: str = '',
        count: int = -1,
        dividend_type: str = 'none',
        fill_data: bool = True,
    ) -> Dict[str, pd.DataFrame]:
        """获取本地数据。DuckDB 本身就是本地数据，与 get_market_data_ex 相同。"""
        return self.get_market_data_ex(
            field_list=field_list,
            stock_list=stock_list,
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            fill_data=fill_data,
        )

    def download_history_data(
        self,
        stock_code: str,
        period: str = '1d',
        start_time: str = '',
        end_time: str = '',
        incrementally: Optional[bool] = True,
        **_ignored,
    ) -> bool:
        """下载历史数据：DuckDB 数据已在本地，无需下载，直接返回成功。

        行情下载在「数据管理」里用 baostock / tushare 完成。
        """
        return True

    def download_history_data2(
        self,
        stock_list: List[str],
        period: str = '1d',
        start_time: str = '',
        end_time: str = '',
        incrementally: Optional[bool] = True,
        callback: Any = None,
        **_ignored,
    ) -> bool:
        """批量下载历史数据：DuckDB 模式跳过下载，直接回调「已完成」。"""
        logging.info(f"DuckDB模式: 跳过批量下载 {len(stock_list)} 只股票")
        # 模拟下载完成回调
        if callback:
            _invoke_progress_callback(
                callback,
                {'finished': len(stock_list), 'total': len(stock_list)},
                index=len(stock_list),
                total=len(stock_list),
            )
        return True

    def is_duckdb_mode(self) -> bool:
        """是否为DuckDB模式"""
        return True

    def close(self) -> None:
        """DuckDB 连接由 xtdata_adapter 管理，这里无需释放。"""
        return None

    def get_available_stocks(self) -> List[str]:
        """获取本地数据库中已有数据的股票列表"""
        try:
            from duckdb_storage.manager import DuckDBManager
            manager = DuckDBManager(self.duckdb_path, max_connections=100, read_only=True)  # 回测场景使用100个连接
            stocks = manager.get_all_stocks()
            return [s['stock_code'] for s in stocks]
        except Exception as e:
            logging.error(f"获取DuckDB股票列表失败: {e}")
            return []

    def check_data_availability(
        self,
        stock_list: List[str],
        period: str = '1d',
    ) -> Dict[str, bool]:
        """检查股票数据是否可用

        Args:
            stock_list: 股票代码列表
            period: 数据周期

        Returns:
            Dict[str, bool]: {股票代码: 是否有数据}
        """
        result = {}
        try:
            from duckdb_storage.manager import DuckDBManager
            manager = DuckDBManager(self.duckdb_path, max_connections=100, read_only=True)  # 回测场景使用100个连接
            all_stocks = manager.get_all_stocks()

            # 根据周期检查数据可用性
            period_field_map = {
                '1d': 'has_1d',
                '1m': 'has_1m',
                '5m': 'has_5m',
                'tick': 'has_tick'
            }
            field = period_field_map.get(period, 'has_1d')

            stock_info = {s['stock_code']: s.get(field, False) for s in all_stocks}

            for code in stock_list:
                result[code] = stock_info.get(code, False)

        except Exception as e:
            logging.error(f"检查DuckDB数据可用性失败: {e}")
            for code in stock_list:
                result[code] = False
        return result


# 全局单例
_data_source_manager: Optional[DataSourceManager] = None


def get_data_source_manager() -> Optional[DataSourceManager]:
    """获取全局数据源管理器实例"""
    return _data_source_manager


def init_data_source_manager(
    data_source: str = 'duckdb',
    duckdb_path: str = None,
) -> DataSourceManager:
    """初始化全局数据源管理器

    Args:
        data_source: 保留参数以兼容旧调用；开源版一律使用 DuckDB
        duckdb_path: DuckDB数据路径；留空时使用默认数据目录

    Returns:
        DataSourceManager: 数据源管理器实例
    """
    if not duckdb_path:
        from kh_platform import default_duckdb_dir
        duckdb_path = default_duckdb_dir()
    global _data_source_manager
    _data_source_manager = DataSourceManager(
        data_source=data_source,
        duckdb_path=duckdb_path,
    )
    return _data_source_manager


def reset_data_source_manager() -> None:
    """Release global data-source resources without changing saved settings."""
    global _data_source_manager

    manager = _data_source_manager
    try:
        if manager is not None:
            try:
                from duckdb_storage import xtdata_adapter
                xtdata_adapter.reset_manager()
            except Exception as exc:
                logging.debug(f"reset DuckDB adapter failed: {exc}")
    finally:
        _data_source_manager = None


def init_from_settings() -> DataSourceManager:
    """从开源版配置 ~/.khquant_os/settings.json 初始化数据源管理器。

    只读取 DuckDB 数据目录；不从 QSettings/注册表读取，避免旧安装遗留值与
    设置界面分叉。

    Returns:
        DataSourceManager: 数据源管理器实例
    """
    import kh_settings as global_settings

    settings_cfg = global_settings.load()
    duckdb_path = settings_cfg.get('duckdb_data_path', '')
    return init_data_source_manager(data_source='duckdb', duckdb_path=duckdb_path)
