# coding: utf-8
"""撮合与股票池的三个回归：成交量限制的单位、删除的股票不进回测、盘后挂单按交易日过期。"""
from types import SimpleNamespace

from khFrame import KhQuantFramework


def _framework(volume_limit=None, trading_days=None, is_trade_day=None):
    fw = KhQuantFramework.__new__(KhQuantFramework)
    fw._match_config = {"volume_limit": volume_limit or {}}
    fw._bt_trading_days = trading_days or []
    fw.tools = SimpleNamespace(is_trade_day=is_trade_day or (lambda d: False))
    return fw


def test_volume_limit_converts_bar_lots_to_shares():
    # DuckDB 里当根成交量 500 手 = 5 万股；参与率 10% 上限 5000 股
    fw = _framework({"enabled": True, "participation_rate": 0.1, "allow_partial_fill": True})
    volume, partial, reason = fw._check_volume_limit({"volume": 8000}, {"volume": 500, "high": 10.2, "low": 9.9, "close": 10.0})
    assert (volume, partial) == (5000, True)
    assert "50000股" in reason


def test_volume_limit_without_partial_fill_rejects_whole_order():
    fw = _framework({"enabled": True, "participation_rate": 0.1, "allow_partial_fill": False})
    assert fw._check_volume_limit({"volume": 8000}, {"volume": 500})[0] == 0
    assert fw._check_volume_limit({"volume": 4000}, {"volume": 500})[0] == 4000


def test_post_market_order_expires_on_next_trading_day_after_weekend():
    fw = _framework(trading_days=["2025-01-02", "2025-01-03", "2025-01-06"])
    assert fw._next_trading_day("2025-01-02") == "2025-01-03"
    assert fw._next_trading_day("2025-01-03") == "2025-01-06"   # 周五 → 下周一


def test_next_trading_day_after_backtest_end_uses_calendar():
    holidays = {"2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08"}
    cal = lambda d: d not in holidays and d not in ("2025-10-04", "2025-10-05")
    fw = _framework(trading_days=["2025-09-30"], is_trade_day=cal)
    assert fw._next_trading_day("2025-09-30") == "2025-10-09"   # 国庆节前 → 节后第一天


def test_next_trading_day_falls_back_to_calendar_day_without_calendar():
    def broken(_):
        raise RuntimeError("no calendar")
    fw = _framework(is_trade_day=broken)
    assert fw._next_trading_day("2025-01-03") == "2025-01-04"


class _Item:
    def __init__(self, text):
        self._text = text

    def text(self):
        return self._text


class _Table:
    def __init__(self, codes):
        self._codes = codes

    def rowCount(self):
        return len(self._codes)

    def item(self, row, col):
        return _Item(self._codes[row])


def test_stock_list_for_backtest_excludes_deleted_board_members(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONHASHSEED", "0")   # 否则导入 GUIkhQuant 会重启进程
    from GUIkhQuant import KhQuantGUI
    pool = tmp_path / "hs300.csv"
    pool.write_text("code,name\n", encoding="utf-8")
    rows = [("600000.SH", "A"), ("000002.SZ", "B"), ("600519.SH", "C")]
    gui = SimpleNamespace(
        pool_checkboxes={"hs300": SimpleNamespace(isChecked=lambda: True)},
        _get_pool_file=lambda code: "hs300.csv",
        get_data_path=lambda name: str(pool),
        _read_stock_rows_from_file=lambda path: rows,
        stock_list=_Table(["600000.SH", "600519.SH"]),
        deleted_stocks={"000002.SZ"},
    )
    assert KhQuantGUI.get_stock_list(gui) == ["600000.SH", "600519.SH"]
