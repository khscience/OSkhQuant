# coding: utf-8
"""示例双均线策略遇到历史K线不足的股票时跳过，而不是让整个回测报错中止。

背景：2026-09-27 用沪深300、2015-06 起的区间跑「双均线多股票_使用khMA函数」，
001280.SZ 这类后上市的股票前期没有足够K线，khMA 按约定抛 ValueError，
示例没有接住，回测在第一天就中止。khMA 属于内核（与 CS 一致），不改；由示例处理。
"""

import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MULTI = os.path.join(ROOT, "strategies", "【1-MA策略案例】双均线多股票_使用khMA函数.py")
SINGLE = os.path.join(ROOT, "strategies", "【1-MA策略案例】双均线精简_使用khMA函数.py")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_khma(short_values):
    def khMA(stock_code, period, **kwargs):
        if stock_code == "001280.SZ":
            raise ValueError(f"股票 {stock_code} 数据量不足 {period} 条，无法计算均线{period}")
        return short_values if period < 15 else 10.0
    return khMA


def _stub_common(module, monkeypatch, stocks, first_stock):
    values = {"stocks": stocks, "first_stock": first_stock, "date_num": 20150601}
    monkeypatch.setattr(module, "khGet", lambda data, key: values[key])
    monkeypatch.setattr(module, "khPrice", lambda data, code, field="close": 12.0)
    monkeypatch.setattr(module, "khHas", lambda data, code: False)
    monkeypatch.setattr(
        module, "generate_signal",
        lambda data, code, price, ratio, action, reason: [{"code": code, "action": action}],
    )


def test_multi_stock_sample_skips_stock_without_enough_history(monkeypatch):
    module = _load(MULTI, "_sample_ma_multi")
    _stub_common(module, monkeypatch, ["001280.SZ", "600000.SH"], "001280.SZ")
    monkeypatch.setattr(module, "khMA", _fake_khma(11.0))

    signals = module.khHandlebar({})

    assert signals == [{"code": "600000.SH", "action": "buy"}]


def test_single_stock_sample_does_nothing_without_enough_history(monkeypatch):
    module = _load(SINGLE, "_sample_ma_single")
    _stub_common(module, monkeypatch, ["001280.SZ"], "001280.SZ")
    monkeypatch.setattr(module, "khMA", _fake_khma(11.0))

    assert module.khHandlebar({}) == []


@pytest.mark.parametrize("path", [MULTI, SINGLE])
def test_samples_do_not_swallow_other_errors(path, monkeypatch):
    """只接住“数据量不足”的 ValueError；用户在缺数据提示里选择停止回测等其他异常照常抛出。"""
    module = _load(path, "_sample_ma_other_error")
    _stub_common(module, monkeypatch, ["600000.SH"], "600000.SH")

    def khMA(stock_code, period, **kwargs):
        raise RuntimeError("用户因 khHistory 缺少历史数据停止回测")

    monkeypatch.setattr(module, "khMA", khMA)
    with pytest.raises(RuntimeError):
        module.khHandlebar({})
