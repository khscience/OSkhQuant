# coding: utf-8
"""滚轮误触保护回归测试。

背景：2026-09-20 的 GUI 回归测试发现，在 MiniQMT 导入对话框里滚动页面时，
滚轮经过未获焦点的日期框会把"5分钟起始日期"从 2025 静默改成 2015，对话框
本身却没有滚动。修复后：未获焦点的下拉框/数字框/日期框放弃滚轮并转交给外层
滚动区域；点中（获得焦点）之后滚轮照常可用。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QDate, QPoint, QPointF, Qt
from PyQt5.QtGui import QWheelEvent
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDateEdit,
    QDialog,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from khUiScale import UnfocusedWheelGuard, install_wheel_guard


@pytest.fixture()
def qt_app():
    return QApplication.instance() or QApplication([])


def _wheel(widget):
    event = QWheelEvent(
        QPointF(widget.rect().center()),
        QPointF(widget.mapToGlobal(widget.rect().center())),
        QPoint(0, -120),
        QPoint(0, -120),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.ScrollUpdate,
        False,
    )
    QApplication.sendEvent(widget, event)


def _build_dialog():
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    content = QWidget()
    content_layout = QVBoxLayout(content)
    date_edit = QDateEdit()
    # QDateEdit() 不带参数时默认落在 2000-01-01——"日"恰好是该字段自身的最小值
    # (1)。在 offscreen 平台下控件默认选中的就是"日"字段，往下滚动等价于对
    # 该字段的边界值调用 stepBy(-1)，Qt 会正常拦下、不产生任何变化，这与
    # 滚轮是否被拦截无关，纯粹是日期边界的正常行为。换一个所有字段都远离
    # 边界的日期，避免测试结果取决于控件恰好选中了哪个字段。
    date_edit.setDate(QDate(2020, 6, 15))
    combo = QComboBox()
    combo.addItems(["不复权", "前复权", "后复权"])
    content_layout.addWidget(date_edit)
    content_layout.addWidget(combo)
    scroll.setWidget(content)
    layout.addWidget(scroll)
    return dialog, scroll, date_edit, combo


def test_unfocused_wheel_does_not_change_date_or_combo(qt_app):
    install_wheel_guard(qt_app)
    dialog, _scroll, date_edit, combo = _build_dialog()
    try:
        date_edit.clearFocus()
        combo.clearFocus()
        before_date = date_edit.date()
        before_index = combo.currentIndex()

        for _ in range(10):
            _wheel(date_edit)
            _wheel(combo)

        assert date_edit.date() == before_date, "未获焦点的日期框不应被滚轮改值"
        assert combo.currentIndex() == before_index, "未获焦点的下拉框不应被滚轮改值"
    finally:
        dialog.close()


def test_focused_widget_still_accepts_wheel(qt_app):
    install_wheel_guard(qt_app)
    dialog, _scroll, date_edit, _combo = _build_dialog()
    try:
        dialog.show()
        dialog.activateWindow()
        QApplication.setActiveWindow(dialog)
        qt_app.processEvents()
        date_edit.setFocus(Qt.MouseFocusReason)
        qt_app.processEvents()
        if not date_edit.hasFocus():
            pytest.skip("当前平台无法获得真实焦点，跳过")

        before = date_edit.date()
        _wheel(date_edit)

        assert date_edit.date() != before, "点中后滚轮应能正常调整数值"
    finally:
        dialog.close()


def test_guard_forwards_wheel_to_enclosing_scroll_area(qt_app):
    install_wheel_guard(qt_app)
    dialog, scroll, date_edit, _combo = _build_dialog()
    try:
        dialog.resize(200, 120)
        dialog.show()
        qt_app.processEvents()
        bar = scroll.verticalScrollBar()
        if bar.maximum() == 0:
            pytest.skip("内容未超出可视区，无法验证转交滚动")
        bar.setValue(0)
        date_edit.clearFocus()

        _wheel(date_edit)
        qt_app.processEvents()

        assert bar.value() > 0, "滚轮应转交给外层滚动区域，而不是被吞掉"
    finally:
        dialog.close()


def test_install_is_idempotent(qt_app):
    first = install_wheel_guard(qt_app)
    second = install_wheel_guard(qt_app)
    assert isinstance(first, UnfocusedWheelGuard)
    assert first is second
