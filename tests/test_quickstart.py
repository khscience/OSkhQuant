# coding: utf-8
"""「新手上手」：讲清下载数据、示例要先填股票池，并引导看快速上手视频。"""
import ast
import html
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication, QLabel, QWidget

import kh_quickstart

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def test_steps_cover_data_examples_stock_pool_and_run(qt_app):
    titles = [title for title, _ in kh_quickstart.QUICKSTART_STEPS]
    assert titles == ["下载数据", "加载示例", "添加股票", "开始运行"]
    pool_step = dict(kh_quickstart.QUICKSTART_STEPS)["添加股票"]
    assert "股票池" in pool_step and "示例策略不带股票" in pool_step
    dialog = kh_quickstart.QuickStartDialog()
    assert len(dialog.step_frames) == len(kh_quickstart.QUICKSTART_STEPS)
    for frame, (title, text) in zip(dialog.step_frames, kh_quickstart.QUICKSTART_STEPS):
        shown = "\n".join(label.text() for label in frame.findChildren(QLabel))
        assert title in shown and html.escape(text) in shown
    assert "观看视频教程" in dialog.hint.text()
    assert "1.5" not in dialog.hint.text()


def test_video_link_points_to_quickstart_page_not_video_file():
    assert kh_quickstart.QUICKSTART_VIDEO_URL == "https://khsci.com/khQuant/quickstart/"
    assert not kh_quickstart.QUICKSTART_VIDEO_URL.endswith(".mp4")


@pytest.mark.parametrize("button, expected", [
    ("video_btn", kh_quickstart.RESULT_WATCH_VIDEO),
    ("data_btn", kh_quickstart.RESULT_OPEN_DATA),
])
def test_buttons_return_their_choice(qt_app, button, expected):
    dialog = kh_quickstart.QuickStartDialog()
    results = []
    dialog.finished.connect(results.append)
    getattr(dialog, button).click()
    assert results == [expected]


def test_help_menu_variant_can_hide_data_button(qt_app):
    dialog = kh_quickstart.QuickStartDialog(show_data_button=False)
    assert dialog.data_btn is None
    assert dialog.video_btn.isDefault()


@pytest.mark.parametrize("result, viewer_calls, url_calls", [
    (kh_quickstart.RESULT_OPEN_DATA, 1, 0),
    (kh_quickstart.RESULT_WATCH_VIDEO, 0, 1),
    (kh_quickstart.RESULT_LATER, 0, 0),
])
def test_main_window_acts_on_choice(qt_app, monkeypatch, result, viewer_calls, url_calls):
    import GUIkhQuant as gui_module
    opened = []
    monkeypatch.setattr(kh_quickstart.QuickStartDialog, "exec_", lambda self: result)
    monkeypatch.setattr(kh_quickstart.QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString()) or True))
    harness = QWidget()
    calls = []
    harness.apply_dark_titlebar = lambda *_args: None
    harness.open_duckdb_viewer = lambda: calls.append("viewer")
    gui_module.KhQuantGUI.show_quickstart(harness, show_data_button=True)
    assert len(calls) == viewer_calls
    assert opened == [kh_quickstart.QUICKSTART_VIDEO_URL] * url_calls


def test_help_button_offers_quickstart_and_full_tutorial():
    tree = ast.parse((ROOT / "GUIkhQuant.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "KhQuantGUI")
    source = "\n".join(ast.unparse(n) for n in cls.body if isinstance(n, ast.FunctionDef))
    assert "self._help_btn.setMenu(help_menu)" in source
    assert "'新手上手（视频教程）'" in source and "'完整在线教程'" in source
