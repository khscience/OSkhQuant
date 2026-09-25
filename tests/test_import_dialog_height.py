# coding: utf-8
"""导入对话框按内容自动撑高的回归测试。

背景：2026-09-20 的 GUI 回归测试发现，MiniQMT / BaoStock 导入对话框按最小尺寸
(800x600 / 800x700) 打开时，放在可滚动页里的"开始/停止/关闭"按钮落在可视区
之外，用户在默认窗口里找不到开始按钮（Tushare 的按钮不在滚动区，所以没问题）。
修复：窗口显示后按当前页内容需要的高度撑一次，并受屏幕可用高度限制。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QRect
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import duckdb_storage.viewer as viewer_module


@pytest.fixture()
def qt_app():
    return QApplication.instance() or QApplication([])


class _Desktop:
    def __init__(self, width, height):
        self.rect = QRect(0, 0, width, height)

    def availableGeometry(self, _widget=None):
        return self.rect


def _build_dialog(content_height=900):
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    tabs = QTabWidget()
    content = QWidget()
    content_layout = QVBoxLayout(content)
    filler = QLabel("内容")
    filler.setMinimumHeight(content_height)
    content_layout.addWidget(filler)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(content)
    tabs.addTab(scroll, "页")
    layout.addWidget(tabs)
    dialog.resize(800, 600)
    return dialog, tabs


def test_dialog_grows_to_fit_content_when_screen_allows(qt_app, monkeypatch):
    monkeypatch.setattr(
        viewer_module.QApplication, "desktop", staticmethod(lambda: _Desktop(1707, 1392))
    )
    dialog, tabs = _build_dialog()
    try:
        dialog.show()
        qt_app.processEvents()
        before = dialog.height()

        viewer_module.fit_dialog_height_to_content(dialog, tabs)
        qt_app.processEvents()

        assert dialog.height() > before, "屏幕放得下时应把窗口撑到内容所需高度"
    finally:
        dialog.close()


def test_dialog_never_exceeds_available_screen_height(qt_app, monkeypatch):
    monkeypatch.setattr(
        viewer_module.QApplication, "desktop", staticmethod(lambda: _Desktop(1280, 700))
    )
    dialog, tabs = _build_dialog()
    try:
        dialog.show()
        qt_app.processEvents()

        viewer_module.fit_dialog_height_to_content(dialog, tabs)
        qt_app.processEvents()

        assert dialog.height() <= 700 - 60, "小屏幕上不能把窗口撑出屏幕"
    finally:
        dialog.close()


def test_fit_is_noop_when_content_already_fits(qt_app, monkeypatch):
    monkeypatch.setattr(
        viewer_module.QApplication, "desktop", staticmethod(lambda: _Desktop(1707, 1392))
    )
    dialog, tabs = _build_dialog(content_height=100)
    try:
        dialog.show()
        qt_app.processEvents()
        before = dialog.height()

        viewer_module.fit_dialog_height_to_content(dialog, tabs)
        qt_app.processEvents()

        assert dialog.height() == before, "内容放得下时不应改动窗口大小"
    finally:
        dialog.close()
