# coding: utf-8
"""#18 分段(动态)加载的时间轴必须是全部股票实际时间点的并集。

旧实现只用首只带 time 的股票生成时间轴，后续分段用该股首日日内偏移 × 交易日历推算：
其他股票的有效行情被漏掉、股票池顺序会改变结果、首股只有预热数据时时间轴为空、
tick 推算基本错误。全量模式(khFrame)取所有股票时间的并集，分段模式必须与之一致。

使用内存替身数据源，不读写真实行情库。
"""
from datetime import datetime

import pandas as pd
import pytest


khDynamicLoader = None


@pytest.fixture(autouse=True)
def _import_target_module():
    """在 conftest 的外部依赖 mock 生效后再导入被测模块，避免改变其他测试的导入顺序。"""
    global khDynamicLoader
    import khDynamicLoader as _module
    khDynamicLoader = _module
    yield


def _ts(day, hhmm):
    return int(datetime.strptime(f"{day} {hhmm}", "%Y%m%d %H:%M").timestamp())


# 预热日 20250303；回测区间 20250304 ~ 20250307（周二~周五）
# A：只有预热日数据，20250304 停牌，之后每天根数不同
# B：每天根数不同，20250305 停牌
_ROWS = {
    "000001.SZ": [
        ("20250303", "09:31"), ("20250303", "09:32"),
        ("20250305", "09:31"), ("20250305", "09:32"),
        ("20250307", "09:31"),
    ],
    "600000.SH": [
        ("20250303", "09:31"),
        ("20250304", "09:31"), ("20250304", "09:32"), ("20250304", "09:33"),
        ("20250306", "09:31"), ("20250306", "13:01"),
        ("20250307", "09:31"), ("20250307", "14:57"),
    ],
}
_STOCK_TIMES = {code: [_ts(d, t) for d, t in rows] for code, rows in _ROWS.items()}
_EXPECTED = sorted({
    _ts(d, t) for rows in _ROWS.values() for d, t in rows if "20250304" <= d <= "20250307"
})


class _MemoryManager:
    """按 [start_time, end_time] 日期过滤返回内存数据，并记录每次请求的字段。"""

    data_source = "duckdb"

    def __init__(self, stock_times, as_datetime=False):
        self.stock_times = stock_times
        self.as_datetime = as_datetime
        self.calls = []

    def _read(self, **kwargs):
        self.calls.append(dict(kwargs))
        start, end = str(kwargs["start_time"]), str(kwargs["end_time"])
        fields = kwargs["field_list"]
        result = {}
        for code in kwargs["stock_list"]:
            times = [
                t for t in self.stock_times.get(code, [])
                if start <= datetime.fromtimestamp(t).strftime("%Y%m%d") <= end
            ]
            if self.as_datetime:
                col = pd.to_datetime([datetime.fromtimestamp(t) for t in times])
            else:
                col = times
            frame = {"time": col}
            if "close" in fields:
                frame["close"] = [10.0] * len(times)
            result[code] = pd.DataFrame(frame)
        return result

    def get_market_data_ex(self, **kwargs):
        return self._read(**kwargs)

    def get_market_data_ex_framework_raw(self, **kwargs):
        return self._read(**kwargs)


def _config():
    return type("Cfg", (), {"config_dict": {"performance": {
        "duckdb_load_batch_size": 1,
        "framework_raw_duckdb_load": True,
        "framework_history_preload_days": 0,
    }}})()


def _loader(monkeypatch, stocks, chunk_size, manager, period="1m"):
    monkeypatch.setattr(khDynamicLoader, "get_data_source_manager", lambda: manager)
    monkeypatch.setattr(khDynamicLoader, "xtdata", None, raising=False)
    return khDynamicLoader.DynamicDataLoader(
        config=_config(),
        stock_codes=list(stocks),
        data_period=period,
        start_date="20250304",
        end_date="20250307",
        load_start_date="20250303",
        enabled=True,
        chunk_size=chunk_size,
        field_list=["time", "close"],
        dividend_type="none",
        tools=None,  # 非周末即交易日
    )


ORDERS = [("000001.SZ", "600000.SH"), ("600000.SH", "000001.SZ")]


@pytest.mark.parametrize("chunk_size", [1, 2, 10])
@pytest.mark.parametrize("row_budget", [1, 5_000_000])
def test_time_axis_is_union_and_order_independent(monkeypatch, chunk_size, row_budget):
    monkeypatch.setattr(khDynamicLoader.DynamicDataLoader, "_TIME_SCAN_ROW_BUDGET", row_budget)
    results = []
    for order in ORDERS:
        loader = _loader(monkeypatch, order, chunk_size, _MemoryManager(_STOCK_TIMES))
        results.append(loader.get_all_times())
    assert results[0] == results[1] == _EXPECTED


def test_first_stock_only_warmup_single_segment_not_empty(monkeypatch):
    # 首股(000001.SZ)在首段内只有预热数据：旧实现单分段时直接返回空时间轴
    loader = _loader(monkeypatch, ORDERS[0], 10, _MemoryManager(_STOCK_TIMES))
    assert len(loader.time_segments) == 1
    times = loader.get_all_times()
    assert times == _EXPECTED
    assert times


def test_later_segments_read_time_column_only_and_are_not_cached(monkeypatch):
    manager = _MemoryManager(_STOCK_TIMES)
    loader = _loader(monkeypatch, ORDERS[0], 1, manager)
    loader.get_all_times()

    first_calls = [c for c in manager.calls if c["end_time"] == "20250304"]
    assert first_calls and all("close" in c["field_list"] for c in first_calls)  # 首段正式加载(主循环会复用)
    later = [c for c in manager.calls if c["end_time"] != "20250304"]
    assert later, "后续分段必须基于实际数据，而非推算"
    assert all(call["field_list"] == ["time"] for call in later)
    assert all(call["dividend_type"] == "none" for call in later)
    # 只保留首段，时间扫描的数据不进入分段缓存
    assert list(loader.loaded_chunks) == [0]
    # 正式字段列表未被时间扫描改动
    assert loader.field_list == ["time", "close"]


def test_matches_full_mode_union(monkeypatch):
    """与全量模式(所有股票时间并集后裁剪到回测区间)一致。"""
    manager = _MemoryManager(_STOCK_TIMES)
    loader = _loader(monkeypatch, ORDERS[1], 1, manager)
    full = manager._read(field_list=["time", "close"], stock_list=list(_STOCK_TIMES),
                         start_time="20250303", end_time="20250307")
    union = sorted({t for df in full.values() for t in df["time"].tolist()})
    full_mode = [t for t in union if "20250304" <= datetime.fromtimestamp(t).strftime("%Y%m%d") <= "20250307"]
    assert loader.get_all_times() == full_mode


@pytest.mark.parametrize("chunk_size", [1, 3])
def test_tick_uses_actual_times_not_inferred(monkeypatch, chunk_size):
    # tick：不同股票时间戳互不对齐，每天笔数不同；必须基于实际数据并统一转毫秒
    tick_rows = {
        "000001.SZ": [("20250303", "10:00:03")] + [("20250305", "10:00:06"), ("20250307", "10:01:09")],
        "600000.SH": [("20250304", "10:00:00"), ("20250304", "10:00:03"), ("20250306", "10:00:09")],
    }

    def _tsec(day, hms):
        return int(datetime.strptime(f"{day} {hms}", "%Y%m%d %H:%M:%S").timestamp())

    stock_times = {code: [_tsec(d, t) for d, t in rows] for code, rows in tick_rows.items()}
    expected = sorted({
        int(pd.Timestamp(datetime.fromtimestamp(_tsec(d, t))).tz_localize("Asia/Shanghai").value // 1_000_000)
        for rows in tick_rows.values() for d, t in rows if "20250304" <= d <= "20250307"
    })
    results = []
    for order in ORDERS:
        loader = _loader(monkeypatch, order, chunk_size,
                         _MemoryManager(stock_times, as_datetime=True), period="tick")
        results.append(loader.get_all_times())
    assert results[0] == results[1] == expected
