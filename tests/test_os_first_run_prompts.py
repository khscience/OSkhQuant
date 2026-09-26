# coding: utf-8
"""首次启动的提示框不能叠在别的模态框上（Windows 沙盒装机测试发现的误点问题）。"""

import os

os.environ.setdefault("PYTHONHASHSEED", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication, QMessageBox, QWidget

import GUIkhQuant as gui_module


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def _harness():
    harness = QWidget()
    harness.check_software_status = lambda: None
    harness.apply_dark_titlebar = lambda *_args: None
    harness.open_duckdb_viewer = lambda: None
    harness._suggest_download_if_empty = (
        lambda data_dir: gui_module.KhQuantGUI._suggest_download_if_empty(harness, data_dir)
    )
    return harness


def test_empty_data_prompt_waits_for_open_modal(qt_app, tmp_path, monkeypatch):
    scheduled, shown = [], []
    modal = {"widget": object()}
    monkeypatch.setattr(gui_module.QApplication, "activeModalWidget", staticmethod(lambda: modal["widget"]))
    monkeypatch.setattr(gui_module.QTimer, "singleShot", staticmethod(lambda ms, fn: scheduled.append((ms, fn))))
    monkeypatch.setattr(QMessageBox, "exec_", lambda self: shown.append(self.windowTitle()) or 0)

    harness = _harness()
    gui_module.KhQuantGUI._suggest_download_if_empty(harness, str(tmp_path))
    assert shown == [] and len(scheduled) == 1

    # 策略复制框还开着：继续等
    scheduled.pop()[1]()
    assert shown == [] and len(scheduled) == 1

    # 关掉之后才问
    modal["widget"] = None
    scheduled.pop()[1]()
    assert shown == ["还没有行情数据"] and scheduled == []


def test_empty_data_prompt_skipped_when_stock_data_exists(qt_app, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(gui_module.QApplication, "activeModalWidget", staticmethod(lambda: None))
    monkeypatch.setattr(QMessageBox, "exec_", lambda self: shown.append(self.windowTitle()) or 0)
    (tmp_path / "SZ").mkdir()
    (tmp_path / "SZ" / "000001.db").write_bytes(b"")
    gui_module.KhQuantGUI._suggest_download_if_empty(_harness(), str(tmp_path))
    assert shown == []


def test_baostock_dialog_buttons_stay_outside_scroll_area(qt_app, tmp_path):
    # 屏幕较矮时滚动区域里的内容会超出窗口，按钮必须固定在滚动区域外面
    from types import SimpleNamespace

    from PyQt5.QtWidgets import QScrollArea
    from duckdb_storage.viewer import BaoStockImportDialog

    dialog = BaoStockImportDialog(SimpleNamespace(data_root=str(tmp_path)))
    for btn in (dialog.start_btn, dialog.close_btn, dialog.indicator_start_btn, dialog.indicator_close_btn):
        parent = btn.parentWidget()
        while parent is not None and parent is not dialog:
            assert not isinstance(parent, QScrollArea), btn.text()
            parent = parent.parentWidget()
    dialog.deleteLater()
