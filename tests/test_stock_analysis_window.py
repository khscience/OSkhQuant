import pandas as pd

import khQTTools
from stock_analysis_window import StockAnalysisWindow, _trade_total_fee


def test_trade_total_fee_includes_every_fee_component():
    row = {
        "commission": 5.0,
        "stamp_tax": 7.5,
        "transfer_fee": 1.25,
        "flow_fee": 0.5,
    }
    assert _trade_total_fee(row) == 14.25


def test_daily_kline_query_includes_backtest_end_day_and_respects_fq(monkeypatch):
    captured = {}

    def fake_history(**kwargs):
        captured.update(kwargs)
        return {
            "000001.SZ": pd.DataFrame(
                {
                    "time": ["2025-01-02", "2025-01-03"],
                    "open": [10.0, 10.1],
                    "high": [10.2, 10.3],
                    "low": [9.9, 10.0],
                    "close": [10.1, 10.2],
                    "volume": [100, 200],
                }
            )
        }

    monkeypatch.setattr(khQTTools, "khHistory", fake_history)
    window = StockAnalysisWindow.__new__(StockAnalysisWindow)
    window.kline_cache = {}
    window.kline_load_error = None
    window.start_time = "20250102"
    window.end_time = "20250103"
    window.dividend_type = "back"

    frame = window._load_daily_kline("000001.SZ")

    assert captured["current_time"] == "20250104"
    assert captured["fq"] == "post"
    assert frame["time"].max() == pd.Timestamp("2025-01-03")


def test_daily_pnl_uses_same_complete_fee_definition_as_trade_table():
    window = StockAnalysisWindow.__new__(StockAnalysisWindow)
    trades = pd.DataFrame(
        [
            {
                "datetime": "2025-09-18",
                "action": "buy",
                "price": 7.40,
                "volume": 20800,
                "amount": 153920.0,
                "commission": 0.0,
                "stamp_tax": 0.0,
                "transfer_fee": 1.5392,
                "flow_fee": 0.0,
            },
            {
                "datetime": "2025-09-23",
                "action": "sell",
                "price": 7.14,
                "volume": 20800,
                "amount": 148512.0,
                "commission": 0.0,
                "stamp_tax": 0.0,
                "transfer_fee": 1.48512,
                "flow_fee": 0.0,
            },
        ]
    )
    kline = pd.DataFrame(
        {
            "time": ["2025-09-18", "2025-09-23"],
            "close": [7.40, 7.14],
        }
    )

    daily_pnl = window._calculate_daily_total_pnl(trades, kline)

    assert abs(daily_pnl[0] - (-1.5392)) < 1e-9
    assert abs(daily_pnl[1] - (-5411.02432)) < 1e-9
