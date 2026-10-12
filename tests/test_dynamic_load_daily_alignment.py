# coding: utf-8
"""分段（动态）加载与一次性加载的回测结果必须逐笔一致。

3.4.1.4 回归：日线库 time 存为当天 09:30，分段模式推算出的时间点带 09:30，
而分段缓存把日线归一到 0 点，每根 bar 都取不到数据 → 0 交易、无 summary.csv，
`kh run` 仍以 0 退出。用临时 DuckDB 库在真实框架上对照两种加载方式。
"""
import json
import math
import os

import pandas as pd
import pytest

STOCKS = ("000001.SZ", "600000.SH")

_STRATEGY = '''
from khQuantImport import *
closes = {}
def init(stocks=None, data=None):
    closes.clear()
def khHandlebar(data):
    out = []
    for sc in khGet(data, "stocks"):
        c = khPrice(data, sc, "close")
        h = closes.setdefault(sc, [])
        h.append(c)
        if len(h) < 10:
            continue
        # 一条腿用当前 bar，一条腿用 khHistory 日线，两种加载方式下都要一致
        hist = khHistory(sc, ["close"], 3, "1d", khGet(data, "date_num"), fq="none", force_download=False)
        daily = list(hist[sc]["close"].values)
        f = (sum(h[-5:]) + sum(daily)) / (5 + len(daily))
        s = sum(h[-10:]) / 10
        if f > s and not khHas(data, sc):
            out.extend(generate_signal(data, sc, c, 0.4, "buy", "up"))
        elif f < s and khHas(data, sc):
            out.extend(generate_signal(data, sc, c, 1.0, "sell", "down"))
    return out
'''


def _minute_stamps(day):
    stamps = []
    for start, count in (("09:31", 120), ("13:01", 120)):
        first = pd.Timestamp(f"{day.date()} {start}")
        stamps.extend(first + pd.Timedelta(minutes=i) for i in range(count))
    return stamps


def _frame(stamps, phase, step):
    px = [10 + 2 * math.sin(i / step + phase) for i in range(len(stamps))]
    return pd.DataFrame({
        "time": stamps,
        "open": px,
        "high": [p * 1.01 for p in px],
        "low": [p * 0.99 for p in px],
        "close": px,
        "volume": [10000.0] * len(px),
        "amount": [p * 1e6 for p in px],
    })


@pytest.fixture(scope="module")
def data_root(tmp_path_factory):
    from duckdb_storage.manager import DuckDBManager

    root = tmp_path_factory.mktemp("dyn_load") / "khData"
    root.mkdir()
    manager = DuckDBManager(data_root=str(root), read_only=False)
    try:
        days = pd.bdate_range("2024-10-01", "2025-03-31")
        minute_days = pd.bdate_range("2025-03-03", "2025-03-14")
        for index, code in enumerate(STOCKS):
            phase = 1.3 * index
            # 与各数据源入库口径一致：日线 time 存为当天 09:30
            daily = _frame([d + pd.Timedelta(hours=9, minutes=30) for d in days], phase, 6.0)
            manager.save_kline_data(daily, code, "1d")
            minutes = [s for d in minute_days for s in _minute_stamps(d)]
            manager.save_kline_data(_frame(minutes, phase, 40.0), code, "1m")
    finally:
        manager.close_all()
        DuckDBManager.reset_instance()
    return str(root)


def _run(tmp_path, monkeypatch, data_root, tag, period, extra):
    import khFrame
    from backtest_runtime_config import build_headless_settings

    strategy = tmp_path / "strategy.py"
    strategy.write_text(_STRATEGY, encoding="utf-8")
    start, end = ("20241101", "20250331") if period == "1d" else ("20250305", "20250314")
    config = {
        "run_mode": "backtest",
        "account": {"account_id": "88888888", "account_type": "STOCK"},
        "strategy_file": str(strategy),
        "backtest": {
            "start_time": start,
            "end_time": end,
            "init_capital": 1000000.0,
            "min_volume": 100,
            "benchmark": STOCKS[0],
            "trade_cost": {
                "min_commission": 5.0, "commission_rate": 0.0001, "stamp_tax_rate": 0.0005,
                "flow_fee": 0.0, "slippage": {"type": "ratio", "tick_size": 0.01, "tick_count": 2, "ratio": 0.01},
            },
            "trigger": {"type": period},
        },
        "data": {
            "kline_period": period,
            "dividend_type": "none",
            "fields": ["open", "high", "low", "close", "volume", "amount"],
            "stock_list": list(STOCKS),
        },
        "risk": {"position_limit": 0.95, "order_limit": 100, "loss_limit": 0.1},
        "performance": {"memory_profile": "standard"},
    }
    for key, value in extra.items():
        config[key] = value
    config_path = tmp_path / f"{tag}.kh"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    out_dir = tmp_path / f"results_{tag}"
    out_dir.mkdir()
    monkeypatch.setattr(khFrame.KhQuantFramework, "_get_writable_backtest_dir", lambda self: str(out_dir))
    headless = build_headless_settings(duckdb_data_path=data_root)
    headless["init_data_enabled"] = False
    framework = khFrame.KhQuantFramework(
        str(config_path),
        str(strategy),
        trader_callback=khFrame.MyTraderCallback(gui=None),
        ui_settings=headless,
    )
    framework.run()

    result = {}
    for folder, _dirs, files in os.walk(out_dir):
        for name in ("summary.csv", "trades.csv"):
            if name in files:
                with open(os.path.join(folder, name), encoding="utf-8-sig") as handle:
                    result[name] = handle.read()
    return result, framework


def _assert_same_backtest(full, segmented):
    assert "summary.csv" in full and "summary.csv" in segmented
    trade_lines = full["trades.csv"].strip().splitlines()
    assert len(trade_lines) > 1, "基准回测应有成交，否则对照没有意义"
    assert segmented["trades.csv"] == full["trades.csv"]
    assert segmented["summary.csv"] == full["summary.csv"]


@pytest.mark.parametrize("segmented_extra", [
    {"dynamic_load": {"enabled": True, "chunk_size": 20, "unit": "day"}},
    # 「省内存」档自动开启分段（chunk 由可用内存推算，可能只有一段）
    {"performance": {"memory_profile": "low", "memory_auto_dynamic_load": True}},
], ids=["chunk20", "profile_low"])
def test_daily_segmented_load_matches_full_load(tmp_path, monkeypatch, data_root, segmented_extra):
    full, full_fw = _run(tmp_path, monkeypatch, data_root, "full", "1d", {})
    segmented, seg_fw = _run(tmp_path, monkeypatch, data_root, "segmented", "1d", segmented_extra)

    assert not getattr(full_fw, "dynamic_loader_enabled", False)
    assert seg_fw.dynamic_loader_enabled
    _assert_same_backtest(full, segmented)


def test_minute_segmented_load_matches_full_load(tmp_path, monkeypatch, data_root):
    full, _ = _run(tmp_path, monkeypatch, data_root, "full", "1m", {})
    segmented, seg_fw = _run(
        tmp_path, monkeypatch, data_root, "segmented", "1m",
        {"dynamic_load": {"enabled": True, "chunk_size": 2, "unit": "day"}},
    )

    assert len(seg_fw.dynamic_loader.time_segments) > 1
    _assert_same_backtest(full, segmented)


def test_backtest_with_no_bar_data_fails_instead_of_silent_success(tmp_path, monkeypatch, data_root):
    import khFrame

    # 模拟每根 bar 都取不到行情（即上面那类时间对不上的故障）
    monkeypatch.setattr(khFrame, "_lookup_time_index", lambda *_args: (None, "missing"))
    with pytest.raises(RuntimeError, match="全部取不到行情数据"):
        _run(tmp_path, monkeypatch, data_root, "empty", "1d", {})
