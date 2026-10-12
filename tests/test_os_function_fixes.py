# coding: utf-8
"""2.2 功能测试发现的问题的回归测试。

- 完整性检查按上市/退市日期表排除上市前、退市后的日子
- 回测历史管理显示真实策略名
- khHistory 缺数据弹窗按实际返回行数说明
- 运行日志面板不显示 Traceback
- 个股分析两张图横轴对齐
- 对话框标题栏不再有「?」
"""
import logging
import os

os.environ.setdefault("PYTHONHASHSEED", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from datetime import datetime
from types import SimpleNamespace

import duckdb
import pytest

import khQTTools


# ---------------------------------------------------------------------------
# 上市/退市日期
# ---------------------------------------------------------------------------

def _make_daily_db(root, code, market, days):
    db_dir = root / market
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_dir / f"{code}.db"))
    conn.execute(
        "CREATE TABLE kline_1d (time TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE,"
        " close DOUBLE, volume DOUBLE, amount DOUBLE)"
    )
    conn.executemany(
        "INSERT INTO kline_1d VALUES (?, 10, 11, 9, 10.5, 100, 1000)",
        [(f"{d[:4]}-{d[4:6]}-{d[6:]} 00:00:00",) for d in days],
    )
    conn.close()


TRADE_DAYS = ["20251201", "20251202", "20251203", "20251204", "20251205"]


@pytest.fixture()
def trade_days(monkeypatch):
    monkeypatch.setattr(khQTTools, "get_trade_days_set", lambda *_args: set(TRADE_DAYS))
    khQTTools.clear_integrity_scan_cache()
    yield TRADE_DAYS
    khQTTools.clear_integrity_scan_cache()


def _scan(root, stocks, listing_dates=None):
    return khQTTools.check_duckdb_data_integrity(
        stock_list=stocks,
        periods=["1d"],
        start_date=TRADE_DAYS[0],
        end_date=TRADE_DAYS[-1],
        duckdb_data_path=str(root),
        now=datetime(2025, 12, 8, 16, 0),
        listing_dates=listing_dates,
    )


def _tasks(result):
    return sorted((t["stock"], t["start"], t["end"], t["missing_days"]) for t in result["missing_tasks"])


def test_integrity_ignores_days_before_listing(tmp_path, trade_days):
    # 12-03 上市，本地数据从上市日开始
    _make_daily_db(tmp_path, "001280", "SZ", TRADE_DAYS[2:])

    without = _scan(tmp_path, ["001280.SZ"])
    assert _tasks(without) == [("001280.SZ", "20251201", "20251202", 2)]

    khQTTools.clear_integrity_scan_cache()
    listed = _scan(tmp_path, ["001280.SZ"], {"001280.SZ": ("20251203", "")})
    assert listed["is_complete"] is True
    assert listed["missing_tasks"] == []


def test_integrity_still_reports_real_gaps_after_listing(tmp_path, trade_days):
    # 上市后缺了 12-04，照样要报
    _make_daily_db(tmp_path, "001280", "SZ", ["20251203", "20251205"])
    result = _scan(tmp_path, ["001280.SZ"], {"001280.SZ": ("20251203", "")})
    assert _tasks(result) == [("001280.SZ", "20251204", "20251204", 1)]


def test_integrity_ignores_days_after_delisting(tmp_path, trade_days):
    # 退市日 12-03 当天仍有一行行情，之后没有
    _make_daily_db(tmp_path, "600087", "SH", TRADE_DAYS[:3])
    result = _scan(tmp_path, ["600087.SH"], {"600087.SH": ("19970612", "20251203")})
    assert result["missing_tasks"] == []

    khQTTools.clear_integrity_scan_cache()
    # 退市日当天没数据仍算缺
    _make_daily_db(tmp_path, "600088", "SH", TRADE_DAYS[:2])
    result = _scan(tmp_path, ["600088.SH"], {"600088.SH": ("19970612", "20251203")})
    assert _tasks(result) == [("600088.SH", "20251203", "20251203", 1)]


def test_integrity_stock_without_database_uses_listing_range(tmp_path, trade_days):
    (tmp_path / "SZ").mkdir()
    result = _scan(
        tmp_path,
        ["001281.SZ", "001282.SZ"],
        {"001281.SZ": ("20251204", ""), "001282.SZ": ("20260105", "")},
    )
    # 001281 只缺上市后的两天；001282 整段都在上市前，不报
    assert _tasks(result) == [("001281.SZ", "20251204", "20251205", 2)]


def test_trim_keeps_same_objects_when_listing_covers_range():
    days = set(TRADE_DAYS)
    ordered = tuple(TRADE_DAYS)
    kept_days, kept_order = khQTTools._trim_days_for_listing(days, ordered, ("19910403", ""))
    assert kept_days is days and kept_order is ordered
    kept_days, kept_order = khQTTools._trim_days_for_listing(days, ordered, ("", ""))
    assert kept_days is days and kept_order is ordered
    kept_days, kept_order = khQTTools._trim_days_for_listing(days, ordered, ("20251202", "20251204"))
    assert kept_order == ("20251202", "20251203", "20251204")
    assert kept_days == set(kept_order)


def test_listing_dates_roundtrip_and_partial_result_is_rejected(tmp_path, monkeypatch):
    rows = [
        {"code": "sz.001280", "ipoDate": "2025-12-03", "outDate": ""},
        {"code": "sh.600087", "ipoDate": "1997-06-12", "outDate": "2014-06-05"},
        {"code": "bad", "ipoDate": "2020-01-01", "outDate": ""},
    ]
    # 半截结果不覆盖
    assert khQTTools.save_listing_dates_from_baostock(rows, str(tmp_path)) == 0
    assert not (tmp_path / khQTTools.LISTING_DATES_FILENAME).exists()

    monkeypatch.setattr(khQTTools, "_LISTING_DATES_MIN_ROWS", 1)
    assert khQTTools.save_listing_dates_from_baostock(rows, str(tmp_path)) == 2
    path = tmp_path / khQTTools.LISTING_DATES_FILENAME
    loaded = khQTTools.load_listing_dates(str(path))
    assert loaded == {"001280.SZ": ("20251203", ""), "600087.SH": ("19970612", "20140605")}
    # 按文件签名缓存
    assert khQTTools.load_listing_dates(str(path)) is loaded
    assert khQTTools.load_listing_dates(str(tmp_path / "missing.csv")) == {}


def test_bundled_listing_dates_cover_known_late_listing():
    bundled = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data",
                           khQTTools.LISTING_DATES_FILENAME)
    dates = khQTTools.load_listing_dates(bundled)
    assert len(dates) > 5000
    assert dates["001280.SZ"][0] == "20251203"
    assert dates["000001.SZ"][0] == "19910403"
    assert dates["000300.SH"][0] == "20050408"


# ---------------------------------------------------------------------------
# 回测历史管理：策略名
# ---------------------------------------------------------------------------

def test_history_shows_strategy_file_name_instead_of_hash():
    from BacktestHistoryManager import BacktestHistoryManager

    harness = SimpleNamespace(extract_strategy_name=lambda dir_name: BacktestHistoryManager.extract_strategy_name(None, dir_name))
    dir_name = "strategy_2849c87a_20250601_20260918_20260927_200834"
    win = {"strategy_file": r"C:\Users\KH\Documents\KhQuant_OS\strategies\【1-MA策略案例】双均线多股票_使用khMA函数.py"}
    posix = {"strategy_file": "/home/kh/strategies/my_strategy.py"}
    assert BacktestHistoryManager.strategy_display_name(harness, win, dir_name) == "【1-MA策略案例】双均线多股票_使用khMA函数"
    assert BacktestHistoryManager.strategy_display_name(harness, posix, dir_name) == "my_strategy"
    assert BacktestHistoryManager.strategy_display_name(harness, {"strategy_file": ""}, dir_name) == "strategy_2849c87a"
    assert BacktestHistoryManager.strategy_display_name(harness, None, dir_name) == "strategy_2849c87a"


# ---------------------------------------------------------------------------
# khHistory 缺数据弹窗
# ---------------------------------------------------------------------------

def _prompt_message(monkeypatch, available_rows):
    shown = []
    framework = SimpleNamespace(ui_settings={
        "khhistory_missing_data_prompt": True,
        "confirm_callback": lambda title, message: shown.append((title, message)) or True,
    })
    monkeypatch.setattr(khQTTools, "_get_current_framework_for_history_warning", lambda: framework)
    khQTTools._confirm_kh_history_missing_data(
        stock_code="001280.SZ", period="1d", fields=["close"], dividend_type="front",
        reason="当前时间前历史数据不足，需要 6 行", current_time="2025-12-04 00:00:00",
        available_rows=available_rows,
    )
    assert len(shown) == 1
    return shown[0][1]


def test_khhistory_prompt_describes_partial_history(monkeypatch):
    message = _prompt_message(monkeypatch, 1)
    assert "现有的 1 行历史数据" in message
    assert "空历史数据" not in message
    assert "字段: close" in message and "['close']" not in message
    assert "复权口径: 前复权" in message


def test_khhistory_prompt_describes_empty_history(monkeypatch):
    message = _prompt_message(monkeypatch, 0)
    assert "系统已返回空历史数据" in message


# ---------------------------------------------------------------------------
# 运行日志面板不显示 Traceback
# ---------------------------------------------------------------------------

def test_gui_log_handler_hides_traceback_and_file_only_records(monkeypatch):
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    import GUIkhQuant

    emitted = []
    gui = SimpleNamespace(log_signal=SimpleNamespace(emit=lambda msg, level: emitted.append((msg, level))))
    handler = GUIkhQuant.GUILogHandler(gui)
    handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))
    logger = logging.getLogger("test_gui_log_handler_hides_traceback")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        try:
            raise RuntimeError("没有找到任何有效的时间点，无法进行回测")
        except RuntimeError:
            logger.error("回测运行异常: 没有找到任何有效的时间点，无法进行回测", exc_info=True)
        logger.error("只写文件", extra={GUIkhQuant.GUI_LOG_SKIP_ATTR: True})
    finally:
        logger.removeHandler(handler)
    assert emitted == [("ERROR - 回测运行异常: 没有找到任何有效的时间点，无法进行回测", "ERROR")]


def test_gui_log_error_skips_errors_already_logged_by_framework(monkeypatch):
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    import GUIkhQuant
    import khFrame

    shown = []
    files = []
    harness = SimpleNamespace(log_message=lambda message, level="INFO": shown.append((message, level)))
    monkeypatch.setattr(GUIkhQuant, "log_exception_to_file", lambda message, error=None: files.append(message))

    logged = RuntimeError("没有找到任何有效的时间点，无法进行回测")
    khFrame._mark_error_logged(logged)
    GUIkhQuant.KhQuantGUI.log_error(harness, "策略运行异常", logged)
    assert shown == [] and files == []

    GUIkhQuant.KhQuantGUI.log_error(harness, "保存配置文件失败", OSError("磁盘已满"))
    assert shown == [("保存配置文件失败: 磁盘已满", "ERROR")]
    assert files == ["保存配置文件失败: 磁盘已满"]


def test_framework_marks_logged_errors():
    import khFrame

    error = RuntimeError("x")
    assert khFrame.is_error_logged(error) is False
    khFrame._mark_error_logged(error)
    assert khFrame.is_error_logged(error) is True


# ---------------------------------------------------------------------------
# 个股分析两张图横轴对齐
# ---------------------------------------------------------------------------

def test_stock_analysis_pnl_axis_matches_price_axis(qapp_offscreen):
    import pandas as pd
    from matplotlib.figure import Figure
    from stock_analysis_window import StockAnalysisWindow

    window = StockAnalysisWindow.__new__(StockAnalysisWindow)
    window.figure = Figure()
    window.ax_price = window.figure.add_subplot(211)
    window.ax_pnl = window.figure.add_subplot(212)
    window.canvas = SimpleNamespace(draw=lambda: None, draw_idle=lambda: None)
    window.font_scale = 1.0
    window.daily_trades = {}
    window.kline_load_error = None
    kline = pd.DataFrame({
        "time": pd.to_datetime(["2025-06-03", "2025-06-04", "2025-06-05", "2025-06-06"]),
        "open": [10.0, 10.2, 10.1, 10.4],
        "high": [10.5, 10.6, 10.5, 10.8],
        "low": [9.8, 10.0, 9.9, 10.2],
        "close": [10.2, 10.1, 10.4, 10.6],
        "volume": [100, 120, 90, 150],
    })
    trades = pd.DataFrame([
        {"datetime": "2025-06-03", "action": "buy", "price": 10.2, "volume": 100, "amount": 1020.0,
         "commission": 5.0, "total_pnl": -5.0},
        {"datetime": "2025-06-05", "action": "sell", "price": 10.4, "volume": 100, "amount": 1040.0,
         "commission": 5.0, "total_pnl": 10.0},
    ])
    window._update_chart(trades, "000001.SZ", kline)
    assert window.ax_pnl.get_xlim() == window.ax_price.get_xlim() == (-0.5, 3.5)


@pytest.fixture()
def qapp_offscreen():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_dialogs_have_no_context_help_button(qapp_offscreen):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QDialog
    from khUiScale import disable_dialog_help_button

    disable_dialog_help_button()
    dialog = QDialog()
    try:
        assert not (dialog.windowFlags() & Qt.WindowContextHelpButtonHint)
    finally:
        dialog.deleteLater()


def test_standard_buttons_are_chinese(qapp_offscreen):
    from PyQt5.QtCore import QCoreApplication
    from PyQt5.QtWidgets import QDialogButtonBox, QMessageBox
    from khUiScale import install_chinese_qt_translations

    install_chinese_qt_translations(qapp_offscreen)
    try:
        box = QMessageBox(QMessageBox.Question, "确认修复", "是否继续？", QMessageBox.Yes | QMessageBox.No)
        assert [button.text() for button in box.buttons()] == ["是(&Y)", "否(&N)"]
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        assert sorted(button.text() for button in buttons.buttons()) == sorted(["确定", "取消"])
        # 没收录的词条退回原文，不会变成空白
        assert QCoreApplication.translate("QPlatformTheme", "Something Else") == "Something Else"
        assert QCoreApplication.translate("KhTest", "hello") == "hello"
    finally:
        for translator in getattr(qapp_offscreen, "_kh_qt_translators", []):
            qapp_offscreen.removeTranslator(translator)
