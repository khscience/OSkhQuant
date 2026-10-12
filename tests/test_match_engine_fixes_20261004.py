# coding: utf-8
"""撮合/执行层修复回归（2026-10-04）

覆盖：挂单成交量限制(2)、挂单执行失败拒单(附A)、撮合字段补齐与缺列警告(4)、
限价越界拒单(7)、市价剩余量挂单(12)、部分成交剩余保留原限价(附B)、
部分成交副本进记录缓冲区(10)、滑点统一与限价封顶(13)、止损成交价统一(16)、
止损限价触发后以限价单入队(17)。
"""
import logging
from types import SimpleNamespace

import pytest

from khFrame import KhQuantFramework, PendingOrderManager, OrderStatus, _stop_order_fill_price
from khTrade import KhTradeManager


DATE = {"date": "2025-01-02", "timestamp": 1735779600}
NEXT = {"date": "2025-01-03", "timestamp": 1735866000}


def _trade_mgr(cash=1_000_000.0, slippage=None):
    config = SimpleNamespace(
        config_dict={"backtest": {"trade_cost": {
            "min_commission": 0.0, "commission_rate": 0.0, "stamp_tax_rate": 0.0,
            "flow_fee": 0.0, "transfer_fee_enabled": False,
            "slippage": slippage or {"type": "tick", "tick_size": 0.01, "tick_count": 0},
        }}},
        run_mode="backtest",
        account_id="test",
    )
    tm = KhTradeManager(config)
    tm.assets = {"cash": cash, "total_asset": cash, "market_value": 0.0}
    return tm


def _framework(volume_limit=None, cash=1_000_000.0, slippage=None, dividend_type="none"):
    fw = KhQuantFramework.__new__(KhQuantFramework)
    fw.price_decimals = 2
    fw._etf_total_return_enabled = False
    fw._match_config = {
        "price_limit": {"enabled": True},
        "volume_limit": volume_limit or {"enabled": False},
    }
    fw.pending_order_mgr = PendingOrderManager(fw._match_config)
    fw.trade_mgr = _trade_mgr(cash, slippage)
    fw.config = SimpleNamespace(config_dict={"data": {"dividend_type": dividend_type}})
    return fw


def _bar(open_=10.0, high=10.5, low=9.8, close=10.2, volume=1000, pre_close=10.0):
    bar = {"open": open_, "high": high, "low": low, "close": close, "volume": volume}
    if pre_close is not None:
        bar["preClose"] = pre_close
    return bar


def _run_pending(fw, data, time_info):
    """模拟主循环：检查挂单 → 逐个执行成交候选"""
    candidates = fw.pending_order_mgr.check_pending_orders(
        data, time_info["timestamp"], time_info["date"], fw._match_config)
    results = []
    for order in candidates:
        results.append((order, fw.trade_mgr.execute_pending_fill(order, data)))
    return results


def _only_order(fw):
    orders = list(fw.pending_order_mgr._pending_orders.values())
    assert len(orders) == 1
    return orders[0]


# ---------------- 2：挂单成交受成交量参与率限制 ----------------
def test_pending_fill_respects_volume_limit_and_keeps_remaining():
    fw = _framework({"enabled": True, "participation_rate": 0.1, "allow_partial_fill": True})
    fw.pending_order_mgr.add_order(
        {"code": "600000.SH", "action": "buy", "price": 10.0, "volume": 10000, "order_type": "limit"}, DATE)
    # 当根 50 手 = 5000 股，参与率 10% → 500 股
    results = _run_pending(fw, {"600000.SH": _bar(open_=9.9, volume=50)}, NEXT)
    assert [(o["fill_volume"], ok) for o, ok in results] == [(500, True)]
    assert fw.trade_mgr.positions["600000.SH"]["volume"] == 500
    order = _only_order(fw)
    assert order["remaining_volume"] == 9500 and order["filled_volume"] == 500
    assert order["status"] == OrderStatus.PENDING


def test_pending_fill_without_partial_permission_waits():
    fw = _framework({"enabled": True, "participation_rate": 0.1, "allow_partial_fill": False})
    fw.pending_order_mgr.add_order(
        {"code": "600000.SH", "action": "buy", "price": 10.0, "volume": 10000, "order_type": "limit"}, DATE)
    assert _run_pending(fw, {"600000.SH": _bar(open_=9.9, volume=50)}, NEXT) == []
    assert _only_order(fw)["remaining_volume"] == 10000
    assert "600000.SH" not in fw.trade_mgr.positions


# ---------------- 附A：执行失败按拒单移出，不悄悄标成交 ----------------
def test_pending_fill_failure_rejects_and_removes(capsys):
    fw = _framework(cash=100.0)
    fw.pending_order_mgr.add_order(
        {"code": "600000.SH", "action": "buy", "price": 10.0, "volume": 1000, "order_type": "limit"}, DATE)
    results = _run_pending(fw, {"600000.SH": _bar(open_=9.9)}, NEXT)
    assert [ok for _, ok in results] == [False]
    assert fw.pending_order_mgr._pending_orders == {}
    assert "资金不足" in results[0][0]["reject_reason"]
    assert "已拒单" in capsys.readouterr().out
    # 下一根不会重复尝试
    assert _run_pending(fw, {"600000.SH": _bar(open_=9.9)}, NEXT) == []


def test_unexecuted_candidate_stays_pending():
    fw = _framework()
    fw.pending_order_mgr.add_order(
        {"code": "600000.SH", "action": "buy", "price": 10.0, "volume": 1000, "order_type": "limit"}, DATE)
    candidates = fw.pending_order_mgr.check_pending_orders(
        {"600000.SH": _bar(open_=9.9)}, NEXT["timestamp"], NEXT["date"], fw._match_config)
    assert len(candidates) == 1
    assert _only_order(fw)["remaining_volume"] == 1000


# ---------------- 4：撮合字段强制补齐 + 缺列警告 ----------------
def test_required_match_fields_by_period_and_dividend():
    assert _framework()._required_match_fields("1d") == ["open", "high", "low", "volume", "preClose"]
    assert "preClose" not in _framework(dividend_type="front")._required_match_fields("1m")
    assert _framework()._required_match_fields("tick") == [
        "lastPrice", "open", "high", "low", "volume", "lastClose"]


def test_missing_match_field_warns_once(caplog):
    fw = _framework()
    bar = {"open": 10.0, "low": 9.8, "close": 10.2, "volume": 1000, "preClose": 10.0}  # 缺 high
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            fw._process_signals_with_match_engine(
                [{"code": "600000.SH", "action": "sell", "price": 10.4, "volume": 100, "order_type": "limit"}],
                {"600000.SH": dict(bar)}, DATE)
    assert sum("撮合所需字段 high" in r.getMessage() for r in caplog.records) == 1


def test_tick_last_close_is_used_for_price_limit():
    tm = _trade_mgr()
    tick = {"lastPrice": 11.0, "close": 11.0, "open": 10.5, "high": 11.0, "low": 10.4, "lastClose": 10.0}
    ok, reason = tm._check_price_limit({"code": "600000.SH", "action": "buy"}, tick)
    assert not ok and "涨停" in reason


def test_inconsistent_pre_close_basis_skips_limit_check():
    tm = _trade_mgr()
    # 复权行情配不复权昨收：当根价格远低于跌停价，不应误判跌停
    bar = _bar(open_=6.0, high=6.1, low=5.9, close=6.0, pre_close=10.0)
    assert tm.get_price_limits("600000.SH", bar) is None
    assert tm._check_price_limit({"code": "600000.SH", "action": "sell"}, bar) == (True, "")


# ---------------- 7：限价越出涨跌停即时拒单 ----------------
def test_immediate_limit_buy_above_limit_up_is_rejected():
    fw = _framework()
    signal = {"code": "600000.SH", "action": "buy", "price": 11.05, "volume": 100, "order_type": "limit"}
    fw._process_signals_with_match_engine([signal], {"600000.SH": _bar()}, DATE)
    assert "600000.SH" not in fw.trade_mgr.positions
    assert fw.pending_order_mgr._pending_orders == {}


def test_immediate_limit_within_band_with_rounding_tolerance_fills():
    fw = _framework()
    signal = {"code": "600000.SH", "action": "sell", "price": 8.9995, "volume": 100, "order_type": "limit"}
    fw.trade_mgr.positions["600000.SH"] = {"volume": 100, "can_use_volume": 100, "avg_price": 10.0}
    fw._process_signals_with_match_engine([signal], {"600000.SH": _bar()}, DATE)
    assert "actual_price" in signal  # 8.9995 在跌停价 9.00 的 0.001 容差内，不拒单


# ---------------- 12：市价单剩余量下一根按开盘价成交 ----------------
def test_market_remaining_fills_next_bar_at_open_and_inherits_day_expiry():
    fw = _framework({"enabled": True, "participation_rate": 0.1, "allow_partial_fill": True})
    signal = {"code": "600000.SH", "action": "buy", "price": 10.2, "volume": 1000}
    fw._process_signals_with_match_engine([signal], {"600000.SH": _bar(volume=50)}, DATE)
    order = _only_order(fw)
    assert order["order_type"] == "market" and order["remaining_volume"] == 500
    assert order["time_in_force"] == "day"

    results = _run_pending(fw, {"600000.SH": _bar(open_=10.1, volume=1000)}, NEXT)
    assert [(o["match_price"], ok) for o, ok in results] == [(10.1, True)]
    assert fw.trade_mgr.positions["600000.SH"]["volume"] == 1000
    assert fw.pending_order_mgr._pending_orders == {}


def test_market_remaining_blocked_by_one_line_board_then_expires():
    fw = _framework({"enabled": True, "participation_rate": 0.1, "allow_partial_fill": True})
    fw._process_signals_with_match_engine(
        [{"code": "600000.SH", "action": "buy", "price": 10.2, "volume": 1000}],
        {"600000.SH": _bar(volume=50)}, DATE)
    one_line = _bar(open_=11.0, high=11.0, low=11.0, close=11.0, volume=1000, pre_close=10.0)
    assert _run_pending(fw, {"600000.SH": one_line}, NEXT) == []
    assert _only_order(fw)["remaining_volume"] == 500
    fw.pending_order_mgr.expire_day_orders(NEXT["date"])
    assert fw.pending_order_mgr._pending_orders == {}


def test_tick_market_remaining_uses_last_price():
    fw = _framework()
    fw.pending_order_mgr.add_order(
        {"code": "600000.SH", "action": "buy", "price": 10.0, "volume": 100, "order_type": "market"}, DATE)
    tick = {"lastPrice": 10.3, "open": 10.0, "high": 10.5, "low": 9.9, "volume": 100, "lastClose": 10.0}
    results = _run_pending(fw, {"600000.SH": tick}, NEXT)
    assert results[0][0]["match_price"] == 10.3


# ---------------- 附B + 10：部分成交 ----------------
def test_limit_partial_fill_keeps_original_limit_and_buffers_executed_copy():
    fw = _framework({"enabled": True, "participation_rate": 0.1, "allow_partial_fill": True})
    signal = {"code": "600000.SH", "action": "buy", "price": 10.3, "volume": 1000, "order_type": "limit"}
    fw._process_signals_with_match_engine([signal], {"600000.SH": _bar(open_=10.0, volume=50)}, DATE)
    # 剩余挂单保留用户原始限价 10.3，而非成交价 10.0
    order = _only_order(fw)
    assert (order["price"], order["order_type"], order["remaining_volume"]) == (10.3, "limit", 500)
    # 已成交部分的副本进入记录缓冲区，原信号不写 actual_price（record_results 会跳过，不重复记录）
    assert "actual_price" not in signal
    assert [(s["volume"], s["actual_price"]) for s in fw._unrecorded_fills] == [(500, 10.0)]


def test_partial_fill_failure_is_not_buffered_and_not_requeued():
    fw = _framework({"enabled": True, "participation_rate": 0.1, "allow_partial_fill": True}, cash=10.0)
    fw._process_signals_with_match_engine(
        [{"code": "600000.SH", "action": "buy", "price": 10.2, "volume": 1000}],
        {"600000.SH": _bar(volume=50)}, DATE)
    assert getattr(fw, "_unrecorded_fills", []) == []
    assert fw.pending_order_mgr._pending_orders == {}


# ---------------- 13：滑点统一 + 限价封顶 ----------------
TWO_TICKS = {"type": "tick", "tick_size": 0.01, "tick_count": 2}


def test_pending_fill_applies_slippage():
    fw = _framework(slippage=TWO_TICKS)
    fw.pending_order_mgr.add_order(
        {"code": "600000.SH", "action": "buy", "price": 10.0, "volume": 100, "order_type": "market"}, DATE)
    order, ok = _run_pending(fw, {"600000.SH": _bar(open_=9.9)}, NEXT)[0]
    assert ok and order["actual_price"] == pytest.approx(9.92)
    assert order["fill_price"] == pytest.approx(9.92)  # 主循环按 fill_price 记录实际成交价
    assert order["match_price"] == pytest.approx(9.9)


def test_pending_limit_fill_slippage_capped_at_limit():
    fw = _framework(slippage=TWO_TICKS)
    fw.pending_order_mgr.add_order(
        {"code": "600000.SH", "action": "buy", "price": 10.0, "volume": 100, "order_type": "limit"}, DATE)
    order, ok = _run_pending(fw, {"600000.SH": _bar(open_=10.2, low=9.9)}, NEXT)[0]
    assert ok and order["actual_price"] == pytest.approx(10.0)


def test_immediate_limit_sell_slippage_capped_at_limit():
    fw = _framework(slippage=TWO_TICKS)
    fw.trade_mgr.positions["600000.SH"] = {"volume": 100, "can_use_volume": 100, "avg_price": 10.0}
    signal = {"code": "600000.SH", "action": "sell", "price": 10.4, "volume": 100, "order_type": "limit"}
    fw._process_signals_with_match_engine([signal], {"600000.SH": _bar(open_=10.0, high=10.5)}, DATE)
    assert signal["actual_price"] == pytest.approx(10.4)  # 不会因滑点卖到 10.38


def test_slippage_only_applied_once_from_match_price():
    tm = _trade_mgr(slippage=TWO_TICKS)
    order = {"code": "600000.SH", "action": "buy", "fill_price": 10.0, "remaining_volume": 100}
    assert tm.execute_pending_fill(order, {"600000.SH": _bar()})
    assert order["fill_price"] == pytest.approx(10.02)
    signal = {"code": "600000.SH", "action": "buy", "price": 10.02, "volume": 100,
              "_price_includes_slippage": True}
    assert tm._place_order_backtest(signal) and signal["actual_price"] == pytest.approx(10.02)


# ---------------- 16：止损成交价统一 ----------------
@pytest.mark.parametrize("action,stop,bar,expected", [
    ("sell", 9.9, _bar(open_=9.7, high=9.8, low=9.5), 9.7),    # 跳空低开越过止损价 → 开盘价
    ("sell", 9.9, _bar(open_=10.0, high=10.1, low=9.5), 9.9),  # 盘中触及 → 止损价
    ("buy", 10.3, _bar(open_=10.5, high=10.6, low=10.4), 10.5),
    ("buy", 10.3, _bar(open_=10.0, high=10.6, low=9.9), 10.3),
    ("buy", 10.3, {"open": 0, "high": 10.25, "low": 10.0}, 10.25),  # 限制在 [low, high]
])
def test_stop_fill_price_rule(action, stop, bar, expected):
    assert _stop_order_fill_price(action, stop, bar) == pytest.approx(expected)


def test_stop_fill_price_same_for_immediate_and_pending():
    bar = _bar(open_=10.0, high=10.1, low=9.5, close=9.6)
    fw = _framework()
    fw.trade_mgr.positions["600000.SH"] = {"volume": 200, "can_use_volume": 200, "avg_price": 10.0}
    signal = {"code": "600000.SH", "action": "sell", "price": 9.0, "volume": 100,
              "order_type": "stop", "stop_price": 9.9}
    fw._process_signals_with_match_engine([signal], {"600000.SH": dict(bar)}, DATE)
    fw.pending_order_mgr.add_order(
        {"code": "600000.SH", "action": "sell", "price": 9.0, "volume": 100,
         "order_type": "stop", "stop_price": 9.9}, DATE)
    order, ok = _run_pending(fw, {"600000.SH": dict(bar)}, NEXT)[0]
    assert ok
    assert signal["actual_price"] == pytest.approx(9.9) == order["actual_price"]


# ---------------- 17：止损限价触发后以限价单入队 ----------------
def test_triggered_stop_limit_queued_as_limit():
    fw = _framework()
    fw.trade_mgr.positions["600000.SH"] = {"volume": 100, "can_use_volume": 100, "avg_price": 10.0}
    signal = {"code": "600000.SH", "action": "sell", "price": 10.6, "volume": 100,
              "order_type": "stop_limit", "stop_price": 9.9}
    fw._process_signals_with_match_engine([signal], {"600000.SH": _bar(high=10.3, low=9.8)}, DATE)
    order = _only_order(fw)
    assert order["order_type"] == "limit" and order["stop_triggered"] is True
    assert order["original_order_type"] == "stop_limit" and order["stop_price"] == 9.9
    assert order["price"] == 10.6
    # 下一根无需再次触发，最高价触及限价即成交
    results = _run_pending(fw, {"600000.SH": _bar(open_=10.2, high=10.7, low=10.1)}, NEXT)
    assert [ok for _, ok in results] == [True]
