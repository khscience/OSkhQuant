# 多进程保护 - 防止在子进程中意外启动Qt应用
import sys
import os

# 检查是否在子进程中，只在子进程中设置环境变量
def is_subprocess():
    """检查是否在子进程中"""
    import multiprocessing
    try:
        current_process = multiprocessing.current_process()
        return current_process.name != 'MainProcess'
    except:
        return False

# 只在子进程中设置环境变量
if is_subprocess():
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    os.environ['QT_LOGGING_RULES'] = 'qt.*=false'

import csv
import time
import threading
import tempfile
from datetime import date as datetime_date, datetime, timedelta
import pandas as pd
import glob
import numpy as np
import logging
import ast
from collections import OrderedDict
from typing import Dict, List, Union, Optional, Sequence, Mapping
import math

from khPathUtils import get_stock_pool_read_dirs, get_stock_pool_path
from security_type_utils import is_etf_code, is_listed_fund_code

# Tick 完整性协议与增量规划共用同一组默认值。导入放在模块级不会触发
# xtquant/Qt；incremental.py 本身只依赖时间解析器，因而不会形成循环。
try:
    from duckdb_storage.incremental import (
        TICK_EXPECTED_ROWS_PER_DAY,
        TICK_MIN_ROWS_PER_DAY,
        TICK_MAX_AGE_DAYS,
        TICK_MAX_SPAN_DAYS,
        TICK_REQUIRED_COLUMNS,
        group_missing_trade_dates_with_counts as _group_missing_trade_dates_with_counts,
    )
except Exception:  # pragma: no cover - 打包/单文件环境的保守回退
    TICK_EXPECTED_ROWS_PER_DAY = 4700
    TICK_MIN_ROWS_PER_DAY = 4465
    TICK_MAX_AGE_DAYS = 31
    TICK_MAX_SPAN_DAYS = 31
    TICK_REQUIRED_COLUMNS = ("lastPrice", "volume", "amount")
    _group_missing_trade_dates_with_counts = None


# 增量扫描通常会连续运行多次（补数后复扫、切换同一股票池、重试失败
# 任务）。数据库文件未发生变化时，重复打开并聚合整张表没有收益；按文件
# stat 做失效条件，缓存只保存已经生成的任务描述，不保存行情行，内存受控。
_INTEGRITY_SCAN_CACHE = OrderedDict()
_INTEGRITY_SCAN_CACHE_LOCK = threading.RLock()
_INTEGRITY_SCAN_CACHE_MAX = 8192


def clear_integrity_scan_cache() -> None:
    """清空本进程的完整性扫描缓存（写库或切换数据根目录后可调用）。"""
    with _INTEGRITY_SCAN_CACHE_LOCK:
        _INTEGRITY_SCAN_CACHE.clear()


from types import SimpleNamespace


# 开源版不含 xtquant：旧策略若调用 xtdata，占位对象会给出明确提示
from kh_xtdata_stub import xtdata

# 延迟导入Qt相关模块，避免在子进程中意外启动Qt应用
_KHQUANT_HEADLESS = os.environ.get("KHQUANT_HEADLESS", "").strip().lower() in {
    "1", "true", "yes", "on"
}

try:
    if not is_subprocess() and not _KHQUANT_HEADLESS:
        # 在主进程中正常导入Qt模块
        from PyQt5.QtCore import QThread, pyqtSignal
    else:
        # 在子进程中创建空的占位符类
        class QThread:
            def __init__(self):
                pass
            def start(self):
                pass
            def run(self):
                pass
        
        def pyqtSignal(*args, **kwargs):
            return lambda: None
except ImportError:
    # 如果导入失败，创建空的占位符类
    class QThread:
        def __init__(self):
            pass
        def start(self):
            pass
        def run(self):
            pass
    
    def pyqtSignal(*args, **kwargs):
        return lambda: None


# ============================================================================
# 独立函数版本 - 可以直接调用，无需实例化类
# ============================================================================

# 初始化全局变量
_trading_periods = [
    ("093000", "113000"),  # 上午
    ("130000", "150000")   # 下午
]
_baostock_trade_days_cache = {}

# 默认价格精度（股票为2位，ETF为3位）
_default_price_decimals = 2

# ── 交易日 CSV 持久化缓存 ─────────────────────────────────────────────────────
# trade_days.csv 每行一个 YYYYMMDD 交易日，与股票池文件使用同一套目录规则：
# 读取时合并「用户可写目录里更新过的那份」和「随包那份」；写入只落到可写目录。
# 打包版装在 Program Files 时，程序目录不可写，缓存写到 LocalAppData；
# 源码模式仍写回项目 data/ 目录，行为与以前一致。
# 历史年份（< 当年）一旦写入永久有效；当年数据若 CSV 最新日期距今 > 7 天自动刷新。

_TRADE_DAYS_CSV_NAME = 'trade_days.csv'
_td_memory: set = set()                 # 内存集合：YYYYMMDD 字符串
_td_min: str = ""                       # 集合最小日期
_td_max: str = ""                       # 集合最大日期
_td_covered_years: set = set()          # 已完整覆盖的年份（str，如 "2024"）
_td_csv_loaded: bool = False            # 是否已从 CSV 初始化过内存


def _get_trade_days_csv_read_paths() -> list:
    """所有已存在的 trade_days.csv，用户更新过的在前，随包的在后。"""
    paths = []
    for data_dir in get_stock_pool_read_dirs(include_missing=False):
        candidate = os.path.join(data_dir, _TRADE_DAYS_CSV_NAME)
        if os.path.isfile(candidate) and candidate not in paths:
            paths.append(candidate)
    return paths


def _get_trade_days_csv_path() -> str:
    """写入用路径：打包版为用户可写目录，源码模式为项目 data/ 目录。"""
    return get_stock_pool_path(_TRADE_DAYS_CSV_NAME, for_write=True)


def _td_load_csv():
    """从 CSV 加载交易日到内存集合（进程内只做一次）。

    合并所有候选文件：用户缓存可能比随包日历新（补过当年），随包日历也可能
    比旧缓存新（升级后带了新一年），两者都是真实交易日，取并集即可。
    """
    global _td_memory, _td_min, _td_max, _td_covered_years, _td_csv_loaded
    _td_csv_loaded = True
    dates = set()
    for path in _get_trade_days_csv_read_paths():
        try:
            df = pd.read_csv(path, dtype=str)
            if 'trade_date' not in df.columns:
                logging.warning(f"{path} 缺少 trade_date 列，已跳过")
                continue
            dates |= set(df['trade_date'].dropna().str.strip())
        except Exception as e:
            logging.warning(f"读取 {path} 失败: {e}")
    if not dates:
        return
    _td_memory = dates
    _td_min = min(dates)
    _td_max = max(dates)
    _td_covered_years = set(d[:4] for d in dates)
    logging.info(f"交易日CSV已加载：{len(dates)} 条，范围 {_td_min}~{_td_max}")


def _td_save_csv():
    """将内存中的交易日集合写回 CSV（只写可写目录，不碰随包文件）。"""
    if not _td_memory:
        return
    path = _get_trade_days_csv_path()
    try:
        df = pd.DataFrame(sorted(_td_memory), columns=['trade_date'])
        df.to_csv(path, index=False)
        logging.info(f"交易日数据已保存：{path}，共 {len(_td_memory)} 条")
    except Exception as e:
        logging.warning(f"保存 trade_days.csv 失败: {e}")


# 拉取失败缓存：year -> 上次失败时间戳。
# 失败后短期内不再重试，否则离线时 get_trade_days_count 按天循环调用
# is_trade_day，每天都会触发一次 baostock 登录，
# 一个回测区间就能把界面卡死几分钟。
_td_fetch_failed: dict = {}
_TD_FETCH_RETRY_INTERVAL = 300  # 失败后5分钟内不重试
_td_weekday_fallback_warned: set = set()


def _td_fetch_year(year: int) -> bool:
    """拉取指定年份交易日（baostock），写入内存+CSV。

    拉取失败会被缓存 _TD_FETCH_RETRY_INTERVAL 秒，期间直接返回 False。
    """
    global _td_memory, _td_min, _td_max, _td_covered_years

    last_failed = _td_fetch_failed.get(year)
    if last_failed is not None and (time.time() - last_failed) < _TD_FETCH_RETRY_INTERVAL:
        return False

    year_start = f"{year}-01-01"
    year_end   = f"{year}-12-31"

    # 优先 baostock
    year_set = _fetch_baostock_trade_days(year_start, year_end)

    if year_set is None:
        _td_fetch_failed[year] = time.time()
        logging.warning(f"无法获取 {year} 年交易日（baostock 不可用），"
                        f"{_TD_FETCH_RETRY_INTERVAL} 秒内不再重试")
        return False

    _td_fetch_failed.pop(year, None)
    _td_memory.update(year_set)
    if _td_memory:
        _td_min = min(_td_memory)
        _td_max = max(_td_memory)
    _td_covered_years.add(str(year))
    logging.info(f"{year} 年交易日已更新：{len(year_set)} 条")
    _td_save_csv()
    return True


def _td_ensure_covered(date_yyyymmdd: str) -> bool:
    """
    确保 date_yyyymmdd 所在年份的数据已在内存中。
    - 首次调用：先加载 CSV
    - 年份不在覆盖集合：拉取该年数据
    - 当年数据：若 CSV 最新日期距今 > 7 天，重新拉取
    返回 True 表示数据可用，False 表示完全无法获取（降级到周末判断）
    """
    global _td_csv_loaded
    if not _td_csv_loaded:
        _td_load_csv()

    year_str = date_yyyymmdd[:4]
    today_str = datetime.now().strftime("%Y%m%d")
    current_year_str = datetime.now().strftime("%Y")

    if year_str not in _td_covered_years:
        return _td_fetch_year(int(year_str))

    # 当年数据：检查是否够新（避免实盘时缺最近几天）
    if year_str == current_year_str:
        if not _td_max or (int(today_str) - int(_td_max)) > 7:
            if not _td_fetch_year(int(year_str)):
                # 拉取失败但内存中已有该年旧数据时继续使用旧数据，
                # 比降级成"非周末即交易日"的粗略判断更准确
                return year_str in _td_covered_years

    return True


# ─────────────────────────────────────────────────────────────────────────────

def _fetch_baostock_trade_days(start_date: str, end_date: str):
    import socket as _sock
    _old_socket_timeout = _sock.getdefaulttimeout()
    _sock.setdefaulttimeout(15)  # baostock login/query 为裸socket无超时, 加进程级超时防网络抽风时无限挂起
    try:
        import baostock as bs
        from baostock_proxy import enable_baostock_proxy

        enable_baostock_proxy()

        lg = bs.login()
        if lg.error_code != "0":
            logging.warning(f"baostock 登录失败: {lg.error_msg}")
            try:
                bs.logout()
            except Exception:
                pass
            return None

        rs = bs.query_trade_dates(start_date=start_date, end_date=end_date)
        if rs.error_code != "0":
            logging.warning(f"baostock 获取交易日历失败: {rs.error_msg or rs.error_code}")
            try:
                bs.logout()
            except Exception:
                pass
            return None

        data_list = []
        while (rs.error_code == "0") & rs.next():
            data_list.append(rs.get_row_data())

        try:
            bs.logout()
        except Exception:
            pass

        if not data_list:
            return set()

        df = pd.DataFrame(data_list, columns=rs.fields)
        if "calendar_date" not in df.columns or "is_trading_day" not in df.columns:
            return None

        df = df[df["is_trading_day"].astype(str) == "1"]
        dates = pd.to_datetime(df["calendar_date"], errors="coerce").dropna().dt.strftime("%Y%m%d")
        return set(dates)
    except Exception as e:
        logging.warning(f"baostock 获取交易日历异常: {e}")
        return None
    finally:
        _sock.setdefaulttimeout(_old_socket_timeout)

def _get_baostock_trade_days_set(start_date: datetime, end_date: datetime):
    if start_date > end_date:
        return set()

    start_year = start_date.year
    end_year = end_date.year
    start_bound = start_date.strftime("%Y%m%d")
    end_bound = end_date.strftime("%Y%m%d")
    result = set()

    for year in range(start_year, end_year + 1):
        year_start = f"{year}-01-01"
        year_end = f"{year}-12-31"
        cache_key = f"{year_start}_{year_end}"
        if cache_key in _baostock_trade_days_cache:
            year_set = _baostock_trade_days_cache[cache_key]
        else:
            year_set = _fetch_baostock_trade_days(year_start, year_end)
            if year_set is None:
                return None
            _baostock_trade_days_cache[cache_key] = year_set

        if start_year == end_year:
            result.update({d for d in year_set if start_bound <= d <= end_bound})
        elif year == start_year:
            result.update({d for d in year_set if d >= start_bound})
        elif year == end_year:
            result.update({d for d in year_set if d <= end_bound})
        else:
            result.update(year_set)

    return result

def is_etf(stock_code: str) -> bool:
    """判断是否为ETF（不包括LOF）
    
    Args:
        stock_code: 股票代码，如 "510300.SH" 或 "159915.SZ"
        
    Returns:
        bool: 是否为ETF
        
    说明:
        上海ETF: 51(主流)、52(跨境)、53(部分)、55(债券)、56(新规)、58(科创)
        深圳ETF: 158/159开头
        注意：50/16开头是LOF，不是ETF
    """
    return is_etf_code(stock_code)

def normalize_stock_code(stock_code: str) -> str:
    """标准化股票代码格式，支持sh.000300和000300.SH两种写法

    Args:
        stock_code: 股票代码，支持以下格式:
            - "sh.000300" (miniQMT格式)
            - "000300.SH" (标准格式)
            - "sz.000001" (miniQMT格式)
            - "000001.SZ" (标准格式)

    Returns:
        str: 标准化后的股票代码 (格式: "000300.SH")

    Examples:
        >>> normalize_stock_code("sh.000300")
        "000300.SH"
        >>> normalize_stock_code("000300.SH")
        "000300.SH"
        >>> normalize_stock_code("sz.000001")
        "000001.SZ"
    """
    if not stock_code:
        return stock_code

    stock_code = stock_code.strip()

    if '.' in stock_code:
        parts = stock_code.split('.')
        if len(parts) == 2:
            first, second = parts

            # miniQMT格式 (sh.000300 或 sz.000001) - 市场在前，代码在后
            if first.lower() in ('sh', 'sz', 'bj') and second.isdigit() and len(second) == 6:
                return f"{second}.{first.upper()}"

            # 标准格式 (000300.SH 或 000001.SZ) - 代码在前，市场在后
            elif first.isdigit() and len(first) == 6 and second.upper() in ('SH', 'SZ', 'BJ'):
                return f"{first}.{second.upper()}"

    # 如果没有市场后缀或格式不对，返回原值
    return stock_code

def denormalize_stock_code(stock_code: str, to_format: str = 'miniQMT') -> str:
    """将标准格式的股票代码转换为指定格式

    Args:
        stock_code: 标准格式的股票代码 (如 "000300.SH")
        to_format: 目标格式，可选 'miniQMT' 或 'standard'
            - 'miniQMT': 转换为 "sh.000300" 格式
            - 'standard': 保持 "000300.SH" 格式

    Returns:
        str: 转换后的股票代码

    Examples:
        >>> denormalize_stock_code("000300.SH", "miniQMT")
        "sh.000300"
        >>> denormalize_stock_code("000300.SH", "standard")
        "000300.SH"
    """
    if not stock_code or '.' not in stock_code:
        return stock_code

    # 先标准化
    stock_code = normalize_stock_code(stock_code)

    if to_format == 'miniQMT':
        parts = stock_code.split('.')
        if len(parts) == 2:
            code, market = parts
            return f"{market.lower()}.{code}"

    return stock_code

def determine_pool_type(stock_list: List[str]) -> tuple:
    """判断股票池类型，返回类型和对应的价格精度
    
    Args:
        stock_list: 股票代码列表
        
    Returns:
        tuple: (pool_type, price_decimals)
            pool_type: 'stock_only' | 'etf_only' | 'mixed'
            price_decimals: 2（纯股票）或 3（含场内基金或混合）
    """
    if not stock_list:
        return ('stock_only', 2)
    
    # 保留历史 pool_type 字符串以免破坏配置兼容，但报价精度按全部场内基金判断，
    # 不能把 16 开头 LOF（如 161226.SZ）误判为两位小数的普通股票。
    has_stock = any(not is_listed_fund_code(code) for code in stock_list)
    has_etf = any(is_listed_fund_code(code) for code in stock_list)
    
    if has_stock and not has_etf:
        # 纯股票池，使用2位小数
        return ('stock_only', 2)
    elif has_etf and not has_stock:
        # 纯ETF池，使用3位小数
        return ('etf_only', 3)
    else:
        # 混合池，使用3位小数
        return ('mixed', 3)

# ==================== T+0交易模式相关函数 ====================

# 全局缓存T0 ETF列表，避免重复读取文件
_t0_etf_cache = None

def load_t0_etf_list() -> set:
    """加载T0型ETF列表
    
    Returns:
        set: T0型ETF的股票代码集合
    """
    global _t0_etf_cache
    
    if _t0_etf_cache is not None:
        return _t0_etf_cache
    
    _t0_etf_cache = set()
    
    # 用户更新目录优先，安装包默认清单兜底。
    t0_file = get_stock_pool_path('T0型ETF.csv')
    
    if not os.path.exists(t0_file):
        logging.warning(f"T0型ETF列表文件不存在: {t0_file}")
        return _t0_etf_cache
    
    try:
        with open(t0_file, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                if row and len(row) >= 1:
                    stock_code = row[0].strip()
                    if stock_code:
                        _t0_etf_cache.add(stock_code)
        logging.info(f"已加载 {len(_t0_etf_cache)} 只T0型ETF")
    except Exception as e:
        logging.error(f"加载T0型ETF列表失败: {e}")
    
    return _t0_etf_cache

def is_t0_etf(stock_code: str) -> bool:
    """判断单个股票是否支持T+0交易
    
    Args:
        stock_code: 股票代码，如 '159001.SZ'
        
    Returns:
        bool: 是否支持T+0
    """
    t0_list = load_t0_etf_list()
    return stock_code in t0_list

def check_t0_support(stock_list: List[str]) -> tuple:
    """检验股票池的T+0支持情况
    
    Args:
        stock_list: 股票代码列表
        
    Returns:
        tuple: (support_type, is_t0_mode)
            support_type: 'all_t0' | 'mixed' | 'no_t0'
            is_t0_mode: True（全T+0）/ False（其他情况）
    """
    if not stock_list:
        return ('no_t0', False)
    
    t0_list = load_t0_etf_list()
    t0_count = sum(1 for code in stock_list if code in t0_list)
    total_count = len(stock_list)
    
    if t0_count == total_count:
        # 全部是T+0 ETF
        return ('all_t0', True)
    elif t0_count > 0:
        # 混合：部分支持T+0，部分不支持
        return ('mixed', False)
    else:
        # 全部不支持T+0
        return ('no_t0', False)

def get_t0_details(stock_list: List[str]) -> dict:
    """获取股票池中T+0支持的详细信息
    
    Args:
        stock_list: 股票代码列表
        
    Returns:
        dict: {
            't0_stocks': List[str],  # 支持T+0的股票
            'non_t0_stocks': List[str],  # 不支持T+0的股票
            't0_count': int,
            'total_count': int
        }
    """
    t0_list = load_t0_etf_list()
    t0_stocks = [code for code in stock_list if code in t0_list]
    non_t0_stocks = [code for code in stock_list if code not in t0_list]
    
    return {
        't0_stocks': t0_stocks,
        'non_t0_stocks': non_t0_stocks,
        't0_count': len(t0_stocks),
        'total_count': len(stock_list)
    }

# ==================== 价格精度相关函数 ====================

def get_price_decimals(data: Dict = None, stock_code: str = None) -> int:
    """从数据字典中获取价格精度设置
    
    Args:
        data: 策略接收的数据对象，包含框架信息 __framework__
        stock_code: 可选证券代码；传入时按证券类型返回报价精度
        
    Returns:
        int: 价格精度（小数位数），默认为2
    """
    decimals = _default_price_decimals
    if data is not None:
        framework = data.get("__framework__", None)
        if framework and hasattr(framework, 'price_decimals'):
            decimals = framework.price_decimals

    if not stock_code:
        return decimals
    if is_listed_fund_code(stock_code):
        return 3

    code = str(stock_code).upper()
    compact = code.replace("SH.", "").replace("SZ.", "").replace("BJ.", "")
    compact = compact.split(".", 1)[0]
    if (
        code.endswith((".SH", ".SZ", ".BJ"))
        or code.startswith(("SH.", "SZ.", "BJ."))
        or (len(compact) == 6 and compact.isdigit())
    ):
        return 2
    return decimals

def round_price(
    price: float,
    decimals: int = None,
    data: Dict = None,
    stock_code: str = None,
) -> float:
    """根据精度设置对价格进行四舍五入
    
    Args:
        price: 原始价格
        decimals: 精度（小数位数），如果为None则从data中获取
        data: 策略接收的数据对象
        stock_code: 可选证券代码；混合资产池中用于选择单只证券精度
        
    Returns:
        float: 四舍五入后的价格
    """
    if decimals is None:
        decimals = get_price_decimals(data, stock_code)
    return round(price, decimals)

def format_price(price: float, decimals: int = None, data: Dict = None) -> str:
    """根据精度设置格式化价格为字符串
    
    Args:
        price: 价格
        decimals: 精度（小数位数），如果为None则从data中获取
        data: 策略接收的数据对象
        
    Returns:
        str: 格式化后的价格字符串
    """
    if decimals is None:
        decimals = get_price_decimals(data)
    return f"{price:.{decimals}f}"

def is_trade_time() -> bool:
    """判断是否为交易时间"""
    current = time.strftime("%H%M%S")
    
    for start, end in _trading_periods:
        if start <= current <= end:
            return True
    return False

def is_trade_day(date_str: str = None, *, allow_weekday_fallback: bool = True) -> bool:
    """判断是否为交易日。

    查询顺序：
      1. data/trade_days.csv 持久化缓存（O(1) set 查找）
      2. CSV 不覆盖该年份时：调用 baostock，写入 CSV 后返回
      3. baostock 不可用：调用 xtquant，写入 CSV 后返回
      4. 全部不可用：降级为"非周末即交易日"

    Args:
        date_str: "YYYY-MM-DD" / "YYYYMMDD" / None（默认今天）
        allow_weekday_fallback: 默认保留研究工具的兼容行为；模拟盘传 False，
            无日历或日期无效时抛出异常，禁止按星期猜测。

    Returns:
        bool: 是否为交易日
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    try:
        # 标准化为 YYYYMMDD
        if '-' in date_str and len(date_str) == 10:
            date_yyyymmdd = date_str.replace('-', '')
        elif date_str.isdigit() and len(date_str) == 8:
            date_yyyymmdd = date_str
        else:
            for fmt in ["%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"]:
                try:
                    date_yyyymmdd = datetime.strptime(date_str, fmt).strftime("%Y%m%d")
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"无法解析日期格式: {date_str}")

        # 严格调用也必须验证日期值，不能把 20260230 当作合法休市日。
        if not allow_weekday_fallback:
            datetime.strptime(date_yyyymmdd, "%Y%m%d")
        # 确保该年份数据已加载
        if _td_ensure_covered(date_yyyymmdd):
            return date_yyyymmdd in _td_memory

        if not allow_weekday_fallback:
            raise RuntimeError(f"{date_yyyymmdd[:4]} 年交易日历不可用，禁止工作日回退")

        # 完全无法获取数据时：降级为非周末判断
        weekday = datetime.strptime(date_yyyymmdd, "%Y%m%d").weekday()
        return weekday < 5

    except Exception as e:
        logging.warning(f"判断交易日异常 ({date_str}): {e}")
        if not allow_weekday_fallback:
            raise
        try:
            for fmt in ["%Y-%m-%d", "%Y%m%d"]:
                try:
                    date_obj = datetime.strptime(date_str, fmt)
                    return date_obj.weekday() < 5
                except ValueError:
                    continue
        except Exception:
            pass
        return True  # 实在判断不出，默认为交易日

def get_trade_days_count(start_date: str, end_date: str) -> int:
    """计算指定日期范围内的交易日天数
    
    Args:
        start_date: 起始日期，格式为"YYYY-MM-DD"
        end_date: 结束日期，格式为"YYYY-MM-DD"
        
    Returns:
        int: 交易日天数
    """
    try:
        # 解析起始和结束日期
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        
        # 确保开始日期不晚于结束日期
        if start_dt > end_dt:
            logging.error(f"起始日期 {start_date} 晚于结束日期 {end_date}")
            return 0
            
        # 初始化计数器
        trade_days = 0
        
        # 遍历日期范围内的每一天
        current_dt = start_dt
        while current_dt <= end_dt:
            current_date_str = current_dt.strftime("%Y-%m-%d")
            # 使用is_trade_day函数判断是否为交易日
            if is_trade_day(current_date_str):
                trade_days += 1
            
            # 前进到下一天
            current_dt += timedelta(days=1)
            
        logging.info(f"从 {start_date} 到 {end_date} 共有 {trade_days} 个交易日")
        return trade_days
        
    except Exception as e:
        logging.error(f"计算交易日天数时出错: {str(e)}")
        return 0


def _get_trade_days_set_impl(
    start_date: str, end_date: str, *, allow_weekday_fallback: bool = True,
) -> set:
    """交易日集合的严格实现；异常由调用方决定是否降级。"""

    # 标准化为 YYYYMMDD
    start_yyyymmdd = start_date.replace('-', '') if '-' in start_date else start_date
    end_yyyymmdd   = end_date.replace('-', '')   if '-' in end_date   else end_date

    if not allow_weekday_fallback:
        datetime.strptime(start_yyyymmdd, "%Y%m%d")
        datetime.strptime(end_yyyymmdd, "%Y%m%d")
        if start_yyyymmdd > end_yyyymmdd:
            raise ValueError("交易日历起始日期晚于结束日期")

    start_year = int(start_yyyymmdd[:4])
    end_year   = int(end_yyyymmdd[:4])

    # 确保所有涉及年份都已覆盖（不足则拉取）。拉取失败时不能再走
    # 另一套独立的 BaoStock 查询链路，否则 khKline 在每个回调中都会
    # 重新登录 BaoStock，网络异常时会把整场回测拖到近乎卡死。
    missing_years = []
    for year in range(start_year, end_year + 1):
        if not _td_ensure_covered(f"{year}0101"):
            missing_years.append(year)

    result = {
        d for d in _td_memory
        if start_yyyymmdd <= d <= end_yyyymmdd
        and int(d[:4]) not in missing_years
    }

    # 完全离线且本地没有对应年份日历时，以工作日降级。失败年份仍受
    # _td_fetch_failed 的冷却保护；这里不再进行任何第二次网络请求。
    if missing_years:
        if not allow_weekday_fallback:
            raise RuntimeError(f"交易日历年份不可用: {missing_years}，禁止工作日回退")
        global _td_weekday_fallback_warned
        for year in missing_years:
            if year not in _td_weekday_fallback_warned:
                logging.warning(
                    f"{year} 年交易日历不可用，临时按周一至周五判断；"
                    f"{_TD_FETCH_RETRY_INTERVAL} 秒内不再联网重试"
                )
                _td_weekday_fallback_warned.add(year)

        current = datetime.strptime(start_yyyymmdd, "%Y%m%d")
        end_dt = datetime.strptime(end_yyyymmdd, "%Y%m%d")
        while current <= end_dt:
            if current.year in missing_years and current.weekday() < 5:
                result.add(current.strftime("%Y%m%d"))
            current += timedelta(days=1)

    return result


def get_trade_days_set_checked(
    start_date: str, end_date: str, *, allow_weekday_fallback: bool = True,
) -> set:
    """严格读取交易日集合。

    与 :func:`get_trade_days_set` 的兼容接口不同，本函数不会把内部异常
    吞成空集合。数据导入的完整性规划使用它区分“合法的零交易日区间”与
    “日历读取失败”，避免把后者误报为已经完整。
    """

    return _get_trade_days_set_impl(
        start_date, end_date, allow_weekday_fallback=allow_weekday_fallback,
    )


def get_trade_days_set(start_date: str, end_date: str) -> set:
    """获取指定日期范围内的所有交易日集合。

    优先从 data/trade_days.csv 持久化缓存中读取；
    若某年份未覆盖，则自动调用 baostock / xtquant 补充并写入 CSV。

    Args:
        start_date: 起始日期，格式为"YYYYMMDD"或"YYYY-MM-DD"
        end_date: 结束日期，格式为"YYYYMMDD"或"YYYY-MM-DD"

    Returns:
        set: 交易日集合，格式为"YYYYMMDD"
    """
    try:
        return _get_trade_days_set_impl(start_date, end_date)

    except Exception as e:
        logging.error(f"获取交易日集合时出错: {e}")
        return set()


def check_duckdb_data_integrity(
    stock_list: List[str],
    periods: List[str],
    start_date: str,
    end_date: str,
    duckdb_data_path: str,
    progress_callback: callable = None,
    stop_flag: callable = None,
    stock_periods: Dict[str, List[str]] = None,
    dividend_type: Union[str, Sequence[str]] = 'none',
    tick_min_rows: int = TICK_MIN_ROWS_PER_DAY,
    tick_max_age_days: Optional[int] = None,
    tick_max_span_days: int = TICK_MAX_SPAN_DAYS,
    now: datetime = None,
    period_ranges: Dict[str, Sequence[str]] = None,
    scan_workers: int = None,
) -> Dict:
    """
    检查DuckDB本地数据库中数据的完整性

    检测指定股票列表在指定周期和时间段内的数据是否完整。
    如果数据不完整，返回缺失数据的详细信息。

    Args:
        stock_list: 股票代码列表，如 ['000001.SZ', '600000.SH']
        periods: 周期列表，如 ['1d', '1m', '5m', 'tick']
        start_date: 起始日期，格式为"YYYYMMDD"
        end_date: 结束日期，格式为"YYYYMMDD"
        duckdb_data_path: DuckDB数据存储路径
        progress_callback: 进度回调函数，签名为 callback(current, total, message, task_count)
        stop_flag: 停止标志函数，返回True时停止扫描
        dividend_type: 回测所选复权口径，支持单个字符串或字符串序列。
            非 none 时，K线周期(1d/1m/5m)会额外要求每一种对应复权口径的
            OHLC 四列（open_<dt>/high_<dt>/low_<dt>/close_<dt>）均为非空且
            有限值，否则该交易日视为缺失——避免"只有不复权数据也判通过、回测时静默降级"
            的问题。tick 周期无复权字段，不受此约束。
            默认 'none' 以保持向后兼容。
        tick_min_rows: tick 每个交易日判定完整所需的最低有效时间戳数；
            默认使用历史 GUI 的 4465（约 4700 的 95%）阈值。
        tick_max_age_days: tick 起始日期相对当前日期允许的最大自然日年龄，
            默认 31；窗口外日期会明确排除而不会生成永远失败的任务。
        tick_max_span_days: 单个 tick 下载任务允许覆盖的最大自然日跨度，
            默认 31，并按闭区间拆分任务。
        now: 可选的当前时间注入，仅用于测试/离线扫描；未提供时使用本机时间。
        period_ranges: 可选的逐周期日期范围映射。提供后，扫描 SQL 只读取
            该周期实际请求区间，避免用日线的十年范围扫描分钟/ tick 全表。
        scan_workers: 可选的扫描线程数。默认保持历史 4 线程；大 QMT GUI
            会按本机 I/O 能力显式传入更高但有上限的值，MiniQMT 不改变默认值。

    Returns:
        dict: {
            'is_complete': bool,           # 数据是否完整
            'total_stocks': int,           # 总股票数
            'total_periods': int,          # 总周期数
            'total_trade_days': int,       # 时间段内的总交易日数
            'missing_tasks': [             # 缺失数据任务列表
                {
                    'stock': str,          # 股票代码
                    'period': str,         # 周期
                    'start': str,          # 缺失起始日期 YYYYMMDD
                    'end': str,            # 缺失结束日期 YYYYMMDD
                    'missing_days': int    # 缺失天数
                },
                ...
            ],
            'summary': {                   # 汇总统计
                'by_stock': {              # 按股票统计
                    'stock_code': {
                        'total_missing': int,
                        'periods': {
                            'period': {'missing_days': int, 'task_count': int}
                        }
                    }
                },
                'by_period': {             # 按周期统计
                    'period': {
                        'stock_count': int,
                        'missing_days': int,
                        'task_count': int
                    }
                }
            }
        }
    """
    import bisect
    import duckdb

    # 规范化 tick 策略配置；坏配置应尽早失败，不能把空表误报为完整。
    try:
        tick_min_rows = int(tick_min_rows)
        tick_max_age_days = int(tick_max_age_days) if tick_max_age_days is not None else None
        tick_max_span_days = int(tick_max_span_days)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("tick 完整性参数必须是整数") from exc
    if tick_min_rows <= 0:
        raise ValueError("tick_min_rows 必须大于 0")
    if tick_max_age_days is not None and tick_max_age_days < 0:
        raise ValueError("tick_max_age_days 不能为负数")
    if tick_max_span_days <= 0:
        raise ValueError("tick_max_span_days 必须大于 0")
    current_now = now or datetime.now()
    if isinstance(current_now, datetime):
        current_date = current_now.date()
    elif isinstance(current_now, datetime_date):
        current_date = current_now
    else:
        current_now = datetime.now()
        current_date = current_now.date()

    # 标准化日期格式
    if '-' in start_date:
        start_date = start_date.replace('-', '')
    if '-' in end_date:
        end_date = end_date.replace('-', '')

    # 规范化复权口径：仅 front/back/front_ratio/back_ratio 需要校验对应复权列。
    # 线程/GUI 可能同时选择多种复权；保留字符串调用的旧语义，并将序列
    # 规范化成去重后的稳定顺序。
    if isinstance(dividend_type, str) or dividend_type is None:
        _dividend_values = (dividend_type or 'none',)
    else:
        _dividend_values = tuple(dividend_type or ())
    _valid_adjustments = ('front', 'back', 'front_ratio', 'back_ratio')
    dt_required = tuple(
        str(getattr(value, 'value', value) or '').strip().lower()
        for value in _dividend_values
        if str(getattr(value, 'value', value) or '').strip().lower()
        in _valid_adjustments
    )
    dt_required = tuple(dict.fromkeys(dt_required))

    # 获取时间段内的所有交易日。统一成 YYYYMMDD，避免交易日缓存返回
    # datetime/date 对象时与字符串比较发生静默错配。
    raw_trade_days = get_trade_days_set(start_date, end_date)
    all_trade_days = set()
    for value in raw_trade_days or ():
        text = str(value).strip()
        compact = text[:10].replace('-', '').replace('/', '')[:8]
        if len(compact) == 8 and compact.isdigit():
            all_trade_days.add(compact)

    periods_normalized = []
    for value in periods or ():
        enum_value = getattr(value, "value", value)
        periods_normalized.append(str(enum_value).strip().lower())
    periods_normalized = list(dict.fromkeys(periods_normalized))

    # Normalize optional per-period ranges once.  The caller may pass enum
    # keys or strings and either tuple/list or a two-key mapping.  Invalid
    # entries are ignored so the legacy whole-range behavior remains the
    # conservative fallback rather than silently narrowing a request.
    normalized_period_ranges = {}
    for raw_period, raw_range in (period_ranges or {}).items():
        period_key = str(getattr(raw_period, "value", raw_period)).strip().lower()
        if period_key not in periods_normalized:
            continue
        start_value = end_value = None
        if isinstance(raw_range, Mapping):
            start_value = raw_range.get("start") or raw_range.get("start_date")
            end_value = raw_range.get("end") or raw_range.get("end_date")
        elif isinstance(raw_range, (list, tuple)) and len(raw_range) >= 2:
            start_value, end_value = raw_range[0], raw_range[1]
        if start_value in (None, "") or end_value in (None, ""):
            continue
        start_text = str(start_value).strip().replace("-", "")[:8]
        end_text = str(end_value).strip().replace("-", "")[:8]
        if (
            len(start_text) == 8 and start_text.isdigit()
            and len(end_text) == 8 and end_text.isdigit()
            and start_text <= end_text
        ):
            normalized_period_ranges[period_key] = (start_text, end_text)

    # A caller can opt into more I/O workers, but never accidentally create an
    # unbounded thread fan-out from a malformed environment/UI value.  Keep a
    # flag so the QMT-only fast path below does not alter MiniQMT defaults.
    optimized_scan = bool(period_ranges) or scan_workers not in (None, "", 4)
    if scan_workers in (None, ""):
        scan_workers = 4
    try:
        scan_workers = max(1, min(32, int(scan_workers)))
    except (TypeError, ValueError, OverflowError):
        scan_workers = 4

    tick_requested = 'tick' in periods_normalized
    tick_cutoff = (current_date - timedelta(days=tick_max_age_days)) if tick_max_age_days is not None else None
    tick_eligible_trade_days = {
        value for value in all_trade_days
        if (tick_cutoff is None or tick_cutoff <= datetime.strptime(value, '%Y%m%d').date())
        and datetime.strptime(value, '%Y%m%d').date() <= current_date
    }
    tick_ignored_retention_dates = {
        value for value in all_trade_days
        if tick_cutoff is not None and datetime.strptime(value, '%Y%m%d').date() < tick_cutoff
    }
    tick_ignored_open_dates = {
        value for value in all_trade_days
        if datetime.strptime(value, '%Y%m%d').date() > current_date
    }

    result = {
        'is_complete': True,
        'total_stocks': len(stock_list),
        'total_periods': len(periods),
        'total_trade_days': len(all_trade_days),
        'missing_tasks': [],
        'summary': {
            'by_stock': {},
            'by_period': {}
        }
    }

    # 只在请求 tick 时增加策略元数据，非 tick 调用的返回字典保持原样，
    # 兼容旧 GUI/CLI 对键集合的严格断言。
    if tick_requested:
        result.update({
            'total_eligible_trade_days': len(tick_eligible_trade_days),
            'ignored_retention_dates': sorted(tick_ignored_retention_dates),
            'retention_excluded_dates': sorted(tick_ignored_retention_dates),
            'ignored_open_dates': sorted(tick_ignored_open_dates),
            'tick_policy': {
                'expected_rows_per_day': TICK_EXPECTED_ROWS_PER_DAY,
                'min_rows_per_day': tick_min_rows,
                'max_age_days': tick_max_age_days,
                'max_span_days': tick_max_span_days,
                'as_of': current_date.strftime('%Y%m%d'),
            },
        })

    if not all_trade_days:
        return result

    table_map = {'1d': 'kline_1d', '1m': 'kline_1m', '5m': 'kline_5m', 'tick': 'tick'}

    def get_existing_dates(db_path: str, table_name: str) -> set:
        """获取数据库中已有的日期"""
        if not os.path.exists(db_path):
            return set()

        try:
            # 完整性扫描只读打开，避免占用写锁。
            conn = duckdb.connect(db_path, read_only=True)
            conn.execute("SET memory_limit='256MB'")

            # 检查表是否存在
            tables = conn.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_name = ?
            """, [table_name]).fetchall()

            if not tables:
                conn.close()
                return set()

            # 查询所有不重复的日期
            dates_result = conn.execute(f"""
                SELECT DISTINCT strftime(time, '%Y%m%d') as date_str
                FROM {table_name}
                WHERE time IS NOT NULL
            """).fetchall()

            conn.close()
            return {row[0] for row in dates_result if row[0]}
        except Exception as e:
            logging.debug(f"获取已有日期失败: {e}")
            return set()

    def group_missing_dates(missing_dates: List[str], sorted_existing: List[str]) -> List[tuple]:
        """
        将缺失日期按连续性分组

        规则：如果两个缺失日期之间有"已存在数据的交易日"，则分开成两个任务
              非交易日不会导致分组

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

    def _period_trade_days(period: object) -> set:
        """返回该周期真正可规划的交易日集合。

        Tick 只允许当前保留窗；日线/分钟线继续使用完整用户区间，保持
        原有扫描语义。
        """

        value = getattr(period, "value", period)
        return (
            set(tick_eligible_trade_days)
            if str(value).strip().lower() == "tick"
            else set(all_trade_days)
        )


    # 交易日集合和日期边界与股票无关。旧实现把集合复制、排序以及 SQL
    # 参数拼接放在每个股票线程里，股票数一多就会重复数千次；预先编译成
    # 只读规格，让工作线程只处理数据库 I/O。
    period_specs = {}
    for period in periods_normalized:
        period_trade_days = _period_trade_days(period)
        sorted_period_days = tuple(sorted(period_trade_days))
        range_values = normalized_period_ranges.get(period)
        range_clause = ""
        range_params = ()
        if range_values:
            range_start, range_end = range_values
            start_sql = f"{range_start[:4]}-{range_start[4:6]}-{range_start[6:]}"
            end_sql = f"{range_end[:4]}-{range_end[4:6]}-{range_end[6:]}"
            range_clause = (
                " AND time >= CAST(? AS TIMESTAMP)"
                " AND time < CAST(? AS TIMESTAMP) + INTERVAL 1 DAY"
            )
            range_params = (start_sql, end_sql)
        period_specs[period] = {
            "trade_days": period_trade_days,
            "sorted_trade_days": sorted_period_days,
            "range_clause": range_clause,
            "range_params": range_params,
        }
    period_specs_key = tuple(
        (
            period,
            (
                len(spec["sorted_trade_days"]),
                spec["sorted_trade_days"][0] if spec["sorted_trade_days"] else "",
                spec["sorted_trade_days"][-1] if spec["sorted_trade_days"] else "",
                hash(spec["sorted_trade_days"]),
            ),
            tuple(spec["range_params"]),
            spec["range_clause"],
        )
        for period, spec in period_specs.items()
    )
    scan_cache_context = (
        os.path.abspath(duckdb_data_path),
        tuple(periods_normalized),
        period_specs_key,
        tuple(dt_required),
        int(tick_min_rows),
        int(tick_max_age_days) if tick_max_age_days is not None else None,
        int(tick_max_span_days),
        current_date.isoformat(),
    )

    def _groups_for_period(
        period: object,
        missing_dates: List[str],
        sorted_existing: List[str],
        expected_dates: Optional[Sequence[str]] = None,
    ) -> List[tuple]:
        """统一生成缺口组，并为 tick 施加 31 个自然日上限。"""

        value = getattr(period, "value", period)
        value = str(value).strip().lower()
        if value != "tick":
            return group_missing_dates(missing_dates, sorted_existing)
        expected = sorted(expected_dates or tick_eligible_trade_days)
        if _group_missing_trade_dates_with_counts is not None:
            return list(_group_missing_trade_dates_with_counts(
                missing_dates,
                expected,
                max_span_days=tick_max_span_days,
            ))
        # 单文件打包时若没有可导入 incremental，保守地按每个自然日拆分；
        # 这样绝不会越过 native bridge 的跨度限制。
        return [(date8, date8, 1) for date8 in sorted(missing_dates)]

    # ========== 优化: 预先扫描存在的数据库文件 ==========
    existing_dbs = set()
    for market in ['SH', 'SZ', 'BJ']:
        market_dir = os.path.join(duckdb_data_path, market)
        if os.path.exists(market_dir):
            try:
                for f in os.listdir(market_dir):
                    if f.endswith('.db'):
                        code = f[:-3]
                        existing_dbs.add(f"{code}.{market}")
            except Exception as e:
                logging.debug(f"扫描 {market} 目录失败: {e}")

    # 分组：有数据库的股票 vs 无数据库的股票
    stocks_with_db = []
    stocks_without_db = []

    for stock in stock_list:
        if stock in existing_dbs:
            stocks_with_db.append(stock)
        else:
            stocks_without_db.append(stock)

    # 快速处理：无数据库的股票，所有周期都标记为缺失
    for stock in stocks_without_db:
        check_periods = (
            stock_periods.get(stock, periods)
            if stock_periods else periods
        )
        for raw_period in check_periods:
            period = str(getattr(raw_period, "value", raw_period)).strip().lower()
            period_days = _period_trade_days(period)
            missing_dates = sorted(period_days)
            if not missing_dates:
                continue
            result['is_complete'] = False
            if period == 'tick':
                groups = _groups_for_period(
                    period,
                    missing_dates,
                    [],
                    expected_dates=sorted(period_days),
                )
            else:
                groups = [
                    (missing_dates[0], missing_dates[-1], len(missing_dates))
                ]
            for group_start, group_end, group_count in groups:
                result['missing_tasks'].append({
                    'stock': stock,
                    'period': period,
                    'start': group_start,
                    'end': group_end,
                    'missing_days': group_count,
                })

    # 遍历有数据库的股票
    total_stocks = len(stocks_with_db)
    current_task_count = 0

    # 在开始前输出一条消息
    if progress_callback:
        progress_callback(0, len(stock_list), "开始扫描股票数据...", 0)

    # ========== 优化: 多线程并行扫描 + read_only + 优化SQL ==========
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 线程安全的结果收集器
    _result_lock = threading.Lock()

    def _scan_one_stock(stock: str) -> list:
        """扫描单只股票的所有周期，返回 (tasks_list, stock)"""
        if stop_flag and stop_flag():
            return []

        if '.' in stock:
            code, market = stock.split('.')
        else:
            code = stock
            market = 'SH' if code.startswith(('6', '5')) else 'SZ'

        db_path = os.path.join(duckdb_data_path, market, f"{code}.db")
        tasks_found = []
        scan_succeeded = False
        try:
            stat = os.stat(db_path)
            # DuckDB 可能把尚未 checkpoint 的写入留在 .wal；把 WAL stat
            # 一并纳入键，避免短锁写入后主库 mtime 未变而误用旧结果。
            try:
                wal_stat = os.stat(db_path + ".wal")
                wal_signature = (
                    int(wal_stat.st_size),
                    int(wal_stat.st_mtime_ns),
                )
            except OSError:
                wal_signature = (0, 0)
            cache_key = (
                scan_cache_context,
                str(stock),
                tuple(
                    str(getattr(value, "value", value)).strip().lower()
                    for value in (
                        stock_periods.get(stock, periods)
                        if stock_periods else periods
                    )
                ),
                int(stat.st_size),
                int(stat.st_mtime_ns),
                wal_signature,
            )
            with _INTEGRITY_SCAN_CACHE_LOCK:
                cached = _INTEGRITY_SCAN_CACHE.get(cache_key)
                if cached is not None:
                    _INTEGRITY_SCAN_CACHE.move_to_end(cache_key)
                    return [dict(item) for item in cached]
        except OSError:
            cache_key = None

        try:
            # 在连接创建阶段一次性设置只读扫描参数，避免每只股票再执行
            # 多条 SET 语句。全市场扫描会打开成百上千个独立数据库，这个
            # 连接启动往返本身会成为可见的固定开销。
            conn = duckdb.connect(
                db_path,
                read_only=True,
                config={"threads": "1", "memory_limit": "256MB"},
            )
            scan_succeeded = True
            # 大 QMT 每个股票库由线程池独立读取；DuckDB 默认还会为单条
            # 查询启动多线程，4/12 个连接叠加后会产生数百线程争抢磁盘，
            # 反而把扫描拖慢。完整性查询是小聚合，单连接单线程更稳。
            # MiniQMT 未进入 optimized_scan 时保持原来的 DuckDB 默认值。
            # 获取所有表
            tables_result = conn.execute("""
                SELECT table_name FROM information_schema.tables
            """).fetchall()
            existing_tables = {row[0] for row in tables_result}

            # 确定要检查的周期
            check_periods = (
                stock_periods.get(stock, periods)
                if stock_periods else periods
            )

            for raw_period in check_periods:
                if stop_flag and stop_flag():
                    break

                period = str(
                    getattr(raw_period, "value", raw_period)
                ).strip().lower()
                table_name = table_map.get(period)
                spec = period_specs.get(period)
                if spec is None:
                    continue
                period_trade_days = spec["trade_days"]
                sorted_period_days = spec["sorted_trade_days"]
                if not table_name or not sorted_period_days:
                    continue

                # 数据库文件存在不代表所选周期已有数据。缺少目标表时必须
                # 生成整段缺口任务；旧逻辑只对 tick 这样做，导致缺少
                # kline_1m/kline_5m 的股票被错误判为完整。
                if table_name not in existing_tables:
                    groups = _groups_for_period(
                        period,
                        sorted_period_days,
                        [],
                        expected_dates=sorted_period_days,
                    )
                    for group_start, group_end, group_count in groups:
                        tasks_found.append({
                            'stock': stock,
                            'period': period,
                            'start': group_start,
                            'end': group_end,
                            'missing_days': group_count,
                        })
                    continue

                if period == 'tick':
                    # Tick 的“当天有一行”不能代表完整：按唯一时间戳和
                    # lastPrice/volume/amount 有效性统计，达到阈值才算完成。
                    coverage = {}
                    try:
                        first_day = sorted_period_days[0]
                        last_day = sorted_period_days[-1]
                        first_sql = (
                            f"{first_day[:4]}-{first_day[4:6]}-"
                            f"{first_day[6:]}"
                        )
                        last_sql = (
                            f"{last_day[:4]}-{last_day[4:6]}-"
                            f"{last_day[6:]}"
                        )
                        tick_rows = conn.execute(f"""
                            SELECT CAST(time AS DATE) AS d,
                                   COUNT(DISTINCT time) AS total_rows,
                                   COUNT(DISTINCT CASE WHEN
                                       lastPrice IS NOT NULL
                                       AND isfinite(TRY_CAST(lastPrice AS DOUBLE))
                                       AND TRY_CAST(lastPrice AS DOUBLE) > 0
                                       AND volume IS NOT NULL
                                       AND isfinite(TRY_CAST(volume AS DOUBLE))
                                       AND amount IS NOT NULL
                                       AND isfinite(TRY_CAST(amount AS DOUBLE))
                                   THEN time END) AS valid_rows
                            FROM tick
                            WHERE time IS NOT NULL
                              AND time >= CAST(? AS DATE)
                              AND time < CAST(? AS DATE) + INTERVAL 1 DAY
                            GROUP BY 1
                        """, [first_sql, last_sql]).fetchall()
                        for row in tick_rows:
                            value = row[0]
                            if value is None:
                                continue
                            if hasattr(value, 'strftime'):
                                date_value = value.strftime('%Y%m%d')
                            else:
                                date_value = str(value).replace('-', '')[:8]
                            coverage[date_value] = {
                                'total_rows': int(row[1] or 0),
                                'valid_rows': int(row[2] or 0),
                            }
                    except Exception as exc:
                        # 缺字段/旧 schema/读取异常都按无覆盖处理，宁可
                        # 生成可重试任务，也不能把坏日静默判完整。
                        scan_succeeded = False
                        logging.debug(
                            "读取 tick 完整度失败 %s: %s", stock, exc
                        )

                    complete_dates = {
                        date_value for date_value, stats in coverage.items()
                        if int(stats.get('total_rows', 0) or 0) >= tick_min_rows
                        and int(stats.get('valid_rows', 0) or 0) >= tick_min_rows
                    }
                    missing = set(sorted_period_days) - complete_dates
                    if missing:
                        groups = _groups_for_period(
                            period,
                            sorted(missing),
                            sorted(complete_dates),
                            expected_dates=sorted_period_days,
                        )
                        for group_start, group_end, group_count in groups:
                            tasks_found.append({
                                'stock': stock,
                                'period': period,
                                'start': group_start,
                                'end': group_end,
                                'missing_days': group_count,
                            })
                    continue

                # 复权完整性：所选复权口径非 none 且为 K 线周期时，要求该日
                # 每个复权口径的 OHLC 四列都存在、非空且为有限数值，否则视为
                # 缺失（tick 无复权字段，跳过）。旧库若缺列，查询会抛错并落入
                # except → 该周期全段判为缺失（保守且正确）。
                adj_filter = ""
                if dt_required and period != 'tick':
                    # 多复权同时选择时，所有口径都必须覆盖；字符串调用
                    # 规范化后同样得到单元素 tuple，保持旧结果不变。
                    required_columns = []
                    for adjustment in dt_required:
                        required_columns.extend(
                            f'"{field}_{adjustment}"'
                            for field in ('open', 'high', 'low', 'close')
                        )
                    adj_filter = "".join(
                        " AND {column} IS NOT NULL"
                        " AND isfinite(TRY_CAST({column} AS DOUBLE))".format(
                            column=column
                        )
                        for column in required_columns
                    )

                # 限定到该周期实际请求区间。之前所有周期都扫描整张表，
                # 导致 1m/5m 在多年历史库上反复读取大量无关行；DuckDB
                # 可以利用时间列的 zone map 直接跳过区间外的数据块。
                range_clause = spec["range_clause"]
                range_params = spec["range_params"]

                # 优化2（仅大 QMT fast path）: 用 CAST(time AS DATE)
                # 替代 strftime，并按周期时间边界裁剪扫描范围；没有显式
                # period_ranges 时保留 MiniQMT/旧脚本原查询语义。
                try:
                    dates_result = conn.execute(f"""
                        SELECT DISTINCT CAST(time AS DATE) as d
                        FROM {table_name}
                        WHERE time IS NOT NULL
                          {range_clause}
                          {adj_filter}
                    """, range_params).fetchall()

                    existing_dates = {row[0].strftime('%Y%m%d') for row in dates_result if row[0]}
                except Exception:
                    scan_succeeded = False
                    existing_dates = set()

                # 计算缺失日期
                missing = period_trade_days - existing_dates
                if missing:
                    missing_dates = sorted(missing)
                    sorted_existing = sorted(existing_dates) if existing_dates else []
                    groups = group_missing_dates(missing_dates, sorted_existing)

                    for group_start, group_end, group_count in groups:
                        tasks_found.append({
                            'stock': stock,
                            'period': period,
                            'start': group_start,
                            'end': group_end,
                            'missing_days': group_count
                        })

            conn.close()
        except Exception as e:
            scan_succeeded = False
            logging.debug(f"处理 {stock} 失败: {e}")

        # 发生锁冲突、schema 异常等失败时不能缓存空任务，否则下一次扫描
        # 会把一次临时错误误当成“本地完整”。
        if cache_key is not None and scan_succeeded:
            with _INTEGRITY_SCAN_CACHE_LOCK:
                _INTEGRITY_SCAN_CACHE[cache_key] = tuple(
                    dict(item) for item in tasks_found
                )
                _INTEGRITY_SCAN_CACHE.move_to_end(cache_key)
                while len(_INTEGRITY_SCAN_CACHE) > _INTEGRITY_SCAN_CACHE_MAX:
                    _INTEGRITY_SCAN_CACHE.popitem(last=False)
        return tasks_found

    # 优化3: 多线程并行扫描。默认仍为 4 线程以保持旧调用的资源占用；
    # 大 QMT GUI 会显式传入自适应值，避免把 MiniQMT/脚本调用的并发行为
    # 一并改变。线程只负责只读 DuckDB，绝不触碰 QMT/复权接口。
    NUM_SCAN_WORKERS = scan_workers
    completed_count = 0
    total_all_stocks = len(stock_list)
    # 进度信号本身会跨线程投递到 Qt 主线程。原实现每只股票都发送一次，
    # 全市场扫描时 UI 队列会积压数千个无意义的刷新；数据库结果仍全部
    # 收集，只把可见进度节流到最多约 100 次。
    progress_step = max(1, min(50, total_all_stocks // 100 or 1))
    last_progress_count = 0

    with ThreadPoolExecutor(max_workers=NUM_SCAN_WORKERS) as executor:
        future_to_stock = {executor.submit(_scan_one_stock, stock): stock for stock in stocks_with_db}

        for future in as_completed(future_to_stock):
            if stop_flag and stop_flag():
                logging.info("数据完整性检查被中断")
                break

            stock = future_to_stock[future]
            completed_count += 1

            try:
                tasks_found = future.result()
                if tasks_found:
                    result['is_complete'] = False
                    result['missing_tasks'].extend(tasks_found)
                    current_task_count += len(tasks_found)

                    # 更新统计信息
                    for t in tasks_found:
                        period = t['period']
                        group_count = t['missing_days']

                        if stock not in result['summary']['by_stock']:
                            result['summary']['by_stock'][stock] = {'total_missing': 0, 'periods': {}}
                        stock_summary = result['summary']['by_stock'][stock]
                        stock_summary['total_missing'] += group_count
                        if period not in stock_summary['periods']:
                            stock_summary['periods'][period] = {'missing_days': 0, 'task_count': 0}
                        stock_summary['periods'][period]['missing_days'] += group_count
                        stock_summary['periods'][period]['task_count'] += 1

                        if period not in result['summary']['by_period']:
                            result['summary']['by_period'][period] = {
                                'stock_count': 0, 'missing_days': 0, 'task_count': 0, 'stocks': set()
                            }
                        period_summary = result['summary']['by_period'][period]
                        period_summary['missing_days'] += group_count
                        period_summary['task_count'] += 1
                        period_summary['stocks'].add(stock)
            except Exception as e:
                logging.debug(f"处理 {stock} 结果失败: {e}")

            # 进度回调：按批次节流，始终保留首个、最后一个进度点。
            if progress_callback and (
                completed_count == total_stocks
                or completed_count - last_progress_count >= progress_step
            ):
                last_progress_count = completed_count
                total_processed = len(stocks_without_db) + completed_count
                progress_callback(
                    total_processed, total_all_stocks,
                    f"扫描 {stock} ({total_processed}/{total_all_stocks}，已发现 {current_task_count} 个缺失任务)",
                    current_task_count,
                )

    # 最终处理 - 计算每个周期涉及的股票数
    for period, stats in result['summary']['by_period'].items():
        stats['stock_count'] = len(stats['stocks'])
        del stats['stocks']  # 删除临时的set

    return result

# ============================================================================
# 兼容性：保留原有的KhQuTools类，但让类方法调用上面的独立函数
# ============================================================================

class KhQuTools:
    """量化工具类（兼容性保留，推荐直接使用模块级函数）"""
    
    def __init__(self):
        # 为了兼容性保留这些属性，但实际会使用模块级函数
        self.trading_periods = _trading_periods
        
    def is_trade_time(self) -> bool:
        """判断是否为交易时间（调用模块级函数）"""
        return is_trade_time()
        
    def is_trade_day(self, date_str: str = None) -> bool:
        """判断是否为交易日（调用模块级函数）"""
        return is_trade_day(date_str)

    def get_trade_days_count(self, start_date: str, end_date: str) -> int:
        """计算指定日期范围内的交易日天数（调用模块级函数）"""
        return get_trade_days_count(start_date, end_date)

    def calculate_moving_average(self, stock_code: str, period: int, field: str = 'close', fre_step: str = '1d', end_time: Optional[str] = None, fq: str = 'pre') -> float:
        """计算移动平均线

        Args:
            stock_code: 股票代码
            period: 周期长度
            field: 计算字段，默认为'close'
            fre_step: 时间频率，如'1d', '1m'等
            end_time: 结束时间，如果为None使用当前时间
            fq: 复权方式，'pre'前复权, 'post'后复权, 'none'不复权

        Returns:
            float: 移动平均值

        Raises:
            ValueError: 如果不在交易时间（日内频率）或数据不足
        """
        from datetime import datetime
        if end_time is None:
            now = datetime.now()
            if fre_step in ['1m', '5m', 'tick']:
                end_time = now.strftime('%Y%m%d %H%M%S')
            else:
                end_time = now.strftime('%Y%m%d')

        # 结合 is_trade_time 判断（仅对日内频率）
        if fre_step in ['1m', '5m', 'tick'] and not self.is_trade_time():
            raise ValueError("不在交易时间内，无法计算日内移动平均线")

        # 获取历史数据（不包含当前时间点）
        data = khHistory(
            symbol_list=stock_code,
            fields=[field],
            bar_count=period,
            fre_step=fre_step,
            current_time=end_time,
            fq=fq,
            force_download=True  # 确保数据最新
        )

        if stock_code not in data or len(data[stock_code]) < period:
            raise ValueError(f"股票 {stock_code} 数据量不足 {period} 条，无法计算 MA{period}")

        prices = data[stock_code][field]
        # 使用动态精度（ETF、LOF 等场内基金均为三位小数）
        decimals = 3 if is_listed_fund_code(stock_code) else 2
        return round(prices.mean(), decimals)


def khMA(stock_code: str, period: int, field: str = 'close', fre_step: str = '1d', end_time: Optional[str] = None, fq: str = 'pre', data: Dict = None) -> float:
    """计算移动平均线（独立函数版本）

    Args:
        stock_code: 股票代码
        period: 周期长度
        field: 计算字段，默认为'close'
        fre_step: 时间频率，如'1d', '1m'等
        end_time: 结束时间，如果为None使用当前时间
        fq: 复权方式，'pre'前复权, 'post'后复权, 'none'不复权
        data: 策略接收的数据对象，用于获取精度设置（可选）

    Returns:
        float: 移动平均值

    Raises:
        ValueError: 如果不在交易时间（日内频率）或数据不足
    """
    from datetime import datetime
    
    if end_time is None:
        now = datetime.now()
        if fre_step in ['1m', '5m', 'tick']:
            end_time = now.strftime('%Y%m%d %H%M%S')
        else:
            end_time = now.strftime('%Y%m%d')

    # 结合 is_trade_time 判断（仅对日内频率）
    tools = KhQuTools()
    if fre_step in ['1m', '5m', 'tick'] and not tools.is_trade_time():
        raise ValueError("不在交易时间内，无法计算日内移动平均线")

    # 获取历史数据（不包含当前时间点）
    history_data = khHistory(
        symbol_list=stock_code,
        fields=[field],
        bar_count=period,
        fre_step=fre_step,
        current_time=end_time,
        fq=fq,
        force_download=False  # 不强制下载数据，提高回测速度
    )

    if stock_code not in history_data or len(history_data[stock_code]) < period:
        raise ValueError(f"股票 {stock_code} 数据量不足 {period} 条，无法计算均线{period}")

    prices = history_data[stock_code][field]
    # 优先从传入的data中获取精度设置，否则根据股票代码判断
    decimals = get_price_decimals(data, stock_code)
    return round(prices.mean(), decimals)


def calculate_max_buy_volume(data: Dict, stock_code: str, price: float, cash_ratio: float = 1.0) -> int:
    """
    计算最大可买入数量，考虑交易成本（包括滑点）

    Args:
        data: 策略接收的数据对象，包含账户信息 __account__ 和框架信息 __framework__
        stock_code: 股票代码
        price: 当前价格
        cash_ratio: 使用可用资金的比例，默认为1.0表示使用全部可用资金

    Returns:
        int: 最大可买入股数(按手取整)
    """
    try:
        # 导入交易管理类
        from khTrade import KhTradeManager

        # 获取账户信息
        account_info = data.get("__account__", {})
        if not account_info:
            logging.warning("无法获取账户信息，无法计算最大买入量")
            return 0

        # 获取资金信息
        available_cash = account_info.get("cash", 0.0)

        # 计算可用的资金
        usable_cash = available_cash * cash_ratio

        # 防止价格为0导致除零错误
        if price <= 0:
            logging.warning(f"股票 {stock_code} 价格异常: {price}，无法计算买入量")
            return 0

        # 获取价格精度设置
        decimals = get_price_decimals(data, stock_code)
        # 对价格进行四舍五入处理
        price = round(price, decimals)

        # 获取框架对象
        framework = data.get("__framework__", None)
        
        # 获取配置对象
        if framework and hasattr(framework, 'config'):
            config = framework.config
        else:
            logging.warning("未从数据字典中获取到框架对象或框架配置不可用，将使用默认交易成本设置")
            config = SimpleNamespace(config_dict={"backtest": {"trade_cost": {}}})
            
        # 创建交易管理器实例（使用实际配置）
        trade_manager = KhTradeManager(config)
        # 把当前成交日期喂给这个临时交易管理器，供过户费按日期分段（与实际成交口径一致）
        trade_manager.current_backtest_date = data.get("__current_time__", {}).get("date")

        # 获取交易成本参数
        commission_rate = trade_manager.commission_rate
        # 过户费"估算种子"用最新(最低)费率 0.00001(沪/深/北非ETF)；精确值在下方 calculate_trade_cost 循环里按
        # 成交日期×市场算（种子偏低→初始股数偏大→循环只下调到精确值，不影响结果，详见 khTrade._transfer_fee_by_date）
        _code = str(stock_code)
        _is_market = _code.upper().endswith((".SH", ".SZ", ".BJ")) or _code.lower().startswith(("sh.", "sz.", "bj."))
        transfer_fee_rate = 0.00001 if (_is_market and not is_listed_fund_code(_code)) else 0.0
        
        # 估算最大股数 (向下取整到100的倍数)
        # 使用更精确的初始估算方式
        estimated_shares = math.floor(usable_cash / price / (1 + commission_rate + transfer_fee_rate))
        shares = math.floor(estimated_shares / 100) * 100

        # 如果估算股数小于100，则无法买入
        if shares < 100:
            return 0

        # 逐步减少股数，使用calculate_trade_cost精确计算成本
        while shares >= 100:
            # 使用calculate_trade_cost计算实际交易成本（包括滑点）
            actual_price, trade_cost = trade_manager.calculate_trade_cost(
                price=price,
                volume=shares,
                direction="buy",
                stock_code=stock_code
            )
            
            # 计算总花费（实际价格 * 数量 + 交易成本）
            total_cost = actual_price * shares + trade_cost

            if total_cost <= usable_cash:
                logging.info(f"计算买入量: 股票={stock_code}, 原始价格={price:.{decimals}f}, 考虑滑点后价格={actual_price:.{decimals}f}, "
                           f"可用现金={available_cash:.{decimals}f}, 使用比例={cash_ratio:.2f}, "
                           f"计划买入={shares}, 成本={trade_cost:.2f}, 总花费={total_cost:.{decimals}f}")
                return int(shares) # 确保返回整数

            shares -= 100 # 减少一手

        return 0 # 循环结束仍未找到合适的买入量

    except Exception as e:
        logging.error(f"计算最大可买入数量时出错: {str(e)}", exc_info=True)
        return 0

def generate_signal(data: Dict, stock_code: str, price: float, ratio: float, action: str, reason: str = "") -> List[Dict]:
    """
    生成标准交易信号

    Args:
        data: 包含时间、账户、持仓信息的字典，以及框架信息 __framework__
        stock_code: 股票代码
        price: 交易价格
        ratio: 当ratio≤1时表示交易比例(买入时指占剩余现金比例，卖出时指占可卖持仓比例)
               当ratio>1时表示买入的股数（必须是100的整数倍）
        action: 'buy' 或 'sell'
        reason: 交易原因

    Returns:
        List[Dict]: 包含单个信号的列表，或空列表
    """
    signals = []
    current_time = data.get("__current_time__", {})
    timestamp = current_time.get("timestamp")
    
    # 获取价格精度设置
    decimals = get_price_decimals(data, stock_code)
    # 对价格进行四舍五入处理
    price = round(price, decimals)

    if action == "buy":
        # 判断ratio是否大于1，若大于1则表示买入股数
        if ratio > 1:
            # 检查股数是否为整百
            target_volume = int(ratio)
            if target_volume % 100 != 0:
                error_msg = f"买入股数必须是100的整数倍: 股票={stock_code}, 输入股数={target_volume}"
                logging.error(error_msg)
                return []
            
            # 计算最大可买入量进行验证
            max_volume = calculate_max_buy_volume(data, stock_code, price, cash_ratio=1.0)
            if max_volume == 0:
                logging.warning(f"无法生成买入信号: 股票={stock_code}, 价格={price:.{decimals}f}, 目标股数={target_volume}, 但资金不足无法买入")
                return []
            elif target_volume > max_volume:
                logging.warning(f"目标买入量超过最大可买入量: 股票={stock_code}, 目标={target_volume}, 最大可买={max_volume}, 将调整为最大可买入量")
                actual_volume = max_volume
            else:
                actual_volume = target_volume
                
            signal = {
                "code": stock_code,
                "action": "buy",
                "price": price,  # 价格已在函数开始时四舍五入
                "volume": actual_volume,
                "reason": reason or f"按价格 {price:.{decimals}f} 买入 {actual_volume}股({actual_volume//100}手)"
            }
            if timestamp:
                signal["timestamp"] = timestamp
            signals.append(signal)
            logging.info(f"生成买入信号: {signal}")
        else:
            # ratio <= 1时按照资金比例计算可买入股数
            max_volume = calculate_max_buy_volume(data, stock_code, price, cash_ratio=ratio)
            if max_volume > 0:
                signal = {
                    "code": stock_code,
                    "action": "buy",
                    "price": price,  # 价格已在函数开始时四舍五入
                    "volume": max_volume,
                    "reason": reason or f"按价格 {price:.{decimals}f} 以 {ratio*100:.0f}% 资金比例买入"
                }
                if timestamp:
                    signal["timestamp"] = timestamp
                signals.append(signal)
                logging.info(f"生成买入信号: {signal}")
            else:
                logging.warning(f"无法生成买入信号: 股票={stock_code}, 价格={price:.{decimals}f}, 资金比例={ratio:.2f}, 计算可买量为0")

    elif action == "sell":
        positions_info = data.get("__positions__", {})
        if stock_code in positions_info:
            # 获取可卖数量，优先使用 'can_use_volume'，否则用 'volume'
            position_data = positions_info[stock_code]
            available_volume = position_data.get("can_use_volume", position_data.get("volume", 0))

            if available_volume > 0:
                # 计算要卖出的股数 (向下取整到100的倍数)
                sell_volume = math.floor((available_volume * ratio) / 100) * 100
                if sell_volume > 0:
                    signal = {
                        "code": stock_code,
                        "action": "sell",
                        "price": price,  # 价格已在函数开始时四舍五入
                        "volume": int(sell_volume), # 确保是整数
                        "reason": reason or f"按价格 {price:.{decimals}f} 卖出 {ratio*100:.0f}% 可用持仓"
                    }
                    if timestamp:
                        signal["timestamp"] = timestamp
                    signals.append(signal)
                    logging.info(f"生成卖出信号: {signal}")
                else:
                    logging.warning(f"无法生成卖出信号: 股票={stock_code}, 价格={price:.{decimals}f}, 持仓比例={ratio:.2f}, 计算可卖量为0 (可用持仓={available_volume})")
            else:
                logging.warning(f"无法生成卖出信号: 股票={stock_code} 无可用持仓")
        else:
            logging.warning(f"无法生成卖出信号: 股票={stock_code} 不在持仓中")

    return signals


def calculate_intraday_features(file_path, sample_file_name, daily_file_name_pattern, feature_types, output_path, output_file_name, trading_minutes=240):
    """
    计算股票的日内特征,并将结果保存到csv文件中。

    参数:
    - file_path: str
        股票数据文件所在的目录路径。
    - sample_file_name: str
        样本文件名,用于提取周类型、起始日期和束日期。
        样文件名应该遵循以下格式: "股票代码_周期类型_起始日_结束日期_时间范围.csv"
        例如: "000001.SZ_1m_20240101_20240430_09_30-11_30.csv"
    - daily_file_name_pattern: str
        日数据文件名的模式,用构造与分钟数据对应的日数据文件名。
        模式中应该包含股票代码的占位符,例如: "000001.SZ_1d_20240101_20240430_all.csv"
    - feature_types: list
        要计算的特征类型列表,可选值包括: 'volume_ratio', 'return_rate'。
    - output_path: str
        输出文件的目录路径。
    - output_file_name: str
        输出文件名。
    - trading_minutes: int, 可选, 默认为240
        每个交易日的交易分钟数,用于计算成交量比例。默认为240分钟(4小时)

    函数功能:
    1. 根据样本文件名提取周期类型、起始日期和结束日期。
    2. 获取与样本文件名格式相同的所有文件。
    3. 对每个文件:
       - 从文件路径中提取股票代码。
       - 读取逐分钟数据文件。
       - 构造正确的日数据文件名,并读取日数据文件。
       - 计算过去5天的平均交易量。
       - 获取前一天的收盘价。
       - 将分钟数据和日数据按日期合并。
       - 根据指定的特征类型计算相应的特征值。
       - 删除不要的列,并添加股票代码列。
       - 去掉前5天(包括第5天)和最后一天的数据。
    4. 如果输出路径不存在,则创建文件夹。
    5. 将计算结果保存到csv文件中,如果文件已经存在,则追加数据。

    返回值:
    无返回值,计算结果直接保存到指定的输出文件中。
    """

    # 从样本文件名中提取周期类型、起始日期和结束日期
    file_name_parts = sample_file_name.split('_')
    data_type = file_name_parts[1]
    start_date = file_name_parts[2]
    end_date = file_name_parts[3]

    # 获取与样本文件名格式相同的所有文件
    file_pattern = f"*_{data_type}_{start_date}_{end_date}_*.csv"
    file_list = glob.glob(os.path.join(file_path, file_pattern))

    # 理每文件
    for idx, minute_file_path in enumerate(file_list):
        # 从文件路径中提取股票代码
        stock_code = os.path.basename(minute_file_path).split('_')[0]

        # 读取逐分钟数据文件
        minute_data = pd.read_csv(minute_file_path)

        # 构造正确的日数据文件名
        stock_code_example = daily_file_name_pattern.split('_')[0]
        daily_file_name = daily_file_name_pattern.replace(stock_code_example, stock_code)
        daily_file_path = os.path.join(file_path, daily_file_name)
        daily_data = pd.read_csv(daily_file_path)

        # 计算过去5天的平均交易量
        daily_data['past_avg_volume'] = daily_data['volume'].rolling(window=5).mean().shift(1)

        # 获取前一天的收盘价
        daily_data['prev_close'] = daily_data['close'].shift(1)

        minute_data['date'] = pd.to_datetime(minute_data['date'])
        daily_data['date'] = pd.to_datetime(daily_data['date'])

        # 检查分钟数据否有 'close' 列,如果没有,则使用 'price' 列
        if 'close' not in minute_data.columns:
            minute_data['price'] = minute_data['price']
        else:
            minute_data['price'] = minute_data['close']

        # 按日期合并分钟据和日数据
        merged_data = pd.merge(minute_data, daily_data[['date', 'past_avg_volume', 'prev_close']], on='date', how='left')

        eps = 1e-8  # 添加一个小的常数

        # 计算特征
        for feature_type in feature_types:
            if feature_type == 'volume_ratio':
                merged_data['volume_ratio'] = merged_data.apply(lambda x: x['volume'] / (x['past_avg_volume'] / trading_minutes + eps) if pd.notna(x['past_avg_volume']) else np.nan, axis=1)
            elif feature_type == 'return_rate':
                merged_data['return_rate'] = merged_data.apply(lambda x: (x['price'] - x['prev_close']) / x['prev_close'] if pd.notna(x['prev_close']) else np.nan, axis=1)

        # 删除不要的列
        merged_data = merged_data[['date', 'time'] + feature_types]
        merged_data['stock_code'] = stock_code  # 添加股票代码列

        # 去掉前6天(包括第6天)和最后一天的数据
        min_date = merged_data['date'].min()
        max_date = merged_data['date'].max()
        merged_data = merged_data[(merged_data['date'] > min_date + pd.Timedelta(days=6)) & (merged_data['date'] < max_date)]

        # 如果输出路径不存在,则创建文件夹
        if not os.path.exists(output_path):
            os.makedirs(output_path)

        # 保存结果到csv文件,如果文件已经存在,则追加数据
        output_file_path = os.path.join(output_path, output_file_name)
        header = idx == 0  # 如果是第一个文件,则写入表头,否则不写入
        mode = 'w' if idx == 0 else 'a'  # 如果是第一个文件,则写入模式为'w',否则为'a'(追加)
        merged_data.to_csv(output_file_path, index=False, header=header, mode=mode)

def calculate_next_day_return(file_path, sample_file_name, feature_types, output_path, output_file_name):
    """
    计算股票的下一个交易日收益率,并将结果保存到csv文件中。

    参数:
    - file_path: str
        股票数据文件所在的目录路径。
    - sample_file_name: str
        样本文件名,用于提取起始日期和结束日期。
        样本文件名应该遵循以下格式: "股票代码_1d_起始日期_结束日期_all.csv"
        例如: "000001.SZ_1d_20240101_20240430_all.csv"
    - feature_types: list
        要计算的特征类型列表,目前支持: 'next_day_return_rate' (下一个交易日收益率)。
    - output_path: str
        输出文件的目录路径。
    - output_file_name: str
        输出文件名。

    函数功能:
    1. 根据样本文件名提取起始日期和结束日期
    2. 获取与样本文件名格式相同的所有文件。
    3. 对每个文件:
       - 从文件路径中提取股票代码。
       - 取日数据文件。
       - 将期列转换为日期时间类型。
       - 如果 'next_day_return_rate' 在特征类型列表中,计算下一个交易日的收盘价收益率,并将其记录到当前交易日。
       - 提取日级别的数据,每个日期只保留一条记录,包括日期和指定的特征。
       - 添加股票代码列。
       - 去掉前6天(包括第6天)和最后一天的数据。
    4. 如果输出路径不存在,则创建文件夹。
    5. 将计算结果保存到csv文件中,如果文件已经存在,则追加数据。

    返回值:
    无返回值,计算结果直接保存到指定的输出文件中。
    """
    # 从样本文件名中提取起始日期和结束日期
    file_name_parts = sample_file_name.split('_')
    start_date = file_name_parts[2]
    end_date = file_name_parts[3]

    # 获取与样本文件名格式相同的所有文件
    file_pattern = f"*_1d_{start_date}_{end_date}_all.csv"
    file_list = glob.glob(os.path.join(file_path, file_pattern))

    # 处理每个文件
    for idx, daily_file_path in enumerate(file_list):
        # 从文路径中提取股票代码
        stock_code = os.path.basename(daily_file_path).split('_')[0]

        # 读取日数据文件
        daily_data = pd.read_csv(daily_file_path)

        # 将日期列转换为日期时间类型
        daily_data['date'] = pd.to_datetime(daily_data['date'])

        # 计算第二天的收盘收益率,并将其记录到当天
        if 'next_day_return_rate' in feature_types:
            daily_data['next_day_return_rate'] = daily_data['close'].pct_change().shift(-1)

        # 提取日级别的数据,每个日期只保留一条记录
        daily_data = daily_data[['date'] + [feature for feature in feature_types if feature in daily_data.columns]].dropna().drop_duplicates(subset='date')
        daily_data['stock_code'] = stock_code  # 添加股票代码列

        # 去掉前6天(包括第6天)和最后一天的数据
        min_date = daily_data['date'].min()
        max_date = daily_data['date'].max()
        second_last_date = max_date  
        daily_data = daily_data[(daily_data['date'] > min_date + pd.Timedelta(days=6)) & (daily_data['date'] <= second_last_date)]

        # 如果输出路径不存在,则创建文件夹
        if not os.path.exists(output_path):
            os.makedirs(output_path)

        # 保存结果到csv文件,如果文件已经存在,则追加数据
        output_file_path = os.path.join(output_path, output_file_name)
        header = idx == 0  # 如果是第一个文件,则写入表头,否则不写入
        mode = 'w' if idx == 0 else 'a'  # 如果是第一个文件,则写入模式为'w',否则为'a'(追加)
        daily_data.to_csv(output_file_path, index=False, header=header, mode=mode)


_STOCK_LIST_BOARD_NAMES = {
    'sh_a': '上证A股',
    'sz_a': '深证A股',
    'gem': '创业板',
    'sci': '科创板',
    'hs_a': '沪深A股',
    'indices': '指数',
    'all_stocks': '全部股票',
    'hs300_components': '沪深300成分股',
    'zz500_components': '中证500成分股',
    'sz50_components': '上证50成分股',
    'hs_convertible_bonds': '沪深转债',
    'hs_etf': '沪深ETF',
    'hs_fund': '沪深场内基金',
}

_STOCK_LIST_SPECIAL_FILENAMES = {
    'hs_convertible_bonds': '沪深转债_列表.csv',
    'hs_etf': '沪深ETF_成分股列表.csv',
    'hs_fund': '沪深基金_列表.csv',
}


def _stock_list_file_path(output_dir, board):
    filename = _STOCK_LIST_SPECIAL_FILENAMES.get(board)
    if filename is None:
        filename = f"{_STOCK_LIST_BOARD_NAMES[board]}_股票列表.csv"
    return os.path.join(output_dir, filename)


def _read_existing_stock_rows(file_path):
    rows = {}
    if not os.path.isfile(file_path):
        return rows
    try:
        with open(file_path, 'r', encoding='utf-8-sig', newline='') as handle:
            for row in csv.reader(handle):
                if not row:
                    continue
                code = normalize_stock_code(str(row[0] or '').strip()).upper()
                if not code:
                    continue
                name = str(row[1] or '').strip() if len(row) > 1 else ''
                rows.setdefault(code, name)
    except (OSError, UnicodeError, csv.Error) as exc:
        logging.warning("读取旧股票池失败 %s: %s", file_path, exc)
    return rows


def _prepare_stock_rows(stocks, old_names):
    prepared = {}
    for stock in stocks or []:
        if not isinstance(stock, dict):
            continue
        code = normalize_stock_code(str(stock.get('code', '') or '').strip()).upper()
        if not code:
            continue
        name = str(stock.get('name', '') or '').strip() or old_names.get(code, '')
        if code not in prepared or (not prepared[code] and name):
            prepared[code] = name
    return sorted(prepared.items())


def _atomic_write_stock_rows(file_path, rows):
    """Write one CSV atomically so an interrupted update cannot truncate it."""
    output_dir = os.path.dirname(os.path.abspath(file_path))
    os.makedirs(output_dir, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8-sig',
            newline='',
            delete=False,
            dir=output_dir,
            prefix=f".{os.path.basename(file_path)}.",
            suffix='.tmp',
        ) as handle:
            temp_path = handle.name
            writer = csv.writer(handle, lineterminator='\n')
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, file_path)
    except Exception:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise


def _save_stock_lists(stock_dict, output_dir, *, progress=None, skip_boards=None):
    """Shared safe writer used by CLI and GUI child process.

    Empty/unsupported results and implausible (>30%) shrinkage preserve the old
    file. This is especially important when the GUI falls back to BaoStock,
    which cannot provide complete ETF, LOF/fund or convertible-bond sectors.
    """
    os.makedirs(output_dir, exist_ok=True)
    skip_boards = set(skip_boards or ())
    outcome = {
        'written': [],
        'preserved': [],
        'written_details': [],
        'preserved_details': [],
    }

    for board, stocks in (stock_dict or {}).items():
        if board not in _STOCK_LIST_BOARD_NAMES:
            logging.warning("忽略未知股票池键: %s", board)
            continue
        label = _STOCK_LIST_BOARD_NAMES[board]
        file_path = _stock_list_file_path(output_dir, board)
        filename = os.path.basename(file_path)

        if board in skip_boards:
            outcome['preserved'].append(label)
            outcome['preserved_details'].append({
                'board': board,
                'label': label,
                'filename': filename,
                'reason': '接口未提供该板块，安全保留原文件',
            })
            message = f"{label}：接口未提供该板块，安全保留原文件"
            logging.warning(message)
            if progress:
                progress(message)
            continue

        # 安装版首次更新时，目标通常位于 LocalAppData，而旧基线仍在随包
        # data 目录。若只检查目标文件，首次出现“非空但严重缺失”的行情结果
        # 就会绕过缩水保护并遮住完整随包清单，因此同时以当前可解析基线校验。
        baseline_path = file_path
        if not os.path.isfile(baseline_path):
            resolved_baseline = get_stock_pool_path(os.path.basename(file_path))
            if os.path.isfile(resolved_baseline):
                baseline_path = resolved_baseline
        old_names = _read_existing_stock_rows(baseline_path)
        rows = _prepare_stock_rows(stocks, old_names)
        if not rows:
            outcome['preserved'].append(label)
            outcome['preserved_details'].append({
                'board': board,
                'label': label,
                'filename': filename,
                'reason': '本次结果为空，未覆盖原文件',
            })
            message = f"{label}：本次结果为空，未覆盖原文件"
            logging.warning(message)
            if progress:
                progress(message)
            continue

        # 正常成分变更不会让大型板块一次丢掉三成以上成员；这种结果更可能是
        # 行情客户端或详情接口暂时不完整。小列表不启用阈值，避免误挡正常调整。
        if len(old_names) >= 20 and len(rows) * 10 < len(old_names) * 7:
            outcome['preserved'].append(label)
            outcome['preserved_details'].append({
                'board': board,
                'label': label,
                'filename': filename,
                'reason': f'数量异常缩水 {len(old_names)}→{len(rows)}，未覆盖原文件',
            })
            message = (
                f"{label}：数量异常缩水 {len(old_names)}→{len(rows)}，"
                "未覆盖原文件"
            )
            logging.warning(message)
            if progress:
                progress(message)
            continue

        if progress:
            progress(f"正在保存{label}列表...")
        _atomic_write_stock_rows(file_path, rows)
        outcome['written'].append(label)
        outcome['written_details'].append({
            'board': board,
            'label': label,
            'filename': filename,
            'count': len(rows),
        })
        logging.info("%s列表保存完成，共 %s 只证券", label, len(rows))
        if progress:
            progress(f"{label}列表保存完成，共 {len(rows)} 只证券")

    return outcome


def save_stock_list_to_csv(stock_dict, output_dir, skip_boards=None):
    """将股票列表安全保存为 CSV，返回写入/保留摘要。"""
    try:
        outcome = _save_stock_lists(
            stock_dict,
            output_dir,
            skip_boards=skip_boards,
        )
        logging.info(f"股票列表已保存到目录: {output_dir}")
        return outcome
    except Exception as e:
        logging.error(f"保存股票列表时出错: {str(e)}", exc_info=True)
        raise


def _empty_stock_list_dict():
    return {
        'sh_a': [],
        'sz_a': [],
        'gem': [],
        'sci': [],
        'hs_a': [],
        'indices': [],
        'all_stocks': [],
        'hs300_components': [],
        'zz500_components': [],
        'sz50_components': [],
        'hs_convertible_bonds': [],
        'hs_etf': [],
        'hs_fund': [],
    }


def _important_indices():
    return [
        {'code': '000001.SH', 'name': '上证指数'},
        {'code': '399001.SZ', 'name': '深证成指'},
        {'code': '399006.SZ', 'name': '创业板指'},
        {'code': '000688.SH', 'name': '科创50'},
        {'code': '000300.SH', 'name': '沪深300'},
        {'code': '000905.SH', 'name': '中证500'},
        {'code': '000852.SH', 'name': '中证1000'}
    ]


def _normalize_baostock_code(code):
    code = (code or '').strip()
    if not code or '.' not in code:
        return ''
    market, raw = code.split('.', 1)
    market = market.upper()
    raw = raw.strip()
    if market == 'SH':
        return f"{raw}.SH"
    if market == 'SZ':
        return f"{raw}.SZ"
    if market == 'BJ':
        return f"{raw}.BJ"
    return ''


def _stock_name_from_baostock_row(row):
    return (
        row.get('code_name')
        or row.get('name')
        or row.get('stock_name')
        or row.get('股票名称')
        or ''
    ).strip()


def _collect_baostock_rows(rs):
    fields = list(getattr(rs, 'fields', []) or [])
    rows = []
    while getattr(rs, 'error_code', '0') == '0' and rs.next():
        rows.append(dict(zip(fields, rs.get_row_data())))
    if getattr(rs, 'error_code', '0') != '0':
        raise RuntimeError(getattr(rs, 'error_msg', '') or getattr(rs, 'error_code', 'baostock query failed'))
    return rows


def _append_unique_stock(target, stock_info):
    if not stock_info or not stock_info.get('code'):
        return
    code = stock_info['code']
    for existing in target:
        if existing.get('code') == code:
            if not existing.get('name') and stock_info.get('name'):
                existing['name'] = stock_info['name']
            return
    target.append(dict(stock_info))


def _finalize_stock_dict(stock_dict):
    for index in _important_indices():
        _append_unique_stock(stock_dict['indices'], index)
        _append_unique_stock(stock_dict['all_stocks'], index)
    for board in stock_dict:
        if board == 'all_stocks':
            unique_stocks = {stock['code']: stock for stock in stock_dict[board]}.values()
            stock_dict[board] = sorted(unique_stocks, key=lambda x: x['code'])
        else:
            stock_dict[board] = sorted(
                {stock['code']: stock for stock in stock_dict[board]}.values(),
                key=lambda x: x['code']
            )
    return stock_dict


def get_stock_list_from_baostock(queue=None):
    """使用 BaoStock 回退生成基础股票池和主要指数成分股。

    BaoStock 可以提供沪深 A 股列表和沪深300/上证50/中证500成分股；
    不提供 miniQMT 同等的转债、ETF、LOF/场内基金板块列表；保存时会保留
    这些板块的原文件，绝不以空结果覆盖。
    """
    import socket as _socket
    import baostock as bs

    def progress(message):
        if queue is not None:
            queue.put(("progress", message))
        print(f"[更新进度] {message}", flush=True)

    from baostock_proxy import enable_baostock_proxy

    old_timeout = _socket.getdefaulttimeout()
    _socket.setdefaulttimeout(20)
    enable_baostock_proxy()
    lg = bs.login()
    if lg.error_code != '0':
        raise RuntimeError(f"BaoStock 登录失败: {lg.error_msg or lg.error_code}")

    try:
        stock_dict = _empty_stock_list_dict()
        progress("miniQMT不可用，已切换到BaoStock更新A股和主要指数成分股...")

        progress("正在从BaoStock获取A股基础列表...")
        basic_rows = _collect_baostock_rows(bs.query_stock_basic())
        for row in basic_rows:
            raw_code = row.get('code', '')
            code = _normalize_baostock_code(raw_code)
            if not code or not (code.endswith('.SH') or code.endswith('.SZ')):
                continue
            if row.get('type') and row.get('type') != '1':
                continue
            if row.get('status') and row.get('status') != '1':
                continue
            name = _stock_name_from_baostock_row(row)
            if not name:
                continue
            stock_info = {'code': code, 'name': name}
            if code.endswith('.SH'):
                _append_unique_stock(stock_dict['sh_a'], stock_info)
                if code.startswith('688'):
                    _append_unique_stock(stock_dict['sci'], stock_info)
            elif code.endswith('.SZ'):
                _append_unique_stock(stock_dict['sz_a'], stock_info)
                if code.startswith(('300', '301')):
                    _append_unique_stock(stock_dict['gem'], stock_info)
            _append_unique_stock(stock_dict['hs_a'], stock_info)
            _append_unique_stock(stock_dict['all_stocks'], stock_info)

        component_queries = [
            ('沪深300', 'hs300_components', bs.query_hs300_stocks),
            ('上证50', 'sz50_components', bs.query_sz50_stocks),
            ('中证500', 'zz500_components', bs.query_zz500_stocks),
        ]
        name_map = {stock['code']: stock['name'] for stock in stock_dict['hs_a']}
        for index_name, dict_key, query_fn in component_queries:
            progress(f"正在从BaoStock获取{index_name}成分股...")
            for row in _collect_baostock_rows(query_fn()):
                code = _normalize_baostock_code(row.get('code', ''))
                name = _stock_name_from_baostock_row(row) or name_map.get(code, '')
                if code and name:
                    stock_info = {'code': code, 'name': name}
                    _append_unique_stock(stock_dict[dict_key], stock_info)
                    _append_unique_stock(stock_dict['all_stocks'], stock_info)

        progress(
            "BaoStock不提供完整转债、ETF及场内基金板块列表；"
            "这些文件不会被覆盖，如需更新请使用miniQMT。"
        )
        return _finalize_stock_dict(stock_dict)
    finally:
        try:
            bs.logout()
        finally:
            _socket.setdefaulttimeout(old_timeout)


def stock_list_worker(output_dir, queue, preferred_source=None):
    """多进程工作函数：在子进程中用 BaoStock 更新股票与成分股列表。

    开源版只有 BaoStock 一个股票列表来源。BaoStock 不提供可转债、ETF、基金
    列表，这三个文件保留随包 / 上次更新的版本。
    """
    try:
        # Windows multiprocessing 保护
        import multiprocessing
        if hasattr(multiprocessing, 'set_start_method'):
            try:
                multiprocessing.set_start_method('spawn', force=True)
            except RuntimeError:
                pass  # 可能已经设置过了

        queue.put(("progress", "正在连接 BaoStock 获取股票与成分股列表..."))
        stock_dict = get_stock_list_from_baostock(queue)
        queue.put(("progress", "BaoStock 股票列表获取完成"))

        # 发送进度消息
        queue.put(("progress", "正在保存股票列表..."))
        unsupported_boards = {'hs_convertible_bonds', 'hs_etf', 'hs_fund'}

        outcome = save_stock_list_to_csv_for_subprocess(
            stock_dict,
            output_dir,
            queue,
            skip_boards=unsupported_boards,
        )
        queue.put(("progress", "股票列表保存完成"))

        # 发送完成消息（详细列出更新了哪几个文件分别是什么列表）
        src_label = "BaoStock"

        written = outcome.get('written_details', [])
        preserved = outcome.get('preserved_details', [])

        msg_lines = [f"<div style='min-width: 620px;'><b>数据源：{src_label}</b><br>"]

        if written:
            msg_lines.append(f"<br><b>已成功更新以下 {len(written)} 个列表文件：</b><br>")
            for idx, item in enumerate(written, 1):
                msg_lines.append(
                    f"{idx}. <b>{item['filename']}</b>（{item['label']} · {item['count']} 只）<br>"
                )

        if preserved:
            msg_lines.append(f"<br><b>以下 {len(preserved)} 个列表文件已保留原文件：</b><br>")
            for item in preserved:
                msg_lines.append(
                    f"• <b>{item['filename']}</b>（{item['label']}：{item['reason']}）<br>"
                )

        msg_lines.append("</div>")
        final_msg = "".join(msg_lines)
        queue.put(("finished", True, final_msg))

    except Exception as e:
        error_msg = f"更新股票列表时出错: {str(e)}"
        logging.error(error_msg, exc_info=True)
        queue.put(("finished", False, error_msg))


def save_stock_list_to_csv_for_subprocess(stock_dict, output_dir, queue, skip_boards=None):
    """子进程版本的保存CSV函数，带进度反馈"""
    def progress(message):
        queue.put(("progress", message))
        print(f"[更新进度] {message}", flush=True)

    return _save_stock_lists(
        stock_dict,
        output_dir,
        progress=progress,
        skip_boards=skip_boards,
    )

# 定义多进程版本的更新管理器类
# 仅在主进程 + PyQt5 可用（即 Windows GUI 环境）时定义；
# Linux/无 PyQt5 环境下 GUI 已禁用，不会调用到该类。
if _KHQUANT_HEADLESS:
    _HAS_PYQT5 = False
else:
    try:
        from PyQt5.QtCore import QObject, pyqtSignal, QTimer  # type: ignore
        _HAS_PYQT5 = True
    except ImportError:
        _HAS_PYQT5 = False

if _HAS_PYQT5 and not is_subprocess():
    import multiprocessing
    import queue
    
    class StockListUpdateManager(QObject):
        """多进程股票列表更新管理器"""
        progress = pyqtSignal(str)  # 用于发送进度信息
        finished = pyqtSignal(bool, str)  # 用于发送完成状态和消息

        def __init__(self, output_dir, preferred_source=None):
            super().__init__()
            self.output_dir = output_dir
            self.preferred_source = preferred_source
            self.process = None
            self.queue = None
            self.timer = None
            self.running = True

        def start(self):
            """启动多进程更新"""
            try:
                # Windows multiprocessing 配置
                if hasattr(multiprocessing, 'set_start_method'):
                    try:
                        multiprocessing.set_start_method('spawn', force=True)
                    except RuntimeError:
                        pass  # 可能已经设置过了
                
                # 创建进程间通信队列
                self.queue = multiprocessing.Queue()
                
                # 创建子进程
                self.process = multiprocessing.Process(
                    target=stock_list_worker,
                    args=(self.output_dir, self.queue, self.preferred_source)
                )
                
                # 启动子进程
                self.process.start()
                
                # 创建定时器检查队列消息
                self.timer = QTimer()
                self.timer.timeout.connect(self.check_queue)
                self.timer.start(100)  # 每100ms检查一次
                
            except Exception as e:
                error_msg = f"启动多进程更新时出错: {str(e)}"
                logging.error(error_msg, exc_info=True)
                self.finished.emit(False, error_msg)

        def check_queue(self):
            """检查进程队列中的消息"""
            try:
                while True:
                    try:
                        # 非阻塞获取消息
                        message = self.queue.get_nowait()
                        
                        if message[0] == "progress":
                            # 进度消息
                            self.progress.emit(message[1])
                        elif message[0] == "finished":
                            # 完成消息
                            success, msg = message[1], message[2]
                            self.stop()
                            self.finished.emit(success, msg)
                            break
                            
                    except queue.Empty:
                        break
                        
                # 检查进程是否还在运行
                if self.process and not self.process.is_alive():
                    # 进程已结束，但没有收到完成消息，可能是异常结束
                    exit_code = self.process.exitcode
                    if exit_code != 0:
                        self.stop()
                        self.finished.emit(False, f"更新进程异常结束，退出码: {exit_code}")
                        
            except Exception as e:
                logging.error(f"检查队列消息时出错: {str(e)}")

        def stop(self):
            """停止更新进程"""
            self.running = False
            
            if self.timer:
                self.timer.stop()
                self.timer = None
                
            if self.process and self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=5)  # 等待5秒
                if self.process.is_alive():
                    self.process.kill()  # 强制结束
                    
            self.process = None
            self.queue = None

def get_and_save_stock_list(output_dir, preferred_source=None):
    """获取并保存股票列表的便捷函数，返回多进程更新管理器实例"""
    # 检查是否在主进程中
    if not is_subprocess():
        # 在主进程中使用多进程管理器
        update_manager = StockListUpdateManager(
            output_dir,
            preferred_source=preferred_source,
        )
        return update_manager
    else:
        # 在子进程中直接执行，不使用Qt相关功能
        try:
            stock_dict = get_stock_list_from_baostock()
            save_stock_list_to_csv(stock_dict, output_dir, skip_boards={'hs_convertible_bonds', 'hs_etf', 'hs_fund'})
            return True, "股票列表更新成功！"
        except Exception as e:
            error_msg = f"更新股票列表时出错: {str(e)}"
            logging.error(error_msg, exc_info=True)
            return False, error_msg


def get_stock_names(stock_codes, stock_list_file):
    """
    从股票列表文件中查询股票名称
    
    Args:
        stock_codes (list): 股票代码列表
        stock_list_file (str): 股票列表文件路径
    
    Returns:
        dict: 股票代码到股票名称的映射字典
    """
    stock_names = {}
    try:
        with open(stock_list_file, 'r', encoding='utf-8-sig') as f:  # 使用utf-8-sig处理BOM
            for line in f:
                if line.strip():
                    parts = line.strip().split(',')
                    if len(parts) >= 2:
                        code = parts[0].strip()
                        name = parts[1].strip()
                        if code in stock_codes:
                            stock_names[code] = name
    except Exception as e:
        logging.error(f"读取股票列表文件出错: {str(e)}")

    return stock_names

# 股票名称缓存（全局）
_stock_name_cache = {}
_stock_name_cache_loaded = False

def get_stock_name(stock_code: str) -> str:
    """
    获取单个股票的名称

    从data目录下的股票列表文件中查询股票名称，首次调用时加载所有股票名称到缓存。

    Args:
        stock_code: 股票代码，如 '000001.SZ'

    Returns:
        股票名称，如果未找到返回空字符串
    """
    global _stock_name_cache, _stock_name_cache_loaded

    # 如果缓存未加载，加载所有股票名称
    if not _stock_name_cache_loaded:
        _load_all_stock_names()
        _stock_name_cache_loaded = True

    return _stock_name_cache.get(stock_code, '')


def get_stock_display(stock_code: str) -> str:
    """
    返回用于显示的股票代码和名称：'代码 (名称)'，若名称不存在则只返回代码。
    便于在界面或日志中统一显示股票信息。
    """
    name = get_stock_name(stock_code)
    if name:
        return f"{stock_code} ({name})"
    else:
        return stock_code

def _load_all_stock_names():
    """加载所有股票名称到缓存"""
    global _stock_name_cache

    # 安装版优先读取用户更新，缺少的文件再回退到打包默认数据。
    possible_dirs = get_stock_pool_read_dirs(include_missing=False)

    # 优先使用的股票列表文件（按优先级排序）
    # 增加 ETF 和 可转债 的文件名，以便加载这些类别的名称
    stock_list_files = [
        '沪深A股_股票列表.csv',
        '全部股票_股票列表.csv',
        '上证A股_股票列表.csv',
        '深证A股_股票列表.csv',
        '创业板_股票列表.csv',
        '科创板_股票列表.csv',
        '指数_股票列表.csv',
        '沪深ETF_成分股列表.csv',     # 新增：ETF 列表（save_stock_list_to_csv 使用的文件名）
        '沪深基金_列表.csv',          # 场内基金（含 ETF/LOF 等）
        '沪深转债_列表.csv',         # 新增：可转债 列表（save_stock_list_to_csv 使用的文件名）
    ]

    for data_dir in possible_dirs:
        if not os.path.exists(data_dir):
            continue

        for filename in stock_list_files:
            filepath = os.path.join(data_dir, filename)
            if os.path.exists(filepath):
                try:
                    with open(filepath, 'r', encoding='utf-8-sig') as f:
                        for line in f:
                            if line.strip():
                                parts = line.strip().split(',')
                                if len(parts) >= 2:
                                    code = parts[0].strip()
                                    name = parts[1].strip()
                                    # 只添加不存在的，避免覆盖
                                    if code and code not in _stock_name_cache:
                                        _stock_name_cache[code] = name
                except Exception as e:
                    logging.warning(f"读取股票列表文件 {filepath} 出错: {e}")

    logging.info(f"已加载 {len(_stock_name_cache)} 个股票名称到缓存")

def reload_stock_name_cache():
    """重新加载股票名称缓存"""
    global _stock_name_cache, _stock_name_cache_loaded
    _stock_name_cache = {}
    _stock_name_cache_loaded = False
    _load_all_stock_names()
    _stock_name_cache_loaded = True

def khDuckWrite(
    stock_list,
    period,
    data,
    fields=None,
    time_col="time",
    duckdb_path=None,
    field_types=None,
    insert_missing=False,
    update_time=True
):
    """
    向 DuckDB 指定周期表写入自定义字段数据，必要时自动新增字段。

    功能:
        - 按股票/周期定位表并写入字段值
        - 缺字段时自动 ALTER TABLE 添加
        - 默认仅更新已有 time 记录，避免插入空行
        - 可选插入不存在的 time 记录

    参数:
        stock_list: 股票代码或列表，支持 '000001.SZ' / 'sz.000001' 等格式
        period: 周期类型，'1d' / '1m' / '5m' / 'tick'
        data: 数据源
            - dict: {stock_code: DataFrame}
            - DataFrame: 单股票时可不含 stock_code，多股票需包含 stock_code
        fields: 要写入的字段名或列表，None 表示从 data 推断
        time_col: 时间列名，默认 'time'
        duckdb_path: DuckDB 数据根目录，None 使用默认路径
        field_types: 字段类型映射，如 {'ma5': 'DOUBLE'}
        insert_missing: 是否插入不存在的 time 记录，默认 False
        update_time: 是否写入 update_time 列，默认 True

    返回:
        dict: {stock_code: {'updated': int, 'inserted': int, 'rows': int, 'fields': list}}
    """
    if not stock_list:
        raise ValueError("stock_list不能为空")
    if not period:
        raise ValueError("period不能为空")
    if data is None:
        raise ValueError("data不能为空")

    if isinstance(stock_list, str):
        stock_codes = [stock_list]
    else:
        stock_codes = list(stock_list)
    stock_codes = [normalize_stock_code(code) for code in stock_codes]

    if isinstance(data, dict):
        data_map = {normalize_stock_code(k): v for k, v in data.items()}
    elif isinstance(data, pd.DataFrame):
        data_map = {}
        if "stock_code" in data.columns:
            for code, group in data.groupby("stock_code"):
                data_map[normalize_stock_code(code)] = group.copy()
        else:
            if len(stock_codes) != 1:
                raise ValueError("data为DataFrame且未包含stock_code列时，stock_list只能是单只股票")
            data_map[stock_codes[0]] = data.copy()
    else:
        raise ValueError("data必须是dict或DataFrame")

    if fields is None:
        fields_list = None
    elif isinstance(fields, str):
        fields_list = [fields]
    else:
        fields_list = list(fields)

    try:
        from duckdb_storage.manager import DuckDBManager
        manager = DuckDBManager(data_root=duckdb_path) if duckdb_path else DuckDBManager()
    except Exception as e:
        raise RuntimeError(f"初始化DuckDB失败: {str(e)}")

    def _infer_type(series):
        if pd.api.types.is_integer_dtype(series):
            return "BIGINT"
        if pd.api.types.is_float_dtype(series):
            return "DOUBLE"
        if pd.api.types.is_bool_dtype(series):
            return "BOOLEAN"
        return "VARCHAR"

    results = {}
    for stock_code in stock_codes:
        df = data_map.get(stock_code)
        if df is None or df.empty:
            results[stock_code] = {"updated": 0, "inserted": 0, "rows": 0, "fields": []}
            continue

        df = df.copy()
        if time_col not in df.columns:
            raise ValueError(f"缺少时间列: {time_col}")

        if fields_list is None:
            use_fields = [c for c in df.columns if c not in [time_col, "stock_code"]]
        else:
            use_fields = [c for c in fields_list if c != time_col]

        use_fields = [f for f in use_fields if f in df.columns]
        if not use_fields:
            raise ValueError("未找到可写入字段")

        df = df[[time_col] + use_fields].copy()
        if time_col != "time":
            df = df.rename(columns={time_col: "time"})

        from duckdb_storage.time_utils import coerce_market_time
        df["time"] = coerce_market_time(df["time"])

        df = df.dropna(subset=["time"])
        if df.empty:
            results[stock_code] = {"updated": 0, "inserted": 0, "rows": 0, "fields": use_fields}
            continue

        df = df.drop_duplicates(subset=["time"], keep="last")

        stock_db = manager.get_stock_db(stock_code)
        table_name = stock_db.PERIOD_TABLE_MAP.get(period)
        if not table_name:
            raise ValueError(f"不支持的周期类型: {period}")

        stock_db._ensure_period_table(period)
        conn = stock_db.conn

        try:
            existing_columns = {row[0] for row in conn.execute(f"DESCRIBE {table_name}").fetchall()}
        except Exception:
            existing_columns = set()

        for field in use_fields:
            if field not in existing_columns:
                if field_types and field in field_types:
                    field_type = field_types[field]
                else:
                    field_type = _infer_type(df[field])
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {field} {field_type}")
                existing_columns.add(field)

        if update_time and "update_time" not in existing_columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
            existing_columns.add("update_time")

        conn.register("temp_df", df)

        updated_rows = 0
        inserted_rows = 0

        try:
            updated_rows = conn.execute(
                f"SELECT COUNT(*) FROM {table_name} t JOIN temp_df s ON t.time = s.time"
            ).fetchone()[0]

            set_parts = [f"{field} = s.{field}" for field in use_fields]
            if update_time and "update_time" in existing_columns:
                set_parts.append("update_time = CURRENT_TIMESTAMP")
            if set_parts:
                set_clause = ", ".join(set_parts)
                conn.execute(
                    f"UPDATE {table_name} AS t SET {set_clause} FROM temp_df AS s WHERE t.time = s.time"
                )

            if insert_missing:
                inserted_rows = conn.execute(
                    f"SELECT COUNT(*) FROM temp_df s LEFT JOIN {table_name} t ON t.time = s.time WHERE t.time IS NULL"
                ).fetchone()[0]
                insert_columns = ["time"] + use_fields
                select_columns = [f"s.{c}" for c in insert_columns]
                if update_time and "update_time" in existing_columns:
                    insert_columns.append("update_time")
                    select_columns.append("CURRENT_TIMESTAMP")
                conn.execute(
                    f"INSERT INTO {table_name} ({', '.join(insert_columns)}) "
                    f"SELECT {', '.join(select_columns)} FROM temp_df s "
                    f"LEFT JOIN {table_name} t ON t.time = s.time WHERE t.time IS NULL"
                )
        finally:
            if hasattr(conn, "unregister"):
                try:
                    conn.unregister("temp_df")
                except Exception:
                    pass

        results[stock_code] = {
            "updated": int(updated_rows),
            "inserted": int(inserted_rows),
            "rows": int(len(df)),
            "fields": use_fields
        }

    return results

# ── khDuckDB 增量查询缓存 ─────────────────────────────────────────────────────
# 对 start_time=None 的累积查询（回测中最常见的写法）启用增量模式：
#   - 首次调用：全量查询，结果缓存到内存
#   - 后续调用（end_time 递增）：只查"上次最大时间 → 本次 end_time"的新增行，
#     追加到缓存 DataFrame，再做内存切片返回
#   - end_time 不增反降（重放/测试）：直接切片，无 DB 调用
# 对 start_time 不为 None 的窗口查询：bypass 缓存，直接走数据库。
# tick 数据数据量大，也 bypass 以防内存暴涨。
#
# 对使用方完全透明，无需改策略代码。

_KHDB_CACHE: dict = {}            # key → {"df": DataFrame, "time_np": ndarray}
_KHDB_CACHE_LOCK = threading.Lock()
_KHDB_CACHE_MAX_ROWS = 500_000   # 单条目行数上限，超出则不缓存（保护内存）
_KHDB_TS_CACHE: dict = {}        # end_time_str → pd.Timestamp，避免重复转换

# ── khHistory 窗口缓存 ────────────────────────────────────────────────────────
# 回测模式（current_time 固定）：按 bar_count 推导最小可用窗口读取，后续调用纯内存切片。
# tick 数据数据量大，bypass 缓存。force_download=True 时也 bypass。
#
_KHIST_CACHE: dict = {}           # key → {"df": DataFrame, "time_np": ndarray, "start_ts": Timestamp, "end_ts": Timestamp}
_KHIST_FRAMEWORK_CACHE: dict = {}
_KHIST_FRAMEWORK_FIRST_TIME_CACHE: dict = {}
_KHIST_EMPTY_FRAME_CACHE: dict = {}
_KHIST_CACHE_LOCK = threading.Lock()
_KHIST_CACHE_MAX_ROWS = 300_000   # 单条目行数上限（保护内存）
_KHIST_MISSING_WARNED: set = set()
_KHIST_MISSING_WARNED_MAX = 5
_KHIST_MISSING_SUMMARY_EVERY = 500
_KHIST_MISSING_TOTAL = 0
_KHIST_MISSING_PROMPT_CONTINUE_ATTR = "_kh_history_missing_user_continue"
_KHIST_MISSING_NO_PROMPT_WARNED = False
_KHIST_STATS = {
    "hits": 0,
    "misses": 0,
    "extensions": 0,
    "rows_cached": 0,
    "bytes_cached": 0,
    "full_prefetch_reads": 0,
    "current_day_prefetch_reads": 0,
    "fallback_reads": 0,
    "memory_fastpath_hits": 0,
    "memory_fastpath_misses": 0,
    "memory_fastpath_prelisting_hits": 0,
    "memory_fastpath_partial_history_hits": 0,
}

# 自适应分段预加载(#17修复): 记录策略 khHistory 实际需要的最大回看天数(日历日)。
# 动态加载器据此在每个分段开头预加载足够历史, 让 khHistory 内存快路命中、
# 避免分段边界逐股回退 DuckDB(idx_short)。只影响"从哪天开始加载", 不改变回测结果。
_KHIST_OBSERVED_LOOKBACK_DAYS = 0
# 交易日口径(#17): 策略观测到的最大"回看交易日数"。按交易日预读比按日历日更稳——
# 跨春节/国庆等长假时, 日历日窗口可能落空(无交易日), 交易日窗口则总能拿到足够历史。
_KHIST_OBSERVED_TRADING_DAYS = 0


def khist_observed_lookback_days() -> int:
    """供动态加载器读取: 策略 khHistory 观测到的最大回看天数(日历日, 含周末/缓冲)。"""
    return int(_KHIST_OBSERVED_LOOKBACK_DAYS)


def khist_observed_trading_days() -> int:
    """供动态加载器读取: 策略 khHistory 观测到的最大回看交易日数。"""
    return int(_KHIST_OBSERVED_TRADING_DAYS)


def trade_day_n_before(end_date_str, n):
    """返回 end_date(YYYYMMDD)往前数 n 个交易日的日期(YYYYMMDD, 不含当天)。
    用 CSV 离线交易日历(get_trade_days_set, 覆盖2000-2026); 失败或交易日不足时回退日历日近似。
    用于分段预加载: 跨长假也能拿到足够交易日的 lookback, 消除分段开头逐股回退。
    注: 不走 _get_trade_days_list——它首选 baostock 联网且无超时, 网络抽风会让每个回测在
    启动(#17首段预读)时无限挂起; get_trade_days_set 命中本地CSV缓存, 离线即返回。"""
    import datetime as _dt
    try:
        end_dt = _dt.datetime.strptime(str(end_date_str), "%Y%m%d")
    except Exception:
        return str(end_date_str)
    if not n or n <= 0:
        return end_dt.strftime("%Y%m%d")
    try:
        window = max(int(n * 1.8) + 35, 45)  # 足够宽, 覆盖 n 个交易日 + 最长假期
        end_str = end_dt.strftime("%Y%m%d")
        tdset = get_trade_days_set((end_dt - _dt.timedelta(days=window)).strftime("%Y%m%d"), end_str)
        prior = sorted(d for d in tdset if d < end_str)  # tdset 为 "YYYYMMDD" 字符串集合
        if len(prior) >= n:
            return prior[-n]
    except Exception:
        pass
    return (end_dt - _dt.timedelta(days=int(n * 1.6) + 5)).strftime("%Y%m%d")  # 兜底


def clear_khDuckDB_cache():
    """清除 khDuckDB 查询缓存。

    在每次回测 init() 中调用可避免跨回测数据污染：
        from khQuantImport import clear_khDuckDB_cache
        def init(stock_list, data):
            clear_khDuckDB_cache()
    """
    with _KHDB_CACHE_LOCK:
        _KHDB_CACHE.clear()
    _KHDB_TS_CACHE.clear()
    logging.info("khDuckDB 缓存已清除")


def clear_khHistory_cache():
    """清除 khHistory 查询缓存。

    在每次回测 init() 中调用可避免跨回测数据污染：
        from khQuantImport import clear_khHistory_cache
        def init(stock_list, data):
            clear_khHistory_cache()
    """
    with _KHIST_CACHE_LOCK:
        _KHIST_CACHE.clear()
        _KHIST_FRAMEWORK_CACHE.clear()
        _KHIST_FRAMEWORK_FIRST_TIME_CACHE.clear()
        _KHIST_EMPTY_FRAME_CACHE.clear()
        _KHIST_MISSING_WARNED.clear()
        global _KHIST_MISSING_TOTAL, _KHIST_MISSING_NO_PROMPT_WARNED
        global _KHIST_OBSERVED_LOOKBACK_DAYS, _KHIST_OBSERVED_TRADING_DAYS
        _KHIST_MISSING_TOTAL = 0
        _KHIST_MISSING_NO_PROMPT_WARNED = False
        # 观测值只属于当前回测。若跨回测保留，会让后续动态加载器
        # 使用上一策略的预读窗口，造成结果/性能随测试顺序变化。
        _KHIST_OBSERVED_LOOKBACK_DAYS = 0
        _KHIST_OBSERVED_TRADING_DAYS = 0
        for key in _KHIST_STATS:
            _KHIST_STATS[key] = 0
    logging.info("khHistory 缓存已清除")


def get_khHistory_cache_stats():
    """返回 khHistory 窗口缓存统计快照。"""
    with _KHIST_CACHE_LOCK:
        stats = dict(_KHIST_STATS)
        stats["entries"] = len(_KHIST_CACHE)
    return stats


def _khist_calc_lookback_days(period: str, bar_count: int, cache_mode: bool = True) -> int:
    """按周期和 bar_count 推导历史预热天数，避免分钟线默认拉多年全量数据。"""
    if period == 'tick':
        return 3
    if period in ['1m', '5m']:
        # A 股每交易日约 240 根 1m K；用自然日冗余覆盖周末/停牌/缺失。
        period_minutes = 1 if period == '1m' else 5
        bars_per_day = max(1, 240 // period_minutes)
        trading_days = max(2, (bar_count + bars_per_day - 1) // bars_per_day + 3)
        multiplier = 3 if cache_mode else 2
        return max(10, trading_days * multiplier)
    if period == '1d':
        return max(30, bar_count * (5 if cache_mode else 3))
    return max(10, bar_count * (3 if cache_mode else 2))


def _khist_cache_cut(period: str, current_datetime) -> "pd.Timestamp":
    return pd.Timestamp(current_datetime.date()) if period == '1d' else pd.Timestamp(current_datetime)


def _khist_use_shallow_return(perf_cfg) -> bool:
    value = str((perf_cfg or {}).get("khhistory_return_copy", True)).strip().lower()
    return value in ("0", "false", "off", "none", "shallow", "view", "no")


def _khist_finalize_slice(frame, perf_cfg=None):
    if _khist_use_shallow_return(perf_cfg):
        result = frame.copy(deep=False)
        result.index = pd.RangeIndex(len(result))
        return result
    # ``reset_index(drop=True)`` already allocates a new manager.  Calling
    # ``copy()`` on that result copied every history column a second time.
    # khHistory is commonly called for thousands of symbols on every bar, so
    # the redundant copy becomes a material cost.  One explicit deep copy plus
    # replacing the index preserves the public isolation guarantee while doing
    # only one data copy.
    result = frame.copy(deep=True)
    result.index = pd.RangeIndex(len(result))
    return result


def _khist_empty_result(requested_columns, perf_cfg=None):
    """Reuse immutable-by-contract empties when shallow returns are enabled."""
    if not _khist_use_shallow_return(perf_cfg):
        return pd.DataFrame(columns=requested_columns)
    key = tuple(requested_columns)
    with _KHIST_CACHE_LOCK:
        empty = _KHIST_EMPTY_FRAME_CACHE.get(key)
        if empty is None:
            empty = pd.DataFrame(columns=requested_columns)
            _KHIST_EMPTY_FRAME_CACHE[key] = empty
        return empty


def _khist_cache_slice(entry: dict, period: str, current_datetime, bar_count: int, perf_cfg=None):
    cut = _khist_cache_cut(period, current_datetime)
    idx = int(np.searchsorted(entry["time_np"], cut.to_datetime64(), side='left'))
    start = max(0, idx - int(bar_count))
    return _khist_finalize_slice(entry["df"].iloc[start:idx], perf_cfg)


def _khist_cache_covers(entry: dict, period: str, current_datetime, required_start) -> bool:
    cut = _khist_cache_cut(period, current_datetime)
    start_ts = entry.get("start_ts")
    end_ts = entry.get("end_ts")
    if start_ts is None or end_ts is None:
        return True
    return pd.Timestamp(start_ts) <= pd.Timestamp(required_start) and pd.Timestamp(end_ts) >= cut


def _khist_record_cache_write(cache_key, stock_data, time_np, requested_start_ts=None, requested_end_ts=None):
    rows = len(stock_data)
    if rows > _KHIST_CACHE_MAX_ROWS:
        return
    bytes_used = int(stock_data.memory_usage(deep=True).sum())
    with _KHIST_CACHE_LOCK:
        old = _KHIST_CACHE.get(cache_key)
        if old is not None:
            _KHIST_STATS["rows_cached"] -= int(len(old.get("df", [])))
            _KHIST_STATS["bytes_cached"] -= int(old.get("bytes", 0))
        _KHIST_CACHE[cache_key] = {
            "df": stock_data,
            "time_np": time_np,
            "start_ts": pd.Timestamp(requested_start_ts if requested_start_ts is not None else stock_data['time'].iloc[0]),
            "end_ts": pd.Timestamp(requested_end_ts if requested_end_ts is not None else stock_data['time'].iloc[-1]),
            "bytes": bytes_used,
        }
        _KHIST_STATS["rows_cached"] += rows
        _KHIST_STATS["bytes_cached"] += bytes_used


def _khist_record_empty_cache(cache_key, requested_columns, requested_start_ts, requested_end_ts):
    """Cache an empty khHistory window so missing data does not repeatedly hit DuckDB."""
    empty_df = pd.DataFrame(columns=requested_columns)
    time_np = np.array([], dtype="datetime64[ns]")
    _khist_record_cache_write(
        cache_key,
        empty_df,
        time_np,
        requested_start_ts=requested_start_ts,
        requested_end_ts=requested_end_ts,
    )


def _khist_perf_config(framework):
    try:
        from performance_config import get_performance_config
        cfg = getattr(framework, "config", None)
        return get_performance_config(cfg)
    except Exception:
        try:
            from performance_config import normalize_performance_config
            return normalize_performance_config()
        except Exception:
            return {}


def _khist_backtest_end_time(framework):
    try:
        cfg = getattr(framework, "config", None)
        if cfg is None:
            return None
        value = getattr(cfg, "backtest_end", None)
        if value is None:
            value = (getattr(cfg, "config_dict", {}) or {}).get("backtest", {}).get("end_time")
        if value is None:
            return None
        text = str(value).strip()
        return text or None
    except Exception:
        return None


def _khist_framework_dynamic_loading(framework) -> bool:
    try:
        return bool(
            getattr(framework, "dynamic_loader_enabled", False)
            and getattr(framework, "dynamic_loader", None) is not None
        )
    except Exception:
        return False


def _khist_prefetch_to_backtest_end_allowed(dividend_type: str) -> bool:
    """Return True only when prefetching past current_time cannot re-anchor prices.

    Some data providers compute front/back adjusted prices relative to the
    requested data range.  Querying adjusted history through the backtest end
    can therefore change earlier rows even though the returned slice excludes
    future bars.  Keep adjusted khHistory calls on current-day semantics.
    """
    return _khist_normalize_dividend_type(dividend_type) in ("", "none")


def _khist_end_ts(end_time_value, fallback_current_datetime):
    try:
        ts = _khdb_end_time_to_ts(end_time_value)
        if ts is not None:
            return ts
    except Exception:
        pass
    return (
        pd.Timestamp(fallback_current_datetime.date())
        + pd.Timedelta(days=1)
        - pd.Timedelta(nanoseconds=1)
    )


# ``khKline`` 读 DuckDB 时经过下面这个函数：它按被调函数的签名过滤关键字
# 参数，旧的兼容签名也能调用。金标准 E（15m / 2d 聚合）依赖这条路径，保留原样。


def _legacy_call_with_supported_kwargs(function, kwargs: dict):
    """Call a manager method while tolerating older compatible signatures."""
    # Keep the original mapping intact.  The previous implementation filtered
    # ``kwargs`` in-place and then used the filtered mapping to decide whether
    # to retry positionally; this made ``def f(code, ...)`` signatures fail
    # under inspect, while retrying *any* TypeError could invoke a download
    # twice when the function body itself raised TypeError.
    original_kwargs = dict(kwargs)
    positional = []
    call_kwargs = dict(original_kwargs)
    try:
        import inspect
        signature = inspect.signature(function)
    except Exception:
        signature = None

    if signature is not None:
        parameters = signature.parameters
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        # A few old embedders expose ``code`` as their first argument (often
        # positional-only), whereas the modern manager uses ``stock_code``.
        # Resolve that shape from the signature before making the call so a
        # TypeError raised by the function body is never retried.
        if "stock_code" in call_kwargs and "stock_code" not in parameters:
            code_parameter = parameters.get("code")
            if code_parameter is None:
                code_parameter = next(
                    (
                        parameter
                        for parameter in parameters.values()
                        if parameter.kind
                        in (
                            inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        )
                    ),
                    None,
                )
            if code_parameter is not None:
                positional.append(call_kwargs.pop("stock_code"))
                # A positional value must not also be passed by keyword.  For
                # positional-only parameters filtering would drop it anyway,
                # but popping explicitly keeps the intent clear.
                call_kwargs.pop(code_parameter.name, None)
        if not accepts_kwargs:
            call_kwargs = {
                name: value
                for name, value in call_kwargs.items()
                if name in parameters
                and parameters[name].kind
                not in (inspect.Parameter.POSITIONAL_ONLY,)
            }
        return function(*positional, **call_kwargs)

    # Signature inspection can fail for C-extension callables.  In that case
    # make one normal attempt; only retry positionally when Python's own
    # argument binder clearly rejected the stock_code keyword.  A TypeError
    # from inside the callable is propagated without a second invocation to
    # avoid duplicate downloads.
    try:
        return function(**original_kwargs)
    except TypeError as first:
        message = str(first).lower()
        binder_error = (
            "unexpected keyword" in message
            or "got an unexpected keyword" in message
            or "takes no keyword arguments" in message
            or "positional-only" in message
        )
        if not binder_error or "stock_code" not in original_kwargs:
            raise
        fallback = dict(original_kwargs)
        code = fallback.pop("stock_code")
        try:
            return function(code, **fallback)
        except TypeError:
            raise first


def _resolve_data_source_manager_for_khqt():
    """取得数据源管理器：优先用已初始化的全局实例，否则按开源版配置初始化。

    开源版只有 DuckDB 一种数据源；初始化失败时返回 None，由调用方给出提示。
    """
    try:
        from khDataSource import get_data_source_manager, init_from_settings
    except Exception:
        return None

    try:
        manager = get_data_source_manager()
    except Exception:
        return None

    # A live/global manager is an explicit runtime choice and must take
    # precedence over stale settings.  This also keeps injected fakes used by
    # existing strategy/tests fully compatible.
    if manager is not None:
        return manager

    try:
        return init_from_settings()
    except Exception:
        return None


def _khist_normalize_dividend_type(value):
    text = str(value or "none").strip().lower()
    return {"pre": "front", "qfq": "front", "post": "back", "hfq": "back"}.get(text, text)


def _khist_framework_is_compatible(framework, period, dividend_type, perf_cfg):
    if framework is None:
        return False
    if str(perf_cfg.get("khhistory_memory_fastpath", True)).lower() in ("0", "false", "off", "none", "disabled"):
        return False
    try:
        cfg = getattr(framework, "config", None)
        config_dict = getattr(cfg, "config_dict", {}) if cfg is not None else {}
        data_cfg = config_dict.get("data", {}) or {}
        framework_period = data_cfg.get("kline_period") or getattr(cfg, "kline_period", None)
        if str(framework_period or "").lower() != str(period).lower():
            return False
        framework_dividend = _khist_normalize_dividend_type(data_cfg.get("dividend_type", "none"))
        requested_dividend = _khist_normalize_dividend_type(dividend_type)
        if framework_dividend != requested_dividend:
            raw_sidecar = getattr(framework, "_khhistory_raw_data_ref", None)
            raw_from_adjusted = (
                framework_dividend in ("front", "back", "front_ratio", "back_ratio")
                and requested_dividend in ("", "none")
                and bool(raw_sidecar)
            )
            # Safe when raw framework data carries explicit adjusted columns
            # (e.g. close_front), or when an adjusted full-load retained the
            # original OHLC in the private sidecar. Per-stock field checks are
            # still performed while building each cache entry.
            adjusted_from_raw = (
                framework_dividend == "none"
                and requested_dividend in ("front", "back", "front_ratio", "back_ratio")
            )
            if not (adjusted_from_raw or raw_from_adjusted):
                return False
        return hasattr(framework, "historical_data_ref") and hasattr(framework, "time_field_cache")
    except Exception:
        return False


def _khist_convert_time_column(time_values):
    from duckdb_storage.time_utils import coerce_market_time

    return coerce_market_time(time_values)


def _khist_framework_entry(framework, stock_code, period, fields, fields_key, dividend_type, skip_paused):
    try:
        data_ref = getattr(framework, "historical_data_ref", {}) or {}
        source_df = data_ref.get(stock_code)
        if source_df is None or source_df.empty:
            return None
        raw_data_ref = getattr(framework, "_khhistory_raw_data_ref", {}) or {}
        raw_source_df = raw_data_ref.get(stock_code)
        time_field = (getattr(framework, "time_field_cache", {}) or {}).get(stock_code)
        cache_key = (
            id(framework), id(source_df), id(raw_source_df), stock_code, period, fields_key,
            _khist_normalize_dividend_type(dividend_type), bool(skip_paused),
        )
        cached = _KHIST_FRAMEWORK_CACHE.get(cache_key)
        if cached is not None:
            return cached

        price_fields = {"open", "high", "low", "close"}
        try:
            cfg = getattr(framework, "config", None)
            config_dict = getattr(cfg, "config_dict", {}) if cfg is not None else {}
            data_cfg = config_dict.get("data", {}) or {}
            framework_dividend = _khist_normalize_dividend_type(data_cfg.get("dividend_type", "none"))
        except Exception:
            framework_dividend = "none"
        requested_dividend = _khist_normalize_dividend_type(dividend_type)
        raw_from_adjusted = (
            framework_dividend in ("front", "back", "front_ratio", "back_ratio")
            and requested_dividend in ("", "none")
            and isinstance(raw_source_df, pd.DataFrame)
            and len(raw_source_df) == len(source_df)
        )
        adjusted_suffix = (
            requested_dividend
            if framework_dividend == "none"
            and requested_dividend in ("front", "back", "front_ratio", "back_ratio")
            else None
        )
        if time_field == "__index__":
            time_values = source_df.index
        elif time_field and time_field in source_df.columns:
            time_values = source_df[time_field]
        elif "time" in source_df.columns:
            time_values = source_df["time"]
        else:
            return None

        converted_time = _khist_convert_time_column(time_values)
        output_columns = {"time": converted_time.values}
        missing_fields = []
        for field in fields:
            if field == "time":
                continue
            source_field = field
            source_frame = source_df
            if raw_from_adjusted and field in price_fields:
                if field in raw_source_df.columns:
                    source_frame = raw_source_df
                else:
                    missing_fields.append(field)
                    continue
            elif adjusted_suffix and field in price_fields:
                adjusted_field = f"{field}_{adjusted_suffix}"
                if adjusted_field in source_df.columns:
                    source_field = adjusted_field
                else:
                    missing_fields.append(field)
                    continue
            elif field not in source_df.columns:
                missing_fields.append(field)
                continue
            output_columns[field] = source_frame[source_field].to_numpy(copy=False)
        if missing_fields:
            return None

        work = pd.DataFrame(output_columns)
        work = work.dropna(subset=["time"])
        if work.empty:
            return None
        if "time" in work.columns and not work["time"].is_monotonic_increasing:
            work = work.sort_values("time")
        if skip_paused and "volume" in work.columns:
            work = work[work["volume"] > 0]
        work = work.reset_index(drop=True)
        entry = {
            "df": work,
            "time_np": work["time"].values.astype("datetime64[ns]"),
            "bytes": int(work.memory_usage(deep=True).sum()),
        }
        with _KHIST_CACHE_LOCK:
            _KHIST_FRAMEWORK_CACHE[cache_key] = entry
        return entry
    except Exception:
        return None


def _khist_framework_first_time_np(framework, stock_code):
    """Read one framework timestamp without materializing a history DataFrame."""
    try:
        data_ref = getattr(framework, "historical_data_ref", {}) or {}
        source_df = data_ref.get(stock_code)
        if source_df is None or source_df.empty:
            return None
        cache_key = (id(framework), id(source_df), stock_code)
        with _KHIST_CACHE_LOCK:
            if cache_key in _KHIST_FRAMEWORK_FIRST_TIME_CACHE:
                return _KHIST_FRAMEWORK_FIRST_TIME_CACHE[cache_key]

        time_field = (getattr(framework, "time_field_cache", {}) or {}).get(stock_code)
        if time_field == "__index__":
            first_value = source_df.index[0]
        elif time_field and time_field in source_df.columns:
            first_value = source_df[time_field].iloc[0]
        elif "time" in source_df.columns:
            first_value = source_df["time"].iloc[0]
        else:
            return None

        converted = _khist_convert_time_column([first_value])
        first_time = None
        if len(converted) and not pd.isna(converted.iloc[0]):
            first_time = pd.Timestamp(converted.iloc[0]).to_datetime64()
        with _KHIST_CACHE_LOCK:
            _KHIST_FRAMEWORK_FIRST_TIME_CACHE[cache_key] = first_time
        return first_time
    except Exception:
        return None


def _khist_try_framework_memory(
    framework,
    stock_codes,
    period,
    fields,
    fields_key,
    dividend_type,
    skip_paused,
    current_datetime,
    bar_count,
    requested_columns,
    perf_cfg,
    required_start=None,
):
    if not _khist_framework_is_compatible(framework, period, dividend_type, perf_cfg):
        return {}, list(stock_codes)

    result = {}
    misses = []
    prelisting_hits = 0
    partial_history_hits = 0
    cut = _khist_cache_cut(period, current_datetime)
    cut_np = cut.to_datetime64()
    full_load_start_np = None
    required_start_np = None
    if not _khist_framework_dynamic_loading(framework):
        try:
            full_load_start = getattr(framework, "_khhistory_full_load_start_time", None)
            if full_load_start:
                # The framework stores this as a Timestamp, while older tests
                # and integrations may still provide YYYYMMDD strings.
                full_load_start_np = pd.Timestamp(full_load_start).normalize().to_datetime64()
            if required_start is not None:
                required_start_np = pd.Timestamp(required_start).to_datetime64()
        except Exception:
            full_load_start_np = None
            required_start_np = None
    for stock_code in stock_codes:
        # Most symbols in a present-day all-A pool did not exist near an old
        # backtest start. Inspecting one timestamp first avoids constructing a
        # full pandas history object merely to discover that the current date
        # precedes the symbol's first bar.
        first_time_np = _khist_framework_first_time_np(framework, stock_code)
        if first_time_np is not None and cut_np < first_time_np:
            result[stock_code] = _khist_empty_result(requested_columns, perf_cfg)
            prelisting_hits += 1
            continue
        entry = _khist_framework_entry(
            framework, stock_code, period, fields, fields_key, dividend_type, skip_paused
        )
        if entry is None:
            misses.append(stock_code)
            continue
        # Keep a defensive check for data layouts whose first timestamp could
        # not be inspected by the lightweight path above.
        if len(entry["time_np"]) and cut_np < entry["time_np"][0]:
            result[stock_code] = _khist_empty_result(requested_columns, perf_cfg)
            prelisting_hits += 1
            continue
        idx = int(np.searchsorted(entry["time_np"], cut_np, side="left"))
        if idx < bar_count:
            # A static framework load covers every row from full_load_start to
            # the backtest end. If this symbol's first row is later than that
            # boundary, querying DuckDB again cannot add any bars inside the
            # already-loaded warm-up window. Return the known short history
            # directly. This mainly covers newly listed stocks during their
            # first ``bar_count`` sessions and avoids repeating the same empty
            # extension query on every following trading day.
            if (
                full_load_start_np is not None
                and (
                    (
                        required_start_np is not None
                        and full_load_start_np <= required_start_np
                    )
                    or (
                        len(entry["time_np"])
                        and entry["time_np"][0] > full_load_start_np
                    )
                )
            ):
                start = max(0, idx - int(bar_count))
                result[stock_code] = _khist_finalize_slice(
                    entry["df"].iloc[start:idx], perf_cfg
                )
                partial_history_hits += 1
                _warn_kh_history_missing_once(
                    stock_code,
                    period,
                    fields,
                    dividend_type,
                    f"当前时间前历史数据不足，需要 {bar_count} 行",
                    current_time=current_datetime,
                    available_rows=idx,
                )
                continue
            misses.append(stock_code)
            continue
        result[stock_code] = _khist_finalize_slice(entry["df"].iloc[idx - bar_count:idx], perf_cfg)

    with _KHIST_CACHE_LOCK:
        _KHIST_STATS["memory_fastpath_hits"] += len(result)
        _KHIST_STATS["memory_fastpath_misses"] += len(misses)
        _KHIST_STATS["memory_fastpath_prelisting_hits"] += prelisting_hits
        _KHIST_STATS["memory_fastpath_partial_history_hits"] += partial_history_hits
    return result, misses


def _warn_kh_history_missing_once(
    stock_code: str,
    period: str,
    fields,
    dividend_type: str,
    reason: str,
    current_time=None,
    available_rows: int = 0,
):
    """对 khHistory 缺数据场景做一次性 WARNING，避免大股票池重复刷屏。"""
    global _KHIST_MISSING_TOTAL
    fields_key = tuple(fields or [])
    key = (stock_code, period, fields_key, dividend_type, reason)
    should_log_detail = False
    should_log_summary = False
    with _KHIST_CACHE_LOCK:
        if key in _KHIST_MISSING_WARNED:
            return
        _KHIST_MISSING_WARNED.add(key)
        _KHIST_MISSING_TOTAL += 1
        if len(_KHIST_MISSING_WARNED) <= _KHIST_MISSING_WARNED_MAX:
            should_log_detail = True
        elif _KHIST_MISSING_TOTAL % _KHIST_MISSING_SUMMARY_EVERY == 0:
            should_log_summary = True

    if should_log_summary:
        logging.warning(
            "khHistory 数据缺失汇总: 已发现 %s 个唯一缺失项；后续仍返回空历史数据并继续回测。"
            "如需详细排查，请缩小股票池或补齐数据后重跑。",
            _KHIST_MISSING_TOTAL,
        )
        return

    if not should_log_detail:
        return

    logging.warning(
        "khHistory 数据缺失: 股票=%s 周期=%s 字段=%s 复权=%s 当前时间=%s 原因=%s 可用历史行数=%s；"
        "已返回当前可用历史数据（可能为空），策略将按数据不足处理。请在数据管理模块补充该股票/周期/复权口径的数据，"
        "或调整回测区间/股票池。",
        stock_code,
        period,
        list(fields_key),
        dividend_type,
        current_time,
        reason,
        available_rows,
    )

    _confirm_kh_history_missing_data(
        stock_code=stock_code,
        period=period,
        fields=list(fields_key),
        dividend_type=dividend_type,
        reason=reason,
        current_time=current_time,
        available_rows=available_rows,
    )


def _get_current_framework_for_history_warning():
    """获取当前策略运行中的框架实例。"""
    try:
        khimport = sys.modules.get("khQuantImport")
        return getattr(khimport, "_CURRENT_FRAMEWORK", None)
    except Exception:
        return None


def _confirm_kh_history_missing_data(
    stock_code: str,
    period: str,
    fields,
    dividend_type: str,
    reason: str,
    current_time=None,
    available_rows: int = 0,
):
    """GUI 模式下首次缺历史数据时弹窗确认是否继续。"""
    framework = _get_current_framework_for_history_warning()
    if framework is None:
        return

    if getattr(framework, _KHIST_MISSING_PROMPT_CONTINUE_ATTR, False):
        return

    ui_settings = getattr(framework, "ui_settings", {}) or {}
    if not bool(ui_settings.get("khhistory_missing_data_prompt", False)):
        global _KHIST_MISSING_NO_PROMPT_WARNED
        with _KHIST_CACHE_LOCK:
            if _KHIST_MISSING_NO_PROMPT_WARNED:
                return
            _KHIST_MISSING_NO_PROMPT_WARNED = True
        logging.warning(
            "khHistory 缺历史数据默认不弹确认框；已继续返回空历史数据。"
            "如需恢复旧交互，请在系统性能设置中开启 khHistory 缺历史数据弹窗确认。"
        )
        return
    confirm_cb = ui_settings.get("confirm_callback")
    if not callable(confirm_cb):
        return

    message = (
        "检测到策略调用 khHistory 时缺少历史数据，这会影响回测结果。\n\n"
        f"股票: {stock_code}\n"
        f"周期: {period}\n"
        f"字段: {fields}\n"
        f"复权口径: {dividend_type}\n"
        f"当前时间: {current_time}\n"
        f"原因: {reason}\n"
        f"可用历史行数: {available_rows}\n\n"
        "系统已返回空历史数据，策略可能会跳过该股票或产生偏差。\n"
        "建议停止回测并补充数据、调整回测区间或调整股票池。\n\n"
        "是否继续运行回测？"
    )

    try:
        should_continue = bool(confirm_cb("历史数据缺失，回测结果可能受影响", message))
    except Exception as exc:
        logging.warning(f"显示 khHistory 缺数据确认弹窗失败: {exc}")
        return

    if should_continue:
        setattr(framework, _KHIST_MISSING_PROMPT_CONTINUE_ATTR, True)
        logging.warning("用户选择继续运行含缺失历史数据的回测")
        return

    try:
        framework.stop()
    except Exception:
        pass
    raise RuntimeError(
        f"用户因 khHistory 缺少历史数据停止回测: 股票={stock_code}, 周期={period}, "
        f"字段={fields}, 当前时间={current_time}, 原因={reason}"
    )


def _khdb_end_time_to_ts(end_time_str) -> "pd.Timestamp | None":
    """将 end_time 字符串/整数统一转为 pd.Timestamp，结果缓存以避免重复转换。"""
    if end_time_str is None:
        return None
    cached = _KHDB_TS_CACHE.get(end_time_str)
    if cached is not None:
        return cached
    s = str(end_time_str).strip()
    try:
        if len(s) == 8 and s.isdigit():
            ts = pd.Timestamp(f"{s[:4]}-{s[4:6]}-{s[6:8]} 23:59:59")
        elif len(s) == 10 and '-' in s:
            ts = pd.Timestamp(f"{s} 23:59:59")
        else:
            ts = pd.Timestamp(s)
    except Exception:
        return None
    if len(_KHDB_TS_CACHE) < 2000:
        _KHDB_TS_CACHE[end_time_str] = ts
    return ts


# ─────────────────────────────────────────────────────────────────────────────

def khDuckDB(
    stock_list,
    period,
    fields=None,
    start_time=None,
    end_time=None,
    dividend_type='none',
    duckdb_path=None,
    return_format='dict'
):
    """
    从 DuckDB 获取历史数据

    Args:
        stock_list: 股票代码列表或单个股票代码字符串
        period: 周期类型，如 '1d', '1m', '5m', 'tick'
        fields: 字段列表，None 表示返回所有字段
        start_time: 开始时间，格式 'YYYYMMDD' 或 'YYYYMMDDHHmmss'
        end_time: 结束时间，格式同上
        dividend_type: 复权类型，'none', 'front', 'back', 'front_ratio', 'back_ratio'
        duckdb_path: DuckDB 数据根目录
        return_format: 返回格式，当前仅支持 'dict'

    Returns:
        dict: {股票代码: DataFrame}
    """
    if not stock_list:
        raise ValueError("stock_list不能为空")
    if not period:
        raise ValueError("period不能为空")
    if return_format != 'dict':
        raise ValueError("return_format仅支持'dict'")

    if isinstance(stock_list, str):
        stock_codes = [stock_list]
    else:
        stock_codes = list(stock_list)

    stock_codes = [normalize_stock_code(code) for code in stock_codes]

    if fields is None:
        fields_list = None
    elif isinstance(fields, str):
        fields_list = [fields]
    else:
        fields_list = list(fields)

    dividend_type = (dividend_type or 'none').lower()
    if dividend_type not in ['none', 'front', 'back', 'front_ratio', 'back_ratio']:
        raise ValueError("dividend_type不支持该值")

    # 缓存开关：start_time=None 且非 tick 时启用
    # 命中时直接做 pandas 切片，零 DB 调用；未命中时一次性加载全量数据写入缓存
    use_cache = (start_time is None and period != 'tick')
    end_ts = _khdb_end_time_to_ts(end_time) if use_cache else None

    # fields 指纹（用于 cache key）
    fields_key = tuple(sorted(fields_list)) if fields_list is not None else None

    # ── 全命中快速返回：所有股票均在缓存中时，完全跳过 DuckDB 初始化 ──────────
    if use_cache and _KHDB_CACHE:
        result = {}
        all_hit = True
        with _KHDB_CACHE_LOCK:
            entries = {sc: _KHDB_CACHE.get((sc, period, duckdb_path or '', fields_key, dividend_type))
                       for sc in stock_codes}
        for sc, entry in entries.items():
            if entry is None:
                all_hit = False
                break
            if end_ts is not None:
                idx = int(np.searchsorted(entry["time_np"], end_ts.to_datetime64(), side='right'))
                result[sc] = entry["df"].iloc[:idx].copy()
            else:
                result[sc] = entry["df"].copy()
        if all_hit:
            return result
    # ──────────────────────────────────────────────────────────────────────────

    try:
        from duckdb_storage.manager import DuckDBManager
        from duckdb_storage import xtdata_adapter
        manager = DuckDBManager(data_root=duckdb_path) if duckdb_path else DuckDBManager()
    except Exception as e:
        print(f"初始化DuckDB失败: {str(e)}")
        return {}

    result = {}
    for stock_code in stock_codes:
        try:
            # ── 缓存命中：纯内存切片，零 DB 调用 ─────────────────────────────
            if use_cache:
                cache_key = (stock_code, period, duckdb_path or '',
                             fields_key, dividend_type)
                with _KHDB_CACHE_LOCK:
                    entry = _KHDB_CACHE.get(cache_key)

                if entry is not None:
                    cached_df = entry["df"]
                    if end_ts is not None:
                        # 二分查找（time列有序）比布尔掩码快约5倍
                        idx = int(np.searchsorted(entry["time_np"], end_ts.to_datetime64(), side='right'))
                        result[stock_code] = cached_df.iloc[:idx].copy()
                    else:
                        result[stock_code] = cached_df.copy()
                    continue

            # ── 缓存未命中（或 bypass）：查询数据库 ────────────────────────────
            query_fields = None
            if fields_list is not None:
                query_fields = fields_list.copy()
                if dividend_type != 'none' and period != 'tick':
                    for af in [f'open_{dividend_type}', f'high_{dividend_type}',
                               f'low_{dividend_type}', f'close_{dividend_type}']:
                        if af not in query_fields:
                            query_fields.append(af)

            # 缓存模式下不传 end_time，一次性加载全量数据；
            # 后续所有 end_time 过滤均在内存中完成，无需再次查 DB。
            load_end_time = None if use_cache else end_time

            df = manager.get_kline_data(
                stock_code=stock_code, period=period,
                start_time=start_time, end_time=load_end_time,
                dividend_type=None, fields=query_fields
            )

            if df is None or df.empty:
                result[stock_code] = pd.DataFrame()
                continue

            if dividend_type != 'none' and period != 'tick':
                df = xtdata_adapter._select_dividend_fields(df, dividend_type)

            if fields_list is not None:
                output_columns = ['time'] + [f for f in fields_list if f != 'time' and f in df.columns]
                df = df[[c for c in output_columns if c in df.columns]]

            # 写入缓存（行数未超限）
            if use_cache and not df.empty and "time" in df.columns:
                cache_key = (stock_code, period, duckdb_path or '', fields_key, dividend_type)
                if len(df) <= _KHDB_CACHE_MAX_ROWS:
                    time_np = df["time"].values.astype('datetime64[ns]')   # 统一 ns 精度
                    with _KHDB_CACHE_LOCK:
                        _KHDB_CACHE[cache_key] = {
                            "df": df,
                            "time_np": time_np,
                        }
                    logging.debug(f"khDuckDB 缓存已写入: {stock_code} {period} {len(df)}行")

            # 首次返回时也按 end_time 切片（复用已计算的 time_np）
            if end_ts is not None:
                time_np = _KHDB_CACHE.get(cache_key, {}).get("time_np") if use_cache else None
                if time_np is None:
                    time_np = df["time"].values.astype('datetime64[ns]')
                idx = int(np.searchsorted(time_np, end_ts.to_datetime64(), side='right'))
                result[stock_code] = df.iloc[:idx].copy()
            else:
                result[stock_code] = df

        except Exception as e:
            print(f"读取 {stock_code} 失败: {str(e)}")
            result[stock_code] = pd.DataFrame()

    return result

def khHistory(symbol_list, fields, bar_count, fre_step, current_time=None, skip_paused=False, fq='pre', force_download=False):
    """
    获取股票历史数据（不包含当前时间点）

    参数:
        symbol_list: 股票代码列表或单个股票代码字符串
        fields: 数据字段列表，如['open', 'high', 'low', 'close', 'volume', 'amount']
        bar_count: 获取的K线数量
        fre_step: 时间频率，如'1d', '1m', '5m'等
        current_time: 当前时间，支持多种格式：
                     - 日线数据：'YYYYMMDD' 或 'YYYY-MM-DD'
                     - 分钟/tick数据：'YYYYMMDD HHMMSS' 或 'YYYY-MM-DD HH:MM:SS'
                     - 如果为None则使用当前日期时间
        skip_paused: 是否跳过停牌数据，True跳过，False不跳过
        fq: 复权方式，'pre'前复权, 'post'后复权, 'none'不复权
        force_download: 是否强制下载最新数据，True强制下载，False使用本地缓存

    返回:
        dict: {股票代码: DataFrame}，DataFrame包含time列和指定的数据字段

    注意: 返回的数据不包含current_time这个时间点，确保回测逻辑正确
    """
    # 导入必要的模块
    try:
        import pandas as pd
        from datetime import datetime, timedelta
    except ImportError as e:
        print(f"导入模块失败: {str(e)}")
        return {}

    data_source_mgr = None
    xtdata = None
    
    # 参数验证
    if not symbol_list:
        raise ValueError("symbol_list不能为空")
    if not fields:
        raise ValueError("fields不能为空")
    if bar_count <= 0:
        raise ValueError("bar_count必须大于0")
    
    # 统一处理股票代码列表
    if isinstance(symbol_list, str):
        stock_codes = [symbol_list]
    else:
        stock_codes = list(symbol_list)

    requested_columns = ['time'] + [field for field in fields if field != 'time']
    
    # 处理当前时间
    current_datetime = None
    current_date_str = None
    
    if current_time is None:
        # 如果没有指定时间，使用当前时间
        current_datetime = datetime.now()
        current_date_str = current_datetime.strftime('%Y%m%d')
    else:
        # 解析输入的时间格式
        if isinstance(current_time, str):
            current_time = current_time.strip()
            
            # 尝试解析不同的时间格式
            time_formats = [
                '%Y%m%d %H%M%S',     # YYYYMMDD HHMMSS
                '%Y-%m-%d %H:%M:%S', # YYYY-MM-DD HH:MM:SS
                '%Y%m%d',            # YYYYMMDD
                '%Y-%m-%d'           # YYYY-MM-DD
            ]
            
            for fmt in time_formats:
                try:
                    current_datetime = datetime.strptime(current_time, fmt)
                    break
                except ValueError:
                    continue
            
            if current_datetime is None:
                raise ValueError(f"无法解析时间格式: {current_time}，支持的格式: YYYYMMDD, YYYY-MM-DD, YYYYMMDD HHMMSS, YYYY-MM-DD HH:MM:SS")
            
            current_date_str = current_datetime.strftime('%Y%m%d')
        else:
            raise ValueError("current_time必须是字符串格式")
    
    #print(f"解析的当前时间: {current_datetime.strftime('%Y-%m-%d %H:%M:%S')} (不包含此时间点)")
    
    # 转换复权方式
    dividend_type_map = {
        'pre': 'front',
        'post': 'back', 
        'none': 'none'
    }
    dividend_type = dividend_type_map.get(fq, 'front')
    
    # 转换时间步长格式
    period_map = {
        '1d': '1d',
        '1m': '1m',
        '5m': '5m',
        'tick': 'tick'
    }
    period = period_map.get(fre_step, fre_step)
    
    # ── 缓存开关：回测模式（current_time 固定）且非 tick，force_download 时 bypass ──
    fields_key = tuple(sorted(fields))
    framework = None
    perf_cfg = {}
    cache_mode = "backtest_window"
    prefetch_end_mode = "current_day"
    try:
        framework = _get_current_framework_for_history_warning()
        perf_cfg = _khist_perf_config(framework)
        cache_mode = str(perf_cfg.get("khhistory_cache_mode", "backtest_window")).lower()
        prefetch_end_mode = str(perf_cfg.get("khhistory_prefetch_end", "current_day")).lower()
    except Exception:
        cache_mode = "backtest_window"
        prefetch_end_mode = "current_day"
    use_cache = (
        current_time is not None
        and not force_download
        and period != 'tick'
        and cache_mode not in ("off", "none", "disabled")
    )

    cache_lookback_days = _khist_calc_lookback_days(period, bar_count, cache_mode=True)
    cache_required_start = current_datetime - timedelta(days=cache_lookback_days)
    try:  # 自适应预加载(#17): 记录策略实际回看需求, 供动态加载器消除分段边界 idx_short 回退
        _kh_need = _khist_calc_lookback_days(period, bar_count, cache_mode=False)
        global _KHIST_OBSERVED_LOOKBACK_DAYS, _KHIST_OBSERVED_TRADING_DAYS
        if _kh_need > _KHIST_OBSERVED_LOOKBACK_DAYS:
            _KHIST_OBSERVED_LOOKBACK_DAYS = _kh_need
        # 交易日口径: 需要 ceil(bar_count / 每日bar数) + 缓冲 个交易日(跨假期更稳)
        if period in ('1m', '5m'):
            _bpd = 240 if period == '1m' else 48
            _td_need = (int(bar_count) + _bpd - 1) // _bpd + 2
        elif period == '1d':
            _td_need = int(bar_count) + 2
        else:
            _td_need = max(2, int(bar_count) // 240 + 2)
        if _td_need > _KHIST_OBSERVED_TRADING_DAYS:
            _KHIST_OBSERVED_TRADING_DAYS = _td_need
    except Exception:
        pass

    # ── 全命中快速返回：所有股票在缓存中时，完全绕过数据源初始化 ─────────────────
    cached_result = {}
    stocks_to_load = stock_codes
    if use_cache:
        memory_result, memory_misses = _khist_try_framework_memory(
            framework,
            stock_codes,
            period,
            fields,
            fields_key,
            dividend_type,
            skip_paused,
            current_datetime,
            bar_count,
            requested_columns,
            perf_cfg,
            cache_required_start,
        )
        if len(memory_result) == len(stock_codes):
            return memory_result
        cached_result.update(memory_result)
        stocks_to_load = memory_misses

    if use_cache and _KHIST_CACHE:
        all_hit = True
        with _KHIST_CACHE_LOCK:
            cache_entries = {sc: _KHIST_CACHE.get((sc, period, fields_key, dividend_type, skip_paused))
                             for sc in stocks_to_load}
        for sc, entry in cache_entries.items():
            if entry is None or not _khist_cache_covers(entry, period, current_datetime, cache_required_start):
                all_hit = False
                continue
            cached_result[sc] = _khist_cache_slice(entry, period, current_datetime, bar_count, perf_cfg)
        if all_hit:
            with _KHIST_CACHE_LOCK:
                _KHIST_STATS["hits"] += len(cache_entries)
            return cached_result
        stocks_to_load = [sc for sc in stock_codes if sc not in cached_result]
        with _KHIST_CACHE_LOCK:
            _KHIST_STATS["hits"] += sum(1 for sc in cache_entries if sc in cached_result)
            _KHIST_STATS["extensions"] += sum(
                1 for sc, entry in cache_entries.items()
                if entry is not None and sc not in cached_result
            )
            _KHIST_STATS["misses"] += sum(1 for entry in cache_entries.values() if entry is None)
    # ──────────────────────────────────────────────────────────────────────────

    result = dict(cached_result)
    try:
        # 只有缓存未命中、窗口需扩展或强制下载时才初始化数据源。
        data_source_mgr = _resolve_data_source_manager_for_khqt()
        if data_source_mgr is None:
            logging.warning("khHistory：DuckDB 数据源未初始化，无法读取历史数据")
            return result

        if force_download:
            # 开源版的数据都在本地 DuckDB，这里不联网下载；force_download
            # 只让本次调用跳过缓存（见上方 use_cache），重新从数据库读取。
            print(f"强制刷新模式：基于时间 {current_date_str} 重新读取本地数据")

        # ── 计算查询范围 ────────────────────────────────────────────────────────
        if use_cache:
            # 缓存模式：仅加载覆盖 bar_count 的回测窗口，不再默认拉取多年分钟数据。
            start_dt = cache_required_start
            query_start_time = start_dt.strftime('%Y%m%d')
            query_end_time = current_date_str
            dynamic_framework_window = _khist_framework_dynamic_loading(framework)
            if (
                prefetch_end_mode in ("backtest_end", "full", "window_end")
                and _khist_prefetch_to_backtest_end_allowed(dividend_type)
                and not dynamic_framework_window
            ):
                bt_end_time = _khist_backtest_end_time(framework)
                if bt_end_time:
                    bt_end_ts = _khist_end_ts(bt_end_time, current_datetime)
                    if bt_end_ts >= _khist_cache_cut(period, current_datetime):
                        query_end_time = bt_end_time
        else:
            # 非缓存模式：保留原有按 bar_count 推算的有限 lookback
            lookback_days = _khist_calc_lookback_days(period, bar_count, cache_mode=False)
            start_dt = current_datetime - timedelta(days=lookback_days)
            query_start_time = start_dt.strftime('%Y%m%d')
            query_end_time = current_date_str

        if not stocks_to_load:
            return result
        empty_cache_end_ts = _khist_end_ts(query_end_time, current_datetime)
        if use_cache and not _KHIST_CACHE:
            with _KHIST_CACHE_LOCK:
                _KHIST_STATS["misses"] += len(stocks_to_load)
        if use_cache:
            with _KHIST_CACHE_LOCK:
                if query_end_time != current_date_str:
                    _KHIST_STATS["full_prefetch_reads"] += 1
                else:
                    _KHIST_STATS["current_day_prefetch_reads"] += 1
        else:
            with _KHIST_CACHE_LOCK:
                _KHIST_STATS["fallback_reads"] += 1

        data = data_source_mgr.get_market_data_ex(
            field_list=['time'] + fields, stock_list=stocks_to_load,
            period=period, start_time=query_start_time, end_time=query_end_time,
            count=-1, dividend_type=dividend_type,
            fill_data=True,
        )
        
        if not data:
            logging.warning(
                "khHistory 未获取到任何数据: 股票=%s 周期=%s 字段=%s 复权=%s 当前时间=%s",
                stocks_to_load, period, fields, dividend_type, current_time
            )
            return result
        
        # ── 处理每只需加载股票的数据 ─────────────────────────────────────────────
        try:
            for stock_code in stocks_to_load:
                if stock_code not in data:
                    _warn_kh_history_missing_once(
                        stock_code, period, fields, dividend_type,
                        "数据源未返回该股票", current_time=current_time
                    )
                    if use_cache:
                        _khist_record_empty_cache(
                            (stock_code, period, fields_key, dividend_type, skip_paused),
                            requested_columns,
                            pd.Timestamp(start_dt),
                            empty_cache_end_ts,
                        )
                    result[stock_code] = _khist_empty_result(requested_columns, perf_cfg)
                    continue
                
                stock_data = data[stock_code]
                
                if stock_data is None or stock_data.empty:
                    if use_cache:
                        _khist_record_empty_cache(
                            (stock_code, period, fields_key, dividend_type, skip_paused),
                            requested_columns,
                            pd.Timestamp(start_dt),
                            empty_cache_end_ts,
                        )
                    _warn_kh_history_missing_once(
                        stock_code, period, fields, dividend_type,
                        "数据源返回空数据", current_time=current_time
                    )
                    result[stock_code] = _khist_empty_result(requested_columns, perf_cfg)
                    continue
                
                stock_data = stock_data.copy()
                
                # 修复 time 列缺失
                if 'time' not in stock_data.columns and not stock_data.empty:
                    stock_data.reset_index(inplace=True)
                    if 'index' in stock_data.columns:
                        stock_data.rename(columns={'index': 'time'}, inplace=True)
                    elif stock_data.index.name and stock_data.index.name in stock_data.columns:
                        stock_data.rename(columns={stock_data.index.name: 'time'}, inplace=True)

                # 转换时间列为 datetime
                if 'time' in stock_data.columns and not stock_data.empty:
                    try:
                        first_val = stock_data['time'].iloc[0]
                        is_yyyymmdd = False
                        try:
                            float_val = float(first_val)
                            if float_val > 10000000000000:
                                is_yyyymmdd = True
                        except Exception:
                            pass
                        if is_yyyymmdd:
                            stock_data['time'] = pd.to_datetime(
                                stock_data['time'].astype(str), format='%Y%m%d%H%M%S', errors='coerce')
                        else:
                            try:
                                stock_data['time'] = (pd.to_datetime(stock_data['time'].astype(float), unit='ms')
                                                      + pd.Timedelta(hours=8))
                            except (ValueError, OverflowError):
                                stock_data['time'] = pd.to_datetime(stock_data['time'])
                    except Exception as e:
                        print(f"警告: 时间列转换出错: {str(e)}")
                        try:
                            stock_data['time'] = pd.to_datetime(stock_data['time'])
                        except Exception:
                            pass

                # 按时间排序
                if 'time' in stock_data.columns:
                    stock_data = stock_data.sort_values('time').reset_index(drop=True)

                # 跳过停牌数据
                if skip_paused and 'volume' in stock_data.columns:
                    original_len = len(stock_data)
                    stock_data = stock_data[stock_data['volume'] > 0].reset_index(drop=True)
                    if original_len != len(stock_data):
                        print(f"股票 {stock_code} 过滤停牌数据: {original_len} -> {len(stock_data)}")

                missing_fields = [col for col in fields if col != 'time' and col not in stock_data.columns]
                if missing_fields:
                    if use_cache:
                        _khist_record_empty_cache(
                            (stock_code, period, fields_key, dividend_type, skip_paused),
                            requested_columns,
                            pd.Timestamp(start_dt),
                            empty_cache_end_ts,
                        )
                    _warn_kh_history_missing_once(
                        stock_code, period, fields, dividend_type,
                        f"缺少请求字段 {missing_fields}", current_time=current_time,
                        available_rows=len(stock_data)
                    )
                    result[stock_code] = _khist_empty_result(requested_columns, perf_cfg)
                    continue

                # 整理列顺序（time 列在前）
                columns_order = ['time'] + [col for col in fields if col != 'time' and col in stock_data.columns]
                stock_data = stock_data[columns_order]

                if use_cache and not stock_data.empty and 'time' in stock_data.columns:
                    # ── 写入窗口缓存（不做 bar_count 裁剪，后续按 current_time 切片）─
                    cache_key = (stock_code, period, fields_key, dividend_type, skip_paused)
                    time_np = stock_data['time'].values.astype('datetime64[ns]')   # 统一 ns 精度
                    if len(stock_data) <= _KHIST_CACHE_MAX_ROWS:
                        requested_end_ts = _khist_end_ts(query_end_time, current_datetime)
                        _khist_record_cache_write(
                            cache_key,
                            stock_data,
                            time_np,
                            requested_start_ts=pd.Timestamp(start_dt),
                            requested_end_ts=requested_end_ts,
                        )
                        logging.debug(f"khHistory 缓存已写入: {stock_code} {period} {len(stock_data)}行")
                    # 按 current_time 切片 + bar_count 裁剪后返回
                    cut = (pd.Timestamp(current_datetime.date())
                           if period == '1d' else pd.Timestamp(current_datetime))
                    idx = int(np.searchsorted(time_np, cut.to_datetime64(), side='left'))
                    sliced = stock_data.iloc[:idx]
                    if len(sliced) < bar_count:
                        _warn_kh_history_missing_once(
                            stock_code, period, fields, dividend_type,
                            f"当前时间前历史数据不足，需要 {bar_count} 行",
                            current_time=current_time,
                            available_rows=len(sliced),
                        )
                    result[stock_code] = (sliced.iloc[-bar_count:].reset_index(drop=True).copy()
                                          if len(sliced) > bar_count
                                          else sliced.reset_index(drop=True).copy())
                else:
                    # ── 非缓存模式：沿用原有时间过滤 + bar_count 裁剪逻辑 ──────────
                    if 'time' in stock_data.columns:
                        if period in ['tick', '1m', '5m']:
                            mask = stock_data['time'] < current_datetime
                        else:
                            mask = stock_data['time'].dt.date < current_datetime.date()
                        stock_data = stock_data[mask].reset_index(drop=True)
                    if len(stock_data) < bar_count:
                        _warn_kh_history_missing_once(
                            stock_code, period, fields, dividend_type,
                            f"当前时间前历史数据不足，需要 {bar_count} 行",
                            current_time=current_time,
                            available_rows=len(stock_data),
                        )
                    if not stock_data.empty and len(stock_data) > bar_count:
                        stock_data = stock_data.tail(bar_count).reset_index(drop=True)
                    result[stock_code] = stock_data

        except Exception as e:
            if isinstance(e, RuntimeError) and "用户因 khHistory 缺少历史数据停止回测" in str(e):
                raise
            print(f"处理股票数据时出错: {str(e)}")
            import traceback
            traceback.print_exc()
            return {}
    except Exception as e:
        if isinstance(e, RuntimeError) and "用户因 khHistory 缺少历史数据停止回测" in str(e):
            raise
        print(f"获取历史数据时出错: {str(e)}")
        import traceback
        traceback.print_exc()
        return {}
    
    return result


def khKline(
    symbol_list: Union[str, List[str]],
    period: str,
    bar_count: int,
    fields: List[str] = None,
    end_time: Optional[str] = None,
    fq: str = 'pre',
    force_download: bool = False
) -> Dict[str, pd.DataFrame]:
    """
    获取任意自定义周期的K线数据，支持年对齐和未收线实时快照
    
    专为缠论多级别共振策略设计，核心特性：
    1. 年对齐：多日周期(2d/3d等)以年初第一个交易日为基准对齐
    2. 未收线支持：交易时段内，未收线K线使用当前时刻数据快照
    3. 多级别共振：支持小周期收线+大周期未收线状态的共振判断
    
    参数:
        symbol_list: 股票代码列表或单个股票代码字符串
                    例如: '000001.SZ' 或 ['000001.SZ', '600519.SH']
        period: K线周期，支持格式：
               - 分钟: '1m', '5m', '15m', '30m', '60m' 等
               - 小时: '1h', '2h', '3h', '4h' 等
               - 天: '1d', '2d', '3d', '4d' ... (无上限)
        bar_count: 获取的K线数量，最大支持240根
        fields: 数据字段列表，默认['open', 'high', 'low', 'close', 'volume']
               可选字段: 'open', 'high', 'low', 'close', 'volume', 'amount' 等
        end_time: 结束时间（可选）
                 - 格式1: '2025-09-24 14:15:00' (2025年9月24日14点15分00秒)
                 - 格式2: '2025-09-24 14:15' (2025年9月24日14点15分)
                 - 格式3: '20250924 1415' (2025年9月24日14点15分)
                 - 格式4: '20250924' (2025年9月24日)
                 - None: 使用当前时间
                 说明: 以该时间点向前获取指定数量的K线，秒数会被忽略
        fq: 复权方式，默认'pre'
           - 'pre': 前复权
           - 'post': 后复权
           - 'none': 不复权
        force_download: 是否强制下载最新数据，默认False
    
    返回:
        dict: {股票代码: DataFrame}
        DataFrame包含列: time, open, high, low, close, volume 等
        
    核心逻辑说明:
        1. 交易时段内: 所有未收线周期的最后一根K线使用当下时间数据快照
        2. 非交易时段: 使用最近的交易时间点数据
        3. 指定end_time: 如果end_time落在某K线周期内，生成该时刻快照K线
        
    使用示例:
        # 获取1分钟K线
        data = khKline('000001.SZ', '1m', 50)
        
        # 获取2小时K线（未收线会包含当前数据）
        data = khKline(['000001.SZ', '600519.SH'], '2h', 30)
        
        # 获取3日K线（年对齐）
        data = khKline('000001.SZ', '3d', 20)
        
        # 指定历史时间点回测（支持多种格式）
        data = khKline('000001.SZ', '1h', 10, end_time='2024-12-01 14:30:00')
        data = khKline('000001.SZ', '1h', 10, end_time='2024-12-01 14:30')
        data = khKline('000001.SZ', '1h', 10, end_time='20241201 1430')
    """
    
    # 导入必要的模块
    try:
        import pandas as pd
        from datetime import datetime, timedelta
        import re
    except ImportError as e:
        logging.error(f"导入模块失败: {str(e)}")
        return {}
    
    # 设置默认字段
    if fields is None:
        fields = ['open', 'high', 'low', 'close', 'volume']
    
    # 参数验证
    if not symbol_list:
        raise ValueError("symbol_list不能为空")
    if not period:
        raise ValueError("period不能为空")
    if bar_count <= 0:
        raise ValueError("bar_count必须大于0")
    
    # 统一处理股票代码列表
    if isinstance(symbol_list, str):
        stock_codes = [symbol_list]
    else:
        stock_codes = list(symbol_list)
    
    # 解析周期参数
    period_match = re.match(r'^(\d+)([mhd])$', period.lower())
    if not period_match:
        raise ValueError(f"不支持的周期格式: {period}，支持格式如: 1m, 5m, 1h, 2h, 1d, 2d等")
    
    period_num = int(period_match.group(1))
    period_unit = period_match.group(2)  # 'm', 'h', 'd'
    
    # 解析结束时间
    if end_time is None:
        target_datetime = datetime.now()
    else:
        end_time = end_time.strip()
        # 尝试解析不同格式(按精确度从高到低尝试)
        for fmt in ['%Y-%m-%d %H:%M:%S', '%Y%m%d %H%M%S', '%Y%m%d %H%M', '%Y-%m-%d %H:%M', '%Y%m%d', '%Y-%m-%d']:
            try:
                target_datetime = datetime.strptime(end_time, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"无法解析时间格式: {end_time}，支持格式: YYYYMMDD, YYYYMMDD HHMM, YYYY-MM-DD HH:MM:SS等")
        
        # 将秒数归零(忽略秒数部分)
        target_datetime = target_datetime.replace(second=0, microsecond=0)
    
    logging.info(f"khKline: 股票={stock_codes}, 周期={period}, 数量={bar_count}, 目标时间={target_datetime}")
    
    # 转换复权方式
    dividend_type_map = {'pre': 'front', 'post': 'back', 'none': 'none'}
    dividend_type = dividend_type_map.get(fq, 'front')

    # 获取数据源管理器（DuckDB）
    data_source_mgr = _get_data_source_manager_for_kline()
    if data_source_mgr is None:
        logging.error("khKline无法初始化数据源：DuckDB 数据源未初始化")
        return {}
    
    try:
        # 根据周期类型选择基础数据获取策略
        if period_unit == 'm':
            # 分钟周期
            result = _get_minute_kline(
                stock_codes, period_num, bar_count, fields, 
                target_datetime, dividend_type, force_download, data_source_mgr
            )
        elif period_unit == 'h':
            # 小时周期
            result = _get_hour_kline(
                stock_codes, period_num, bar_count, fields,
                target_datetime, dividend_type, force_download, data_source_mgr
            )
        elif period_unit == 'd':
            # 天周期
            result = _get_day_kline(
                stock_codes, period_num, bar_count, fields,
                target_datetime, dividend_type, force_download, data_source_mgr
            )
        else:
            raise ValueError(f"不支持的周期单位: {period_unit}")
        
        return result
        
    except Exception as e:
        logging.error(f"khKline获取数据时出错: {str(e)}", exc_info=True)
        return {}


def _get_data_source_manager_for_kline():
    """获取 khKline 使用的数据源管理器。

    优先使用全局实例，若未初始化则尝试从设置初始化。
    """
    return _resolve_data_source_manager_for_khqt()


def _kline_get_market_data_ex(
    data_source_mgr,
    field_list: List[str],
    stock_list: List[str],
    period: str,
    start_time: str,
    end_time: str,
    dividend_type: str,
):
    """khKline 内部取数封装（DuckDB）。

    经 ``_legacy_call_with_supported_kwargs`` 按签名过滤参数后调用
    ``get_market_data_ex(fill_data=True)``，与 CS 版的 DuckDB 路径一致。
    """
    if data_source_mgr is None:
        return {}
    kwargs = {
        "field_list": field_list,
        "stock_list": stock_list,
        "period": period,
        "start_time": start_time,
        "end_time": end_time,
        "count": -1,
        "dividend_type": dividend_type,
        "fill_data": True,
    }
    return _legacy_call_with_supported_kwargs(
        data_source_mgr.get_market_data_ex, kwargs
    )


def _parse_period(period: str) -> tuple:
    """解析周期字符串，返回(数字, 单位)"""
    import re
    match = re.match(r'^(\d+)([mhd])$', period.lower())
    if not match:
        raise ValueError(f"不支持的周期格式: {period}")
    return int(match.group(1)), match.group(2)


def _get_year_first_trade_day(year: int) -> datetime:
    """获取指定年份的第一个交易日"""
    from datetime import datetime, timedelta
    
    # 从1月1日开始查找
    current_date = datetime(year, 1, 1)
    end_date = datetime(year, 2, 1)  # 最多查到2月1日
    
    while current_date < end_date:
        date_str = current_date.strftime('%Y-%m-%d')
        if is_trade_day(date_str):
            return current_date
        current_date += timedelta(days=1)
    
    # 如果找不到，返回1月1日（理论上不应该发生）
    logging.warning(f"未找到{year}年的第一个交易日，使用1月1日")
    return datetime(year, 1, 1)


def _get_trade_days_list(start_date: datetime, end_date: datetime) -> List[datetime]:
    """
    获取指定日期范围内的所有交易日列表

    统一复用 data/trade_days.csv 持久化缓存；缓存缺失时由
    get_trade_days_set 负责一次联网补充、失败冷却和离线降级。

    Args:
        start_date: 开始日期（datetime对象）
        end_date: 结束日期（datetime对象）

    Returns:
        交易日列表（datetime对象列表）
    """
    if start_date > end_date:
        return []

    # 统一走持久化交易日缓存。联网拉取、失败冷却和离线工作日降级均由
    # get_trade_days_set 负责，避免旧链路在每次 khKline 调用时重复登录。
    trade_days = get_trade_days_set(
        start_date.strftime("%Y%m%d"),
        end_date.strftime("%Y%m%d"),
    )
    return [datetime.strptime(d, "%Y%m%d") for d in sorted(trade_days)]


def _process_930_data(df: pd.DataFrame, fields: List[str]) -> pd.DataFrame:
    """
    处理09:30的数据问题
    
    问题：09:30的高开低收都是开盘价（数据不完整）
    解决：将09:30的开盘价和成交量合并到09:31
    
    参数:
        df: 原始分钟K线数据，已排序
        fields: 数据字段列表
    
    返回:
        处理后的DataFrame
    """
    import pandas as pd
    
    if df.empty:
        return df
    
    # 按日期分组处理
    result_dfs = []
    
    for date, date_df in df.groupby(df['time'].dt.date):
        # 查找09:30和09:31的数据
        mask_930 = (date_df['time'].dt.hour == 9) & (date_df['time'].dt.minute == 30)
        mask_931 = (date_df['time'].dt.hour == 9) & (date_df['time'].dt.minute == 31)
        
        data_930 = date_df[mask_930]
        data_931 = date_df[mask_931]
        
        if not data_930.empty and not data_931.empty:
            # 有09:30和09:31的数据，需要合并
            idx_930 = data_930.index[0]
            idx_931 = data_931.index[0]
            
            # 将09:30的开盘价赋给09:31
            if 'open' in fields and 'open' in date_df.columns:
                date_df.loc[idx_931, 'open'] = date_df.loc[idx_930, 'open']
            
            # 将09:30的成交量累加到09:31
            if 'volume' in fields and 'volume' in date_df.columns:
                date_df.loc[idx_931, 'volume'] = date_df.loc[idx_930, 'volume'] + date_df.loc[idx_931, 'volume']
            
            # 将09:30的成交额累加到09:31（如果有）
            if 'amount' in fields and 'amount' in date_df.columns:
                date_df.loc[idx_931, 'amount'] = date_df.loc[idx_930, 'amount'] + date_df.loc[idx_931, 'amount']
            
            # 删除09:30的数据
            date_df = date_df[~mask_930].copy()
        
        result_dfs.append(date_df)
    
    # 合并所有日期的数据
    if result_dfs:
        result = pd.concat(result_dfs, ignore_index=True)
        result = result.sort_values('time').reset_index(drop=True)
        return result
    else:
        return df


def _aggregate_kline(df: pd.DataFrame, group_col: str, fields: List[str]) -> pd.DataFrame:
    """
    聚合K线数据
    
    参数:
        df: 原始K线数据，必须包含time列
        group_col: 分组列名（用于groupby）
        fields: 需要聚合的字段
    """
    agg_dict = {}
    
    # 时间取最后一个
    agg_dict['time'] = 'last'
    
    # 处理各个字段
    if 'open' in fields:
        agg_dict['open'] = 'first'
    if 'high' in fields:
        agg_dict['high'] = 'max'
    if 'low' in fields:
        agg_dict['low'] = 'min'
    if 'close' in fields:
        agg_dict['close'] = 'last'
    if 'volume' in fields:
        agg_dict['volume'] = 'sum'
    if 'amount' in fields:
        agg_dict['amount'] = 'sum'
    
    # 执行聚合
    result = df.groupby(group_col, as_index=False).agg(agg_dict)
    return result


def _ensure_time_column_for_kline_df(df: pd.DataFrame, source_period: str) -> pd.DataFrame:
    """确保K线DataFrame存在标准datetime类型的time列。

    兼容两类输入：
    - xtdata风格：time列为毫秒时间戳
    - duckdb适配器风格：分钟线time在索引中（YYYYMMDDHHMMSS），不在列中
    """
    if df is None or df.empty:
        return df

    df = df.copy()

    from duckdb_storage.time_utils import coerce_market_time

    # 1) 先保证存在 time 列
    if 'time' not in df.columns:
        # 使用 numpy 值赋列，避免原索引与解析结果索引对齐后产生空值。
        df['time'] = coerce_market_time(df.index).to_numpy()

    # 2) 统一 time 列为 datetime
    if 'time' in df.columns:
        df['time'] = coerce_market_time(df['time'])

    # 3) 丢弃无法解析时间的行，避免后续分组/筛选报错
    if 'time' in df.columns:
        df = df.dropna(subset=['time'])

    return df


def _get_minute_kline(
    stock_codes: List[str],
    period_minutes: int,
    bar_count: int,
    fields: List[str],
    target_datetime: datetime,
    dividend_type: str,
    force_download: bool,
    data_source_mgr=None
) -> Dict[str, pd.DataFrame]:
    """获取分钟级别的K线数据"""
    import pandas as pd
    from datetime import datetime, timedelta
    
    result = {}
    
    # 判断是否为原生支持的分钟周期
    native_periods = [1, 5]
    
    if period_minutes in native_periods:
        # 使用原生周期直接获取
        period_str = f"{period_minutes}m"
        
        # 计算需要获取的数据范围
        lookback_days = max(10, (bar_count * period_minutes + 1439) // 1440)
        start_dt = target_datetime - timedelta(days=lookback_days)
        start_time = start_dt.strftime('%Y%m%d')
        end_time = target_datetime.strftime('%Y%m%d')
        
        
        # 获取数据
        data = _kline_get_market_data_ex(
            data_source_mgr=data_source_mgr,
            field_list=['time'] + fields,
            stock_list=stock_codes,
            period=period_str,
            start_time=start_time,
            end_time=end_time,
            dividend_type=dividend_type,
        )
        
        if not data:
            return {}
        
        # 处理每只股票
        for stock_code in stock_codes:
            if stock_code not in data or data[stock_code] is None or data[stock_code].empty:
                result[stock_code] = pd.DataFrame()
                continue
            
            df = data[stock_code].copy()
            
            # 统一时间列（兼容DuckDB适配器返回索引时间）
            df = _ensure_time_column_for_kline_df(df, period_str)
            if df.empty or 'time' not in df.columns:
                result[stock_code] = pd.DataFrame()
                continue
            
            # 筛选到目标时间（包含未收线）
            df = df[df['time'] <= target_datetime].copy()
            
            # 排序
            df = df.sort_values('time').reset_index(drop=True)
            
            # 取最近的bar_count条
            if len(df) > bar_count:
                df = df.tail(bar_count).reset_index(drop=True)
            
            result[stock_code] = df
    
    else:
        # 非原生周期，需要聚合1分钟数据
        lookback_days = max(10, (bar_count * period_minutes + 1439) // 1440)
        start_dt = target_datetime - timedelta(days=lookback_days)
        start_time = start_dt.strftime('%Y%m%d')
        end_time = target_datetime.strftime('%Y%m%d')
        
        
        # 获取1分钟数据
        data = _kline_get_market_data_ex(
            data_source_mgr=data_source_mgr,
            field_list=['time'] + fields,
            stock_list=stock_codes,
            period='1m',
            start_time=start_time,
            end_time=end_time,
            dividend_type=dividend_type,
        )
        
        if not data:
            return {}
        
        # 处理每只股票
        for stock_code in stock_codes:
            if stock_code not in data or data[stock_code] is None or data[stock_code].empty:
                result[stock_code] = pd.DataFrame()
                continue
            
            df = data[stock_code].copy()
            
            # 统一时间列（兼容DuckDB适配器返回索引时间）
            df = _ensure_time_column_for_kline_df(df, '1m')
            if df.empty or 'time' not in df.columns:
                result[stock_code] = pd.DataFrame()
                continue
            
            # 筛选到目标时间
            df = df[df['time'] <= target_datetime].copy()
            
            if df.empty:
                result[stock_code] = pd.DataFrame()
                continue
            
            # 排序
            df = df.sort_values('time').reset_index(drop=True)
            
            # 处理09:30的数据问题
            # 09:30的高开低收都是开盘价，需要将其开盘价和成交量合并到09:31
            df = _process_930_data(df, fields)
            
            # 按自定义周期聚合
            # 策略：从09:31开始，每period_minutes分钟一组
            # 09:31-09:45为第1组(0-14分钟)，09:46-10:00为第2组(15-29分钟)
            df['date'] = df['time'].dt.date
            
            # 计算从09:31开始的分钟数（09:31算第0分钟）
            df['minutes_since_931'] = (df['time'].dt.hour - 9) * 60 + df['time'].dt.minute - 31
            
            # 处理下午时段（13:00之后需要减去午休的90分钟）
            # 午休是11:31-13:00，共90分钟
            df.loc[df['time'].dt.hour >= 13, 'minutes_since_931'] -= 90
            
            # 计算分组ID（每period_minutes分钟一组）
            # 使用3位数字格式，确保字符串排序正确
            df['group_id'] = df['date'].astype(str) + '_' + (df['minutes_since_931'] // period_minutes).apply(lambda x: f"{x:03d}")
            
            # 聚合
            agg_df = _aggregate_kline(df, 'group_id', fields)
            
            # 删除临时列
            if 'group_id' in agg_df.columns:
                agg_df = agg_df.drop(columns=['group_id'])
            
            # 取最近的bar_count条
            if len(agg_df) > bar_count:
                agg_df = agg_df.tail(bar_count).reset_index(drop=True)
            
            result[stock_code] = agg_df
    
    return result


def _get_hour_kline(
    stock_codes: List[str],
    period_hours: int,
    bar_count: int,
    fields: List[str],
    target_datetime: datetime,
    dividend_type: str,
    force_download: bool,
    data_source_mgr=None
) -> Dict[str, pd.DataFrame]:
    """获取小时级别的K线数据"""
    import pandas as pd
    from datetime import datetime, timedelta
    
    result = {}
    
    # 小时周期通过聚合1分钟数据实现
    period_minutes = period_hours * 60
    lookback_days = max(10, (bar_count * period_minutes + 1439) // 1440)
    start_dt = target_datetime - timedelta(days=lookback_days)
    start_time = start_dt.strftime('%Y%m%d')
    end_time = target_datetime.strftime('%Y%m%d')
    
    
    # 获取1分钟数据
    data = _kline_get_market_data_ex(
        data_source_mgr=data_source_mgr,
        field_list=['time'] + fields,
        stock_list=stock_codes,
        period='1m',
        start_time=start_time,
        end_time=end_time,
        dividend_type=dividend_type,
    )
    
    if not data:
        return {}
    
    # 处理每只股票
    for stock_code in stock_codes:
        if stock_code not in data or data[stock_code] is None or data[stock_code].empty:
            result[stock_code] = pd.DataFrame()
            continue
        
        df = data[stock_code].copy()
        
        # 统一时间列（兼容DuckDB适配器返回索引时间）
        df = _ensure_time_column_for_kline_df(df, '1m')
        if df.empty or 'time' not in df.columns:
            result[stock_code] = pd.DataFrame()
            continue
        
        # 筛选到目标时间
        df = df[df['time'] <= target_datetime].copy()
        
        if df.empty:
            result[stock_code] = pd.DataFrame()
            continue
        
        # 排序
        df = df.sort_values('time').reset_index(drop=True)
        
        # 处理09:30的数据问题
        # 09:30的高开低收都是开盘价，需要将其开盘价和成交量合并到09:31
        df = _process_930_data(df, fields)
        
        # 按小时周期聚合
        # 策略：从09:31开始，每period_hours小时一组
        df['date'] = df['time'].dt.date
        
        # 计算从09:31开始的分钟数（09:31算第0分钟）
        df['minutes_since_931'] = (df['time'].dt.hour - 9) * 60 + df['time'].dt.minute - 31
        
        # 处理下午时段（13:00之后需要减去午休的90分钟）
        df.loc[df['time'].dt.hour >= 13, 'minutes_since_931'] -= 90
        
        # 计算分组ID（每period_minutes分钟一组）
        # 使用3位数字格式，确保字符串排序正确
        df['group_id'] = df['date'].astype(str) + '_' + (df['minutes_since_931'] // period_minutes).apply(lambda x: f"{x:03d}")
        
        # 聚合
        agg_df = _aggregate_kline(df, 'group_id', fields)
        
        # 删除临时列
        if 'group_id' in agg_df.columns:
            agg_df = agg_df.drop(columns=['group_id'])
        
        # 取最近的bar_count条
        if len(agg_df) > bar_count:
            agg_df = agg_df.tail(bar_count).reset_index(drop=True)
        
        result[stock_code] = agg_df
    
    return result


def _get_day_kline(
    stock_codes: List[str],
    period_days: int,
    bar_count: int,
    fields: List[str],
    target_datetime: datetime,
    dividend_type: str,
    force_download: bool,
    data_source_mgr=None
) -> Dict[str, pd.DataFrame]:
    """获取天级别的K线数据，支持年对齐"""
    import pandas as pd
    from datetime import datetime, timedelta
    
    result = {}
    
    if period_days == 1:
        # 1日线直接获取
        lookback_days = bar_count * 5
        start_dt = target_datetime - timedelta(days=lookback_days)
        start_time = start_dt.strftime('%Y%m%d')
        end_time = target_datetime.strftime('%Y%m%d')
        
        
        # 获取数据
        data = _kline_get_market_data_ex(
            data_source_mgr=data_source_mgr,
            field_list=['time'] + fields,
            stock_list=stock_codes,
            period='1d',
            start_time=start_time,
            end_time=end_time,
            dividend_type=dividend_type,
        )
        
        if not data:
            return {}
        
        # 处理每只股票
        for stock_code in stock_codes:
            if stock_code not in data or data[stock_code] is None or data[stock_code].empty:
                result[stock_code] = pd.DataFrame()
                continue
            
            df = data[stock_code].copy()
            
            # 统一时间列（兼容DuckDB适配器返回索引时间）
            df = _ensure_time_column_for_kline_df(df, '1d')
            if df.empty or 'time' not in df.columns:
                result[stock_code] = pd.DataFrame()
                continue
            
            # 筛选到目标时间（对于日线，包含当天）
            target_date = target_datetime.date()
            df = df[df['time'].dt.date <= target_date].copy()
            
            # 排序
            df = df.sort_values('time').reset_index(drop=True)
            
            # 取最近的bar_count条
            if len(df) > bar_count:
                df = df.tail(bar_count).reset_index(drop=True)
            
            result[stock_code] = df
    
    else:
        # 多日周期，需要年对齐
        # 获取目标年份的第一个交易日
        target_year = target_datetime.year
        year_first_day = _get_year_first_trade_day(target_year)
        
        # 如果目标时间在年初第一个交易日之前，需要用上一年
        if target_datetime < year_first_day:
            target_year -= 1
            year_first_day = _get_year_first_trade_day(target_year)
        
        # 计算需要获取的数据范围
        lookback_days = bar_count * period_days * 5
        start_dt = max(year_first_day, target_datetime - timedelta(days=lookback_days))
        start_time = start_dt.strftime('%Y%m%d')
        end_time = target_datetime.strftime('%Y%m%d')
        
        
        # 获取日线数据
        data = _kline_get_market_data_ex(
            data_source_mgr=data_source_mgr,
            field_list=['time'] + fields,
            stock_list=stock_codes,
            period='1d',
            start_time=start_time,
            end_time=end_time,
            dividend_type=dividend_type,
        )
        
        if not data:
            return {}
        
        # 获取年度交易日列表（用于对齐）
        year_end = datetime(target_year, 12, 31)
        trade_days_list = _get_trade_days_list(year_first_day, min(target_datetime, year_end))
        
        if not trade_days_list:
            logging.error(f"未找到{target_year}年的交易日列表")
            return {}
        
        # 处理每只股票
        for stock_code in stock_codes:
            if stock_code not in data or data[stock_code] is None or data[stock_code].empty:
                result[stock_code] = pd.DataFrame()
                continue
            
            df = data[stock_code].copy()
            
            # 统一时间列（兼容DuckDB适配器返回索引时间）
            df = _ensure_time_column_for_kline_df(df, '1d')
            if df.empty or 'time' not in df.columns:
                result[stock_code] = pd.DataFrame()
                continue
            
            # 筛选到目标时间
            target_date = target_datetime.date()
            df = df[df['time'].dt.date <= target_date].copy()
            
            if df.empty:
                result[stock_code] = pd.DataFrame()
                continue
            
            # 排序
            df = df.sort_values('time').reset_index(drop=True)
            
            # 创建交易日索引映射
            df['trade_date'] = df['time'].dt.date
            
            # 根据年初第一个交易日计算分组
            # 为每个交易日分配组号
            trade_day_to_index = {}
            for idx, trade_day in enumerate(trade_days_list):
                trade_day_to_index[trade_day.date()] = idx
            
            # 计算每行数据的组ID
            def get_group_id(row):
                trade_date = row['trade_date']
                if trade_date not in trade_day_to_index:
                    return None
                trade_index = trade_day_to_index[trade_date]
                group_num = trade_index // period_days
                # 使用3位数字格式，确保字符串排序正确（支持到999组）
                return f"{target_year}_{group_num:03d}"
            
            df['group_id'] = df.apply(get_group_id, axis=1)
            
            # 过滤掉无效的组
            df = df[df['group_id'].notna()].copy()
            
            if df.empty:
                result[stock_code] = pd.DataFrame()
                continue
            
            # 聚合
            agg_df = _aggregate_kline(df, 'group_id', fields)
            
            # 取最近的bar_count条
            if len(agg_df) > bar_count:
                agg_df = agg_df.tail(bar_count).reset_index(drop=True)
            
            result[stock_code] = agg_df
    
    return result


# ============================================================================
# 便利实例 - 为了向后兼容，创建一个默认实例
# ============================================================================

# 创建一个默认的工具实例，供旧代码兼容使用
# 这样 `from khQTTools import KhQuTools; tools = KhQuTools()` 和 `from khQTTools import tools` 都能工作
tools = KhQuTools()


# ============================================================================
# Tushare 乘法前复权工具函数
# 策略代码中直接 `from khQuantImport import *` 后可调用 tushare_mul_qfq()
# Token 和代理配置由软件设置自动读取，无需手动传入
# ============================================================================

# 进程级缓存：相同参数及同一 Tushare 配置的请求只发一次 API。
_tushare_df_cache: "dict" = {}


def tushare_mul_qfq(
    ts_code:    str,
    start_date: str,
    end_date:   str,
    freq:       str = "D",
) -> "pd.DataFrame":
    """
    乘法前复权（tushare adj_factor 方式）。

    公式：前复权价 = 原始价 × (当日 adj_factor / 最新 adj_factor)

    结果新增列：open_front / high_front / low_front / close_front

    Args:
        ts_code    : 股票代码，如 '000001.SZ'
        start_date : 日线传 'YYYYMMDD'，分钟线传 'YYYY-MM-DD HH:MM:SS'
        end_date   : 日线传 'YYYYMMDD'，分钟线传 'YYYY-MM-DD HH:MM:SS'
                     日线建议传最新实际交易日（否则前复权基准日不对）
        freq       : 'D'=日线，'1min'/'5min'=分钟线

    Returns:
        DataFrame（含原始 OHLCV + open_front/high_front/low_front/close_front）

    注意：
    - 同参数结果在本次运行内缓存，重复调用不重复请求 API
    - adj_factor 接口需要约 2000 积分权限
    - Token 未配置时返回空 DataFrame 并打印警告
    - 日线和分钟线均支持复权
    - **请在策略 init() 中调用，不要放在 khHandlebar() 中**（否则每根 K 线都发 API 请求）

    示例（策略 init 中）::

        global g_df_adj
        g_df_adj = tushare_mul_qfq("000001.SZ", "20240101", "20241231", freq="D")

    示例（策略 khHandlebar 中使用缓存结果）::

        if not g_df_adj.empty:
            close_adj = g_df_adj["close_front"].iloc[-1]
    """
    try:
        import pandas as pd
        from duckdb_storage.tushare_importer import TushareImporter
        from tushare_config import load_tushare_settings
    except ImportError as e:
        logging.warning(f"[tushare_mul_qfq] 导入失败: {e}")
        try:
            import pandas as pd
            return pd.DataFrame()
        except Exception:
            return None

    settings = load_tushare_settings()
    if not settings.token:
        logging.warning("[tushare_mul_qfq] Tushare Token 未配置，请在【软件设置 → 数据设置】中设置。")
        return pd.DataFrame()

    # 命中缓存直接返回副本（防止调用方修改影响缓存）。配置签名可确保
    # 运行中修改 Token/API/代理后不会继续返回旧服务端的数据。
    cache_key = ("qfq", ts_code, start_date, end_date, freq, settings.cache_signature)
    if cache_key in _tushare_df_cache:
        return _tushare_df_cache[cache_key].copy()

    importer = TushareImporter(
        token=settings.token,
        use_proxy=settings.use_proxy,
        proxy_url=settings.proxy_url,
        api_url=settings.api_url,
    )
    result = importer.mul_qfq(ts_code, start_date, end_date, freq=freq)
    if not result.empty:
        _tushare_df_cache[cache_key] = result
    return result.copy() if not result.empty else result


def tushare_mul_hfq(
    ts_code:    str,
    start_date: str,
    end_date:   str,
    freq:       str = "D",
) -> "pd.DataFrame":
    """
    乘法后复权（tushare adj_factor 方式）。

    公式：后复权价 = 原始价 × (当日 adj_factor / 最早 adj_factor)

    结果新增列：open_back / high_back / low_back / close_back

    Args / Returns / 注意 同 tushare_mul_qfq，区别仅在于复权方向。
    **请在策略 init() 中调用，不要放在 khHandlebar() 中。**
    """
    try:
        import pandas as pd
        from duckdb_storage.tushare_importer import TushareImporter
        from tushare_config import load_tushare_settings
    except ImportError as e:
        logging.warning(f"[tushare_mul_hfq] 导入失败: {e}")
        try:
            import pandas as pd
            return pd.DataFrame()
        except Exception:
            return None

    settings = load_tushare_settings()
    if not settings.token:
        logging.warning("[tushare_mul_hfq] Tushare Token 未配置，请在【软件设置 → 数据设置】中设置。")
        return pd.DataFrame()

    cache_key = ("hfq", ts_code, start_date, end_date, freq, settings.cache_signature)
    if cache_key in _tushare_df_cache:
        return _tushare_df_cache[cache_key].copy()

    importer = TushareImporter(
        token=settings.token,
        use_proxy=settings.use_proxy,
        proxy_url=settings.proxy_url,
        api_url=settings.api_url,
    )
    result = importer.mul_hfq(ts_code, start_date, end_date, freq=freq)
    if not result.empty:
        _tushare_df_cache[cache_key] = result
    return result.copy() if not result.empty else result
