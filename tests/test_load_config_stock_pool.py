# coding: utf-8
"""加载配置时股票池表格必须跟着配置走：新配置股票池为空时清空，不能留着上一个策略的股票。"""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _methods(names, namespace):
    source = ROOT / "GUIkhQuant.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "KhQuantGUI")
    body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=body, type_ignores=[]), str(source), "exec"), namespace)
    return [namespace[name] for name in names]


def _gui():
    from PyQt5.QtWidgets import QApplication, QCheckBox, QTableWidget, QTableWidgetItem
    from PyQt5.QtCore import Qt
    app = QApplication.instance() or QApplication([])
    ns = {"QTableWidgetItem": QTableWidgetItem, "Qt": Qt}
    load, set_rows = _methods(["load_stock_list_from_config", "_set_stock_list_rows"], ns)
    pool = QCheckBox("沪深300")
    gui = SimpleNamespace(
        stock_list=QTableWidget(0, 2),
        pool_checkboxes={"hs300": pool},
        deleted_stocks={"600000.SH"},
        _load_all_stock_entries=lambda: [("000001.SZ", "平安银行"), ("000002.SZ", "万科Ａ")],
        log_message=MagicMock(),
    )
    gui._set_stock_list_rows = lambda rows: set_rows(gui, rows)
    gui.load = lambda stocks: load(gui, stocks)
    return app, gui, pool


def _codes(table):
    return [table.item(row, 0).text() for row in range(table.rowCount())]


@pytest.mark.parametrize("empty", [[], None])
def test_empty_pool_clears_previous_strategy_stocks(empty):
    app, gui, pool = _gui()
    gui.load(["000001.SZ"])
    pool.setChecked(True)
    assert _codes(gui.stock_list) == ["000001.SZ"]
    gui.load(empty)
    app.processEvents()
    assert gui.stock_list.rowCount() == 0
    assert not pool.isChecked()
    assert gui.deleted_stocks == set()


def test_nonempty_pool_replaces_table_with_names():
    app, gui, _ = _gui()
    gui.load(["000001.SZ"])
    gui.load(["000002.SZ", "000002.SZ", "000001.SZ"])
    assert _codes(gui.stock_list) == ["000002.SZ", "000001.SZ"]
    assert gui.stock_list.item(0, 1).text() == "万科Ａ"


def test_config_without_any_pool_field_also_clears_table():
    tree = ast.parse((ROOT / "GUIkhQuant.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "KhQuantGUI")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "update_ui_from_config")
    source = ast.unparse(fn)
    assert source.count("self.load_stock_list_from_config([])") >= 2
