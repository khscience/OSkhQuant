# coding: utf-8
"""khMA / khHistory 等接口的两个口径问题回归测试。

#21 日内周期(1m/5m/tick)的墙钟交易时段检查只对"实时语义"生效：
    未指定 end_time 且不在回测框架中才检查；传了历史 end_time 或处于回测框架时跳过，
    否则夜间跑历史回测必然失败。
#3  fq 默认值改为"未指定"(None)：回测框架内跟随框架(界面)复权口径，独立调用仍为前复权；
    显式 fq 一律尊重，与框架口径不一致时每个组合只警告一次。

全部使用 mock 墙钟和内存数据，不读写真实行情库。
"""
import inspect
import logging
import sys
import time
from types import SimpleNamespace

import pandas as pd
import pytest


khQTTools = None


@pytest.fixture(autouse=True)
def _import_target_module():
    """在 conftest 的外部依赖 mock 生效后再导入被测模块，避免改变其他测试的导入顺序。"""
    global khQTTools
    import khQTTools as _module
    khQTTools = _module
    yield


# ── 公共构造 ────────────────────────────────────────────────────────────

def _framework(dividend_type="none", run_mode="backtest", period="1d"):
    config = SimpleNamespace(
        run_mode=run_mode,
        config_dict={"run_mode": run_mode, "data": {"kline_period": period, "dividend_type": dividend_type}},
    )
    return SimpleNamespace(config=config, run_mode=run_mode)


def _set_framework(monkeypatch, framework):
    """与框架回调前设置 khQuantImport._CURRENT_FRAMEWORK 的方式一致(用轻量替身模块)。"""
    monkeypatch.setitem(sys.modules, "khQuantImport", SimpleNamespace(_CURRENT_FRAMEWORK=framework))


def _no_framework(monkeypatch):
    _set_framework(monkeypatch, None)


@pytest.fixture
def fake_history(monkeypatch):
    """替换 khHistory：返回内存数据并记录调用参数。"""
    calls = []

    def _khHistory(symbol_list, fields, bar_count, fre_step, current_time=None, skip_paused=False,
                   fq=None, force_download=False, **kwargs):
        calls.append({"fre_step": fre_step, "current_time": current_time, "fq": fq})
        code = symbol_list if isinstance(symbol_list, str) else symbol_list[0]
        return {code: pd.DataFrame({"time": list(range(bar_count)), fields[0]: [10.0] * bar_count})}

    monkeypatch.setattr(khQTTools, "khHistory", _khHistory)
    return calls


@pytest.fixture
def night_clock(monkeypatch):
    """模拟夜间墙钟(22:00:00)：is_trade_time() 为 False。"""
    real_strftime = time.strftime

    def _strftime(fmt, *args):
        if fmt == "%H%M%S" and not args:
            return "220000"
        return real_strftime(fmt, *args)

    monkeypatch.setattr(khQTTools.time, "strftime", _strftime)
    assert khQTTools.is_trade_time() is False


# ── #21 墙钟交易时段检查 ────────────────────────────────────────────────

@pytest.mark.parametrize("fre_step", ["1m", "5m", "tick"])
def test_khma_with_history_end_time_ignores_wall_clock(monkeypatch, night_clock, fake_history, fre_step):
    _no_framework(monkeypatch)
    value = khQTTools.khMA("000001.SZ", 5, fre_step=fre_step, end_time="20250303 100000")
    assert value == 10.0
    assert fake_history[-1]["current_time"] == "20250303 100000"


@pytest.mark.parametrize("fre_step", ["1m", "5m", "tick"])
def test_khma_in_backtest_framework_ignores_wall_clock(monkeypatch, night_clock, fake_history, fre_step):
    _set_framework(monkeypatch, _framework(run_mode="backtest", period=fre_step))
    assert khQTTools.khMA("000001.SZ", 5, fre_step=fre_step) == 10.0


def test_khma_realtime_outside_trade_time_still_raises(monkeypatch, night_clock, fake_history):
    _no_framework(monkeypatch)
    with pytest.raises(ValueError, match="不在交易时间内"):
        khQTTools.khMA("000001.SZ", 5, fre_step="1m")
    assert fake_history == []


def test_khma_realtime_in_non_backtest_framework_still_checks(monkeypatch, night_clock, fake_history):
    _set_framework(monkeypatch, _framework(run_mode="simulate", period="1m"))
    with pytest.raises(ValueError, match="不在交易时间内"):
        khQTTools.khMA("000001.SZ", 5, fre_step="1m")


def test_khma_realtime_inside_trade_time_ok(monkeypatch, fake_history):
    _no_framework(monkeypatch)
    monkeypatch.setattr(khQTTools, "is_trade_time", lambda: True)
    assert khQTTools.khMA("000001.SZ", 5, fre_step="1m") == 10.0


def test_calculate_moving_average_same_rules(monkeypatch, night_clock, fake_history):
    tools = khQTTools.KhQuTools()
    _no_framework(monkeypatch)
    assert tools.calculate_moving_average("000001.SZ", 5, fre_step="1m", end_time="20250303 100000") == 10.0
    with pytest.raises(ValueError, match="不在交易时间内"):
        tools.calculate_moving_average("000001.SZ", 5, fre_step="1m")

    _set_framework(monkeypatch, _framework(run_mode="backtest", period="1m"))
    assert tools.calculate_moving_average("000001.SZ", 5, fre_step="tick") == 10.0


def test_daily_khma_never_checks_wall_clock(monkeypatch, night_clock, fake_history):
    _no_framework(monkeypatch)
    assert khQTTools.khMA("000001.SZ", 5, fre_step="1d") == 10.0


# ── #3 fq 默认跟随框架复权口径 ─────────────────────────────────────────

def test_public_fq_defaults_are_unspecified():
    for func in (khQTTools.khMA, khQTTools.khHistory, khQTTools.khKline,
                 khQTTools.KhQuTools.calculate_moving_average):
        assert inspect.signature(func).parameters["fq"].default is None, func.__name__


@pytest.fixture
def fresh_warnings(monkeypatch):
    monkeypatch.setattr(khQTTools, "_KH_FQ_MISMATCH_WARNED", set())


@pytest.mark.parametrize(
    "framework_dividend, expected",
    [("none", "none"), ("front", "front"), ("back", "back"),
     ("front_ratio", "front_ratio"), ("back_ratio", "back_ratio"),
     ("pre", "front"), ("post", "back")],
)
def test_default_fq_follows_framework(monkeypatch, fresh_warnings, framework_dividend, expected):
    _set_framework(monkeypatch, _framework(dividend_type=framework_dividend))
    assert khQTTools._kh_resolve_dividend_type(None) == expected


def test_default_fq_without_framework_is_pre(monkeypatch, fresh_warnings):
    _no_framework(monkeypatch)
    assert khQTTools._kh_resolve_dividend_type(None) == "front"


def test_explicit_fq_respected_and_warned_once_per_combination(monkeypatch, fresh_warnings, caplog):
    _set_framework(monkeypatch, _framework(dividend_type="none"))
    with caplog.at_level(logging.WARNING):
        assert khQTTools._kh_resolve_dividend_type("pre", caller="khHistory") == "front"
        assert khQTTools._kh_resolve_dividend_type("pre", caller="khHistory") == "front"
        assert khQTTools._kh_resolve_dividend_type("post", caller="khHistory") == "back"
        assert khQTTools._kh_resolve_dividend_type("post", caller="khHistory") == "back"
        # 与框架一致的显式 fq 不警告
        assert khQTTools._kh_resolve_dividend_type("none", caller="khHistory") == "none"
    warnings = [r for r in caplog.records if "不一致" in r.getMessage()]
    assert len(warnings) == 2
    assert "fq='pre'" in warnings[0].getMessage()
    assert "fq='post'" in warnings[1].getMessage()


def test_explicit_fq_without_framework_no_warning(monkeypatch, fresh_warnings, caplog):
    _no_framework(monkeypatch)
    with caplog.at_level(logging.WARNING):
        assert khQTTools._kh_resolve_dividend_type("none") == "none"
        assert khQTTools._kh_resolve_dividend_type("post") == "back"
        assert khQTTools._kh_resolve_dividend_type("pre") == "front"
    assert not [r for r in caplog.records if "不一致" in r.getMessage()]


@pytest.fixture
def capture_khhistory_dividend(monkeypatch):
    """截获 khHistory 内部解析出的 dividend_type(在框架内存快速路径处直接返回)。"""
    seen = []

    def _fake_memory(framework, stock_codes, period, fields, fields_key, dividend_type, *args, **kwargs):
        seen.append(dividend_type)
        return {code: pd.DataFrame({"time": [1], "close": [1.0]}) for code in stock_codes}, []

    monkeypatch.setattr(khQTTools, "_khist_try_framework_memory", _fake_memory)
    monkeypatch.setattr(khQTTools, "_khist_perf_config", lambda framework: {})
    # khHistory 会记录观测到的回看需求(全局)，测试结束后还原
    monkeypatch.setattr(khQTTools, "_KHIST_OBSERVED_LOOKBACK_DAYS", khQTTools._KHIST_OBSERVED_LOOKBACK_DAYS)
    monkeypatch.setattr(khQTTools, "_KHIST_OBSERVED_TRADING_DAYS", khQTTools._KHIST_OBSERVED_TRADING_DAYS)
    return seen


@pytest.mark.parametrize("framework_dividend", ["none", "front", "back"])
def test_khhistory_default_fq_uses_framework_dividend(monkeypatch, fresh_warnings, capture_khhistory_dividend,
                                                      framework_dividend):
    _set_framework(monkeypatch, _framework(dividend_type=framework_dividend))
    khQTTools.khHistory("000001.SZ", ["close"], 3, "1d", "20250303")
    assert capture_khhistory_dividend == [framework_dividend]


def test_khhistory_without_framework_defaults_to_pre(monkeypatch, fresh_warnings, capture_khhistory_dividend):
    _no_framework(monkeypatch)
    khQTTools.khHistory("000001.SZ", ["close"], 3, "1d", "20250303")
    assert capture_khhistory_dividend == ["front"]


def test_khhistory_explicit_fq_respected_with_warning(monkeypatch, fresh_warnings, capture_khhistory_dividend, caplog):
    _set_framework(monkeypatch, _framework(dividend_type="none"))
    with caplog.at_level(logging.WARNING):
        khQTTools.khHistory("000001.SZ", ["close"], 3, "1d", "20250303", fq="pre")
        khQTTools.khHistory("000001.SZ", ["close"], 3, "1d", "20250304", fq="pre")
    assert capture_khhistory_dividend == ["front", "front"]
    assert len([r for r in caplog.records if "不一致" in r.getMessage()]) == 1


def test_khma_default_fq_reaches_khhistory_as_framework_dividend(monkeypatch, fresh_warnings,
                                                                 capture_khhistory_dividend):
    _set_framework(monkeypatch, _framework(dividend_type="back"))
    khQTTools.khMA("000001.SZ", 1, end_time="20250303")
    assert capture_khhistory_dividend == ["back"]
