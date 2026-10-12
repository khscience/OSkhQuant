# -*- coding: utf-8 -*-
from __future__ import annotations

"""
DuckDB数据管理器

管理所有股票的数据存储，提供统一的读写接口
支持自定义数据存储路径
"""

import os
import json
import threading
import weakref
import logging
import time
import queue
import multiprocessing
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Callable, Tuple, Any
from datetime import datetime

import duckdb
import pandas as pd

from .config import DuckDBConfig, resolve_market_dir, resolve_stock_db_path
from .stock_db import StockDB
from .lock_retry import is_duckdb_lock_error


def _inspect_database_periods_process_task(candidate):
    """隔离进程任务：异常转成可序列化结果，避免一个坏库终止整轮核验。"""
    stock_code, db_path, periods_to_check, already_indexed = candidate
    try:
        inspected_code, periods, fingerprint = (
            DuckDBManager._inspect_database_periods(
                stock_code,
                db_path,
                periods_to_check,
            )
        )
        return (
            inspected_code,
            db_path,
            already_indexed,
            periods,
            fingerprint,
            "",
        )
    except Exception as exc:
        return (
            stock_code,
            db_path,
            already_indexed,
            [],
            None,
            f"{type(exc).__name__}: {exc}",
        )


def _is_stock_database_filename(filename: str) -> bool:
    """证券数据库文件名必须是恰好六位 ASCII 数字加 .db。"""
    raw_name = str(filename or "")
    stem, extension = os.path.splitext(raw_name)
    return (
        extension.lower() == ".db"
        and len(stem) == 6
        and stem.isascii()
        and stem.isdigit()
    )


def _is_immutable_data_root(data_root: str) -> bool:
    """Return whether a data root explicitly declares itself immutable.

    Regression snapshots carry a small ``manifest.json`` with
    ``"immutable": true``.  Refusing a writable manager at the lowest shared
    layer prevents GUI import actions, CLI helpers, and future callers from
    accidentally modifying a golden dataset.
    """
    manifest_path = os.path.join(os.path.abspath(data_root), "manifest.json")
    if not os.path.isfile(manifest_path):
        return False
    try:
        with open(manifest_path, "r", encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
    except (OSError, ValueError, TypeError):
        return False
    return manifest.get("immutable") is True


def _close_workers(count: int, requested=None) -> int:
    """close_all 并行关闭的线程数。

    优先用 KHQUANT_DUCKDB_CLOSE_WORKERS；未设置时用调用方传入的 requested，
    都没有则串行。
    """
    configured = os.environ.get("KHQUANT_DUCKDB_CLOSE_WORKERS")
    if configured in (None, ""):
        configured = requested if requested is not None else 1
    try:
        configured = int(configured)
    except (TypeError, ValueError):
        configured = 1
    return max(1, min(8, configured, count))


class DuckDBManager:
    """
    DuckDB数据管理器
    
    管理所有股票的数据存储，提供统一的读写接口
    使用单例模式（可通过reset_instance重置）
    """
    
    _instance = None
    _instances = {}
    _lock = threading.Lock()
    
    def _legacy_new_disabled(cls, *args, **kwargs):
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    @classmethod
    def _legacy_reset_instance_disabled(cls):
        """重置单例实例（用于切换数据目录）"""
        with cls._lock:
            if cls._instance is not None:
                # 先关闭所有连接
                try:
                    cls._instance.close_all()
                except Exception as e:
                    logging.warning(f"关闭连接时出错: {e}")

                # 标记为未初始化
                cls._instance._initialized = False

                # 清除所有属性，确保完全释放
                cls._instance._stock_dbs = {}
                cls._instance._metadata_conn = None

            # 重置单例为 None
            cls._instance = None

            # 强制垃圾回收
            import gc
            gc.collect()
    
    @classmethod
    def _instance_key(cls, data_root=None, config=None, read_only=False):
        if data_root is None and config is not None:
            data_root = getattr(config, "data_root", None)
        if data_root is None:
            data_root = DuckDBConfig().data_root
        return (os.path.abspath(data_root), bool(read_only))

    @classmethod
    def _close_instance_locked(cls, key):
        instance = cls._instances.pop(key, None)
        if instance is None:
            return
        try:
            instance.close_all()
        except Exception as e:
            logging.warning(f"关闭连接时出错: {e}")
        instance._initialized = False
        instance._stock_dbs = {}
        instance._metadata_conn = None

    def __new__(cls, *args, **kwargs):
        data_root = kwargs.get("data_root", args[0] if args else None)
        config = kwargs.get("config", args[1] if len(args) > 1 else None)
        read_only = kwargs.get("read_only", False)
        key = cls._instance_key(data_root, config, read_only)
        if not read_only and _is_immutable_data_root(key[0]):
            raise PermissionError(
                f"数据目录已标记为不可变回归快照，禁止写入: {key[0]}"
            )
        with cls._lock:
            if read_only:
                # 研究、回测及其他浏览任务必须拿到真正的只读连接。
                # 旧逻辑会在同一进程内把 read_only=True 请求复用到已存在的
                # 可写实例上，导致 metadata.db 被长期写锁占用；Codex/CLI 侧
                # 再以只读方式打开时也会报“另一个程序正在使用此文件”。
                # 因此这里不再复用可写实例；如果同进程已有同目录可写实例，
                # 先关闭它，再创建独立只读实例。
                writable_instance = cls._instances.get((key[0], False))
                if writable_instance is not None and getattr(writable_instance, "_protect_writes_from_read_only", False):
                    # 短锁写入任务正在运行时，不允许只读 manager 创建动作误关写 manager。
                    # 只读连接能否打开由 DuckDB 文件锁决定；上层会重试/降级，但不能破坏写任务。
                    pass
                else:
                    cls._close_instance_locked((key[0], False))
            if not read_only:
                # 写入任务启动前释放同进程只读连接，否则 Windows + DuckDB 下
                # 只读连接也会阻止写连接打开。
                cls._close_instance_locked((key[0], True))
            instance = cls._instances.get(key)
            if instance is None:
                instance = super().__new__(cls)
                instance._initialized = False
                instance._instance_key = key
                cls._instances[key] = instance
            cls._instance = instance
            return instance

    @classmethod
    def reset_instance(cls):
        with cls._lock:
            for key in list(cls._instances.keys()):
                cls._close_instance_locked(key)
            cls._instance = None
            import gc
            gc.collect()

    @classmethod
    def close_instances(cls, data_root=None, read_only: Optional[bool] = None):
        """关闭指定目录/读写模式的管理器实例。

        Args:
            data_root: 指定数据目录；为空则匹配所有目录。
            read_only: True 仅关只读实例，False 仅关写实例，None 全部关闭。
        """
        root = os.path.abspath(data_root) if data_root else None
        with cls._lock:
            for key in list(cls._instances.keys()):
                key_root, key_read_only = key
                if root is not None and os.path.abspath(key_root) != root:
                    continue
                if read_only is not None and bool(key_read_only) != bool(read_only):
                    continue
                cls._close_instance_locked(key)
            if cls._instance is not None:
                inst_key = getattr(cls._instance, "_instance_key", None)
                if inst_key not in cls._instances:
                    cls._instance = None
            import gc
            gc.collect()

    @classmethod
    def close_read_only_instances(cls, data_root=None):
        """关闭只读连接实例，用于即将执行写入任务前释放 DuckDB 文件锁。"""
        cls.close_instances(data_root=data_root, read_only=True)

    @classmethod
    def close_writable_instances(cls, data_root=None):
        """关闭可写连接实例，用于回测及其他只读任务前释放 DuckDB 文件锁。"""
        cls.close_instances(data_root=data_root, read_only=False)

    def __init__(self, data_root: str = None, config: DuckDBConfig = None,
                 max_connections: int = 100, read_only: bool = False,
                 defer_metadata_init: bool = False):
        """
        初始化DuckDB管理器
        
        Args:
            data_root: 数据存储根目录，默认 './stock_data'
            config: 配置对象
            max_connections: 最大连接数限制，默认100。回测场景建议100，数据补充场景建议20
            defer_metadata_init: 延迟打开 metadata.db。用于短锁批量写入：先写
                各股票库，收尾时再由 batch_update_metadata() 尝试刷新元数据。
        """
        if self._initialized:
            return
        self.read_only = bool(read_only)
        
        # 加载配置。显式指定目录时必须先读取该目录已有的 config.json；
        # 旧逻辑直接构造默认配置，任何可写 manager 首次打开都会静默覆盖
        # default_dividend_type、auto_create_tables、version 等原有设置。
        if config is not None:
            self.config = config
        elif data_root:
            self.config = DuckDBConfig.load_from_root(os.path.abspath(data_root))
        else:
            self.config = DuckDBConfig()
        
        # 设置数据根目录（优先使用参数）
        if data_root:
            self.data_root = os.path.abspath(data_root)
            self.config.data_root = self.data_root
        else:
            self.data_root = os.path.abspath(self.config.data_root)
        
        # 确保目录存在
        if not self.read_only:
            os.makedirs(self.data_root, exist_ok=True)
            for market in ['SH', 'SZ', 'BJ']:
                resolve_market_dir(self.data_root, market, create=True)
        
        # 保存配置
        if not self.read_only:
            self.config.save()
        
        # 股票数据库连接池
        self._stock_dbs: Dict[str, StockDB] = {}
        self._db_lock = threading.Lock()
        # 同一股票的 DuckDB 连接不能被多个线程同时开启事务。
        # RLock 允许批事务失败后在同一线程内逐周期回退。
        # 只在有调用方持有/等待时保留单股锁。普通 dict 会让每只处理过的
        # 股票永久留下一个 Windows 内核句柄，大股票池长任务会从数百个
        # 线性涨到数千甚至上万个，并让后半程关闭连接越来越慢。
        # WeakValueDictionary 不改变并发语义：活跃调用方持有强引用，所有
        # 同股线程仍会拿到同一把锁；无人使用后条目自动释放。
        self._stock_write_locks = weakref.WeakValueDictionary()
        self._stock_write_locks_guard = threading.Lock()
        # 连接池驱逐不能在“当前股票锁”内同步等待另一只股票锁，否则两个
        # 并发任务可能 A 等 B、B 等 A。忙碌连接先移出缓存，待其股票锁
        # 可用时再非阻塞回收。
        self._deferred_stock_closes: List[Tuple[str, StockDB]] = []
        self._max_connections = max_connections  # 最大连接数限制（降低以减少内存占用和线程竞争）
        self._connection_access_time: Dict[str, float] = {}  # 记录每个连接的最后访问时间
        
        # 元数据库
        self._metadata_lock = threading.RLock()
        self._metadata_conn: Optional[duckdb.DuckDBPyConnection] = None
        self._protect_writes_from_read_only = False
        self._short_lock_guard = threading.RLock()
        self._short_lock_depth = 0
        self._defer_metadata_init = bool(defer_metadata_init)
        if not self._defer_metadata_init:
            self._init_metadata_db()
        
        self._initialized = True
        logging.info(f"DuckDBManager 初始化完成，数据目录: {self.data_root}")
    
    def _ensure_writable(self, operation: str = "write"):
        if getattr(self, "read_only", False):
            raise RuntimeError(f"DuckDBManager is opened read-only; cannot {operation}: {self.data_root}")

    def enable_short_lock_write(self):
        """启用长任务短锁写入保护。

        该模式不改变 save_* 的默认行为；调用方仍需在大批量写入时显式传
        skip_metadata=True，并在任务结束后调用 batch_update_metadata()。
        这里主要做两件事：
        1. 关闭 metadata.db 长连接，避免写任务全过程占用 metadata.db；
        2. 防止同进程只读 manager 被创建时误关当前写 manager。
        """
        self._ensure_writable("enable short-lock write mode")
        with self._short_lock_guard:
            self._short_lock_depth += 1
            self._protect_writes_from_read_only = True
            if self._short_lock_depth == 1:
                try:
                    self.close_metadata_connection()
                except Exception:
                    # 启用操作必须具备强异常安全：调用方收到失败后，不应留下
                    # 半开启的保护层，也不能影响之后任务的嵌套计数。
                    self._short_lock_depth -= 1
                    self._protect_writes_from_read_only = self._short_lock_depth > 0
                    raise
        return self

    def disable_short_lock_write(self):
        """退出一层短锁写入保护，支持多个导入任务嵌套。"""
        with self._short_lock_guard:
            if self._short_lock_depth > 0:
                self._short_lock_depth -= 1
            self._protect_writes_from_read_only = self._short_lock_depth > 0
        return self

    @contextmanager
    def short_lock_write_session(self):
        """有作用域的短锁写入会话，异常和用户停止也会正确退出。"""
        self.enable_short_lock_write()
        try:
            yield self
        finally:
            self.disable_short_lock_write()

    def _get_stock_write_lock(self, stock_code: str) -> threading.RLock:
        """获取进程内的单股写入锁。"""
        key = str(stock_code or '').upper()
        with self._stock_write_locks_guard:
            lock = self._stock_write_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._stock_write_locks[key] = lock
            return lock

    def _try_close_evicted_stock_db(self, stock_code: str, stock_db: StockDB) -> bool:
        """非阻塞关闭被驱逐连接；正在使用则交给后续回收。"""
        stock_lock = self._get_stock_write_lock(stock_code)
        acquired = stock_lock.acquire(blocking=False)
        if not acquired:
            return False
        try:
            stock_db.close(skip_checkpoint=True)
        except Exception as exc:
            logging.warning(f"关闭已驱逐连接失败 {stock_code}: {exc}")
        finally:
            stock_lock.release()
        return True

    def _defer_stock_close(self, stock_code: str, stock_db: StockDB) -> None:
        with self._db_lock:
            self._deferred_stock_closes.append((stock_code, stock_db))

    def _drain_deferred_stock_closes(
        self,
        *,
        blocking: bool = False,
        stock_code: str | None = None,
        exclude_stock_code: str | None = None,
    ) -> int:
        """回收已从连接池移除、但驱逐时仍在使用的旧连接。"""
        with self._db_lock:
            selected = []
            remaining = []
            for item in self._deferred_stock_closes:
                if exclude_stock_code is not None and item[0] == exclude_stock_code:
                    remaining.append(item)
                elif stock_code is None or item[0] == stock_code:
                    selected.append(item)
                else:
                    remaining.append(item)
            self._deferred_stock_closes = remaining

        still_busy = []
        closed = 0
        for deferred_code, deferred_db in selected:
            stock_lock = self._get_stock_write_lock(deferred_code)
            acquired = stock_lock.acquire(blocking=blocking)
            if not acquired:
                still_busy.append((deferred_code, deferred_db))
                continue
            try:
                deferred_db.close(skip_checkpoint=True)
                closed += 1
            except Exception as exc:
                logging.warning(f"关闭延迟回收连接失败 {deferred_code}: {exc}")
            finally:
                stock_lock.release()

        if still_busy:
            with self._db_lock:
                self._deferred_stock_closes.extend(still_busy)
        return closed

    def _metadata_path(self) -> str:
        return os.path.join(self.data_root, 'metadata.db')

    def close_metadata_connection(self):
        """关闭 metadata.db 连接，释放 metadata.db 文件锁。"""
        with self._metadata_lock:
            if self._metadata_conn:
                try:
                    self._metadata_conn.close()
                finally:
                    self._metadata_conn = None

    def _ensure_metadata_conn(self, retries: int = 3, delay: float = 0.2):
        """确保 metadata.db 连接可用；失败时抛出最后一次异常。"""
        with self._metadata_lock:
            if self._metadata_conn is not None:
                return self._metadata_conn
            last_error = None
            for attempt in range(max(1, retries)):
                try:
                    self._init_metadata_db()
                    if self._metadata_conn is not None:
                        return self._metadata_conn
                except Exception as exc:
                    last_error = exc
                    if attempt < retries - 1:
                        time.sleep(delay)
            if last_error:
                raise last_error
            raise RuntimeError(f"无法打开 metadata.db: {self._metadata_path()}")

    def _init_metadata_db(self):
        """初始化元数据库"""
        metadata_path = self._metadata_path()
        if self.read_only and not os.path.exists(metadata_path):
            raise FileNotFoundError(
                f"DuckDB metadata.db does not exist: {metadata_path}. 请先扫描/初始化数据。"
            )
        self._metadata_conn = duckdb.connect(metadata_path, read_only=self.read_only)
        if self.read_only:
            return

        # 创建股票列表表
        self._metadata_conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_list (
                stock_code    VARCHAR PRIMARY KEY,
                stock_name    VARCHAR,
                market        VARCHAR,
                has_1d        BOOLEAN DEFAULT FALSE,
                has_1m        BOOLEAN DEFAULT FALSE,
                has_5m        BOOLEAN DEFAULT FALSE,
                has_tick      BOOLEAN DEFAULT FALSE,
                last_sync     TIMESTAMP,
                update_time   TIMESTAMP
            )
        """)

        # 创建同步日志表（使用序列自增）
        self._metadata_conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS sync_log_seq START 1
        """)

        self._metadata_conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_log (
                id            INTEGER DEFAULT nextval('sync_log_seq'),
                stock_code    VARCHAR,
                period        VARCHAR,
                records_count INTEGER,
                sync_status   VARCHAR,
                error_message VARCHAR,
                sync_time     TIMESTAMP
            )
        """)

    @staticmethod
    def _period_column(period: str) -> str:
        period_col = f"has_{period}"
        if period_col not in {'has_1d', 'has_1m', 'has_5m', 'has_tick'}:
            raise ValueError(f"不支持的数据周期: {period}")
        return period_col

    @staticmethod
    def _parse_metadata_record(record: Any) -> Tuple[str, str, Optional[int], str, Optional[str]]:
        """兼容 (stock, period) / (stock, period, records) / dict 三类批量元数据记录。"""
        if isinstance(record, dict):
            return (
                record.get("stock_code") or record.get("stock"),
                record.get("period"),
                record.get("records") if record.get("records") is not None else record.get("records_count"),
                record.get("status", "success"),
                record.get("error_msg") or record.get("error"),
            )
        if len(record) == 2:
            stock_code, period = record
            return stock_code, period, None, "success", None
        if len(record) == 3:
            stock_code, period, records = record
            return stock_code, period, records, "success", None
        if len(record) >= 5:
            stock_code, period, records, status, error_msg = record[:5]
            return stock_code, period, records, status or "success", error_msg
        raise ValueError(f"无法识别的元数据记录: {record!r}")
    
    def get_stock_db(self, stock_code: str) -> StockDB:
        """获取股票数据库实例（带连接池限制，驱逐close在锁外执行）"""
        to_close = None  # (stock_code, StockDB)，在锁外且持单股锁关闭
        with self._db_lock:
            # 如果连接已存在,更新访问时间并返回
            if stock_code in self._stock_dbs:
                self._connection_access_time[stock_code] = time.time()
                return self._stock_dbs[stock_code]

            # 如果超过最大连接数,从字典中移除最久未使用的连接（锁外关闭）
            if len(self._stock_dbs) >= self._max_connections:
                if self._connection_access_time:
                    oldest_code = min(self._connection_access_time.items(), key=lambda x: x[1])[0]
                    evicted = self._stock_dbs.pop(oldest_code, None)
                    if evicted is not None:
                        to_close = (oldest_code, evicted)
                    self._connection_access_time.pop(oldest_code, None)

            # 创建新连接
            self._stock_dbs[stock_code] = StockDB(stock_code, self.data_root, read_only=self.read_only)
            self._connection_access_time[stock_code] = time.time()

            result = self._stock_dbs[stock_code]

        # 在锁外尝试关闭被驱逐连接。这里可能仍位于调用方持有的“当前
        # 股票锁”内，绝不能阻塞等待另一只股票锁，否则会形成交叉死锁。
        if to_close is not None:
            evicted_code, evicted_db = to_close
            if not self._try_close_evicted_stock_db(evicted_code, evicted_db):
                self._defer_stock_close(evicted_code, evicted_db)

        # 顺手回收此前已经结束使用的连接；全程非阻塞。
        # 调用方通常正持有 stock_code 的 RLock。若该连接刚被另一线程驱逐，
        # 本线程可重入取得同一锁，但此时关闭会让即将返回的 result 失效，
        # 因此本次明确排除当前股票。
        self._drain_deferred_stock_closes(
            blocking=False,
            exclude_stock_code=stock_code,
        )

        return result
    
    # ============ 数据写入接口 ============

    def save_kline_data(
        self,
        df: pd.DataFrame,
        stock_code: str,
        period: str,
        dividend_type: str = 'none',
        skip_metadata: bool = False,
        overwrite: bool = True,
        merge_missing: bool = False,
        append_missing_only: bool = False,
        overwrite_trade_dates: bool = False,
        invalidate_adjustment_columns: Optional[List[str]] = None,
        invalidate_adjustment_columns_full_history: Optional[List[str]] = None,
        verified_short_trade_dates: Optional[dict] = None,
    ) -> int:
        """
        保存K线数据

        Args:
            df: K线数据；volume 统一为手、amount 统一为元，数据源适配器负责换算。
            stock_code: 股票代码
            period: 周期类型 ('1d', '1m', '5m', 'tick')
            dividend_type: 复权类型
            skip_metadata: 批量导入时跳过逐条元数据更新
            overwrite: True=覆盖；False=增量补充（仅补缺失时间）
            merge_missing: 增量时同时填充已有行中的 NULL 字段，不改已有非空值
            append_missing_only: 增量时只插入缺失时间键，不扫描更新已有行
            overwrite_trade_dates: 覆写分钟线时按输入交易日整日清理多余时间戳
            invalidate_adjustment_columns: 与 raw 写入同事务置空的旧复权派生列
            invalidate_adjustment_columns_full_history: 与 raw 写入同事务置空
                该证券/周期全部历史行的指定复权派生列

        Returns:
            写入记录数
        """
        self._ensure_writable("save kline data")
        stock_lock = self._get_stock_write_lock(stock_code)
        with stock_lock:
            stock_db = self.get_stock_db(stock_code)

            # 根据周期类型调用不同的保存方法
            if period == 'tick':
                tick_kwargs = {}
                if append_missing_only:
                    tick_kwargs['append_missing_only'] = True
                records = stock_db.save_tick(df, **tick_kwargs)
            else:
                save_kwargs = {'overwrite': overwrite}
                # 保持第三方扩展/旧测试替身的 save_kline 调用签名兼容；只有
                # 新增量链路明确启用时才下传新参数。
                if merge_missing:
                    save_kwargs['merge_missing'] = True
                if append_missing_only:
                    save_kwargs['append_missing_only'] = True
                if overwrite_trade_dates:
                    save_kwargs['overwrite_trade_dates'] = True
                if invalidate_adjustment_columns:
                    save_kwargs['invalidate_adjustment_columns'] = list(
                        invalidate_adjustment_columns
                    )
                if invalidate_adjustment_columns_full_history:
                    save_kwargs[
                        'invalidate_adjustment_columns_full_history'
                    ] = list(invalidate_adjustment_columns_full_history)
                # 整日覆写时调用方证明合法缺根的交易日 {交易日: 根数}；只在
                # 提供时下传，保持旧替身/扩展的 save_kline 签名兼容。
                if verified_short_trade_dates:
                    save_kwargs['verified_short_trade_dates'] = dict(
                        verified_short_trade_dates
                    )
                records = stock_db.save_kline(
                    df, period, dividend_type, **save_kwargs
                )

        # 更新元数据（批量导入时跳过，结束后统一扫描更新）
        if records > 0 and not skip_metadata:
            self._update_stock_metadata(stock_code, period)
            self._log_sync(stock_code, period, records, 'success')

        return records

    def save_tick_data(
        self,
        df: pd.DataFrame,
        stock_code: str,
        skip_metadata: bool = False,
        append_missing_only: bool = False,
    ) -> int:
        """
        保存Tick数据

        Args:
            df: Tick数据
            stock_code: 股票代码
            skip_metadata: 批量导入时跳过逐条元数据更新
            append_missing_only: 只追加缺失时间戳，保留已有 Tick

        Returns:
            写入记录数
        """
        self._ensure_writable("save tick data")
        return self.save_kline_data(
            df,
            stock_code,
            'tick',
            skip_metadata=skip_metadata,
            append_missing_only=append_missing_only,
        )

    def save_stock_period_batch(
        self,
        stock_code: str,
        period_frames: List[tuple],
        overwrite: bool = True,
        skip_unchanged: bool = False,
        timings: dict | None = None,
    ) -> tuple:
        """在一个事务中保存同一股票的多个周期，失败时逐周期回退。

        本接口只负责股票库；调用方应在整批结束后统一更新 metadata.db。
        ``period_frames`` 兼容原有 ``(period, df)``，也接受
        ``(period, df, options)``。options 可逐周期指定 overwrite、
        merge_missing、append_missing_only、overwrite_trade_dates 以及复权
        失效列。``skip_unchanged`` 仅在调用方明确启用时生效：业务字段完全
        一致的数据仍按成功处理并返回记录数，但不会重复物理写入。
        返回 ``(results, used_fallback)``，其中每项结果包含 period、saved、error。
        """
        self._ensure_writable("save stock period batch")
        entries = []
        for entry in period_frames:
            if len(entry) == 2:
                period, df = entry
                options = {}
            elif len(entry) == 3:
                period, df, options = entry
                options = dict(options or {})
            else:
                raise ValueError(
                    "period_frames 每项必须是 (period, df) 或 "
                    "(period, df, options)"
                )
            entries.append((str(period), df, options))
        if not entries:
            return [], False

        stock_lock = self._get_stock_write_lock(stock_code)
        with stock_lock:
            stock_db = None
            batch_results = []
            batch_error = None
            try:
                open_started = time.perf_counter()
                stock_db = self.get_stock_db(stock_code)
                stock_db.conn.execute("BEGIN TRANSACTION")
                sql_started = time.perf_counter()
                if timings is not None:
                    timings['open_s'] = timings.get('open_s', 0.0) + sql_started - open_started
                for period, df, options in entries:
                    if period == 'tick':
                        tick_kwargs = {'manage_transaction': False}
                        if skip_unchanged or options.get('skip_unchanged'):
                            tick_kwargs['skip_unchanged'] = True
                        if options.get('append_missing_only'):
                            tick_kwargs['append_missing_only'] = True
                        saved = stock_db.save_tick(df, **tick_kwargs)
                    else:
                        kline_kwargs = {
                            'overwrite': bool(options.get('overwrite', overwrite)),
                            'manage_transaction': False,
                        }
                        for name in (
                            'merge_missing',
                            'append_missing_only',
                            'overwrite_trade_dates',
                            'verified_short_trade_dates',
                            'invalidate_adjustment_columns',
                            'invalidate_adjustment_columns_full_history',
                        ):
                            if name in options:
                                kline_kwargs[name] = options[name]
                        if skip_unchanged or options.get('skip_unchanged'):
                            kline_kwargs['skip_unchanged'] = True
                        saved = stock_db.save_kline(df, period, **kline_kwargs)
                    batch_results.append({
                        'period': period,
                        'saved': int(saved or 0),
                        'error': None,
                    })
                sql_finished = time.perf_counter()
                if timings is not None:
                    timings['sql_s'] = timings.get('sql_s', 0.0) + sql_finished - sql_started
                stock_db.conn.execute("COMMIT")
                if timings is not None:
                    timings['commit_s'] = timings.get('commit_s', 0.0) + time.perf_counter() - sql_finished
                return batch_results, False
            except Exception as batch_exc:
                batch_error = batch_exc
                if stock_db is not None:
                    rollback_failed = False
                    try:
                        stock_db.conn.execute("ROLLBACK")
                    except Exception:
                        rollback_failed = True
                    if rollback_failed:
                        # 事务状态未知时不能继续复用连接；从缓存摘除后让回退路径
                        # 重新建立干净连接。close 失败也不会把坏连接放回缓存。
                        try:
                            self.close_stock_connection(
                                stock_code, skip_checkpoint=True
                            )
                        except Exception:
                            pass
                        stock_db = None
                    else:
                        stock_db.invalidate_period_table_cache(
                            [period for period, _df, _options in entries]
                        )

            # DuckDB 1.4.x 没有 SAVEPOINT。批事务失败后恢复原有逐周期事务，
            # 从而保证坏周期不会阻止其他健康周期写入。
            fallback_results = []
            for period, df, options in entries:
                try:
                    if period == 'tick':
                        saved = self.save_tick_data(
                            df,
                            stock_code,
                            skip_metadata=True,
                            append_missing_only=bool(
                                options.get('append_missing_only')
                            ),
                        )
                    else:
                        saved = self.save_kline_data(
                            df,
                            stock_code,
                            period,
                            skip_metadata=True,
                            overwrite=bool(options.get('overwrite', overwrite)),
                            merge_missing=bool(options.get('merge_missing')),
                            append_missing_only=bool(
                                options.get('append_missing_only')
                            ),
                            overwrite_trade_dates=bool(
                                options.get('overwrite_trade_dates')
                            ),
                            verified_short_trade_dates=options.get('verified_short_trade_dates'),
                            invalidate_adjustment_columns=options.get(
                                'invalidate_adjustment_columns'
                            ),
                            invalidate_adjustment_columns_full_history=options.get(
                                'invalidate_adjustment_columns_full_history'
                            ),
                        )
                    fallback_results.append({
                        'period': period,
                        'saved': int(saved or 0),
                        'error': None,
                    })
                except Exception as exc:
                    fallback_results.append({
                        'period': period,
                        'saved': 0,
                        'error': str(exc),
                        'error_code': getattr(exc, 'code', type(exc).__name__),
                    })

            failed_fallbacks = [
                f"{item['period']}: {item['error']}"
                for item in fallback_results
                if item.get('error')
            ]
            if failed_fallbacks:
                logging.warning(
                    "%s 多周期批事务失败，且逐周期回退仍有失败；"
                    "批事务错误=%s；回退失败=%s",
                    stock_code,
                    batch_error,
                    "; ".join(failed_fallbacks),
                )
            return fallback_results, True

    def update_daily_indicators(
        self,
        df: pd.DataFrame,
        stock_code: str,
        columns: Optional[List[str]] = None,
    ) -> int:
        """按日期更新已有日线指标，不新增或覆盖行情行。"""
        self._ensure_writable("update daily indicators")
        with self._get_stock_write_lock(stock_code):
            stock_db = self.get_stock_db(stock_code)
            return stock_db.update_daily_indicators(df, columns=columns)

    def update_kline_columns(
        self,
        df: pd.DataFrame,
        stock_code: str,
        period: str,
        columns: List[str],
    ) -> int:
        """按时间（或日线交易日期）更新指定行情列，保留其它字段。"""

        self._ensure_writable("update kline columns")
        with self._get_stock_write_lock(stock_code):
            stock_db = self.get_stock_db(stock_code)
            return stock_db.update_kline_columns(df, period, columns)

    def clear_kline_columns(
        self,
        stock_code: str,
        period: str,
        columns: List[str],
        start_time: str = None,
        end_time: str = None,
    ) -> int:
        """在单股票写锁内把指定派生 K 线列事务化置为 NULL。"""

        self._ensure_writable("clear kline columns")
        with self._get_stock_write_lock(stock_code):
            stock_db = self.get_stock_db(stock_code)
            return stock_db.clear_kline_columns(
                period,
                columns,
                start_time=start_time,
                end_time=end_time,
            )

    def normalize_daily_timestamps(self, stock_code: str) -> int:
        """把单股票旧日线的 00:00/重复时间统一迁移为 09:30。"""

        self._ensure_writable("normalize daily timestamps")
        with self._get_stock_write_lock(stock_code):
            stock_db = self.get_stock_db(stock_code)
            return stock_db.normalize_daily_timestamps()

    def batch_save_kline(
        self,
        data_dict: Dict[str, pd.DataFrame],
        period: str,
        dividend_type: str = 'none',
        progress_callback: Callable[[int], None] = None
    ) -> Dict[str, int]:
        """
        批量保存多只股票数据

        Args:
            data_dict: {stock_code: DataFrame}
            period: 周期类型
            dividend_type: 复权类型
            progress_callback: 进度回调函数，参数为百分比

        Returns:
            {stock_code: records_count}，-1表示失败
        """
        self._ensure_writable("batch save kline data")
        results = {}
        total = len(data_dict)

        for i, (stock_code, df) in enumerate(data_dict.items()):
            try:
                records = self.save_kline_data(df, stock_code, period, dividend_type)
                results[stock_code] = records

                # 释放DataFrame内存
                del df
            except Exception as e:
                logging.error(f"保存失败 {stock_code}: {e}")
                results[stock_code] = -1
                self._log_sync(stock_code, period, 0, 'failed', str(e))

            if progress_callback:
                progress_callback(int((i + 1) / total * 100))

            # v3.1.6: 减少清理频率，从每5个改为每20个
            # 每处理20只股票后，执行清理
            if (i + 1) % 20 == 0:
                # 关闭不活跃的连接
                self._cleanup_idle_connections(keep_recent=2)
                import gc
                gc.collect()

        return results

    # ============ 数据读取接口 ============

    def get_kline_data_range(
        self, stock_code: str, period: str
    ) -> Tuple[Optional[datetime], Optional[datetime]]:
        """返回本地某证券/周期的真实最早、最晚时间。

        复权价格的基准可能随新除权日变化；导入器据此把刷新范围扩展到库内
        全部历史，避免只刷新用户本次区间后留下两套前复权基准。
        """

        with self._get_stock_write_lock(stock_code):
            stock_db = self.get_stock_db(stock_code)
            return stock_db.get_data_range(period)

    def get_kline_data(
        self,
        stock_code: str,
        period: str,
        start_time: str = None,
        end_time: str = None,
        dividend_type: str = None,
        fields: List[str] = None
    ) -> pd.DataFrame:
        """读取单只股票K线数据"""
        with self._get_stock_write_lock(stock_code):
            stock_db = self.get_stock_db(stock_code)
            if period == 'tick':
                return stock_db.get_tick(start_time, end_time, fields)
            return stock_db.get_kline(period, start_time, end_time, dividend_type, fields)

    def get_kline_data_epoch_seconds(
        self,
        stock_code: str,
        period: str,
        start_time: str = None,
        end_time: str = None,
        dividend_type: str = None,
        fields: List[str] = None,
        tz_offset_seconds: int = 0
    ) -> pd.DataFrame:
        """Read K-line data with framework-local epoch seconds in the ``time`` column."""
        with self._get_stock_write_lock(stock_code):
            stock_db = self.get_stock_db(stock_code)
            if period == 'tick':
                return stock_db.get_tick(start_time, end_time, fields)
            return stock_db.get_kline_epoch_seconds(
                period,
                start_time,
                end_time,
                dividend_type,
                fields,
                tz_offset_seconds=tz_offset_seconds,
            )

    def get_tick_data(
        self,
        stock_code: str,
        start_time: str = None,
        end_time: str = None,
        fields: List[str] = None
    ) -> pd.DataFrame:
        """读取单只股票Tick数据"""
        with self._get_stock_write_lock(stock_code):
            stock_db = self.get_stock_db(stock_code)
            return stock_db.get_tick(start_time, end_time, fields)

    def get_kline_data_batch(
        self,
        stock_codes: List[str],
        period: str,
        start_time: str = None,
        end_time: str = None,
        dividend_type: str = None,
        fields: List[str] = None,
        workers: int = 1
    ) -> Dict[str, pd.DataFrame]:
        """批量读取多只股票数据"""
        try:
            workers = int(workers or 1)
        except Exception:
            workers = 1
        workers = max(1, min(workers, len(stock_codes) if stock_codes else 1))

        if workers > 1 and len(stock_codes) > 1:
            results = {}
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_code = {
                    executor.submit(
                        self.get_kline_data,
                        code,
                        period,
                        start_time,
                        end_time,
                        dividend_type,
                        fields,
                    ): code
                    for code in stock_codes
                }
                for future in as_completed(future_to_code):
                    code = future_to_code[future]
                    try:
                        results[code] = future.result()
                    except Exception as exc:
                        if is_duckdb_lock_error(exc):
                            raise
                        logging.warning(f"并行读取股票数据失败 {code}: {exc}")
                        results[code] = pd.DataFrame()
            return {code: results.get(code, pd.DataFrame()) for code in stock_codes}

        results = {}
        for code in stock_codes:
            results[code] = self.get_kline_data(
                code, period, start_time, end_time, dividend_type, fields
            )
        return results

    def get_kline_data_batch_epoch_seconds(
        self,
        stock_codes: List[str],
        period: str,
        start_time: str = None,
        end_time: str = None,
        dividend_type: str = None,
        fields: List[str] = None,
        workers: int = 1,
        tz_offset_seconds: int = 0,
    ) -> Dict[str, pd.DataFrame]:
        """Batch read K-line data with framework-local epoch seconds in ``time``."""
        try:
            workers = int(workers or 1)
        except Exception:
            workers = 1
        workers = max(1, min(workers, len(stock_codes) if stock_codes else 1))

        if workers > 1 and len(stock_codes) > 1:
            results = {}
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_code = {
                    executor.submit(
                        self.get_kline_data_epoch_seconds,
                        code,
                        period,
                        start_time,
                        end_time,
                        dividend_type,
                        fields,
                        tz_offset_seconds,
                    ): code
                    for code in stock_codes
                }
                for future in as_completed(future_to_code):
                    code = future_to_code[future]
                    try:
                        results[code] = future.result()
                    except Exception as exc:
                        if is_duckdb_lock_error(exc):
                            raise
                        logging.warning(f"并行读取股票数据失败 {code}: {exc}")
                        results[code] = pd.DataFrame()
            return {code: results.get(code, pd.DataFrame()) for code in stock_codes}

        results = {}
        for code in stock_codes:
            results[code] = self.get_kline_data_epoch_seconds(
                code,
                period,
                start_time,
                end_time,
                dividend_type,
                fields,
                tz_offset_seconds,
            )
        return results

    # ============ 元数据管理 ============

    def _update_stock_metadata(self, stock_code: str, period: str):
        """更新股票元数据 - v3.1.6 性能优化版本"""
        # 解析市场
        if getattr(self, "read_only", False):
            return
        if '.' in stock_code:
            _, market = stock_code.split('.')
        else:
            market = 'SZ'

        period_col = self._period_column(period)
        now = datetime.now()

        try:
            with self._metadata_lock:
                self._ensure_metadata_conn()
                self._upsert_stock_metadata(stock_code, market, period_col, now)

        except Exception as e:
            logging.warning(f"更新元数据失败 {stock_code} {period}: {e}")

    def _upsert_stock_metadata(self, stock_code: str, market: str, period_col: str, now: datetime):
        """保留已有 stock_list 字段的幂等更新，避免 INSERT OR REPLACE 清空其它周期标记。"""
        self._metadata_conn.execute("""
            INSERT INTO stock_list (stock_code, stock_name, market, last_sync, update_time)
            VALUES (?, '', ?, ?, ?)
            ON CONFLICT(stock_code) DO NOTHING
        """, [stock_code, market, now, now])
        self._metadata_conn.execute(f"""
            UPDATE stock_list
            SET {period_col} = TRUE,
                market = COALESCE(NULLIF(market, ''), ?),
                last_sync = ?,
                update_time = ?
            WHERE stock_code = ?
        """, [market, now, now, stock_code])

    def _get_stock_name(self, stock_code: str) -> str:
        """获取股票名称"""
        try:
            # 优先从 khQTTools 获取（从本地股票列表文件）
            import sys
            import os
            parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if parent_dir not in sys.path:
                sys.path.insert(0, parent_dir)
            import khQTTools
            name = khQTTools.get_stock_name(stock_code)
            if name:
                return name
        except Exception:
            pass

        return ''

    def batch_update_metadata(self, stock_period_pairs: list):
        """批量更新元数据 - 用于导入结束后统一刷新（事务批量写入）
        Args:
            stock_period_pairs: [(stock_code, period), ...] 或
                [(stock_code, period, records), ...] 或 dict 列表
        """
        self._ensure_writable("batch update metadata")
        if not stock_period_pairs:
            return 0
        with self._metadata_lock:
            self._ensure_metadata_conn()
            updated = 0
            self._metadata_conn.execute("BEGIN TRANSACTION")
            try:
                for raw_record in stock_period_pairs:
                    stock_code, period, records, status, error_msg = self._parse_metadata_record(raw_record)
                    if not stock_code or not period:
                        raise ValueError(f"元数据记录缺少 stock_code 或 period: {raw_record!r}")
                    if '.' in stock_code:
                        _, market = stock_code.split('.')
                    else:
                        market = 'SZ'
                    period_col = self._period_column(period)
                    now = datetime.now()
                    if status == "success":
                        self._upsert_stock_metadata(stock_code, market, period_col, now)
                        updated += 1
                    if records is not None:
                        self._log_sync(stock_code, period, int(records or 0), status, error_msg)
                self._metadata_conn.execute("COMMIT")
                return updated
            except Exception:
                try:
                    self._metadata_conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                if getattr(self, "_protect_writes_from_read_only", False):
                    self.close_metadata_connection()

    def _log_sync(self, stock_code: str, period: str, records: int,
                  status: str, error_msg: str = None):
        """记录同步日志"""
        if getattr(self, "read_only", False):
            return
        try:
            with self._metadata_lock:
                self._ensure_metadata_conn()
                now = datetime.now()
                self._metadata_conn.execute("""
                    INSERT INTO sync_log (stock_code, period, records_count, sync_status, error_message, sync_time)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, [stock_code, period, records, status, error_msg, now])
        except Exception as e:
            logging.warning(f"记录同步日志失败: {e}")

    def get_available_stocks(self, period: str = None, market: str = None) -> List[str]:
        """
        获取有数据的股票列表

        Args:
            period: 周期筛选
            market: 市场筛选 ('SH', 'SZ', 'BJ')
        """
        with self._metadata_lock:
            if self._metadata_conn is None:
                self._ensure_metadata_conn()
                if self._metadata_conn is None:
                    logging.warning("元数据库连接无效，无法获取股票列表")
                    return []
            sql = "SELECT stock_code FROM stock_list WHERE 1=1"
            params = []

            if period:
                period_col = f"has_{period}"
                sql += f" AND {period_col} = TRUE"

            if market:
                sql += " AND market = ?"
                params.append(market)

            sql += " ORDER BY stock_code"

            result = self._metadata_conn.execute(sql, params).fetchall()
            return [r[0] for r in result]

    def get_stock_info(self, stock_code: str) -> dict:
        """获取股票信息"""
        try:
            with self._metadata_lock:
                if self._metadata_conn is None:
                    self._ensure_metadata_conn()
                result = self._metadata_conn.execute("""
                    SELECT * FROM stock_list WHERE stock_code = ?
                """, [stock_code]).fetchdf()

            if len(result) > 0:
                return result.iloc[0].to_dict()
        except:
            pass
        return {}

    def delete_stock_metadata(self, stock_code: str):
        """删除股票的元数据记录

        Args:
            stock_code: 股票代码
        """
        self._ensure_writable("delete stock metadata")
        try:
            with self._metadata_lock:
                self._ensure_metadata_conn()
                if self._metadata_conn is None:
                    logging.warning(f"元数据库连接无效，无法删除 {stock_code}")
                    return
                self._metadata_conn.execute("BEGIN TRANSACTION")
                try:
                    self._metadata_conn.execute("""
                        DELETE FROM stock_list WHERE stock_code = ?
                    """, [stock_code])
                    self._metadata_conn.execute("""
                        DELETE FROM sync_log WHERE stock_code = ?
                    """, [stock_code])
                    self._metadata_conn.execute("COMMIT")
                except Exception:
                    self._metadata_conn.execute("ROLLBACK")
                    raise

            logging.info(f"已删除 {stock_code} 的元数据")

        except Exception as e:
            logging.error(f"删除元数据失败 {stock_code}: {e}")
            import traceback
            traceback.print_exc()

    # ============ 统计与维护 ============

    def _physical_database_inventory(self) -> Dict[str, Any]:
        """快速枚举市场目录；不打开任何 DuckDB 文件。"""
        inventory = {
            "stock_paths": {},
            "non_stock_paths": [],
            "markets": {},
            "total_database_files": 0,
            "total_size_bytes": 0,
        }
        for market in ("SH", "SZ", "BJ"):
            market_dir = resolve_market_dir(self.data_root, market)
            market_info = {
                "stocks": 0,
                "database_files": 0,
                "non_stock_files": 0,
                "size_bytes": 0,
            }
            if os.path.isdir(market_dir):
                for filename in os.listdir(market_dir):
                    if not str(filename).lower().endswith(".db"):
                        continue
                    db_path = os.path.join(market_dir, filename)
                    if not os.path.isfile(db_path):
                        continue
                    try:
                        file_size = os.path.getsize(db_path)
                    except OSError:
                        file_size = 0
                    market_info["database_files"] += 1
                    market_info["size_bytes"] += file_size
                    inventory["total_database_files"] += 1
                    inventory["total_size_bytes"] += file_size
                    if _is_stock_database_filename(filename):
                        stock_code = f"{os.path.splitext(filename)[0].upper()}.{market}"
                        inventory["stock_paths"][stock_code] = db_path
                        market_info["stocks"] += 1
                    else:
                        inventory["non_stock_paths"].append(db_path)
                        market_info["non_stock_files"] += 1
            inventory["markets"][market] = market_info
        return inventory

    def _indexed_stock_periods(self) -> Dict[str, set]:
        """返回元数据中每只规范证券已标记为可用的周期集合。"""
        indexed_periods: Dict[str, set] = {}
        with self._metadata_lock:
            self._ensure_metadata_conn()
            metadata_rows = self._metadata_conn.execute("""
                SELECT
                    stock_code,
                    UPPER(COALESCE(market, '')) AS market,
                    COALESCE(has_1d, FALSE),
                    COALESCE(has_1m, FALSE),
                    COALESCE(has_5m, FALSE),
                    COALESCE(has_tick, FALSE)
                FROM stock_list
            """).fetchall()
        for stock_code, market, has_1d, has_1m, has_5m, has_tick in metadata_rows:
            raw_code = str(stock_code or "").strip().upper()
            normalized_market = str(market or "").strip().upper()
            if "." in raw_code:
                raw_code, suffix = raw_code.rsplit(".", 1)
                normalized_market = suffix or normalized_market
            if (
                len(raw_code) == 6
                and raw_code.isascii()
                and raw_code.isdigit()
                and normalized_market in {"SH", "SZ", "BJ"}
            ):
                normalized_code = f"{raw_code}.{normalized_market}"
                periods = indexed_periods.setdefault(normalized_code, set())
                for period, enabled in (
                    ("1d", has_1d),
                    ("1m", has_1m),
                    ("5m", has_5m),
                    ("tick", has_tick),
                ):
                    if bool(enabled):
                        periods.add(period)
        return indexed_periods

    def _indexed_stock_keys(self) -> set:
        """兼容旧调用方：返回存在于 stock_list 的规范证券代码。"""
        return set(self._indexed_stock_periods())

    def get_statistics(self) -> Dict:
        """获取快速统计；未打开的候选文件只称“待核验”，不等同于漏数据。"""
        inventory = self._physical_database_inventory()
        physical_stocks = set(inventory["stock_paths"])
        stats = {
            "data_root": self.data_root,
            "total_stocks": len(physical_stocks),
            "total_database_files": inventory["total_database_files"],
            "indexed_stocks": 0,
            "unindexed_database_files": 0,
            "unverified_unindexed_database_files": 0,
            "non_stock_database_files": len(inventory["non_stock_paths"]),
            "metadata_only_stocks": 0,
            "metadata_available": False,
            "metadata_error": "",
            "total_size_mb": round(inventory["total_size_bytes"] / (1024 * 1024), 2),
            "markets": {},
            "by_period": {"1d": 0, "1m": 0, "5m": 0, "tick": 0},
        }
        for market, raw_info in inventory["markets"].items():
            stats["markets"][market] = {
                "stocks": raw_info["stocks"],
                "database_files": raw_info["database_files"],
                "non_stock_files": raw_info["non_stock_files"],
                "indexed_stocks": 0,
                "size_mb": round(raw_info["size_bytes"] / (1024 * 1024), 2),
            }

        indexed_stocks = set()
        indexed_periods: Dict[str, set] = {}
        try:
            indexed_periods = self._indexed_stock_periods()
            indexed_stocks = set(indexed_periods)
            stats["indexed_stocks"] = len(indexed_stocks)
            for stock_code in indexed_stocks:
                market = stock_code.rsplit(".", 1)[1]
                stats["markets"][market]["indexed_stocks"] += 1
            with self._metadata_lock:
                self._ensure_metadata_conn()
                for period in ("1d", "1m", "5m", "tick"):
                    count = self._metadata_conn.execute(f"""
                        SELECT COUNT(*) FROM stock_list WHERE has_{period} = TRUE
                    """).fetchone()[0]
                    stats["by_period"][period] = count
            stats["metadata_available"] = True
        except Exception as exc:
            stats["metadata_error"] = str(exc)
            logging.warning(f"统计元数据股票数失败: {exc}")

        # 这里只做文件名与索引集合比较。候选可能是空壳库，必须经过下面的
        # audit_unindexed_databases() 只读核验后，才能判断是否真的漏索引。
        unverified = physical_stocks - indexed_stocks
        stats["unindexed_database_files"] = len(unverified)  # 兼容旧调用方
        stats["unverified_unindexed_database_files"] = len(unverified)
        stats["period_flag_candidate_files"] = sum(
            1
            for stock_code in physical_stocks
            if stock_code in indexed_periods
            and len(indexed_periods.get(stock_code, set())) < 4
        )
        stats["metadata_only_stocks"] = len(indexed_stocks - physical_stocks)
        return stats

    @staticmethod
    def _inspect_database_periods(
        stock_code: str,
        db_path: str,
        periods_to_check: Optional[Tuple[str, ...]] = None,
    ) -> Tuple[str, List[str], Dict[str, Any]]:
        """只读判断固定行情表是否含可用数据；不会创建表或修改文件。"""
        before_stat = os.stat(db_path)
        connection = duckdb.connect(db_path, read_only=True)
        try:
            table_names = {
                row[0]
                for row in connection.execute("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'main'
                """).fetchall()
            }
            columns_by_table: Dict[str, set] = {}
            for table_name, column_name in connection.execute("""
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'main'
            """).fetchall():
                columns_by_table.setdefault(str(table_name), set()).add(
                    str(column_name).lower()
                )
            periods = []
            allowed_periods = set(periods_to_check or ("1d", "1m", "5m", "tick"))
            for period, table_name in (
                ("1d", "kline_1d"),
                ("1m", "kline_1m"),
                ("5m", "kline_5m"),
                ("tick", "tick"),
            ):
                if period not in allowed_periods:
                    continue
                if table_name not in table_names:
                    continue
                has_rows = connection.execute(
                    f"SELECT EXISTS(SELECT 1 FROM {table_name} LIMIT 1)"
                ).fetchone()[0]
                if has_rows:
                    required_columns = (
                        {"time", "lastprice", "volume"}
                        if period == "tick"
                        else {"time", "open", "high", "low", "close", "volume"}
                    )
                    missing_columns = sorted(
                        required_columns - columns_by_table.get(table_name, set())
                    )
                    if missing_columns:
                        raise RuntimeError(
                            f"{table_name} 有数据但缺少必要字段: "
                            + ", ".join(missing_columns)
                        )
                    periods.append(period)
        finally:
            connection.close()
        after_stat = os.stat(db_path)
        before_fingerprint = (before_stat.st_size, before_stat.st_mtime_ns)
        after_fingerprint = (after_stat.st_size, after_stat.st_mtime_ns)
        if before_fingerprint != after_fingerprint:
            raise RuntimeError("数据库在核验期间发生变化，请稍后重新核验")
        fingerprint = {
            "path": os.path.abspath(db_path),
            "size": int(after_stat.st_size),
            "mtime_ns": int(after_stat.st_mtime_ns),
        }
        return stock_code, periods, fingerprint

    def audit_unindexed_databases(
        self,
        progress_callback: Callable = None,
        should_stop: Callable = None,
        max_workers: int = 8,
        process_isolation: bool = False,
    ) -> Dict[str, Any]:
        """只读核验整库索引和缺失周期标志，返回可供显式提交的摘要。"""
        inventory = self._physical_database_inventory()
        try:
            indexed_periods = self._indexed_stock_periods()
        finally:
            # 下面可能要核验数千个证券库，期间不再依赖 metadata.db。
            # Windows 下即使只读连接也会阻止其他进程取得写锁，因此索引
            # 快照读取完就立即释放，避免和定时补充/数据导入长时间互锁。
            self.close_metadata_connection()
        all_periods = frozenset(("1d", "1m", "5m", "tick"))
        candidates = sorted(
            (
                (
                    stock_code,
                    db_path,
                    tuple(sorted(all_periods - indexed_periods.get(stock_code, set()))),
                    stock_code in indexed_periods,
                )
                for stock_code, db_path in inventory["stock_paths"].items()
                if len(indexed_periods.get(stock_code, set())) < len(all_periods)
            ),
            key=lambda item: item[0],
        )
        non_stock_paths = sorted(inventory["non_stock_paths"])
        total_to_check = len(candidates) + len(non_stock_paths)
        summary = {
            "data_root": os.path.abspath(self.data_root),
            "total_database_files": inventory["total_database_files"],
            "checked_files": len(non_stock_paths),
            "candidate_files": total_to_check,
            "unindexed_candidates": sum(1 for item in candidates if not item[3]),
            "period_flag_candidates": sum(1 for item in candidates if item[3]),
            "valid_data_files": 0,
            "empty_data_files": 0,
            "no_missing_period_data_files": 0,
            "non_stock_files": len(non_stock_paths),
            "failed_files": 0,
            "failures": [],
            "stocks_to_index": [],
            "metadata_records": [],
            "candidate_fingerprints": {},
            "cancelled": False,
            "audit_complete": False,
            "applied": False,
            "updated_stocks": 0,
            "updated_periods": 0,
            "backup_path": "",
        }

        def stopped() -> bool:
            try:
                return bool(should_stop and should_stop())
            except Exception:
                return False

        def emit_progress():
            if progress_callback:
                percent = 100 if total_to_check == 0 else int(
                    summary["checked_files"] * 100 / total_to_check
                )
                progress_callback(max(0, min(percent, 100)))

        emit_progress()
        if stopped():
            summary["cancelled"] = True
            return summary
        if not candidates:
            summary["audit_complete"] = summary["checked_files"] == total_to_check
            emit_progress()
            return summary

        worker_count = max(1, min(int(max_workers or 1), 16, len(candidates)))
        def consume_result(result) -> None:
            stock_code, db_path, already_indexed, periods, fingerprint, error = result
            summary["checked_files"] += 1
            if error:
                summary["failed_files"] += 1
                if len(summary["failures"]) < 20:
                    summary["failures"].append(
                        f"{os.path.basename(db_path)}: {error}"
                    )
            elif periods:
                summary["valid_data_files"] += 1
                summary["stocks_to_index"].append(stock_code)
                summary["candidate_fingerprints"][stock_code] = fingerprint
                summary["metadata_records"].extend(
                    (stock_code, period) for period in periods
                )
            elif already_indexed:
                summary["no_missing_period_data_files"] += 1
            else:
                summary["empty_data_files"] += 1
            emit_progress()

        completed_candidates = 0
        if process_isolation:
            # GUI/CLI 正式入口使用可终止的 spawn 进程池。取消时 terminate 会
            # 连同卡在 duckdb.connect/query 的子进程一起回收，不留下仍持有
            # 股票库句柄的孤儿线程。
            context = multiprocessing.get_context("spawn")
            pool = context.Pool(processes=worker_count)
            try:
                results = pool.imap_unordered(
                    _inspect_database_periods_process_task,
                    candidates,
                    chunksize=1,
                )
                while completed_candidates < len(candidates):
                    if stopped():
                        summary["cancelled"] = True
                        pool.terminate()
                        break
                    try:
                        result = results.next(timeout=0.1)
                    except multiprocessing.TimeoutError:
                        continue
                    consume_result(result)
                    completed_candidates += 1
                if not summary["cancelled"]:
                    pool.close()
            except BaseException:
                pool.terminate()
                raise
            finally:
                pool.join()
        else:
            # 单元测试及嵌入调用可使用进程内短任务路径；正式 GUI/CLI 不走此分支。
            task_queue: queue.Queue = queue.Queue()
            result_queue: queue.Queue = queue.Queue()
            cancel_event = threading.Event()
            for candidate in candidates:
                task_queue.put(candidate)

            def audit_worker() -> None:
                while not cancel_event.is_set():
                    try:
                        candidate = task_queue.get_nowait()
                    except queue.Empty:
                        return
                    result_queue.put(
                        _inspect_database_periods_process_task(candidate)
                    )

            workers = [
                threading.Thread(
                    target=audit_worker,
                    name=f"KhQuantMetadataAudit-{index + 1}",
                    daemon=True,
                )
                for index in range(worker_count)
            ]
            for worker in workers:
                worker.start()

            while completed_candidates < len(candidates):
                if stopped():
                    summary["cancelled"] = True
                    cancel_event.set()
                    break
                try:
                    result = result_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                consume_result(result)
                completed_candidates += 1
            if not summary["cancelled"]:
                for worker in workers:
                    worker.join()

        if summary["cancelled"]:
            # 取消后禁止调用方误用已收集的部分结果执行写入。
            summary["stocks_to_index"] = []
            summary["metadata_records"] = []
            summary["candidate_fingerprints"] = {}
        else:
            summary["stocks_to_index"].sort()
            summary["metadata_records"].sort()
            summary["audit_complete"] = (
                summary["checked_files"] == summary["candidate_files"]
            )
            emit_progress()
        return summary

    def backup_metadata_database(self, purpose: str = "reindex") -> str:
        """在独占写连接内创建并验证一致的 DuckDB 逻辑备份。"""
        self._ensure_writable("backup metadata database")
        metadata_path = self._metadata_path()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = f"{metadata_path}.backup-{purpose}-{timestamp}"
        backup_alias = "khquant_reindex_backup"

        def quote_identifier(value: str) -> str:
            return '"' + str(value).replace('"', '""') + '"'

        def quote_literal(value: str) -> str:
            return "'" + str(value).replace("'", "''") + "'"

        attached = False
        with self._metadata_lock:
            self._ensure_metadata_conn()
            source_database = self._metadata_conn.execute(
                "SELECT current_database()"
            ).fetchone()[0]
            source_counts = {
                table_name: int(
                    self._metadata_conn.execute(
                        f"SELECT COUNT(*) FROM {quote_identifier(table_name)}"
                    ).fetchone()[0]
                )
                for table_name in ("stock_list", "sync_log")
            }
            try:
                self._metadata_conn.execute("CHECKPOINT")
                self._metadata_conn.execute(
                    f"ATTACH {quote_literal(backup_path)} "
                    f"AS {quote_identifier(backup_alias)}"
                )
                attached = True
                self._metadata_conn.execute(
                    "COPY FROM DATABASE "
                    f"{quote_identifier(source_database)} TO "
                    f"{quote_identifier(backup_alias)}"
                )
                self._metadata_conn.execute(
                    f"CHECKPOINT {quote_identifier(backup_alias)}"
                )
                self._metadata_conn.execute(
                    f"DETACH {quote_identifier(backup_alias)}"
                )
                attached = False

                validation = duckdb.connect(backup_path, read_only=True)
                try:
                    backup_counts = {
                        table_name: int(
                            validation.execute(
                                f"SELECT COUNT(*) FROM {quote_identifier(table_name)}"
                            ).fetchone()[0]
                        )
                        for table_name in ("stock_list", "sync_log")
                    }
                finally:
                    validation.close()
                if backup_counts != source_counts:
                    raise RuntimeError(
                        "metadata.db 备份校验失败：核心表行数与源数据库不一致"
                    )
            except Exception:
                if attached:
                    try:
                        self._metadata_conn.execute(
                            f"DETACH {quote_identifier(backup_alias)}"
                        )
                    except Exception:
                        pass
                try:
                    if os.path.isfile(backup_path):
                        os.remove(backup_path)
                except OSError:
                    pass
                raise
        return backup_path

    def apply_metadata_reindex(self, audit_summary: Dict[str, Any]) -> Dict[str, Any]:
        """对完整核验结果做备份和单事务提交；空结果严格零写入。"""
        self._ensure_writable("apply metadata reindex")
        summary = dict(audit_summary or {})
        summary_root = os.path.normcase(
            os.path.abspath(str(summary.get("data_root") or ""))
        )
        manager_root = os.path.normcase(os.path.abspath(self.data_root))
        if summary_root != manager_root:
            raise RuntimeError("索引核验结果不属于当前数据目录，未写入 metadata.db")
        if summary.get("cancelled"):
            raise RuntimeError("索引核验已取消，未写入 metadata.db")
        if not summary.get("audit_complete"):
            raise RuntimeError("索引核验结果不完整，未写入 metadata.db")
        checked_files = int(summary.get("checked_files", 0) or 0)
        candidate_files = int(summary.get("candidate_files", 0) or 0)
        if checked_files != candidate_files:
            raise RuntimeError(
                f"索引核验结果不完整（{checked_files}/{candidate_files}），未写入 metadata.db"
            )
        if int(summary.get("failed_files", 0) or 0) > 0:
            raise RuntimeError("存在无法读取的候选数据库，未写入任何索引")
        raw_records = list(summary.get("metadata_records") or [])

        with self._metadata_lock:
            current_periods = self._indexed_stock_periods()
            normalized_records = set()
            for raw_record in raw_records:
                if not isinstance(raw_record, (list, tuple)) or len(raw_record) != 2:
                    raise RuntimeError("索引核验记录格式无效，未写入 metadata.db")
                stock_code, period = map(str, raw_record)
                if period not in {"1d", "1m", "5m", "tick"}:
                    raise RuntimeError(
                        f"{stock_code} 包含未知周期 {period}，未写入 metadata.db"
                    )
                if period not in current_periods.get(stock_code, set()):
                    normalized_records.add((stock_code, period))
            records = sorted(normalized_records)
            if not records:
                summary.update(
                    metadata_records=[],
                    stocks_to_index=[],
                    applied=False,
                    updated_stocks=0,
                    updated_periods=0,
                    backup_path="",
                )
                return summary

            fingerprints = summary.get("candidate_fingerprints") or {}
            current_inventory = self._physical_database_inventory()["stock_paths"]
            for stock_code in sorted({record[0] for record in records}):
                fingerprint = fingerprints.get(stock_code)
                if not isinstance(fingerprint, dict):
                    raise RuntimeError(
                        f"{stock_code} 缺少核验文件指纹，未写入 metadata.db"
                    )
                db_path = os.path.abspath(str(fingerprint.get("path") or ""))
                expected_path = current_inventory.get(stock_code)
                if (
                    not expected_path
                    or os.path.normcase(os.path.abspath(expected_path))
                    != os.path.normcase(db_path)
                ):
                    raise RuntimeError(
                        f"{stock_code} 核验文件不属于当前数据目录，未写入 metadata.db"
                    )
                try:
                    current_stat = os.stat(db_path)
                except OSError as exc:
                    raise RuntimeError(
                        f"{stock_code} 数据库在核验后不可用，请重新核验：{exc}"
                    ) from exc
                if (
                    int(fingerprint.get("size", -1)) != int(current_stat.st_size)
                    or int(fingerprint.get("mtime_ns", -1))
                    != int(current_stat.st_mtime_ns)
                ):
                    raise RuntimeError(
                        f"{stock_code} 数据库在核验后发生变化，请重新核验"
                    )

            summary["metadata_records"] = records
            summary["stocks_to_index"] = sorted({record[0] for record in records})
            backup_path = self.backup_metadata_database("reindex")
            updated_periods = self.batch_update_metadata(records)
            summary.update(
                applied=True,
                updated_stocks=len(summary["stocks_to_index"]),
                updated_periods=int(updated_periods or 0),
                backup_path=backup_path,
            )
            return summary

    def scan_and_update_metadata(
        self,
        progress_callback: Callable = None,
        should_stop: Callable = None,
        max_workers: int = 8,
    ) -> Dict[str, Any]:
        """兼容入口：先只读核验；可写 manager 仅在有完整结果时提交。"""
        summary = self.audit_unindexed_databases(
            progress_callback=progress_callback,
            should_stop=should_stop,
            max_workers=max_workers,
            process_isolation=True,
        )
        if getattr(self, "read_only", False):
            return summary
        return self.apply_metadata_reindex(summary)

    def close_all(self, workers=None):
        """关闭所有连接；workers>1 时按股票并行关闭（各自 CHECKPOINT）。"""
        with self._db_lock:
            stock_codes = list(self._stock_dbs)
        workers = _close_workers(len(stock_codes), workers)
        if workers > 1:
            # 每只股票一个库文件，关闭时各自 CHECKPOINT；机械盘上逐只串行关闭
            # 50 只大库要几十秒。不同文件互不影响，按股票锁并行关闭。
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kh-db-close") as pool:
                list(pool.map(self.close_stock_connection, stock_codes))
        else:
            for stock_code in stock_codes:
                self.close_stock_connection(stock_code)

        self._drain_deferred_stock_closes(blocking=True)

        self.close_metadata_connection()

        logging.info("DuckDBManager 所有连接已关闭")

    def close_stock_connection(self, stock_code: str, skip_checkpoint: bool = False):
        """关闭单只股票连接，用于批量写入后尽快释放单股 .db 文件锁。"""
        stock_lock = self._get_stock_write_lock(stock_code)
        with stock_lock:
            with self._db_lock:
                stock_db = self._stock_dbs.pop(stock_code, None)
                self._connection_access_time.pop(stock_code, None)
            if stock_db is not None:
                stock_db.close(skip_checkpoint=skip_checkpoint)
            # 当前股票锁已经独占，顺便回收驱逐时仍在使用的旧连接。
            self._drain_deferred_stock_closes(
                blocking=True,
                stock_code=stock_code,
            )

    def checkpoint_all(self):
        """对所有打开的连接执行CHECKPOINT，将WAL数据写入磁盘"""
        with self._db_lock:
            stock_codes = list(self._stock_dbs)
        for stock_code in stock_codes:
            with self._get_stock_write_lock(stock_code):
                with self._db_lock:
                    stock_db = self._stock_dbs.get(stock_code)
                if stock_db is not None:
                    stock_db.checkpoint()

    def checkpoint_and_close_all(self):
        """批量 checkpoint 并关闭所有股票DB连接，释放资源"""
        with self._db_lock:
            stock_codes = list(self._stock_dbs)
        for stock_code in stock_codes:
            with self._get_stock_write_lock(stock_code):
                with self._db_lock:
                    stock_db = self._stock_dbs.pop(stock_code, None)
                    self._connection_access_time.pop(stock_code, None)
                if stock_db is not None:
                    stock_db.checkpoint()
                    stock_db.close(skip_checkpoint=True)

        self._drain_deferred_stock_closes(blocking=True)

        self.close_metadata_connection()

    def close_all_no_checkpoint(self):
        """仅关闭所有连接释放资源，不做CHECKPOINT（靠WAL自动恢复）
        
        适用于批量导入期间的中间批次，减少I/O开销。
        数据已写入WAL文件，下次打开时DuckDB自动恢复。
        """
        with self._db_lock:
            stock_codes = list(self._stock_dbs)
        for stock_code in stock_codes:
            self.close_stock_connection(stock_code, skip_checkpoint=True)

        self._drain_deferred_stock_closes(blocking=True)

        # metadata.db 也是 DuckDB 连接；如果不关闭，它会继续持有文件锁。
        # 之前 GUI “释放占用”调用本方法后仍可能被 metadata.db 锁住，
        # 根因就在这里。
        self.close_metadata_connection()

    def _cleanup_oldest_connection(self, skip_checkpoint: bool = False):
        """清理最久未使用的连接（单个）"""
        # 只在全局连接锁内选目标，关闭时走统一的“单股锁 -> 全局锁”路径。
        # 旧实现会直接关闭字典中的连接，若和并发写入交错，可能关闭正在
        # 使用的连接，也可能与连接驱逐形成相反的锁顺序。
        with self._db_lock:
            candidates = [
                (code, accessed_at)
                for code, accessed_at in self._connection_access_time.items()
                if code in self._stock_dbs
            ]
        if not candidates:
            return

        oldest_code = min(candidates, key=lambda item: item[1])[0]
        try:
            self.close_stock_connection(oldest_code, skip_checkpoint=skip_checkpoint)
        except Exception as e:
            logging.warning(f"清理连接失败 {oldest_code}: {e}")

    def _cleanup_idle_connections(self, keep_recent: int = 10, skip_checkpoint: bool = False):
        """
        清理不活跃的数据库连接，释放内存

        Args:
            keep_recent: 保留最近使用的连接数量
        """
        with self._db_lock:
            if len(self._stock_dbs) <= keep_recent:
                return

            # 根据访问时间排序,保留最近使用的连接
            if self._connection_access_time:
                sorted_codes = sorted(
                    self._connection_access_time.items(),
                    key=lambda x: x[1],
                    reverse=True
                )
                to_keep = set([code for code, _ in sorted_codes[:keep_recent]])
                to_remove = [code for code in self._stock_dbs.keys() if code not in to_keep]
            else:
                # 如果没有访问时间记录,使用原有逻辑
                items = list(self._stock_dbs.items())
                to_remove = [code for code, _ in items[:-keep_recent]]

        # 不能持 _db_lock 等待单股锁，否则会与“单股锁 -> _db_lock”的写入顺序死锁。
        for stock_code in to_remove:
            try:
                self.close_stock_connection(stock_code, skip_checkpoint=skip_checkpoint)
            except Exception as e:
                logging.warning(f"关闭连接失败 {stock_code}: {e}")

    def cleanup_connections_aggressive(self, skip_checkpoint: bool = False):
        """
        激进地清理所有数据库连接，释放最大内存

        用于大批量数据导入时定期调用，确保内存不会持续增长
        v3.1.5: 增强版本，更彻底地释放内存
        """
        with self._db_lock:
            stock_codes = list(self._stock_dbs)
        for stock_code in stock_codes:
            try:
                self.close_stock_connection(stock_code, skip_checkpoint=skip_checkpoint)
            except Exception as e:
                logging.warning(f"关闭连接失败 {stock_code}: {e}")

        self._drain_deferred_stock_closes(blocking=False)

        import gc
        gc.collect()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close_all()

    def get_missing_dates(self, stock_code: str, period: str,
                          target_start: str, target_end: str,
                          use_temp_conn: bool = True) -> List[str]:
        """
        检测指定股票在目标时间范围内缺失的交易日

        Args:
            stock_code: 股票代码
            period: 数据周期 ('1d', '1m', '5m', 'tick')
            target_start: 目标开始日期 (YYYYMMDD)
            target_end: 目标结束日期 (YYYYMMDD)
            use_temp_conn: 是否使用临时连接（减少内存占用）

        Returns:
            缺失日期列表 ['20240101', '20240102', ...]
        """
        try:
            # 导入交易日历函数
            import sys
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from khQTTools import is_trade_day
            from datetime import datetime, timedelta

            # 1. 生成目标范围内的所有交易日
            start_dt = datetime.strptime(target_start, '%Y%m%d')
            end_dt = datetime.strptime(target_end, '%Y%m%d')

            all_trade_days = set()
            current = start_dt
            while current <= end_dt:
                date_str = current.strftime('%Y%m%d')
                if is_trade_day(date_str):
                    all_trade_days.add(date_str)
                current += timedelta(days=1)

            # 2. 获取数据库中已有的日期
            if use_temp_conn:
                # 使用临时连接，避免缓存连接占用内存
                existing_dates = self._get_existing_dates_temp(stock_code, period)
            else:
                with self._get_stock_write_lock(stock_code):
                    stock_db = self.get_stock_db(stock_code)
                    existing_dates = stock_db.get_existing_dates(period)

            # 3. 计算差集
            missing = all_trade_days - existing_dates
            return sorted(list(missing))

        except Exception as e:
            logging.error(f"检测缺失数据失败 {stock_code}/{period}: {e}")
            # 出错时返回整个范围（作为安全回退）
            return [target_start]

    def _get_existing_dates_temp(self, stock_code: str, period: str) -> set:
        """
        使用临时连接获取已有数据的日期集合（内存友好版本）

        Args:
            stock_code: 股票代码
            period: 周期类型

        Returns:
            日期集合
        """
        table_map = {'1d': 'kline_1d', '1m': 'kline_1m', '5m': 'kline_5m', 'tick': 'tick'}
        table_name = table_map.get(period)
        if not table_name:
            return set()

        # 解析股票代码获取数据库路径
        if '.' in stock_code:
            code, market = stock_code.split('.')
        else:
            code = stock_code
            market = 'SH' if code.startswith(('6', '5')) else 'SZ'

        db_path = resolve_stock_db_path(self.data_root, f"{code}.{market}")

        if not os.path.exists(db_path):
            return set()

        # 复用 StockDB 连接池，避免文件锁冲突
        try:
            with self._get_stock_write_lock(stock_code):
                stock_db = self.get_stock_db(stock_code)
                conn = stock_db.conn

                tables = conn.execute("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_name = ?
                """, [table_name]).fetchall()
                if not tables:
                    return set()

                result = conn.execute(f"""
                    SELECT DISTINCT strftime(time, '%Y%m%d') as date_str
                    FROM {table_name}
                    WHERE time IS NOT NULL
                """).fetchall()
                return {row[0] for row in result if row[0]}
        except Exception as e:
            logging.warning(f"获取已有日期失败 {stock_code}/{period}: {e}")
            return set()

    def get_existing_dates_batch(
        self,
        stock_code: str,
        periods: List[str],
        raise_on_error: bool = False,
    ) -> Dict[str, set]:
        """
        批量获取一只股票多个周期的已有日期（性能优化版本）

        只打开一次数据库连接，查询所有周期

        Args:
            stock_code: 股票代码
            periods: 周期列表 ['1d', '1m', '5m']

        Returns:
            {period: set(日期)} 字典
        """
        table_map = {'1d': 'kline_1d', '1m': 'kline_1m', '5m': 'kline_5m', 'tick': 'tick'}
        result = {p: set() for p in periods}

        # 解析股票代码获取数据库路径
        if '.' in stock_code:
            code, market = stock_code.split('.')
        else:
            code = stock_code
            market = 'SH' if code.startswith(('6', '5')) else 'SZ'

        db_path = resolve_stock_db_path(self.data_root, f"{code}.{market}")

        if not os.path.exists(db_path):
            return result

        # 复用 StockDB 连接池，避免文件锁冲突
        try:
            with self._get_stock_write_lock(stock_code):
                stock_db = self.get_stock_db(stock_code)
                conn = stock_db.conn

                existing_tables = set()
                try:
                    tables_result = conn.execute("""
                        SELECT table_name FROM information_schema.tables
                    """).fetchall()
                    existing_tables = {row[0] for row in tables_result}
                except Exception as exc:
                    if raise_on_error or is_duckdb_lock_error(exc):
                        raise

                for period in periods:
                    table_name = table_map.get(period)
                    if not table_name or table_name not in existing_tables:
                        continue
                    try:
                        dates_result = conn.execute(f"""
                            SELECT DISTINCT strftime(time, '%Y%m%d') as date_str
                            FROM {table_name}
                            WHERE time IS NOT NULL
                        """).fetchall()
                        result[period] = {row[0] for row in dates_result if row[0]}
                    except Exception as exc:
                        if raise_on_error or is_duckdb_lock_error(exc):
                            raise

        except Exception as e:
            if raise_on_error or is_duckdb_lock_error(e):
                raise
            logging.warning(f"批量获取已有日期失败 {stock_code}: {e}")

        return result

    def get_existing_date_counts(
        self,
        stock_code: str,
        period: str,
        raise_on_error: bool = False,
        start_date: str = "",
        end_date: str = "",
    ) -> Dict[str, int]:
        """返回单个周期按交易日统计的有效时间戳数量。

        主要用于分钟数据增量补充：仅知道“当天有数据”无法识别半天、断点或
        中途失败形成的不完整交易日。锁冲突始终向上抛出，供统一重试与交互层处理。
        """
        coverage = self.get_existing_date_completeness(
            stock_code,
            period,
            required_columns=(),
            raise_on_error=raise_on_error,
            start_date=start_date,
            end_date=end_date,
        )
        return {
            date_value: int(stats.get('total_rows', 0) or 0)
            for date_value, stats in coverage.items()
        }

    def get_existing_date_completeness(
        self,
        stock_code: str,
        period: str,
        required_columns: Optional[List[str]] = None,
        raise_on_error: bool = False,
        start_date: str = "",
        end_date: str = "",
    ) -> Dict[str, Dict[str, int]]:
        """返回每天的总时间戳数和必需字段完整时间戳数。

        ``required_columns`` 只接受当前行情表真实存在的列名。旧库尚未包含某个
        复权/指标列时，该日 ``valid_rows`` 为 0，而不是把 schema 异常吞成
        “整段无数据”。锁冲突仍向上抛出，供 GUI 的统一重试流程处理。
        """

        period_value = str(getattr(period, 'value', period) or '').strip().lower()
        table_map = {
            '1d': 'kline_1d', '1m': 'kline_1m',
            '5m': 'kline_5m', 'tick': 'tick',
        }
        table_name = table_map.get(period_value)
        if not table_name:
            return {}

        if '.' in stock_code:
            code, market = stock_code.split('.', 1)
        else:
            code = stock_code
            market = 'SH' if code.startswith(('6', '5')) else 'SZ'
        db_path = resolve_stock_db_path(self.data_root, f"{code}.{market}")
        if not os.path.exists(db_path):
            return {}

        requested_columns = list(dict.fromkeys(
            str(column).strip() for column in (required_columns or [])
            if str(column).strip()
        ))
        if period_value == 'tick':
            # GUI/CLI 可能把 K 线 raw/复权字段列表原样传给 tick。
            # tick 表没有 close/open_front 等列，若直接拿这些字段做
            # valid_rows 会把所有已有日误判为 0；统一到真实质量字段。
            tick_columns = {
                'time', 'lastPrice', 'open', 'high', 'low', 'lastClose',
                'amount', 'volume', 'pvolume', 'stockStatus', 'openInt',
                'lastSettlementPrice', 'transactionNum',
            }
            if not requested_columns or not set(requested_columns).issubset(tick_columns):
                requested_columns = ['lastPrice', 'volume', 'amount']

        def _date_param(value: str) -> str:
            text = str(value or "").strip()[:10]
            compact = text.replace('-', '')
            if len(compact) >= 8 and compact[:8].isdigit():
                return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
            return text

        try:
            with self._get_stock_write_lock(stock_code):
                stock_db = self.get_stock_db(stock_code)
                conn = stock_db.conn
                exists = conn.execute(
                    "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
                    [table_name],
                ).fetchone()
                if not exists or not int(exists[0] or 0):
                    return {}

                schema_rows = conn.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = ?
                    """,
                    [table_name],
                ).fetchall()
                schema_columns = {str(row[0]) for row in schema_rows}
                unsafe = [
                    column for column in requested_columns
                    if not column.replace('_', '').isalnum()
                ]
                if unsafe:
                    raise ValueError(f"非法完整性字段: {unsafe[0]}")

                missing_schema = [
                    column for column in requested_columns
                    if column not in schema_columns
                ]
                if requested_columns and not missing_schema:
                    valid_expr = " AND ".join(
                        (
                            f'"{column}" IS NOT NULL '
                            f'AND isfinite(TRY_CAST("{column}" AS DOUBLE))'
                        )
                        for column in requested_columns
                    )
                    if period_value == 'tick' and 'lastPrice' in requested_columns:
                        # QMT 在本地缓存未准备好时可能返回整批 0 占位快照；
                        # 这类行不能满足“可回测 tick”门禁。暂停/无成交的
                        # 正常快照仍有 lastPrice，不受影响。
                        valid_expr += (
                            ' AND TRY_CAST("lastPrice" AS DOUBLE) > 0'
                        )
                    valid_count_sql = (
                        f"COUNT(DISTINCT CASE WHEN {valid_expr} THEN time END)"
                    )
                elif requested_columns:
                    valid_count_sql = "0"
                else:
                    valid_count_sql = "COUNT(DISTINCT time)"

                where_parts = ["time IS NOT NULL"]
                params = []
                if start_date:
                    where_parts.append("time >= CAST(? AS DATE)")
                    params.append(_date_param(start_date))
                if end_date:
                    where_parts.append("time < CAST(? AS DATE) + INTERVAL 1 DAY")
                    params.append(_date_param(end_date))
                where_sql = " AND ".join(where_parts)
                rows = conn.execute(f"""
                    SELECT strftime(time, '%Y%m%d') AS date_str,
                           COUNT(DISTINCT time) AS total_rows,
                           {valid_count_sql} AS valid_rows
                    FROM {table_name}
                    WHERE {where_sql}
                    GROUP BY 1
                """, params).fetchall()
                return {
                    str(date_value): {
                        'total_rows': int(total_rows or 0),
                        'valid_rows': int(valid_rows or 0),
                    }
                    for date_value, total_rows, valid_rows in rows
                    if date_value
                }
        except Exception as exc:
            if raise_on_error or is_duckdb_lock_error(exc):
                raise
            logging.warning(f"获取已有行情完整度失败 {stock_code}/{period}: {exc}")
            return {}

    def get_missing_dates_batch(self, stock_code: str, periods_config: Dict[str, tuple],
                                 trade_days_cache: Dict[str, set] = None) -> Dict[str, List[str]]:
        """
        批量检测一只股票多个周期的缺失日期（性能优化版本）

        Args:
            stock_code: 股票代码
            periods_config: {period: (start, end)} 周期配置
            trade_days_cache: 交易日缓存，避免重复生成

        Returns:
            {period: [缺失日期列表]}
        """
        result = {p: [] for p in periods_config.keys()}

        try:
            # 导入交易日历函数
            import sys
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from khQTTools import is_trade_day
            from datetime import datetime, timedelta

            # 批量获取所有周期的已有日期（只打开一次数据库）
            existing_dates = self.get_existing_dates_batch(stock_code, list(periods_config.keys()))

            for period, (target_start, target_end) in periods_config.items():
                # 使用缓存的交易日列表
                cache_key = f"{target_start}_{target_end}"
                if trade_days_cache and cache_key in trade_days_cache:
                    all_trade_days = trade_days_cache[cache_key]
                else:
                    # 生成交易日列表
                    start_dt = datetime.strptime(target_start, '%Y%m%d')
                    end_dt = datetime.strptime(target_end, '%Y%m%d')

                    all_trade_days = set()
                    current = start_dt
                    while current <= end_dt:
                        date_str = current.strftime('%Y%m%d')
                        if is_trade_day(date_str):
                            all_trade_days.add(date_str)
                        current += timedelta(days=1)

                    # 存入缓存
                    if trade_days_cache is not None:
                        trade_days_cache[cache_key] = all_trade_days

                # 计算缺失日期
                missing = all_trade_days - existing_dates.get(period, set())
                result[period] = sorted(list(missing))

        except Exception as e:
            logging.error(f"批量检测缺失数据失败 {stock_code}: {e}")

        return result

    def group_consecutive_dates(self, dates: List[str]) -> List[tuple]:
        """
        将日期列表分组为连续日期范围

        Args:
            dates: 已排序的日期列表 ['20240101', '20240102', '20240103', '20240108', '20240109']

        Returns:
            连续日期范围列表 [('20240101', '20240103'), ('20240108', '20240109')]
        """
        if not dates:
            return []

        from datetime import datetime, timedelta

        groups = []
        dates = sorted(dates)

        group_start = dates[0]
        prev_date = dates[0]

        for date_str in dates[1:]:
            # 检查是否连续（下一个自然日）
            prev_dt = datetime.strptime(prev_date, '%Y%m%d')
            curr_dt = datetime.strptime(date_str, '%Y%m%d')

            # 如果间隔超过1天，则开始新的分组
            if (curr_dt - prev_dt).days > 1:
                groups.append((group_start, prev_date))
                group_start = date_str

            prev_date = date_str

        # 添加最后一个分组
        groups.append((group_start, prev_date))

        return groups
