# coding: utf-8
"""单股库残留 WAL 无法回放时：备份移走后重开，不让该证券永久写库失败。

2026-09-16 强制结束下载进程后约 100 只单股库留下 WAL，DuckDB 回放时报
"Failure while replaying WAL ... GetDefaultDatabase with no default database set"，
之后每次写这些证券都失败。
"""

import os

import duckdb
import pytest

from duckdb_storage import stock_db as stock_db_module
from duckdb_storage.stock_db import StockDB


def _make_db(tmp_path):
    market_dir = tmp_path / "SZ"
    market_dir.mkdir()
    db_path = market_dir / "001356.db"
    duckdb.connect(str(db_path)).close()
    wal_path = str(db_path) + ".wal"
    with open(wal_path, "wb") as handle:
        handle.write(b"broken-wal-tail")
    return db_path, wal_path


def _patch_connect(monkeypatch, error_text, fail_times=1):
    real_connect = duckdb.connect
    calls = {"n": 0}

    def fake_connect(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise duckdb.InternalException(error_text)
        if os.path.exists(str(path) + ".wal"):
            raise AssertionError("WAL should have been moved aside before reconnect")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(stock_db_module.duckdb, "connect", fake_connect)
    return calls


def test_unreplayable_wal_is_moved_and_db_reopens(tmp_path, monkeypatch):
    db_path, wal_path = _make_db(tmp_path)
    calls = _patch_connect(
        monkeypatch,
        'INTERNAL Error: Failure while replaying WAL file "x.db.wal": '
        "Calling DatabaseManager::GetDefaultDatabase with no default database set",
    )
    db = StockDB("001356.SZ", str(tmp_path))
    db.db_path = str(db_path)
    try:
        assert db.conn is not None
    finally:
        db.close(skip_checkpoint=True)
    assert calls["n"] == 2
    assert not os.path.exists(wal_path)
    recovered = tmp_path / "_wal_recovered" / "SZ"
    moved = list(recovered.iterdir())
    assert len(moved) == 1 and moved[0].name.startswith("001356.db.wal.")
    assert moved[0].read_bytes() == b"broken-wal-tail"


def test_other_internal_errors_still_raise(tmp_path, monkeypatch):
    db_path, wal_path = _make_db(tmp_path)
    _patch_connect(monkeypatch, "INTERNAL Error: something else entirely")
    db = StockDB("001356.SZ", str(tmp_path))
    db.db_path = str(db_path)
    with pytest.raises(duckdb.InternalException):
        db.conn
    assert os.path.exists(wal_path)


def test_read_only_open_does_not_touch_wal(tmp_path, monkeypatch):
    db_path, wal_path = _make_db(tmp_path)
    _patch_connect(monkeypatch, "INTERNAL Error: Failure while replaying WAL file")
    db = StockDB("001356.SZ", str(tmp_path), read_only=True)
    db.db_path = str(db_path)
    with pytest.raises(duckdb.InternalException):
        db.conn
    assert os.path.exists(wal_path)
