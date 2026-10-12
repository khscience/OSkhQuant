# coding: utf-8
"""Shared adjusted-price selection policy for DuckDB readers.

Ratio-adjusted and ordinary adjusted prices use different coordinate systems.
They must never be spliced row by row within one requested price series.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np
import pandas as pd


OHLC_FIELDS = ("open", "high", "low", "close")
DIVIDEND_TYPES = ("front", "back", "front_ratio", "back_ratio")
RATIO_DIVIDEND_TYPES = ("front_ratio", "back_ratio")

_warning_lock = threading.Lock()
_warned_fallbacks = set()


def _warn_once(dividend_type: str, fallback_mode: str, message: str, *args) -> None:
    """Emit one warning per adjustment type and fallback mode per process."""
    key = (dividend_type, fallback_mode)
    with _warning_lock:
        if key in _warned_fallbacks:
            return
        _warned_fallbacks.add(key)
    logging.warning(message, *args)


def select_dividend_fields(
    df: pd.DataFrame,
    dividend_type: str,
    *,
    context: Optional[str] = None,
) -> pd.DataFrame:
    """Select a single, internally consistent OHLC adjustment family.

    For ``front_ratio``/``back_ratio`` the exact ratio family is used only
    when all four OHLC columns exist and contain no missing or non-finite
    value in the requested interval. Otherwise the *entire* interval falls
    back to the corresponding ordinary ``front``/``back`` family. If neither
    family has all four columns, the original OHLC columns are left untouched
    rather than creating a mixed series.
    """
    if df is None or df.empty:
        return df

    dt = (dividend_type or "none").lower()
    if dt not in DIVIDEND_TYPES:
        return df

    out = df.copy()
    if dt not in RATIO_DIVIDEND_TYPES:
        adjusted_columns = {field: f"{field}_{dt}" for field in OHLC_FIELDS}
        if any(column not in df.columns for column in adjusted_columns.values()):
            return df
        for field, column in adjusted_columns.items():
            out[field] = df[column]
        return out

    base_dt = dt.replace("_ratio", "")
    ratio_columns = {field: f"{field}_{dt}" for field in OHLC_FIELDS}
    fallback_columns = {field: f"{field}_{base_dt}" for field in OHLC_FIELDS}

    ratio_schema_complete = all(column in df.columns for column in ratio_columns.values())
    ratio_values = None
    ratio_invalid_rows = None
    if ratio_schema_complete:
        ratio_values = df[list(ratio_columns.values())].apply(
            pd.to_numeric, errors="coerce"
        )
        ratio_invalid_rows = ~np.isfinite(
            ratio_values.to_numpy(dtype="float64")
        ).all(axis=1)
    ratio_values_complete = bool(
        ratio_schema_complete and not ratio_invalid_rows.any()
    )

    if ratio_values_complete:
        for field, column in ratio_columns.items():
            out[field] = ratio_values[column]
        return out

    fallback_schema_complete = all(
        column in df.columns for column in fallback_columns.values()
    )
    if fallback_schema_complete:
        for field, column in fallback_columns.items():
            out[field] = df[column]
        if context:
            missing_columns = sum(
                column not in df.columns for column in ratio_columns.values()
            )
            missing_rows = 0
            if ratio_schema_complete:
                missing_rows = int(ratio_invalid_rows.sum())
            _warn_once(
                dt,
                "ordinary_adjustment",
                "%s 的 %s 数据在请求区间内不完整（缺列 %d，含缺失/无效值行 %d）；"
                "为保持价格口径一致，整个区间统一回退到 %s。后续同类提示不再重复。",
                context,
                dt,
                missing_columns,
                missing_rows,
                base_dt,
            )
        return out

    if context:
        _warn_once(
            dt,
            "raw_ohlc",
            "%s 的 %s 与 %s 字段都不完整；为避免混合复权口径，本次保留原始 OHLC。"
            "请重新补充该区间的复权数据。后续同类提示不再重复。",
            context,
            dt,
            base_dt,
        )
    return df
