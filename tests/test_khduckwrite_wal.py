# coding: utf-8
"""khDuckWrite 写完不得留下无法回放的 WAL。

DuckDB 1.4.x：表里有 ``DEFAULT CURRENT_TIMESTAMP`` 列（kline 表的 update_time），
WAL 里又记了 ``ALTER TABLE ADD COLUMN``，进程不 checkpoint 就退出 → 下次打开
报 "Failure while replaying WAL"。只读连接（回测）直接失败；可写连接会把 WAL
移走，刚写的指标整列丢失。khDuckWrite 首次写新字段必定 ALTER。
"""
import os
import subprocess
import sys
import textwrap

import pandas as pd
import pytest

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCK = "600001.SH"
TIMES = ["2024-01-02 09:30:00", "2024-01-03 09:30:00", "2024-01-04 09:30:00"]


def _seed_daily(data_root):
    from duckdb_storage.stock_db import StockDB

    db = StockDB(STOCK, str(data_root))
    try:
        db._ensure_period_table("1d")
        for i, t in enumerate(TIMES):
            db.conn.execute(
                "INSERT INTO kline_1d (time, open, high, low, close, volume, amount) "
                "VALUES (CAST(? AS TIMESTAMP), ?, ?, ?, ?, ?, ?)",
                [t, 10.0 + i, 10.5 + i, 9.5 + i, 10.2 + i, 1000.0, 1.0e6],
            )
    finally:
        db.close()
    assert not os.path.exists(db.db_path + ".wal")
    return db.db_path


def _run_child(script, data_root):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), CODE_DIR, str(data_root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


WRITE_SCRIPT = """
import os, sys
sys.path.insert(0, sys.argv[1])
import pandas as pd
from khQTTools import khDuckDB, khDuckWrite
# 照 4-1 案例的顺序：先 khDuckDB 读基础数据，再 khDuckWrite，最后 khDuckDB 验证。
base = khDuckDB(%r, "1d", fields=["time", "close"], duckdb_path=sys.argv[2])
assert len(base[%r]) == 3, base
df = pd.DataFrame({"time": %r, "ma5": [1.5, 2.5, 3.5]})
res = khDuckWrite(%r, "1d", df, fields=["ma5"], duckdb_path=sys.argv[2])
assert res[%r]["updated"] == 3, res
check = khDuckDB(%r, "1d", fields=["time", "ma5"], duckdb_path=sys.argv[2])
assert check[%r]["ma5"].tolist() == [1.5, 2.5, 3.5], check
sys.stdout.flush()
%s
"""


@pytest.mark.parametrize("exit_stmt", ["", "os._exit(0)"], ids=["normal_exit", "hard_exit"])
def test_khduckwrite_new_column_survives_process_exit(tmp_path, exit_stmt):
    data_root = tmp_path / "khData"
    db_path = _seed_daily(data_root)

    _run_child(WRITE_SCRIPT % (STOCK, STOCK, TIMES, STOCK, STOCK, STOCK, STOCK, exit_stmt), data_root)

    # 新进程只读打开（回测走这条路）必须成功，且指标值仍在。
    read_script = """
import sys
sys.path.insert(0, sys.argv[1])
import duckdb
conn = duckdb.connect(sys.argv[3], read_only=True)
rows = conn.execute("SELECT ma5 FROM kline_1d ORDER BY time").fetchall()
conn.close()
print("ROWS=" + repr([r[0] for r in rows]))
"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", read_script, CODE_DIR, str(data_root), db_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ROWS=[1.5, 2.5, 3.5]" in proc.stdout, proc.stdout + proc.stderr
    assert not (data_root / "_wal_recovered").exists()
