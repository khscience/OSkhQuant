# coding: utf-8
"""指定 K 线派生列安全置空的真实 DuckDB 回归测试。"""
from __future__ import annotations

from contextlib import contextmanager

import pandas as pd
import pytest

from duckdb_storage.manager import DuckDBManager
from duckdb_storage.stock_db import StockDB


STOCK = "000001.SZ"


def _frame(times):
    count = len(times)
    values = [10.0 + index for index in range(count)]
    return pd.DataFrame(
        {
            "time": pd.to_datetime(times),
            "open": values,
            "high": [value + 1.0 for value in values],
            "low": [value - 1.0 for value in values],
            "close": [value + 0.5 for value in values],
            "volume": [1000 + index for index in range(count)],
            "amount": [10000.0 + index for index in range(count)],
            "open_front": [value - 2.0 for value in values],
            "high_front": [value - 1.0 for value in values],
            "low_front": [value - 3.0 for value in values],
            "close_front": [value - 1.5 for value in values],
            "open_back": [value * 2.0 for value in values],
            "close_back": [(value + 0.5) * 2.0 for value in values],
        }
    )


@pytest.fixture()
def stock_db(tmp_path):
    db = StockDB(STOCK, str(tmp_path))
    try:
        yield db
    finally:
        db.close(skip_checkpoint=True)


def test_clear_kline_columns_full_only_clears_selected_derived_fields(stock_db):
    assert stock_db.save_kline(
        _frame(["2024-01-02 09:30:00", "2024-01-03 09:30:00"]),
        "1d",
        overwrite=True,
    ) == 2

    assert stock_db.clear_kline_columns(
        "1d", ["open_front", "close_front", "open_front"]
    ) == 2

    rows = stock_db.execute_sql(
        """
        SELECT open, close, open_front, high_front, close_front,
               open_back, close_back
        FROM kline_1d ORDER BY time
        """
    )
    assert rows["open_front"].isna().all()
    assert rows["close_front"].isna().all()
    assert rows["high_front"].notna().all()
    assert rows["open_back"].notna().all()
    assert rows["close_back"].notna().all()
    assert rows["open"].tolist() == [10.0, 11.0]
    assert rows["close"].tolist() == [10.5, 11.5]

    # 已经为空的行不会被重复计为受影响。
    assert stock_db.clear_kline_columns(
        "1d", ["open_front", "close_front"]
    ) == 0


def test_clear_daily_range_matches_trade_date_not_timestamp(stock_db):
    assert stock_db.save_kline(
        _frame(
            [
                "2024-01-02 09:30:00",
                "2024-01-03 09:30:00",
                "2024-01-04 09:30:00",
            ]
        ),
        "1d",
        overwrite=True,
    ) == 3

    # 边界时间不等于 09:30，但日线必须按交易日期匹配整日。
    assert stock_db.clear_kline_columns(
        "1d",
        ["open_front"],
        start_time="2024-01-03 23:00:00",
        end_time="2024-01-03 23:00:00",
    ) == 1

    rows = stock_db.execute_sql(
        "SELECT time, open_front, high_front FROM kline_1d ORDER BY time"
    )
    assert rows["open_front"].notna().tolist() == [True, False, True]
    assert rows["high_front"].notna().all()


def test_clear_minute_range_uses_exact_time_boundaries(stock_db):
    assert stock_db.save_kline(
        _frame(
            [
                "2024-01-02 09:31:00",
                "2024-01-02 09:32:00",
                "2024-01-02 09:33:00",
            ]
        ),
        "1m",
        overwrite=True,
    ) == 3

    assert stock_db.clear_kline_columns(
        "1m",
        ["open_front"],
        start_time="2024-01-02 09:31:30",
        end_time="2024-01-02 09:32:30",
    ) == 1

    rows = stock_db.execute_sql(
        "SELECT time, open_front, close_front FROM kline_1m ORDER BY time"
    )
    assert rows["open_front"].notna().tolist() == [True, False, True]
    assert rows["close_front"].notna().all()


@pytest.mark.parametrize(
    "column",
    [
        "time",
        "update_time",
        "close",
        "dividend_type",
        "unknown_column",
        'open_front" = NULL; DELETE FROM kline_1d; --',
    ],
)
def test_clear_kline_columns_rejects_unsafe_or_non_whitelisted_fields(
    stock_db, column
):
    assert stock_db.save_kline(
        _frame(["2024-01-02 09:30:00"]), "1d", overwrite=True
    ) == 1

    with pytest.raises(ValueError):
        stock_db.clear_kline_columns("1d", [column])

    remaining = stock_db.execute_sql(
        "SELECT COUNT(*) AS count, close, open_front FROM kline_1d GROUP BY close, open_front"
    )
    assert remaining.iloc[0].to_dict() == {
        "count": 1,
        "close": 10.5,
        "open_front": 8.0,
    }


def test_clear_kline_columns_rejects_tick_and_missing_actual_column(tmp_path):
    db = StockDB(STOCK, str(tmp_path))
    try:
        with pytest.raises(ValueError, match="tick"):
            db.clear_kline_columns("tick", ["open_front"])

        _ = db.conn
        db.conn.execute(
            """
            CREATE TABLE kline_1m (
                time TIMESTAMP PRIMARY KEY,
                open DOUBLE,
                update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        with pytest.raises(ValueError, match="不存在字段"):
            db.clear_kline_columns("1m", ["open_front"])
    finally:
        db.close(skip_checkpoint=True)


def test_clear_kline_columns_rolls_back_if_commit_fails(stock_db):
    assert stock_db.save_kline(
        _frame(["2024-01-02 09:30:00", "2024-01-03 09:30:00"]),
        "1d",
        overwrite=True,
    ) == 2
    real_connection = stock_db.conn

    class FailBeforeCommit:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, params=None):
            if str(sql).strip().upper() == "COMMIT":
                raise RuntimeError("injected commit failure")
            if params is None:
                return self.connection.execute(sql)
            return self.connection.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    stock_db._conn = FailBeforeCommit(real_connection)
    try:
        with pytest.raises(RuntimeError, match="injected commit failure"):
            stock_db.clear_kline_columns("1d", ["open_front", "close_front"])
    finally:
        stock_db._conn = real_connection

    rows = real_connection.execute(
        "SELECT open_front, close_front FROM kline_1d ORDER BY time"
    ).fetchdf()
    assert rows["open_front"].notna().all()
    assert rows["close_front"].notna().all()


def test_manager_clear_kline_columns_holds_stock_write_lock():
    events = []

    class FakeStockDB:
        def clear_kline_columns(
            self, period, columns, start_time=None, end_time=None
        ):
            events.append(
                ("clear", period, tuple(columns), start_time, end_time)
            )
            return 3

    @contextmanager
    def stock_lock():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    manager = DuckDBManager.__new__(DuckDBManager)
    manager.read_only = False
    manager.data_root = "test"
    manager._get_stock_write_lock = lambda _stock: stock_lock()
    manager.get_stock_db = lambda _stock: FakeStockDB()

    assert manager.clear_kline_columns(
        STOCK,
        "1d",
        ["open_front"],
        start_time="20240101",
        end_time="20240131",
    ) == 3
    assert events == [
        "enter",
        ("clear", "1d", ("open_front",), "20240101", "20240131"),
        "exit",
    ]


def test_completeness_treats_nonfinite_adjustment_as_partial(tmp_path):
    manager = DuckDBManager(str(tmp_path))
    try:
        frame = _frame(["2024-01-02 09:30:00"])
        frame["open_front"] = float("inf")
        assert manager.save_kline_data(frame, STOCK, "1d") == 1

        coverage = manager.get_existing_date_completeness(
            STOCK,
            "1d",
            required_columns=[
                "open_front", "high_front", "low_front", "close_front",
            ],
            raise_on_error=True,
            start_date="20240102",
            end_date="20240102",
        )

        assert coverage["20240102"] == {"total_rows": 1, "valid_rows": 0}
    finally:
        manager.close_all_no_checkpoint()
