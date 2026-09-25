"""baostock_proxy 解析域名时必须先用系统 DNS，8.8.8.8 只作后备。"""
import socket

import pytest

import baostock_proxy


def test_system_dns_first_and_google_not_queried(monkeypatch):
    calls = []
    monkeypatch.setattr(baostock_proxy.socket, "gethostbyname", lambda host: calls.append("system") or "1.2.3.4")
    monkeypatch.setattr(baostock_proxy, "_resolve_google_dns", lambda host: calls.append("google") or "8.8.4.4")

    assert baostock_proxy._resolve_host("public-api.baostock.com") == "1.2.3.4"
    assert calls == ["system"]


def test_google_dns_used_only_after_system_failure(monkeypatch):
    calls = []

    def system_fail(host):
        calls.append("system")
        raise socket.gaierror("no such host")

    monkeypatch.setattr(baostock_proxy.socket, "gethostbyname", system_fail)
    monkeypatch.setattr(baostock_proxy, "_resolve_google_dns", lambda host: calls.append("google") or "5.6.7.8")

    assert baostock_proxy._resolve_host("public-api.baostock.com") == "5.6.7.8"
    assert calls == ["system", "google"]


def test_both_fail_raises_system_error(monkeypatch):
    def system_fail(host):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(baostock_proxy.socket, "gethostbyname", system_fail)
    monkeypatch.setattr(baostock_proxy, "_resolve_google_dns", lambda host: None)

    with pytest.raises(socket.gaierror):
        baostock_proxy._resolve_host("public-api.baostock.com")


def test_generic_proxy_host_env(monkeypatch):
    for key in ("BAOSTOCK_PROXY_URL", "ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KHQUANT_PROXY_HOST", "10.0.0.2")

    assert list(baostock_proxy._iter_proxy_candidates()) == ["http://10.0.0.2:7890"]
