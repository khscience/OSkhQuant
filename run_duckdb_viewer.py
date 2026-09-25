# -*- coding: utf-8 -*-
"""
DuckDB数据查看器启动脚本

用法:
    python run_duckdb_viewer.py [数据目录路径]
    
示例:
    python run_duckdb_viewer.py
    python run_duckdb_viewer.py ./stock_data
    python run_duckdb_viewer.py D:/my_stock_data
"""

import sys
import os

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from duckdb_storage.viewer import main

if __name__ == '__main__':
    main()

