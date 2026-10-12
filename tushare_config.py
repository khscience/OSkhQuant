# coding: utf-8
"""Shared Tushare runtime configuration for GUI, CLI helpers, and strategies."""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass

import kh_settings as global_settings


# 留空表示完全使用 Tushare SDK 自带的数据接口。不要把官网域名当成
# ``DataApi.__http_url``：SDK 会继续在该地址后拼接 ``/daily`` 等接口名。
DEFAULT_TUSHARE_API_URL = ""
_LEGACY_INVALID_API_URLS = frozenset({
    "api.tushare.pro",
    "http://api.tushare.pro",
    "https://api.tushare.pro",
})
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class TushareRuntimeSettings:
    token: str
    use_proxy: bool
    proxy_url: str
    api_url: str

    @property
    def cache_signature(self) -> tuple[str, bool, str, str]:
        token_digest = hashlib.sha256(self.token.encode("utf-8")).hexdigest()
        return token_digest, self.use_proxy, self.proxy_url, self.api_url


def _decode_token(stored_token: object) -> str:
    text = str(stored_token or "").strip()
    if not text:
        return ""
    try:
        return base64.b64decode(text.encode("ascii")).decode("utf-8")
    except Exception:
        return text


def validate_tushare_token(token: object) -> tuple[bool, str]:
    """做不依赖服务端的轻量格式校验，拦截路径、提示语等误存内容。"""
    text = str(token or "").strip()
    if not text:
        return False, "Token 未配置"
    if not _TOKEN_PATTERN.fullmatch(text):
        return False, "Token 格式异常，请重新粘贴纯 Token（不要包含说明文字、路径或空格）"
    return True, ""


def normalize_tushare_api_url(value: object) -> str:
    """规范化共享配置中的 API 地址，并迁移 v3.3.8 的错误官方默认值。"""
    text = str(value or "").replace("：", ":").strip().rstrip("/")
    if text.lower() in _LEGACY_INVALID_API_URLS:
        return ""
    return text


def load_tushare_settings() -> TushareRuntimeSettings:
    # GUI 安装可以顺便迁移旧版 QSettings；纯 CLI/Linux 安装没有 PyQt5，
    # 此时直接读取 JSON 真源，不能让可选 GUI 依赖阻断数据功能。
    try:
        from qt_settings_bridge import KhQtSettings

        KhQtSettings()
    except (ImportError, ModuleNotFoundError):
        pass

    settings = global_settings.load()
    api_url = normalize_tushare_api_url(
        settings.get("tushare_api_url", DEFAULT_TUSHARE_API_URL)
    )
    proxy_url = str(settings.get("tushare_proxy_url", "") or "").replace("：", ":").strip()
    return TushareRuntimeSettings(
        token=_decode_token(settings.get("tushare_token", "")),
        use_proxy=global_settings._coerce_value(
            "tushare_use_proxy",
            settings.get("tushare_use_proxy", False),
        ),
        proxy_url=proxy_url,
        api_url=api_url,
    )
