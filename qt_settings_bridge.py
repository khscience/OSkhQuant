# coding: utf-8
"""Bridge Qt UI preferences and shared KhQuant settings.

QSettings is still used for window geometry and GUI-only state. Settings that
affect backtest behavior are routed to ~/.khquant/settings.json so GUI and CLI
share one source of truth.
"""
from __future__ import annotations

import json
import os
from typing import Any

from PyQt5.QtCore import QSettings

import kh_settings as global_settings


SHARED_SETTING_KEYS = set(global_settings.DEFAULTS.keys()) | {
    "account_id",
    "account_type",
}

TUSHARE_SETTING_KEYS = set(global_settings.TUSHARE_SETTING_KEYS)

_TUSHARE_MIGRATION_MARKER = "_shared_tushare_settings_migrated_v1"


class KhQtSettings:
    def __init__(self, organization: str = "KHQuant", application: str = "StockAnalyzer"):
        self._qsettings = QSettings(organization, application)
        self._migrate_legacy_shared_settings()

    @property
    def qsettings(self) -> QSettings:
        return self._qsettings

    def load(self) -> dict:
        return global_settings.load()

    def value(self, key: str, default: Any = None, type=None):  # noqa: A002 - Qt API compatibility
        if key in SHARED_SETTING_KEYS:
            value = global_settings.load().get(key, default)
        else:
            if type is None:
                value = self._qsettings.value(key, default)
            else:
                value = self._qsettings.value(key, default, type=type)
        if type is None or value is None:
            return value
        try:
            if type is bool and isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "y")
            return type(value)
        except Exception:
            return default

    def setValue(self, key: str, value: Any):
        if key in SHARED_SETTING_KEYS:
            global_settings.set_value(key, value)
            # 保持旧安装版/旧模块仍能读取到最新 Tushare 配置。新代码只把
            # JSON 当作真源，这里的 QSettings 写入仅用于向后兼容。
            if key in TUSHARE_SETTING_KEYS:
                normalized = global_settings.get(key, value)
                self._qsettings.setValue(key, normalized)
                self._qsettings.setValue(_TUSHARE_MIGRATION_MARKER, True)
                self._qsettings.sync()
            return
        self._qsettings.setValue(key, value)

    def sync(self):
        self._qsettings.sync()

    def __getattr__(self, name: str):
        return getattr(self._qsettings, name)

    def _migrate_legacy_shared_settings(self):
        try:
            # Mac 旧版配置位于 ~/.khquant。先通过共享设置层完成位置迁移，
            # 再执行 QSettings 的字段迁移，避免默认值覆盖旧配置。
            global_settings.load()
            os.makedirs(global_settings.SETTINGS_DIR, exist_ok=True)
            if os.path.exists(global_settings.SETTINGS_FILE):
                with open(global_settings.SETTINGS_FILE, "r", encoding="utf-8-sig") as f:
                    raw_cfg = json.load(f)
                    if not isinstance(raw_cfg, dict):
                        raw_cfg = {}
            else:
                raw_cfg = {}

            changed = False
            cfg = dict(global_settings.DEFAULTS)
            cfg.update(raw_cfg)

            # 旧版只把 Tushare 配置写入注册表。首次升级时恢复仍有效的
            # Token/代理，但绝不让旧 URL 覆盖 JSON 中已经保存的新 URL。
            migrated_tushare = self._qsettings.value(
                _TUSHARE_MIGRATION_MARKER,
                False,
                type=bool,
            )
            if not migrated_tushare:
                raw_explicit = raw_cfg.get(global_settings._TUSHARE_EXPLICIT_KEYS_KEY, [])
                explicit_keys = set(raw_explicit if isinstance(raw_explicit, list) else [])
                legacy = {
                    key: self._qsettings.value(key)
                    for key in TUSHARE_SETTING_KEYS
                    if self._qsettings.contains(key)
                }
                if (
                    "tushare_token" not in explicit_keys
                    and not str(raw_cfg.get("tushare_token") or "").strip()
                    and legacy.get("tushare_token")
                ):
                    cfg["tushare_token"] = legacy["tushare_token"]
                    changed = True
                if (
                    "tushare_api_url" not in explicit_keys
                    and not str(raw_cfg.get("tushare_api_url") or "").strip()
                    and legacy.get("tushare_api_url")
                ):
                    cfg["tushare_api_url"] = legacy["tushare_api_url"]
                    changed = True
                if (
                    "tushare_proxy_url" not in explicit_keys
                    and not str(raw_cfg.get("tushare_proxy_url") or "").strip()
                    and legacy.get("tushare_proxy_url")
                ):
                    cfg["tushare_proxy_url"] = legacy["tushare_proxy_url"]
                    if "tushare_use_proxy" not in explicit_keys and "tushare_use_proxy" not in raw_cfg:
                        cfg["tushare_use_proxy"] = legacy.get("tushare_use_proxy", False)
                    changed = True

            for key in SHARED_SETTING_KEYS - TUSHARE_SETTING_KEYS:
                if key in raw_cfg or not self._qsettings.contains(key):
                    continue
                value = self._qsettings.value(key)
                if value is None:
                    continue
                cfg[key] = global_settings._coerce_value(key, value)
                changed = True
            normalized_cfg, normalized_changed = global_settings._normalize_loaded_settings(cfg)
            if normalized_changed:
                cfg = normalized_cfg
                changed = True
            if changed or not os.path.exists(global_settings.SETTINGS_FILE):
                global_settings.save(cfg)

            # JSON 是唯一真源；同步回注册表只为兼容仍在运行的旧模块，并
            # 防止升级期间同一台机器上的新旧版本读到不同值。
            effective = global_settings.load()
            for key in TUSHARE_SETTING_KEYS:
                self._qsettings.setValue(key, effective.get(key, global_settings.DEFAULTS.get(key)))
            self._qsettings.setValue(_TUSHARE_MIGRATION_MARKER, True)
            self._qsettings.sync()
        except Exception:
            pass
