# coding: utf-8
"""日末估值的收盘价必须与回测主数据同一复权口径。

前复权价按下载时的复权因子存库，分钟表与日线表基准可能不同（差一次或多次分红），
所以分钟回测的日末估值取主数据周期当天最后一根 bar，而不是去读日线表。
使用替身数据源，不读写真实行情库。
"""
from types import SimpleNamespace

import pandas as pd
import pytest


khFrame = None


@pytest.fixture(autouse=True)
def _import_target_module():
    global khFrame
    import khFrame as _module
    khFrame = _module
    yield


class _FakeSource:
    data_source = "duckdb"

    def __init__(self):
        self.calls = []

    def get_market_data_ex(self, **kwargs):
        self.calls.append(("ex", kwargs))
        return {
            code: pd.DataFrame({"time": [1, 2, 3], "close": [11.0, 11.112, 11.102]})
            for code in kwargs["stock_list"]
        }

    def get_market_data(self, **kwargs):
        self.calls.append(("daily", kwargs))
        return {"close": pd.DataFrame({"20250606": [10.742]}, index=kwargs["stock_list"])}


def _framework(period, dividend_type="front"):
    fw = khFrame.KhQuantFramework.__new__(khFrame.KhQuantFramework)
    fw._loaded_data_period = period
    fw.config = SimpleNamespace(config_dict={"data": {"dividend_type": dividend_type}})
    fw.data_source_mgr = _FakeSource()
    fw.daily_price_cache = {}
    return fw


def test_minute_backtest_uses_same_period_last_bar(monkeypatch):
    monkeypatch.setattr(khFrame, "_is_native_qmt_read_manager", lambda _mgr: False, raising=False)
    fw = _framework("1m")
    prices = fw._query_session_close(["000001.SZ"], "20250606")
    assert prices == {"000001.SZ": 11.102}
    kind, kwargs = fw.data_source_mgr.calls[0]
    assert kind == "ex"
    assert kwargs["period"] == "1m" and kwargs["dividend_type"] == "front"
    # 同一天同一代码只查询一次
    fw._query_session_close(["000001.SZ"], "20250606")
    assert len(fw.data_source_mgr.calls) == 1


def test_daily_and_tick_backtest_use_daily_table(monkeypatch):
    monkeypatch.setattr(khFrame, "_is_native_qmt_read_manager", lambda _mgr: False, raising=False)
    for period in ("1d", "tick"):
        fw = _framework(period)
        prices = fw._query_session_close(["000001.SZ"], "20250606")
        assert prices == {"000001.SZ": 10.742}
        assert fw.data_source_mgr.calls[0][0] == "daily"
    # tick 是未复权价，日线也按不复权取
    assert fw.data_source_mgr.calls[0][1]["dividend_type"] == "none"
