# coding: utf-8
"""Tushare 服务商地址兼容与分钟线时间标注（2026-10-19，开源版 2.2 发布前）。

1. 服务商给的地址常是「地址/接口名 + 请求头 X-API-Key」写法，官方写法 POST 到该地址返回 404；
   自动识别后改用服务商写法，两种返回格式都认，暂时不可用的错误可重试并按建议秒数等待。
2. 部分服务商分钟线按开始时间标注（5m 09:30…14:55、1m 09:30…14:59），导入时统一为收盘时刻标注；
   1m 没有 09:30 那一根的 240 根算完整；按开始时间标注存进库的旧数据只识别、不重下、不混写。
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

import duckdb_storage.tushare_transport as transport
from duckdb_storage.incremental import (
    build_incremental_plan,
    is_complete_bar_day,
    validate_overwrite_frame,
)
from duckdb_storage.manager import DuckDBManager
from duckdb_storage.stock_db import StockDB
from duckdb_storage.tushare_importer import TushareImporter, align_minute_labels_to_bar_end
from duckdb_storage.tushare_transport import TushareHTTPClient, TushareProtocolError

STOCK = "000011.SZ"
DAY = "2026-10-12"
DAY8 = "20261012"
NOW = datetime(2026, 10, 19, 16, 0)


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Session:
    def __init__(self, post_responses=(), get_responses=()):
        self.post_responses = list(post_responses)
        self.get_responses = list(get_responses)
        self.posts, self.gets = [], []
        self.trust_env = True
        self.proxies = {}

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.post_responses.pop(0)

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.get_responses.pop(0)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_detected_protocol():
    transport._DETECTED_PROTOCOL.clear()
    yield
    transport._DETECTED_PROTOCOL.clear()


# ── 接口写法自动识别 ───────────────────────────────────────────────────

def test_auto_switches_to_gateway_after_404_and_remembers_it():
    ok = {"ok": True, "data": {"fields": ["trade_date", "close"], "items": [["20261012", 11.97]]}}
    session = _Session(post_responses=[_Resp(404, {"detail": "Not Found"})],
                       get_responses=[_Resp(200, ok), _Resp(200, ok)])
    client = TushareHTTPClient("tsr-key", "https://gw.example/tushare/pro", session=session)
    frame = client.query("daily", ts_code="000001.SZ", start_date="20261012", fields="")
    assert list(frame.columns) == ["trade_date", "close"]
    url, kwargs = session.gets[0]
    assert url == "https://gw.example/tushare/pro/daily"
    assert kwargs["headers"]["X-API-Key"] == "tsr-key"
    assert kwargs["params"] == {"ts_code": "000001.SZ", "start_date": "20261012"}
    assert kwargs["allow_redirects"] is False
    assert client.protocol == "gateway"
    # 第二次直接走服务商写法，不再先发官方请求。
    client.query("daily", ts_code="000001.SZ")
    assert len(session.posts) == 1 and len(session.gets) == 2


def test_gateway_accepts_record_list_and_official_payloads():
    records = {"ok": True, "data": [{"trade_time": "2026-10-12 09:35:00", "close": 11.8},
                                    {"trade_time": "2026-10-12 09:40:00", "close": 11.85}]}
    official_like = {"code": 0, "msg": "", "data": {"fields": ["a"], "items": [[1]]}}
    session = _Session(get_responses=[_Resp(200, records), _Resp(200, official_like)])
    client = TushareHTTPClient("k", "https://gw.example/pro", session=session, protocol="gateway")
    frame = client.query("stk_mins", ts_code=STOCK, freq="5min")
    assert frame["trade_time"].tolist() == ["2026-10-12 09:35:00", "2026-10-12 09:40:00"]
    assert client.query("daily").iloc[0]["a"] == 1


def test_gateway_errors_are_classified():
    busy = {"ok": False, "error": "upstream_pool_exhausted", "message": "数据当前不可用，请稍后重试；建议等待30秒后重试；request_id=x"}
    unauthorized = {"ok": False, "error": "unauthorized", "message": "缺少或使用了无效的 X-API-Key"}
    session = _Session(get_responses=[_Resp(200, busy), _Resp(401, unauthorized)])
    client = TushareHTTPClient("k", "https://gw.example/pro", session=session, protocol="gateway")
    with pytest.raises(TushareProtocolError) as busy_error:
        client.query("adj_factor", ts_code=STOCK)
    assert busy_error.value.retryable is True
    with pytest.raises(TushareProtocolError) as auth_error:
        client.query("daily")
    assert auth_error.value.code == "HTTP_401" and auth_error.value.retryable is False
    assert "X-API-Key" in str(auth_error.value)


def test_official_business_busy_error_is_retryable():
    session = _Session(post_responses=[_Resp(200, {"code": 50001, "msg": "[upstream_pool_exhausted] 数据当前不可用，请稍后重试"})])
    client = TushareHTTPClient("k", "https://gw.example/sdk", session=session)
    with pytest.raises(TushareProtocolError) as error:
        client.query("adj_factor")
    assert error.value.retryable is True


def test_explicit_official_protocol_does_not_fall_back():
    session = _Session(post_responses=[_Resp(404, {"detail": "Not Found"})])
    client = TushareHTTPClient("k", "https://gw.example/pro", session=session, protocol="official")
    with pytest.raises(TushareProtocolError) as error:
        client.query("daily")
    assert error.value.code == "HTTP_404" and session.gets == []


def test_importer_retries_busy_error_with_suggested_wait(monkeypatch):
    monkeypatch.setenv("KH_TUSHARE_API_ATTEMPTS", "3")
    monkeypatch.setenv("KH_TUSHARE_API_RETRY_DELAY", "0.5")
    sleeps = []
    monkeypatch.setattr("duckdb_storage.tushare_importer.time.sleep", lambda seconds: sleeps.append(seconds))
    busy = {"ok": False, "error": "upstream_pool_exhausted", "message": "数据当前不可用；建议等待30秒后重试"}
    ok = {"ok": True, "data": {"fields": ["trade_date", "adj_factor"], "items": [["20261012", 1.0]]}}
    session = _Session(get_responses=[_Resp(200, busy), _Resp(200, ok)])
    importer = TushareImporter(token="k" * 20, api_url="https://gw.example/pro")
    importer._pro = TushareHTTPClient(importer.token, importer.api_url, session=session, protocol="gateway")
    frame = importer._call("adj_factor", ts_code=STOCK)
    assert len(frame) == 1
    assert sleeps and sleeps[0] == pytest.approx(30.0)


# ── 分钟线时间标注 ─────────────────────────────────────────────────────

def _start_labeled_5m(day=DAY):
    return list(pd.date_range(f"{day} 09:30", f"{day} 11:25", freq="5min")) + \
        list(pd.date_range(f"{day} 13:00", f"{day} 14:55", freq="5min"))


def _end_labeled_5m(day=DAY):
    return list(pd.date_range(f"{day} 09:35", f"{day} 11:30", freq="5min")) + \
        list(pd.date_range(f"{day} 13:05", f"{day} 15:00", freq="5min"))


def _start_labeled_1m(day=DAY):
    return list(pd.date_range(f"{day} 09:30", f"{day} 11:29", freq="1min")) + \
        list(pd.date_range(f"{day} 13:00", f"{day} 14:59", freq="1min"))


def _end_labeled_1m(day=DAY, with_auction=False):
    times = list(pd.date_range(f"{day} 09:31", f"{day} 11:30", freq="1min")) + \
        list(pd.date_range(f"{day} 13:01", f"{day} 15:00", freq="1min"))
    return ([pd.Timestamp(f"{day} 09:30")] if with_auction else []) + times


def test_start_labeled_5m_and_1m_are_shifted_to_bar_end():
    aligned, shifted = align_minute_labels_to_bar_end(pd.Series(_start_labeled_5m()), "5min")
    assert shifted == 1 and list(aligned) == _end_labeled_5m()
    aligned, shifted = align_minute_labels_to_bar_end(pd.Series(_start_labeled_1m()), "1min")
    assert shifted == 1 and list(aligned) == _end_labeled_1m()


def test_end_labeled_official_data_is_unchanged():
    official_5m = [pd.Timestamp(f"{DAY} 09:30")] + _end_labeled_5m()
    aligned, shifted = align_minute_labels_to_bar_end(pd.Series(official_5m), "5min")
    assert shifted == 0 and list(aligned) == official_5m
    aligned, shifted = align_minute_labels_to_bar_end(pd.Series(_end_labeled_1m(with_auction=True)), "1min")
    assert shifted == 0


def test_hourly_start_labels_are_shifted():
    start = [pd.Timestamp(f"{DAY} {t}") for t in ("09:30", "10:30", "13:00", "14:00")]
    aligned, shifted = align_minute_labels_to_bar_end(pd.Series(start), "60min")
    assert shifted == 1
    assert [t.strftime("%H:%M") for t in aligned] == ["10:30", "11:30", "14:00", "15:00"]


class _FakePro:
    """按请求区间返回某服务商按开始时间标注的 5 分钟线。"""

    def __init__(self, times):
        self.times = times

    def stk_mins(self, ts_code, freq, start_date, end_date, **_kwargs):
        start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
        rows = [t for t in self.times if start <= t <= end]
        return pd.DataFrame({
            "ts_code": [ts_code] * len(rows),
            "trade_time": [t.strftime("%Y-%m-%d %H:%M:%S") for t in rows],
            "open": [10.0] * len(rows), "high": [10.2] * len(rows), "low": [9.9] * len(rows),
            "close": [10.1] * len(rows), "vol": [1000.0] * len(rows), "amount": [10100.0] * len(rows),
        })

    def close(self):
        pass


def test_download_minutes_returns_bar_end_labels():
    importer = TushareImporter(token="k" * 20, api_url="https://gw.example/pro")
    importer._pro = _FakePro(_start_labeled_5m("2026-10-12") + _start_labeled_5m("2026-10-13"))
    frame = importer.download_minutes(STOCK, "5min", "2026-10-12 09:00:00", "2026-10-13 15:30:00")
    assert len(frame) == 96
    days = frame["time"].dt.normalize()
    assert frame.groupby(days)["time"].min().dt.strftime("%H:%M").tolist() == ["09:35", "09:35"]
    assert frame.groupby(days)["time"].max().dt.strftime("%H:%M").tolist() == ["15:00", "15:00"]
    assert not (frame["time"].dt.strftime("%H:%M") == "13:00").any()


# ── 1m 240 根与旧标注识别 ─────────────────────────────────────────────

@pytest.mark.parametrize(
    ("total", "valid", "opening", "complete"),
    [(241, 241, 1, True), (240, 240, 0, True), (240, 240, 1, False), (240, 239, 0, False), (240, 240, None, False)],
)
def test_one_minute_day_completeness(total, valid, opening, complete):
    assert is_complete_bar_day("1m", total, valid, opening, opening) is complete


class _CoverageManager:
    def __init__(self, coverage):
        self.coverage = coverage

    def get_existing_date_completeness(self, *_args, **_kwargs):
        return self.coverage


def test_planner_keeps_legacy_start_labeled_days_without_redownload():
    manager = _CoverageManager({
        DAY8: {"total_rows": 240, "valid_rows": 240, "opening_rows": 1, "opening_valid_rows": 1,
               "start_label_rows": 1},
    })
    plan = build_incremental_plan(manager, STOCK, "1m", DAY8, DAY8, trade_days=[DAY8], now=NOW)
    assert plan.legacy_label_dates == (DAY8,)
    assert plan.complete_dates == (DAY8,)
    assert plan.download_ranges == ()


def _bars(times):
    count = len(times)
    return pd.DataFrame({
        "time": pd.to_datetime(times), "open": [10.0] * count, "high": [10.2] * count,
        "low": [9.9] * count, "close": [10.1] * count, "volume": [10.0] * count, "amount": [101.0] * count,
    })


def test_overwrite_validation_accepts_240_one_minute_bars_without_auction():
    validate_overwrite_frame(_bars(_end_labeled_1m()), "1m", (DAY8,))
    missing_other = _end_labeled_1m(with_auction=True)
    del missing_other[100]
    with pytest.raises(ValueError, match="拒绝整日覆写"):
        validate_overwrite_frame(_bars(missing_other), "1m", (DAY8,))


@pytest.fixture()
def manager(tmp_path):
    mgr = DuckDBManager(data_root=str(tmp_path))
    try:
        yield mgr
    finally:
        mgr.close_all_no_checkpoint()


def test_manager_stats_mark_start_labeled_and_accept_aligned_one_minute(manager):
    manager.save_kline_data(df=_bars(_start_labeled_1m("2026-10-12")), stock_code=STOCK, period="1m",
                            dividend_type="none", skip_metadata=True)
    manager.save_kline_data(df=_bars(_end_labeled_1m("2026-10-13")), stock_code=STOCK, period="1m",
                            dividend_type="none", skip_metadata=True)
    coverage = manager.get_existing_date_completeness(
        STOCK, "1m", raise_on_error=True, start_date="20261012", end_date="20261013")
    assert coverage["20261012"]["start_label_rows"] == 1
    assert coverage["20261013"] == {"total_rows": 240, "valid_rows": 240, "opening_rows": 0,
                                    "opening_valid_rows": 0, "start_label_rows": 0}
    plan = build_incremental_plan(manager, STOCK, "1m", "20261012", "20261013",
                                  trade_days=["20261012", "20261013"], now=NOW)
    assert plan.legacy_label_dates == ("20261012",)
    assert plan.download_ranges == ()


def test_stock_db_whole_day_overwrite_accepts_aligned_one_minute(tmp_path):
    db = StockDB(STOCK, str(tmp_path))
    try:
        db.save_kline(_bars(_start_labeled_1m()), "1m", overwrite=False, merge_missing=True)
        db.save_kline(_bars(_end_labeled_1m()), "1m", overwrite=True, overwrite_trade_dates=True)
        saved = pd.to_datetime(db.get_kline("1m", fields=["time"])["time"])
        assert len(saved) == 240
        assert saved.min().strftime("%H:%M") == "09:31" and saved.max().strftime("%H:%M") == "15:00"
    finally:
        db.close(skip_checkpoint=True)
