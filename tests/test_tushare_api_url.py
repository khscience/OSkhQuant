# coding: utf-8
"""Tushare API 地址选择与实例隔离测试。"""
from __future__ import annotations

import os
import types

import pandas as pd
import pytest

from duckdb_storage.tushare_importer import (
    AdjustmentFactorCoverageError,
    TUSHARE_DEFAULT_URL,
    TushareImporter,
    _normalize_api_url,
)


def _install_fake_transport(monkeypatch):
    """Mock the wire once; exercise real importer URL and response handling."""
    import duckdb_storage.tushare_transport as transport
    class Session:
        def __init__(self):
            self.calls, self.proxies = [], {}
            self.trust_env = True
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return types.SimpleNamespace(status_code=200, json=lambda: {
                "code": 0, "data": {"fields": ["trade_date"], "items": [["20240102"]]},
            })
        def close(self):
            pass
    monkeypatch.setattr(transport.requests, "Session", Session)


def test_normalize_api_url_preserves_selected_server():
    assert _normalize_api_url("") == TUSHARE_DEFAULT_URL
    assert _normalize_api_url("api.tushare.pro/") == TUSHARE_DEFAULT_URL
    assert _normalize_api_url("http://api.waditu.com/dataapi") == TUSHARE_DEFAULT_URL
    assert _normalize_api_url("https：//new.example.test/daily") == "https://new.example.test"
    assert _normalize_api_url("http://tushare.xyz/") == "http://tushare.xyz"


def test_proxy_disabled_bypasses_custom_api_host(monkeypatch):
    _install_fake_transport(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://system-proxy.example:8080")
    before = dict(os.environ)
    direct = TushareImporter("token", api_url="https://custom.example/api")._get_pro()
    proxied = TushareImporter("token", use_proxy=True, proxy_url="http://local.example:7890")._get_pro()
    assert direct._session.trust_env is False and direct._session.proxies == {}
    assert proxied._session.proxies == {"http": "http://local.example:7890", "https": "http://local.example:7890"}
    assert dict(os.environ) == before


def test_api_url_is_bound_to_each_data_api_instance(monkeypatch):
    _install_fake_transport(monkeypatch)
    official = TushareImporter(token="official", api_url="http://api.tushare.pro")
    mirror = TushareImporter(token="mirror", api_url="https://mirror.example.test/api")
    official_pro, mirror_pro = official._get_pro(), mirror._get_pro()
    assert official_pro._url == "https://api.tushare.pro"
    assert mirror_pro._url == "https://mirror.example.test/api"
    assert not official._call("daily", ts_code="000001.SZ").empty
    assert official_pro._session.calls[0][0] == "https://api.tushare.pro"
    assert official_pro._session.calls[0][1]["json"]["api_name"] == "daily"
    assert official_pro._session.calls[0][1]["allow_redirects"] is False
    assert mirror_pro._session.calls == []


def test_connection_rebuild_keeps_configured_url(monkeypatch):
    _install_fake_transport(monkeypatch)
    importer = TushareImporter(token="token", api_url="https://new.example.test")
    previous = importer._get_pro()
    ok, message = importer.test_connection()
    assert ok is True and "https://new.example.test" in message
    assert importer._pro is not previous
    calls = importer._pro._session.calls
    assert all(call[0] == "https://new.example.test" for call in calls)
    assert [call[1]["json"]["params"]["ts_code"] for call in calls] == ["000001.SZ", "000300.SH"]


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
