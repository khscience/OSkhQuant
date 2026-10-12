# coding: utf-8
"""Shared market-data time normalization helpers."""
from __future__ import annotations

from datetime import timedelta, timezone
import re
from typing import Any

import numpy as np
import pandas as pd


_CALENDAR_FORMATS = {
    8: "%Y%m%d",
    12: "%Y%m%d%H%M",
    14: "%Y%m%d%H%M%S",
}
_TIMEZONE_SUFFIX = re.compile(r"(?:[zZ]|[+-]\d{2}:?\d{2}|(?:UTC|GMT))\s*$")


def _empty_datetime_series(source: pd.Series) -> pd.Series:
    return pd.Series(pd.NaT, index=source.index, dtype="datetime64[ns]", name=source.name)


def _normalize_aware_time(
    parsed: pd.Series,
    *,
    epoch_offset_hours: int,
) -> pd.Series:
    """Convert timezone-aware values to the same local-naive basis as epochs."""
    if isinstance(parsed.dtype, pd.DatetimeTZDtype):
        target_tz = timezone(timedelta(hours=int(epoch_offset_hours)))
        return parsed.dt.tz_convert(target_tz).dt.tz_localize(None)
    return parsed


def _has_explicit_timezone(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_TIMEZONE_SUFFIX.search(value.strip()))
    tzinfo = getattr(value, "tzinfo", None)
    if tzinfo is None:
        return False
    try:
        return value.utcoffset() is not None
    except Exception:
        return False


def _coerce_numeric_market_time(
    numeric: pd.Series,
    *,
    epoch_offset_hours: int,
) -> pd.Series:
    """Parse numeric calendar encodings and Unix timestamps without digit ambiguity."""
    result = _empty_datetime_series(numeric)
    if numeric.empty:
        return result

    numeric_float = pd.to_numeric(numeric, errors="coerce")
    finite_values = numeric_float.to_numpy(dtype="float64", na_value=np.nan)
    finite_mask = pd.Series(np.isfinite(finite_values), index=numeric.index)
    if not finite_mask.any():
        return result

    rounded = numeric_float.round()
    integer_mask = finite_mask & ((numeric_float - rounded).abs() < 1e-6)
    integer_text = pd.Series(pd.NA, index=numeric.index, dtype="string")
    integer_text.loc[integer_mask] = rounded.loc[integer_mask].astype("int64").astype(str)

    # A 12-digit value is only a calendar encoding when its leading year is
    # plausible. Old epoch milliseconds (for example 885398400000) must fall
    # through to the magnitude-based epoch parser.
    lengths = integer_text.str.len()
    years = pd.to_numeric(integer_text.str.slice(0, 4), errors="coerce")
    calendar_intent = integer_mask & lengths.isin(tuple(_CALENDAR_FORMATS)) & years.between(1900, 2200)
    for length, fmt in _CALENDAR_FORMATS.items():
        mask = calendar_intent & lengths.eq(length)
        if mask.any():
            result.loc[mask] = pd.to_datetime(integer_text.loc[mask], format=fmt, errors="coerce")

    remaining = finite_mask & ~calendar_intent
    absolute = numeric_float.abs()
    unit_masks = (
        ("ns", remaining & absolute.ge(1e17)),
        ("us", remaining & absolute.ge(1e14) & absolute.lt(1e17)),
        ("ms", remaining & absolute.ge(1e11) & absolute.lt(1e14)),
        ("s", remaining & absolute.ge(1e8) & absolute.lt(1e11)),
    )
    offset = pd.Timedelta(hours=int(epoch_offset_hours))
    for unit, mask in unit_masks:
        if mask.any():
            parsed = pd.to_datetime(numeric_float.loc[mask], unit=unit, errors="coerce")
            result.loc[mask] = parsed + offset

    return result


def coerce_market_time(
    values: Any,
    *,
    epoch_offset_hours: int = 8,
) -> pd.Series:
    """Convert market-data times to a naive ``datetime64[ns]`` Series.

    Supported inputs include pandas datetimes, calendar encodings
    (YYYYMMDD, YYYYMMDDHHMM, YYYYMMDDHHMMSS), Unix seconds/milliseconds/
    microseconds/nanoseconds, numeric strings, and ordinary date strings.
    The returned Series preserves the source Series index to avoid assignment
    alignment bugs in DataFrames.
    """
    source = values.copy() if isinstance(values, pd.Series) else pd.Series(values)
    if source.empty:
        return _empty_datetime_series(source)

    if pd.api.types.is_datetime64_any_dtype(source.dtype):
        parsed = pd.to_datetime(source, errors="coerce")
        parsed = _normalize_aware_time(
            parsed,
            epoch_offset_hours=epoch_offset_hours,
        )
        parsed.name = source.name
        return parsed

    result = _empty_datetime_series(source)
    numeric = pd.to_numeric(source, errors="coerce")
    numeric_mask = numeric.notna()
    if numeric_mask.any():
        parsed_numeric = _coerce_numeric_market_time(
            numeric.loc[numeric_mask],
            epoch_offset_hours=epoch_offset_hours,
        )
        result.loc[numeric_mask] = parsed_numeric

    text_mask = source.notna() & ~numeric_mask
    if text_mask.any():
        text_values = source.loc[text_mask]
        aware_mask = text_values.map(_has_explicit_timezone)
        if aware_mask.any():
            parsed_aware = pd.to_datetime(
                text_values.loc[aware_mask],
                errors="coerce",
                utc=True,
            )
            parsed_aware = _normalize_aware_time(
                parsed_aware,
                epoch_offset_hours=epoch_offset_hours,
            )
            result.loc[parsed_aware.index] = parsed_aware

        naive_values = text_values.loc[~aware_mask]
        if not naive_values.empty:
            result.loc[naive_values.index] = pd.to_datetime(
                naive_values,
                errors="coerce",
            )

    return result
