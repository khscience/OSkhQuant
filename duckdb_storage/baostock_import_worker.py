# -*- coding: utf-8 -*-
"""
BaoStock 多进程下载工作模块

目标：
- 多进程下载设计与 CS 版的 MultiProcessImporter 相同
- 将 baostock 请求放到子进程执行，主进程负责写入 DuckDB（避免并发写库冲突）
- Windows 使用 spawn 方式创建进程

注意：
- 软件对 BaoStock 设置每日3万次保护上限。请求计数与限流在主线程中控制（见 viewer.py 的 BaoStockRequestTracker）
"""

from __future__ import annotations

import multiprocessing as mp
from multiprocessing import Process, Queue
from collections import deque
from queue import Empty
from typing import Dict, Optional, List
import os
import math
import time
import traceback

try:
    from .worker_common import _effective_task_timeout, _is_transient_data_source_error
except ImportError:
    from worker_common import _effective_task_timeout, _is_transient_data_source_error


def _fetch_kline_once(bs, stock: str, period: str, start_date: str, end_date: str, adjustflag: str):
    """在子进程内执行单次 baostock K线查询，返回 DataFrame（含 time 列）。"""
    import pandas as pd

    if period == "1d":
        fields = "date,code,open,high,low,close,preclose,volume,amount"
        frequency = "d"
    else:
        # 当前 UI 仅支持 5m；保持与 viewer.py 一致
        fields = "date,time,code,open,high,low,close,volume,amount"
        frequency = "5"

    rs = bs.query_history_k_data_plus(
        stock,
        fields,
        start_date=start_date,
        end_date=end_date,
        frequency=frequency,
        adjustflag=adjustflag,
    )

    if rs.error_code != "0":
        raise RuntimeError(rs.error_msg or rs.error_code)

    data_list = []
    while (rs.error_code == "0") & rs.next():
        data_list.append(rs.get_row_data())

    if not data_list:
        return pd.DataFrame()

    df = pd.DataFrame(data_list, columns=rs.fields)

    # 统一生成 time 列（datetime64）
    if period == "1d":
        # 与 miniQMT/Tushare 的日线规范保持一致，避免跨源导入后同一天同时
        # 出现 00:00 与 09:30 两根日 K。
        df["time"] = (
            pd.to_datetime(df["date"], format="%Y-%m-%d", errors="coerce")
            + pd.Timedelta(hours=9, minutes=30)
        )
    else:
        if "time" in df.columns:
            # baostock 5分钟返回的时间格式如 "20250306093500000" (YYYYMMDDHHMMSSsss)
            # 我们先截取前14位 (YYYYMMDDHHMMSS)，然后解析
            time_str_series = df["time"].astype(str).str.slice(0, 14)
            df["time"] = pd.to_datetime(time_str_series, format="%Y%m%d%H%M%S", errors="coerce")
        else:
            df["time"] = pd.to_datetime(df["date"], format="%Y-%m-%d", errors="coerce")

    df = df.dropna(subset=["time"])
    return df


def _prepare_base_df(df):
    """清洗基础行情数据（不复权）。"""
    import pandas as pd

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # 兼容 preclose -> preClose（DuckDB schema 使用 preClose）
    if "preclose" in df.columns and "preClose" not in df.columns:
        df["preClose"] = df["preclose"]

    # 转数值列
    for col in ["open", "high", "low", "close", "preClose", "volume", "amount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    keep_cols = ["time", "open", "high", "low", "close", "preClose", "volume", "amount"]
    df = df[[c for c in keep_cols if c in df.columns]]
    df = df.sort_values("time")
    from duckdb_storage.units import normalize_kline_units
    return normalize_kline_units(df, source_volume_unit="shares")


def _fetch_indicators_once(
    bs,
    stock: str,
    start_date: str,
    end_date: str,
    indicators: List[str],
):
    """在可终止的子进程中查询日线指标。"""
    import pandas as pd

    fields = ["date", "code"] + list(indicators or [])
    rs = bs.query_history_k_data_plus(
        stock,
        ",".join(fields),
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="3",
    )
    if rs.error_code != "0":
        raise RuntimeError(rs.error_msg or rs.error_code)

    rows = []
    while (rs.error_code == "0") & rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=rs.fields)
    df["time"] = pd.to_datetime(df["date"], format="%Y-%m-%d", errors="coerce")
    df = df.dropna(subset=["time"])
    for column in indicators or []:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    keep = ["time"] + [column for column in indicators or [] if column in df.columns]
    return df[keep]


_ADJUSTMENT_PRICE_COLUMNS = ("open", "high", "low", "close")
_ADJUSTMENT_LABELS = {"front": "前复权", "back": "后复权"}


class AdjustmentCoverageError(ValueError):
    """BaoStock 复权结果与对应 raw 时间轴不完整一致。"""


def validate_adjustment_coverage(raw_df, adjusted_df, suffix: str):
    """校验并清洗一个复权口径，返回 ``(完整结果, 错误)``。

    完整结果只可能是 ``time + OHLC_<suffix>`` 五列；任何缺列、空值、
    重复时间或与 raw 时间轴不一致都会返回空结果和错误，从而确保调用方
    不会把部分复权列写入数据库。该纯函数也供 GUI/CLI 主进程二次防御。
    """
    import pandas as pd

    suffix = str(suffix or "").strip()
    output_columns = ["time"] + [f"{column}_{suffix}" for column in _ADJUSTMENT_PRICE_COLUMNS]
    empty_result = pd.DataFrame(columns=output_columns)

    if not suffix:
        return empty_result, "复权口径为空"
    if raw_df is None or getattr(raw_df, "empty", True):
        return empty_result, "原始行情为空，无法核验复权覆盖"
    if adjusted_df is None or getattr(adjusted_df, "empty", True):
        return empty_result, "返回空数据"
    if "time" not in raw_df.columns:
        return empty_result, "原始行情缺少 time 列"
    if "time" not in adjusted_df.columns:
        return empty_result, "复权行情缺少 time 列"

    missing_columns = [
        column for column in _ADJUSTMENT_PRICE_COLUMNS if column not in adjusted_df.columns
    ]
    if missing_columns:
        return empty_result, f"复权行情缺少字段: {', '.join(missing_columns)}"

    raw_times = pd.to_datetime(raw_df["time"], errors="coerce")
    adjusted_times = pd.to_datetime(adjusted_df["time"], errors="coerce")
    if raw_times.isna().any():
        return empty_result, "原始行情包含无效 time"
    if adjusted_times.isna().any():
        return empty_result, "复权行情包含无效 time"
    if raw_times.duplicated().any():
        return empty_result, "原始行情存在重复 time，无法一一核验"
    duplicate_count = int(adjusted_times.duplicated(keep=False).sum())
    if duplicate_count:
        return empty_result, f"复权行情存在重复 time（{duplicate_count} 行）"

    raw_time_set = set(raw_times.tolist())
    adjusted_time_set = set(adjusted_times.tolist())
    if len(raw_times) != len(adjusted_times) or raw_time_set != adjusted_time_set:
        missing_count = len(raw_time_set - adjusted_time_set)
        extra_count = len(adjusted_time_set - raw_time_set)
        return (
            empty_result,
            "复权行情未完整覆盖原始行情"
            f"（raw={len(raw_times)}，复权={len(adjusted_times)}，"
            f"缺少={missing_count}，多出={extra_count}）",
        )

    cleaned = adjusted_df[["time", *_ADJUSTMENT_PRICE_COLUMNS]].copy()
    cleaned["time"] = adjusted_times
    invalid_columns = []
    for column in _ADJUSTMENT_PRICE_COLUMNS:
        cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce")
        if (
            cleaned[column].isna().any()
            or not cleaned[column].map(math.isfinite).all()
        ):
            invalid_columns.append(column)
    if invalid_columns:
        return empty_result, f"复权行情字段存在空值或无效数值: {', '.join(invalid_columns)}"

    rename_map = {
        column: f"{column}_{suffix}" for column in _ADJUSTMENT_PRICE_COLUMNS
    }
    cleaned = cleaned.rename(columns=rename_map)
    cleaned = cleaned[output_columns].sort_values("time").reset_index(drop=True)
    return cleaned, None


def sanitize_requested_adjustments(frame, requested_suffixes, *, raw_frame=None):
    """移除返回 frame 中不完整的请求复权口径，返回 ``(frame, errors)``。

    ``raw_frame`` 应由 GUI/CLI 传入本地或本次 raw 行情；未传时使用
    ``frame`` 自身的时间轴。一个口径校验失败时会删除该口径现有的所有
    OHLC 列，同时保留 raw 与其它已通过核验的复权口径。
    """
    import pandas as pd

    result = frame.copy() if frame is not None else pd.DataFrame()
    expected_raw = raw_frame if raw_frame is not None else result
    errors = []
    seen = set()
    for requested in requested_suffixes or ():
        suffix = str(requested or "").strip()
        if not suffix or suffix == "none" or suffix in seen:
            continue
        seen.add(suffix)
        suffixed_columns = [f"{column}_{suffix}" for column in _ADJUSTMENT_PRICE_COLUMNS]
        candidate_columns = ["time", *suffixed_columns]
        if all(column in result.columns for column in candidate_columns):
            candidate = result[candidate_columns].rename(
                columns={
                    f"{column}_{suffix}": column
                    for column in _ADJUSTMENT_PRICE_COLUMNS
                }
            )
        else:
            candidate = result[[column for column in candidate_columns if column in result.columns]].copy()
            candidate = candidate.rename(
                columns={
                    f"{column}_{suffix}": column
                    for column in _ADJUSTMENT_PRICE_COLUMNS
                }
            )

        cleaned, error = validate_adjustment_coverage(expected_raw, candidate, suffix)
        if error:
            result = result.drop(
                columns=[column for column in suffixed_columns if column in result.columns],
                errors="ignore",
            )
            label = _ADJUSTMENT_LABELS.get(suffix, suffix)
            errors.append(f"{label}: {error}")
            continue

        # 使用清洗后的数值列替换原列，且用 one-to-one 合并再次保护时间轴。
        result["time"] = pd.to_datetime(result["time"], errors="coerce")
        result = result.drop(columns=suffixed_columns, errors="ignore").merge(
            cleaned,
            on="time",
            how="left",
            validate="one_to_one",
        )
    return result, errors


def _merge_adjusted(base_df, adj_df, suffix: str):
    """完整核验后将复权 OHLC 并入基础行情；部分返回一律拒绝。"""
    if base_df is None or base_df.empty:
        return base_df

    cleaned, error = validate_adjustment_coverage(base_df, adj_df, suffix)
    if error:
        raise AdjustmentCoverageError(error)
    return base_df.merge(cleaned, on="time", how="left", validate="one_to_one")


def _prepare_adjusted_columns(adj_df, suffix: str):
    """把单独下载的复权行情整理成仅含 time + 复权价格列的 DataFrame。"""
    import pandas as pd

    if adj_df is None or adj_df.empty:
        return pd.DataFrame()
    frame = adj_df.copy()
    rename_map = {}
    for column in ("open", "high", "low", "close"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
            rename_map[column] = f"{column}_{suffix}"
    frame = frame.rename(columns=rename_map)
    columns = ["time"] + list(rename_map.values())
    frame = frame[[column for column in columns if column in frame.columns]]
    # 不在这里静默去重；主进程须结合 raw 时间轴调用
    # validate_adjustment_coverage/sanitize_requested_adjustments 做完整核验。
    return frame.sort_values("time")


# 登录遇到网络类错误（服务器限流时连接被断开、读超时）先在本进程里等一等再重试。
# 登录失败会被主进程当作致命错误、让整批任务失败；也不能立刻重登加重限流。
_LOGIN_RETRY_DELAYS = (10.0, 30.0, 60.0)
# 进程已退出、任务停在「数据已就绪」多久仍没收到结果，就判定结果丢失并重试
_LOST_RESULT_GRACE_SECONDS = 30.0


def _is_network_login_error(login_result) -> bool:
    # BaoStock 网络类错误码 10002001～10002008（连接失败/超时、接收断开/错误/超时等）
    code = str(getattr(login_result, "error_code", "") or "")
    message = str(getattr(login_result, "error_msg", "") or "")
    return code.startswith("100020") or "网络" in message


def _login_with_backoff(bs, progress_queue, delays=_LOGIN_RETRY_DELAYS, sleep=time.sleep):
    login_result = bs.login()
    for delay in delays:
        if login_result.error_code == "0" or not _is_network_login_error(login_result):
            return login_result
        progress_queue.put({
            "type": "status",
            "msg": f"BaoStock 登录暂时失败（{login_result.error_msg}），{delay:.0f} 秒后重试",
        })
        sleep(delay)
        login_result = bs.login()
    return login_result


def _baostock_download_worker(task_queue: Queue, result_queue: Queue, progress_queue: Queue):
    """子进程 worker：登录 baostock，执行任务，返回序列化 DataFrame。"""
    try:
        import baostock as bs

        # 官方 socket 没有超时、断线时收包循环不会结束；统一换成带超时的连接
        try:
            from baostock_proxy import enable_baostock_proxy
        except ImportError:
            enable_baostock_proxy = None
        if enable_baostock_proxy is not None:
            enable_baostock_proxy()

        lg = _login_with_backoff(bs, progress_queue)
        if lg.error_code != "0":
            progress_queue.put({"type": "fatal", "msg": f"BaoStock登录失败: {lg.error_msg}"})
            return

        progress_queue.put({"type": "status", "msg": "BaoStock 工作进程已启动"})

        try:
            while True:
                try:
                    task = task_queue.get(timeout=1)
                except Exception:
                    continue

                if task is None:
                    break

                stock_code = task["stock_code"]
                bs_code = task["bs_code"]
                period = task["period"]
                start_date = task["start_date"]
                end_date = task["end_date"]
                task_id = task.get("task_id", 0)
                worker_pid = os.getpid()
                attempt = int(task.get("attempt", 0) or 0)
                task_kind = str(task.get("task_kind") or "kline")
                adjustment_errors = []
                progress_queue.put(
                    {
                        "type": "started",
                        "task_id": task_id,
                        "stock_code": stock_code,
                        "period": period,
                        "pid": worker_pid,
                        "attempt": attempt,
                        "task_kind": task_kind,
                    }
                )

                df_out = None
                try:
                    if task_kind == "indicator":
                        df_out = _fetch_indicators_once(
                            bs,
                            bs_code,
                            start_date,
                            end_date,
                            list(task.get("indicators") or []),
                        )
                    elif task_kind == "adjustment":
                        suffix = str(task.get("suffix") or "front")
                        adjusted_raw = _fetch_kline_once(
                            bs,
                            bs_code,
                            period,
                            start_date,
                            end_date,
                            str(task.get("adjustflag") or "2"),
                        )
                        df_out = _prepare_adjusted_columns(adjusted_raw, suffix)
                    else:
                        # raw 必须先独立取得；复权接口失败时仍把 raw 返回主进程
                        # 落库，避免一个可选字段拖垮整段原始行情。
                        base_raw = _fetch_kline_once(bs, bs_code, period, start_date, end_date, "3")
                        base_df = _prepare_base_df(base_raw)

                        if base_df is None or base_df.empty:
                            df_out = None
                        else:
                            merged = base_df
                            for enabled, adjustflag, suffix, label in (
                                (task.get("do_front", True), "2", "front", "前复权"),
                                (task.get("do_back", True), "1", "back", "后复权"),
                            ):
                                if not enabled:
                                    continue
                                try:
                                    adjusted_raw = _fetch_kline_once(
                                        bs, bs_code, period, start_date, end_date, adjustflag,
                                    )
                                    if adjusted_raw is None or adjusted_raw.empty:
                                        adjustment_errors.append(f"{label}返回空数据")
                                    else:
                                        merged = _merge_adjusted(merged, adjusted_raw, suffix)
                                except Exception as exc:
                                    adjustment_errors.append(f"{label}: {exc}")
                            merged = merged.drop_duplicates(subset=["time"], keep="last").sort_values("time")
                            df_out = merged

                    df_dict = None
                    records = 0
                    if df_out is not None and len(df_out) > 0:
                        # 进程间传输：用 dict-of-lists + 简单 index（避免依赖索引名）
                        records = int(len(df_out))
                        df_dict = {
                            "data": df_out.to_dict("list"),
                            "index": list(range(len(df_out))),
                        }

                    progress_queue.put(
                        {
                            "type": "data_ready",
                            "task_id": task_id,
                            "stock_code": stock_code,
                            "period": period,
                            "start_date": start_date,
                            "end_date": end_date,
                            "pid": worker_pid,
                            "attempt": attempt,
                            "task_kind": task_kind,
                        }
                    )
                    result_queue.put(
                        {
                            "success": True,
                            "task_id": task_id,
                            "stock_code": stock_code,
                            "period": period,
                            "start_date": start_date,
                            "end_date": end_date,
                            "attempt": attempt,
                            "worker_pid": worker_pid,
                            "task_kind": task_kind,
                            "records": records,
                            "df_dict": df_dict,
                            "adjustment_errors": list(adjustment_errors),
                        }
                    )
                    progress_queue.put(
                        {
                            "type": "progress",
                            "task_id": task_id,
                            "stock_code": stock_code,
                            "period": period,
                            "attempt": attempt,
                            "worker_pid": worker_pid,
                            "task_kind": task_kind,
                            "records": records,
                        }
                    )
                except KeyboardInterrupt:
                    # 由主进程中断时静默退出，避免子进程打印 traceback
                    break
                except Exception as e:
                    result_queue.put(
                        {
                            "success": False,
                            "task_id": task_id,
                            "stock_code": stock_code,
                            "period": period,
                            "error": str(e),
                            "attempt": attempt,
                            "worker_pid": worker_pid,
                            "task_kind": task_kind,
                            "traceback": traceback.format_exc(),
                        }
                    )
                    progress_queue.put(
                        {
                            "type": "error",
                            "task_id": task_id,
                            "stock_code": stock_code,
                            "period": period,
                            "error": str(e),
                            "attempt": attempt,
                            "task_kind": task_kind,
                        }
                    )
                finally:
                    if df_out is not None:
                        try:
                            del df_out
                        except Exception:
                            pass
        except KeyboardInterrupt:
            # 子进程被 Ctrl+C 波及时直接退出，不输出异常栈
            pass

        try:
            bs.logout()
        except Exception:
            pass

    except ImportError as e:
        progress_queue.put({"type": "fatal", "msg": f"无法导入 baostock 模块: {e}"})
    except KeyboardInterrupt:
        # 子进程级别静默退出，避免 multiprocessing 默认 traceback
        pass
    except Exception as e:
        progress_queue.put({"type": "fatal", "msg": f"工作进程异常: {e}\n{traceback.format_exc()}"})


class MultiProcessBaoStockImporter:
    """
    BaoStock 多进程下载器（主进程调度 + 子进程下载）

    使用方式：
        importer = MultiProcessBaoStockImporter(num_workers=2)
        importer.start()
        importer.add_task(stock_code, bs_code, period, start, end)
        while not importer.is_done(): ...
        importer.stop()
    """

    def __init__(
        self,
        num_workers: int = 2,
        timeout_per_task: float = 120.0,
        max_task_retries: int = 0,
        retry_backoff: Optional[List[float]] = None,
    ):
        self.num_workers = int(max(1, num_workers))
        self.timeout_per_task = float(timeout_per_task)
        self.max_task_retries = max(0, int(max_task_retries))
        self.retry_backoff = tuple(float(v) for v in (retry_backoff or (2.0, 5.0, 10.0)))

        self.task_queue: Optional[Queue] = None
        self.result_queue: Optional[Queue] = None
        self.progress_queue: Optional[Queue] = None

        self.workers: List[Process] = []
        self.pending_tasks = 0
        self.completed_tasks = 0
        self._task_id = 0
        self._is_running = False
        self._ctx = None
        self._task_meta: Dict[int, Dict] = {}
        self._completed_task_ids = set()
        self._synthetic_results = deque()
        self._progress_buffer = deque()
        self._fatal_error: Optional[str] = None

    def start(self):
        if self._is_running:
            return

        ctx = mp.get_context("spawn")
        self._ctx = ctx
        self.task_queue = ctx.Queue()
        self.result_queue = ctx.Queue(maxsize=max(1, self.num_workers * 2))
        self.progress_queue = ctx.Queue()

        for _ in range(self.num_workers):
            self._spawn_worker()

        self._is_running = True

    def _spawn_worker(self):
        """启动一个工作进程；异常退出或超时后也通过这里补位。"""
        if self._ctx is None or self.task_queue is None:
            return None
        worker = self._ctx.Process(
            target=_baostock_download_worker,
            args=(self.task_queue, self.result_queue, self.progress_queue),
            name=f"BaoStockWorker-{int(time.time() * 1000) % 100000}",
        )
        worker.daemon = True
        worker.start()
        self.workers.append(worker)
        return worker

    def add_task(
        self,
        stock_code: str,
        bs_code: str,
        period: str,
        start_date: str,
        end_date: str,
        *,
        do_front: bool = True,
        do_back: bool = True,
    ) -> int:
        if not self._is_running or self.task_queue is None:
            raise RuntimeError("Importer not started")

        self._task_id += 1
        payload = {
            "stock_code": stock_code,
            "bs_code": bs_code,
            "period": period,
            "start_date": start_date,
            "end_date": end_date,
            "task_id": self._task_id,
            "task_kind": "kline",
            "do_front": bool(do_front),
            "do_back": bool(do_back),
            "request_cost": 1 + int(bool(do_front)) + int(bool(do_back)),
            "attempt": 0,
        }
        self.task_queue.put(payload)
        self._task_meta[self._task_id] = {
            "payload": payload,
            "submitted_at": time.monotonic(),
            "started_at": None,
            "pid": None,
            "stage": "queued",
            "attempt": 0,
            "retry_at": None,
        }
        self.pending_tasks += 1
        return self._task_id

    def add_indicator_task(
        self,
        stock_code: str,
        bs_code: str,
        start_date: str,
        end_date: str,
        indicators: List[str],
    ) -> int:
        """添加单只股票的日线指标任务。"""
        if not self._is_running or self.task_queue is None:
            raise RuntimeError("Importer not started")
        self._task_id += 1
        payload = {
            "stock_code": stock_code,
            "bs_code": bs_code,
            "period": "1d",
            "start_date": start_date,
            "end_date": end_date,
            "task_id": self._task_id,
            "task_kind": "indicator",
            "indicators": list(indicators or []),
            "attempt": 0,
        }
        self.task_queue.put(payload)
        self._task_meta[self._task_id] = {
            "payload": payload,
            "submitted_at": time.monotonic(),
            "started_at": None,
            "pid": None,
            "stage": "queued",
            "attempt": 0,
            "retry_at": None,
        }
        self.pending_tasks += 1
        return self._task_id

    def add_adjustment_task(
        self,
        stock_code: str,
        bs_code: str,
        period: str,
        start_date: str,
        end_date: str,
        *,
        adjustflag: str = "2",
        suffix: str = "front",
    ) -> int:
        """添加只刷新复权价格列的任务；一次任务只消耗一次接口请求。"""
        if not self._is_running or self.task_queue is None:
            raise RuntimeError("Importer not started")
        self._task_id += 1
        payload = {
            "stock_code": stock_code,
            "bs_code": bs_code,
            "period": period,
            "start_date": start_date,
            "end_date": end_date,
            "task_id": self._task_id,
            "task_kind": "adjustment",
            "adjustflag": str(adjustflag),
            "suffix": str(suffix),
            "attempt": 0,
        }
        self.task_queue.put(payload)
        self._task_meta[self._task_id] = {
            "payload": payload,
            "submitted_at": time.monotonic(),
            "started_at": None,
            "pid": None,
            "stage": "queued",
            "attempt": 0,
            "retry_at": None,
        }
        self.pending_tasks += 1
        return self._task_id

    def get_result(self, timeout: float = 0.1) -> Optional[Dict]:
        if not self._is_running or self.result_queue is None:
            return None
        deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
        while self._is_running:
            self._drain_progress_internal()
            self._dispatch_due_retries()
            self._check_worker_health_and_timeouts()
            if self._synthetic_results:
                result = self._synthetic_results.popleft()
                if self._retry_or_discard_result(result):
                    continue
                return self._accept_result(result)

            remaining = max(0.0, deadline - time.monotonic())
            try:
                result = self.result_queue.get(timeout=min(0.05, remaining))
            except Empty:
                if time.monotonic() >= deadline:
                    return None
                continue
            except (EOFError, OSError, ValueError) as exc:
                self._fatal_error = f"结果队列异常: {exc}"
                self._fail_all_pending(self._fatal_error)
                continue

            task_id = int(result.get("task_id", 0) or 0)
            if task_id in self._completed_task_ids:
                continue
            if self._retry_or_discard_result(result):
                continue
            return self._accept_result(result)
        return None

    def _accept_result(self, result: Dict) -> Dict:
        task_id = int(result.get("task_id", 0) or 0)
        if task_id:
            self._completed_task_ids.add(task_id)
            self._task_meta.pop(task_id, None)
        self.completed_tasks += 1
        self.pending_tasks = max(0, self.pending_tasks - 1)
        return result

    def _record_progress(self, event: Dict):
        task_id = int(event.get("task_id", 0) or 0)
        meta = self._task_meta.get(task_id)
        event_type = event.get("type")
        if meta is not None and event_type not in ("status", "fatal"):
            event_attempt = int(event.get("attempt", 0) or 0)
            if event_attempt != int(meta.get("attempt", 0) or 0):
                return False
        if meta is not None:
            if event_type == "started":
                meta["started_at"] = time.monotonic()
                meta["pid"] = event.get("pid")
                meta["stage"] = "running"
            elif event_type == "data_ready":
                meta["stage"] = "data_ready"
                meta["stage_at"] = time.monotonic()
            elif event_type in ("progress", "error"):
                meta["stage"] = "result_sent"
                meta["stage_at"] = time.monotonic()
        if event_type == "fatal":
            self._fatal_error = str(event.get("msg") or "BaoStock 工作进程致命错误")
        return True

    def _retry_delay(self, attempt: int) -> float:
        if not self.retry_backoff:
            return 0.0
        index = min(max(0, attempt - 1), len(self.retry_backoff) - 1)
        return max(0.0, self.retry_backoff[index])

    def _retry_or_discard_result(self, result: Dict) -> bool:
        task_id = int(result.get("task_id", 0) or 0)
        meta = self._task_meta.get(task_id)
        if meta is None:
            return task_id in self._completed_task_ids
        result_attempt = int(result.get("attempt", 0) or 0)
        current_attempt = int(meta.get("attempt", 0) or 0)
        if result_attempt != current_attempt:
            return True
        if result.get("success"):
            return False
        if current_attempt >= self.max_task_retries:
            return False
        if not _is_transient_data_source_error(
            result.get("error"), result.get("error_type")
        ):
            return False

        # “用户未登录”等错误必须销毁旧会话；新 worker 会重新执行 bs.login()。
        pid = result.get("worker_pid") or meta.get("pid")
        if pid:
            self._terminate_worker_pid(int(pid))
        next_attempt = current_attempt + 1
        delay = self._retry_delay(next_attempt)
        payload = dict(meta.get("payload") or {})
        payload["attempt"] = next_attempt
        meta.update(
            {
                "payload": payload,
                "attempt": next_attempt,
                "stage": "retry_wait",
                "retry_at": time.monotonic() + delay,
                "started_at": None,
                "pid": None,
            }
        )
        self._progress_buffer.append(
            {
                "type": "retry",
                "task_id": task_id,
                "stock_code": payload.get("stock_code", ""),
                "period": payload.get("period", ""),
                "task_kind": payload.get("task_kind", "kline"),
                "attempt": next_attempt,
                "max_retries": self.max_task_retries,
                "delay": delay,
                "request_cost": int(
                    payload.get("request_cost")
                    or (1 if payload.get("task_kind") in ("indicator", "adjustment") else 3)
                ),
                "error": str(result.get("error") or result.get("error_type") or "瞬时故障"),
            }
        )
        return True

    def _dispatch_due_retries(self):
        if self.task_queue is None:
            return
        now = time.monotonic()
        for meta in list(self._task_meta.values()):
            if meta.get("stage") != "retry_wait":
                continue
            if now < float(meta.get("retry_at") or 0.0):
                continue
            payload = dict(meta.get("payload") or {})
            try:
                self.task_queue.put_nowait(payload)
            except Exception:
                self.task_queue.put(payload)
            meta.update(
                {
                    "submitted_at": now,
                    "started_at": None,
                    "pid": None,
                    "stage": "queued",
                    "retry_at": None,
                }
            )

    def _drain_progress_internal(self):
        if self.progress_queue is None:
            return
        while True:
            try:
                event = self.progress_queue.get_nowait()
            except Empty:
                break
            except (EOFError, OSError, ValueError):
                break
            if self._record_progress(event):
                self._progress_buffer.append(event)

    def _failure_result(self, task_id: int, error: str, error_type: str) -> Dict:
        meta = self._task_meta.get(task_id) or {}
        payload = meta.get("payload") or {}
        return {
            "success": False,
            "stock_code": payload.get("stock_code", ""),
            "period": payload.get("period", ""),
            "task_id": task_id,
            "error": error,
            "error_type": error_type,
            "attempt": int(meta.get("attempt", 0) or 0),
            "worker_pid": meta.get("pid"),
            "task_kind": payload.get("task_kind", "kline"),
        }

    def _fail_all_pending(self, error: str):
        queued_ids = {
            int(item.get("task_id", 0) or 0)
            for item in self._synthetic_results
        }
        for task_id in list(self._task_meta):
            if task_id in self._completed_task_ids or task_id in queued_ids:
                continue
            self._synthetic_results.append(
                self._failure_result(task_id, error, "worker_fatal")
            )

    def _terminate_worker_pid(self, pid: int):
        for worker in list(self.workers):
            if worker.pid != pid:
                continue
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=1)
            self.workers.remove(worker)
            return

    def _check_worker_health_and_timeouts(self):
        if not self._is_running:
            return
        now = time.monotonic()
        self._dispatch_due_retries()

        timed_out = []
        for task_id, meta in list(self._task_meta.items()):
            started_at = meta.get("started_at")
            effective_timeout = _effective_task_timeout(
                meta.get("payload") or {}, self.timeout_per_task
            )
            if (
                started_at is not None
                and meta.get("stage") == "running"
                and effective_timeout > 0
                and now - started_at > effective_timeout
            ):
                timed_out.append((task_id, meta, effective_timeout))

        queue_waves = max(1, math.ceil(len(self._task_meta) / max(1, self.num_workers)))
        queue_timeout = max(30.0, self.timeout_per_task * (queue_waves + 1))
        queued_lost = []
        for task_id, meta in list(self._task_meta.items()):
            if (
                meta.get("stage") == "queued"
                and now - float(meta.get("submitted_at") or now) > queue_timeout
            ):
                queued_lost.append((task_id, meta))

        for task_id, meta, effective_timeout in timed_out:
            pid = meta.get("pid")
            if pid:
                self._terminate_worker_pid(pid)
            self._synthetic_results.append(
                self._failure_result(
                    task_id,
                    f"任务超时（>{effective_timeout:.0f}秒）",
                    "timeout",
                )
            )
            meta["stage"] = "failed_pending_delivery"

        for task_id, meta in queued_lost:
            self._synthetic_results.append(
                self._failure_result(
                    task_id,
                    f"任务长时间未被工作进程接收（>{queue_timeout:.0f}秒）",
                    "queue_timeout",
                )
            )
            meta["stage"] = "failed_pending_delivery"

        dead_pids = set()
        for worker in list(self.workers):
            if worker.is_alive():
                continue
            dead_pids.add(worker.pid)
            self.workers.remove(worker)
        for task_id, meta in list(self._task_meta.items()):
            if meta.get("pid") in dead_pids and meta.get("stage") not in (
                "data_ready",
                "result_sent",
                "failed_pending_delivery",
            ):
                self._synthetic_results.append(
                    self._failure_result(
                        task_id,
                        f"工作进程异常退出（PID {meta.get('pid')}）",
                        "worker_exit",
                    )
                )
                meta["stage"] = "failed_pending_delivery"

        # 已报「数据已就绪 / 结果已发出」的任务不受运行超时约束。它的进程若在结果
        # 送达前被结束（例如主进程为同一进程上一个任务的网络错误换进程重登，
        # _terminate_worker_pid 已把它移出 self.workers，上面的检查看不到），
        # 结果就丢了，调度线程会一直等下去。进程已不在且超过宽限时间仍没收到
        # 结果，按进程退出重试；原结果若晚到，会因 attempt 不一致被当作过期丢弃。
        live_pids = {worker.pid for worker in self.workers if worker.is_alive()}
        for task_id, meta in list(self._task_meta.items()):
            pid = meta.get("pid")
            since = float(meta.get("stage_at") or meta.get("started_at") or now)
            if (
                meta.get("stage") in ("data_ready", "result_sent")
                and pid
                and pid not in live_pids
                and now - since > _LOST_RESULT_GRACE_SECONDS
            ):
                self._synthetic_results.append(
                    self._failure_result(
                        task_id,
                        f"工作进程在结果送达前退出（PID {pid}）",
                        "worker_exit",
                    )
                )
                meta["stage"] = "failed_pending_delivery"

        if self._fatal_error:
            self._fail_all_pending(self._fatal_error)
            return

        while self._is_running and len(self.workers) < self.num_workers:
            if self._spawn_worker() is None:
                break

    def get_progress(self, timeout: float = 0.01) -> Optional[Dict]:
        if not self._is_running or self.progress_queue is None:
            return None
        self._drain_progress_internal()
        if self._progress_buffer:
            return self._progress_buffer.popleft()
        try:
            event = self.progress_queue.get(timeout=timeout)
            return event if self._record_progress(event) else None
        except Empty:
            return None
        except (EOFError, OSError, ValueError):
            return None

    def get_all_progress(self) -> List[Dict]:
        """一次取走当前积压的进度/重试事件，避免 GUI 只读到其中一条。"""
        progress_list = []
        while True:
            event = self.get_progress(timeout=0.001)
            if event is None:
                break
            progress_list.append(event)
        return progress_list

    def is_done(self) -> bool:
        return self.pending_tasks <= 0

    def stop(self, timeout: float = 5.0):
        if not self._is_running:
            return
        self._is_running = False

        # 尝试发退出信号（非阻塞，避免队列满时卡住）
        for _ in self.workers:
            try:
                self.task_queue.put_nowait(None)
            except Exception:
                pass

        deadline = time.monotonic() + max(0.0, float(timeout))
        for worker in self.workers:
            remaining = max(0.0, deadline - time.monotonic())
            worker.join(timeout=min(0.25, remaining))
        alive = [worker for worker in self.workers if worker.is_alive()]
        for worker in alive:
            worker.terminate()
        terminate_deadline = time.monotonic() + 1.0
        for worker in alive:
            worker.join(timeout=max(0.0, terminate_deadline - time.monotonic()))
            if worker.is_alive() and hasattr(worker, "kill"):
                worker.kill()
                worker.join(timeout=0.2)

        # 关闭队列句柄，避免主进程退出时等待后台 feeder 线程
        for q in [self.task_queue, self.result_queue, self.progress_queue]:
            if q is None:
                continue
            try:
                q.cancel_join_thread()
            except Exception:
                pass
            try:
                q.close()
            except Exception:
                pass

        self.workers.clear()
        self.task_queue = None
        self.result_queue = None
        self.progress_queue = None
        self._ctx = None

    def force_stop(self, timeout: float = 2.0):
        self._is_running = False
        for worker in self.workers:
            if worker.is_alive():
                try:
                    worker.terminate()
                except Exception:
                    pass
        deadline = time.monotonic() + max(0.0, float(timeout))
        for worker in self.workers:
            try:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
                if worker.is_alive() and hasattr(worker, "kill"):
                    worker.kill()
                    worker.join(timeout=0.2)
            except Exception:
                pass
        self.workers.clear()
        for q in [self.task_queue, self.result_queue, self.progress_queue]:
            if q is None:
                continue
            try:
                q.cancel_join_thread()
            except Exception:
                pass
            try:
                q.close()
            except Exception:
                pass
        self.task_queue = None
        self.result_queue = None
        self.progress_queue = None
        self._ctx = None

    @property
    def is_running(self) -> bool:
        return self._is_running

