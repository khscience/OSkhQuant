# coding: utf-8
"""共享增量规划与 DuckDB 日线合并行为的回归测试。

这些测试不访问真实行情接口，也不依赖本机交易日历；增量规划器的交易日
由测试显式注入，数据库行为使用 pytest 临时目录中的独立 DuckDB 文件。
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from duckdb_storage.incremental import (
    EXPECTED_BARS_PER_DAY,
    RAW_COMPLETENESS_COLUMNS,
    RAW_PRICE_COLUMNS,
    TICK_REQUIRED_COLUMNS,
    build_incremental_plan,
    validate_overwrite_frame,
)
from duckdb_storage.stock_db import StockDB


STOCK = "000001.SZ"


class _CoverageManager:
    def __init__(self, coverage=None):
        self.coverage = coverage or {}
        self.calls = []

    def get_existing_date_completeness(
        self,
        stock_code,
        period,
        *,
        required_columns,
        raise_on_error,
        start_date,
        end_date,
    ):
        self.calls.append(
            {
                "stock_code": stock_code,
                "period": period,
                "required_columns": tuple(required_columns),
                "raise_on_error": raise_on_error,
                "start_date": start_date,
                "end_date": end_date,
            }
        )
        return self.coverage


@pytest.mark.parametrize(
    ("period", "expected_rows"),
    [("1d", 1), ("1m", 241), ("5m", 48)],
)
def test_exact_bar_count_marks_day_complete(period, expected_rows):
    manager = _CoverageManager(
        {"20240102": {"total_rows": expected_rows, "valid_rows": expected_rows}}
    )

    plan = build_incremental_plan(
        manager,
        STOCK,
        period,
        "20240102",
        "20240102",
        required_columns=RAW_COMPLETENESS_COLUMNS,
        trade_days=["20240102"],
        now=datetime(2024, 1, 10, 16, 0),
    )

    assert plan.complete_dates == ("20240102",)
    assert plan.missing_dates == ()
    assert plan.partial_dates == ()
    assert plan.anomalous_dates == ()
    assert plan.download_ranges == ()
    assert manager.calls == [
        {
            "stock_code": STOCK,
            "period": period,
            "required_columns": RAW_COMPLETENESS_COLUMNS,
            "raise_on_error": True,
            "start_date": "20240102",
            "end_date": "20240102",
        }
    ]


def test_missing_friday_and_monday_are_one_trade_day_range():
    manager = _CoverageManager()

    plan = build_incremental_plan(
        manager,
        STOCK,
        "1d",
        "20240105",
        "20240108",
        trade_days=["20240105", "20240108"],
        now=datetime(2024, 1, 10, 16, 0),
    )

    assert plan.expected_dates == ("20240105", "20240108")
    assert plan.missing_dates == ("20240105", "20240108")
    assert plan.download_ranges == (("20240105", "20240108"),)


def test_unclosed_current_trade_day_is_not_planned():
    manager = _CoverageManager(
        {"20240105": {"total_rows": 1, "valid_rows": 1}}
    )

    plan = build_incremental_plan(
        manager,
        STOCK,
        "1d",
        "20240105",
        "20240108",
        trade_days=["20240105", "20240108"],
        now=datetime(2024, 1, 8, 14, 30),
    )

    assert plan.expected_dates == ("20240105",)
    assert plan.ignored_open_dates == ("20240108",)
    assert plan.download_ranges == ()


def test_calendar_failure_is_not_misreported_as_up_to_date(monkeypatch):
    import khQTTools

    manager = _CoverageManager()
    monkeypatch.setattr(khQTTools, "get_trade_days_set", lambda *_args: set())

    def _raise_calendar_error(*_args):
        raise RuntimeError("calendar cache is unreadable")

    monkeypatch.setattr(
        khQTTools, "get_trade_days_set_checked", _raise_calendar_error,
    )

    with pytest.raises(RuntimeError, match="calendar cache is unreadable"):
        build_incremental_plan(
            manager,
            STOCK,
            "1d",
            "20240102",
            "20240103",
            now=datetime(2024, 1, 10, 16, 0),
        )
    assert manager.calls == []


def test_verified_zero_trade_day_range_remains_a_valid_empty_plan(monkeypatch):
    import khQTTools

    manager = _CoverageManager()
    monkeypatch.setattr(khQTTools, "get_trade_days_set", lambda *_args: set())
    monkeypatch.setattr(
        khQTTools, "get_trade_days_set_checked", lambda *_args: set(),
    )

    plan = build_incremental_plan(
        manager,
        STOCK,
        "1d",
        "20240106",
        "20240107",
        now=datetime(2024, 1, 10, 16, 0),
    )

    assert plan.expected_dates == ()
    assert plan.download_ranges == ()
    assert len(manager.calls) == 1


def test_partial_and_overfull_days_are_replanned_and_reported():
    expected_rows = EXPECTED_BARS_PER_DAY["1m"]
    manager = _CoverageManager(
        {
            "20240103": {
                "total_rows": expected_rows - 1,
                "valid_rows": expected_rows - 1,
            },
            "20240104": {
                "total_rows": expected_rows + 1,
                "valid_rows": expected_rows + 1,
            },
            # 行数完整但所需行情字段有空值，也必须视为部分日。
            "20240105": {
                "total_rows": expected_rows,
                "valid_rows": expected_rows - 1,
            },
        }
    )

    plan = build_incremental_plan(
        manager,
        STOCK,
        "1m",
        "20240102",
        "20240105",
        trade_days=["20240102", "20240103", "20240104", "20240105"],
        now=datetime(2024, 1, 10, 16, 0),
    )

    assert plan.missing_dates == ("20240102",)
    assert plan.partial_dates == ("20240103", "20240104", "20240105")
    assert plan.anomalous_dates == ("20240104",)
    assert plan.unresolved_dates == (
        "20240102",
        "20240103",
        "20240104",
        "20240105",
    )
    assert plan.download_ranges == (
        ("2024-01-02 09:00:00", "2024-01-05 15:00:00"),
    )


def test_tick_uses_native_fields_and_minimum_threshold():
    """tick 不能复用 K 线 close/open 字段，也不能要求固定 4700 根。"""

    manager = _CoverageManager(
        {"20240108": {"total_rows": 5, "valid_rows": 5}}
    )
    plan = build_incremental_plan(
        manager,
        STOCK,
        "tick",
        "20240108",
        "20240108",
        # 模拟一只证券的较小测试阈值；生产默认仍为 4465。
        tick_min_rows=5,
        required_columns=RAW_COMPLETENESS_COLUMNS,
        trade_days=["20240108"],
        now=datetime(2024, 1, 10, 16, 0),
    )

    assert plan.complete_dates == ("20240108",)
    assert plan.download_ranges == ()
    assert manager.calls[0]["required_columns"] == TICK_REQUIRED_COLUMNS


def test_tick_partial_and_out_of_retention_dates_are_planned_separately():
    manager = _CoverageManager(
        {
            "20240108": {"total_rows": 4, "valid_rows": 3},
            "20240109": {"total_rows": 5, "valid_rows": 5},
        }
    )
    plan = build_incremental_plan(
        manager,
        STOCK,
        "tick",
        "20231201",
        "20240110",
        tick_min_rows=5,
        tick_max_age_days=31,
        trade_days=["20231201", "20240108", "20240109", "20240110"],
        now=datetime(2024, 1, 10, 16, 0),
    )

    # 2023-12-01 超过 31 天，不得形成永远失败的下载任务。
    assert plan.expected_dates == ("20240108", "20240109", "20240110")
    assert plan.ignored_retention_dates == ("20231201",)
    assert plan.complete_dates == ("20240109",)
    assert plan.partial_dates == ("20240108",)
    assert plan.missing_dates == ("20240110",)
    assert plan.download_ranges == (
        ("2024-01-08 09:00:00", "2024-01-08 15:00:00"),
        ("2024-01-10 09:00:00", "2024-01-10 15:00:00"),
    )


def test_tick_default_retention_does_not_drop_requested_history():
    plan = build_incremental_plan(
        _CoverageManager(), STOCK, "tick", "20231201", "20240110",
        tick_min_rows=5, trade_days=["20231201", "20240110"],
        now=datetime(2024, 1, 10, 16, 0),
    )
    assert plan.expected_dates == ("20231201", "20240110")
    assert plan.ignored_retention_dates == ()
    assert plan.missing_dates == ("20231201", "20240110")


def test_tick_range_is_split_at_configured_natural_day_limit():
    manager = _CoverageManager()
    plan = build_incremental_plan(
        manager,
        STOCK,
        "tick",
        "20240101",
        "20240110",
        tick_min_rows=1,
        tick_max_span_days=3,
        trade_days=["20240101", "20240102", "20240103", "20240104", "20240105"],
        now=datetime(2024, 1, 10, 16, 0),
    )

    assert plan.download_ranges == (
        ("2024-01-01 09:00:00", "2024-01-03 15:00:00"),
        ("2024-01-04 09:00:00", "2024-01-05 15:00:00"),
    )


def test_validate_overwrite_frame_accepts_tick_above_threshold_and_rejects_partial():
    frame = pd.DataFrame(
        {
            "time": pd.to_datetime(
                ["2024-01-08 09:30:00", "2024-01-08 09:31:00"]
            ),
            "lastPrice": [10.0, 10.1],
            "volume": [100, 120],
            "amount": [1000.0, 1212.0],
        }
    )
    validate_overwrite_frame(
        frame,
        "tick",
        ("20240108",),
        tick_min_rows=2,
    )

    with pytest.raises(ValueError, match="根数/字段不完整"):
        validate_overwrite_frame(
            frame.iloc[:1],
            "tick",
            ("20240108",),
            tick_min_rows=2,
        )


def test_validate_overwrite_frame_rejects_missing_expected_trade_day():
    frame = pd.DataFrame(
        {
            "time": pd.to_datetime(["2024-01-02 09:30:00"]),
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "volume": [1000.0],
            "amount": [10000.0],
        }
    )

    with pytest.raises(ValueError, match="缺少 1 个交易日"):
        validate_overwrite_frame(
            frame,
            "1d",
            ("20240102", "20240103"),
        )


def test_validate_overwrite_frame_accepts_xtdata_milliseconds_in_beijing_date():
    frame = pd.DataFrame(
        {
            # 2026-08-11 16:00 UTC，即北京时间 2026-08-12 00:00。
            "time": [1786464000000],
            "open": [11.26],
            "high": [11.32],
            "low": [11.20],
            "close": [11.25],
            "volume": [1000.0],
            "amount": [11250.0],
        }
    )

    validate_overwrite_frame(frame, "1d", ("20260812",))


@pytest.mark.parametrize("invalid_column", ["volume", "amount"])
def test_validate_overwrite_frame_rejects_null_raw_field(invalid_column):
    frame = pd.DataFrame(
        {
            "time": pd.to_datetime(["2024-01-02 09:30:00"]),
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "volume": [1000.0],
            "amount": [10000.0],
        }
    )
    frame.loc[0, invalid_column] = None

    with pytest.raises(ValueError, match="根数/字段不完整"):
        validate_overwrite_frame(frame, "1d", ("20240102",))


@pytest.mark.parametrize("invalid_value", [float("inf"), float("-inf")])
def test_validate_overwrite_frame_rejects_nonfinite_required_field(invalid_value):
    frame = pd.DataFrame({
        "time": pd.to_datetime(["2024-01-02 09:30:00"]),
        "open": [invalid_value],
        "high": [10.5],
        "low": [9.8],
        "close": [10.2],
        "volume": [1000.0],
        "amount": [10000.0],
    })

    with pytest.raises(ValueError, match="拒绝整日覆写"):
        validate_overwrite_frame(frame, "1d", ("20240102",))


def _daily_frame(time_value, *, open_value=10.0, open_front=None):
    data = {
        "time": [pd.Timestamp(time_value)],
        "open": [open_value],
        "high": [open_value + 1.0],
        "low": [open_value - 1.0],
        "close": [open_value + 0.5],
        "volume": [1000.0],
        "amount": [10000.0],
    }
    if open_front is not None:
        data.update(
            {
                "open_front": [open_front],
                "high_front": [open_front + 1.0],
                "low_front": [open_front - 1.0],
                "close_front": [open_front + 0.5],
            }
        )
    return pd.DataFrame(data)


@pytest.fixture()
def stock_db(tmp_path):
    db = StockDB(STOCK, str(tmp_path))
    try:
        yield db
    finally:
        db.close(skip_checkpoint=True)


def test_daily_merge_matches_calendar_date_and_only_fills_missing_fields(stock_db):
    assert stock_db.save_kline(
        _daily_frame("2024-01-02 00:00:00", open_value=10.0),
        "1d",
        overwrite=True,
    ) == 1

    assert stock_db.save_kline(
        _daily_frame(
            "2024-01-02 09:30:00",
            open_value=99.0,
            open_front=8.0,
        ),
        "1d",
        overwrite=False,
        merge_missing=True,
    ) == 1

    stored = stock_db.execute_sql(
        "SELECT time, open, open_front, close_front FROM kline_1d ORDER BY time"
    )
    assert stored.to_dict("records") == [
        {
            "time": pd.Timestamp("2024-01-02 09:30:00"),
            "open": 10.0,
            "open_front": 8.0,
            "close_front": 8.5,
        }
    ]


def test_daily_force_overwrite_removes_old_time_on_same_calendar_date(stock_db):
    assert stock_db.save_kline(
        _daily_frame("2024-01-02 00:00:00", open_value=10.0),
        "1d",
        overwrite=True,
    ) == 1

    assert stock_db.save_kline(
        _daily_frame("2024-01-02 09:30:00", open_value=20.0),
        "1d",
        overwrite=True,
    ) == 1

    stored = stock_db.execute_sql(
        "SELECT time, open FROM kline_1d ORDER BY time"
    )
    assert stored.to_dict("records") == [
        {
            "time": pd.Timestamp("2024-01-02 09:30:00"),
            "open": 20.0,
        }
    ]


def test_daily_indicator_update_matches_date_across_different_times(stock_db):
    assert stock_db.save_kline(
        _daily_frame("2024-01-02 09:30:00"),
        "1d",
        overwrite=True,
    ) == 1

    updates = pd.DataFrame(
        {
            "time": [pd.Timestamp("2024-01-02 00:00:00")],
            "turn": [3.25],
            "pctChg": [1.5],
        }
    )
    assert stock_db.update_daily_indicators(updates) == 1

    stored = stock_db.execute_sql(
        "SELECT time, turn, pctChg FROM kline_1d ORDER BY time"
    )
    assert stored.to_dict("records") == [
        {
            "time": pd.Timestamp("2024-01-02 09:30:00"),
            "turn": 3.25,
            "pctChg": 1.5,
        }
    ]
