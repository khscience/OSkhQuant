# -*- coding: utf-8 -*-
"""一次性加载时 khHistory 内存快路不能把老股票误判为“新上市”而截短历史。

预读起点落在周末/节假日，或日线时间带 09:30 时，首根 K 线的时间戳总会晚于
预读起点；旧逻辑据此认定“窗口内上市、库里没有更早数据”，直接返回内存里的
短历史。正确做法：首根所在交易日晚于窗口第一个交易日才算新上市，否则回查 DuckDB。
"""
import numpy as np
import pandas as pd
import pytest

import khQTTools


class _FakeFramework:
    def __init__(self, load_start):
        self._khhistory_full_load_start_time = pd.Timestamp(load_start)


def _entry(first_day, n, hour=0, minute=0):
    days = pd.bdate_range(first_day, periods=n)
    times = days + pd.Timedelta(hours=hour, minutes=minute)
    df = pd.DataFrame({"time": times, "close": np.arange(n, dtype=float)})
    return {"time_np": times.values.astype("datetime64[ns]"), "df": df}


@pytest.fixture
def patched(monkeypatch):
    khQTTools.clear_khHistory_cache()
    state = {}
    monkeypatch.setattr(khQTTools, "_khist_framework_is_compatible", lambda *a, **k: True)
    monkeypatch.setattr(khQTTools, "_khist_framework_dynamic_loading", lambda fw: False)
    monkeypatch.setattr(khQTTools, "_khist_framework_first_time_np", lambda fw, code: None)
    monkeypatch.setattr(khQTTools, "_khist_framework_entry", lambda fw, code, *a: state["entry"])
    monkeypatch.setattr(khQTTools, "_warn_kh_history_missing_once", lambda *a, **k: None)

    def _trade_days(start, end):
        return {d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)}

    monkeypatch.setattr(khQTTools, "get_trade_days_set", _trade_days)
    yield state
    khQTTools.clear_khHistory_cache()


def _call(framework, current, bar_count):
    return khQTTools._khist_try_framework_memory(
        framework, ["000001.SZ"], "1d", ["close"], ("close",), "none", False,
        pd.Timestamp(current).to_pydatetime(), bar_count, ["time", "close"], {},
    )


def test_weekend_load_start_falls_back_to_duckdb(patched):
    # 预读起点 2024-01-06 是周六，首根在 2024-01-08（周一）：不是新上市。
    patched["entry"] = _entry("2024-01-08", 120)
    result, misses = _call(_FakeFramework("2024-01-06"), "2024-06-28", 250)
    assert misses == ["000001.SZ"]
    assert "000001.SZ" not in result


def test_daily_bars_at_0930_on_first_session_fall_back_to_duckdb(patched):
    patched["entry"] = _entry("2024-01-08", 120, hour=9, minute=30)
    result, misses = _call(_FakeFramework("2024-01-08"), "2024-06-28", 250)
    assert misses == ["000001.SZ"]
    assert "000001.SZ" not in result


def test_listed_inside_window_returns_known_short_history(patched):
    # 首根晚于窗口第一个交易日：确实在窗口内上市，库里没有更早数据。
    patched["entry"] = _entry("2024-03-04", 60)
    result, misses = _call(_FakeFramework("2024-01-06"), "2024-06-28", 250)
    assert misses == []
    assert len(result["000001.SZ"]) == 60


def test_enough_bars_in_memory_uses_fast_path(patched):
    patched["entry"] = _entry("2024-01-08", 120)
    result, misses = _call(_FakeFramework("2024-01-06"), "2024-06-28", 50)
    assert misses == []
    assert len(result["000001.SZ"]) == 50
