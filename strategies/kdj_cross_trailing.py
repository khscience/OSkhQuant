# coding: utf-8
"""MACD trend strategy with MA filter and layered loss control."""
import logging
from typing import Dict, List

from khQuantImport import *  # noqa: F401,F403
from MyTT import MA, ATR

logger = logging.getLogger("macd_trend_strategy")

MIN_HISTORY_BARS = 60
SHORT_EMA = 12
LONG_EMA = 26
SIGNAL = 9
BUY_RATIO = 0.95
MAX_POSITION_RATIO = 0.05
ATR_PERIOD = 14
TRAIL_ATR_MULT = 1.0
TRAIL_DROP_PCT = 0.03
PARTIAL_STOP_RATIO = 0.5
STATE_ATTR = "_macd_trend_state"


def _calc_buy_ratio(data: Dict, price: float) -> float:
    cash = khGet(data, "cash") or 0.0
    total = khGet(data, "total_asset") or 0.0
    if cash <= 0 or total <= 0 or price <= 0:
        return 0.0
    intended = cash * BUY_RATIO
    per_stock_cap = total * MAX_POSITION_RATIO
    order_cash = min(intended, per_stock_cap)
    if order_cash <= 0:
        return 0.0
    return max(0.0, min(order_cash / cash, 1.0))


def init(stocks=None, data=None):  # pragma: no cover
    pass


def khHandlebar(data: Dict) -> List[Dict]:
    signals: List[Dict] = []
    stock_list = khGet(data, "stocks") or data.get("stocks") or []
    date_num = khGet(data, "date_num") or data.get("date_num")
    if not stock_list or not date_num:
        return signals

    positions = data.get("__positions__", {}) or {}
    position_count = sum(
        1
        for info in positions.values()
        if info and info.get("volume", 0) > 0
    )
    logger.info(
        "positions on %s: %s",
        khGet(data, "date") or khGet(data, "date_str") or date_num,
        position_count,
    )
    state = _ensure_state(data)
    position_states = state.setdefault("positions", {})
    _cleanup_state(position_states, positions)

    try:
        history = khHistory(
            symbol_list=stock_list,
            fields=["close", "high", "low"],
            bar_count=MIN_HISTORY_BARS,
            fre_step="1d",
            current_time=date_num,
            fq="pre",
            force_download=False,
        )
    except Exception as err:  # pragma: no cover
        logger.warning("failed to load history: %s", err)
        return signals

    for code in stock_list:
        df = history.get(code)
        if df is None or len(df) < MIN_HISTORY_BARS:
            continue
        try:
            close = df["close"].values
        except KeyError:
            continue
        highs = df.get("high").values if "high" in df else None
        lows = df.get("low").values if "low" in df else None
        if len(close) < 60:
            continue
        ma20 = MA(close, 20)[-1]
        ma60 = MA(close, 60)[-1]
        price = float(close[-1])
        if price <= ma60 or ma20 <= ma60:
            continue
        atr_value = _calc_atr(close, highs, lows)
        has_pos = khHas(data, code)
        if has_pos:
            risk_sells = _apply_loss_controls(
                data,
                code,
                price,
                atr_value,
                positions.get(code) or {},
                position_states,
            )
            if risk_sells:
                signals.extend(risk_sells)
                continue

        if not has_pos:
            ratio = _calc_buy_ratio(data, price)
            if ratio <= 0:
                continue
            reason = f"{code[:6]} trend buy"
            signals.extend(generate_signal(data, code, price, ratio, "buy", reason))
    return signals


def _ensure_state(data: Dict) -> Dict:
    framework = data.get("__framework__")
    if framework is not None:
        state = getattr(framework, STATE_ATTR, None)
        if state is None:
            state = {}
            setattr(framework, STATE_ATTR, state)
        return state
    return data.setdefault(STATE_ATTR, {})


def _cleanup_state(position_states: Dict, positions: Dict) -> None:
    for code in list(position_states.keys()):
        if code not in positions:
            position_states.pop(code, None)


def _calc_atr(close, highs, lows) -> float:
    if close is None or highs is None or lows is None:
        return 0.0
    try:
        atr_series = ATR(close, highs, lows, ATR_PERIOD)
        if atr_series is None or len(atr_series) == 0:
            return 0.0
        return float(atr_series[-1])
    except Exception as err:  # pragma: no cover
        logger.debug("ATR calc failed: %s", err)
        return 0.0


def _apply_loss_controls(
    data: Dict,
    code: str,
    price: float,
    atr_value: float,
    position_info: Dict,
    position_states: Dict,
) -> List[Dict]:
    entry_price = float(position_info.get("avg_price") or 0.0)
    if entry_price <= 0:
        return []
    state = position_states.setdefault(
        code, {"highest": entry_price, "reduced_once": False}
    )
    highest = max(float(state.get("highest", entry_price)), price)
    state["highest"] = highest

    reason = ""
    trail_by_pct = highest * (1 - TRAIL_DROP_PCT)
    trail_by_atr = highest - (TRAIL_ATR_MULT * atr_value if atr_value else 0.0)
    trailing_price = max(trail_by_pct, trail_by_atr)
    if price <= trailing_price:
        reason = f"{code[:6]} trail stop {price:.2f}<={trailing_price:.2f}"
    if not reason:
        return []

    reduced_once = state.get("reduced_once", False)
    ratio = 1.0 if reduced_once else PARTIAL_STOP_RATIO
    if ratio <= 0:
        return []
    sell_signals = generate_signal(data, code, price, ratio, "sell", reason)
    if not sell_signals:
        return []
    if reduced_once or ratio >= 1.0:
        position_states.pop(code, None)
    else:
        state["reduced_once"] = True
    return sell_signals
