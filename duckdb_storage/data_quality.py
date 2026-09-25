# coding: utf-8
"""Market-data quality checks shared by download and storage paths."""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


_OHLC_COLUMNS = ("open", "high", "low", "close")
_TRADE_COLUMNS = ("volume", "amount")


class MarketDataQualityError(ValueError):
    """Raised before invalid market data can overwrite stored history."""


def invalid_traded_kline_mask(df: Optional[pd.DataFrame]) -> pd.Series:
    """Return rows that report trading activity but have unusable raw OHLC."""
    if df is None or df.empty or not set(_OHLC_COLUMNS).issubset(df.columns):
        index = getattr(df, "index", None)
        return pd.Series(False, index=index, dtype="bool")

    traded = pd.Series(False, index=df.index, dtype="bool")
    has_trade_measure = False
    for column in _TRADE_COLUMNS:
        if column not in df.columns:
            continue
        has_trade_measure = True
        values = pd.to_numeric(df[column], errors="coerce")
        traded |= values.fillna(0).gt(0)

    if not has_trade_measure or not traded.any():
        return pd.Series(False, index=df.index, dtype="bool")

    prices = df.loc[:, list(_OHLC_COLUMNS)].apply(pd.to_numeric, errors="coerce")
    finite = pd.DataFrame(
        np.isfinite(prices.to_numpy(dtype="float64", na_value=np.nan)),
        index=prices.index,
        columns=prices.columns,
    )
    all_zero = prices.eq(0).all(axis=1)
    missing_or_nonfinite = ~finite.all(axis=1)
    return traded & (all_zero | missing_or_nonfinite)


def validate_kline_quality(
    df: Optional[pd.DataFrame],
    *,
    stock_code: str = "",
    period: str = "",
) -> None:
    """Reject a whole K-line batch before it can partially replace good data."""
    if str(period).lower() == "tick":
        return

    invalid_mask = invalid_traded_kline_mask(df)
    invalid_count = int(invalid_mask.sum())
    if invalid_count <= 0:
        return

    if df is not None and "time" in df.columns:
        sample_values = df.loc[invalid_mask, "time"].head(3)
    else:
        sample_values = invalid_mask[invalid_mask].index[:3]
    sample_text = ", ".join(str(value) for value in sample_values)
    identity = " ".join(value for value in (stock_code, period) if value).strip()
    prefix = f"{identity} " if identity else ""
    raise MarketDataQualityError(
        f"{prefix}数据质量校验失败: 检测到 {invalid_count} 条有成交但 OHLC 全零或无效的K线"
        + (f"，样本时间: {sample_text}" if sample_text else "")
    )
