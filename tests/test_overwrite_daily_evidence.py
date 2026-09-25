# coding: utf-8
"""整日覆写校验：分钟帧缺根时用日线成交量核对停牌（xtdata/baostock/tushare 共用）。"""

import pandas as pd
import pytest

from duckdb_storage.incremental import (
    daily_volume_overwrite_evidence,
    validate_overwrite_frame,
    validate_overwrite_frame_with_daily_evidence,
)

DAYS = ["20260910", "20260911"]


def _ms(text):
    return int(pd.Timestamp(text, tz="Asia/Shanghai").timestamp() * 1000)


def _five_minute_times(day):
    stamp = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    morning = pd.date_range(f"{stamp} 09:35", f"{stamp} 11:30", freq="5min")
    afternoon = pd.date_range(f"{stamp} 13:05", f"{stamp} 15:00", freq="5min")
    return [_ms(str(value)) for value in list(morning) + list(afternoon)]


def _bars(times, volume_per_bar=100.0):
    count = len(times)
    return pd.DataFrame({
        "time": times,
        "open": [10.0] * count, "high": [10.5] * count,
        "low": [9.5] * count, "close": [10.2] * count,
        "volume": [volume_per_bar] * count,
        "amount": [volume_per_bar * 10.0] * count,
    })


def _daily(volumes):
    return pd.DataFrame({
        "time": [_ms(f"{day[:4]}-{day[4:6]}-{day[6:]} 00:00") for day in volumes],
        "open": [10.0] * len(volumes), "high": [10.5] * len(volumes),
        "low": [9.5] * len(volumes), "close": [10.2] * len(volumes),
        "volume": list(volumes.values()),
        "amount": [value * 10.0 for value in volumes.values()],
    })


def _halted_frame():
    # 0910 满 48 根；0911 上午停牌到 10:30，只有 36 根
    return _bars(_five_minute_times("20260910") + _five_minute_times("20260911")[12:])


def test_strict_validation_still_rejects_short_day_without_evidence():
    with pytest.raises(ValueError, match="36/48"):
        validate_overwrite_frame(_halted_frame(), "5m", DAYS)


def test_matching_daily_volume_verifies_short_day_and_returns_evidence():
    calls = []

    def fetch_daily():
        calls.append(True)
        return _daily({"20260910": 4800.0, "20260911": 3600.0})

    evidence = validate_overwrite_frame_with_daily_evidence(
        _halted_frame(), "5m", DAYS, fetch_daily=fetch_daily,
    )

    assert evidence == {"20260911": 36}
    assert calls == [True]


def test_truncated_day_with_larger_daily_volume_is_still_rejected():
    with pytest.raises(ValueError, match="36/48"):
        validate_overwrite_frame_with_daily_evidence(
            _halted_frame(), "5m", DAYS,
            fetch_daily=lambda: _daily({"20260910": 4800.0, "20260911": 4800.0}),
        )


def test_full_day_halt_is_accepted_when_daily_has_zero_volume_or_no_row():
    only_first_day = _bars(_five_minute_times("20260910"))
    with pytest.raises(ValueError, match="缺少 1 个交易日"):
        validate_overwrite_frame(only_first_day, "5m", DAYS)

    assert validate_overwrite_frame_with_daily_evidence(
        only_first_day, "5m", DAYS,
        fetch_daily=lambda: _daily({"20260910": 4800.0, "20260911": 0.0}),
    ) == {}
    assert validate_overwrite_frame_with_daily_evidence(
        only_first_day, "5m", DAYS,
        fetch_daily=lambda: _daily({"20260910": 4800.0}),
    ) == {}


def test_missing_or_failing_daily_gives_no_evidence():
    with pytest.raises(ValueError, match="36/48"):
        validate_overwrite_frame_with_daily_evidence(
            _halted_frame(), "5m", DAYS, fetch_daily=lambda: pd.DataFrame(),
        )

    def broken():
        raise RuntimeError("daily unavailable")

    with pytest.raises(ValueError, match="36/48"):
        validate_overwrite_frame_with_daily_evidence(
            _halted_frame(), "5m", DAYS, fetch_daily=broken,
        )


def test_complete_frame_never_fetches_daily_and_daily_period_is_not_relaxed():
    full = _bars(_five_minute_times("20260910") + _five_minute_times("20260911"))

    def must_not_fetch():
        raise AssertionError("完整帧不应请求日线")

    assert validate_overwrite_frame_with_daily_evidence(
        full, "5m", DAYS, fetch_daily=must_not_fetch,
    ) == {}

    daily_only_first = _daily({"20260910": 4800.0})
    with pytest.raises(ValueError):
        validate_overwrite_frame_with_daily_evidence(
            daily_only_first, "1d", DAYS,
            fetch_daily=lambda: _daily({"20260910": 4800.0, "20260911": 0.0}),
        )


def test_evidence_helper_ignores_complete_days_and_unmatched_volume():
    short, absent = daily_volume_overwrite_evidence(
        _halted_frame(), _daily({"20260910": 4800.0, "20260911": 3600.0}), "5m", DAYS,
    )
    assert short == {"20260911": 36}
    assert absent == set()

    short, absent = daily_volume_overwrite_evidence(
        _halted_frame(), _daily({"20260910": 4800.0, "20260911": 9999.0}), "5m", DAYS,
    )
    assert short == {} and absent == set()
