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

# 接口写法：official = 官方 SDK（POST 到一个地址，token 在请求体）；
# gateway = 服务商常见的「地址/接口名」+ 请求头 X-API-Key。auto 先按官方写法请求，
# 地址返回 404/405（不认这种写法）时改用 gateway，并按地址记住，后续直接用。
TUSHARE_PROTOCOLS = ("auto", "official", "gateway")
GATEWAY_KEY_HEADER = "X-API-Key"
_DETECTED_PROTOCOL: dict = {}

# 服务端声明暂时不可用、可稍后重试的业务错误（区别于权限、参数等永久错误）。
_TRANSIENT_BUSINESS_MARKERS = (
    "upstream_pool_exhausted", "pool_exhausted", "数据当前不可用", "稍后重试",
    "服务繁忙", "系统繁忙", "busy", "temporarily unavailable", "rate limit", "too many requests",
)


def _is_transient_business_error(message: str) -> bool:
    text = str(message or "").lower()
    return any(marker.lower() in text for marker in _TRANSIENT_BUSINESS_MARKERS)


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
    def __init__(self, token: str, api_url: str, *, use_proxy=False, proxy_url="", session=None,
                 protocol: str = "auto"):
        parsed = urlsplit(api_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Tushare API 地址必须是无凭据、无查询参数的 HTTP(S) 接口地址")
        protocol = str(protocol or "auto").strip().lower()
        if protocol not in TUSHARE_PROTOCOLS:
            raise ValueError(f"不支持的 Tushare 接口写法: {protocol}")
        self._token = token
        self._url = api_url
        self._protocol = protocol
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

    @property
    def protocol(self) -> str:
        """当前生效的接口写法；auto 尚未识别时按官方写法。"""
        if self._protocol != "auto":
            return self._protocol
        return _DETECTED_PROTOCOL.get(self._url, "official")

    def _send(self, method: str, url: str, **kwargs):
        sender = self._session.post if method == "POST" else self._session.get
        try:
            return sender(url, timeout=(8, 35), allow_redirects=False, **kwargs)
        except requests.RequestException as exc:
            code = network_error_code(exc)
            raise TushareProtocolError(safe_text(exc, self._token), code,
                                       retryable=code != "NETWORK_ACCESS_DENIED") from None

    def _http_error(self, api_name: str, response) -> TushareProtocolError:
        status = int(response.status_code)
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = safe_text(body.get("msg") or body.get("message") or body.get("error") or "", self._token)[:300]
        except ValueError:
            pass
        return TushareProtocolError(
            f"Tushare {api_name}: HTTP {status}" + (f" ({detail})" if detail else ""), f"HTTP_{status}",
            retryable=status in {408, 429, 500, 502, 503, 504} or _is_transient_business_error(detail),
        )

    def query(self, api_name: str, **params) -> pd.DataFrame:
        fields = params.pop("fields", "")
        if self.protocol == "official":
            response = self._send(
                "POST", self._url,
                json={"api_name": api_name, "token": self._token, "params": params, "fields": fields},
                headers={"Accept-Encoding": "gzip"},
            )
            status = int(response.status_code)
            if status in {404, 405} and self._protocol == "auto":
                # 地址不认官方写法：按服务商的「地址/接口名 + 请求头 Key」写法重发同一请求。
                _DETECTED_PROTOCOL[self._url] = "gateway"
                return self._query_gateway(api_name, params, fields)
            if status != 200:
                raise self._http_error(api_name, response)
            if self._protocol == "auto":
                _DETECTED_PROTOCOL.setdefault(self._url, "official")
            return self._parse_response(api_name, response)
        return self._query_gateway(api_name, params, fields)

    def _query_gateway(self, api_name: str, params: dict, fields) -> pd.DataFrame:
        query = {key: value for key, value in params.items() if value is not None}
        if fields:
            query["fields"] = fields
        response = self._send(
            "GET", f"{self._url.rstrip('/')}/{api_name}", params=query,
            headers={GATEWAY_KEY_HEADER: self._token, "Accept-Encoding": "gzip"},
        )
        if int(response.status_code) != 200:
            raise self._http_error(api_name, response)
        return self._parse_response(api_name, response)

    def _parse_response(self, api_name: str, response) -> pd.DataFrame:
        try:
            payload = response.json()
        except ValueError:
            raise TushareProtocolError(f"Tushare {api_name}: 返回非 JSON 数据", "INVALID_RESPONSE") from None
        if isinstance(payload, dict) and "code" not in payload and "ok" in payload:
            # 服务商写法：{ok, data, message/error}；data 为 {fields, items} 或记录列表。
            if payload.get("ok") is not True:
                message = payload.get("message") or payload.get("msg") or payload.get("error") or "服务返回失败"
                message = safe_text(message, self._token)
                raise TushareProtocolError(f"Tushare {api_name}: {message}", "API_ERROR",
                                           retryable=_is_transient_business_error(message))
            payload = {"code": 0, "msg": "", **{k: v for k, v in payload.items() if k not in {"ok", "code"}}}
            data = payload.get("data")
            if isinstance(data, list) and all(isinstance(row, dict) for row in data):
                columns = list(dict.fromkeys(key for row in data for key in row))
                payload["data"] = {"fields": columns, "items": [[row.get(c) for c in columns] for row in data]}
            elif isinstance(data, dict) and isinstance(data.get("items"), list) and data["items"] \
                    and all(isinstance(row, dict) for row in data["items"]) and not isinstance(data.get("fields"), list):
                columns = list(dict.fromkeys(key for row in data["items"] for key in row))
                payload["data"] = {**data, "fields": columns,
                                   "items": [[row.get(c) for c in columns] for row in data["items"]]}
        if (not isinstance(payload, dict) or isinstance(payload.get("code"), bool)
                or payload.get("code") not in (0, "0")):
            message = payload.get("msg", "响应缺少成功 code") if isinstance(payload, dict) else "响应不是对象"
            api_code = payload.get("code") if isinstance(payload, dict) else None
            # Preserve a scalar business code, never arbitrary gateway objects.
            api_code = api_code if isinstance(api_code, (int, str)) and not isinstance(api_code, bool) else None
            if isinstance(api_code, str):
                api_code = safe_text(api_code, self._token)[:100]
            message = safe_text(message, self._token)
            raise TushareProtocolError(f"Tushare {api_name}: {message}",
                                       "API_ERROR", api_code=api_code,
                                       retryable=_is_transient_business_error(message))
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
