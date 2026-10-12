"""Instance-local HTTP transport for official Tushare and compatible gateways.

Do not use Response truthiness: HTTP 4xx must never become NO_DATA.  A formal
empty result is one completed request; it is never followed by an empty probe.
"""
from __future__ import annotations

import socket
from functools import partial
from urllib.parse import urlsplit

import pandas as pd
import requests


OFFICIAL_TUSHARE_API_URL = "https://api.tushare.pro"


class TushareProtocolError(RuntimeError):
    def __init__(self, message: str, code: str = "API_ERROR", *, retryable: bool = False, api_code=None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.api_code = api_code


def network_error_code(error: BaseException) -> str:
    """Inspect wrapped exceptions without inferring authentication from text."""
    pending, seen, errors = [error], set(), []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        errors.append(current)
        pending.extend(value for value in (
            current.__cause__, current.__context__,
            getattr(current, "reason", None), getattr(current, "_reason", None),
            getattr(current, "original_error", None),
            *current.args,
        ) if isinstance(value, BaseException))
    if any(isinstance(value, socket.gaierror) for value in errors):
        return "DNS_RESOLUTION_FAILED"
    if any(isinstance(value, PermissionError) or getattr(value, "winerror", None) == 10013 for value in errors):
        return "NETWORK_ACCESS_DENIED"
    if isinstance(error, requests.ConnectTimeout):
        return "CONNECT_TIMEOUT"
    if isinstance(error, requests.ReadTimeout):
        return "READ_TIMEOUT"
    if isinstance(error, requests.exceptions.SSLError):
        return "TLS_ERROR"
    if any(isinstance(value, ConnectionRefusedError) for value in errors):
        return "CONNECTION_REFUSED"
    return "NETWORK_ERROR"


def safe_text(value: object, token: str) -> str:
    return str(value).replace(token, "***") if token else str(value)


def _safe_metadata(value, token):
    if isinstance(value, str):
        return safe_text(value, token)
    if isinstance(value, dict):
        return {safe_text(key, token): _safe_metadata(item, token) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_metadata(item, token) for item in value]
    return value


class TushareHTTPClient:
    def __init__(self, token: str, api_url: str, *, use_proxy=False, proxy_url="", session=None):
        parsed = urlsplit(api_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Tushare API 地址必须是无凭据、无查询参数的 HTTP(S) 接口地址")
        self._token = token
        self._url = api_url.rstrip("/")
        self._session = session or requests.Session()
        # Configuring one provider must not change QMT/other providers' proxies.
        self._session.trust_env = False
        if use_proxy:
            if not proxy_url:
                raise ValueError("已启用 Tushare 代理，但未配置代理地址")
            self._session.proxies.update({"http": proxy_url, "https": proxy_url})

    def __getattr__(self, api_name):
        # Keep the pro.daily(...) calling convention without the SDK's HTTP
        # status-to-empty behaviour or its process-global proxy settings.
        if api_name.startswith("_"):
            raise AttributeError(api_name)
        return partial(self.query, api_name)

    def close(self):
        self._session.close()

    def query(self, api_name: str, **params) -> pd.DataFrame:
        fields = params.pop("fields", "")
        try:
            response = self._session.post(
                self._url,
                json={"api_name": api_name, "token": self._token, "params": params, "fields": fields},
                headers={"Accept-Encoding": "gzip"}, timeout=(8, 35), allow_redirects=False,
            )
        except requests.RequestException as exc:
            code = network_error_code(exc)
            raise TushareProtocolError(safe_text(exc, self._token), code,
                                       retryable=code != "NETWORK_ACCESS_DENIED") from None
        status = int(response.status_code)
        if status != 200:
            detail = ""
            try:
                body = response.json()
                if isinstance(body, dict):
                    detail = safe_text(body.get("msg", ""), self._token)[:300]
            except ValueError:
                pass
            raise TushareProtocolError(
                f"Tushare {api_name}: HTTP {status}" + (f" ({detail})" if detail else ""), f"HTTP_{status}",
                retryable=status in {408, 429, 500, 502, 503, 504},
            )
        try:
            payload = response.json()
        except ValueError:
            raise TushareProtocolError(f"Tushare {api_name}: 返回非 JSON 数据", "INVALID_RESPONSE") from None
        if (not isinstance(payload, dict) or isinstance(payload.get("code"), bool)
                or payload.get("code") not in (0, "0")):
            message = payload.get("msg", "响应缺少成功 code") if isinstance(payload, dict) else "响应不是对象"
            api_code = payload.get("code") if isinstance(payload, dict) else None
            # Preserve a scalar business code, never arbitrary gateway objects.
            api_code = api_code if isinstance(api_code, (int, str)) and not isinstance(api_code, bool) else None
            if isinstance(api_code, str):
                api_code = safe_text(api_code, self._token)[:100]
            raise TushareProtocolError(f"Tushare {api_name}: {safe_text(message, self._token)}",
                                       "API_ERROR", api_code=api_code)
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("fields"), list) or not isinstance(data.get("items"), list):
            raise TushareProtocolError(f"Tushare {api_name}: 缺少 fields/items", "INVALID_RESPONSE")
        columns, items = data["fields"], data["items"]
        if any(not isinstance(c, str) for c in columns) or len(set(columns)) != len(columns):
            raise TushareProtocolError(f"Tushare {api_name}: 字段无效或重复", "INVALID_RESPONSE")
        if any(not isinstance(row, (list, tuple)) or len(row) != len(columns) for row in items):
            raise TushareProtocolError(f"Tushare {api_name}: 数据行与字段数量不符", "INVALID_RESPONSE")
        metadata = {}
        for container in (payload, data):
            for key in ("meta", "metadata"):
                value = container.get(key)
                if isinstance(value, dict):
                    metadata.update(value)
        # No arbitrary gateway headers/metadata are copied into logs or receipts.
        evidence = {key: _safe_metadata(metadata[key], self._token) for key in (
            "partial", "truncated", "has_more", "coverage", "row_count", "limit",
            "source", "source_time", "source_time_ms", "time_basis", "cache_hit",
            "stale", "stale_reason", "cache_age_seconds",
        ) if key in metadata}
        for key in ("partial", "truncated", "has_more"):
            if str(metadata.get(key, "")).strip().lower() in {"true", "1", "yes"}:
                raise TushareProtocolError(f"Tushare {api_name}: 服务声明 {key}，结果不完整", "DATA_QUALITY")
        if str(metadata.get("coverage", "")).lower() in {"partial", "incomplete", "truncated"}:
            raise TushareProtocolError(f"Tushare {api_name}: 服务声明覆盖不完整", "DATA_QUALITY")
        frame = pd.DataFrame(items, columns=columns)
        frame.attrs["tushare_response_meta"] = {"api_name": api_name, "rows": len(frame), **evidence}
        return frame
