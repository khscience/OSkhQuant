# -*- coding: utf-8 -*-

import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from duckdb_storage.lock_retry import (
    is_duckdb_lock_error,
    parse_duckdb_lock_error,
    retry_on_duckdb_lock,
)
from duckdb_storage.stock_db import StockDB


def _start_lock_holder(tmp_path: Path, db_path: str, read_only: bool):
    script = tmp_path / ("hold_read.py" if read_only else "hold_write.py")
    script.write_text(
        "import duckdb, sys\n"
        "conn = duckdb.connect(sys.argv[1], read_only=sys.argv[2] == '1')\n"
        "if sys.argv[2] != '1':\n"
        "    conn.execute('BEGIN TRANSACTION')\n"
        "print('READY', flush=True)\n"
        "sys.stdin.readline()\n"
        "if sys.argv[2] != '1':\n"
        "    conn.execute('ROLLBACK')\n"
        "conn.close()\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script), db_path, "1" if read_only else "0"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert proc.stdout.readline().strip() == "READY"
    return proc


def _stop_lock_holder(proc):
    try:
        proc.stdin.write("stop\n")
        proc.stdin.flush()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_retry_helper_only_retries_lock_errors(monkeypatch):
    monkeypatch.setenv("KH_DUCKDB_LOCK_ATTEMPTS", "4")
    monkeypatch.setenv("KH_DUCKDB_LOCK_RETRY_DELAY", "0")
    calls = []

    def operation():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError('IO Error: Cannot open file "I:\\khData\\SH\\600150.db": '
                               '另一个程序正在使用此文件，进程无法访问。')
        return "ok"

    assert retry_on_duckdb_lock(operation) == "ok"
    assert len(calls) == 3

    with pytest.raises(ValueError):
        retry_on_duckdb_lock(lambda: (_ for _ in ()).throw(ValueError("bad sql")))


def test_lock_error_parser_extracts_pid_and_path():
    exc = RuntimeError(
        'IO Error: Cannot open file "i:\\khdata\\sh\\600150.db": 另一个程序正在使用此文件\n\n'
        'File is already open in \nC:\\Python311\\python.exe (PID 49916)'
    )
    assert is_duckdb_lock_error(exc)
    info = parse_duckdb_lock_error(exc, stock_code="600150.SH", period="1d", attempts=5)
    assert info["pid"] == 49916
    assert info["stock"] == "600150.SH"
    assert info["period"] == "1d"
    assert info["db_path"].lower().endswith(r"khdata\sh\600150.db")
    assert info["process"] == r"C:\Python311\python.exe"


def test_writer_retries_then_surfaces_read_process_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("KH_DUCKDB_LOCK_ATTEMPTS", "2")
    monkeypatch.setenv("KH_DUCKDB_LOCK_RETRY_DELAY", "0.01")
    db = StockDB("600150.SH", str(tmp_path), read_only=False)
    _ = db.conn
    db.close()

    holder = _start_lock_holder(tmp_path, db.db_path, read_only=True)
    blocked_writer = StockDB("600150.SH", str(tmp_path), read_only=False)
    try:
        with pytest.raises(Exception) as captured:
            _ = blocked_writer.conn
        assert is_duckdb_lock_error(captured.value)
        assert blocked_writer._conn is None
    finally:
        _stop_lock_holder(holder)

    # 占用解除后，同一个对象可以继续连接，不需要重启应用。
    assert blocked_writer.conn is not None
    blocked_writer.close()


def test_reader_does_not_turn_writer_lock_into_empty_dataframe(tmp_path, monkeypatch):
    monkeypatch.setenv("KH_DUCKDB_LOCK_ATTEMPTS", "2")
    monkeypatch.setenv("KH_DUCKDB_LOCK_RETRY_DELAY", "0.01")
    db_path = tmp_path / "SH" / "600150.db"
    db_path.parent.mkdir(parents=True)
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE kline_1d(time TIMESTAMP, close DOUBLE)")
    conn.close()

    holder = _start_lock_holder(tmp_path, str(db_path), read_only=False)
    reader = StockDB("600150.SH", str(tmp_path), read_only=True)
    try:
        with pytest.raises(Exception) as captured:
            reader.get_kline("1d", fields=["close"])
        assert is_duckdb_lock_error(captured.value)
    finally:
        _stop_lock_holder(holder)
        reader.close()
