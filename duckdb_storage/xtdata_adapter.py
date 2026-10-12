# -*- coding: utf-8 -*-
"""
xtdata 兼容适配器（基于本地 DuckDB）

提供与 xtquant.xtdata 部分接口兼容的函数，便于用本地 DuckDB 数据替换在线数据源。

实现函数：
 - get_market_data_ex(field_list, stock_list, period, start_time=None, end_time=None, dividend_type='none', fill_data=False)
 - get_market_data(...) 简化合并视图
 - get_local_data(...) get_market_data_ex 的别名
 - download_history_data(stock_code, period, start_time, end_time, incrementally=False)
 - set_data_root(path) 设置数据根目录

复权方式（dividend_type）支持：
 - 'none'：不复权
 - 'front'：前复权
 - 'back'：后复权
 - 'front_ratio'：等比前复权（请求区间不完整时整段回退到 front）
 - 'back_ratio'：等比后复权（请求区间不完整时整段回退到 back）

注意：本适配器不会回退到在线 xtdata，当 DB 中无数据时返回空 DataFrame 或空字典。
"""
from typing import List, Dict, Optional
import datetime
import os
import pandas as pd
import numpy as np

from .manager import DuckDBManager
from .lock_retry import is_duckdb_lock_error
from .dividend_adjustment import select_dividend_fields


# 全局数据根目录（可通过 set_data_root 设置）
_data_root: Optional[str] = None
_manager: Optional[DuckDBManager] = None
_MAX_BACKTEST_CONNECTIONS = int(os.environ.get("KH_DUCKDB_MAX_CONNECTIONS", "600"))
_BATCH_READ_WORKERS = int(os.environ.get("KH_DUCKDB_BATCH_READ_WORKERS", "1"))
_LOCAL_TZ_OFFSET_SECONDS = int((datetime.datetime.now().astimezone().utcoffset() or datetime.timedelta()).total_seconds())
_FRAMEWORK_RAW_PREFIX = "__kh_raw_"


def set_max_backtest_connections(max_connections: int):
    global _MAX_BACKTEST_CONNECTIONS
    try:
        value = int(max_connections)
    except Exception:
        value = 600
    value = max(50, min(value, 2000))
    _MAX_BACKTEST_CONNECTIONS = value
    if _manager is not None:
        try:
            _manager._max_connections = value
        except Exception:
            pass


def set_batch_read_workers(workers: int):
    global _BATCH_READ_WORKERS
    try:
        value = int(workers)
    except Exception:
        value = 1
    _BATCH_READ_WORKERS = max(1, min(value, 64))


def reset_manager():
    """重置 DuckDB 管理器，释放旧路径上的连接池。"""
    global _manager
    try:
        if _manager is not None:
            _manager.close_all()
    except Exception:
        pass
    _manager = None
    try:
        DuckDBManager.reset_instance()
    except Exception:
        pass


def set_data_root(path: str):
    """设置DuckDB数据根目录

    Args:
        path: 数据根目录路径
    """
    global _data_root, _manager
    normalized_path = os.path.abspath(path) if path else None
    current_root = getattr(_manager, "data_root", None) if _manager is not None else None
    if current_root and normalized_path and os.path.abspath(current_root) != normalized_path:
        reset_manager()
    _data_root = normalized_path
    if normalized_path and os.path.exists(normalized_path):
        _manager = DuckDBManager(normalized_path, max_connections=_MAX_BACKTEST_CONNECTIONS, read_only=True)  # 回测场景使用连接池
    else:
        reset_manager()
        _manager = None


def get_data_root() -> Optional[str]:
    """获取当前数据根目录"""
    return _data_root


def _get_manager() -> DuckDBManager:
    """获取DuckDB管理器实例

    如果未通过 set_data_root 设置，则使用默认路径
    """
    global _manager, _data_root

    if _manager is not None:
        return _manager

    # 未设置时，尝试使用默认路径
    if _data_root is None:
        # 尝试从环境变量或默认路径获取
        default_path = os.environ.get('DUCKDB_DATA_ROOT', '')
        if not default_path:
            # 使用当前目录下的 stock_data
            default_path = os.path.join(os.path.dirname(__file__), '..', 'stock_data')
        _data_root = default_path

    if _data_root and os.path.exists(_data_root):
        _manager = DuckDBManager(_data_root, max_connections=_MAX_BACKTEST_CONNECTIONS, read_only=True)  # 回测场景使用连接池
        return _manager
    else:
        # 如果路径不存在，抛出错误
        raise ValueError(f"DuckDB数据根目录不存在: {_data_root}，请先调用 set_data_root() 设置正确的路径")


_OHLC_FIELDS = ('open', 'high', 'low', 'close')
_DIVIDEND_TYPES = ('front', 'back', 'front_ratio', 'back_ratio')
_RATIO_DIVIDEND_TYPES = ('front_ratio', 'back_ratio')


def _build_dividend_query_fields(
    field_list: Optional[List[str]],
    dividend_type: str,
    *,
    legacy_ratio_only: bool = False,
) -> Optional[List[str]]:
    """Build fields for adjusted-price reads with old-database fallback."""
    if not field_list:
        return None

    dt = (dividend_type or 'none').lower()
    fields = list(dict.fromkeys(['time'] + [f for f in field_list if f != 'time']))
    if dt not in _DIVIDEND_TYPES:
        return fields

    base_dt = dt.replace('_ratio', '')
    if legacy_ratio_only and dt in _RATIO_DIVIDEND_TYPES:
        ratio_columns = {f'{field}_{dt}' for field in _OHLC_FIELDS}
        fields = [field for field in fields if field not in ratio_columns]
        suffixes = (base_dt,)
    elif dt in _RATIO_DIVIDEND_TYPES:
        # 同时查询 ratio 与普通复权字段；选择阶段只会整段使用其中一套，
        # 不会在历史缺口处逐行拼接两种价格口径。
        suffixes = (dt, base_dt)
    else:
        suffixes = (dt,)

    for suffix in suffixes:
        for field in _OHLC_FIELDS:
            adjusted = f'{field}_{suffix}'
            if adjusted not in fields:
                fields.append(adjusted)
    return fields


def _select_dividend_fields(
    df: pd.DataFrame,
    dividend_type: str,
    *,
    context: Optional[str] = None,
) -> pd.DataFrame:
    """
    根据 dividend_type 选择对应的复权字段

    将复权字段（如 open_front）重命名为标准字段（open）

    Args:
        df: 原始 DataFrame（包含所有复权字段）
        dividend_type: 'front'|'back'|'front_ratio'|'back_ratio'

    Returns:
        重命名后的 DataFrame
    """
    return select_dividend_fields(df, dividend_type, context=context)


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    """确保 time 列为 pandas datetime"""
    if df is None or df.empty:
        return df
    if 'time' in df.columns:
        try:
            if not pd.api.types.is_datetime64_any_dtype(df['time']):
                df = df.copy()
                df['time'] = pd.to_datetime(df['time'])
        except Exception:
            # 保守处理，忽略转换错误
            pass
    return df


def _datetime_to_timestamp_ms(df: pd.DataFrame) -> pd.DataFrame:
    """将 time 列从 datetime 转换为毫秒时间戳（与 xtdata 格式一致）"""
    if df is None or df.empty:
        return df
    if 'time' in df.columns:
        try:
            df = df.copy()
            if pd.api.types.is_datetime64_any_dtype(df['time']):
                # 转换为毫秒时间戳（UTC 时间，需要减去8小时）
                # pandas datetime 默认是无时区的，假设是东八区时间
                df['time'] = ((df['time'].astype('int64') // 10**3) - 8 * 3600 * 1000).astype('int64')
        except Exception:
            pass
    return df


def _datetime_to_framework_seconds(df: pd.DataFrame) -> pd.DataFrame:
    """Convert naive DuckDB timestamps to local-epoch seconds for framework internals."""
    if df is None or df.empty or 'time' not in df.columns:
        return df

    try:
        result = df.copy()
        time_col = result['time']
        time_series = pd.to_datetime(time_col, errors='coerce')
        if time_series.isna().all():
            return df
        try:
            time_series = time_series.astype('datetime64[ns]')
        except Exception:
            pass

        seconds = (time_series.astype('int64') // 10**9).astype('int64')
        seconds = seconds - _LOCAL_TZ_OFFSET_SECONDS
        result['time'] = seconds
        return result
    except Exception:
        return df


def _convert_time_to_xtdata_format(df: pd.DataFrame, period: str, keep_time_column: bool = False) -> pd.DataFrame:
    """
    将 time 列转换为 xtdata 返回的格式：
    - 1d: 字符串索引，格式 '20241202'，索引名为 None，不保留 time 列（除非 keep_time_column=True）
    - 1m/5m: 字符串索引，格式 '20241227093000'（YYYYMMDDHHmmss），索引名为 None，不保留 time 列
    - tick: 整数索引（毫秒时间戳），不保留 time 列

    Args:
        df: 原始 DataFrame
        period: 周期类型
        keep_time_column: 是否保留 time 列（仅对 1d 有效）
    """
    if df is None or df.empty:
        return df

    if 'time' not in df.columns:
        return df

    df = df.copy()

    # 确保 time 是 datetime 类型
    if not pd.api.types.is_datetime64_any_dtype(df['time']):
        # 如果是整数，假设是毫秒时间戳
        if df['time'].dtype in ['int64', 'int32', 'float64']:
            # 已经是毫秒时间戳，转换为 datetime
            df['time'] = pd.to_datetime(df['time'], unit='ms') + pd.Timedelta(hours=8)
        else:
            df['time'] = pd.to_datetime(df['time'])

    # 统一转换为纳秒精度的 datetime64[ns]
    # DuckDB 可能返回 datetime64[us]（微秒），需要转换
    df['time'] = df['time'].astype('datetime64[ns]')

    # 根据周期决定索引格式
    if period == '1d':
        # 日线：转换为字符串索引 '20241202'
        df.index = df['time'].dt.strftime('%Y%m%d')
        # 索引名设置为 None（与 xtdata 保持一致）
        df.index.name = None

        if keep_time_column:
            # 保留 time 列，转换为毫秒时间戳
            # pandas datetime64[ns] 转换为毫秒：除以 10**6
            # xtdata 返回的是 UTC 时间戳，需要减去 8 小时
            df['time'] = (df['time'].astype('int64') // 10**6 - 8 * 3600 * 1000).astype('int64')
        else:
            # 删除 time 列
            df = df.drop(columns=['time'])

    elif period in ['1m', '5m']:
        # 分钟线：转换为字符串索引 '20241227093000'（YYYYMMDDHHmmss）
        df.index = df['time'].dt.strftime('%Y%m%d%H%M%S')
        # 索引名设置为 None
        df.index.name = None
        # 删除 time 列（xtdata 的分钟线数据没有 time 列）
        df = df.drop(columns=['time'])

    else:
        # tick：保持整数索引（毫秒时间戳）
        # pandas datetime64[ns] 转换为毫秒，并减去 8 小时转为 UTC
        df['time'] = (df['time'].astype('int64') // 10**6 - 8 * 3600 * 1000).astype('int64')
        # 设置为索引，索引名为 None
        df = df.set_index('time')
        df.index.name = None

    return df


# 复权计算函数已移除，改为直接从数据库读取对应的复权字段
# 数据导入时会同时保存 none/front/back/front_ratio/back_ratio 五种复权数据


def get_market_data_ex(field_list: List[str],
                       stock_list: List[str],
                       period: str,
                       start_time: Optional[str] = None,
                       end_time: Optional[str] = None,
                       count: int = -1,
                       dividend_type: str = 'none',
                       fill_data: bool = False) -> Dict[str, pd.DataFrame]:
    """
    返回 {stock_code: DataFrame}，DataFrame 至少包含 time 列。
    field_list: 请求的字段列表（会做字段过滤）
    period: '1d','1m','5m','tick'
    dividend_type: 'none'|'front'|'back'|'front_ratio'|'back_ratio'
    count: 数据条数，-1 表示不限制

    注意：复权数据直接从数据库对应字段读取，不进行计算
    """
    manager = _get_manager()
    result: Dict[str, pd.DataFrame] = {}

    dt = dividend_type.lower() if dividend_type else 'none'
    query_fields = _build_dividend_query_fields(field_list, dt)

    # 注：曾尝试"每线程独立 read_only 连接并行读取"以提速，但只读连接在 GUI 长驻
    # 进程中（已持有同库读写实例）会返回空 DataFrame，导致回测无数据。故回退为
    # 经连接池的顺序读写读取（稳健，GUI/CLI 一致）。提速由 #1 缓存与 #3 索引承担。
    batch_failed = False
    try:
        batch_data = manager.get_kline_data_batch(
            stock_list,
            period,
            start_time,
            end_time,
            dividend_type=None,
            fields=query_fields,
            workers=_BATCH_READ_WORKERS,
        )
    except Exception as exc:
        if is_duckdb_lock_error(exc):
            raise
        batch_failed = True
        batch_data = {}

    if not batch_failed and dt in _RATIO_DIVIDEND_TYPES and query_fields:
        retry_codes = [
            stock for stock in stock_list
            if batch_data.get(stock) is None or batch_data[stock].empty
        ]
        if retry_codes:
            legacy_fields = _build_dividend_query_fields(
                field_list, dt, legacy_ratio_only=True
            )
            try:
                legacy_data = manager.get_kline_data_batch(
                    retry_codes,
                    period,
                    start_time,
                    end_time,
                    dividend_type=None,
                    fields=legacy_fields,
                    workers=_BATCH_READ_WORKERS,
                )
                for stock in retry_codes:
                    legacy_df = legacy_data.get(stock)
                    if legacy_df is not None and not legacy_df.empty:
                        batch_data[stock] = legacy_df
            except Exception as exc:
                if is_duckdb_lock_error(exc):
                    raise
                pass

    for stock in stock_list:
        try:
            if batch_failed:
                df = manager.get_kline_data(stock, period, start_time, end_time, fields=query_fields)
                if (df is None or df.empty) and dt in _RATIO_DIVIDEND_TYPES and query_fields:
                    legacy_fields = _build_dividend_query_fields(
                        field_list, dt, legacy_ratio_only=True
                    )
                    df = manager.get_kline_data(
                        stock, period, start_time, end_time, fields=legacy_fields
                    )
            else:
                df = batch_data.get(stock)
            if df is None or df.empty:
                result[stock] = pd.DataFrame(columns=field_list if field_list else [])
                continue
            if dividend_type and dt != 'none':
                df = _select_dividend_fields(
                    df, dividend_type, context=f"{stock}/{period}"
                )
            keep_time = not field_list or 'time' in field_list
            df = _convert_time_to_xtdata_format(df, period, keep_time_column=keep_time)
            if field_list:
                available = [f for f in field_list if f in df.columns]
                df = df[available].copy()
            else:
                basic_fields = ['time', 'open', 'high', 'low', 'close', 'volume', 'amount',
                               'settelementPrice', 'openInterest', 'preClose', 'suspendFlag']
                available = [f for f in basic_fields if f in df.columns]
                df = df[available].copy()
            result[stock] = df
        except Exception as exc:
            if is_duckdb_lock_error(exc):
                raise
            result[stock] = pd.DataFrame(columns=field_list if field_list else [])
    return result


def get_market_data_ex_framework_raw(field_list: List[str],
                                     stock_list: List[str],
                                     period: str,
                                     start_time: Optional[str] = None,
                                     end_time: Optional[str] = None,
                                     count: int = -1,
                                     dividend_type: str = 'none',
                                     fill_data: bool = False,
                                     preserve_raw_ohlc: bool = False) -> Dict[str, pd.DataFrame]:
    """Framework internal fast path: keep ``time`` as local-epoch seconds."""
    manager = _get_manager()
    result: Dict[str, pd.DataFrame] = {}

    dt = dividend_type.lower() if dividend_type else 'none'
    query_fields = _build_dividend_query_fields(field_list, dt)

    use_epoch_sql = query_fields is not None and 'time' in query_fields
    try:
        if use_epoch_sql:
            batch_data = manager.get_kline_data_batch_epoch_seconds(
                stock_list,
                period,
                start_time,
                end_time,
                dividend_type=None,
                fields=query_fields,
                workers=_BATCH_READ_WORKERS,
                tz_offset_seconds=_LOCAL_TZ_OFFSET_SECONDS,
            )
        else:
            batch_data = manager.get_kline_data_batch(
                stock_list,
                period,
                start_time,
                end_time,
                dividend_type=None,
                fields=query_fields,
                workers=_BATCH_READ_WORKERS,
            )
    except Exception as exc:
        if is_duckdb_lock_error(exc):
            raise
        batch_data = {}

    if dt in _RATIO_DIVIDEND_TYPES and query_fields:
        retry_codes = [
            stock for stock in stock_list
            if batch_data.get(stock) is None or batch_data[stock].empty
        ]
        if retry_codes:
            legacy_fields = _build_dividend_query_fields(
                field_list, dt, legacy_ratio_only=True
            )
            try:
                if use_epoch_sql:
                    legacy_data = manager.get_kline_data_batch_epoch_seconds(
                        retry_codes,
                        period,
                        start_time,
                        end_time,
                        dividend_type=None,
                        fields=legacy_fields,
                        workers=_BATCH_READ_WORKERS,
                        tz_offset_seconds=_LOCAL_TZ_OFFSET_SECONDS,
                    )
                else:
                    legacy_data = manager.get_kline_data_batch(
                        retry_codes,
                        period,
                        start_time,
                        end_time,
                        dividend_type=None,
                        fields=legacy_fields,
                        workers=_BATCH_READ_WORKERS,
                    )
                for stock in retry_codes:
                    legacy_df = legacy_data.get(stock)
                    if legacy_df is not None and not legacy_df.empty:
                        batch_data[stock] = legacy_df
            except Exception as exc:
                if is_duckdb_lock_error(exc):
                    raise
                pass

    for stock in stock_list:
        try:
            df = batch_data.get(stock)
            if df is None or df.empty:
                result[stock] = pd.DataFrame(columns=field_list if field_list else [])
                continue
            raw_ohlc = {}
            if preserve_raw_ohlc and dt in _DIVIDEND_TYPES:
                raw_ohlc = {
                    field: df[field].copy(deep=False)
                    for field in _OHLC_FIELDS
                    if field in df.columns and (not field_list or field in field_list)
                }
            if dividend_type and dt != 'none':
                df = _select_dividend_fields(
                    df, dividend_type, context=f"{stock}/{period}"
                )
            if not use_epoch_sql:
                df = _datetime_to_framework_seconds(df)
            if field_list:
                available = [f for f in field_list if f in df.columns]
                df = df[available].copy()
            else:
                basic_fields = ['time', 'open', 'high', 'low', 'close', 'volume', 'amount',
                               'settelementPrice', 'openInterest', 'preClose', 'suspendFlag']
                available = [f for f in basic_fields if f in df.columns]
                df = df[available].copy()
            # The full-load framework can retain the raw OHLC values in an
            # internal sidecar.  They are prefixed here and removed by khFrame
            # before current_data reaches a strategy, so public fields remain
            # unchanged while a later khHistory(..., fq="none") can reuse the
            # bytes already read from DuckDB.
            for field, values in raw_ohlc.items():
                df[f"{_FRAMEWORK_RAW_PREFIX}{field}"] = values.to_numpy(copy=False)
            result[stock] = df
        except Exception:
            result[stock] = pd.DataFrame(columns=field_list if field_list else [])
    return result


def get_market_data(field_list: List[str],
                    stock_list: List[str],
                    period: str,
                    start_time: Optional[str] = None,
                    end_time: Optional[str] = None,
                    count: int = -1,
                    dividend_type: str = 'none',
                    fill_data: bool = False) -> Dict[str, pd.DataFrame]:
    """
    返回透视表格式的数据字典（与 xtdata.get_market_data 格式一致）

    返回格式：
    {
        'open': DataFrame(index=stock_codes, columns=date_strings),
        'close': DataFrame(index=stock_codes, columns=date_strings),
        ...
    }

    注意：这与 get_market_data_ex 的返回格式完全不同！
    """
    # 先获取原始数据
    data = get_market_data_ex(field_list, stock_list, period, start_time, end_time, count, dividend_type, fill_data)

    if not data or all(df is None or df.empty for df in data.values()):
        # 返回空字典
        return {field: pd.DataFrame() for field in (field_list or [])}

    # 构建透视表格式
    result = {}

    # 获取所有字段
    fields_to_pivot = field_list if field_list else []
    if not fields_to_pivot and data:
        # 如果未指定字段，从第一个非空 DataFrame 获取列名
        for df in data.values():
            if df is not None and not df.empty:
                fields_to_pivot = list(df.columns)
                break

    for field in fields_to_pivot:
        # 为每个字段创建一个透视表
        field_data = {}

        for stock, df in data.items():
            if df is None or df.empty or field not in df.columns:
                continue

            # 获取该字段的数据（索引是日期字符串）
            field_series = df[field]
            field_data[stock] = field_series

        if field_data:
            # 创建 DataFrame（行=股票，列=日期）
            result[field] = pd.DataFrame(field_data).T
        else:
            result[field] = pd.DataFrame()

    return result


def download_history_data(stock_code: str,
                          period: str,
                          start_time: Optional[str],
                          end_time: Optional[str],
                          incrementally: bool = False) -> bool:
    """
    兼容接口：在 DB 为主的环境下，尝试检查数据库中是否已有数据。
    不会进行在线下载；若数据存在返回 True，否则返回 False。
    """
    manager = _get_manager()  # 使用统一的 manager 获取方法，确保连接数一致
    try:
        df = manager.get_kline_data(stock_code, period, start_time, end_time)
        if df is None or df.empty:
            return False
        return True
    except Exception:
        return False


def get_local_data(field_list: List[str],
                   stock_list: List[str],
                   period: str,
                   start_time: Optional[str] = None,
                   end_time: Optional[str] = None,
                   count: Optional[int] = None,
                   dividend_type: str = 'none',
                   fill_data: bool = False) -> Dict[str, pd.DataFrame]:
    """
    本地数据接口（与 get_market_data_ex 完全相同）

    注意：count 参数暂不实现，直接忽略，返回时间范围内的所有数据
    """
    # 直接调用 get_market_data_ex
    return get_market_data_ex(
        field_list=field_list,
        stock_list=stock_list,
        period=period,
        start_time=start_time,
        end_time=end_time,
        dividend_type=dividend_type,
        fill_data=fill_data
    )
