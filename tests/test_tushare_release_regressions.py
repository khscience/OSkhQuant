"""Official/custom transport errors, asset routes, and financial unit regressions."""
from types import SimpleNamespace

import pandas as pd
import pytest

from duckdb_storage.tushare_importer import TushareImporter, TushareAPIError
from duckdb_storage.tushare_transport import TushareHTTPClient
from security_type_utils import is_convertible_bond_code


class Session:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload if payload is not None else {'code': 0, 'data': {'fields': [], 'items': []}}
        self.calls, self.proxies = [], {}
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(status_code=self.status, json=lambda: self.payload)
    def close(self):
        pass


@pytest.mark.parametrize('api_url', ['', 'https://gateway.example/sdk'])
@pytest.mark.parametrize('status,attempts', [(301, 1), (403, 1), (429, 3), (500, 3), (503, 3)])
def test_http_failure_never_becomes_empty(monkeypatch, api_url, status, attempts):
    import duckdb_storage.tushare_transport as transport
    session = Session(status, {'msg': 'test-token server error'})
    monkeypatch.setattr(transport.requests, 'Session', lambda: session)
    monkeypatch.setenv('KH_TUSHARE_API_ATTEMPTS', '3')
    monkeypatch.setenv('KH_TUSHARE_API_RETRY_DELAY', '0')
    importer = TushareImporter('test-token', api_url=api_url)
    with pytest.raises(TushareAPIError) as caught:
        importer._call('daily')
    assert caught.value.code == f'HTTP_{status}'
    assert len(session.calls) == attempts
    assert 'test-token' not in str(caught.value)
    assert all(call[1]['allow_redirects'] is False for call in session.calls)


@pytest.mark.parametrize('api_url', ['', 'https://gateway.example/sdk'])
def test_formal_empty_has_one_request_and_no_probe(monkeypatch, api_url):
    import duckdb_storage.tushare_transport as transport
    session = Session()
    monkeypatch.setattr(transport.requests, 'Session', lambda: session)
    importer = TushareImporter('test-token', api_url=api_url)
    assert importer._call('daily').empty
    assert len(session.calls) == importer.request_count == 1


@pytest.mark.parametrize('payload', [
    {'code': 2002, 'msg': 'test-token no permission'},
    {'code': 0, 'data': None},
    {'code': 0, 'data': {'fields': ['close'], 'items': [[1, 2]]}},
    {'code': 0, 'data': {'fields': [], 'items': []}, 'meta': {'truncated': True}},
])
def test_business_or_malformed_response_is_failure(monkeypatch, payload):
    monkeypatch.setenv('KH_TUSHARE_API_ATTEMPTS', '1')
    importer = TushareImporter('test-token')
    session = Session(payload=payload)
    importer._pro = TushareHTTPClient('test-token', 'https://api.tushare.pro', session=session)
    with pytest.raises(TushareAPIError) as caught:
        importer._call('daily')
    assert 'test-token' not in str(caught.value)
    assert len(session.calls) == 1


def market_frame():
    return pd.DataFrame([{'trade_date': '20260901', 'open': 10., 'high': 11., 'low': 9.,
                          'close': 10.5, 'vol': 123.45, 'amount': 12.5}])


@pytest.mark.parametrize('code,api,amount', [
    ('000001.SZ', 'daily', 12500), ('688001.SH', 'daily', 12500),
    ('510300.SH', 'fund_daily', 12500), ('160119.SZ', 'fund_daily', 12500),
    ('113001.SH', 'cb_daily', 125000), ('123001.SZ', 'cb_daily', 125000),
])
def test_daily_routes_and_units(monkeypatch, code, api, amount):
    importer, calls = TushareImporter('token'), []
    def call(name, **kwargs):
        calls.append(name)
        return market_frame()
    monkeypatch.setattr(importer, '_call', call)
    frame = importer.download_daily(code, '20260901', '20260901')
    assert calls == [api]
    assert frame['volume'].tolist() == [123.45]
    assert frame['amount'].tolist() == [amount]
    normalized_again = importer._normalize_daily(frame, amount_scale=10000)
    assert normalized_again['volume'].tolist() == [123.45]
    assert normalized_again['amount'].tolist() == [amount]
    importer.get_latest_trade_date(code)
    assert calls[-1] == api


@pytest.mark.parametrize('code,api', [('000001.SZ', 'stk_mins'), ('510300.SH', 'etf_mins'), ('159001.SZ', 'etf_mins')])
@pytest.mark.parametrize('freq', ['1min', '5min'])
def test_minutes_routes_and_fractional_lots(monkeypatch, code, api, freq):
    importer, calls = TushareImporter('token'), []
    def call(name, **kwargs):
        calls.append(name)
        frame = market_frame().rename(columns={'trade_date': 'trade_time'})
        frame['trade_time'] = '2026-09-01 09:35:00'
        frame['vol'] = 1234.5
        return frame
    monkeypatch.setattr(importer, '_call', call)
    frame = importer.download_minutes(code, freq, '2026-09-01 09:30:00', '2026-09-01 15:00:00')
    assert calls == [api]
    assert frame['volume'].iloc[0] == pytest.approx(12.345)
    assert frame['amount'].iloc[0] == 12.5
    assert importer._normalize_minutes(frame)['volume'].iloc[0] == pytest.approx(12.345)


@pytest.mark.parametrize('code', ['113001.SH', '123001.SZ', '160119.SZ', '000300.SH'])
def test_unsupported_minutes_are_explicit_without_network(monkeypatch, code):
    importer = TushareImporter('token')
    monkeypatch.setattr(importer, '_call', lambda *args, **kwargs: pytest.fail('wrong API must not be called'))
    with pytest.raises(TushareAPIError):
        importer.download_minutes(code, '1min', '2026-09-01 09:30:00', '2026-09-01 15:00:00')


def test_bond_batch_respects_2000_row_limit_and_no_stock_factor(monkeypatch):
    importer, spans = TushareImporter('token'), []
    def call(api, **kwargs):
        assert api == 'cb_daily'
        spans.append((pd.Timestamp(kwargs['end_date']) - pd.Timestamp(kwargs['start_date'])).days + 1)
        return pd.DataFrame()
    monkeypatch.setattr(importer, '_call', call)
    assert importer.download_daily('113001.SH', '20000101', '20261001').empty
    assert len(spans) > 1 and max(spans) < 2000
    with pytest.raises(TushareAPIError, match='不复权日线'):
        importer.download_adj_factor('113001.SH')


@pytest.mark.parametrize('code', ['000001.SZ', '000300.SH', '510300.SH', '113001.SZ', '123001.SH', '113001.BJ', '110'])
def test_bond_classifier_does_not_capture_other_securities(code):
    assert not is_convertible_bond_code(code)
