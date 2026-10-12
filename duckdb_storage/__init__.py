# -*- coding: utf-8 -*-
"""
DuckDB 股票数据存储模块

按股票组织数据文件，每只股票一个独立的.db文件
支持自定义存储路径
"""

from .config import DuckDBConfig
from .stock_db import StockDB, DuplicateTimestampError as StockDuplicateTimestampError
from .manager import DuckDBManager
import importlib


def __getattr__(name):
    """懒加载 DuckDB 读取适配器，避免 import duckdb_storage 时加载多余模块"""
    if name == "xtdata_adapter":
        return importlib.import_module(".xtdata_adapter", __name__)
    raise AttributeError(f"module 'duckdb_storage' has no attribute {name!r}")

__all__ = [
    'DuckDBConfig',
    'StockDB',
    'StockDuplicateTimestampError',
    'DuckDBManager',
    'xtdata_adapter',
]

__version__ = '1.2.0'


def run_viewer(data_root: str = None):
    """
    启动DuckDB数据查看器

    Args:
        data_root: 数据目录路径，默认为None则打开空白界面
    """
    from .viewer import main, DuckDBViewer
    import sys
    from PyQt5.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    viewer = DuckDBViewer(data_root=data_root)
    viewer.show()

    sys.exit(app.exec_())
