# coding: utf-8
"""khDuckWrite / khDuckDB 的路径、时间对齐与字段名约定。"""
import os

import pandas as pd
import pytest

STOCK = "600001.SH"
TIMES = ["2024-01-02 09:30:00", "2024-01-03 09:30:00", "2024-01-04 09:30:00"]


@pytest.fixture
def data_root(tmp_path):
    from duckdb_storage.manager import DuckDBManager
    from duckdb_storage.stock_db import StockDB

    root = tmp_path / "khData"
    db = StockDB(STOCK, str(root))
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
    yield root
    DuckDBManager.close_instances(data_root=str(root))


@pytest.fixture
def settings_root(monkeypatch):
    """让 khDuckDB/khDuckWrite 的默认目录只来自 settings，不受全局数据源影响。"""
    import khDataSource
    import kh_settings as settings

    monkeypatch.setattr(khDataSource, "_data_source_manager", None)
    holder = {"duckdb_data_path": ""}
    monkeypatch.setattr(settings, "load", lambda *a, **k: dict(holder))
    return holder


def _read_col(root, col):
    import duckdb

    from duckdb_storage.manager import DuckDBManager

    DuckDBManager.close_instances(data_root=str(root))
    conn = duckdb.connect(os.path.join(str(root), "SH", "600001.db"), read_only=True)
    try:
        return [r[0] for r in conn.execute(f"SELECT {col} FROM kline_1d ORDER BY time").fetchall()]
    finally:
        conn.close()


def test_default_path_comes_from_settings(data_root, settings_root, tmp_path, monkeypatch):
    from khQTTools import khDuckDB, khDuckWrite

    monkeypatch.chdir(tmp_path)
    settings_root["duckdb_data_path"] = str(data_root)
    df = pd.DataFrame({"time": TIMES, "ma5": [1.0, 2.0, 3.0]})
    res = khDuckWrite(STOCK, "1d", df, fields=["ma5"])
    assert res[STOCK]["updated"] == 3
    out = khDuckDB(STOCK, "1d", fields=["time", "ma5"], start_time="20240101")
    assert out[STOCK]["ma5"].tolist() == [1.0, 2.0, 3.0]
    # 旧默认 ./stock_data 不应再被建出来
    assert not (tmp_path / "stock_data").exists()


def test_no_path_and_no_settings_is_an_error(settings_root, tmp_path, monkeypatch):
    from khQTTools import khDuckDB, khDuckWrite

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="duckdb_path"):
        khDuckWrite(STOCK, "1d", pd.DataFrame({"time": TIMES, "ma5": [1.0, 2.0, 3.0]}))
    assert khDuckDB(STOCK, "1d", start_time="20240101") == {}
    assert not (tmp_path / "stock_data").exists()


def test_khduckdb_does_not_create_missing_directory(tmp_path, capsys):
    from khQTTools import khDuckDB

    missing = tmp_path / "typo_khData"
    assert khDuckDB(STOCK, "1d", duckdb_path=str(missing)) == {}
    assert not missing.exists()
    assert "数据目录不存在" in capsys.readouterr().out


def test_khduckdb_does_not_create_missing_stock_db(data_root):
    from khQTTools import khDuckDB

    out = khDuckDB("000001.SZ", "1d", duckdb_path=str(data_root), start_time="20240101")
    assert out.get("000001.SZ") is None or out["000001.SZ"].empty
    assert not (data_root / "SZ" / "000001.db").exists()


@pytest.mark.parametrize("times", [
    ["2024-01-02", "2024-01-03", "2024-01-04"],
    ["20240102", "20240103", "20240104"],
    ["2024-01-02 00:00:00", "2024-01-03 15:00:00", "2024-01-04 09:30:00"],
])
def test_daily_pure_date_matches_0930_bars(data_root, times):
    from khQTTools import khDuckWrite

    df = pd.DataFrame({"time": times, "ma5": [1.0, 2.0, 3.0]})
    res = khDuckWrite(STOCK, "1d", df, fields=["ma5"], duckdb_path=str(data_root))
    assert res[STOCK]["updated"] == 3
    assert _read_col(data_root, "ma5") == [1.0, 2.0, 3.0]


def test_unmatched_rows_warn(data_root, capsys):
    from khQTTools import khDuckWrite

    df = pd.DataFrame({"time": ["2024-01-02", "2024-01-05"], "ma5": [1.0, 2.0]})
    res = khDuckWrite(STOCK, "1d", df, fields=["ma5"], duckdb_path=str(data_root))
    assert res[STOCK]["updated"] == 1
    assert "1/2 行在库中找不到" in capsys.readouterr().out


def test_field_name_case_insensitive(data_root):
    from khQTTools import khDuckWrite

    khDuckWrite(STOCK, "1d", pd.DataFrame({"time": TIMES, "DIF": [1.0, 2.0, 3.0]}),
                duckdb_path=str(data_root))
    res = khDuckWrite(STOCK, "1d", pd.DataFrame({"time": TIMES, "dif": [4.0, 5.0, 6.0]}),
                      duckdb_path=str(data_root))
    assert res[STOCK]["updated"] == 3
    assert _read_col(data_root, "DIF") == [4.0, 5.0, 6.0]

    with pytest.raises(ValueError, match="大小写"):
        khDuckWrite(STOCK, "1d", pd.DataFrame({"time": TIMES, "DIF": [1.0] * 3, "dif": [2.0] * 3}),
                    duckdb_path=str(data_root))


@pytest.mark.parametrize("field", ["close", "Close", "close_front", "update_time"])
def test_builtin_columns_are_protected(data_root, field):
    from khQTTools import khDuckWrite

    df = pd.DataFrame({"time": TIMES, field: [0.0, 0.0, 0.0]})
    with pytest.raises(ValueError, match="内置列"):
        khDuckWrite(STOCK, "1d", df, fields=[field], duckdb_path=str(data_root))
    assert _read_col(data_root, "close") == [10.2, 11.2, 12.2]


def test_builtin_columns_writable_when_explicit(data_root):
    from khQTTools import khDuckWrite

    df = pd.DataFrame({"time": TIMES, "close": [1.0, 2.0, 3.0]})
    res = khDuckWrite(STOCK, "1d", df, duckdb_path=str(data_root), allow_builtin_fields=True)
    assert res[STOCK]["updated"] == 3
    assert _read_col(data_root, "close") == [1.0, 2.0, 3.0]


def test_write_invalidates_khduckdb_cache(data_root):
    from khQTTools import clear_khDuckDB_cache, khDuckDB, khDuckWrite

    clear_khDuckDB_cache()
    khDuckWrite(STOCK, "1d", pd.DataFrame({"time": TIMES, "ma5": [1.0, 2.0, 3.0]}),
                duckdb_path=str(data_root))
    first = khDuckDB(STOCK, "1d", fields=["time", "ma5"], duckdb_path=str(data_root))
    assert first[STOCK]["ma5"].tolist() == [1.0, 2.0, 3.0]
    khDuckWrite(STOCK, "1d", pd.DataFrame({"time": TIMES, "ma5": [7.0, 8.0, 9.0]}),
                duckdb_path=str(data_root))
    second = khDuckDB(STOCK, "1d", fields=["time", "ma5"], duckdb_path=str(data_root))
    assert second[STOCK]["ma5"].tolist() == [7.0, 8.0, 9.0]
