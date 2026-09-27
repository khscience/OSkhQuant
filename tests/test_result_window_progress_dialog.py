# coding: utf-8
"""结果窗口的“生成回测结果”进度框用完即销毁，不留在结果窗口下。

背景：2026-09-26 的界面测试里出现过一次残留的空白“生成回测结果”小框。原来只
close()，隐藏的对话框会一直挂在结果窗口下，每打开一次结果就多一个原生窗口。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
import pytest
from PyQt5.QtCore import QEvent
from PyQt5.QtWidgets import QApplication, QProgressDialog, QWidget

from backtest_result_window import BacktestResultWindow


@pytest.fixture()
def qt_app():
    return QApplication.instance() or QApplication([])


class _FakeResultWindow(QWidget):
    """只借用 _apply_loaded_data 的流程，渲染步骤都换成空操作。"""

    def __init__(self, fail_at=None):
        super().__init__()
        self.calls = []
        self.fail_at = fail_at

    def _step(self, name):
        self.calls.append(name)
        if name == self.fail_at:
            raise RuntimeError("渲染失败")

    def _update_benchmark_warning(self):
        """开源版在这里多一步“缺少基准数据”的提示。"""

    def update_basic_info(self, *args):
        self._step("basic")

    def update_chart(self, *args):
        self._step("chart")

    def update_trades_table(self, *args):
        self._step("trades")

    def update_daily_stats_table(self, *args):
        self._step("daily")

    def update_performance_charts(self, *args):
        self._step("performance")


def _data():
    empty = pd.DataFrame()
    return {
        "price_decimals": 2,
        "trades_raw_df": empty,
        "daily_stats_original_df": empty,
        "benchmark_code": "000300.SH",
        "benchmark_df": empty,
        "config_row": {},
        "daily_stats_df": empty,
        "trades_display_df": empty,
    }


def _flush_deferred_deletes():
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()


def test_progress_dialog_is_destroyed_after_rendering(qt_app):
    window = _FakeResultWindow()
    BacktestResultWindow._apply_loaded_data(window, _data())
    _flush_deferred_deletes()

    assert window.calls == ["basic", "chart", "trades", "daily", "performance"]
    assert window.findChildren(QProgressDialog) == []
    window.deleteLater()
    _flush_deferred_deletes()


def test_progress_dialog_is_destroyed_when_a_step_fails(qt_app):
    window = _FakeResultWindow(fail_at="chart")
    with pytest.raises(RuntimeError):
        BacktestResultWindow._apply_loaded_data(window, _data())
    _flush_deferred_deletes()

    assert window.findChildren(QProgressDialog) == []
    window.deleteLater()
    _flush_deferred_deletes()
