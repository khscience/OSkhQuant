# coding: utf-8
"""分钟线两处回归测试（2026-10-18 开源版 2.2 发布前装机验收发现）。

1. BaoStock 分钟线前复权全历史刷新：本地从 DuckDB 读出的时间是 datetime64[us]，
   来源解析后是 datetime64[ns]，原来直接 astype("int64") 比较，同一时刻也对不上，
   刷新必然失败，整段前复权列一直为空。
2. Tushare 官方 5 分钟线每天含 09:30 集合竞价一根共 49 根，原来的完整性规则只认
   48 根，每次增量都逐日重下，又被整日覆写校验拒绝。

不访问真实行情接口，数据库使用 pytest 临时目录中的独立 DuckDB 文件。
"""
from __future__ import annotations

import types
from datetime import datetime

import pandas as pd
import pytest

from duckdb_storage.incremental import (
    RAW_COMPLETENESS_COLUMNS,
    build_incremental_plan,
    is_complete_bar_day,
    market_time_keys,
    validate_adjustment_coverage,
    validate_overwrite_frame,
)
from duckdb_storage.manager import DuckDBManager
from duckdb_storage.stock_db import StockDB


STOCK = "000001.SZ"
DAY = "2024-01-02"
DAY8 = "20240102"
NOW = datetime(2024, 1, 10, 16, 0)
FRONT_COLUMNS = ["open_front", "high_front", "low_front", "close_front"]


def _grid_5m(day=DAY, *, with_auction=False):
    """BaoStock/QMT 的 48 根 5 分钟时间点；with_auction 时在前面加 Tushare 的 09:30。"""
    times = list(pd.date_range(f"{day} 09:35", f"{day} 11:30", freq="5min"))
    times += list(pd.date_range(f"{day} 13:05", f"{day} 15:00", freq="5min"))
    if with_auction:
        times.insert(0, pd.Timestamp(f"{day} 09:30"))
    return times


def _bars(times):
    count = len(times)
    return pd.DataFrame({
        "time": pd.to_datetime(times),
        "open": [10.0] * count,
        "high": [10.2] * count,
        "low": [9.9] * count,
        "close": [10.1] * count,
        "volume": [100.0] * count,
        "amount": [1010.0] * count,
    })


class _CoverageManager:
    def __init__(self, coverage):
        self.coverage = coverage

    def get_existing_date_completeness(self, stock_code, period, **_kwargs):
        return self.coverage


@pytest.fixture()
def manager(tmp_path):
    mgr = DuckDBManager(data_root=str(tmp_path))
    try:
        yield mgr
    finally:
        mgr.close_all_no_checkpoint()


@pytest.fixture()
def stock_db(tmp_path):
    db = StockDB(STOCK, str(tmp_path))
    try:
        yield db
    finally:
        db.close(skip_checkpoint=True)


# ── 时间精度 ───────────────────────────────────────────────────────────

def test_market_time_keys_ignore_datetime_resolution():
    if int(pd.__version__.split(".")[0]) < 2:
        pytest.skip("pandas 1.x 只有纳秒精度")
    stamps = ["2025-10-20 09:35:00", "2025-10-20 15:00:00"]
    ns = pd.Series(pd.to_datetime(stamps))
    us = ns.astype("datetime64[us]")
    # 原来的比较方式：同一时刻的整数相差 1000 倍。
    assert ns.astype("int64").tolist() != us.astype("int64").tolist()
    expected = market_time_keys(ns).tolist()
    assert market_time_keys(us).tolist() == expected
    assert market_time_keys(pd.Series(stamps)).tolist() == expected
    assert market_time_keys(ns.dt.tz_localize("Asia/Shanghai")).tolist() == expected


def test_adjustment_coverage_accepts_mixed_time_resolution():
    if int(pd.__version__.split(".")[0]) < 2:
        pytest.skip("pandas 1.x 只有纳秒精度")
    times = _grid_5m()
    raw = _bars(times)
    raw["time"] = raw["time"].astype("datetime64[us]")
    adjusted = pd.DataFrame({
        "time": pd.to_datetime(times),
        **{column: [9.5] * len(times) for column in FRONT_COLUMNS},
    })
    assert validate_adjustment_coverage(raw, adjusted, ["front"], period="5m") == 48


def _front_refresh_validator():
    pytest.importorskip("PyQt5")
    from duckdb_storage.viewer import BaoStockImportThread

    return BaoStockImportThread._validate_adjustment_refresh_frame


@pytest.mark.parametrize("source_as_text", [True, False])
def test_front_refresh_validation_matches_times_read_from_duckdb(manager, source_as_text):
    times = _grid_5m()
    manager.save_kline_data(
        df=_bars(times), stock_code=STOCK, period="5m",
        dividend_type="none", skip_metadata=True,
    )
    source_times = (
        [stamp.strftime("%Y-%m-%d %H:%M:%S") for stamp in times]
        if source_as_text else list(times)
    )
    frame = pd.DataFrame({
        "time": source_times,
        **{column: [9.5] * len(times) for column in FRONT_COLUMNS},
    })
    validate = _front_refresh_validator()
    fake_thread = types.SimpleNamespace(manager=manager)
    assert validate(fake_thread, frame, STOCK, "5m", DAY, DAY, FRONT_COLUMNS) == 48


def test_front_refresh_validation_still_rejects_missing_time(manager):
    times = _grid_5m()
    manager.save_kline_data(
        df=_bars(times), stock_code=STOCK, period="5m",
        dividend_type="none", skip_metadata=True,
    )
    frame = pd.DataFrame({
        "time": [stamp.strftime("%Y-%m-%d %H:%M:%S") for stamp in times[:-1]],
        **{column: [9.5] * (len(times) - 1) for column in FRONT_COLUMNS},
    })
    validate = _front_refresh_validator()
    with pytest.raises(RuntimeError, match="未覆盖本地 1 个行情时间点"):
        validate(types.SimpleNamespace(manager=manager), frame, STOCK, "5m", DAY, DAY, FRONT_COLUMNS)


# ── Tushare 5m 的 09:30 集合竞价 K 线 ─────────────────────────────────

@pytest.mark.parametrize(
    ("period", "total", "valid", "opening", "opening_valid", "complete"),
    [
        ("5m", 48, 48, 0, 0, True),
        ("5m", 49, 49, 1, 1, True),
        ("5m", 49, 49, 0, 0, False),   # 多出来的不是 09:30
        ("5m", 49, 48, 1, 1, False),   # 有一根字段不完整
        ("5m", 49, 49, 1, 0, False),   # 09:30 那一根字段不完整
        ("5m", 50, 50, 1, 1, False),
        ("1m", 242, 242, 1, 1, False),  # 1 分钟本来就含 09:30，不额外放宽
        ("1d", 1, 1, 1, 1, True),
    ],
)
def test_is_complete_bar_day(period, total, valid, opening, opening_valid, complete):
    assert is_complete_bar_day(period, total, valid, opening, opening_valid) is complete


def test_planner_accepts_tushare_5m_day_with_auction_bar():
    manager = _CoverageManager({
        DAY8: {"total_rows": 49, "valid_rows": 49, "opening_rows": 1, "opening_valid_rows": 1},
    })
    plan = build_incremental_plan(
        manager, STOCK, "5m", DAY8, DAY8,
        required_columns=RAW_COMPLETENESS_COLUMNS, trade_days=[DAY8], now=NOW,
    )
    assert plan.complete_dates == (DAY8,)
    assert plan.anomalous_dates == ()
    assert plan.download_ranges == ()


def test_planner_still_flags_overfull_5m_day_without_auction_bar():
    manager = _CoverageManager({DAY8: {"total_rows": 49, "valid_rows": 49}})
    plan = build_incremental_plan(
        manager, STOCK, "5m", DAY8, DAY8,
        required_columns=RAW_COMPLETENESS_COLUMNS, trade_days=[DAY8], now=NOW,
    )
    assert plan.complete_dates == ()
    assert plan.anomalous_dates == (DAY8,)


def test_overwrite_validation_accepts_5m_frames_with_and_without_auction_bar():
    validate_overwrite_frame(_bars(_grid_5m(with_auction=True)), "5m", (DAY8,))
    validate_overwrite_frame(_bars(_grid_5m()), "5m", (DAY8,))


def test_overwrite_validation_rejects_49_bars_without_auction_bar():
    times = _grid_5m() + [pd.Timestamp(f"{DAY} 15:05")]
    with pytest.raises(ValueError, match="拒绝整日覆写"):
        validate_overwrite_frame(_bars(times), "5m", (DAY8,))


def test_stock_db_whole_day_overwrite_keeps_auction_bar(stock_db):
    stock_db.save_kline(
        _bars(_grid_5m(with_auction=True)), "5m",
        overwrite=True, overwrite_trade_dates=True,
    )
    saved = stock_db.get_kline("5m", fields=["time"])
    assert len(saved) == 49
    assert pd.Timestamp(f"{DAY} 09:30") in set(pd.to_datetime(saved["time"]))


def test_stock_db_whole_day_overwrite_rejects_49_bars_without_auction_bar(stock_db):
    times = _grid_5m() + [pd.Timestamp(f"{DAY} 15:05")]
    with pytest.raises(ValueError, match="拒绝整日覆写"):
        stock_db.save_kline(_bars(times), "5m", overwrite=True, overwrite_trade_dates=True)


def test_manager_reports_auction_bar_so_incremental_plan_is_up_to_date(manager):
    manager.save_kline_data(
        df=_bars(_grid_5m(with_auction=True)), stock_code=STOCK, period="5m",
        dividend_type="none", skip_metadata=True, overwrite=False, merge_missing=True,
    )
    coverage = manager.get_existing_date_completeness(
        STOCK, "5m", required_columns=list(RAW_COMPLETENESS_COLUMNS),
        raise_on_error=True, start_date=DAY8, end_date=DAY8,
    )
    assert coverage[DAY8] == {
        "total_rows": 49, "valid_rows": 49, "opening_rows": 1, "opening_valid_rows": 1,
    }
    plan = build_incremental_plan(manager, STOCK, "5m", DAY8, DAY8, trade_days=[DAY8], now=NOW)
    assert plan.complete_dates == (DAY8,)
    assert plan.download_ranges == ()


def test_manager_completeness_for_other_periods_keeps_old_keys(manager):
    manager.save_kline_data(
        df=_bars([pd.Timestamp(f"{DAY} 09:30")]), stock_code=STOCK, period="1d",
        dividend_type="none", skip_metadata=True,
    )
    coverage = manager.get_existing_date_completeness(
        STOCK, "1d", required_columns=list(RAW_COMPLETENESS_COLUMNS),
        raise_on_error=True, start_date=DAY8, end_date=DAY8,
    )
    assert coverage[DAY8] == {"total_rows": 1, "valid_rows": 1}
