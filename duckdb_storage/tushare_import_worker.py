# -*- coding: utf-8 -*-
"""
Tushare 导入工作线程

负责在后台线程中执行 tushare 数据下载，并将结果存入 DuckDB。
信号接口与 BaoStockImportThread 保持一致，便于 viewer.py 统一处理。
"""

from __future__ import annotations

import logging
import os
import traceback
from datetime import datetime, timedelta
from threading import Event, Lock
from typing import List, Dict, Any

from PyQt5.QtCore import QThread, pyqtSignal

from .tushare_importer import TushareImporter, is_tushare_index_code
from .manager import DuckDBManager
from .lock_retry import is_duckdb_lock_error, parse_duckdb_lock_error
from .incremental import (
    ADJUSTMENT_COLUMNS,
    FRONT_ADJUSTMENT_COLUMNS,
    RAW_COMPLETENESS_COLUMNS,
    RAW_PRICE_COLUMNS,
    build_incremental_plan,
    expected_trade_dates,
    full_range,
    group_missing_trade_dates,
    normalize_date8,
    required_price_columns,
    validate_adjustment_coverage,
    validate_overwrite_frame,
    validate_overwrite_frame_with_daily_evidence,
)

logger = logging.getLogger(__name__)


class _SkipLockedTask(RuntimeError):
    """用户选择跳过当前被占用的数据库文件。"""

    def __init__(self, info: dict):
        super().__init__(info.get("error") or "数据库文件被占用")
        self.info = info


class _AbortImport(RuntimeError):
    """用户选择终止 Tushare 补充任务。"""


class TushareImportThread(QThread):
    """
    Tushare 数据下载线程

    下载完成后自动调用 DuckDBManager.save_kline_data() 存入本地库。

    Signals:
        progress(completed, total)  : 已完成/总任务数
        status(msg)                 : 当前状态文本（显示在状态标签）
        log(msg)                    : 日志行（追加到日志框）
        finished(ok, summary)       : 全部完成，ok=True/False，summary=摘要文本
    """

    _TS_FREQ_MAP = {"1d": "D", "1m": "1min", "5m": "5min"}

    progress = pyqtSignal(int, int, int, int)  # (已完成, 总数, 总请求数, 最近一分钟请求数)
    status   = pyqtSignal(str)            # 状态文本
    log      = pyqtSignal(str)            # 日志行
    finished = pyqtSignal(bool, str)      # (成功, 摘要)
    lock_conflict = pyqtSignal(dict)      # 自动重试耗尽后的数据库占用详情

    def __init__(
        self,
        token:           str,
        use_proxy:       bool,
        proxy_url:       str,
        stock_list:      List[str],
        periods:         List[Dict[str, Any]],
        manager:         DuckDBManager,
        api_url:         str = "",
        force_overwrite: bool = False,
        parent=None,
    ):
        """
        Args:
            token           : tushare token
            use_proxy       : 是否使用代理
            proxy_url       : 代理地址
            stock_list      : 股票代码列表，格式 ['000001.SZ', ...]
            periods         : 下载任务列表，每项形如：
                              {
                                'period':     '1d' / '1m' / '5m',
                                'start_date': 'YYYYMMDD'（日线）或 'YYYY-MM-DD HH:MM:SS'（分钟），
                                'end_date':   同上，
                                'adj_front':  bool  # 是否同时下载前复权
                                'adj_back':   bool  # 是否同时下载后复权
                              }
            manager         : DuckDBManager 实例（共享主线程连接缓存以防冲突）
            api_url         : API 地址，留空使用默认值
            force_overwrite : True = 强制覆写（跳过缺口检测，全量重新下载）
                              False = 增量模式（默认，仅下载缺失数据段）
        """
        super().__init__(parent)
        self.token           = token
        self.use_proxy       = use_proxy
        self.proxy_url       = proxy_url
        self.api_url         = api_url
        self.stock_list      = stock_list
        self.periods         = periods
        self.manager         = manager
        self.force_overwrite = force_overwrite
        self._stop_flag      = False
        self._lock_decision_event = Event()
        self._lock_decision_guard = Lock()
        self._lock_decision = None
        self._lock_prompt_enabled = False
        self._benchmark_unresolved: List[str] = []
        self._benchmark_error = ""
        self.last_results: Dict[str, Any] = {}

    def stop(self):
        """请求停止（线程检查此标志后会在下一个股票处退出）。"""
        self._stop_flag = True
        self.set_lock_resolution("abort")

    def enable_lock_prompt(self):
        """GUI 调用时启用交互；无 GUI 调用默认跳过而不会永久等待。"""
        self._lock_prompt_enabled = True

    def set_lock_resolution(self, resolution: str):
        value = str(resolution or "skip").strip().lower()
        if value not in ("retry", "skip", "abort"):
            value = "skip"
        with self._lock_decision_guard:
            self._lock_decision = value
            self._lock_decision_event.set()

    def _request_lock_resolution(self, info: dict) -> str:
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

    def _release_lock_target(self, mgr: DuckDBManager, stock_code: str, info: dict):
        """重试前丢弃本进程缓存的失效连接，避免一直复用同一个锁状态。"""
        try:
            if os.path.basename(info.get("db_path") or "").lower() == "metadata.db":
                mgr.close_metadata_connection()
            elif stock_code:
                mgr.close_stock_connection(stock_code, skip_checkpoint=True)
        except Exception:
            pass

    def _run_lockable(
        self,
        operation,
        mgr: DuckDBManager,
        stock_code: str,
        period: str,
        operation_name: str,
        allow_when_stopped: bool = False,
    ):
        """执行数据库操作；收尾写入可在停止信号后继续完成。"""
        while allow_when_stopped or not self._stop_flag:
            try:
                return operation()
            except Exception as exc:
                if not is_duckdb_lock_error(exc):
                    raise
                info = parse_duckdb_lock_error(
                    exc,
                    stock_code=stock_code,
                    period=period,
                    operation=operation_name,
                    attempts=5,
                )
                self.log.emit(
                    f"[数据库占用] {stock_code or '元数据'} {period}，自动重试后仍未释放"
                )
                if allow_when_stopped and self._stop_flag:
                    # 用户已经要求停止时不能再弹出阻塞式选择框；底层自动重试耗尽后
                    # 记录为待修复元数据，保证线程可以及时、安全退出。
                    raise _SkipLockedTask(info)
                decision = self._request_lock_resolution(info)
                if decision == "retry":
                    self.log.emit(f"[重试] {stock_code or '元数据'} {period}")
                    self._release_lock_target(mgr, stock_code, info)
                    continue
                if decision == "skip":
                    raise _SkipLockedTask(info)
                self._stop_flag = True
                raise _AbortImport("用户停止任务")
        raise _AbortImport("任务已停止")

    def _normalize_benchmark_dataframe(self, df):
        """指数没有复权，补齐前后复权列，避免基线读取时出现空值。"""
        if df is None or df.empty:
            return df

        df = df.copy()
        for suffix in ("front", "back", "front_ratio", "back_ratio"):
            for field in ("open", "high", "low", "close"):
                src = field
                dst = f"{field}_{suffix}"
                if src in df.columns and dst not in df.columns:
                    df[dst] = df[src]
        return df

    @staticmethod
    def _plan_download_specs(plan, period: str) -> list:
        """把普通缺口与超额条数异常拆开；后者必须逐日覆写清理。"""

        anomalous = set(plan.anomalous_dates)
        normal_unresolved = (
            set(plan.missing_dates) | set(plan.partial_dates)
        ) - anomalous
        specs = [
            (*full_range(period, start, end), False)
            for start, end in group_missing_trade_dates(
                normal_unresolved, plan.expected_dates,
            )
        ]
        specs.extend(
            (*full_range(period, date8, date8), True)
            for date8 in sorted(anomalous)
        )
        return specs

    def _ensure_benchmark_data(
        self,
        importer: TushareImporter,
        mgr: DuckDBManager,
        latest_trade_date: str | None,
    ) -> int:
        """增量补齐 000300.SH 日线基准，并在写入后复核。"""
        benchmark_code = "000300.SH"
        self._benchmark_unresolved = []
        self._benchmark_error = ""

        try:
            self.status.emit("检查基准指数 000300.SH 数据...")
            end_date = latest_trade_date or datetime.now().strftime("%Y%m%d")
            end_dt = datetime.strptime(end_date[:8], "%Y%m%d")
            start_date = (end_dt - timedelta(days=365 * 20)).strftime("%Y%m%d")
            plan_expected_dates = ()

            # 旧的测试替身/第三方 manager 可能还没有完整度接口。
            # 对真实 DuckDBManager 必须走下面的精确交易日规划。
            if not hasattr(mgr, "get_existing_date_completeness"):
                stocks_db = self._run_lockable(
                    lambda: mgr.get_available_stocks(period="1d"),
                    mgr, benchmark_code, "1d", "检查基准元数据",
                )
                try:
                    mgr.close_metadata_connection()
                except Exception:
                    pass
                if benchmark_code in stocks_db:
                    self.log.emit("[基线] 000300.SH 日线数据已存在（兼容检查）")
                    return 0
                download_specs = [(start_date, end_date, False)]
            else:
                plan = self._run_lockable(
                    lambda: build_incremental_plan(
                        mgr,
                        benchmark_code,
                        "1d",
                        start_date,
                        end_date,
                        required_columns=RAW_COMPLETENESS_COLUMNS,
                    ),
                    mgr, benchmark_code, "1d", "扫描基准增量缺口",
                )
                plan_expected_dates = plan.expected_dates
                self._benchmark_unresolved = list(plan.unresolved_dates)
                download_specs = self._plan_download_specs(plan, "1d")
                if plan.partial_dates:
                    self.log.emit(
                        f"[基线] 发现 {len(plan.partial_dates)} 个不完整交易日，将重新请求"
                    )
                if plan.ignored_open_dates:
                    self.log.emit("[基线] 当日尚未收盘，暂不纳入缺口复核")
                if not download_specs:
                    self.log.emit("[基线] 000300.SH 日线数据已是最新")
                    return 0

            try:
                mgr.close_stock_connection(benchmark_code, skip_checkpoint=True)
            except Exception:
                pass

            records = 0
            for range_start, range_end, overwrite_range in download_specs:
                if self._stop_flag:
                    raise _AbortImport("任务已停止")
                self.log.emit(
                    f"[基线] 通过 Tushare 增量下载 {range_start} ~ {range_end}"
                )
                df = importer.download_index_daily(
                    benchmark_code, range_start, range_end,
                )
                df = self._normalize_benchmark_dataframe(df)
                if df is None or df.empty:
                    self.log.emit(
                        f"[警告] 000300.SH {range_start} ~ {range_end} 返回为空"
                    )
                    continue
                if overwrite_range:
                    range_start8 = normalize_date8(range_start)
                    range_end8 = normalize_date8(range_end)
                    validate_overwrite_frame(
                        df,
                        "1d",
                        [
                            date8 for date8 in plan_expected_dates
                            if range_start8 <= date8 <= range_end8
                        ],
                    )
                saved = self._run_lockable(
                    lambda frame=df: mgr.save_kline_data(
                        frame,
                        benchmark_code,
                        "1d",
                        "none",
                        skip_metadata=True,
                        overwrite=overwrite_range,
                        merge_missing=not overwrite_range,
                        **(
                            {"overwrite_trade_dates": True}
                            if overwrite_range else {}
                        ),
                    ),
                    mgr, benchmark_code, "1d", "写入基准数据",
                )
                records += int(saved or 0)
                try:
                    mgr.close_stock_connection(benchmark_code, skip_checkpoint=True)
                except Exception:
                    pass

            if hasattr(mgr, "get_existing_date_completeness"):
                post_plan = self._run_lockable(
                    lambda: build_incremental_plan(
                        mgr,
                        benchmark_code,
                        "1d",
                        start_date,
                        end_date,
                        required_columns=RAW_COMPLETENESS_COLUMNS,
                    ),
                    mgr, benchmark_code, "1d", "复核基准数据",
                )
                self._benchmark_unresolved = list(post_plan.unresolved_dates)
                if self._benchmark_unresolved:
                    self.log.emit(
                        f"[基线][待核验] 仍有 {len(self._benchmark_unresolved)} 个交易日"
                        "未完整（数据源空返或日历差异）"
                    )

            if records > 0:
                self.log.emit(f"[基线] 已写入 000300.SH 日线 {records} 条")
            return records
        except (_SkipLockedTask, _AbortImport):
            raise
        except Exception as e:
            self._benchmark_error = str(e)
            self.log.emit(f"[警告] 检查/下载 000300.SH 基线数据失败: {e}")
            return 0
        finally:
            try:
                mgr.close_stock_connection(benchmark_code, skip_checkpoint=True)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 主逻辑
    # ------------------------------------------------------------------

    def run(self):
        results: Dict[str, Any] = {
            "total": 0,
            "completed": 0,
            "success": 0,
            "up_to_date": 0,
            "failed": 0,
            "no_data": 0,
            "partial": 0,
            "skipped": 0,
            "total_records": 0,
            "adjustment_updated": 0,
            "adjustment_failed": 0,
            "adjustment_errors": [],
            "unresolved": [],
            "benchmark_unresolved": [],
            "benchmark_error": "",
            "lock_skipped": [],
            "errors": [],
            "cancelled": False,
            "metadata_updated": 0,
            "metadata_refresh_skipped": False,
        }
        self.last_results = results
        importer = TushareImporter(
            token=self.token,
            use_proxy=self.use_proxy,
            proxy_url=self.proxy_url,
            api_url=self.api_url,
        )

        # 验证连接
        self.status.emit("正在验证 token...")
        ok, msg = importer.test_connection()
        if not ok:
            self.log.emit(f"[错误] {msg}")
            results["failed"] = 1
            results["errors"].append(msg)
            self.finished.emit(False, f"token 验证失败: {msg}")
            return

        self.log.emit(f"[OK] {msg}")

        # 使用传入的 manager，避免"Database is already open"锁冲突
        mgr = self.manager
        imported_totals: Dict[tuple, int] = {}
        # 复权因子与周期、缺口区间无关。同一股票有多个周期/缺口时只请求一次，
        # 但只保留当前股票，避免全市场导入时缓存持续增长。
        factor_cache: Dict[str, Any] = {}
        factor_cache_stock = None
        short_lock_enabled = False
        try:
            if hasattr(mgr, "enable_short_lock_write"):
                mgr.enable_short_lock_write()
                short_lock_enabled = True
                self.log.emit("[数据库] 已启用短锁写入，元数据将在任务结束后统一刷新")
        except Exception as e:
            self.log.emit(f"[警告] 启用短锁写入失败，将继续执行: {e}")

        # 预查最新交易日（供日线使用，避免传未来日期）
        self.status.emit("查询最新交易日...")
        try:
            latest_trade_date = importer.get_latest_trade_date()
            self.log.emit(f"最新交易日: {latest_trade_date}")
        except Exception as e:
            latest_trade_date = None
            self.log.emit(f"[警告] 获取最新交易日失败: {e}，将使用设定的 end_date")

        try:
            benchmark_saved = self._ensure_benchmark_data(importer, mgr, latest_trade_date)
            if benchmark_saved > 0:
                imported_totals[("000300.SH", "1d")] = benchmark_saved
            results["benchmark_unresolved"] = list(self._benchmark_unresolved)
            results["benchmark_error"] = self._benchmark_error
        except _SkipLockedTask as skipped:
            info = dict(skipped.info)
            info["scope"] = "benchmark"
            results["lock_skipped"].append(info)
            self.log.emit("[基线] 000300.SH 因数据库占用暂未补充，不影响后续股票任务")
        except _AbortImport:
            results["cancelled"] = True

        # 构建任务列表：(stock, period_info)
        tasks = [(s, p) for s in self.stock_list for p in self.periods]
        total     = len(tasks)
        completed = 0
        results["total"] = total

        self.progress.emit(0, total, 0, 0)
        self.status.emit(f"开始下载，共 {total} 个任务...")

        for stock_code, period_info in tasks:
            if self._stop_flag:
                self.log.emit("用户已停止")
                break

            period     = period_info["period"]
            start_date = period_info["start_date"]
            end_date   = period_info.get("end_date", "")
            do_none    = period_info.get("adj_none",  True)
            do_front   = period_info.get("adj_front", period_info.get("adj", False))
            do_back    = period_info.get("adj_back",  False)
            # 前复权以最新因子为基准，任何价格时间轴变化都可能影响整段。
            # Tushare 后复权使用接口返回的全证券历史最早因子作为固定基准，
            # 本地向前扩展不会改变旧行口径，只需失效实际变化的输入行。
            selected_full_history_invalidation = FRONT_ADJUSTMENT_COLUMNS
            if factor_cache_stock != stock_code:
                factor_cache.clear()
                factor_cache_stock = stock_code

            # 日线 end_date：若用户指定日期超过最新交易日（如填了未来日期）则截断，否则保留用户设定
            if period == "1d" and latest_trade_date and end_date > latest_trade_date:
                end_date = latest_trade_date

            try:
                # 先只以 raw OHLC 生成行情缺口。复权列单独规划，
                # 不能因子失败就让已下载的 raw 行情无法落库。
                raw_plan = None
                adjustment_plan = None
                if self.force_overwrite:
                    plan_expected_dates, ignored_open = expected_trade_dates(
                        start_date, end_date,
                    )
                    download_specs = (
                        [(*full_range(period, start_date, end_date), True)]
                        if plan_expected_dates else []
                    )
                    if ignored_open:
                        self.log.emit(
                            f"[{completed + 1}/{total}] [扫描] {stock_code} {period} "
                            "尚未收盘/未来交易日不执行强制覆写"
                        )
                else:
                    self.status.emit(f"[{completed + 1}/{total}] {stock_code} [{period}] 扫描缺失...")
                    raw_plan = self._run_lockable(
                        lambda: self._get_incremental_plan(
                            mgr,
                            stock_code,
                            period,
                            start_date,
                            end_date,
                            required_columns=RAW_COMPLETENESS_COLUMNS,
                        ),
                        mgr, stock_code, period, "扫描增量缺口",
                    )
                    if self._stop_flag:
                        raise _AbortImport("任务已停止")
                    download_specs = self._plan_download_specs(raw_plan, period)
                    plan_expected_dates = raw_plan.expected_dates
                    if do_front or do_back:
                        adjustment_plan = self._run_lockable(
                            lambda: self._get_incremental_plan(
                                mgr,
                                stock_code,
                                period,
                                start_date,
                                end_date,
                                required_columns=required_price_columns(
                                    front=do_front,
                                    back=do_back,
                                ),
                            ),
                            mgr, stock_code, period, "扫描复权列缺口",
                        )
                    if self._stop_flag:
                        raise _AbortImport("任务已停止")
                    adjustment_needed = bool(
                        adjustment_plan and adjustment_plan.needs_download
                    )
                    if not download_specs and not adjustment_needed:
                        results["up_to_date"] += 1
                        self.log.emit(
                            f"[{completed + 1}/{total}] [最新] {stock_code} {period} "
                            "raw 行情与所选复权列均完整"
                        )
                        continue
                    partial_dates = set(raw_plan.partial_dates)
                    if adjustment_plan:
                        partial_dates.update(adjustment_plan.partial_dates)
                    if partial_dates:
                        preview = "、".join(sorted(partial_dates)[:5])
                        suffix = "..." if len(partial_dates) > 5 else ""
                        self.log.emit(
                            f"[{completed + 1}/{total}] [扫描] {stock_code} {period} "
                            f"发现 {len(partial_dates)} 个不完整交易日：{preview}{suffix}"
                        )
                    if len(download_specs) > 1:
                        self.log.emit(
                            f"[{completed + 1}/{total}] [扫描] {stock_code} {period} "
                            f"raw 行情有 {len(download_specs)} 段缺口"
                        )

                adjustment_needed = bool(
                    (do_front or do_back)
                    and (
                        download_specs
                        or (adjustment_plan and adjustment_plan.needs_download)
                    )
                )
                if self._stop_flag:
                    raise _AbortImport("任务已停止")

                try:
                    mgr.close_stock_connection(stock_code, skip_checkpoint=True)
                except Exception:
                    pass

                self.status.emit(f"[{completed + 1}/{total}] {stock_code} [{period}]...")
                adj_parts = [x for x, f in [("前复权", do_front), ("后复权", do_back)] if f]
                adj_note  = f"（含{'/'.join(adj_parts)}）" if adj_parts else ""
                total_saved = 0
                empty_ranges = 0
                for r_start, r_end, overwrite_range in download_specs:
                    if self._stop_flag:
                        raise _AbortImport("任务已停止")
                    df_batch = self._download_raw_frame(
                        importer, stock_code, period, r_start, r_end,
                    )
                    if df_batch is None or df_batch.empty:
                        saved = 0
                    else:
                        short_day_evidence = {}
                        if overwrite_range:
                            range_start8 = normalize_date8(r_start)
                            range_end8 = normalize_date8(r_end)
                            # 分钟帧缺根时用同区间日线成交量核对停牌，停牌日不再
                            # 让 --force 整日覆写被拒绝。
                            short_day_evidence = validate_overwrite_frame_with_daily_evidence(
                                df_batch,
                                period,
                                [
                                    date8 for date8 in plan_expected_dates
                                    if range_start8 <= date8 <= range_end8
                                ],
                                fetch_daily=(
                                    lambda s=r_start, e=r_end: self._download_raw_frame(
                                        importer, stock_code, "1d", s, e,
                                    )
                                ),
                            )
                        # 重试仅重复写库，不重复消耗 Tushare API 请求。
                        saved = self._run_lockable(
                            lambda frame=df_batch: mgr.save_kline_data(
                                df=frame,
                                stock_code=stock_code,
                                period=period,
                                dividend_type="none",
                                skip_metadata=True,
                                overwrite=overwrite_range,
                                merge_missing=not overwrite_range,
                                invalidate_adjustment_columns=ADJUSTMENT_COLUMNS,
                                invalidate_adjustment_columns_full_history=(
                                    selected_full_history_invalidation
                                ),
                                **(
                                    {"overwrite_trade_dates": True}
                                    if overwrite_range else {}
                                ),
                                **(
                                    {"verified_short_trade_dates": short_day_evidence}
                                    if short_day_evidence else {}
                                ),
                            ),
                            mgr, stock_code, period, "写入行情数据",
                        )
                    saved = int(saved or 0)
                    if saved > 0:
                        total_saved += saved
                        key = (stock_code, period)
                        imported_totals[key] = imported_totals.get(key, 0) + saved
                    else:
                        empty_ranges += 1
                    try:
                        mgr.close_stock_connection(stock_code, skip_checkpoint=True)
                    except Exception:
                        pass

                # raw 先落库。之后再基于本地 raw 刷新复权列。
                # 前复权的基准因子可随新除权日改变，因此一旦需要
                # 刷新就重算用户所选整段，避免库内混用两套基准。
                adjustment_updated = 0
                adjustment_error = ""
                if adjustment_needed:
                    try:
                        adjustment_updated = self._refresh_adjustment_columns(
                            importer,
                            mgr,
                            stock_code,
                            period,
                            start_date,
                            end_date,
                            do_front=do_front,
                            do_back=do_back,
                            factor_cache=factor_cache,
                        )
                        results["adjustment_updated"] += int(adjustment_updated or 0)
                    except (_SkipLockedTask, _AbortImport):
                        raise
                    except Exception as exc:
                        adjustment_error = str(exc)
                        results["adjustment_failed"] += 1
                        results["adjustment_errors"].append(
                            f"{stock_code} {period}: {adjustment_error}"
                        )
                        self.log.emit(
                            f"[{completed + 1}/{total}] [复权待补] {stock_code} {period}: "
                            f"{adjustment_error}；raw 行情已保留，"
                            "受 raw 影响的复权列已在同一事务标记待补"
                        )

                # 导入结束后用同一套交易日/根数规则复核。
                can_verify = any(
                    hasattr(mgr, method_name)
                    for method_name in (
                        "get_existing_date_completeness",
                        "get_existing_date_counts",
                        "get_existing_dates_batch",
                    )
                )
                post_raw_plan = None
                post_adjustment_plan = None
                if can_verify:
                    post_raw_plan = self._run_lockable(
                        lambda: self._get_incremental_plan(
                            mgr,
                            stock_code,
                            period,
                            start_date,
                            end_date,
                            required_columns=RAW_COMPLETENESS_COLUMNS,
                        ),
                        mgr, stock_code, period, "复核 raw 行情",
                    )
                    if do_front or do_back:
                        post_adjustment_plan = self._run_lockable(
                            lambda: self._get_incremental_plan(
                                mgr,
                                stock_code,
                                period,
                                start_date,
                                end_date,
                                required_columns=required_price_columns(
                                    front=do_front,
                                    back=do_back,
                                ),
                            ),
                            mgr, stock_code, period, "复核复权列",
                        )

                raw_unresolved = (
                    list(post_raw_plan.unresolved_dates)
                    if post_raw_plan is not None else []
                )
                adjustment_unresolved = (
                    list(post_adjustment_plan.unresolved_dates)
                    if post_adjustment_plan is not None else []
                )
                if raw_unresolved or adjustment_unresolved:
                    unresolved_entry = {
                        "stock": stock_code,
                        "period": period,
                        "raw_dates": raw_unresolved,
                        "adjustment_dates": adjustment_unresolved,
                    }
                    results["unresolved"].append(unresolved_entry)
                    self.log.emit(
                        f"[{completed + 1}/{total}] [待核验] {stock_code} {period}: "
                        f"raw {len(raw_unresolved)} 日，复权 {len(adjustment_unresolved)} 日"
                    )

                results["total_records"] += total_saved
                has_progress = bool(total_saved > 0 or adjustment_updated > 0)
                has_unresolved = bool(raw_unresolved or adjustment_unresolved)
                task_partial = bool(empty_ranges or has_unresolved or adjustment_error)
                if empty_ranges:
                    results["no_data"] += int(empty_ranges)
                    results["errors"].append(
                        f"{stock_code} {period}: {empty_ranges} 个请求区间返回空数据"
                    )

                if has_progress:
                    results["success"] += 1
                    if task_partial:
                        results["partial"] += 1
                        if empty_ranges:
                            results["failed"] += 1
                        self.log.emit(
                            f"[{completed + 1}/{total}] [部分完成] {stock_code} {period}: "
                            f"raw 写入 {total_saved} 条，复权更新 {adjustment_updated} 条{adj_note}"
                        )
                    else:
                        self.log.emit(
                            f"[{completed + 1}/{total}] [OK] {stock_code} {period}: "
                            f"raw 写入 {total_saved} 条，复权更新 {adjustment_updated} 条{adj_note}"
                        )
                elif adjustment_error or empty_ranges or (download_specs and raw_unresolved):
                    results["failed"] += 1
                    results["partial"] += 1
                    message = (
                        f"{stock_code} {period} 导入后仍未完整"
                        + (f"：{adjustment_error}" if adjustment_error else "")
                    )
                    results["errors"].append(message)
                    self.log.emit(f"[{completed + 1}/{total}] [未完成] {message}")
                elif has_unresolved:
                    results["partial"] += 1
                else:
                    # 理论上只会在并发任务已经补齐时到达这里。
                    results["up_to_date"] += 1
            except _SkipLockedTask as skipped:
                results["skipped"] += 1
                results["lock_skipped"].append(skipped.info)
                self.log.emit(
                    f"[{completed + 1}/{total}] [跳过] {stock_code} {period} 因数据库文件被占用"
                )
            except _AbortImport:
                self._stop_flag = True
                self.log.emit("用户已停止")
            except Exception as e:
                results["failed"] += 1
                results["errors"].append(f"{stock_code} {period}: {e}")
                self.log.emit(f"[{completed + 1}/{total}] [错误] {stock_code} {period}: {e}")
                logger.debug(traceback.format_exc())
            finally:
                try:
                    mgr.close_stock_connection(stock_code, skip_checkpoint=True)
                except Exception:
                    pass
                completed += 1
                results["completed"] = completed
                self.progress.emit(
                    completed, total, importer.request_count, len(importer.request_times)
                )

            if self._stop_flag:
                break

        # 数据已经写入各股票库；在收尾阶段短暂刷新 metadata.db。
        if imported_totals:
            metadata_records = [
                (stock, period, records)
                for (stock, period), records in imported_totals.items()
            ]
            try:
                results["metadata_updated"] = int(self._run_lockable(
                    lambda: mgr.batch_update_metadata(metadata_records),
                    mgr, "metadata", "", "刷新元数据",
                    allow_when_stopped=True,
                ) or 0)
                self.log.emit(
                    f"[元数据] 刷新完成: {results['metadata_updated']}/{len(metadata_records)}"
                )
            except _SkipLockedTask as skipped:
                info = dict(skipped.info)
                info["scope"] = "metadata"
                results["lock_skipped"].append(info)
                results["metadata_refresh_skipped"] = True
                self.log.emit("[警告] 数据已写入，但 metadata.db 被占用；可稍后扫描修复元数据")
            except _AbortImport:
                results["cancelled"] = True
            except Exception as e:
                results["metadata_refresh_skipped"] = True
                results["errors"].append(f"元数据刷新失败: {e}")
                self.log.emit(f"[警告] 数据已写入，但元数据刷新失败: {e}")

        try:
            mgr.close_metadata_connection()
        except Exception:
            pass
        if short_lock_enabled:
            try:
                mgr.disable_short_lock_write()
            except Exception:
                pass

        results["cancelled"] = bool(self._stop_flag or results["cancelled"])
        summary_parts = [
            f"完成 {results['completed']}/{results['total']} 个任务",
            f"成功 {results['success']} 个",
            f"已是最新 {results['up_to_date']} 个",
            f"写入 {results['total_records']} 条",
        ]
        if results["failed"]:
            summary_parts.append(f"失败 {results['failed']} 个")
        if results["skipped"]:
            summary_parts.append(f"占用跳过 {results['skipped']} 个")
        if len(results["lock_skipped"]) > results["skipped"]:
            summary_parts.append(f"其他占用 {len(results['lock_skipped']) - results['skipped']} 项")
        if results["partial"]:
            summary_parts.append(f"部分完成 {results['partial']} 个")
        if results["adjustment_updated"]:
            summary_parts.append(f"复权刷新 {results['adjustment_updated']} 条")
        if results["adjustment_failed"]:
            summary_parts.append(f"复权待补 {results['adjustment_failed']} 个")
        if results["unresolved"]:
            unresolved_days = sum(
                len(set(item.get("raw_dates", ())) | set(item.get("adjustment_dates", ())))
                for item in results["unresolved"]
            )
            summary_parts.append(
                f"导入后待核验 {len(results['unresolved'])} 个任务/"
                f"{unresolved_days} 个交易日"
            )
        if results["benchmark_unresolved"]:
            summary_parts.append(
                f"基准待核验 {len(results['benchmark_unresolved'])} 个交易日"
            )
        if results["benchmark_error"]:
            summary_parts.append("基准检查失败")
        if results["metadata_refresh_skipped"]:
            summary_parts.append("元数据待刷新")
        if results["cancelled"]:
            summary_parts.append("任务已停止")
        summary = "，".join(summary_parts)
        self.status.emit(summary)
        ok = not (
            results["failed"]
            or results["adjustment_failed"]
            or results["unresolved"]
            or results["benchmark_unresolved"]
            or results["benchmark_error"]
            or results["lock_skipped"]
            or results["metadata_refresh_skipped"]
            or results["cancelled"]
        )
        self.finished.emit(ok, summary)

    # ------------------------------------------------------------------
    # 内部：raw 先落库，复权列再独立刷新
    # ------------------------------------------------------------------

    def _download_raw_frame(
        self,
        importer: TushareImporter,
        stock_code: str,
        period: str,
        start_date: str,
        end_date: str,
    ):
        """仅下载原始行情，不请求复权因子。"""

        freq = self._TS_FREQ_MAP.get(period, "D")
        is_index = is_tushare_index_code(stock_code)
        if period == "1d" and is_index:
            return importer.download_index_daily(stock_code, start_date, end_date)
        if period == "1d":
            return importer.download_daily(
                stock_code, start_date, end_date, adj="none",
            )
        if is_index:
            raise RuntimeError(
                f"{stock_code} 是指数；Tushare 当前仅支持指数日线，"
                "不能按股票分钟接口下载"
            )
        return importer.download_minutes(
            stock_code, freq, start_date, end_date,
        )

    def _refresh_adjustment_columns(
        self,
        importer: TushareImporter,
        mgr: DuckDBManager,
        stock_code: str,
        period: str,
        start_date: str,
        end_date: str,
        *,
        do_front: bool,
        do_back: bool,
        factor_cache: Dict[str, Any] | None = None,
    ) -> int:
        """从库内全部 raw 历史重算所选复权列，其它字段不变。

        ``start_date``/``end_date`` 仅表示触发本次导入的用户区间。前复权
        以最新因子为基准，真正刷新时必须覆盖该证券/周期已存的完整时间轴，
        否则区间外旧行会继续使用上一套基准。
        """

        if not (do_front or do_back):
            return 0

        df_raw = self._run_lockable(
            lambda: mgr.get_kline_data(
                stock_code,
                period,
                fields=list(RAW_PRICE_COLUMNS),
            ),
            mgr, stock_code, period, "读取本地 raw 行情",
        )
        try:
            # 下一步可能需要网络请求因子；不在等网络时占用 DB。
            mgr.close_stock_connection(stock_code, skip_checkpoint=True)
        except Exception:
            pass

        if df_raw is None or df_raw.empty:
            raise RuntimeError(f"{stock_code} {period} 本地 raw 行情为空，无法计算复权")

        result = df_raw[["time"]].copy()
        selected_columns: List[str] = []
        is_index = is_tushare_index_code(stock_code)
        if is_index:
            for suffix, enabled in (("front", do_front), ("back", do_back)):
                if not enabled:
                    continue
                for field in RAW_PRICE_COLUMNS:
                    column = f"{field}_{suffix}"
                    result[column] = df_raw[field]
                    selected_columns.append(column)
        else:
            if factor_cache is not None and stock_code in factor_cache:
                df_factor = factor_cache[stock_code]
            else:
                df_factor = importer.download_adj_factor(stock_code)
                if factor_cache is not None and df_factor is not None and not df_factor.empty:
                    factor_cache[stock_code] = df_factor
            if df_factor is None or df_factor.empty:
                raise RuntimeError(f"{stock_code} 复权因子为空，无法刷新所选复权列")

            if do_front and do_back:
                df_front, df_back = TushareImporter._apply_both_adj(
                    df_raw, df_factor,
                )
            elif do_front:
                df_front = TushareImporter._apply_adj(
                    df_raw, df_factor, mode="qfq",
                )
                df_back = None
            else:
                df_front = None
                df_back = TushareImporter._apply_adj(
                    df_raw, df_factor, mode="hfq",
                )

            for adjusted, suffix, enabled, label in (
                (df_front, "front", do_front, "前复权"),
                (df_back, "back", do_back, "后复权"),
            ):
                if not enabled:
                    continue
                if adjusted is None or adjusted.empty:
                    raise RuntimeError(
                        f"{stock_code} {label}计算失败，复权因子与行情日期不匹配"
                    )
                for field in RAW_PRICE_COLUMNS:
                    column = f"{field}_{suffix}"
                    if column not in adjusted.columns or adjusted[column].isna().all():
                        raise RuntimeError(f"{stock_code} {label}列 {column} 无有效数据")
                    result[column] = adjusted[column].values
                    selected_columns.append(column)

        if not selected_columns:
            return 0
        suffixes = []
        if do_front:
            suffixes.append("front")
        if do_back:
            suffixes.append("back")
        expected_updates = validate_adjustment_coverage(
            df_raw,
            result,
            suffixes,
            period=period,
        )
        updated = int(self._run_lockable(
            lambda: mgr.update_kline_columns(
                result,
                stock_code,
                period,
                columns=selected_columns,
            ),
            mgr, stock_code, period, "刷新复权列",
        ) or 0)
        if updated != expected_updates:
            raise RuntimeError(
                f"{stock_code} {period} 复权刷新仅匹配 "
                f"{updated}/{expected_updates} 行，"
                "已保留 raw 并标记为待核验"
            )
        return updated

    def _download_and_save_batch(
        self,
        importer:   TushareImporter,
        mgr:        DuckDBManager,
        stock_code: str,
        period:     str,
        start_date: str,
        end_date:   str,
        do_none:    bool,
        do_front:   bool,
        do_back:    bool,
    ) -> tuple:
        """
        兼容旧调用方：先保存 raw，再独立刷新复权列。

        表主键为 time（单列），同一时间戳只允许一行。所有复权类型通过不同的列
        （open_front/open_back 等）存在同一行里，而非用 dividend_type 区分多行。

        返回 (saved, saved if do_front else 0, saved if do_back else 0)。
        """
        df_raw = self._download_raw_frame(
            importer, stock_code, period, start_date, end_date,
        )
        if df_raw is None or df_raw.empty:
            return 0, 0, 0

        short_day_evidence = {}
        if self.force_overwrite:
            overwrite_expected_dates, _ignored_open = expected_trade_dates(
                start_date, end_date,
            )
            short_day_evidence = validate_overwrite_frame_with_daily_evidence(
                df_raw,
                period,
                overwrite_expected_dates,
                fetch_daily=lambda: self._download_raw_frame(
                    importer, stock_code, "1d", start_date, end_date,
                ),
            )

        saved = mgr.save_kline_data(
            df=df_raw, stock_code=stock_code, period=period, dividend_type="none",
            **(
                {"verified_short_trade_dates": short_day_evidence}
                if short_day_evidence else {}
            ),
            skip_metadata=True,
            overwrite=self.force_overwrite,
            merge_missing=not self.force_overwrite,
            invalidate_adjustment_columns=ADJUSTMENT_COLUMNS,
            **(
                {
                    "invalidate_adjustment_columns_full_history": (
                        FRONT_ADJUSTMENT_COLUMNS
                    )
                }
            ),
            **(
                {"overwrite_trade_dates": True}
                if self.force_overwrite else {}
            ),
        )
        if do_front or do_back:
            self._refresh_adjustment_columns(
                importer,
                mgr,
                stock_code,
                period,
                start_date,
                end_date,
                do_front=do_front,
                do_back=do_back,
                factor_cache={},
            )
        return saved, saved if do_front else 0, saved if do_back else 0

    def _download_batch_frame(
        self,
        importer: TushareImporter,
        stock_code: str,
        period: str,
        start_date: str,
        end_date: str,
        do_front: bool,
        do_back: bool,
        factor_cache: Dict[str, Any] | None = None,
    ):
        """下载并计算复权列，但不打开 DuckDB，便于写锁重试复用同一批数据。"""
        # 1. 下载原始数据
        is_index = is_tushare_index_code(stock_code)
        df_raw = self._download_raw_frame(
            importer, stock_code, period, start_date, end_date,
        )

        if df_raw is None or df_raw.empty:
            return df_raw

        # 2. 计算复权价，将结果列合并到 df_raw（单行包含全部数据）
        _front_cols = ["open_front", "high_front", "low_front", "close_front"]
        _back_cols  = ["open_back",  "high_back",  "low_back",  "close_back"]

        if is_index and (do_front or do_back):
            # 指数没有股票复权因子。为兼容 KH 的统一字段选择逻辑，将原始价
            # 复制到所选复权列，数值不做任何变换。
            for suffix, enabled in (("front", do_front), ("back", do_back)):
                if enabled:
                    for field in ("open", "high", "low", "close"):
                        if field in df_raw.columns:
                            df_raw[f"{field}_{suffix}"] = df_raw[field]
        elif do_front or do_back:
            if factor_cache is not None and stock_code in factor_cache:
                df_factor = factor_cache[stock_code]
            else:
                df_factor = importer.download_adj_factor(stock_code)
                if factor_cache is not None and df_factor is not None and not df_factor.empty:
                    factor_cache[stock_code] = df_factor
            if df_factor is None or df_factor.empty:
                raise RuntimeError(f"{stock_code} 复权因子为空，无法生成所选复权行情")

            if do_front and do_back:
                df_f, df_b = TushareImporter._apply_both_adj(df_raw, df_factor)
            elif do_front:
                df_f = TushareImporter._apply_adj(df_raw, df_factor, mode="qfq")
                df_b = None
            else:
                df_f = None
                df_b = TushareImporter._apply_adj(df_raw, df_factor, mode="hfq")

            expected_frames = []
            if do_front:
                expected_frames.append((df_f, _front_cols, "前复权"))
            if do_back:
                expected_frames.append((df_b, _back_cols, "后复权"))
            for adjusted, columns, label in expected_frames:
                if adjusted is None or adjusted.empty:
                    raise RuntimeError(f"{stock_code} {label}计算失败，复权因子与行情日期不匹配")
                required_columns = [
                    column for column in columns
                    if column.rsplit("_", 1)[0] in df_raw.columns
                ]
                for column in required_columns:
                    if column not in adjusted.columns or adjusted[column].isna().all():
                        raise RuntimeError(f"{stock_code} {label}列 {column} 无有效数据")
                    df_raw[column] = adjusted[column].values

        return df_raw

    # ------------------------------------------------------------------
    # 增量辅助：复用全局交易日/根数/必需字段规则
    # ------------------------------------------------------------------

    def _get_incremental_plan(
        self,
        mgr: DuckDBManager,
        stock_code: str,
        period: str,
        start_date: str,
        end_date: str,
        *,
        required_columns=RAW_COMPLETENESS_COLUMNS,
        now: datetime | None = None,
    ):
        """生成增量计划；对旧 manager 替身保留最小兼容。"""

        planning_manager = mgr
        trade_days = None
        if not hasattr(mgr, "get_existing_date_completeness"):
            start8 = normalize_date8(start_date)
            end8 = normalize_date8(end_date)

            class _LegacyCoverageAdapter:
                def get_existing_date_completeness(
                    self,
                    _stock_code,
                    _period,
                    required_columns=None,
                    raise_on_error=True,
                    start_date=None,
                    end_date=None,
                ):
                    del required_columns, raise_on_error
                    if hasattr(mgr, "get_existing_date_counts"):
                        counts = mgr.get_existing_date_counts(
                            _stock_code,
                            _period,
                            raise_on_error=True,
                            start_date=start_date,
                            end_date=end_date,
                        )
                        return {
                            str(date_value): {
                                "total_rows": int(count or 0),
                                "valid_rows": int(count or 0),
                            }
                            for date_value, count in (counts or {}).items()
                        }
                    existing = mgr.get_existing_dates_batch(
                        _stock_code, [_period], raise_on_error=True,
                    ).get(_period, set())
                    return {
                        str(date_value): {"total_rows": 1, "valid_rows": 1}
                        for date_value in (existing or set())
                        if start_date <= str(date_value) <= end_date
                    }

            planning_manager = _LegacyCoverageAdapter()
            # 旧单元测会替换 is_trade_day；仅在兼容路径枚举。
            from khQTTools import is_trade_day

            cursor = datetime.strptime(start8, "%Y%m%d")
            end_dt = datetime.strptime(end8, "%Y%m%d")
            trade_days = []
            while cursor <= end_dt:
                date8 = cursor.strftime("%Y%m%d")
                if is_trade_day(date8):
                    trade_days.append(date8)
                cursor += timedelta(days=1)

        return build_incremental_plan(
            planning_manager,
            stock_code,
            period,
            start_date,
            end_date,
            required_columns=required_columns,
            trade_days=trade_days,
            now=now,
        )

    def _get_missing_ranges(
        self,
        mgr:        DuckDBManager,
        stock_code: str,
        period:     str,
        start_date: str,
        end_date:   str,
    ) -> list:
        """兼容旧调用方；新代码优先使用 ``_get_incremental_plan``。"""
        try:
            plan = self._get_incremental_plan(
                mgr,
                stock_code,
                period,
                start_date,
                end_date,
                required_columns=RAW_COMPLETENESS_COLUMNS,
            )
            return list(plan.download_ranges)
        except Exception:
            logger.debug(
                f"_get_missing_ranges failed for {stock_code} {period}",
                exc_info=True,
            )
            raise
