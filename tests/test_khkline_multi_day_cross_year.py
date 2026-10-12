# -*- coding: utf-8 -*-
"""khKline 多日周期在年初也要取够根数：当年不够时向前一年接着取，每年仍按年初对齐。"""
from datetime import datetime

import pandas as pd

import khQTTools


def _bdays(start, end):
    return [d.to_pydatetime() for d in pd.bdate_range(start, end)]


def _patch(monkeypatch):
    monkeypatch.setattr(khQTTools, "_get_trade_days_list", lambda s, e: _bdays(s, e))
    monkeypatch.setattr(
        khQTTools, "_get_year_first_trade_day",
        lambda y: pd.bdate_range(f"{y}-01-01", periods=1)[0].to_pydatetime(),
    )

    def fake_fetch(**kwargs):
        days = pd.bdate_range(kwargs["start_time"], kwargs["end_time"])
        df = pd.DataFrame({
            "time": days,
            "open": range(len(days)), "high": range(len(days)),
            "low": range(len(days)), "close": range(len(days)),
            "volume": [1] * len(days),
        })
        return {code: df for code in kwargs["stock_list"]}

    monkeypatch.setattr(khQTTools, "_kline_get_market_data_ex", fake_fetch)


def test_early_january_gets_full_bar_count_from_previous_year(monkeypatch):
    _patch(monkeypatch)
    out = khQTTools._get_day_kline(
        ["000001.SZ"], 2, 30, ["open", "high", "low", "close", "volume"],
        datetime(2025, 1, 10), "none", False,
    )
    df = out["000001.SZ"]
    assert len(df) == 30
    assert df["time"].iloc[-1].date() == datetime(2025, 1, 10).date()
    assert df["time"].iloc[0].year == 2024
    assert df["time"].is_monotonic_increasing


def test_groups_restart_at_each_year_start(monkeypatch):
    _patch(monkeypatch)
    out = khQTTools._get_day_kline(
        ["000001.SZ"], 3, 10, ["open", "close", "volume"],
        datetime(2025, 1, 10), "none", False,
    )
    df = out["000001.SZ"]
    # 2025 年第一组从 1 月 1 日（周三）起算：1/1、1/2、1/3 为一组，1/6~1/8 为下一组
    in_2025 = df[df["time"].dt.year == 2025]
    assert in_2025["time"].dt.strftime("%m-%d").tolist() == ["01-03", "01-08", "01-10"]
    assert in_2025["volume"].tolist() == [3, 3, 2]
