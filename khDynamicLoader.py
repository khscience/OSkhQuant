# coding: utf-8
"""
动态数据加载器模块

功能：
1. 按时段分批加载历史数据，降低内存占用
2. 自动清理已用完的数据释放内存
3. 支持不同数据周期的智能分段策略
4. 数据来自 DuckDB 本地数据源

作者: 看海量化
版本: V1.1.0
"""

import datetime
import inspect
import logging
import gc
import os
from collections.abc import Mapping
from typing import Dict, List, Optional, Tuple
import pandas as pd
from duckdb_storage.lock_retry import is_duckdb_lock_error, parse_duckdb_lock_error
from performance_config import get_performance_config

# 导入数据源管理器
try:
    from khDataSource import get_data_source_manager
except ImportError:
    get_data_source_manager = None
    logging.warning("khDataSource模块未导入，动态加载器无法加载数据")


class DynamicDataLoader:
    """动态数据加载器

    按时段分批加载回测数据，降低内存占用。
    支持按天分段，自动管理数据生命周期。

    使用示例:
        loader = DynamicDataLoader(
            config=config,
            stock_codes=['000001.SZ', '600000.SH'],
            data_period='1m',
            start_date='20240101',
            end_date='20241231',
            chunk_size=5
        )

        # 在回测主循环中
        for current_time in all_times:
            chunk_data = loader.ensure_data_loaded(current_time)
            # ... 使用数据

        # 回测结束清理
        loader.cleanup()
    """

    # 不同数据周期的默认分段策略（交易日数量）
    # 默认都设为1天，实现真正的边回测边加载，用完即释放
    DEFAULT_CHUNK_STRATEGIES = {
        'tick': 1,     # tick数据每1天加载
        '1m': 1,       # 1分钟数据每1天加载
        '5m': 1,       # 5分钟数据每1天加载
        '15m': 1,      # 15分钟数据每1天加载
        '30m': 1,      # 30分钟数据每1天加载
        '1h': 1,       # 1小时数据每1天加载
        '1d': 1,       # 日线数据每1天加载
    }

    def __init__(self,
                 config,
                 stock_codes: List[str],
                 data_period: str,
                 start_date: str,
                 end_date: str,
                 load_start_date: str = None,
                 enabled: bool = True,
                 chunk_size: int = None,
                 field_list: List[str] = None,
                 dividend_type: str = 'front',
                 tools = None,
                 callback = None,
                 lock_decision_callback = None,
                 lock_skip_callback = None):
        """初始化动态数据加载器

        Args:
            config: KhConfig配置对象
            stock_codes: 股票代码列表
            data_period: 数据周期 (tick/1m/5m/1d等)
            start_date: 回测开始日期 (YYYYMMDD格式)
            end_date: 回测结束日期 (YYYYMMDD格式)
            enabled: 是否启用动态加载
            chunk_size: 分段大小（交易日数量），None则使用默认策略
            field_list: 数据字段列表
            dividend_type: 复权类型
            tools: KhQuTools实例（用于判断交易日）
            callback: 日志回调对象（trader_callback）
        """
        self.config = config
        self.stock_codes = stock_codes
        self.data_period = data_period
        self.start_date = start_date
        self.end_date = end_date
        self.load_start_date = load_start_date or start_date
        self.enabled = enabled
        self.field_list = field_list or ['time', 'open', 'high', 'low', 'close', 'volume']
        self.dividend_type = dividend_type
        self.tools = tools
        self.callback = callback
        self.lock_decision_callback = lock_decision_callback
        self.lock_skip_callback = lock_skip_callback
        self._lock_skipped_stocks = set()

        # 确保field_list中包含必要字段
        if 'time' not in self.field_list:
            self.field_list = ['time'] + self.field_list
        if 'close' not in self.field_list:
            self.field_list.append('close')

        # 确定分段大小
        if chunk_size is not None and chunk_size > 0:
            self.chunk_size = chunk_size
        else:
            # 使用默认策略（默认1天）
            self.chunk_size = self.DEFAULT_CHUNK_STRATEGIES.get(data_period, 1)

        # 数据状态
        self.loaded_chunks: Dict[int, Dict[str, pd.DataFrame]] = {}  # chunk_idx -> {code: DataFrame}
        self.current_chunk_idx: int = -1
        self.time_segments: List[Tuple[str, str]] = []  # [(start_date, end_date), ...]
        self.trading_days: List[str] = []  # 所有交易日列表 (YYYYMMDD格式)

        # 初始化时间分段
        self._init_time_segments()

        self._log(f"动态数据加载器初始化完成: enabled={enabled}, chunk_size={self.chunk_size}天, "
                  f"period={data_period}, 总分段数={len(self.time_segments)}, 交易日数={len(self.trading_days)}")

    def _log(self, message: str, level: str = "INFO"):
        """输出日志

        Args:
            message: 日志消息
            level: 日志级别 (INFO/WARNING/ERROR)
        """
        full_message = f"[动态加载] {message}"
        if self.callback and hasattr(self.callback, 'gui'):
            logging.log(getattr(logging, level.upper(), logging.INFO), full_message)
        else:
            print(f"[{level}] {full_message}")

    def _init_time_segments(self):
        """初始化时间分段列表

        将回测时段按chunk_size个交易日分成多个分段
        """
        # 解析日期
        try:
            start = datetime.datetime.strptime(self.start_date, "%Y%m%d").date()
            end = datetime.datetime.strptime(self.end_date, "%Y%m%d").date()
        except ValueError as e:
            self._log(f"日期解析失败: {e}", "ERROR")
            return

        # 获取所有交易日
        current = start
        self.trading_days = []

        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            date_str_compact = current.strftime("%Y%m%d")

            is_trading_day = False
            if self.tools and hasattr(self.tools, 'is_trade_day'):
                # 使用KhQuTools判断交易日
                is_trading_day = self.tools.is_trade_day(date_str)
            else:
                # 简单判断：非周末即为交易日（不考虑节假日）
                is_trading_day = current.weekday() < 5

            if is_trading_day:
                self.trading_days.append(date_str_compact)

            current += datetime.timedelta(days=1)

        # 按chunk_size分段
        self.time_segments = []
        for i in range(0, len(self.trading_days), self.chunk_size):
            segment_days = self.trading_days[i:i + self.chunk_size]
            if segment_days:
                self.time_segments.append((segment_days[0], segment_days[-1]))

        if self.time_segments:
            self._log(f"时间分段完成: {len(self.time_segments)}个分段, "
                     f"首段={self.time_segments[0]}, 末段={self.time_segments[-1]}")

    def load_all_at_once(self) -> Dict[str, pd.DataFrame]:
        """一次性加载所有数据（用于非动态加载模式或向后兼容）

        Returns:
            Dict[str, pd.DataFrame]: {股票代码: DataFrame}
        """
        self._log("传统模式：一次性加载所有数据")
        return self._load_data_for_period(self.start_date, self.end_date)

    def load_chunk(self, chunk_idx: int) -> Dict[str, pd.DataFrame]:
        """加载指定分段的数据

        Args:
            chunk_idx: 分段索引 (从0开始)

        Returns:
            Dict[str, pd.DataFrame]: {股票代码: DataFrame}
        """
        if chunk_idx < 0 or chunk_idx >= len(self.time_segments):
            self._log(f"无效的分段索引: {chunk_idx} (有效范围: 0-{len(self.time_segments)-1})", "WARNING")
            return {}

        # 检查是否已加载
        if chunk_idx in self.loaded_chunks:
            return self.loaded_chunks[chunk_idx]

        segment = self.time_segments[chunk_idx]
        load_start = self._chunk_load_start(chunk_idx)
        self._log(f"加载分段 {chunk_idx + 1}/{len(self.time_segments)}: {segment[0]} ~ {segment[1]}")

        data = self._load_data_for_period(load_start, segment[1])
        # #13 Layer3: tick 的 time 列是 datetime 对象, 而框架下游(khFrame:3612 _is_ms_timestamp 等)假定
        # time 为整数毫秒(kline 口径)。统一把 datetime time 列转毫秒整数, 与 kline 一致, 避免 int(datetime) 崩。
        self._normalize_tick_time_columns(data)
        self.loaded_chunks[chunk_idx] = data

        # 统计加载的数据量
        total_rows = sum(len(df) for df in data.values() if isinstance(df, pd.DataFrame))
        self._log(f"分段 {chunk_idx + 1} 加载完成: {len(data)}只股票, {total_rows}条数据")

        return data

    def _chunk_load_start(self, chunk_idx: int) -> str:
        """Return the physical load start for a chunk, including lookback data.

        time_segments are still based on the real backtest range.  Only the
        data query is allowed to start earlier, so the main loop never runs
        warm-up bars as backtest bars.
        """
        if chunk_idx <= 0:
            return min(str(self.load_start_date), str(self.time_segments[chunk_idx][0]))

        seg0 = str(self.time_segments[chunk_idx][0])
        try:
            perf = get_performance_config(self.config)
            preload_days_cal = int(perf.get("framework_history_preload_days", 0) or 0)
        except Exception:
            preload_days_cal = 0

        cands = []
        # 显式配置的日历日预读(向后兼容: 用户手动设了就尊重)
        if preload_days_cal > 0:
            try:
                cands.append((datetime.datetime.strptime(seg0, "%Y%m%d").date()
                              - datetime.timedelta(days=preload_days_cal)).strftime("%Y%m%d"))
            except Exception:
                pass
        # 自适应"交易日"预读(默认): 按策略 khHistory 观测到的回看交易日数往回数,
        # 复用真实交易日历(trade_day_n_before), 跨春节等长假也能拿到足够 lookback,
        # 消除分段开头 idx_short 逐股回退。不改回测结果, 只改"从哪天加载"。
        try:
            from khQTTools import khist_observed_trading_days, trade_day_n_before
            _td = int(khist_observed_trading_days())
            if _td > 0:
                cands.append(trade_day_n_before(seg0, _td))
        except Exception:
            pass

        if not cands:
            return seg0
        lookback_start = min(cands)  # YYYYMMDD 字符串比较, 取更早(更充分的预读)
        try:
            return max(str(self.load_start_date), lookback_start)  # 不早于配置的总加载起点
        except Exception:
            return lookback_start

    def _load_data_for_period(self, start: str, end: str) -> Dict[str, pd.DataFrame]:
        """加载指定时间段的数据

        Args:
            start: 开始日期 (YYYYMMDD)
            end: 结束日期 (YYYYMMDD)

        Returns:
            Dict[str, pd.DataFrame]: {股票代码: DataFrame}
        """
        # 优先使用全局数据源管理器
        data_source_mgr = None
        if get_data_source_manager is not None:
            data_source_mgr = get_data_source_manager()

        if data_source_mgr is None:
            self._log("数据源管理器未初始化，无法加载数据", "ERROR")
            return {}

        return self._load_data_with_manager(data_source_mgr, start, end)

    def _load_data_with_manager(self, data_source_mgr, start: str, end: str) -> Dict[str, pd.DataFrame]:
        """Load one dynamic segment through the configured data source manager."""
        perf = get_performance_config(self.config)
        raw_batch_size = perf.get("duckdb_load_batch_size", "auto")
        try:
            raw_batch_text = str(raw_batch_size).strip().lower()
        except Exception:
            raw_batch_text = "auto"
        if raw_batch_size is None or raw_batch_text in ("", "auto", "default"):
            batch_size = min(len(self.stock_codes) or 1, 250)
        else:
            try:
                batch_size = int(raw_batch_size or 250)
            except Exception:
                batch_size = min(len(self.stock_codes) or 1, 250)
        batch_size = max(1, min(batch_size, len(self.stock_codes) or 1))

        use_raw_fastpath = (
            str(perf.get("framework_raw_duckdb_load", True)).strip().lower() in ("1", "true", "yes", "on")
            and getattr(data_source_mgr, "data_source", "") == "duckdb"
            and hasattr(data_source_mgr, "get_market_data_ex_framework_raw")
        )
        read_fn = (
            data_source_mgr.get_market_data_ex_framework_raw
            if use_raw_fastpath else data_source_mgr.get_market_data_ex
        )

        historical_data = {}
        total_batches = (len(self.stock_codes) + batch_size - 1) // batch_size
        for batch_idx in range(total_batches):
            batch_codes = self.stock_codes[batch_idx * batch_size:(batch_idx + 1) * batch_size]
            batch_codes = [code for code in batch_codes if code not in self._lock_skipped_stocks]
            if not batch_codes:
                continue
            try:
                data = read_fn(
                    field_list=self.field_list,
                    stock_list=batch_codes,
                    period=self.data_period,
                    start_time=start,
                    end_time=end,
                    dividend_type=self.dividend_type,
                    fill_data=True
                )
            except Exception as e:
                self._log(
                    f"批量加载分段 {start}~{end} 第 {batch_idx + 1}/{total_batches} 批失败: {str(e)}，回退逐个加载",
                    "ERROR",
                )
                data = {}
                for code in batch_codes:
                    while True:
                        try:
                            single_data = read_fn(
                                field_list=self.field_list,
                                stock_list=[code],
                                period=self.data_period,
                                start_time=start,
                                end_time=end,
                                dividend_type=self.dividend_type,
                                fill_data=True,
                            )
                            if single_data:
                                data.update(single_data)
                            break
                        except Exception as single_exc:
                            if not is_duckdb_lock_error(single_exc):
                                self._log(f"加载 {code} 数据失败: {str(single_exc)}", "ERROR")
                                break
                            info = parse_duckdb_lock_error(
                                single_exc,
                                stock_code=code,
                                period=self.data_period,
                                operation="read",
                                attempts=5,
                            )
                            decision = "skip"
                            if callable(self.lock_decision_callback):
                                decision = str(self.lock_decision_callback(info) or "skip").lower()
                            if decision == "retry":
                                continue
                            if decision == "abort":
                                raise RuntimeError(
                                    f"用户停止回测：数据库文件持续被占用 {code} {self.data_period}"
                                ) from single_exc
                            self._lock_skipped_stocks.add(code)
                            if callable(self.lock_skip_callback):
                                self.lock_skip_callback(info)
                            self._log(
                                f"数据库占用，已跳过本次回测数据: {code} {self.data_period}",
                                "WARNING",
                            )
                            break

            for code in batch_codes:
                try:
                    if data and code in data:
                        df = data[code]
                        if isinstance(df, pd.DataFrame) and len(df) > 0:
                            historical_data[code] = df
                except Exception as e:
                    self._log(f"加载 {code} 数据失败: {str(e)}", "ERROR")

        return historical_data


    def unload_chunk(self, chunk_idx: int):
        """卸载指定分段的数据，释放内存

        Args:
            chunk_idx: 分段索引
        """
        if chunk_idx in self.loaded_chunks:
            # 计算释放的内存大小
            chunk_size_mb = sum(
                df.memory_usage(deep=True).sum()
                for df in self.loaded_chunks[chunk_idx].values()
                if isinstance(df, pd.DataFrame)
            ) / (1024 * 1024)

            self._log(f"卸载分段 {chunk_idx + 1}: 释放约 {chunk_size_mb:.2f} MB")

            del self.loaded_chunks[chunk_idx]
            gc.collect()  # 强制垃圾回收

    def get_chunk_for_time(self, current_time) -> int:
        """获取指定时间点对应的分段索引

        Args:
            current_time: 时间点（时间戳或日期字符串）

        Returns:
            int: 分段索引，-1表示未找到
        """
        # 转换时间为日期字符串 (YYYYMMDD)
        current_date = self._time_to_date_str(current_time)

        if not current_date:
            return -1

        for idx, (start, end) in enumerate(self.time_segments):
            if start <= current_date <= end:
                return idx

        return -1

    def _time_to_date_str(self, current_time) -> str:
        """将时间转换为日期字符串 (YYYYMMDD)

        Args:
            current_time: 时间戳(int/float)或日期字符串

        Returns:
            str: 日期字符串 (YYYYMMDD格式)
        """
        try:
            if isinstance(current_time, (int, float)):
                # 时间戳处理
                if current_time > 1e10:
                    # 毫秒级时间戳
                    dt = datetime.datetime.fromtimestamp(current_time / 1000)
                else:
                    # 秒级时间戳
                    dt = datetime.datetime.fromtimestamp(current_time)
                return dt.strftime("%Y%m%d")
            else:
                # 字符串/datetime: 先去分隔符再取前8位日期。
                # #13 Layer2: tick 的 time 是 "2026-04-22 09:30:00"(带横杠), 原 [:8] 截成 "2026-04-"、
                # 去横杠只剩 "202604"(6位) → 回测区间过滤把所有 tick 时间点滤掉 → "没有找到任何有效的时间点"。
                return str(current_time).replace("-", "").replace("/", "")[:8]
        except Exception:
            return ""

    def ensure_data_loaded(self, current_time) -> Dict[str, pd.DataFrame]:
        """确保当前时间点的数据已加载

        这是主要的数据获取接口，会自动：
        1. 检查当前时间所属分段
        2. 加载所需分段（如果未加载）
        3. 卸载已完成的旧分段（释放内存）- 只保留当前分段

        Args:
            current_time: 当前时间点

        Returns:
            Dict[str, pd.DataFrame]: 当前分段的数据
        """
        chunk_idx = self.get_chunk_for_time(current_time)

        if chunk_idx < 0:
            return {}

        # 如果当前分段未加载，加载它
        if chunk_idx not in self.loaded_chunks:
            self.load_chunk(chunk_idx)

        # 检查是否切换了分段
        if chunk_idx != self.current_chunk_idx:
            old_chunk = self.current_chunk_idx
            self.current_chunk_idx = chunk_idx

            # 卸载旧分段（只保留当前分段，释放所有其他分段）
            if old_chunk >= 0:
                for i in list(self.loaded_chunks.keys()):
                    if i != chunk_idx:  # 只保留当前分段
                        self.unload_chunk(i)

        return self.loaded_chunks.get(chunk_idx, {})

    def get_all_times_for_chunk(self, chunk_idx: int) -> List:
        """获取指定分段的所有时间点

        Args:
            chunk_idx: 分段索引

        Returns:
            List: 时间点列表
        """
        chunk_data = self.load_chunk(chunk_idx)
        all_times = []

        for code, df in chunk_data.items():
            if isinstance(df, pd.DataFrame) and 'time' in df.columns:
                times = df['time'].values.tolist()
                all_times.extend(times)

        # 去重并排序
        return sorted(list(set(all_times)))

    def _filter_times_to_backtest_range(self, times: List) -> List:
        """Keep only actual backtest times, excluding warm-up rows."""
        filtered = []
        for value in times:
            day = self._time_to_date_str(value)
            if day and not (str(self.start_date) <= day <= str(self.end_date)):
                continue
            filtered.append(value)
        return filtered

    # 时间轴扫描时单次读取的行数预算(股票数 × 每日bar数 × 交易日数)。
    # 只读 time 列, 按该预算把后续分段合并成若干窗口, 既避免逐日查询过慢, 也不会一次性装入全部数据。
    _TIME_SCAN_ROW_BUDGET = 5_000_000
    _TIME_SCAN_BARS_PER_DAY = {
        'tick': 4800,
        '1m': 240,
        '5m': 48,
        '15m': 16,
        '30m': 8,
        '1h': 4,
        '1d': 1,
    }

    @staticmethod
    def _normalize_tick_time_columns(data: Dict[str, pd.DataFrame]) -> None:
        """把 datetime 类型的 time 列统一转为 UTC 毫秒整数(与 kline 口径一致), 原地修改。"""
        for _df in data.values():
            if isinstance(_df, pd.DataFrame) and 'time' in _df.columns and len(_df) and \
                    pd.api.types.is_datetime64_any_dtype(_df['time']):
                # datetime 是中国本地时间, 按上海时区→UTC 毫秒(与 kline 口径一致; 否则成交时间会 +8h)
                _df['time'] = _df['time'].dt.tz_localize('Asia/Shanghai').astype('int64') // 1_000_000

    @staticmethod
    def _collect_times_union(data: Dict[str, pd.DataFrame], into: set) -> None:
        """把 data 中所有股票的 time 列并入集合 into(与股票顺序无关)。"""
        for _code, _df in (data or {}).items():
            if isinstance(_df, pd.DataFrame) and 'time' in _df.columns and len(_df):
                into.update(_df['time'].values.tolist())

    def _load_times_for_period(self, start: str, end: str) -> Dict[str, pd.DataFrame]:
        """只读取 time 列(复权口径与正式加载一致, 保证行集合相同), 用于构建时间轴。

        不写入 loaded_chunks 缓存, 读完即可释放。
        """
        saved_fields = self.field_list
        try:
            self.field_list = ['time']
            data = self._load_data_for_period(start, end)
        finally:
            self.field_list = saved_fields
        self._normalize_tick_time_columns(data)
        return data

    def get_all_times(self) -> List:
        """获取所有时间点（用于主循环）

        时间轴 = 回测区间内全部股票实际时间点的并集, 与全量模式(khFrame 取所有股票时间并集)一致:
        - 第一个分段: 正式加载(主循环会立即复用), 取所有股票 time 的并集;
        - 后续分段: 只读 time 列, 按行数预算合并成若干窗口逐窗读取, 取所有股票 time 的并集;
        - 不再用"首只股票首日日内偏移 × 交易日历"推算(会漏掉其他股票的有效行情、
          结果依赖股票池顺序、首股只有预热数据时时间轴为空, tick/不规则周期推算更是错误)。

        Returns:
            List: 所有时间点列表（已去重排序）
        """
        # 注意: 日线(1d)不再走"按交易日直接生成时间戳"的快捷优化。
        # 原优化用 datetime.strptime(date).timestamp()*1000 生成本地毫秒时间戳, 但与 DuckDB 实际
        # 加载的日线 time 列(datetime)格式不一致 → 每根日线bar匹配不到数据=全空(2026-06 回归测试发现:
        # low_memory 档下所有日线回测静默空跑、无交易无 summary)。现让 1d 与分钟级一样走下方
        # "数据驱动"路径(用实际加载的 time 值), 保证与全量档字节一致; 日线数据量小, 多读分段开销可忽略。
        if not self.time_segments:
            self._log("没有可用的时间分段", "WARNING")
            return []

        all_times = set()

        # 第一个分段正式加载并保留(主循环会立即复用, 卸载只会导致重复加载)
        first_chunk = self.load_chunk(0)
        self._collect_times_union(first_chunk, all_times)

        # 后续分段: 只读 time 列, 按行数预算把连续分段合并为窗口读取
        if len(self.time_segments) > 1:
            n_stocks = max(1, len(self.stock_codes or []))
            bars_per_day = self._TIME_SCAN_BARS_PER_DAY.get(str(self.data_period).lower(), 240)
            window_days = max(1, int(self._TIME_SCAN_ROW_BUDGET // (n_stocks * bars_per_day)))

            idx = 1
            total = len(self.time_segments)
            while idx < total:
                win_start = self.time_segments[idx][0]
                win_end = self.time_segments[idx][1]
                days_in_window = len([d for d in self.trading_days if win_start <= d <= win_end])
                j = idx + 1
                while j < total:
                    seg_start, seg_end = self.time_segments[j]
                    seg_days = len([d for d in self.trading_days if seg_start <= d <= seg_end])
                    if days_in_window + seg_days > window_days:
                        break
                    days_in_window += seg_days
                    win_end = seg_end
                    j += 1

                # 已加载的分段直接复用, 否则只读 time 列
                if idx in self.loaded_chunks and j == idx + 1:
                    self._collect_times_union(self.loaded_chunks[idx], all_times)
                else:
                    window_data = self._load_times_for_period(win_start, win_end)
                    self._collect_times_union(window_data, all_times)
                    del window_data
                idx = j

        if not all_times:
            self._log("所有分段均无有效 time 列(股票无数据/数据路径异常?), 时间轴为空", "WARNING")
            return []

        # 去重并排序, 裁掉预热段
        result = self._filter_times_to_backtest_range(sorted(all_times))

        self._log(f"获取所有时间点完成: 共 {len(result)} 个时间点（全部股票时间并集）")
        return result

    def cleanup(self):
        """清理所有数据，释放内存

        应在回测结束时调用
        """
        self._log("清理所有已加载数据...")

        total_size_mb = sum(
            sum(df.memory_usage(deep=True).sum() for df in chunk.values() if isinstance(df, pd.DataFrame))
            for chunk in self.loaded_chunks.values()
        ) / (1024 * 1024)

        self.loaded_chunks.clear()
        self.current_chunk_idx = -1
        gc.collect()

        self._log(f"清理完成，释放约 {total_size_mb:.2f} MB 内存")

    def get_memory_stats(self) -> Dict:
        """获取内存使用统计

        Returns:
            Dict: 内存统计信息
        """
        total_size = 0
        chunk_sizes = {}

        for chunk_idx, data in self.loaded_chunks.items():
            chunk_size = 0
            for code, df in data.items():
                if isinstance(df, pd.DataFrame):
                    chunk_size += df.memory_usage(deep=True).sum()
            chunk_sizes[chunk_idx] = chunk_size
            total_size += chunk_size

        return {
            'total_mb': total_size / (1024 * 1024),
            'loaded_chunks': len(self.loaded_chunks),
            'total_chunks': len(self.time_segments),
            'current_chunk': self.current_chunk_idx,
            'chunk_sizes_mb': {k: v / (1024 * 1024) for k, v in chunk_sizes.items()}
        }

    def get_segment_info(self) -> List[Dict]:
        """获取所有分段信息

        Returns:
            List[Dict]: 分段信息列表
        """
        return [
            {
                'index': idx,
                'start': segment[0],
                'end': segment[1],
                'loaded': idx in self.loaded_chunks
            }
            for idx, segment in enumerate(self.time_segments)
        ]
