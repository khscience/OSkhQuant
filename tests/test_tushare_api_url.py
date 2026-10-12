# coding: utf-8
"""Tushare API 地址选择与实例隔离测试。"""
from __future__ import annotations

import os
import sys
import types

import pandas as pd
import pytest

from duckdb_storage.tushare_importer import (
    AdjustmentFactorCoverageError,
    TUSHARE_DEFAULT_URL,
    TushareImporter,
    _normalize_api_url,
    _setup_proxy,
)


class _FakeDataApi:
    # 模拟 tushare 1.x 自带的旧默认值，测试 importer 不修改类变量。
    _DataApi__http_url = "http://api.waditu.com/dataapi"

    def __init__(self, token: str):
        self.token = token
        self.calls = []

    def daily(self, **kwargs):
        self.calls.append((self._DataApi__http_url, kwargs))
        return pd.DataFrame([{"trade_date": "20240102"}])

    def index_daily(self, **kwargs):
        self.calls.append((self._DataApi__http_url, kwargs))
        return pd.DataFrame([{"trade_date": "20240102"}])

    def trade_cal(self, **kwargs):
        self.calls.append((self._DataApi__http_url, kwargs))
        return pd.DataFrame([{"cal_date": "20240101", "is_open": 1}])


def _install_fake_tushare(monkeypatch):
    tushare_module = types.ModuleType("tushare")
    pro_module = types.ModuleType("tushare.pro")
    client_module = types.ModuleType("tushare.pro.client")

    tushare_module.__path__ = []
    pro_module.__path__ = []
    client_module.DataApi = _FakeDataApi
    tushare_module.pro = pro_module
    pro_module.client = client_module
    tushare_module.pro_api = lambda token: _FakeDataApi(token)

    monkeypatch.setitem(sys.modules, "tushare", tushare_module)
    monkeypatch.setitem(sys.modules, "tushare.pro", pro_module)
    monkeypatch.setitem(sys.modules, "tushare.pro.client", client_module)


def test_normalize_api_url_preserves_selected_server():
    assert _normalize_api_url("") == TUSHARE_DEFAULT_URL
    assert _normalize_api_url("api.tushare.pro/") == TUSHARE_DEFAULT_URL
    assert _normalize_api_url("https：//new.example.test/daily") == "https://new.example.test"
    assert _normalize_api_url("http://tushare.xyz/") == "http://tushare.xyz"


def test_proxy_disabled_bypasses_custom_api_host(monkeypatch):
    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
        "ALL_PROXY", "all_proxy",
    ):
        monkeypatch.setenv(key, "http://old-proxy.example.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "localhost")

    _setup_proxy(False, api_url="https://new-api.example.test/dataapi")

    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
        "ALL_PROXY", "all_proxy",
    ):
        assert key not in os.environ
    assert "new-api.example.test" in os.environ["NO_PROXY"].split(",")


def test_api_url_is_bound_to_each_data_api_instance(monkeypatch):
    _install_fake_tushare(monkeypatch)

    official = TushareImporter(token="official", api_url="https://api.tushare.pro")
    mirror = TushareImporter(token="mirror", api_url="https://mirror.example.test/api")

    official_pro = official._get_pro()
    mirror_pro = mirror._get_pro()

    # v3.3.8 曾把官网域名错误写进 SDK 数据地址；旧配置必须自动迁移为
    # SDK 自带的完整 dataapi 地址，且不能改动 DataApi 类变量。
    assert official_pro._DataApi__http_url == "http://api.waditu.com/dataapi"
    assert mirror_pro._DataApi__http_url == "https://mirror.example.test/api"
    assert _FakeDataApi._DataApi__http_url == "http://api.waditu.com/dataapi"

    result = official._call("daily", ts_code="000001.SZ")
    assert not result.empty
    assert official_pro.calls == [
        ("http://api.waditu.com/dataapi", {"ts_code": "000001.SZ"})
    ]
    assert mirror_pro.calls == []


def test_connection_rebuild_keeps_configured_url(monkeypatch):
    _install_fake_tushare(monkeypatch)
    importer = TushareImporter(token="token", api_url="https://new.example.test")

    ok, message = importer.test_connection()

    assert ok is True
    assert "https://new.example.test" in message
    assert importer._pro._DataApi__http_url == "https://new.example.test"
    assert [call[1]["ts_code"] for call in importer._pro.calls] == [
        "000001.SZ",
        "000300.SH",
    ]


def test_connection_rejects_empty_required_market_endpoint(monkeypatch):
    importer = TushareImporter(token="token", api_url="")
    monkeypatch.setattr(
        importer,
        "_call",
        lambda api_name, **_kwargs: (
            pd.DataFrame([{"trade_date": "20240102"}])
            if api_name == "daily"
            else pd.DataFrame()
        ),
    )

    ok, message = importer.test_connection()

    assert ok is False
    assert "index_daily" in message
    assert "空数据" in message


def test_connection_rejects_missing_required_permission(monkeypatch):
    importer = TushareImporter(token="token", api_url="")
    monkeypatch.setattr(
        importer,
        "_call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("抱歉，您没有访问 index_daily 接口的权限")
        ),
    )

    ok, message = importer.test_connection()

    assert ok is False
    assert "权限" in message


def test_connection_rejects_path_accidentally_saved_as_token(monkeypatch):
    importer = TushareImporter(
        token="找不到命令行工具: /Applications/khQuant.app/Contents/MacOS/kh",
        api_url="https://example.test/api",
    )
    monkeypatch.setattr(
        importer,
        "_call",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应发起请求")),
    )

    ok, message = importer.test_connection()

    assert ok is False
    assert "Token 格式异常" in message


def test_latest_trade_date_routes_index_to_index_daily(monkeypatch):
    importer = TushareImporter(token="token", api_url="")
    calls = []

    def fake_call(api_name, **kwargs):
        calls.append((api_name, kwargs))
        return pd.DataFrame([{"trade_date": "20240102"}])

    monkeypatch.setattr(importer, "_call", fake_call)

    assert importer.get_latest_trade_date("000300.SH") == "20240102"
    assert calls[0][0] == "index_daily"


def test_adjustment_normalizes_numeric_factor_dates():
    raw = pd.DataFrame(
        {
            "time": pd.to_datetime(["2024-01-01", "2024-01-02"]),
            "open": [10.0, 20.0],
            "high": [11.0, 21.0],
            "low": [9.0, 19.0],
            "close": [10.5, 20.5],
        }
    )
    factors = pd.DataFrame(
        {
            "trade_date": [20240102, 20240101],
            "adj_factor": [2.0, 1.0],
        }
    )

    adjusted = TushareImporter._apply_adj(raw, factors, mode="qfq")

    assert adjusted["open_front"].tolist() == [5.0, 20.0]
    assert adjusted["close_front"].tolist() == [5.25, 20.5]


def test_adjustment_rejects_factor_dates_without_market_overlap():
    raw = pd.DataFrame(
        {
            "time": pd.to_datetime(["2024-01-01", "2024-01-02"]),
            "open": [10.0, 20.0],
            "high": [11.0, 21.0],
            "low": [9.0, 19.0],
            "close": [10.5, 20.5],
        }
    )
    factors = pd.DataFrame(
        {
            "trade_date": ["20230102", "20230101"],
            "adj_factor": [2.0, 1.0],
        }
    )

    with pytest.raises(AdjustmentFactorCoverageError, match="20240101"):
        TushareImporter._apply_adj(raw, factors, mode="qfq")
    with pytest.raises(AdjustmentFactorCoverageError, match="20240101"):
        TushareImporter._apply_both_adj(raw, factors)
