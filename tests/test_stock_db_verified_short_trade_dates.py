# coding: utf-8
"""DuckDB 层整日覆写：合法缺根交易日（停牌）凭调用方证据按证据根数精确核对。

默认仍要求满根（5m 48 根）；只有 ``verified_short_trade_dates`` 列出的交易日
按证据中的根数核对，根数对不上照样拒绝，原数据不动。
"""

import pandas as pd
import pytest

from duckdb_storage.stock_db import StockDB

CODE = "513100.SH"


def _five_minute_times(day):
    morning = pd.date_range(f"{day} 09:35:00", f"{day} 11:30:00", freq="5min")
    afternoon = pd.date_range(f"{day} 13:05:00", f"{day} 15:00:00", freq="5min")
    times = morning.append(afternoon)
    assert len(times) == 48
    return times


def _frame(times, volume_per_bar=100.0):
    count = len(times)
    return pd.DataFrame({
        "time": times,
        "open": [10.0] * count, "high": [10.5] * count,
        "low": [9.5] * count, "close": [10.2] * count,
        "volume": [volume_per_bar] * count,
        "amount": [volume_per_bar * 10.0] * count,
    })


def _rows_on(db, day):
    stored = db.get_kline("5m")
    stamps = pd.to_datetime(stored["time"])
    return int((stamps.dt.strftime("%Y-%m-%d") == day).sum())


@pytest.fixture
def db(tmp_path):
    database = StockDB(CODE, str(tmp_path))
    try:
        yield database
    finally:
        database.close()


def test_halted_day_overwrite_accepted_with_matching_evidence(db):
    day = "2026-09-11"
    full = _five_minute_times(day)
    assert db.save_kline(_frame(full), period="5m", overwrite=True, overwrite_trade_dates=True) == 48

    halted = full[12:]  # 上午停牌到 10:30，只有 36 根
    saved = db.save_kline(
        _frame(halted), period="5m", overwrite=True, overwrite_trade_dates=True,
        verified_short_trade_dates={"20260911": 36},
    )
    assert saved == 36
    assert _rows_on(db, day) == 36


def test_short_day_overwrite_rejected_without_evidence(db):
    day = "2026-09-11"
    full = _five_minute_times(day)
    db.save_kline(_frame(full), period="5m", overwrite=True, overwrite_trade_dates=True)

    with pytest.raises(ValueError, match="拒绝整日覆写：数据源返回的交易日不完整"):
        db.save_kline(_frame(full[12:]), period="5m", overwrite=True, overwrite_trade_dates=True)
    assert _rows_on(db, day) == 48


def test_evidence_row_count_mismatch_is_rejected(db):
    day = "2026-09-11"
    full = _five_minute_times(day)
    db.save_kline(_frame(full), period="5m", overwrite=True, overwrite_trade_dates=True)

    with pytest.raises(ValueError, match="18/36 根"):
        db.save_kline(
            _frame(full[-18:]), period="5m", overwrite=True, overwrite_trade_dates=True,
            verified_short_trade_dates={"2026-09-11": 36},
        )
    assert _rows_on(db, day) == 48


def test_evidence_only_relaxes_the_listed_trade_date(db):
    day1, day2 = "2026-09-10", "2026-09-11"
    full1, full2 = _five_minute_times(day1), _five_minute_times(day2)
    db.save_kline(_frame(full1.append(full2)), period="5m", overwrite=True, overwrite_trade_dates=True)

    short_both = full1[12:].append(full2[12:])
    with pytest.raises(ValueError, match="2026-09-10 36/48 根"):
        db.save_kline(
            _frame(short_both), period="5m", overwrite=True, overwrite_trade_dates=True,
            verified_short_trade_dates={"20260911": 36},
        )
    assert _rows_on(db, day1) == 48
    assert _rows_on(db, day2) == 48


def test_evidence_cannot_relax_daily_bars_or_exceed_full_count(db):
    day = "2026-09-11"
    full = _five_minute_times(day)
    db.save_kline(_frame(full), period="5m", overwrite=True, overwrite_trade_dates=True)

    # 证据根数不小于标准根数时不生效，缺根仍被拒绝。
    with pytest.raises(ValueError, match="36/48 根"):
        db.save_kline(
            _frame(full[12:]), period="5m", overwrite=True, overwrite_trade_dates=True,
            verified_short_trade_dates={"20260911": 48},
        )
    assert _rows_on(db, day) == 48
