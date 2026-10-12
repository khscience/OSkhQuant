# -*- coding: utf-8 -*-
"""
单股票数据库操作类

每只股票对应一个独立的 .db 文件
"""

import os
import logging
import shutil
import math
import sys
import tempfile
from typing import Optional, List, Tuple
from datetime import datetime

import duckdb
import numpy as np
import pandas as pd

from .data_quality import validate_kline_quality
from .time_utils import coerce_market_time
from .lock_retry import is_duckdb_lock_error, retry_on_duckdb_lock
from .config import resolve_stock_db_path
from .units import ensure_kline_storage_units, VolumeUnitError


class DuplicateTimestampError(ValueError):
    """行情输入中出现无法区分的重复时间戳。

    Tick 表目前为了兼容既有回测查询仍使用单列 ``time`` 主键。上游若在
    同一精度返回两条记录，静默 ``drop_duplicates`` 会丢掉真实行情，因此
    在写库边界明确拒绝整批写入，并把稳定错误码交给 GUI/CLI 展示。
    """

    code = "DUPLICATE_TIMESTAMP"
    retryable = False

    def __init__(self, stock_code: str, duplicates: int, sample=None, period: str = "tick"):
        self.stock_code = str(stock_code)
        self.duplicates = int(duplicates)
        self.sample = tuple(sample or ())
        self.period = str(period or "tick")
        # Keep the same machine-readable shape as the bridge/adapter errors;
        # GUI and batch callers can report the stable code without parsing the
        # localized ValueError text.
        self.details = {
            "stock_code": self.stock_code,
            "period": self.period,
            "duplicate_rows": self.duplicates,
            "sample": list(self.sample[:8]),
        }
        preview = ", ".join(str(value) for value in self.sample[:3])
        suffix = f"（示例: {preview}）" if preview else ""
        super().__init__(
            f"{self.stock_code} {self.period} 存在 {self.duplicates} 个重复时间戳，"
            f"为避免静默丢数据已拒绝写入{suffix}"
        )


_DUCKDB_ORDER_MODE = os.environ.get("KH_DUCKDB_ORDER_MODE", "verify_after_fetch").strip().lower()
_DUCKDB_READ_ENSURE_TABLES = os.environ.get("KH_DUCKDB_READ_ENSURE_TABLES", "1").strip().lower() not in (
    "0", "false", "no", "off"
)


def set_duckdb_order_mode(mode: str):
    global _DUCKDB_ORDER_MODE
    value = str(mode or "verify_after_fetch").strip().lower()
    if value not in ("verify_after_fetch", "sql_order_by"):
        value = "verify_after_fetch"
    _DUCKDB_ORDER_MODE = value


def get_duckdb_order_mode() -> str:
    return _DUCKDB_ORDER_MODE


def set_duckdb_read_ensure_tables(enabled):
    global _DUCKDB_READ_ENSURE_TABLES
    value = str(enabled).strip().lower()
    _DUCKDB_READ_ENSURE_TABLES = value not in ("0", "false", "no", "off")


def get_duckdb_read_ensure_tables() -> bool:
    return _DUCKDB_READ_ENSURE_TABLES


def _is_unreplayable_wal_error(exc) -> bool:
    """DuckDB 回放残留 WAL 时的内部断言失败（进程被强杀后常见）。"""
    text = str(exc)
    return "Failure while replaying WAL" in text


def _move_unreplayable_wal(db_path):
    """把 <库>.wal 移到数据根目录的 _wal_recovered/<市场>/ 下；不存在则返回 None。"""
    wal_path = str(db_path) + ".wal"
    if not os.path.exists(wal_path):
        return None
    market_dir = os.path.dirname(os.path.abspath(str(db_path)))
    data_root = os.path.dirname(market_dir)
    target_dir = os.path.join(data_root, "_wal_recovered", os.path.basename(market_dir))
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(
        target_dir,
        os.path.basename(wal_path) + "." + datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
    )
    shutil.move(wal_path, target)
    return target


class StockDB:
    """
    单只股票的数据库操作类
    
    每只股票对应一个 .db 文件，包含该股票所有周期的K线数据
    """
    
    # 周期到表名的映射
    PERIOD_TABLE_MAP = {
        '1d': 'kline_1d',
        '1m': 'kline_1m', 
        '5m': 'kline_5m',
        'tick': 'tick',
    }

    DAILY_INDICATOR_COLUMNS = (
        'turn', 'tradestatus', 'pctChg', 'peTTM', 'psTTM',
        'pcfNcfTTM', 'pbMRQ', 'isST'
    )

    RAW_KLINE_COLUMNS = (
        'open', 'high', 'low', 'close', 'volume', 'amount',
        'settelementPrice', 'openInterest', 'preClose', 'suspendFlag',
    )
    RAW_PRICE_COLUMNS = ('open', 'high', 'low', 'close')

    ADJUSTMENT_COLUMNS = (
        'open_front', 'high_front', 'low_front', 'close_front',
        'open_back', 'high_back', 'low_back', 'close_back',
        'open_front_ratio', 'high_front_ratio', 'low_front_ratio',
        'close_front_ratio', 'open_back_ratio', 'high_back_ratio',
        'low_back_ratio', 'close_back_ratio',
    )

    # 只允许清理由行情派生、可重新生成的列。原始 OHLCV、time、
    # dividend_type、update_time 等基础/关键字段即使调用方显式传入也拒绝，
    # 避免复权失败处理误伤原始行情。
    CLEARABLE_KLINE_COLUMNS = frozenset(
        ADJUSTMENT_COLUMNS + DAILY_INDICATOR_COLUMNS
    )
    
    def __init__(self, stock_code: str, data_root: str, read_only: bool = False):
        """
        初始化股票数据库
        
        Args:
            stock_code: 股票代码，如 '000001.SZ'
            data_root: 数据根目录
        """
        self.stock_code = stock_code
        self.data_root = data_root
        self.read_only = bool(read_only)
        self.close_policy = 'checkpoint'
        
        # 解析股票代码
        if '.' in stock_code:
            self.code, self.market = stock_code.split('.')
        else:
            self.code = stock_code
            # 根据代码推断市场
            if stock_code.startswith(('6', '5')):
                self.market = 'SH'
            elif stock_code.startswith(('0', '3')):
                self.market = 'SZ'
            else:
                self.market = 'SZ'
        
        # 数据库文件路径
        self.db_path = resolve_stock_db_path(
            data_root,
            f"{self.code}.{self.market}",
            create_market=not self.read_only,
        )
        
        # 确保目录存在
        if not self.read_only:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        # 检查是否是新数据库（文件不存在则是新建的）
        self._is_new_db = not os.path.exists(self.db_path)
        
        # 连接（延迟创建）
        self._conn: Optional[duckdb.DuckDBPyConnection] = None
        # 已验证过结构的表名缓存，避免重复 DESCRIBE
        self._verified_tables: set = set()
        # 已在本次连接中创建过的表（避免重复 CREATE TABLE IF NOT EXISTS）
        self._created_tables: set = set()

    def _ensure_writable(self, operation: str = "write"):
        if self.read_only:
            raise RuntimeError(f"StockDB is opened read-only; cannot {operation}: {self.stock_code}")
    
    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        """获取数据库连接（延迟初始化）"""
        if self._conn is None:
            connect_config = None
            if self.read_only:
                if sys.platform.startswith("linux"):
                    spill_dir = os.path.join(
                        tempfile.gettempdir(), "khquant", "duckdb", str(os.getpid())
                    )
                    os.makedirs(spill_dir, mode=0o700, exist_ok=True)
                else:
                    spill_dir = "temp"
                # Passing read-only settings at connect time avoids three SQL
                # round trips for every stock database.  Full-market loads open
                # thousands of small files, so this saves measurable startup
                # time without changing query semantics.
                connect_config = {
                    "threads": "1",
                    "memory_limit": "256MB",
                    "temp_directory": spill_dir,
                }
            def _connect():
                if connect_config is not None:
                    return duckdb.connect(
                        self.db_path,
                        read_only=self.read_only,
                        config=connect_config,
                    )
                # Some DuckDB releases reject an explicit ``config=None``.
                # Keep write connections on the original call signature.
                return duckdb.connect(self.db_path, read_only=self.read_only)

            try:
                self._conn = retry_on_duckdb_lock(
                    _connect,
                    on_retry=lambda exc, attempt, total, wait: logging.warning(
                        "DuckDB文件被占用，%.2f秒后重试 (%s/%s): %s",
                        wait, attempt + 1, total, self.db_path,
                    ),
                )
            except Exception as exc:
                if _is_unreplayable_wal_error(exc) and os.path.exists(str(self.db_path) + '.khwal.json'):
                    raise RuntimeError('WAL_RECOVERY_REQUIRED: 已提交日志无法回放，DB/WAL 保持原位，停止写入: ' + self.db_path) from exc
                if self.read_only or not _is_unreplayable_wal_error(exc):
                    raise
                moved_to = _move_unreplayable_wal(self.db_path)
                if moved_to is None:
                    raise
                logging.warning(
                    "DuckDB WAL 无法回放，已备份移走后重开: %s -> %s (%s)",
                    self.db_path, moved_to, str(exc).splitlines()[0][:160],
                )
                self._conn = _connect()
            if not self.read_only:
                # 写连接沿用原有初始化路径，避免改变导入/补充模块的行为。
                self._conn.execute("SET threads=1")
                self._conn.execute("SET memory_limit='256MB'")
                if sys.platform.startswith("linux"):
                    spill_dir = os.path.join(
                        tempfile.gettempdir(), "khquant", "duckdb", str(os.getpid())
                    )
                    os.makedirs(spill_dir, mode=0o700, exist_ok=True)
                    escaped = spill_dir.replace("'", "''")
                    self._conn.execute(f"SET temp_directory='{escaped}'")
                else:
                    self._conn.execute("SET temp_directory='temp'")
            if not self.read_only:
                self._init_tables()
        return self._conn
    
    def _init_tables(self):
        """初始化基础表结构（仅 stock_info）。K线/Tick 表按需延迟创建。"""
        # 已存在的数据库无需重复初始化 stock_info（表已存在）
        if not self._is_new_db:
            return
        # 股票信息表（轻量，仅新数据库创建）
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_info (
                key           VARCHAR PRIMARY KEY,
                value         VARCHAR,
                update_time   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 插入股票代码信息
        self._conn.execute("""
            INSERT OR REPLACE INTO stock_info (key, value, update_time)
            VALUES ('stock_code', ?, CURRENT_TIMESTAMP)
        """, [self.stock_code])

    def _ensure_period_table(self, period: str):
        """
        按需创建特定周期的数据表（延迟创建，只在首次写入/读取时触发）。
        对于新数据库，创建后直接标记为已验证（跳过后续 DESCRIBE）。
        """
        if period == 'tick':
            table_name = 'tick'
        else:
            table_name = self.PERIOD_TABLE_MAP.get(period)
        if not table_name or table_name in self._created_tables:
            return
        if self.read_only:
            return

        if period in ('1d', '1m', '5m'):
            indicator_columns = ""
            if period == '1d':
                indicator_columns = """
                    turn              DOUBLE,
                    tradestatus       DOUBLE,
                    pctChg            DOUBLE,
                    peTTM             DOUBLE,
                    psTTM             DOUBLE,
                    pcfNcfTTM         DOUBLE,
                    pbMRQ             DOUBLE,
                    isST              DOUBLE,
                """
            self.conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {table_name} (
                    time              TIMESTAMP PRIMARY KEY,
                    open              DOUBLE,
                    high              DOUBLE,
                    low               DOUBLE,
                    close             DOUBLE,
                    volume            DOUBLE,
                    amount            DOUBLE,
                    settelementPrice  DOUBLE,
                    openInterest      BIGINT,
                    preClose          DOUBLE,
                    suspendFlag       INTEGER DEFAULT 0,
                    open_front        DOUBLE,
                    high_front        DOUBLE,
                    low_front         DOUBLE,
                    close_front       DOUBLE,
                    open_back         DOUBLE,
                    high_back         DOUBLE,
                    low_back          DOUBLE,
                    close_back        DOUBLE,
                    open_front_ratio  DOUBLE,
                    high_front_ratio  DOUBLE,
                    low_front_ratio   DOUBLE,
                    close_front_ratio DOUBLE,
                    open_back_ratio   DOUBLE,
                    high_back_ratio   DOUBLE,
                    low_back_ratio    DOUBLE,
                    close_back_ratio  DOUBLE,
                    {indicator_columns}
                    dividend_type     VARCHAR DEFAULT 'none',
                    update_time       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        elif period == 'tick':
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS tick (
                    time                  TIMESTAMP PRIMARY KEY,
                    lastPrice             DOUBLE,
                    open                  DOUBLE,
                    high                  DOUBLE,
                    low                   DOUBLE,
                    lastClose             DOUBLE,
                    amount                DOUBLE,
                    volume                BIGINT,
                    pvolume               BIGINT,
                    stockStatus           INTEGER,
                    openInt               BIGINT,
                    lastSettlementPrice   DOUBLE,
                    askPrice1             DOUBLE,
                    askPrice2             DOUBLE,
                    askPrice3             DOUBLE,
                    askPrice4             DOUBLE,
                    askPrice5             DOUBLE,
                    bidPrice1             DOUBLE,
                    bidPrice2             DOUBLE,
                    bidPrice3             DOUBLE,
                    bidPrice4             DOUBLE,
                    bidPrice5             DOUBLE,
                    askVol1               BIGINT,
                    askVol2               BIGINT,
                    askVol3               BIGINT,
                    askVol4               BIGINT,
                    askVol5               BIGINT,
                    bidVol1               BIGINT,
                    bidVol2               BIGINT,
                    bidVol3               BIGINT,
                    bidVol4               BIGINT,
                    bidVol5               BIGINT,
                    transactionNum        BIGINT,
                    update_time           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

        self._created_tables.add(table_name)
        # 新数据库的表一定拥有完整 schema，跳过后续 DESCRIBE
        if self._is_new_db:
            self._verified_tables.add(table_name)
    
    def _ensure_table_columns(self, table_name: str, expected_columns: List[str], period: str = None):
        """确保表结构包含所有必需的列（带缓存，同一连接只检查一次）"""
        if self.read_only:
            return
        if table_name in self._verified_tables:
            return

        # 触发 conn 初始化（如果尚未初始化），并按需创建此表
        _ = self.conn
        if period:
            self._ensure_period_table(period)

        # _ensure_period_table 可能已把表标记为已验证（新数据库的情况）
        if table_name in self._verified_tables:
            return

        try:
            result = self.conn.execute(f"DESCRIBE {table_name}").fetchall()
            existing_columns = {row[0] for row in result}
            
            for col in expected_columns:
                if col not in existing_columns:
                    if col == 'dividend_type':
                        self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} VARCHAR DEFAULT 'none'")
                    elif col == 'update_time':
                        self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                    elif col == 'suspendFlag':
                        self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} INTEGER DEFAULT 0")
                    elif col == 'volume' and period != 'tick':
                        self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} DOUBLE")
                    elif col in ['volume', 'openInterest']:
                        self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} BIGINT")
                    elif col == 'time':
                        continue
                    else:
                        self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} DOUBLE")
                    logging.info(f"已为表 {table_name} 添加列 {col}")
            self._verified_tables.add(table_name)
        except Exception as e:
            logging.warning(f"检查表结构时出错: {e}")

    def _ensure_adjustment_factor_table(self):
        """按需创建可复现复权因子表，不改变既有K线表读取路径。"""
        self._ensure_writable("save adjustment factors")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS adjustment_factors (
                effective_date  DATE NOT NULL,
                adjustment_type VARCHAR NOT NULL,
                factor          DECIMAL(38, 18) NOT NULL,
                factor_version  VARCHAR NOT NULL,
                checksum        VARCHAR NOT NULL,
                source          VARCHAR NOT NULL,
                captured_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (effective_date, adjustment_type)
            )
        """)

    def save_adjustment_factors(
        self, values, *, adjustment_type: str, factor_version: str,
        checksum: str, source: str, force: bool = False,
    ) -> int:
        """保存带生效日和版本的复权因子；普通模式只填缺失键。"""
        adjustment = str(adjustment_type or "").strip().lower()
        if adjustment not in {"front", "back", "front_ratio", "back_ratio"}:
            raise ValueError(f"非法复权类型: {adjustment_type}")
        frame = pd.DataFrame(values).copy()
        if not {"effective_date", "factor"}.issubset(frame.columns):
            raise ValueError("复权因子缺少 effective_date/factor")
        frame["effective_date"] = pd.to_datetime(
            frame["effective_date"], errors="coerce"
        ).dt.date
        frame["factor"] = pd.to_numeric(frame["factor"], errors="coerce")
        frame = frame.dropna(subset=["effective_date", "factor"])
        frame = frame[
            frame["factor"].map(lambda value: math.isfinite(float(value)))
            & frame["factor"].gt(0)
        ].drop_duplicates(subset=["effective_date"], keep="last")
        if frame.empty:
            raise ValueError("没有可保存的有效复权因子")
        self._ensure_adjustment_factor_table()
        sql = """
            INSERT INTO adjustment_factors
              (effective_date, adjustment_type, factor, factor_version,
               checksum, source, captured_at)
            VALUES (?, ?, CAST(? AS DECIMAL(38, 18)), ?, ?, ?, CURRENT_TIMESTAMP)
        """
        if force:
            sql += """
                ON CONFLICT(effective_date, adjustment_type) DO UPDATE SET
                  factor=excluded.factor,
                  factor_version=excluded.factor_version,
                  checksum=excluded.checksum,
                  source=excluded.source,
                  captured_at=excluded.captured_at
            """
        else:
            sql += " ON CONFLICT(effective_date, adjustment_type) DO NOTHING"
        rows = [
            (
                row.effective_date, adjustment, format(float(row.factor), ".18g"),
                str(factor_version), str(checksum), str(source),
            )
            for row in frame.itertuples(index=False)
        ]
        self.conn.executemany(sql, rows)
        return len(rows)

    def get_adjustment_factors(
        self, adjustment_type: Optional[str] = None,
    ) -> pd.DataFrame:
        if self.read_only:
            tables = {
                str(row[0]) for row in self.conn.execute("SHOW TABLES").fetchall()
            }
            if "adjustment_factors" not in tables:
                return pd.DataFrame()
        else:
            self._ensure_adjustment_factor_table()
        if adjustment_type:
            return self.conn.execute(
                """
                SELECT * FROM adjustment_factors
                WHERE adjustment_type=? ORDER BY effective_date
                """,
                [str(adjustment_type).strip().lower()],
            ).fetchdf()
        return self.conn.execute(
            "SELECT * FROM adjustment_factors ORDER BY adjustment_type, effective_date"
        ).fetchdf()

    def invalidate_period_table_cache(self, periods: List[str]):
        """事务回滚后清除可能随 DDL 一同回滚的表结构缓存。"""
        for period in periods:
            table_name = 'tick' if period == 'tick' else self.PERIOD_TABLE_MAP.get(period)
            if table_name:
                self._created_tables.discard(table_name)
                self._verified_tables.discard(table_name)

    @staticmethod
    def _normalize_string_dtypes_for_duckdb(df: pd.DataFrame) -> pd.DataFrame:
        """将新字符串扩展类型降级为 object，兼容打包环境中的 DuckDB。"""
        if df is None or df.empty:
            return df

        df = df.copy()
        for col in df.columns:
            dtype_name = str(df[col].dtype).lower()
            if dtype_name == 'str' or dtype_name.startswith('string'):
                # 部分打包环境下 DuckDB 无法识别 pandas/numpy 的新字符串 dtype，
                # 降级为普通 Python 字符串列可避免 "Data type 'str' not recognized"。
                df[col] = df[col].astype(object)
        return df

    def _drop_invalid_time_rows(self, df: pd.DataFrame, period: str) -> pd.DataFrame:
        """过滤 time 为空或无效的记录，避免整批写入失败。"""
        if df is None or df.empty or 'time' not in df.columns:
            return df

        invalid_mask = df['time'].isna()
        invalid_count = int(invalid_mask.sum())
        if invalid_count <= 0:
            return df

        logging.warning(
            f"{self.stock_code} {period} 跳过 {invalid_count} 条 time 为空或无效的数据"
        )
        return df.loc[~invalid_mask].copy()

    def _incoming_rows_unchanged(
        self,
        table_name: str,
        incoming_df: pd.DataFrame,
        columns: List[str],
        *,
        exact_range: bool,
    ) -> bool:
        """判断待写入业务字段是否已原样存在，失败时安全退回正常覆盖。

        K线覆盖语义是“输入最小/最大时间之间完全替换”，因此还需确保该区间
        的现有行数与输入一致；Tick 原逻辑只替换输入中出现的时间点，允许同一
        时间范围内存在额外 Tick。``update_time`` 由调用方排除，不应因为一次
        无内容变化的重复补充而制造整日数据的物理重写。
        """
        if incoming_df is None or incoming_df.empty or not columns:
            return False

        quoted_table = f'"{table_name}"'
        business_columns = [column for column in columns if column != 'time']
        differences = " OR ".join(
            f'i."{column}" IS DISTINCT FROM t."{column}"'
            for column in business_columns
        ) or "FALSE"

        try:
            min_time = pd.Timestamp(incoming_df['time'].min()).to_pydatetime()
            max_time = pd.Timestamp(incoming_df['time'].max()).to_pydatetime()
            stored_count, difference_count = self.conn.execute(f"""
                WITH existing AS (
                    SELECT *
                    FROM {quoted_table}
                    WHERE time >= ? AND time <= ?
                )
                SELECT
                    (SELECT COUNT(*) FROM existing),
                    COUNT(*) FILTER (
                        WHERE t.time IS NULL OR {differences}
                    )
                FROM incoming_df AS i
                LEFT JOIN existing AS t ON t.time = i.time
            """, [min_time, max_time]).fetchone()
            if int(difference_count or 0) != 0:
                return False
            return not exact_range or int(stored_count or 0) == len(incoming_df)
        except Exception as exc:
            # 该路径只是性能优化。表尚未建立、旧 schema 或比较类型不兼容时，
            # 必须保持原覆盖写入行为，不能影响数据补充的可靠性。
            logging.debug(
                "%s %s 一致性比较不可用，继续正常覆盖: %s",
                self.stock_code,
                table_name,
                exc,
            )
            return False

    def save_kline(
        self,
        df: pd.DataFrame,
        period: str,
        dividend_type: str = 'none',
        overwrite: bool = True,
        manage_transaction: bool = True,
        skip_unchanged: bool = False,
        *,
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
            df: K线数据DataFrame；volume 必须为手（可含小数），amount 必须为元。
                源为股/份时应在适配入口换算，存储层不会猜测并缩放输入数据。
            period: 周期类型 ('1d', '1m', '5m')
            dividend_type: 复权类型
            overwrite: True=覆盖（匹配行原位更新、删除区间多余行并插入
                       缺失行；保留来源未返回的已有扩展字段）；
                       False=增量补充（INSERT ON CONFLICT(time) DO NOTHING，
                       已存在的时间保持不变，仅补入缺失的 K 线）
            manage_transaction: True 时本方法独立开启/提交事务；False 仅供
                                同股多周期批事务内部调用
            skip_unchanged: 覆盖写入前比较除 update_time 外的全部业务字段；
                            完全相同时不产生物理重写
            merge_missing: 仅在 overwrite=False 时生效；除插入缺失时间外，
                           还会用新数据填充已有行中的 NULL 字段，但绝不改写
                           已有非空值。日线按交易日期匹配，兼容旧库 00:00 与
                            当前规范 09:30 的时间差异。
            append_missing_only: 仅在 overwrite=False 时生效；直接插入缺失
                                 时间键，已存在时间键保持不变。用于已经由完整性
                                 规划证明为“整日缺失”的增量追加，避免无意义的
                                 UPDATE 扫描大型历史表。
            overwrite_trade_dates: 仅在 overwrite=True 时生效；分钟线按输入中
                                   出现的交易日期整日校正，用于清理根数异常日
                                   中落在输入首尾时间之外的多余时间戳。
            verified_short_trade_dates: 仅在整日覆写分钟线时生效；``{交易日: 根数}``，
                                   表示调用方已用同日日线成交量证明这些交易日
                                   根数不足是合法的（例如上午停牌）。这些交易日
                                   按证据中的根数精确核对，其余交易日仍要求满根。
            invalidate_adjustment_columns: 与本次 raw 写入在同一事务内置空的
                                           旧复权派生列；用于避免 raw 已变化但
                                           旧复权非空而被误判为完整。
            invalidate_adjustment_columns_full_history: 与本次 raw 写入在同一
                                           事务内置空该证券/周期全部历史行的
                                           指定复权列；用于前复权等全历史基准
                                           会随新增数据改变的派生值。

        Returns:
            写入记录数
        """
        self._ensure_writable("save kline data")
        if df is None or len(df) == 0:
            return 0
        if append_missing_only and overwrite:
            raise ValueError("append_missing_only 不能与 overwrite=True 同时使用")
        if append_missing_only and merge_missing:
            raise ValueError("append_missing_only 不能与 merge_missing=True 同时使用")

        table_name = self.PERIOD_TABLE_MAP.get(period)
        if not table_name:
            raise ValueError(f"不支持的周期类型: {period}")

        # 准备数据
        df_save = df.copy()

        # 首先处理索引 - MiniQMT返回的数据time通常在索引中
        if df_save.index.name == 'time' or isinstance(df_save.index, pd.DatetimeIndex):
            # QMT/xtdata may provide both an explicit ``time`` column and a
            # DatetimeIndex named ``time``.  Keep the protocol column and
            # discard only the redundant index to avoid duplicate fields.
            if 'time' in df_save.columns:
                df_save = df_save.reset_index(drop=True)
            else:
                df_save = df_save.reset_index()

        # 如果索引被重置后变成了'index'列，重命名为'time'
        if 'index' in df_save.columns and 'time' not in df_save.columns:
            df_save = df_save.rename(columns={'index': 'time'})

        # 如果还没有time列，尝试从索引创建
        if 'time' not in df_save.columns:
            df_save = df_save.reset_index()
            first_col = df_save.columns[0]
            if first_col != 'time':
                df_save = df_save.rename(columns={first_col: 'time'})

        if 'time' in df_save.columns:
            if 'time_us' in df_save.columns:
                # QMT native Tick can contain multiple different snapshots
                # in one millisecond.  Use the bridge-provided microsecond
                # persistence key; the official raw ms value remains present
                # in the API frame and no market-data field is discarded.
                df_save['time'] = coerce_market_time(df_save['time_us'])
            else:
                df_save['time'] = coerce_market_time(df_save['time'])

        df_save = self._drop_invalid_time_rows(df_save, period)
        if df_save.empty:
            logging.warning(f"{self.stock_code} {period} 所有记录的 time 都无效，已跳过保存")
            return 0
        if period == '1d':
            df_save['time'] = (
                df_save['time'].dt.normalize()
                + pd.Timedelta(hours=9, minutes=30)
            )
            # 不同来源可能分别使用 00:00 / 09:30 表示同一根日线；写入前按
            # 交易日期去重，避免一次输入内部就形成同日多行。
            df_save = (
                df_save.assign(_trade_date=df_save['time'].dt.normalize())
                .drop_duplicates(subset=['_trade_date'], keep='last')
                .drop(columns=['_trade_date'])
            )
        else:
            duplicate_mask = df_save.duplicated(subset=['time'], keep=False)
            if bool(duplicate_mask.any()):
                duplicate_values = (
                    df_save.loc[duplicate_mask, 'time']
                    .drop_duplicates()
                    .sort_values()
                    .head(8)
                    .tolist()
                )
                raise DuplicateTimestampError(
                    self.stock_code,
                    int(duplicate_mask.sum()),
                    duplicate_values,
                    period=period,
                )

        # Validate before missing schema columns are added and before any
        # DELETE + INSERT transaction can replace healthy stored history.
        validate_kline_quality(df_save, stock_code=self.stock_code, period=period)

        base_columns = [
            'time', 'open', 'high', 'low', 'close', 'volume', 'amount',
            'settelementPrice', 'openInterest', 'preClose', 'suspendFlag',
            'open_front', 'high_front', 'low_front', 'close_front',
            'open_back', 'high_back', 'low_back', 'close_back',
            'open_front_ratio', 'high_front_ratio', 'low_front_ratio', 'close_front_ratio',
            'open_back_ratio', 'high_back_ratio', 'low_back_ratio', 'close_back_ratio'
        ]
        indicator_columns = []
        if period == '1d':
            indicator_columns = list(self.DAILY_INDICATOR_COLUMNS)
        table_columns = base_columns + indicator_columns + ['dividend_type', 'update_time']

        # 字段名映射（处理可能的字段名差异）
        field_mapping = {
            'settlementPrice': 'settelementPrice',  # 可能的拼写差异
            'settlement': 'settelementPrice',
            'openInt': 'openInterest',
            'oi': 'openInterest',
            'prevClose': 'preClose',
            'lastClose': 'preClose',
        }

        # 应用字段名映射
        for old_name, new_name in field_mapping.items():
            if old_name in df_save.columns and new_name not in df_save.columns:
                df_save[new_name] = df_save[old_name]

        # 记录调用方实际提供的字段。overwrite=True 时，只有明确提供的 raw
        # 行情字段才覆盖匹配旧行；复权/指标字段则仅以非空新值覆盖。这样既
        # 保留区间覆盖语义，又不会因某个数据源只返回 raw 而清空已有扩展列。
        provided_columns = set(df_save.columns)
        invalidate_full_history_columns = list(dict.fromkeys(
            str(column).strip()
            for column in (
                invalidate_adjustment_columns_full_history or ()
            )
            if str(column).strip()
        ))
        full_history_column_set = set(invalidate_full_history_columns)
        invalidate_columns = list(dict.fromkeys(
            str(column).strip()
            for column in (invalidate_adjustment_columns or ())
            if str(column).strip()
            and str(column).strip() not in full_history_column_set
        ))
        unsafe_invalidation = [
            column
            for column in (
                invalidate_columns
                + invalidate_full_history_columns
            )
            if not column.replace('_', '').isalnum()
        ]
        if unsafe_invalidation:
            raise ValueError(
                f"非法复权失效字段: {unsafe_invalidation[0]}"
            )
        forbidden_invalidation = [
            column
            for column in (
                invalidate_columns
                + invalidate_full_history_columns
            )
            if column not in self.ADJUSTMENT_COLUMNS
        ]
        if forbidden_invalidation:
            raise ValueError(
                f"不允许事务化失效字段: {forbidden_invalidation[0]}"
            )

        if overwrite and overwrite_trade_dates:
            expected_rows = {"1d": 1, "1m": 241, "5m": 48}.get(period)
            if expected_rows is not None:
                required_raw = (
                    "open", "high", "low", "close", "volume", "amount",
                )
                missing_raw = [
                    column for column in required_raw
                    if column not in provided_columns
                ]
                if missing_raw:
                    raise ValueError(
                        "整日覆写缺少必需行情字段: " + ", ".join(missing_raw)
                    )
                valid_mask = df_save[list(required_raw)].notna().all(axis=1)
                day_stats = (
                    df_save.assign(
                        _trade_date=df_save["time"].dt.normalize(),
                        _raw_valid=valid_mask,
                    )
                    .groupby("_trade_date", sort=True)
                    .agg(
                        total_rows=("time", "nunique"),
                        valid_rows=("_raw_valid", "sum"),
                    )
                )
                verified_short = {}
                for raw_date, raw_rows in dict(verified_short_trade_dates or {}).items():
                    date_key = str(raw_date).strip().replace("-", "")[:8]
                    try:
                        allowed_rows = int(raw_rows)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ValueError(
                            "verified_short_trade_dates 必须是 {交易日: 根数}"
                        ) from exc
                    if (
                        len(date_key) == 8
                        and date_key.isdigit()
                        and 0 < allowed_rows < expected_rows
                    ):
                        verified_short[date_key] = allowed_rows
                invalid_days = []
                for trade_date, stats in day_stats.iterrows():
                    date_key = pd.Timestamp(trade_date).strftime("%Y%m%d")
                    total_rows = int(stats["total_rows"])
                    valid_rows = int(stats["valid_rows"])
                    # 默认要求整日根数精确。调用方用同日日线成交量证明了合法缺根
                    # （例如跨境 ETF 上午停牌到 10:30，5m 只有 36 根）的交易日，
                    # 按证据中的根数精确核对；根数对不上仍然拒绝。
                    allowed_rows = expected_rows
                    if period != "1d" and date_key in verified_short:
                        allowed_rows = verified_short[date_key]
                    if total_rows != allowed_rows or valid_rows != allowed_rows:
                        invalid_days.append((
                            pd.Timestamp(trade_date).strftime("%Y-%m-%d"),
                            total_rows,
                            valid_rows,
                            allowed_rows,
                        ))
                if invalid_days:
                    preview = "；".join(
                        f"{date_value} {total}/{allowed} 根（有效 {valid}）"
                        for date_value, total, valid, allowed in invalid_days[:5]
                    )
                    # 整日覆写会先按交易日删除旧数据再写入。若数据源本次返回的
                    # 某个交易日根数不足（大 QMT ContextInfo 通道已知会偶发缺失
                    # 分钟线），放行等于用残缺数据覆盖掉库里完好的那一天。这里
                    # 必须 fail-closed：调用方的 validate_overwrite_frame 是第一
                    # 道闸，本检查是最后一道兜底，两道都不能降级成告警。
                    raise ValueError(
                        f"{self.stock_code} {period} 拒绝整日覆写：数据源返回的交易日不完整，"
                        "原数据未改动。" + preview
                    )

        # 确保所有需要的列都存在，缺失的填充None
        for col in table_columns:
            if col not in df_save.columns:
                if col == 'dividend_type':
                    df_save[col] = dividend_type
                elif col == 'update_time':
                    df_save[col] = datetime.now()
                else:
                    df_save[col] = None

        # 只保留表结构中的列，并按照表结构顺序排列
        df_save = df_save[table_columns]
        df_save = self._normalize_string_dtypes_for_duckdb(df_save)

        # 确保time列是datetime类型
        if not pd.api.types.is_datetime64_any_dtype(df_save['time']):
            df_save['time'] = pd.to_datetime(df_save['time'], errors='coerce')

        columns_to_insert = table_columns
        df_to_insert = df_save
        columns_str = ', '.join(columns_to_insert)

        # 预计算时间范围（DELETE + INSERT 需要）。日线必须按整日删除，
        # 不能让不同数据源的 00:00/09:30 时间差留下同日重复记录。
        min_time = pd.Timestamp(df_save['time'].min()).to_pydatetime()
        max_time = pd.Timestamp(df_save['time'].max()).to_pydatetime()
        if period == '1d':
            delete_start = pd.Timestamp(min_time).normalize().to_pydatetime()
            delete_end_exclusive = (
                pd.Timestamp(max_time).normalize() + pd.Timedelta(days=1)
            ).to_pydatetime()
            target_range_predicate = (
                "target.time >= ? AND target.time < ?"
            )
            target_range_params = [delete_start, delete_end_exclusive]
        else:
            delete_start = min_time
            delete_end_exclusive = None
            target_range_predicate = (
                "target.time >= ? AND target.time <= ?"
            )
            target_range_params = [min_time, max_time]

        # direct-range 通常只覆盖一个交易日。所有与目标表的匹配都显式附加
        # 时间范围，让 DuckDB 的 zonemap 能跳过历史 row group；否则即使来源
        # 只有 241 根 1m K 线，UPDATE ... FROM 仍可能扫描整张多年分钟表。
        target_range_join = f"({target_range_predicate})"

        merge_columns = [
            column for column in columns_to_insert
            if column not in ('time', 'update_time')
            and df_save[column].notna().any()
        ]
        merge_assignments = ', '.join(
            f'"{column}" = COALESCE(target."{column}", source."{column}")'
            for column in merge_columns
        )
        # 增量只补空值；不要把已有的多年行情列写成同一个值。
        # 同时排除来源全空的列，减少 UPDATE 读取的历史列和 WAL 写放大。
        merge_changed_predicate = ' OR '.join(
            f'(target."{column}" IS NULL AND source."{column}" IS NOT NULL)'
            for column in merge_columns
        )
        if period == '1d':
            merge_match = "CAST(target.time AS DATE) = CAST(source.time AS DATE)"
        else:
            merge_match = "target.time = source.time"

        def _find_price_changed_input_rows(input_frame):
            """在写入前找出实际新增或 OHLC 发生变化的输入时间键。"""

            price_columns = [
                column for column in self.RAW_PRICE_COLUMNS
                if column in provided_columns
            ]

            # replacement scan 仅解析当前栈帧局部变量。
            df_to_compare = input_frame
            if not price_columns:
                # 即使调用方只提供 time + 量额，也必须识别真正的新时间键；
                # 已有时间键的量额补齐仍不会触发任何复权失效。
                price_changed_sql = "FALSE"
            elif overwrite:
                price_changed_sql = " OR ".join(
                    f'(TRY_CAST(source."{column}" AS DOUBLE) '
                    f'IS DISTINCT FROM target."{column}")'
                    for column in price_columns
                )
            elif merge_missing:
                price_changed_sql = " OR ".join(
                    f'(target."{column}" IS NULL AND '
                    f'TRY_CAST(source."{column}" AS DOUBLE) IS NOT NULL)'
                    for column in price_columns
                )
            else:
                # INSERT ... DO NOTHING 不会改变已存在时间键的价格。
                price_changed_sql = "FALSE"
            changed = self.conn.execute(f"""
                SELECT source.time
                FROM df_to_compare AS source
                LEFT JOIN {table_name} AS target
                  ON {merge_match} AND {target_range_join}
                WHERE target.time IS NULL OR ({price_changed_sql})
            """, target_range_params).fetchdf()
            if changed.empty:
                return changed[["time"]].copy()

            changed["time"] = pd.to_datetime(changed["time"], errors="coerce")
            changed = changed.dropna(subset=["time"])
            # ``df_to_compare`` has already crossed the input duplicate gate
            # above and non-daily tables use ``time`` as their physical key.
            # Keep this derived query honest as well: if a malformed/legacy
            # target creates two matches, do not hide the ambiguity with a
            # last-row de-duplication.  Daily matching intentionally remains
            # date-based because 00:00/09:30 are historical aliases.
            if period != '1d':
                duplicate_mask = changed.duplicated(subset=['time'], keep=False)
                if bool(duplicate_mask.any()):
                    duplicate_values = (
                        changed.loc[duplicate_mask, 'time']
                        .drop_duplicates()
                        .sort_values()
                        .head(8)
                        .tolist()
                    )
                    raise DuplicateTimestampError(
                        self.stock_code,
                        int(duplicate_mask.sum()),
                        duplicate_values,
                        period=period,
                    )
            else:
                changed = changed.drop_duplicates(subset=['time'], keep='last')
            return changed[["time"]].copy()

        def _invalidate_adjustments_for_input(input_frame):
            if not invalidate_columns:
                return
            # DuckDB 的 DataFrame replacement scan 只可靠解析当前 Python
            # 栈帧中的局部变量；不要依赖外层闭包捕获的同名对象。
            df_to_insert = input_frame
            assignments = ', '.join(
                f'"{column}" = NULL' for column in invalidate_columns
            )
            self.conn.execute(f"""
                UPDATE {table_name} AS target
                SET {assignments}, update_time = CURRENT_TIMESTAMP
                FROM df_to_insert AS source
                WHERE {merge_match} AND {target_range_predicate}
            """, target_range_params)

        def _invalidate_adjustments_full_history():
            if not invalidate_full_history_columns:
                return
            assignments = ', '.join(
                f'"{column}" = NULL'
                for column in invalidate_full_history_columns
            )
            self.conn.execute(f"""
                UPDATE {table_name}
                SET {assignments}, update_time = CURRENT_TIMESTAMP
            """)

        def _invalidate_adjustments_after_write(changed_input_frame):
            # 必须与 raw 的 UPDATE/INSERT 位于同一事务。全历史口径先整体
            # 标记待补，再处理仅与本次输入时间匹配的其它派生列；任一步
            # 失败都会连同 raw 一起回滚。
            if changed_input_frame.empty:
                return
            _invalidate_adjustments_full_history()
            _invalidate_adjustments_for_input(changed_input_frame)

        overwrite_assignments = []
        for column in self.RAW_KLINE_COLUMNS:
            if column in provided_columns:
                overwrite_assignments.append(
                    f'"{column}" = source."{column}"'
                )
        extension_columns = list(self.ADJUSTMENT_COLUMNS) + indicator_columns
        overwrite_assignments.extend(
            f'"{column}" = COALESCE(source."{column}", target."{column}")'
            for column in extension_columns
        )
        if 'dividend_type' in provided_columns:
            overwrite_assignments.append(
                '"dividend_type" = COALESCE('
                'source."dividend_type", target."dividend_type")'
            )
        overwrite_assignments.append('"update_time" = source."update_time"')
        overwrite_assignments_sql = ', '.join(overwrite_assignments)

        def _write_rows(input_frame):
            # DuckDB 的 DataFrame replacement scan 只检查当前 Python 栈帧的
            # 局部变量，不会解析闭包；保留这个显式局部名供 SQL 引用。
            df_to_insert = input_frame
            ensure_kline_storage_units(self.conn, table_name, df_to_insert, self.stock_code)
            if (
                invalidate_columns
                or invalidate_full_history_columns
            ):
                changed_input_frame = _find_price_changed_input_rows(
                    df_to_insert
                )
            else:
                changed_input_frame = df_to_insert.iloc[0:0][["time"]].copy()
            if period == '1d':
                self._normalize_daily_timestamps_in_transaction(table_name)
            if append_missing_only:
                self.conn.execute(f"""
                    INSERT INTO {table_name} ({columns_str})
                    SELECT {columns_str} FROM df_to_insert
                    ON CONFLICT (time) DO NOTHING
                """)
                return
            if overwrite:
                # 匹配行原位更新，避免 DELETE + INSERT 抹掉数据库中未包含在
                # 标准 schema 内的 khDuckWrite 自定义字段。raw 字段保持明确
                # 覆盖语义；复权和指标字段仅在来源非空时替换旧值。
                self.conn.execute(f"""
                    UPDATE {table_name} AS target
                    SET {overwrite_assignments_sql}
                    FROM df_to_insert AS source
                    WHERE {merge_match} AND {target_range_predicate}
                """, target_range_params)

                # overwrite 仍表示输入最小/最大时间区间的完整替换：删除区间
                # 内不再出现在本次输入中的旧 K 线。以上 UPDATE、这里的 DELETE
                # 与后续 INSERT 均处于同一事务，任一步失败都会整体回滚。
                if period == '1d':
                    self.conn.execute(f"""
                        DELETE FROM {table_name} AS target
                        WHERE target.time >= ? AND target.time < ?
                          AND NOT EXISTS (
                              SELECT 1 FROM df_to_insert AS source
                              WHERE CAST(target.time AS DATE)
                                    = CAST(source.time AS DATE)
                          )
                    """, [delete_start, delete_end_exclusive])
                elif overwrite_trade_dates:
                    trade_date_start = (
                        pd.Timestamp(min_time).normalize().to_pydatetime()
                    )
                    trade_date_end = (
                        pd.Timestamp(max_time).normalize()
                        + pd.Timedelta(days=1)
                    ).to_pydatetime()
                    self.conn.execute(f"""
                        DELETE FROM {table_name} AS target
                        WHERE target.time >= ? AND target.time < ?
                          AND CAST(target.time AS DATE) IN (
                            SELECT DISTINCT CAST(source.time AS DATE)
                            FROM df_to_insert AS source
                        )
                          AND NOT EXISTS (
                              SELECT 1 FROM df_to_insert AS source
                              WHERE target.time = source.time
                          )
                    """, [trade_date_start, trade_date_end])
                else:
                    self.conn.execute(f"""
                        DELETE FROM {table_name} AS target
                        WHERE target.time >= ? AND target.time <= ?
                          AND NOT EXISTS (
                              SELECT 1 FROM df_to_insert AS source
                              WHERE target.time = source.time
                          )
                    """, [min_time, max_time])
                self.conn.execute(f"""
                    INSERT INTO {table_name} ({columns_str})
                    SELECT {columns_str}
                    FROM df_to_insert AS source
                    WHERE NOT EXISTS (
                        SELECT 1 FROM {table_name} AS target
                        WHERE {merge_match} AND {target_range_predicate}
                    )
                """, target_range_params)
                _invalidate_adjustments_after_write(changed_input_frame)
                return

            if merge_missing:
                # 小范围补数常是整段新日期。必须在当前写事务内证明目标区间
                # 为空，再走普通 INSERT；无需执行空 UPDATE、反连接和冲突
                # 处理。主键约束仍由 DuckDB 校验，不能以库外规划代替此证明。
                range_exists = self.conn.execute(
                    f"SELECT 1 FROM {table_name} AS target "
                    f"WHERE {target_range_predicate} LIMIT 1",
                    target_range_params,
                ).fetchone()
                if range_exists is None:
                    self.conn.execute(f"""
                        INSERT INTO {table_name} ({columns_str})
                        SELECT {columns_str} FROM df_to_insert
                    """)
                    _invalidate_adjustments_after_write(changed_input_frame)
                    return
                if merge_assignments:
                    self.conn.execute(f"""
                        UPDATE {table_name} AS target
                        SET {merge_assignments}
                        FROM df_to_insert AS source
                        WHERE {merge_match} AND {target_range_predicate}
                          AND ({merge_changed_predicate})
                    """, target_range_params)
                self.conn.execute(f"""
                    INSERT INTO {table_name} ({columns_str})
                    SELECT {columns_str}
                    FROM df_to_insert AS source
                    WHERE NOT EXISTS (
                        SELECT 1 FROM {table_name} AS target
                        WHERE {merge_match} AND {target_range_predicate}
                    )
                    ON CONFLICT (time) DO NOTHING
                """, target_range_params)
                _invalidate_adjustments_after_write(changed_input_frame)
                return

            if period == '1d':
                self.conn.execute(f"""
                    INSERT INTO {table_name} ({columns_str})
                    SELECT {columns_str}
                    FROM df_to_insert AS source
                    WHERE NOT EXISTS (
                        SELECT 1 FROM {table_name} AS target
                        WHERE CAST(target.time AS DATE) = CAST(source.time AS DATE)
                    )
                    ON CONFLICT (time) DO NOTHING
                """)
            else:
                self.conn.execute(f"""
                    INSERT INTO {table_name} ({columns_str})
                    SELECT {columns_str} FROM df_to_insert
                    ON CONFLICT (time) DO NOTHING
                """)
            _invalidate_adjustments_after_write(changed_input_frame)

        try:
            # 预热连接
            _ = self.conn
            # 乐观路径: 新数据库先创建表; 已有数据库跳过 schema 检查直接写入
            if self._is_new_db and table_name not in self._created_tables:
                self._ensure_period_table(period)

            # 表主键为 time（单列）。overwrite=True 时精确替换输入区间，
            # 但匹配行原位更新以保留已有扩展字段；overwrite=False 时仅补入
            # 缺失时间（增量），已存在的行保持不变。
            if manage_transaction:
                self.conn.execute("BEGIN TRANSACTION")
            if overwrite:
                if (
                    skip_unchanged
                    and not invalidate_columns
                    and not invalidate_full_history_columns
                    and self._incoming_rows_unchanged(
                    table_name,
                    df_to_insert,
                    [column for column in columns_to_insert if column != 'update_time'],
                    exact_range=True,
                    )
                ):
                    if manage_transaction:
                        self.conn.execute("COMMIT")
                    return len(df_save)
            _write_rows(df_to_insert)
            if manage_transaction:
                self.conn.execute("COMMIT")

        except Exception as _first_err:
            # 文件锁冲突与表结构无关，不能进入 schema 修复分支重复制造异常。
            if is_duckdb_lock_error(_first_err):
                raise
            # 外层批事务由调用方统一回滚并退回逐周期保存；此处不能修复
            # schema 或操作外层事务，否则会破坏其他周期的原子性。
            if not manage_transaction:
                raise
            # 回滚可能未完成的事务
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            if isinstance(_first_err, VolumeUnitError):
                raise
            # 表或列缺失 -> 完整 schema 检查修复后重试
            self._ensure_period_table(period)
            self._ensure_table_columns(table_name, table_columns, period=period)

            try:
                self.conn.execute("BEGIN TRANSACTION")
                _write_rows(df_to_insert)
                self.conn.execute("COMMIT")
            except Exception as _retry_err:
                try:
                    self.conn.execute("ROLLBACK")
                except Exception:
                    pass
                logging.error(f"保存K线数据失败 {self.stock_code}: {_retry_err}")
                raise

        return len(df_save)

    def _normalize_daily_timestamps_in_transaction(self, table_name: str) -> int:
        """在当前事务中把既有日线统一到 09:30，并合并同日重复行。"""

        stats = self.conn.execute(f"""
            SELECT
                COUNT(*) AS total_rows,
                COUNT(DISTINCT CAST(time AS DATE)) AS distinct_dates,
                COUNT(*) FILTER (
                    WHERE CAST(time AS TIME) <> TIME '09:30:00'
                ) AS noncanonical_rows
            FROM {table_name}
            WHERE time IS NOT NULL
        """).fetchone()
        total_rows = int((stats or (0, 0, 0))[0] or 0)
        distinct_dates = int((stats or (0, 0, 0))[1] or 0)
        noncanonical_rows = int((stats or (0, 0, 0))[2] or 0)
        if total_rows <= 0 or (
            total_rows == distinct_dates and noncanonical_rows == 0
        ):
            return 0

        schema_rows = self.conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = ?
            ORDER BY ordinal_position
            """,
            [table_name],
        ).fetchall()
        columns = [str(row[0]) for row in schema_rows]
        if 'time' not in columns:
            return 0

        select_parts = []
        for column in columns:
            quoted = f'"{column}"'
            if column == 'time':
                select_parts.append(
                    "CAST(time AS DATE) + INTERVAL '9 hours 30 minutes' AS \"time\""
                )
            elif column == 'update_time':
                select_parts.append(f'MAX({quoted}) AS {quoted}')
            else:
                # arg_max 忽略 NULL arg；同日两行时优先保留较晚时间点的
                # 非空值，同时能从旧 00:00 行补回新行缺失的扩展字段。
                select_parts.append(f'ARG_MAX({quoted}, time) AS {quoted}')

        temp_table = '_kh_daily_time_canonical'
        quoted_columns = ', '.join(f'"{column}"' for column in columns)
        self.conn.execute(f'DROP TABLE IF EXISTS {temp_table}')
        self.conn.execute(f"""
            CREATE TEMP TABLE {temp_table} AS
            SELECT {', '.join(select_parts)}
            FROM {table_name}
            WHERE time IS NOT NULL
            GROUP BY CAST(time AS DATE)
        """)
        canonical_rows = int(
            self.conn.execute(f'SELECT COUNT(*) FROM {temp_table}').fetchone()[0]
            or 0
        )
        self.conn.execute(f'DELETE FROM {table_name}')
        self.conn.execute(f"""
            INSERT INTO {table_name} ({quoted_columns})
            SELECT {quoted_columns} FROM {temp_table}
        """)
        self.conn.execute(f'DROP TABLE {temp_table}')
        return canonical_rows

    def normalize_daily_timestamps(self) -> int:
        """把旧库日线时间统一迁移为 09:30；无变化时为只读检查。"""

        self._ensure_writable("normalize daily timestamps")
        _ = self.conn
        table_name = self.PERIOD_TABLE_MAP['1d']
        exists = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table_name],
        ).fetchone()
        if not exists or not int(exists[0] or 0):
            return 0
        try:
            self.conn.execute("BEGIN TRANSACTION")
            migrated = self._normalize_daily_timestamps_in_transaction(table_name)
            self.conn.execute("COMMIT")
            return migrated
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def update_daily_indicators(
        self,
        df: pd.DataFrame,
        columns: Optional[List[str]] = None,
    ) -> int:
        """只更新已存在日线的指标列，不创建新的行情日期。"""
        self._ensure_writable("update daily indicators")
        if df is None or df.empty:
            return 0

        allowed = set(self.DAILY_INDICATOR_COLUMNS)
        requested = list(self.DAILY_INDICATOR_COLUMNS) if columns is None else list(columns)
        selected = list(dict.fromkeys(
            col for col in requested if col in allowed and col in df.columns
        ))
        if not selected:
            return 0

        indicator_updates = df.copy()
        if indicator_updates.index.name == 'time' or isinstance(
            indicator_updates.index, pd.DatetimeIndex
        ):
            indicator_updates = indicator_updates.reset_index()
        if 'index' in indicator_updates.columns and 'time' not in indicator_updates.columns:
            indicator_updates = indicator_updates.rename(columns={'index': 'time'})
        if 'time' not in indicator_updates.columns:
            return 0

        indicator_updates['time'] = coerce_market_time(indicator_updates['time'])
        indicator_updates = self._drop_invalid_time_rows(indicator_updates, '1d')
        if indicator_updates.empty:
            return 0

        for col in selected:
            indicator_updates[col] = pd.to_numeric(indicator_updates[col], errors='coerce')
        indicator_updates = (
            indicator_updates[['time'] + selected]
            .dropna(subset=selected, how='all')
            .assign(_trade_date=lambda frame: frame['time'].dt.normalize())
            .drop_duplicates(subset=['_trade_date'], keep='last')
            .drop(columns=['_trade_date'])
            .sort_values('time')
        )
        if indicator_updates.empty:
            return 0
        indicator_updates = self._normalize_string_dtypes_for_duckdb(indicator_updates)

        table_name = self.PERIOD_TABLE_MAP['1d']
        _ = self.conn
        self._ensure_period_table('1d')
        self._ensure_table_columns(
            table_name,
            ['time'] + list(self.DAILY_INDICATOR_COLUMNS) + ['update_time'],
            period='1d',
        )

        assignments = ', '.join(
            f"{col} = COALESCE(source.{col}, target.{col})" for col in selected
        )
        try:
            self.conn.execute("BEGIN TRANSACTION")
            matched = int(self.conn.execute(
                f"""
                SELECT COUNT(*)
                FROM {table_name} AS target
                INNER JOIN indicator_updates AS source
                    ON CAST(target.time AS DATE) = CAST(source.time AS DATE)
                """
            ).fetchone()[0])
            if matched > 0:
                self.conn.execute(f"""
                    UPDATE {table_name} AS target
                    SET {assignments}, update_time = CURRENT_TIMESTAMP
                    FROM indicator_updates AS source
                    WHERE CAST(target.time AS DATE) = CAST(source.time AS DATE)
                """)
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

        return matched

    def update_kline_columns(
        self,
        df: pd.DataFrame,
        period: str,
        columns: List[str],
    ) -> int:
        """只更新已有 K 线的指定非空字段，不改写其他行情列。

        用于从本地原始价重算前/后复权列。日线按交易日期匹配，以兼容
        BaoStock 旧版 00:00 和当前统一的 09:30 时间戳。
        """

        self._ensure_writable("update kline columns")
        if df is None or df.empty:
            return 0
        table_name = self.PERIOD_TABLE_MAP.get(period)
        if not table_name or period == 'tick':
            raise ValueError(f"不支持列更新的周期: {period}")

        updates = df.copy()
        if updates.index.name == 'time' or isinstance(updates.index, pd.DatetimeIndex):
            updates = updates.reset_index()
        if 'index' in updates.columns and 'time' not in updates.columns:
            updates = updates.rename(columns={'index': 'time'})
        if 'time' not in updates.columns:
            return 0

        selected = list(dict.fromkeys(
            str(column).strip() for column in (columns or [])
            if str(column).strip() and str(column).strip() != 'time'
        ))
        if not selected:
            return 0
        if any(not column.replace('_', '').isalnum() for column in selected):
            raise ValueError("包含非法行情字段名")
        selected = [column for column in selected if column in updates.columns]
        if not selected:
            return 0

        updates['time'] = coerce_market_time(updates['time'])
        updates = self._drop_invalid_time_rows(updates, period)
        if updates.empty:
            return 0
        for column in selected:
            updates[column] = pd.to_numeric(updates[column], errors='coerce')
        updates = updates[['time'] + selected].dropna(subset=selected, how='all')
        if period == '1d':
            updates = (
                updates.assign(_trade_date=lambda frame: frame['time'].dt.normalize())
                .drop_duplicates(subset=['_trade_date'], keep='last')
                .drop(columns=['_trade_date'])
            )
            match = "CAST(target.time AS DATE) = CAST(source.time AS DATE)"
        else:
            duplicate_mask = updates.duplicated(subset=['time'], keep=False)
            if bool(duplicate_mask.any()):
                duplicate_values = (
                    updates.loc[duplicate_mask, 'time']
                    .drop_duplicates()
                    .sort_values()
                    .head(8)
                    .tolist()
                )
                raise DuplicateTimestampError(
                    self.stock_code,
                    int(duplicate_mask.sum()),
                    duplicate_values,
                    period=period,
                )
            match = "target.time = source.time"
        if updates.empty:
            return 0
        updates = self._normalize_string_dtypes_for_duckdb(updates)

        _ = self.conn
        self._ensure_period_table(period)
        self._ensure_table_columns(
            table_name,
            ['time'] + selected + ['update_time'],
            period=period,
        )
        assignments = ', '.join(
            f'"{column}" = COALESCE(source."{column}", target."{column}")'
            for column in selected
        )
        try:
            self.conn.execute("BEGIN TRANSACTION")
            matched = int(self.conn.execute(f"""
                SELECT COUNT(*)
                FROM {table_name} AS target
                INNER JOIN updates AS source ON {match}
            """).fetchone()[0])
            if matched > 0:
                self.conn.execute(f"""
                    UPDATE {table_name} AS target
                    SET {assignments}, update_time = CURRENT_TIMESTAMP
                    FROM updates AS source
                    WHERE {match}
                """)
            self.conn.execute("COMMIT")
            return matched
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def clear_kline_columns(
        self,
        period: str,
        columns: List[str],
        start_time: str = None,
        end_time: str = None,
    ) -> int:
        """事务化地把指定派生 K 线列置为 NULL。

        该接口用于复权/指标生成失败后显式标记“待补”。只允许清理
        ``CLEARABLE_KLINE_COLUMNS`` 中且目标表真实存在的列；不会创建表或
        补列。日线范围按交易日期匹配，分钟线按精确时间范围匹配。
        返回实际从非 NULL 变为 NULL 的行数。
        """

        self._ensure_writable("clear kline columns")
        if period == 'tick':
            raise ValueError("tick 不支持清空 K 线列")
        table_name = self.PERIOD_TABLE_MAP.get(period)
        if period not in ('1d', '1m', '5m') or not table_name:
            raise ValueError(f"不支持清空列的周期: {period}")

        requested = [columns] if isinstance(columns, str) else list(columns or [])
        selected = list(dict.fromkeys(
            str(column).strip() for column in requested
            if str(column).strip()
        ))
        if not selected:
            return 0

        unsafe = [
            column for column in selected
            if not column.replace('_', '').isalnum()
        ]
        if unsafe:
            raise ValueError(f"非法行情字段名: {unsafe[0]}")
        forbidden = [
            column for column in selected
            if column not in self.CLEARABLE_KLINE_COLUMNS
        ]
        if forbidden:
            raise ValueError(f"不允许清空行情字段: {forbidden[0]}")

        _ = self.conn
        exists = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table_name],
        ).fetchone()
        if not exists or not int(exists[0] or 0):
            return 0

        schema_rows = self.conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = ?
            """,
            [table_name],
        ).fetchall()
        actual_columns = {str(row[0]) for row in schema_rows}
        missing = [column for column in selected if column not in actual_columns]
        if missing:
            raise ValueError(
                f"行情表 {table_name} 不存在字段: {missing[0]}"
            )

        predicates = []
        params = []
        parsed_start = None
        parsed_end = None
        if start_time is not None and str(start_time).strip():
            parsed_start = self._parse_time(start_time)
            if period == '1d':
                predicates.append("CAST(time AS DATE) >= CAST(? AS DATE)")
            else:
                predicates.append("time >= ?")
            params.append(parsed_start)
        if end_time is not None and str(end_time).strip():
            parsed_end = self._parse_time(end_time, is_end_time=True)
            if period == '1d':
                predicates.append("CAST(time AS DATE) <= CAST(? AS DATE)")
            else:
                predicates.append("time <= ?")
            params.append(parsed_end)
        if parsed_start is not None and parsed_end is not None:
            if pd.Timestamp(parsed_start) > pd.Timestamp(parsed_end):
                raise ValueError("开始时间不能晚于结束时间")

        # 已经为 NULL 的行不算受影响，也不进行无意义 UPDATE。
        non_null = " OR ".join(
            f'"{column}" IS NOT NULL' for column in selected
        )
        predicates.append(f"({non_null})")
        where_sql = " AND ".join(predicates)
        assignments = ", ".join(
            f'"{column}" = NULL' for column in selected
        )

        try:
            self.conn.execute("BEGIN TRANSACTION")
            affected = int(self.conn.execute(
                f'SELECT COUNT(*) FROM "{table_name}" WHERE {where_sql}',
                params,
            ).fetchone()[0])
            if affected > 0:
                self.conn.execute(
                    f'UPDATE "{table_name}" SET {assignments} WHERE {where_sql}',
                    params,
                )
            self.conn.execute("COMMIT")
            return affected
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def checkpoint(self):
        """显式执行CHECKPOINT，将WAL数据写入磁盘"""
        if self.read_only:
            return
        if self._conn is not None:
            try:
                self._conn.execute("CHECKPOINT")
            except Exception:
                pass

    def save_tick(
        self,
        df: pd.DataFrame,
        manage_transaction: bool = True,
        skip_unchanged: bool = False,
        append_missing_only: bool = False,
    ) -> int:
        """
        保存Tick数据

        Args:
            df: Tick数据DataFrame
            manage_transaction: True时本方法独立管理事务；False仅供
                                同股批事务内部调用
            skip_unchanged: 业务字段完全相同时不重复删除和写入
            append_missing_only: 只插入缺失时间戳，已有 Tick 保持不变；适合
                                 当日增量追加，避免对整张历史 Tick 表执行删除

        Returns:
            写入记录数
        """
        self._ensure_writable("save tick data")
        if df is None or len(df) == 0:
            return 0

        # 准备数据
        df_save = df.copy()

        # 处理索引
        if df_save.index.name == 'time' or isinstance(df_save.index, pd.DatetimeIndex):
            # Keep an explicit QMT ``time`` column authoritative when the
            # returned frame also carries a same-named DatetimeIndex.
            if 'time' in df_save.columns:
                df_save = df_save.reset_index(drop=True)
            else:
                df_save = df_save.reset_index()

        if 'index' in df_save.columns and 'time' not in df_save.columns:
            df_save = df_save.rename(columns={'index': 'time'})

        if 'time' not in df_save.columns:
            df_save = df_save.reset_index()
            first_col = df_save.columns[0]
            if first_col != 'time':
                df_save = df_save.rename(columns={first_col: 'time'})

        if 'time' in df_save.columns:
            if 'time_us' in df_save.columns:
                df_save['time'] = coerce_market_time(df_save['time_us'])
            else:
                df_save['time'] = coerce_market_time(df_save['time'])

        df_save['update_time'] = datetime.now()

        # 处理askPrice和bidPrice数组字段（MiniQMT返回的是数组）
        for prefix in ['askPrice', 'bidPrice', 'askVol', 'bidVol']:
            if prefix in df_save.columns:
                # 如果是数组列，展开为5个独立列
                arr_col = df_save[prefix]
                if arr_col.dtype == 'object' and len(arr_col) > 0:
                    try:
                        raw_list = arr_col.tolist()
                        mat = np.asarray(raw_list, dtype=object)
                        if mat.ndim == 2 and mat.shape[1] >= 5:
                            for i in range(5):
                                df_save[f'{prefix}{i+1}'] = mat[:, i]
                        else:
                            for i in range(5):
                                df_save[f'{prefix}{i+1}'] = [
                                    x[i] if isinstance(x, (list, tuple)) and len(x) > i else None
                                    for x in raw_list
                                ]
                    except Exception:
                        for i in range(5):
                            df_save[f'{prefix}{i+1}'] = arr_col.apply(
                                lambda x: x[i] if isinstance(x, (list, tuple)) and len(x) > i else None
                            )
                df_save = df_save.drop(columns=[prefix], errors='ignore')

        # Tick表完整字段列表
        table_columns = [
            'time', 'lastPrice', 'open', 'high', 'low', 'lastClose',
            'amount', 'volume', 'pvolume', 'stockStatus', 'openInt',
            'lastSettlementPrice',
            'askPrice1', 'askPrice2', 'askPrice3', 'askPrice4', 'askPrice5',
            'bidPrice1', 'bidPrice2', 'bidPrice3', 'bidPrice4', 'bidPrice5',
            'askVol1', 'askVol2', 'askVol3', 'askVol4', 'askVol5',
            'bidVol1', 'bidVol2', 'bidVol3', 'bidVol4', 'bidVol5',
            'transactionNum', 'update_time'
        ]

        # 确保所有需要的列都存在
        for col in table_columns:
            if col not in df_save.columns:
                if col == 'update_time':
                    pass
                else:
                    df_save[col] = None

        # 只保留表结构中的列
        df_save = df_save[[c for c in table_columns if c in df_save.columns]]
        df_save = self._normalize_string_dtypes_for_duckdb(df_save)

        # 确保time列是datetime类型
        if not pd.api.types.is_datetime64_any_dtype(df_save['time']):
            df_save['time'] = pd.to_datetime(df_save['time'], errors='coerce')

        df_save = self._drop_invalid_time_rows(df_save, 'tick')
        if df_save.empty:
            logging.warning(f"{self.stock_code} tick 所有记录的 time 都无效，已跳过保存")
            return 0

        # 单列 time 是既有回测协议的一部分。不能再像旧实现那样静默
        # drop_duplicates：同一时间戳可能代表两条不同的盘口快照，丢弃一条
        # 会造成不可逆的数据缺口。适配器若能保留更细粒度时间应在此之前
        # 完成；到达这里仍冲突就整批拒绝，事务不会被开启/不会改库。
        duplicate_mask = df_save.duplicated(subset=['time'], keep=False)
        if bool(duplicate_mask.any()):
            duplicate_values = (
                df_save.loc[duplicate_mask, 'time']
                .drop_duplicates()
                .sort_values()
                .head(8)
                .tolist()
            )
            raise DuplicateTimestampError(
                self.stock_code,
                int(duplicate_mask.sum()),
                duplicate_values,
            )

        transaction_started = False
        try:
            # 按需创建 tick 表
            self._ensure_period_table('tick')

            # DELETE + INSERT 必须原子化。旧逻辑在 INSERT 失败时
            # 可能已经删除原有 Tick，造成数据丢失。
            if manage_transaction:
                self.conn.execute("BEGIN TRANSACTION")
                transaction_started = True

            if skip_unchanged and self._incoming_rows_unchanged(
                'tick',
                df_save,
                [column for column in table_columns if column != 'update_time'],
                exact_range=False,
            ):
                if manage_transaction:
                    self.conn.execute("COMMIT")
                    transaction_started = False
                return len(df_save)

            if append_missing_only:
                self.conn.execute("""
                    INSERT INTO tick
                    SELECT * FROM df_save
                    ON CONFLICT (time) DO NOTHING
                """)
            else:
                has_existing = bool(self.conn.execute("SELECT 1 FROM tick LIMIT 1").fetchone())
                if has_existing:
                    # 获取所有要插入的时间点
                    times_to_insert = df_save['time'].tolist()
                    if times_to_insert:
                        self.conn.execute("""
                            DELETE FROM tick
                            WHERE time IN (SELECT UNNEST(?::TIMESTAMP[]))
                        """, [times_to_insert])

                self.conn.execute("""
                    INSERT INTO tick
                    SELECT * FROM df_save
                """)

            if manage_transaction:
                self.conn.execute("COMMIT")
                transaction_started = False

            # CHECKPOINT 已移至批量导入结束后统一执行，不再逐次写盘

            return len(df_save)
        except Exception as e:
            if manage_transaction and transaction_started:
                try:
                    self.conn.execute("ROLLBACK")
                except Exception:
                    pass
            logging.error(f"保存Tick数据失败 {self.stock_code}: {e}")
            raise

    def get_kline(
        self,
        period: str,
        start_time: str = None,
        end_time: str = None,
        dividend_type: str = None,
        fields: List[str] = None
    ) -> pd.DataFrame:
        """
        读取K线数据

        Args:
            period: 周期类型
            start_time: 开始时间 (YYYYMMDD 或 YYYY-MM-DD)
            end_time: 结束时间
            dividend_type: 复权类型筛选
            fields: 需要的字段列表，None表示获取所有字段

        Returns:
            K线数据DataFrame
        """
        table_name = self.PERIOD_TABLE_MAP.get(period)
        if not table_name:
            return pd.DataFrame()

        # 只读回测热路径可跳过 DDL 检查；缺表时在查询异常里返回空 DataFrame。
        if get_duckdb_read_ensure_tables():
            self._ensure_period_table(period)

        # 构建字段 - 默认获取所有字段（不含内部字段）
        if fields:
            field_str = ', '.join(['time'] + [f for f in fields if f != 'time'])
        else:
            # 获取所有业务字段
            field_str = '*'

        # 构建查询
        sql = f"SELECT {field_str} FROM {table_name} WHERE 1=1"
        params = []

        if start_time:
            sql += " AND time >= ?"
            params.append(self._parse_time(start_time))

        if end_time:
            sql += " AND time <= ?"
            params.append(self._parse_time(end_time, is_end_time=True))

        if dividend_type:
            sql += " AND dividend_type = ?"
            params.append(dividend_type)

        order_mode = get_duckdb_order_mode()
        if order_mode == "sql_order_by":
            sql += " ORDER BY time"

        try:
            df = self.conn.execute(sql, params).fetchdf()
            if order_mode == "verify_after_fetch" and "time" in df.columns and not df["time"].is_monotonic_increasing:
                df = df.sort_values("time").reset_index(drop=True)
            # 移除内部字段
            internal_cols = ['dividend_type', 'update_time']
            for col in internal_cols:
                if col in df.columns:
                    df = df.drop(columns=[col])
            return df
        except Exception as e:
            msg = str(e)
            if is_duckdb_lock_error(e):
                raise
            if "does not exist" not in msg and "Catalog Error" not in msg:
                logging.error(f"查询失败 {self.stock_code}: {e}")
            return pd.DataFrame()

    def get_kline_epoch_seconds(
        self,
        period: str,
        start_time: str = None,
        end_time: str = None,
        dividend_type: str = None,
        fields: List[str] = None,
        tz_offset_seconds: int = 0
    ) -> pd.DataFrame:
        """Read K-line data with ``time`` returned as local epoch seconds."""
        table_name = self.PERIOD_TABLE_MAP.get(period)
        if not table_name:
            return pd.DataFrame()

        if get_duckdb_read_ensure_tables():
            self._ensure_period_table(period)

        if not fields:
            return self.get_kline(period, start_time, end_time, dividend_type, fields)

        selected_fields = [f for f in fields if f != 'time']
        field_exprs = ['CAST(epoch(time) AS BIGINT) - ? AS time'] + selected_fields
        field_str = ', '.join(field_exprs)
        sql = f"SELECT {field_str} FROM {table_name} WHERE 1=1"
        params = [int(tz_offset_seconds or 0)]

        if start_time:
            sql += " AND time >= ?"
            params.append(self._parse_time(start_time))

        if end_time:
            sql += " AND time <= ?"
            params.append(self._parse_time(end_time, is_end_time=True))

        if dividend_type:
            sql += " AND dividend_type = ?"
            params.append(dividend_type)

        order_mode = get_duckdb_order_mode()
        if order_mode == "sql_order_by":
            sql += " ORDER BY time"

        try:
            df = self.conn.execute(sql, params).fetchdf()
            if order_mode == "verify_after_fetch" and "time" in df.columns and not df["time"].is_monotonic_increasing:
                df = df.sort_values("time").reset_index(drop=True)
            internal_cols = ['dividend_type', 'update_time']
            for col in internal_cols:
                if col in df.columns:
                    df = df.drop(columns=[col])
            return df
        except Exception as e:
            msg = str(e)
            if is_duckdb_lock_error(e):
                raise
            if "does not exist" not in msg and "Catalog Error" not in msg:
                logging.error(f"查询失败 {self.stock_code}: {e}")
            return pd.DataFrame()

    def get_tick(
        self,
        start_time: str = None,
        end_time: str = None,
        fields: List[str] = None
    ) -> pd.DataFrame:
        """
        读取Tick数据

        Args:
            start_time: 开始时间
            end_time: 结束时间
            fields: 需要的字段列表，None表示获取所有字段

        Returns:
            Tick数据DataFrame
        """
        # 只读回测热路径可跳过 DDL 检查；缺表时在查询异常里返回空 DataFrame。
        if get_duckdb_read_ensure_tables():
            self._ensure_period_table('tick')

        # 构建字段（#13: tick表字段名与K线不同, 直接 SELECT 原名如 close 会 Binder Error 致 tick 回测全崩。
        # 映射 close→lastPrice / preClose→lastClose 等, tick 无对应字段者跳过）
        if fields:
            _TICK_COLS = {'time', 'lastPrice', 'open', 'high', 'low', 'lastClose', 'amount', 'volume',
                          'pvolume', 'stockStatus', 'openInt', 'lastSettlementPrice', 'transactionNum'}
            _K2T = {'close': 'lastPrice', 'preClose': 'lastClose', 'settelementPrice': 'lastSettlementPrice',
                    'openInterest': 'openInt', 'suspendFlag': 'stockStatus'}
            cols = []
            for f in fields:
                if f == 'time':
                    continue
                if f in _TICK_COLS:
                    cols.append(f)
                elif f in _K2T:
                    cols.append(f"{_K2T[f]} AS {f}")
                # 否则 tick 无此字段, 跳过
            field_str = ', '.join(['time'] + cols)
        else:
            field_str = '*'

        # 构建查询
        sql = f"SELECT {field_str} FROM tick WHERE 1=1"
        params = []

        if start_time:
            sql += " AND time >= ?"
            params.append(self._parse_time(start_time))

        if end_time:
            sql += " AND time <= ?"
            params.append(self._parse_time(end_time, is_end_time=True))

        sql += " ORDER BY time"

        try:
            df = self.conn.execute(sql, params).fetchdf()
            # 移除内部字段
            if 'update_time' in df.columns:
                df = df.drop(columns=['update_time'])
            return df
        except Exception as e:
            msg = str(e)
            if is_duckdb_lock_error(e):
                raise
            if "does not exist" not in msg and "Catalog Error" not in msg:
                logging.error(f"查询Tick失败 {self.stock_code}: {e}")
            return pd.DataFrame()

    def _parse_time(self, time_str: str, is_end_time: bool = False) -> str:
        """解析时间字符串

        Args:
            time_str: 时间字符串
            is_end_time: 是否是结束时间。如果是结束时间且只有日期，则设置为当天23:59:59
        """
        time_str = str(time_str).strip()
        if len(time_str) == 8 and time_str.isdigit():  # YYYYMMDD
            date_str = f"{time_str[:4]}-{time_str[4:6]}-{time_str[6:8]}"
            if is_end_time:
                return f"{date_str} 23:59:59"
            return date_str
        elif len(time_str) == 10 and '-' in time_str:  # YYYY-MM-DD
            if is_end_time:
                return f"{time_str} 23:59:59"
            return time_str
        return time_str

    def get_data_range(self, period: str) -> Tuple[Optional[datetime], Optional[datetime]]:
        """获取数据时间范围（不区分复权类型）"""
        table_name = self.PERIOD_TABLE_MAP.get(period)
        if not table_name:
            return None, None

        try:
            self._ensure_period_table(period)
            result = self.conn.execute(f"""
                SELECT MIN(time), MAX(time) FROM {table_name}
            """).fetchone()
            return result
        except Exception as exc:
            if is_duckdb_lock_error(exc):
                raise
            message = str(exc)
            if "does not exist" not in message and "Catalog Error" not in message:
                logging.error(
                    "查询数据范围失败 %s %s: %s",
                    self.stock_code,
                    period,
                    exc,
                )
            return None, None

    def get_data_range_by_type(
        self, period: str, dividend_type: str
    ) -> Tuple[Optional[datetime], Optional[datetime]]:
        """获取指定复权类型的数据时间范围（供增量导入使用）"""
        table_name = self.PERIOD_TABLE_MAP.get(period)
        if not table_name:
            return None, None

        try:
            self._ensure_period_table(period)
            result = self.conn.execute(f"""
                SELECT MIN(time), MAX(time) FROM {table_name}
                WHERE dividend_type = ?
            """, [dividend_type]).fetchone()
            return result
        except Exception as exc:
            if is_duckdb_lock_error(exc):
                raise
            message = str(exc)
            if "does not exist" not in message and "Catalog Error" not in message:
                logging.error(
                    "按复权类型查询数据范围失败 %s %s %s: %s",
                    self.stock_code,
                    period,
                    dividend_type,
                    exc,
                )
            return None, None

    def get_record_count(self, period: str) -> int:
        """获取记录数"""
        table_name = self.PERIOD_TABLE_MAP.get(period)
        if not table_name:
            return 0

        try:
            self._ensure_period_table(period)
            return self.conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        except:
            return 0

    def get_all_counts(self) -> dict:
        """获取所有周期的记录数"""
        counts = {}
        for period in self.PERIOD_TABLE_MAP.keys():
            counts[period] = self.get_record_count(period)
        return counts

    def execute_sql(self, sql: str, params: list = None) -> pd.DataFrame:
        """执行自定义SQL查询"""
        try:
            if params:
                return self.conn.execute(sql, params).fetchdf()
            else:
                return self.conn.execute(sql).fetchdf()
        except Exception as e:
            logging.error(f"SQL执行失败: {e}")
            return pd.DataFrame()

    def close(self, skip_checkpoint=False):
        """关闭连接"""
        if self._conn:
            if self.close_policy == 'preserve_wal' and not self.read_only:
                self._conn.execute('PRAGMA disable_checkpoint_on_shutdown')
                self._conn.close()
                self._conn = None
                return
            if not skip_checkpoint and not self.read_only:
                try:
                    self._conn.execute("CHECKPOINT")
                except:
                    pass
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def get_existing_dates(self, period: str) -> set:
        """
        获取指定周期已有数据的日期集合

        Args:
            period: 周期类型 ('1d', '1m', '5m', 'tick')

        Returns:
            日期集合，格式 {'20240101', '20240102', ...}
        """
        table_name = self.PERIOD_TABLE_MAP.get(period)
        if not table_name:
            return set()

        try:
            # 检查表是否存在
            tables = self.conn.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_name = ?
            """, [table_name]).fetchall()

            if not tables:
                return set()

            # 查询所有不重复的日期（只取日期部分）
            result = self.conn.execute(f"""
                SELECT DISTINCT strftime(time, '%Y%m%d') as date_str
                FROM {table_name}
                WHERE time IS NOT NULL
            """).fetchall()

            return {row[0] for row in result if row[0]}
        except Exception as e:
            logging.warning(f"获取已有日期失败 {self.stock_code}/{period}: {e}")
            return set()
