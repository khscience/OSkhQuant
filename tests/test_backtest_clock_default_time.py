# coding: utf-8
"""回测中 khMA / khHistory 未传时间参数时，应以当前 bar 的回测时钟为截止，
而不是电脑当前时间（否则会读到回测时点之后的数据）。

使用替身框架和内存数据，不读写真实行情库。
"""
import datetime as _dt
import sys
from types import SimpleNamespace

import pandas as pd
import pytest


khQTTools = None
khFrame = None


@pytest.fixture(autouse=True)
def _import_target_modules():
    global khQTTools, khFrame
    import khQTTools as _tools
    import khFrame as _frame
    khQTTools = _tools
    khFrame = _frame
    yield


def _framework(run_mode="backtest", clock=None):
    config = SimpleNamespace(run_mode=run_mode, config_dict={"run_mode": run_mode, "data": {"dividend_type": "none"}})
    fw = SimpleNamespace(config=config, run_mode=run_mode)
    if clock is not None:
        fw._kh_callback_clock = clock
    return fw


def _set_framework(monkeypatch, framework):
    monkeypatch.setitem(sys.modules, "khQuantImport", SimpleNamespace(_CURRENT_FRAMEWORK=framework))


@pytest.fixture
def fake_history(monkeypatch):
    calls = []

    def _khHistory(symbol_list, fields, bar_count, fre_step, current_time=None, skip_paused=False,
                   fq=None, force_download=False, **kwargs):
        calls.append(current_time)
        code = symbol_list if isinstance(symbol_list, str) else symbol_list[0]
        return {code: pd.DataFrame({"time": list(range(bar_count)), fields[0]: [10.0] * bar_count})}

    monkeypatch.setattr(khQTTools, "khHistory", _khHistory)
    return calls


def test_khma_uses_backtest_clock_when_end_time_missing(monkeypatch, fake_history):
    clock = _dt.datetime(2025, 1, 3, 10, 0, 0)
    _set_framework(monkeypatch, _framework(clock=clock))
    khQTTools.khMA("600000.SH", 5, fre_step="1d")
    khQTTools.khMA("600000.SH", 5, fre_step="1m")
    assert fake_history == ["20250103 100000", "20250103 100000"]


def test_khma_explicit_end_time_is_respected(monkeypatch, fake_history):
    _set_framework(monkeypatch, _framework(clock=_dt.datetime(2025, 1, 3, 10, 0, 0)))
    khQTTools.khMA("600000.SH", 5, fre_step="1d", end_time="20240601")
    assert fake_history == ["20240601"]


def test_khma_without_framework_still_uses_wall_clock(monkeypatch, fake_history):
    _set_framework(monkeypatch, None)
    khQTTools.khMA("600000.SH", 5, fre_step="1d")
    assert fake_history == [_dt.datetime.now().strftime("%Y%m%d")]


def test_backtest_clock_str_ignores_non_backtest_and_missing_clock(monkeypatch):
    _set_framework(monkeypatch, _framework(run_mode="simulate", clock=_dt.datetime(2025, 1, 3, 10, 0)))
    assert khQTTools._kh_backtest_clock_str("1d") is None
    _set_framework(monkeypatch, _framework(clock=None))
    assert khQTTools._kh_backtest_clock_str("1d") is None


def test_invoke_strategy_callback_records_clock():
    fw = SimpleNamespace()
    seen = {}

    def _callback(data):
        seen["clock"] = getattr(fw, "_kh_callback_clock", None)
        return []

    data = {"__current_time__": {"datetime": "2025-01-03 09:31:00"}}
    khFrame._invoke_strategy_callback(fw, _callback, data, set_framework_fn=lambda _f: None)
    assert seen["clock"] == _dt.datetime(2025, 1, 3, 9, 31, 0)

    dt_obj = _dt.datetime(2025, 1, 6, 8, 30, 0)
    khFrame._invoke_strategy_callback(fw, _callback, {"__current_time__": {"_dt": dt_obj}},
                                      set_framework_fn=lambda _f: None)
    assert seen["clock"] == dt_obj
