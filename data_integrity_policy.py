# coding: utf-8
"""Policy helpers for pre-backtest data integrity checks."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any


HIGH_FREQ_PERIODS = {"tick", "1m", "5m", "15m", "30m", "60m", "1h"}

# The GUI integrity check opens and scans one DuckDB file per stock. For very
# large pools this can cost minutes before the backtest actually starts, while
# the backtest engine already reports missing data in summary form.
LARGE_POOL_STOCK_THRESHOLD = 1000
HIGH_FREQ_STOCK_THRESHOLD = 500
ESTIMATED_BAR_SCAN_THRESHOLD = 250_000


@dataclass(frozen=True)
class IntegrityCheckDecision:
    should_run: bool
    reason: str = ""
    stock_count: int = 0
    period: str = ""
    estimated_trading_days: int | None = None
    estimated_bars: int | None = None


def normalize_integrity_mode(value: Any, legacy_enabled: bool = True) -> str:
    if not legacy_enabled:
        return "off"
    if isinstance(value, bool):
        return "auto" if value else "off"
    raw = str(value if value is not None else "auto").strip().lower().replace("-", "_")
    aliases = {
        "true": "auto",
        "yes": "auto",
        "on": "auto",
        "1": "auto",
        "false": "off",
        "no": "off",
        "off": "off",
        "0": "off",
        "always": "full",
        "force": "full",
        "forced": "full",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in {"auto", "full", "off"} else "auto"


def _parse_yyyymmdd(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip().replace("-", "")
    if len(text) < 8:
        return None
    text = text[:8]
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except Exception:
        return None


def estimate_trading_days(start_date: Any, end_date: Any) -> int | None:
    start = _parse_yyyymmdd(start_date)
    end = _parse_yyyymmdd(end_date)
    if start is None or end is None or end < start:
        return None
    days = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return max(days, 1)


def estimate_bars_per_day(period: Any) -> int:
    text = str(period or "").strip().lower()
    return {
        "tick": 2000,
        "1m": 240,
        "5m": 48,
        "15m": 16,
        "30m": 8,
        "60m": 4,
        "1h": 4,
        "1d": 1,
    }.get(text, 1)


def should_run_integrity_check(
    *,
    mode: Any,
    legacy_enabled: bool,
    stock_count: int,
    period: Any,
    start_date: Any,
    end_date: Any,
) -> IntegrityCheckDecision:
    normalized_mode = normalize_integrity_mode(mode, legacy_enabled)
    period_text = str(period or "").strip().lower()
    trading_days = estimate_trading_days(start_date, end_date)
    bars_per_day = estimate_bars_per_day(period_text)
    estimated_bars = None
    if trading_days is not None and stock_count > 0:
        estimated_bars = stock_count * trading_days * bars_per_day

    base = {
        "stock_count": int(stock_count or 0),
        "period": period_text,
        "estimated_trading_days": trading_days,
        "estimated_bars": estimated_bars,
    }

    if normalized_mode == "off":
        return IntegrityCheckDecision(False, "disabled", **base)
    if normalized_mode == "full":
        return IntegrityCheckDecision(True, "forced_full", **base)

    if stock_count >= LARGE_POOL_STOCK_THRESHOLD:
        return IntegrityCheckDecision(False, "large_pool", **base)
    if period_text in HIGH_FREQ_PERIODS and stock_count >= HIGH_FREQ_STOCK_THRESHOLD:
        return IntegrityCheckDecision(False, "large_high_frequency_pool", **base)
    if estimated_bars is not None and estimated_bars >= ESTIMATED_BAR_SCAN_THRESHOLD:
        return IntegrityCheckDecision(False, "large_estimated_scan", **base)

    return IntegrityCheckDecision(True, "auto_small_enough", **base)
