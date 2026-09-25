# coding: utf-8
"""全局配置管理 (~/.khquant_os/settings.json)

开源版只读写自己的配置文件，绝不读写 CS 版的 ~/.khquant/settings.json。
- 回测数据源固定为 DuckDB：旧配置里存的任何数据源名都按 duckdb 处理；
- 读取时不回写文件，遇到不认识的旧键也不报错；
- 性能预设相关函数与会影响成交的默认值（成交量限制、参与率、部分成交）
  与 CS 版保持一致，由构建脚本从主库原样取来。
"""
from __future__ import annotations

import os
import json
import sys
import tempfile

from kh_revisioned_config import RevisionedConfigStore

from performance_config import (
    DEFAULT_PERFORMANCE_CONFIG,
    normalize_performance_preset,
    performance_preset_settings,
)
from data_integrity_policy import normalize_integrity_mode
from kh_app_identity import SETTINGS_DIR, SETTINGS_FILE

SETTINGS_SCHEMA_VERSION = 6
_SETTINGS_SCHEMA_KEY = "__schema_version__"
_TUSHARE_EXPLICIT_KEYS_KEY = "__tushare_explicit_keys_v1__"
_INTERNAL_SETTING_KEYS = {
    _SETTINGS_SCHEMA_KEY,
    _TUSHARE_EXPLICIT_KEYS_KEY,
}
TUSHARE_SETTING_KEYS = frozenset({
    "tushare_token",
    "tushare_api_url",
    "tushare_use_proxy",
    "tushare_proxy_url",
})
_LEGACY_MEMORY_LOW_CHUNK_SIZE = 5
_LEGACY_MEMORY_ULTRA_LOW_CHUNK_SIZE = 1

DEFAULTS = {
    "backtest_data_source": "duckdb",
    "config_revision": 0,
    "duckdb_data_path": "",
    "strategy_dir": "",
    "tushare_token": "",
    # 留空时使用 Tushare SDK 自带的数据接口；仅镜像/私有服务需要显式填写。
    "tushare_api_url": "",
    "tushare_use_proxy": False,
    "tushare_proxy_url": "",
    "baostock_enabled": False,
    "risk_free_rate": 0.03,
    "volume_limit_enabled": False,
    "participation_rate": 0.1,
    "allow_partial_fill": True,
    "account_id": "",
    "account_type": "STOCK",
    "delay_log_display": True,
    "max_log_lines": 1000,
    "stop_exit_immediately": True,
    "check_data_integrity": True,
    "check_data_integrity_mode": "auto",
    "init_data_enabled": True,

    # 系统级回测性能设置。旧 .kh 中的 performance/dynamic_load 显式配置仍可覆盖这些默认值。
    "performance_preset": DEFAULT_PERFORMANCE_CONFIG["preset"],
    "performance_detail_customized": False,
    "performance_memory_profile": DEFAULT_PERFORMANCE_CONFIG["memory_profile"],
    "performance_memory_auto_dynamic_load": DEFAULT_PERFORMANCE_CONFIG["memory_auto_dynamic_load"],
    "performance_memory_low_chunk_size": DEFAULT_PERFORMANCE_CONFIG["memory_low_chunk_size"],
    "performance_memory_ultra_low_chunk_size": DEFAULT_PERFORMANCE_CONFIG["memory_ultra_low_chunk_size"],
    "performance_khhistory_cache_mode": DEFAULT_PERFORMANCE_CONFIG["khhistory_cache_mode"],
    "performance_khhistory_prefetch_end": DEFAULT_PERFORMANCE_CONFIG["khhistory_prefetch_end"],
    "performance_khhistory_memory_fastpath": DEFAULT_PERFORMANCE_CONFIG["khhistory_memory_fastpath"],
    # 开源版默认打开：V2.1 的 khHistory 缺数据会自动下载，2.2 返回空，提示能让用户及时发现
    "performance_khhistory_missing_data_prompt": True,
    "performance_empty_data_log_mode": DEFAULT_PERFORMANCE_CONFIG["empty_data_log_mode"],
    "performance_duckdb_order_mode": DEFAULT_PERFORMANCE_CONFIG["duckdb_order_mode"],
    "performance_duckdb_load_batch_size": DEFAULT_PERFORMANCE_CONFIG["duckdb_load_batch_size"],
    "performance_duckdb_parallel_read_workers": DEFAULT_PERFORMANCE_CONFIG["duckdb_parallel_read_workers"],
    "performance_duckdb_load_max_connections": DEFAULT_PERFORMANCE_CONFIG["duckdb_load_max_connections"],
    "performance_duckdb_max_connections": DEFAULT_PERFORMANCE_CONFIG["duckdb_max_connections"],
    "performance_framework_raw_duckdb_load": DEFAULT_PERFORMANCE_CONFIG["framework_raw_duckdb_load"],
    "performance_framework_history_preload_days": DEFAULT_PERFORMANCE_CONFIG["framework_history_preload_days"],
    "performance_parquet_cache_pack": DEFAULT_PERFORMANCE_CONFIG["parquet_cache_pack"],
    "performance_parquet_cache_pack_root": DEFAULT_PERFORMANCE_CONFIG["parquet_cache_pack_root"],
    "performance_parquet_cache_pack_batch_size": DEFAULT_PERFORMANCE_CONFIG["parquet_cache_pack_batch_size"],
    "performance_parquet_cache_pack_workers": DEFAULT_PERFORMANCE_CONFIG["parquet_cache_pack_workers"],
    "performance_parquet_cache_pack_compression": DEFAULT_PERFORMANCE_CONFIG["parquet_cache_pack_compression"],
}

BOOL_KEYS = {
    "volume_limit_enabled",
    "tushare_use_proxy",
    "allow_partial_fill",
    "baostock_enabled",
    "delay_log_display",
    "stop_exit_immediately",
    "check_data_integrity",
    "init_data_enabled",
    "performance_memory_auto_dynamic_load",
    "performance_khhistory_memory_fastpath",
    "performance_khhistory_missing_data_prompt",
    "performance_framework_raw_duckdb_load",
    "performance_detail_customized",
}

FLOAT_KEYS = {
    "risk_free_rate",
    "participation_rate",
}

INT_KEYS = {
    "max_log_lines",
    "performance_memory_low_chunk_size",
    "performance_memory_ultra_low_chunk_size",
    "performance_framework_history_preload_days",
    "performance_duckdb_parallel_read_workers",
    "performance_duckdb_load_max_connections",
    "performance_duckdb_max_connections",
}

STRING_INT_KEYS = {
    "performance_duckdb_load_batch_size",
    "performance_parquet_cache_pack_batch_size",
    "performance_parquet_cache_pack_workers",
}

PERFORMANCE_PRESET_SETTING_MAP = {
    "memory_profile": "performance_memory_profile",
    "memory_auto_dynamic_load": "performance_memory_auto_dynamic_load",
    "memory_low_chunk_size": "performance_memory_low_chunk_size",
    "memory_ultra_low_chunk_size": "performance_memory_ultra_low_chunk_size",
    "khhistory_cache_mode": "performance_khhistory_cache_mode",
    "khhistory_prefetch_end": "performance_khhistory_prefetch_end",
    "khhistory_memory_fastpath": "performance_khhistory_memory_fastpath",
    "empty_data_log_mode": "performance_empty_data_log_mode",
    "duckdb_order_mode": "performance_duckdb_order_mode",
    "duckdb_load_batch_size": "performance_duckdb_load_batch_size",
    "duckdb_parallel_read_workers": "performance_duckdb_parallel_read_workers",
    "duckdb_load_max_connections": "performance_duckdb_load_max_connections",
    "duckdb_max_connections": "performance_duckdb_max_connections",
    "framework_raw_duckdb_load": "performance_framework_raw_duckdb_load",
    "framework_history_preload_days": "performance_framework_history_preload_days",
}


def normalize_data_source(value) -> str:
    """开源版只读 DuckDB。旧配置里的 xtdata / miniqmt / qmt_native 等一律当作 duckdb。"""
    return "duckdb"


def canonical_setting_key(key: object) -> str:
    return str(key or "").strip()


def _coerce_value(key: str, value):
    if key == "performance_preset":
        return normalize_performance_preset(value)
    if key == "backtest_data_source":
        return normalize_data_source(value)
    if key == "check_data_integrity_mode":
        return normalize_integrity_mode(value, legacy_enabled=True)
    if key in FLOAT_KEYS:
        return float(value)
    if key in INT_KEYS:
        return int(value)
    if key in STRING_INT_KEYS:
        text = str(value).strip()
        return "auto" if text.lower() in ("", "auto", "default") else str(int(text))
    if key in BOOL_KEYS:
        return str(value).lower() in ("true", "1", "yes", "on", "y")
    return value


def apply_performance_preset(cfg: dict, preset: str) -> dict:
    normalized = normalize_performance_preset(preset)
    result = dict(cfg or {})
    result["performance_preset"] = normalized
    result["performance_detail_customized"] = False
    for perf_key, value in performance_preset_settings(normalized).items():
        settings_key = PERFORMANCE_PRESET_SETTING_MAP.get(perf_key)
        if settings_key:
            result[settings_key] = _coerce_value(settings_key, value)
    return result


def _preset_detail_values(preset: str) -> dict:
    values = {}
    for perf_key, value in performance_preset_settings(preset).items():
        settings_key = PERFORMANCE_PRESET_SETTING_MAP.get(perf_key)
        if settings_key:
            values[settings_key] = _coerce_value(settings_key, value)
    return values


def _details_match_preset(cfg: dict, preset: str) -> bool:
    data = dict(DEFAULTS)
    data.update(cfg or {})
    for key, value in _preset_detail_values(preset).items():
        if data.get(key) != value:
            return False
    return True


def _sync_preset_details(cfg: dict, *, force: bool = False) -> tuple[dict, bool]:
    result = dict(cfg or {})
    preset = normalize_performance_preset(
        result.get("performance_preset") or infer_performance_preset(result)
    )
    changed = False
    if result.get("performance_preset") != preset:
        result["performance_preset"] = preset
        changed = True

    customized = bool(result.get("performance_detail_customized", False))
    if force or not customized:
        for key, value in _preset_detail_values(preset).items():
            if result.get(key, DEFAULTS.get(key)) != value:
                result[key] = value
                changed = True
        if result.get("performance_detail_customized") is not False:
            result["performance_detail_customized"] = False
            changed = True
        return result, changed

    still_customized = not _details_match_preset(result, preset)
    if result.get("performance_detail_customized") != still_customized:
        result["performance_detail_customized"] = still_customized
        changed = True
    return result, changed


def infer_performance_preset(cfg: dict | None) -> str:
    data = dict(cfg or {})
    explicit = data.get("performance_preset")
    if explicit:
        return normalize_performance_preset(explicit)

    profile = str(data.get("performance_memory_profile", "")).strip().lower()
    prefetch = str(data.get("performance_khhistory_prefetch_end", "")).strip().lower()
    # 新预设: 全量档 = memory_profile=standard(其余键=默认), 故 standard 即判为全量/performance。
    if profile == "standard":
        return "performance"
    if profile in ("low", "ultra_low"):
        return "low_memory"
    if prefetch == "backtest_end":
        return "performance"
    return "balanced"


def _coerce_schema_version(value) -> int:
    try:
        return max(int(value), 0)
    except Exception:
        return 0


def _normalize_loaded_settings(data: dict | None) -> tuple[dict, bool]:
    cfg = dict(data or {})
    changed = False
    raw_version = _coerce_schema_version(cfg.get(_SETTINGS_SCHEMA_KEY, 0))
    target_version = max(raw_version, SETTINGS_SCHEMA_VERSION)

    if raw_version < SETTINGS_SCHEMA_VERSION:
        if cfg.get("performance_memory_low_chunk_size") == _LEGACY_MEMORY_LOW_CHUNK_SIZE:
            cfg["performance_memory_low_chunk_size"] = DEFAULT_PERFORMANCE_CONFIG["memory_low_chunk_size"]
            changed = True
        if cfg.get("performance_memory_ultra_low_chunk_size") == _LEGACY_MEMORY_ULTRA_LOW_CHUNK_SIZE:
            cfg["performance_memory_ultra_low_chunk_size"] = DEFAULT_PERFORMANCE_CONFIG[
                "memory_ultra_low_chunk_size"
            ]
            changed = True
        if raw_version < 3:
            preset_source = dict(DEFAULTS)
            preset_source.update(cfg)
            cfg, preset_changed = _sync_preset_details(
                cfg,
                force=not bool(preset_source.get("performance_detail_customized", False)),
            )
            changed = changed or preset_changed
        changed = True

    if cfg.get(_SETTINGS_SCHEMA_KEY) != target_version:
        cfg[_SETTINGS_SCHEMA_KEY] = target_version
        changed = True

    if "backtest_data_source" in cfg:
        normalized_source = normalize_data_source(cfg.get("backtest_data_source"))
        if cfg.get("backtest_data_source") != normalized_source:
            cfg["backtest_data_source"] = normalized_source
            changed = True

    merged_for_check = dict(DEFAULTS)
    merged_for_check.update(cfg)
    cfg, preset_changed = _sync_preset_details(
        cfg,
        force=not bool(merged_for_check.get("performance_detail_customized", False)),
    )
    changed = changed or preset_changed

    return cfg, changed


def _strip_internal_keys(data: dict | None) -> dict:
    cfg = dict(data or {})
    for key in _INTERNAL_SETTING_KEYS:
        cfg.pop(key, None)
    return cfg


def _read_raw_settings() -> dict:
    settings_file = _settings_source_file()
    if not os.path.exists(settings_file):
        return {}
    try:
        with open(settings_file, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _settings_source_file() -> str:
    return SETTINGS_FILE


def is_initialized():
    return os.path.exists(_settings_source_file())


def load():
    """读取配置并与默认值合并。只在内存中标准化，不回写文件。"""
    settings_file = _settings_source_file()
    if not os.path.exists(settings_file):
        return dict(DEFAULTS)
    try:
        with open(settings_file, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        # 文件损坏时按默认配置运行，不因为配置问题阻止回测。
        return dict(DEFAULTS)
    if not isinstance(data, dict):
        data = {}
    normalized, _changed = _normalize_loaded_settings(data)
    merged = dict(DEFAULTS)
    merged.update(normalized)
    merged["performance_preset"] = infer_performance_preset(merged)
    if not bool(merged.get("performance_detail_customized", False)):
        merged = apply_performance_preset(merged, merged["performance_preset"])
    else:
        merged["performance_detail_customized"] = not _details_match_preset(
            merged, merged["performance_preset"]
        )
    merged["backtest_data_source"] = normalize_data_source(merged.get("backtest_data_source"))
    return _strip_internal_keys(merged)


def save(cfg: dict, *, expected_revision: int | None = None):
    """合并写入：文件里已有、本次没传的键会保留；原子替换落盘。"""
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    normalized = dict(cfg or {})
    if expected_revision is None and "config_revision" in normalized:
        candidate_revision = int(normalized.get("config_revision") or 0)
        # revision=0 也是DEFAULTS的兼容占位值，不能把首次/旧调用误判为
        # 明确CAS；从load()取得的真实配置 revision 始终大于0。
        if candidate_revision > 0:
            expected_revision = candidate_revision
    persisted = _read_raw_settings()
    for key in _INTERNAL_SETTING_KEYS:
        if key not in normalized and key in persisted:
            normalized[key] = persisted[key]
    if "backtest_data_source" in normalized:
        normalized["backtest_data_source"] = normalize_data_source(normalized.get("backtest_data_source"))
    normalized[_SETTINGS_SCHEMA_KEY] = max(
        _coerce_schema_version(normalized.get(_SETTINGS_SCHEMA_KEY)),
        SETTINGS_SCHEMA_VERSION,
    )
    store = RevisionedConfigStore(SETTINGS_FILE)
    return store.merge(normalized, expected_revision=expected_revision)


def get(key: str, default=None):
    canonical = canonical_setting_key(key)
    return load().get(canonical, default)


def set_value(key: str, value):
    key = canonical_setting_key(key)
    cfg = load()
    expected_revision = int(cfg.get("config_revision") or 0)
    if key == "performance_preset":
        cfg = apply_performance_preset(cfg, value)
    elif key == "performance_detail_customized":
        customized = _coerce_value(key, value)
        if customized:
            cfg[key] = True
        else:
            cfg = apply_performance_preset(cfg, cfg.get("performance_preset", DEFAULTS["performance_preset"]))
    else:
        cfg[key] = _coerce_value(key, value)
        if key in PERFORMANCE_PRESET_SETTING_MAP.values():
            preset = normalize_performance_preset(cfg.get("performance_preset", DEFAULTS["performance_preset"]))
            cfg["performance_detail_customized"] = not _details_match_preset(cfg, preset)
    if key in TUSHARE_SETTING_KEYS:
        raw_explicit = _read_raw_settings().get(_TUSHARE_EXPLICIT_KEYS_KEY, [])
        explicit_keys = set(raw_explicit if isinstance(raw_explicit, list) else [])
        explicit_keys.add(key)
        cfg[_TUSHARE_EXPLICIT_KEYS_KEY] = sorted(explicit_keys)
    save(cfg, expected_revision=expected_revision)
