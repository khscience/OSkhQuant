# coding: utf-8
"""物理 DuckDB 与 metadata 索引核验/修复回归测试。"""

import hashlib
import multiprocessing
import time
from pathlib import Path

import duckdb
import pytest

from duckdb_storage.manager import DuckDBManager


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _create_market_db(path, *, rows=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE kline_1d("
            "time TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, "
            "close DOUBLE, volume BIGINT)"
        )
        if rows:
            connection.execute(
                "INSERT INTO kline_1d VALUES "
                "(TIMESTAMP '2025-01-02 15:00:00', 10, 10.2, 9.9, 10.1, 1000)"
            )
    finally:
        connection.close()


@pytest.fixture()
def reindex_root(tmp_path):
    root = tmp_path / "khData"
    for market in ("SH", "SZ", "BJ"):
        (root / market).mkdir(parents=True)

    # 初始化根 metadata.db，但不登记下面这些物理库。
    manager = DuckDBManager(data_root=str(root), read_only=False)
    manager.close_all()
    DuckDBManager.reset_instance()

    _create_market_db(root / "SH" / "600000.db", rows=1)
    _create_market_db(root / "SZ" / "000001.db", rows=0)
    _create_market_db(root / "SH" / "XXXXXX.db", rows=1)
    _create_market_db(root / "SZ" / "metadata.db", rows=0)
    yield root
    DuckDBManager.reset_instance()


def test_statistics_call_unindexed_files_candidates_not_missing_data(reindex_root):
    manager = DuckDBManager(data_root=str(reindex_root), read_only=True)
    stats = manager.get_statistics()

    assert stats["total_database_files"] == 4
    assert stats["total_stocks"] == 2
    assert stats["indexed_stocks"] == 0
    assert stats["unverified_unindexed_database_files"] == 2
    assert stats["unindexed_database_files"] == 2
    assert stats["non_stock_database_files"] == 2


def test_read_only_audit_distinguishes_data_empty_and_non_stock_files(reindex_root):
    metadata_path = reindex_root / "metadata.db"
    before_hash = _sha256(metadata_path)
    progress = []
    manager = DuckDBManager(data_root=str(reindex_root), read_only=True)

    summary = manager.audit_unindexed_databases(progress_callback=progress.append)

    assert summary["candidate_files"] == 4
    assert summary["checked_files"] == 4
    assert summary["valid_data_files"] == 1
    assert summary["empty_data_files"] == 1
    assert summary["non_stock_files"] == 2
    assert summary["failed_files"] == 0
    assert summary["stocks_to_index"] == ["600000.SH"]
    assert summary["metadata_records"] == [("600000.SH", "1d")]
    assert progress[-1] == 100
    assert manager._metadata_conn is None
    assert _sha256(metadata_path) == before_hash


def test_cancelled_audit_discards_partial_results_and_never_writes(reindex_root):
    metadata_path = reindex_root / "metadata.db"
    before_hash = _sha256(metadata_path)
    manager = DuckDBManager(data_root=str(reindex_root), read_only=True)

    summary = manager.audit_unindexed_databases(should_stop=lambda: True)

    assert summary["cancelled"] is True
    assert summary["stocks_to_index"] == []
    assert summary["metadata_records"] == []
    assert _sha256(metadata_path) == before_hash


def test_apply_reindex_backs_up_and_commits_all_periods_in_one_batch(reindex_root):
    reader = DuckDBManager(data_root=str(reindex_root), read_only=True)
    summary = reader.audit_unindexed_databases()
    reader.close_all()
    DuckDBManager.reset_instance()

    writer = DuckDBManager(data_root=str(reindex_root), read_only=False)
    original_connection = writer._metadata_conn
    applied = writer.apply_metadata_reindex(summary)

    assert applied["applied"] is True
    assert applied["updated_stocks"] == 1
    assert applied["updated_periods"] == 1
    assert Path(applied["backup_path"]).is_file()
    assert writer._metadata_conn is original_connection
    backup_connection = duckdb.connect(applied["backup_path"], read_only=True)
    try:
        assert backup_connection.execute("SELECT COUNT(*) FROM stock_list").fetchone() == (0,)
    finally:
        backup_connection.close()
    row = writer._metadata_conn.execute(
        "SELECT market, has_1d, has_1m FROM stock_list WHERE stock_code = '600000.SH'"
    ).fetchone()
    assert row == ("SH", True, False)


def test_zero_result_apply_does_not_touch_metadata_or_create_backup(reindex_root):
    metadata_path = reindex_root / "metadata.db"
    before_hash = _sha256(metadata_path)
    before_backups = set(reindex_root.glob("metadata.db.backup-reindex-*"))
    writer = DuckDBManager(data_root=str(reindex_root), read_only=False)
    empty_summary = {
        "data_root": str(reindex_root),
        "cancelled": False,
        "audit_complete": True,
        "candidate_files": 0,
        "checked_files": 0,
        "failed_files": 0,
        "metadata_records": [],
        "stocks_to_index": [],
    }

    applied = writer.apply_metadata_reindex(empty_summary)
    writer.close_all()

    assert applied["applied"] is False
    assert applied["backup_path"] == ""
    assert _sha256(metadata_path) == before_hash
    assert set(reindex_root.glob("metadata.db.backup-reindex-*")) == before_backups


def test_failed_candidate_blocks_all_writes(tmp_path):
    root = tmp_path / "khData"
    for market in ("SH", "SZ", "BJ"):
        (root / market).mkdir(parents=True)
    writer = DuckDBManager(data_root=str(root), read_only=False)
    writer.close_all()
    DuckDBManager.reset_instance()
    (root / "SH" / "600001.db").write_bytes(b"not a duckdb file")
    metadata_path = root / "metadata.db"
    before_hash = _sha256(metadata_path)

    reader = DuckDBManager(data_root=str(root), read_only=True)
    summary = reader.audit_unindexed_databases()
    reader.close_all()
    DuckDBManager.reset_instance()
    assert summary["failed_files"] == 1

    writer = DuckDBManager(data_root=str(root), read_only=False)
    with pytest.raises(RuntimeError, match="未写入任何索引"):
        writer.apply_metadata_reindex(summary)
    writer.close_all()
    assert _sha256(metadata_path) == before_hash
    assert list(root.glob("metadata.db.backup-reindex-*")) == []


def test_existing_stock_missing_period_flag_is_repaired(tmp_path):
    root = tmp_path / "khData"
    for market in ("SH", "SZ", "BJ"):
        (root / market).mkdir(parents=True)
    stock_path = root / "SH" / "600002.db"
    connection = duckdb.connect(str(stock_path))
    try:
        for table_name in ("kline_1d", "kline_1m"):
            connection.execute(
                f"CREATE TABLE {table_name}("
                "time TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, "
                "close DOUBLE, volume BIGINT)"
            )
        connection.execute(
            "INSERT INTO kline_1d VALUES "
            "(TIMESTAMP '2025-01-02', 10, 10.2, 9.9, 10.1, 1000)"
        )
        connection.execute(
            "INSERT INTO kline_1m VALUES "
            "(TIMESTAMP '2025-01-02 09:31:00', 10, 10.2, 9.9, 10.1, 1000)"
        )
    finally:
        connection.close()

    writer = DuckDBManager(data_root=str(root), read_only=False)
    writer.batch_update_metadata([("600002.SH", "1d")])
    writer.close_all()
    DuckDBManager.reset_instance()

    reader = DuckDBManager(data_root=str(root), read_only=True)
    summary = reader.audit_unindexed_databases()
    reader.close_all()
    DuckDBManager.reset_instance()

    assert summary["unindexed_candidates"] == 0
    assert summary["period_flag_candidates"] == 1
    assert summary["metadata_records"] == [("600002.SH", "1m")]

    writer = DuckDBManager(data_root=str(root), read_only=False)
    applied = writer.apply_metadata_reindex(summary)
    flags = writer._metadata_conn.execute(
        "SELECT has_1d, has_1m, has_5m, has_tick "
        "FROM stock_list WHERE stock_code = '600002.SH'"
    ).fetchone()
    assert applied["updated_periods"] == 1
    assert flags == (True, True, False, False)


def test_changed_candidate_file_blocks_commit(reindex_root):
    reader = DuckDBManager(data_root=str(reindex_root), read_only=True)
    summary = reader.audit_unindexed_databases()
    reader.close_all()
    DuckDBManager.reset_instance()

    stock_path = reindex_root / "SH" / "600000.db"
    old_stat = stock_path.stat()
    changed_mtime = old_stat.st_mtime_ns + 1_000_000_000
    stock_path.touch()
    import os
    os.utime(stock_path, ns=(old_stat.st_atime_ns, changed_mtime))

    writer = DuckDBManager(data_root=str(reindex_root), read_only=False)
    with pytest.raises(RuntimeError, match="核验后发生变化"):
        writer.apply_metadata_reindex(summary)
    assert list(reindex_root.glob("metadata.db.backup-reindex-*")) == []


def test_rows_with_missing_required_columns_are_not_indexed(tmp_path):
    root = tmp_path / "khData"
    for market in ("SH", "SZ", "BJ"):
        (root / market).mkdir(parents=True)
    writer = DuckDBManager(data_root=str(root), read_only=False)
    writer.close_all()
    DuckDBManager.reset_instance()

    malformed_path = root / "SH" / "600003.db"
    connection = duckdb.connect(str(malformed_path))
    try:
        connection.execute("CREATE TABLE kline_1d(time TIMESTAMP, close DOUBLE)")
        connection.execute(
            "INSERT INTO kline_1d VALUES (TIMESTAMP '2025-01-02', 10)"
        )
    finally:
        connection.close()

    reader = DuckDBManager(data_root=str(root), read_only=True)
    summary = reader.audit_unindexed_databases()

    assert summary["failed_files"] == 1
    assert summary["valid_data_files"] == 0
    assert summary["metadata_records"] == []
    assert "缺少必要字段" in summary["failures"][0]


def test_cancellation_does_not_wait_for_slow_database_worker(
    reindex_root,
    monkeypatch,
):
    reader = DuckDBManager(data_root=str(reindex_root), read_only=True)
    original = DuckDBManager._inspect_database_periods

    def slow_inspect(*args, **kwargs):
        time.sleep(0.8)
        return original(*args, **kwargs)

    monkeypatch.setattr(DuckDBManager, "_inspect_database_periods", slow_inspect)
    stop_calls = 0

    def should_stop():
        nonlocal stop_calls
        stop_calls += 1
        return stop_calls > 1

    started = time.monotonic()
    summary = reader.audit_unindexed_databases(
        should_stop=should_stop,
        max_workers=1,
    )
    elapsed = time.monotonic() - started

    assert summary["cancelled"] is True
    assert summary["audit_complete"] is False
    assert summary["metadata_records"] == []
    assert elapsed < 0.5


def test_process_isolated_cancellation_terminates_all_audit_children(reindex_root):
    reader = DuckDBManager(data_root=str(reindex_root), read_only=True)
    existing_children = {child.pid for child in multiprocessing.active_children()}
    stop_calls = 0

    def should_stop():
        nonlocal stop_calls
        stop_calls += 1
        return stop_calls > 1

    summary = reader.audit_unindexed_databases(
        should_stop=should_stop,
        max_workers=2,
        process_isolation=True,
    )

    remaining_children = {
        child.pid
        for child in multiprocessing.active_children()
        if child.pid not in existing_children
    }
    assert summary["cancelled"] is True
    assert summary["metadata_records"] == []
    assert remaining_children == set()


def test_process_isolated_keyboard_interrupt_terminates_children(reindex_root):
    reader = DuckDBManager(data_root=str(reindex_root), read_only=True)
    existing_children = {child.pid for child in multiprocessing.active_children()}
    stop_calls = 0

    def interrupt():
        nonlocal stop_calls
        stop_calls += 1
        if stop_calls > 1:
            raise KeyboardInterrupt
        return False

    with pytest.raises(KeyboardInterrupt):
        reader.audit_unindexed_databases(
            should_stop=interrupt,
            max_workers=2,
            process_isolation=True,
        )

    assert {
        child.pid
        for child in multiprocessing.active_children()
        if child.pid not in existing_children
    } == set()
