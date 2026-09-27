# coding: utf-8
"""迁移旧策略的提示框里，路径整行显示，不在 “C:” 后面或中文字符之间折断。

背景：2026-09-26 Windows 沙盒装机测试（用户名 WDAGUtilityAccount）发现，
“复制 V2.1 的策略”提示框里的路径被折成 “C:” 和后半段两行。QMessageBox 的宽度
取“默认宽度”和“正文里最长的不可断开片段”中较大的一个；Qt 的换行规则允许在
“C:” 之后、中文字符之间断行，所以路径的其余部分放得下、“C:” 却被留在上一行。

离屏平台下 QMessageBox.show() 会直接崩溃，这里按同样的规则在 QTextDocument 上排版：
先取正文能收到的最窄宽度，再看每个路径占几行。
"""

import os

# GUIkhQuant 导入时发现哈希种子不是 0 会重启进程
os.environ.setdefault("PYTHONHASHSEED", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtGui import QTextDocument
from PyQt5.QtWidgets import QApplication

from GUIkhQuant import KhQuantGUI

LEGACY = r"C:\Users\WDAGUtilityAccount\AppData\Local\KhQuant\strategies"
CHINESE = r"C:\Users\张三李四王五赵六钱七\AppData\Local\KhQuantOS\strategies"


@pytest.fixture()
def qt_app():
    return QApplication.instance() or QApplication([])


def _lines(new_dir):
    return [
        "检测到 V2.1 的策略目录中有 12 个文件：", LEGACY, "",
        "要把它们复制到开源版的策略目录吗？", new_dir, "",
        "只复制，不改动也不删除 V2.1 目录里的文件，V2.1 仍可照常使用。",
    ]


def _path_line_counts_at_min_width(text, rich, paths):
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    if rich:
        doc.setHtml(text)
    else:
        doc.setPlainText(text)
    doc.setTextWidth(0)
    doc.setTextWidth(doc.idealWidth())   # 最窄宽度 = 最长的不可断开片段
    counts = {}
    block = doc.begin()
    while block.isValid():
        if block.text() in paths:
            counts[block.text()] = block.layout().lineCount()
        block = block.next()
    return counts


@pytest.mark.parametrize("new_dir", [r"D:\KhQuantOS_Data\strategies", CHINESE])
def test_paths_stay_on_one_line(qt_app, new_dir):
    paths = (LEGACY, new_dir)
    html = KhQuantGUI._message_text_with_paths(_lines(new_dir), paths=paths)
    assert _path_line_counts_at_min_width(html, True, set(paths)) == {LEGACY: 1, new_dir: 1}


@pytest.mark.parametrize("path", [LEGACY, CHINESE])
def test_plain_text_would_break_the_path(qt_app, path):
    """对照：原来的纯文本写法在同样条件下会把路径折开（说明上面的测试能发现问题）。"""
    text = "\n".join(["检测到 V2.1 的策略目录中有 12 个文件：", path, "", "要把它们复制过来吗？"])
    assert _path_line_counts_at_min_width(text, False, {path})[path] > 1


def test_text_is_escaped_and_blank_lines_kept():
    html = KhQuantGUI._message_text_with_paths(["a & b", "", r"C:\x<y>"], paths=(r"C:\x<y>",))
    assert "a &amp; b" in html
    assert "<div>&nbsp;</div>" in html
    assert '<div style="white-space:pre">C:\\x&lt;y&gt;</div>' in html
