# coding: utf-8
"""Tushare 复权因子分页与逐交易日覆盖校验。"""
from __future__ import annotations

from unittest.mock import call

import pandas as pd
import pytest

from duckdb_storage.tushare_importer import (
    AdjustmentFactorCoverageError,
    TushareAPIError,
    TushareImporter,
)


def _factor_page(start: str, periods: int) -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=periods)
    return pd.DataFrame(
        {
            "trade_date": dates.strftime("%Y%m%d")[::-1],
            "adj_factor": [1.0] * periods,
        }
    )


def _raw(dates: list[str]) -> pd.DataFrame:
    count = len(dates)
    return pd.DataFrame(
        {
            "time": pd.to_datetime(dates),
            "open": [10.0] * count,
            "high": [11.0] * count,
            "low": [9.0] * count,
            "close": [10.5] * count,
        }
    )


def test_stock_adj_factor_uses_official_single_stock_full_history_contract(monkeypatch):
    importer = TushareImporter(token="test-token")
    expected = pd.DataFrame(
        {
            "trade_date": ["20240102", "20240103"],
            "adj_factor": [1.0, 1.1],
        }
    )
    calls = []

    def fake_call(method, **kwargs):
        calls.append(call(method, **kwargs))
        return expected

    monkeypatch.setattr(importer, "_call", fake_call)

    result = importer.download_adj_factor("000001.SZ")

    assert calls == [call("adj_factor", ts_code="000001.SZ")]
    assert result["trade_date"].tolist() == ["20240103", "20240102"]


def test_fund_adj_factor_pages_until_short_page(monkeypatch):
    importer = TushareImporter(token="test-token")
    first = _factor_page("2010-01-01", 2000)
    second = _factor_page("2009-12-28", 3)
    calls = []

    def fake_call(method, **kwargs):
        calls.append(call(method, **kwargs))
        return first if kwargs["offset"] == 0 else second

    monkeypatch.setattr(importer, "_call", fake_call)

    result = importer.download_adj_factor("510300.SH")

    assert calls == [
        call("fund_adj", ts_code="510300.SH", offset=0, limit=2000),
        call("fund_adj", ts_code="510300.SH", offset=2000, limit=2000),
    ]
    assert len(result) == 2003
    assert result["trade_date"].is_unique
    assert result["trade_date"].is_monotonic_decreasing


def test_lof_adj_factor_routes_to_fund_api(monkeypatch):
    importer = TushareImporter(token="test-token")
    expected = _factor_page("2024-01-02", 3)
    calls = []

    def fake_call(method, **kwargs):
        calls.append(call(method, **kwargs))
        return expected

    monkeypatch.setattr(importer, "_call", fake_call)

    result = importer.download_adj_factor("161226.SZ")

    assert calls == [
        call("fund_adj", ts_code="161226.SZ", offset=0, limit=2000),
    ]
    assert len(result) == 3


def test_fund_adj_factor_rejects_server_that_ignores_offset(monkeypatch):
    importer = TushareImporter(token="test-token")
    repeated_page = _factor_page("2010-01-01", 2000)
    monkeypatch.setattr(importer, "_call", lambda *_args, **_kwargs: repeated_page)

    with pytest.raises(TushareAPIError, match="忽略 offset"):
        importer.download_adj_factor("159915.SZ")


@pytest.mark.parametrize("method_name", ["_apply_adj", "_apply_both_adj"])
def test_adjustment_rejects_any_missing_market_date(method_name):
    raw = _raw(["2024-01-02", "2024-01-03", "2024-01-04"])
    factors = pd.DataFrame(
        {
            "trade_date": ["20240104", "20240102"],
            "adj_factor": [1.2, 1.0],
        }
    )

    method = getattr(TushareImporter, method_name)
    with pytest.raises(AdjustmentFactorCoverageError, match="20240103"):
        method(raw, factors)


def test_adjustment_accepts_multiple_bars_when_each_trade_date_has_factor():
    raw = _raw(["2024-01-02 09:30:00", "2024-01-02 09:31:00", "2024-01-03 09:30:00"])
    factors = pd.DataFrame(
        {
            "trade_date": ["20240103", "20240102"],
            "adj_factor": [2.0, 1.0],
        }
    )

    adjusted = TushareImporter._apply_adj(raw, factors, mode="qfq")

    assert adjusted["open_front"].tolist() == [5.0, 5.0, 10.0]


def test_adjustment_rejects_unparseable_market_time():
    raw = _raw(["2024-01-02"])
    raw.loc[0, "time"] = pd.NaT
    factors = pd.DataFrame(
        {"trade_date": ["20240102"], "adj_factor": [1.0]}
    )

    with pytest.raises(AdjustmentFactorCoverageError, match="time 无法解析"):
        TushareImporter._apply_adj(raw, factors, mode="qfq")


@pytest.mark.parametrize("invalid_factor", [float("inf"), float("-inf")])
def test_adjustment_rejects_nonfinite_factor(invalid_factor):
    raw = _raw(["2024-01-02"])
    factors = pd.DataFrame(
        {"trade_date": ["20240102"], "adj_factor": [invalid_factor]}
    )

    with pytest.raises(AdjustmentFactorCoverageError, match="因子为空|缺少"):
        TushareImporter._apply_adj(raw, factors, mode="qfq")
