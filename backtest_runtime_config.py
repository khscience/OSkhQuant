# coding: utf-8
"""Shared GUI/CLI runtime config merging for backtests.

The user's .kh file remains the strategy-level config. System-level settings
from ~/.khquant_os/settings.json provide runtime defaults for matching
behavior and performance features; the data source is always DuckDB. Explicit .kh performance or
dynamic_load blocks keep taking precedence for backward compatibility.
"""
from __future__ import annotations

import copy
import os
from typing import Any, Mapping

import kh_settings as global_settings
from performance_config import (
    DEFAULT_PERFORMANCE_CONFIG,
    normalize_memory_profile,
    resolve_memory_profile_from_config,
)


PERFORMANCE_SETTING_MAP = {
    "performance_memory_profile": "memory_profile",
    "performance_memory_auto_dynamic_load": "memory_auto_dynamic_load",
    "performance_memory_low_chunk_size": "memory_low_chunk_size",
    "performance_memory_ultra_low_chunk_size": "memory_ultra_low_chunk_size",
    "performance_khhistory_cache_mode": "khhistory_cache_mode",
    "performance_khhistory_prefetch_end": "khhistory_prefetch_end",
    "performance_khhistory_memory_fastpath": "khhistory_memory_fastpath",
    "performance_khhistory_missing_data_prompt": "khhistory_missing_data_prompt",
    "performance_empty_data_log_mode": "empty_data_log_mode",
    "performance_duckdb_order_mode": "duckdb_order_mode",
    "performance_duckdb_load_batch_size": "duckdb_load_batch_size",
    "performance_duckdb_parallel_read_workers": "duckdb_parallel_read_workers",
    "performance_duckdb_load_max_connections": "duckdb_load_max_connections",
    "performance_duckdb_max_connections": "duckdb_max_connections",
    "performance_framework_raw_duckdb_load": "framework_raw_duckdb_load",
    "performance_framework_history_preload_days": "framework_history_preload_days",
    "performance_parquet_cache_pack": "parquet_cache_pack",
    "performance_parquet_cache_pack_root": "parquet_cache_pack_root",
    "performance_parquet_cache_pack_batch_size": "parquet_cache_pack_batch_size",
    "performance_parquet_cache_pack_workers": "parquet_cache_pack_workers",
    "performance_parquet_cache_pack_compression": "parquet_cache_pack_compression",
}

RUNTIME_SETTING_KEYS = (
    "backtest_data_source",
    "duckdb_data_path",
    "volume_limit_enabled",
    "participation_rate",
    "allow_partial_fill",
    "init_data_enabled",
)

RUNTIME_PERFORMANCE_KEYS = {
    "memory_profile_effective",
    "memory_profile_decision",
    "memory_profile_retry_from",
    "memory_profile_retry_reason",
    "memory_profile_retry_attempt",
}


def _setting_is_non_default(settings_cfg: Mapping[str, Any], settings_key: str) -> bool:
    if settings_key not in settings_cfg:
        return False
    if settings_key not in global_settings.DEFAULTS:
        return True
    return settings_cfg.get(settings_key) != global_settings.DEFAULTS.get(settings_key)


def performance_overrides_from_settings(settings_cfg: Mapping[str, Any] | None = None) -> dict:
    """Return only system performance settings changed from their defaults.

    Avoiding default injection matters: KhConfig distinguishes explicit
    performance keys from defaults when applying low-memory derived settings.
    """
    cfg = settings_cfg or global_settings.load()
    overrides = {}
    for settings_key, perf_key in PERFORMANCE_SETTING_MAP.items():
        if not _setting_is_non_default(cfg, settings_key):
            continue
        value = cfg.get(settings_key)
        if value == "" and DEFAULT_PERFORMANCE_CONFIG.get(perf_key, "") == "":
            continue
        overrides[perf_key] = value
    return overrides


def build_headless_settings(
    settings_cfg: Mapping[str, Any] | None = None,
    *,
    duckdb_data_path: str | None = None,
    include_callbacks: Mapping[str, Any] | None = None,
    include_runtime_settings: Mapping[str, Any] | None = None,
) -> dict:
    cfg = settings_cfg or global_settings.load()
    data_source = global_settings.normalize_data_source(cfg.get("backtest_data_source", "duckdb"))
    headless = {
        "backtest_data_source": data_source,
        "duckdb_data_path": duckdb_data_path if duckdb_data_path is not None else cfg.get("duckdb_data_path", ""),
        "init_data_enabled": cfg.get("init_data_enabled", True),
        "volume_limit_enabled": cfg.get("volume_limit_enabled", False),
        "participation_rate": cfg.get("participation_rate", 0.1),
        "allow_partial_fill": cfg.get("allow_partial_fill", True),
    }
    if include_runtime_settings:
        headless.update(dict(include_runtime_settings))
    if include_callbacks:
        headless.update(include_callbacks)
    return headless


def apply_system_runtime_settings(
    config: Mapping[str, Any],
    settings_cfg: Mapping[str, Any] | None = None,
    *,
    force_performance_overrides: Mapping[str, Any] | None = None,
) -> dict:
    """Merge system-level runtime settings into a copy of a strategy config.

    Precedence:
      CLI one-shot overrides > explicit .kh performance/dynamic_load >
      changed system defaults > framework defaults.
    """
    cfg = copy.deepcopy(dict(config or {}))
    sys_cfg = settings_cfg or global_settings.load()

    performance = cfg.setdefault("performance", {})
    for perf_key, value in performance_overrides_from_settings(sys_cfg).items():
        performance.setdefault(perf_key, value)
    if force_performance_overrides:
        performance.update(dict(force_performance_overrides))

    return cfg


def strip_runtime_config(
    config: Mapping[str, Any] | None,
    *,
    explicit_performance_keys: set[str] | None = None,
    explicit_dynamic_load_keys: set[str] | None = None,
) -> dict:
    """Remove runtime-only fields before persisting a strategy .kh file."""
    cfg = copy.deepcopy(dict(config or {}))
    performance = cfg.get("performance")
    if isinstance(performance, Mapping):
        cleaned = dict(performance)
        for key in RUNTIME_PERFORMANCE_KEYS:
            cleaned.pop(key, None)
        if explicit_performance_keys is not None:
            cleaned = {key: value for key, value in cleaned.items() if key in explicit_performance_keys}
        if cleaned:
            cfg["performance"] = cleaned
        else:
            cfg.pop("performance", None)
    dynamic_load = cfg.get("dynamic_load")
    if isinstance(dynamic_load, Mapping) and explicit_dynamic_load_keys is not None:
        cleaned_dynamic = {
            key: value for key, value in dict(dynamic_load).items() if key in explicit_dynamic_load_keys
        }
        if cleaned_dynamic:
            cfg["dynamic_load"] = cleaned_dynamic
        else:
            cfg.pop("dynamic_load", None)
    return cfg


def preserve_strategy_runtime_blocks(
    new_config: Mapping[str, Any] | None,
    old_config: Mapping[str, Any] | None,
) -> dict:
    """Carry explicit strategy-level performance/dynamic_load blocks forward.

    The GUI rebuilds config dictionaries from widgets. These blocks are not
    edited in the GUI because the canonical performance controls are
    system-level settings, so preserve any hand-written strategy overrides.
    """
    cfg = strip_runtime_config(new_config or {})
    old = strip_runtime_config(old_config or {})
    for key in ("performance", "dynamic_load"):
        value = old.get(key)
        if isinstance(value, Mapping) and value:
            cfg[key] = copy.deepcopy(dict(value))
    return cfg


def stamp_memory_decision(config: Mapping[str, Any], config_path: str | None = None) -> tuple[dict, str, dict]:
    cfg = copy.deepcopy(dict(config or {}))
    perf = cfg.setdefault("performance", {})
    effective, decision = resolve_memory_profile_from_config(cfg, config_path=config_path)
    perf["memory_profile"] = normalize_memory_profile(perf.get("memory_profile", "auto"))
    perf["memory_profile_effective"] = effective
    perf["memory_profile_decision"] = decision
    return cfg, effective, decision


def is_memory_error(exc: BaseException) -> bool:
    if isinstance(exc, MemoryError):
        return True
    cls_name = exc.__class__.__name__.lower()
    mod_name = exc.__class__.__module__.lower()
    text = f"{cls_name} {mod_name} {exc}".lower()
    patterns = (
        "memoryerror",
        "arraymemoryerror",
        "unable to allocate",
        "out of memory",
        "cannot allocate memory",
        "failed to allocate",
        "bad allocation",
        "内存不足",
        "无法分配",
    )
    return any(p in text for p in patterns)


def write_temp_runtime_config(config: Mapping[str, Any], config_dir: str, base_name: str, tag: str = "") -> str:
    import json
    import tempfile

    tag = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in str(tag or "").strip())
    tag_part = f"{tag}_" if tag else ""
    base_suffix = "".join(
        ch if ch.isalnum() or ch in ("_", "-", ".") else "_"
        for ch in os.path.basename(base_name)
    )
    fd, tmp_path = tempfile.mkstemp(
        prefix=f"_tmp_{tag_part}",
        suffix=f"_{base_suffix}",
        dir=config_dir,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dict(config), f, ensure_ascii=False, indent=2)
    return tmp_path
