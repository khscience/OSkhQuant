# coding: utf-8
"""首次启动的提示框不能叠在别的模态框上（Windows 沙盒装机测试发现的误点问题）。"""

import os

os.environ.setdefault("PYTHONHASHSEED", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication, QMessageBox, QWidget

import GUIkhQuant as gui_module
import kh_quickstart


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
    harness.show_quickstart = lambda show_data_button=True: gui_module.KhQuantGUI.show_quickstart(harness, show_data_button)
    return harness


def test_empty_data_prompt_waits_for_open_modal(qt_app, tmp_path, monkeypatch):
    scheduled, shown = [], []
    modal = {"widget": object()}
    monkeypatch.setattr(gui_module.QApplication, "activeModalWidget", staticmethod(lambda: modal["widget"]))
    monkeypatch.setattr(gui_module.QTimer, "singleShot", staticmethod(lambda ms, fn: scheduled.append((ms, fn))))
    monkeypatch.setattr(kh_quickstart.QuickStartDialog, "exec_", lambda self: shown.append(self.windowTitle()) or 0)

    harness = _harness()
    gui_module.KhQuantGUI._suggest_download_if_empty(harness, str(tmp_path))
    assert shown == [] and len(scheduled) == 1

    # 策略复制框还开着：继续等
    scheduled.pop()[1]()
    assert shown == [] and len(scheduled) == 1

    # 关掉之后才问
    modal["widget"] = None
    scheduled.pop()[1]()
    assert shown == ["新手上手"] and scheduled == []


def test_empty_data_prompt_skipped_when_stock_data_exists(qt_app, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(gui_module.QApplication, "activeModalWidget", staticmethod(lambda: None))
    monkeypatch.setattr(kh_quickstart.QuickStartDialog, "exec_", lambda self: shown.append(self.windowTitle()) or 0)
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


class _FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *args):
        for fn in self.slots:
            fn(*args)


class _FakeBench:
    """基准下载对话框的替身：只留 _after_benchmark_download 用到的部分"""

    def __init__(self, visible=True, error=None):
        from types import SimpleNamespace
        self.visible = visible
        self.result = {"benchmark_error": error} if error else {}
        self.status_label = SimpleNamespace(text=lambda: "000300.SH 日线已就绪，可以作为回测基准。")
        self.finished = _FakeSignal()
        self.accepted = False

    def isVisible(self):
        return self.visible

    def accept(self):
        self.accepted = True
        self.visible = False


def _bench_harness(monkeypatch):
    scheduled, suggested, logs = [], [], []
    monkeypatch.setattr(gui_module.QTimer, "singleShot", staticmethod(lambda ms, fn: scheduled.append((ms, fn))))
    harness = QWidget()
    harness.log_message = lambda msg, level="INFO": logs.append(msg)
    harness._suggest_download_if_empty = lambda data_dir: suggested.append(data_dir)
    return harness, scheduled, suggested, logs


def test_benchmark_dialog_closes_before_empty_data_prompt(qt_app, monkeypatch):
    # 提示框是模态的：基准对话框没关就弹，会把它压在下面关不掉（录新手教程时发现）
    harness, scheduled, suggested, logs = _bench_harness(monkeypatch)
    bench = _FakeBench()
    gui_module.KhQuantGUI._after_benchmark_download(harness, bench, "D:/khData")
    assert not bench.accepted and suggested == []
    scheduled.pop(0)[1]()          # 停留一会儿后自动收起
    assert bench.accepted and "已就绪" in logs[-1]
    scheduled.pop(0)[1]()          # 收起之后才提示
    assert suggested == ["D:/khData"]


def test_benchmark_failure_keeps_dialog_until_user_closes(qt_app, monkeypatch):
    harness, scheduled, suggested, _logs = _bench_harness(monkeypatch)
    bench = _FakeBench(error="网络不通")
    gui_module.KhQuantGUI._after_benchmark_download(harness, bench, "D:/khData")
    assert scheduled == [] and not bench.accepted
    bench.finished.emit(0)         # 用户看完原因关掉
    scheduled.pop(0)[1]()
    assert suggested == ["D:/khData"]


def test_hidden_benchmark_dialog_prompts_right_away(qt_app, monkeypatch):
    harness, scheduled, suggested, _logs = _bench_harness(monkeypatch)
    gui_module.KhQuantGUI._after_benchmark_download(harness, _FakeBench(visible=False), "D:/khData")
    scheduled.pop(0)[1]()
    assert suggested == ["D:/khData"]
