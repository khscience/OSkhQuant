# coding: utf-8
"""StockDB 覆写行情时保护已有扩展列的真实 DuckDB 回归测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from duckdb_storage.stock_db import StockDB


STOCK = "000001.SZ"


def _frame(times, opens, *, front=None, turn=None):
    opens = list(opens)
    frame = pd.DataFrame(
        {
            "time": pd.to_datetime(times),
            "open": opens,
            "high": [value + 1.0 for value in opens],
            "low": [value - 1.0 for value in opens],
            "close": [value + 0.5 for value in opens],
            "volume": [1000 + index for index in range(len(opens))],
            "amount": [10000.0 + index for index in range(len(opens))],
        }
    )
    if front is not None:
        frame["open_front"] = front
        frame["high_front"] = [value + 1.0 for value in front]
        frame["low_front"] = [value - 1.0 for value in front]
        frame["close_front"] = [value + 0.5 for value in front]
    if turn is not None:
        frame["turn"] = turn
    return frame


@pytest.fixture()
def stock_db(tmp_path):
    db = StockDB(STOCK, str(tmp_path))
    try:
        yield db
    finally:
        db.close(skip_checkpoint=True)


def test_daily_raw_overwrite_preserves_adjustment_indicator_and_custom_columns(
    stock_db,
):
    original = _frame(
        ["2024-01-02 00:00:00"],
        [10.0],
        front=[8.0],
        turn=[2.5],
    )
    assert stock_db.save_kline(original, "1d", overwrite=True) == 1
    stock_db.conn.execute("ALTER TABLE kline_1d ADD COLUMN macd DOUBLE")
    stock_db.conn.execute("UPDATE kline_1d SET macd = 1.25")

    # raw 来源未返回扩展字段；即使显式携带 NaN，也不能抹掉旧复权值。
    replacement = _frame(["2024-01-02 09:30:00"], [20.0])
    replacement["open_front"] = np.nan
    assert stock_db.save_kline(replacement, "1d", overwrite=True) == 1

    result = stock_db.execute_sql(
        """
        SELECT time, open, high, close, open_front, high_front, close_front,
               turn, macd
        FROM kline_1d
        """
    )
    assert result.to_dict("records") == [
        {
            "time": pd.Timestamp("2024-01-02 09:30:00"),
            "open": 20.0,
            "high": 21.0,
            "close": 20.5,
            "open_front": 8.0,
            "high_front": 9.0,
            "close_front": 8.5,
            "turn": 2.5,
            "macd": 1.25,
        }
    ]


def test_daily_overwrite_uses_non_null_new_extension_and_date_match(stock_db):
    assert stock_db.save_kline(
        _frame(
            ["2024-01-03 00:00:00"],
            [10.0],
            front=[8.0],
            turn=[3.5],
        ),
        "1d",
        overwrite=True,
    ) == 1

    replacement = _frame(
        ["2024-01-03 09:30:00"],
        [11.0],
        front=[9.0],
        turn=[np.nan],
    )
    assert stock_db.save_kline(replacement, "1d", overwrite=True) == 1

    result = stock_db.execute_sql(
        "SELECT time, open, open_front, close_front, turn FROM kline_1d"
    ).iloc[0]
    assert result["time"] == pd.Timestamp("2024-01-03 09:30:00")
    assert result["open"] == pytest.approx(11.0)
    assert result["open_front"] == pytest.approx(9.0)
    assert result["close_front"] == pytest.approx(9.5)
    assert result["turn"] == pytest.approx(3.5)


def test_raw_overwrite_invalidates_only_requested_adjustments_atomically(stock_db):
    original = _frame(
        ["2024-01-03 09:30:00"],
        [10.0],
        front=[8.0],
    )
    original["open_back"] = [12.0]
    original["open_front_ratio"] = [0.8]
    original["open_back_ratio"] = [1.2]
    assert stock_db.save_kline(original, "1d", overwrite=True) == 1

    replacement = _frame(["2024-01-03 09:30:00"], [20.0])
    assert stock_db.save_kline(
        replacement,
        "1d",
        overwrite=True,
        invalidate_adjustment_columns=["open_front", "open_front_ratio"],
    ) == 1

    result = stock_db.execute_sql(
        """
        SELECT open, open_front, open_back, open_front_ratio, open_back_ratio
        FROM kline_1d
        """
    ).iloc[0]
    assert result["open"] == pytest.approx(20.0)
    assert pd.isna(result["open_front"])
    assert pd.isna(result["open_front_ratio"])
    assert result["open_back"] == pytest.approx(12.0)
    assert result["open_back_ratio"] == pytest.approx(1.2)


def test_raw_write_invalidates_front_history_but_back_only_on_input_rows(stock_db):
    from duckdb_storage.incremental import (
        BACK_ADJUSTMENT_COLUMNS,
        FRONT_ADJUSTMENT_COLUMNS,
    )

    original = _frame(
        [
            "2024-01-02 09:30:00",
            "2024-01-03 09:30:00",
            "2024-01-04 09:30:00",
        ],
        [10.0, 11.0, 12.0],
        front=[8.0, 9.0, 10.0],
    )
    for field in ("open", "high", "low", "close"):
        original[f"{field}_back"] = original[field] * 1.2
        original[f"{field}_front_ratio"] = [0.8, 0.8, 0.8]
        original[f"{field}_back_ratio"] = [1.2, 1.2, 1.2]
    assert stock_db.save_kline(original, "1d", overwrite=True) == 3

    replacement = _frame(["2024-01-03 09:30:00"], [50.0])
    assert stock_db.save_kline(
        replacement,
        "1d",
        overwrite=True,
        invalidate_adjustment_columns=BACK_ADJUSTMENT_COLUMNS,
        invalidate_adjustment_columns_full_history=FRONT_ADJUSTMENT_COLUMNS,
    ) == 1

    stored = stock_db.execute_sql(
        "SELECT * FROM kline_1d ORDER BY time"
    )
    assert stored[list(FRONT_ADJUSTMENT_COLUMNS)].isna().all().all()
    assert stored.loc[0, list(BACK_ADJUSTMENT_COLUMNS)].notna().all()
    assert stored.loc[1, list(BACK_ADJUSTMENT_COLUMNS)].isna().all()
    assert stored.loc[2, list(BACK_ADJUSTMENT_COLUMNS)].notna().all()
    assert stored.loc[1, "open"] == pytest.approx(50.0)


def test_volume_amount_only_fill_does_not_invalidate_adjustments(stock_db):
    from duckdb_storage.incremental import FRONT_ADJUSTMENT_COLUMNS

    original = _frame(
        ["2024-01-02 09:30:00", "2024-01-03 09:30:00"],
        [10.0, 11.0],
        front=[8.0, 9.0],
    )
    for field in ("open", "high", "low", "close"):
        original[f"{field}_front_ratio"] = [0.8, 0.8]
    assert stock_db.save_kline(original, "1d", overwrite=True) == 2
    stock_db.conn.execute(
        """
        UPDATE kline_1d SET volume = NULL, amount = NULL
        WHERE CAST(time AS DATE) = DATE '2024-01-03'
        """
    )

    fill_only = pd.DataFrame({
        "time": pd.to_datetime(["2024-01-03 09:30:00"]),
        "volume": [9999.0],
        "amount": [88888.0],
    })
    assert stock_db.save_kline(
        fill_only,
        "1d",
        overwrite=False,
        merge_missing=True,
        invalidate_adjustment_columns_full_history=FRONT_ADJUSTMENT_COLUMNS,
    ) == 1

    stored = stock_db.execute_sql("SELECT * FROM kline_1d ORDER BY time")
    assert stored.loc[1, "volume"] == pytest.approx(9999.0)
    assert stored.loc[1, "amount"] == pytest.approx(88888.0)
    assert stored["open_front"].tolist() == pytest.approx([8.0, 9.0])
    assert stored["close_front"].tolist() == pytest.approx([8.5, 9.5])


def test_volume_amount_only_new_time_invalidates_front_history(stock_db):
    from duckdb_storage.incremental import FRONT_ADJUSTMENT_COLUMNS

    original = _frame(
        ["2024-01-02 09:30:00", "2024-01-03 09:30:00"],
        [10.0, 11.0],
        front=[8.0, 9.0],
    )
    for field in ("open", "high", "low", "close"):
        original[f"{field}_front_ratio"] = [0.8, 0.8]
    assert stock_db.save_kline(original, "1d", overwrite=True) == 2

    new_time_with_trade_totals = pd.DataFrame({
        "time": pd.to_datetime(["2024-01-04 09:30:00"]),
        "volume": [9999.0],
        "amount": [88888.0],
    })
    assert stock_db.save_kline(
        new_time_with_trade_totals,
        "1d",
        overwrite=False,
        invalidate_adjustment_columns_full_history=FRONT_ADJUSTMENT_COLUMNS,
    ) == 1

    stored = stock_db.execute_sql("SELECT * FROM kline_1d ORDER BY time")
    assert len(stored) == 3
    assert stored.loc[2, "volume"] == pytest.approx(9999.0)
    assert stored.loc[2, "amount"] == pytest.approx(88888.0)
    assert stored[list(FRONT_ADJUSTMENT_COLUMNS)].isna().all().all()


def test_stale_identical_ohlc_does_not_invalidate_adjustments(stock_db):
    from duckdb_storage.incremental import (
        ADJUSTMENT_COLUMNS,
        FRONT_ADJUSTMENT_COLUMNS,
    )

    original = _frame(
        ["2024-01-02 09:30:00", "2024-01-03 09:30:00"],
        [10.0, 11.0],
        front=[8.0, 9.0],
    )
    assert stock_db.save_kline(original, "1d", overwrite=True) == 2

    stale = original.iloc[[1]][
        ["time", "open", "high", "low", "close", "volume", "amount"]
    ].copy()
    stale["volume"] = 4321.0
    stale["amount"] = 54321.0
    assert stock_db.save_kline(
        stale,
        "1d",
        overwrite=True,
        invalidate_adjustment_columns=ADJUSTMENT_COLUMNS,
        invalidate_adjustment_columns_full_history=FRONT_ADJUSTMENT_COLUMNS,
    ) == 1

    stored = stock_db.execute_sql(
        """
        SELECT time, volume, amount, open_front, close_front
        FROM kline_1d ORDER BY time
        """
    )
    assert stored.loc[1, "volume"] == pytest.approx(4321.0)
    assert stored.loc[1, "amount"] == pytest.approx(54321.0)
    assert stored["open_front"].tolist() == pytest.approx([8.0, 9.0])
    assert stored["close_front"].tolist() == pytest.approx([8.5, 9.5])


def test_minute_raw_overwrite_preserves_extensions_by_exact_timestamp(stock_db):
    original = _frame(
        ["2024-01-02 09:31:00", "2024-01-02 09:32:00"],
        [10.0, 11.0],
        front=[8.0, 9.0],
    )
    assert stock_db.save_kline(original, "1m", overwrite=True) == 2
    stock_db.conn.execute("ALTER TABLE kline_1m ADD COLUMN signal DOUBLE")
    stock_db.conn.execute(
        "UPDATE kline_1m SET signal = 7.0 WHERE time = '2024-01-02 09:31:00'"
    )

    replacement = _frame(["2024-01-02 09:31:00"], [20.0])
    assert stock_db.save_kline(replacement, "1m", overwrite=True) == 1

    result = stock_db.execute_sql(
        """
        SELECT time, open, open_front, close_front, signal
        FROM kline_1m ORDER BY time
        """
    )
    assert result[["time", "open", "open_front", "close_front"]].to_dict(
        "records"
    ) == [
        {
            "time": pd.Timestamp("2024-01-02 09:31:00"),
            "open": 20.0,
            "open_front": 8.0,
            "close_front": 8.5,
        },
        {
            "time": pd.Timestamp("2024-01-02 09:32:00"),
            "open": 11.0,
            "open_front": 9.0,
            "close_front": 9.5,
        },
    ]
    assert result.loc[0, "signal"] == pytest.approx(7.0)
    assert pd.isna(result.loc[1, "signal"])


def test_minute_anomalous_day_overwrite_removes_extra_bar_outside_source_bounds(
    stock_db,
):
    canonical_times = pd.date_range(
        "2024-01-02 09:35:00", periods=48, freq="5min",
    )
    original = _frame(
        [pd.Timestamp("2024-01-02 09:30:00"), *canonical_times],
        [9.0, *[10.0 + index for index in range(48)]],
    )
    assert stock_db.save_kline(original, "5m", overwrite=True) == 49

    repaired = _frame(
        canonical_times,
        [20.0 + index for index in range(48)],
    )
    assert stock_db.save_kline(
        repaired,
        "5m",
        overwrite=True,
        overwrite_trade_dates=True,
    ) == 48

    result = stock_db.execute_sql(
        "SELECT time, open FROM kline_5m ORDER BY time"
    )
    assert len(result) == 48
    assert result.iloc[0].to_dict() == {
        "time": pd.Timestamp("2024-01-02 09:35:00"), "open": 20.0,
    }
    assert pd.Timestamp("2024-01-02 09:30:00") not in set(result["time"])


def test_incomplete_minute_day_is_rejected_before_destructive_overwrite(stock_db):
    complete_times = pd.date_range(
        "2024-01-02 09:35:00", periods=48, freq="5min",
    )
    original = _frame(complete_times, [10.0 + index for index in range(48)])
    assert stock_db.save_kline(original, "5m", overwrite=True) == 48

    truncated = _frame(complete_times[:20], [50.0 + index for index in range(20)])
    with pytest.raises(ValueError, match="拒绝整日覆写"):
        stock_db.save_kline(
            truncated,
            "5m",
            overwrite=True,
            overwrite_trade_dates=True,
        )

    stored = stock_db.execute_sql(
        "SELECT time, open FROM kline_5m ORDER BY time"
    )
    assert len(stored) == 48
    assert stored.iloc[0]["open"] == pytest.approx(10.0)


def test_legacy_daily_normalization_merges_0000_and_0930_without_data_loss(
    stock_db,
):
    assert stock_db.save_kline(
        _frame(["2024-01-04 09:30:00"], [12.0]),
        "1d",
        overwrite=True,
    ) == 1
    stock_db.conn.execute("ALTER TABLE kline_1d ADD COLUMN custom_rank DOUBLE")
    stock_db.conn.execute(
        """
        INSERT INTO kline_1d (
            time, open, high, low, close, volume, amount,
            open_front, high_front, low_front, close_front, turn, custom_rank
        ) VALUES (
            '2024-01-04 00:00:00', 10, 11, 9, 10.5, 1000, 10000,
            8, 9, 7, 8.5, 2.25, 6
        )
        """
    )

    assert stock_db.normalize_daily_timestamps() == 1

    result = stock_db.execute_sql(
        """
        SELECT time, open, open_front, close_front, turn, custom_rank
        FROM kline_1d
        """
    )
    assert result.to_dict("records") == [
        {
            "time": pd.Timestamp("2024-01-04 09:30:00"),
            # 09:30 行是较新的 raw 记录；00:00 行提供其缺失的扩展值。
            "open": 12.0,
            "open_front": 8.0,
            "close_front": 8.5,
            "turn": 2.25,
            "custom_rank": 6.0,
        }
    ]


def test_overwrite_failure_rolls_back_raw_and_full_history_invalidation(tmp_path):
    db = StockDB(STOCK, str(tmp_path))
    try:
        # 手工建立带约束的旧 schema，第二次插入用它制造发生在 UPDATE 后的
        # 事务失败，验证匹配旧行不会停留在“已更新但新行未写入”的中间态。
        _ = db.conn
        db.conn.execute(
            """
            CREATE TABLE kline_1m (
                time TIMESTAMP PRIMARY KEY,
                open DOUBLE CHECK (open < 100),
                open_front DOUBLE,
                custom_signal DOUBLE,
                update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.conn.execute(
            """
            INSERT INTO kline_1m (time, open, open_front, custom_signal) VALUES
                ('2024-01-02 09:30:00', 9, 7, 4),
                ('2024-01-02 09:31:00', 10, 8, 5)
            """
        )
        # 这是人为构造的 legacy 表；让保存路径按“已存在数据库”执行完整
        # schema 探测，而不是走新文件的结构缓存捷径。
        db._is_new_db = False

        incoming = _frame(
            ["2024-01-02 09:31:00", "2024-01-02 09:32:00"],
            [20.0, 150.0],
        )
        with pytest.raises(Exception, match="CHECK constraint failed"):
            db.save_kline(
                incoming,
                "1m",
                overwrite=True,
                invalidate_adjustment_columns_full_history=["open_front"],
            )

        result = db.execute_sql(
            """
            SELECT time, open, open_front, custom_signal
            FROM kline_1m ORDER BY time
            """
        )
        assert result.to_dict("records") == [
            {
                "time": pd.Timestamp("2024-01-02 09:30:00"),
                "open": 9.0,
                "open_front": 7.0,
                "custom_signal": 4.0,
            },
            {
                "time": pd.Timestamp("2024-01-02 09:31:00"),
                "open": 10.0,
                "open_front": 8.0,
                "custom_signal": 5.0,
            }
        ]
    finally:
        db.close(skip_checkpoint=True)


def test_save_kline_keeps_legacy_positional_transaction_arguments(stock_db):
    """新增增量参数不能改变旧扩展使用的第 5/6 个位置参数语义。"""

    incoming = _frame(["2024-01-02 09:30:00"], [10.0])
    stock_db._ensure_period_table("1d")
    stock_db.conn.execute("BEGIN TRANSACTION")
    try:
        # 旧签名：overwrite=True, manage_transaction=False,
        # skip_unchanged=False。若参数顺序被新选项插入，这里会自行 COMMIT。
        assert stock_db.save_kline(
            incoming,
            "1d",
            "none",
            True,
            False,
            False,
        ) == 1
        stock_db.conn.execute("ROLLBACK")
    except Exception:
        try:
            stock_db.conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    remaining = stock_db.execute_sql("SELECT COUNT(*) AS count FROM kline_1d")
    assert int(remaining.iloc[0]["count"]) == 0
