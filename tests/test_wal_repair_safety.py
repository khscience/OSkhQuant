"""WAL 修复安全性：环境类错误（只读/拒绝访问/磁盘满/内存不足）不删任何文件；
真正需要修复/重建时，动手前主库和 WAL 已完整备份。"""
import os
import stat
import subprocess
import sys

import duckdb
import pytest

from duckdb_storage import wal_repair


ROWS_IN_WAL = 1000


def _make_db_with_wal(path):
    """建库并留下一份“已提交、未 CHECKPOINT”的 WAL（子进程直接退出，不走正常关闭）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    code = (
        "import duckdb, os\n"
        f"c = duckdb.connect({path!r})\n"
        "c.execute(\"SET checkpoint_threshold='1GB'\")\n"
        "c.execute('CREATE TABLE t AS SELECT 1 AS x')\n"
        "c.execute('CHECKPOINT')\n"
        f"c.execute('INSERT INTO t SELECT range FROM range({ROWS_IN_WAL})')\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
    assert os.path.exists(path + ".wal")
    assert os.path.getsize(path + ".wal") > 0


def _make_plain_db(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = duckdb.connect(path)
    conn.execute("CREATE TABLE t AS SELECT 1 AS x")
    conn.close()


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _count(path):
    conn = duckdb.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def _set_readonly(path, readonly):
    os.chmod(path, stat.S_IREAD if readonly else (stat.S_IREAD | stat.S_IWRITE))


# ---------- (a) 主库只读：不删任何文件，数据完整 ----------

def test_readonly_database_is_skipped_and_wal_kept(tmp_path):
    root = tmp_path / "data"
    db = str(root / "SH" / "a.db")
    _make_db_with_wal(db)
    db_bytes = _read_bytes(db)
    wal_bytes = _read_bytes(db + ".wal")

    _set_readonly(db, True)
    try:
        result = wal_repair.repair_data_root(str(root))
        # 只读期间：主库、WAL 一个字节都不能动
        assert _read_bytes(db) == db_bytes
        assert _read_bytes(db + ".wal") == wal_bytes
    finally:
        _set_readonly(db, False)

    assert result["ok"] == 0
    assert result["fixed"] == 0
    assert result["rebuilt"] == 0
    assert result["removed_sidecars"] == 0
    assert result["skipped"] == 1
    assert result["backups"] == []
    assert len(result["failed"]) == 1
    path, message = result["failed"][0]
    assert path == db
    assert "已跳过" in message and "只读" in message
    assert not os.path.exists(result["quarantine_root"])
    # 恢复可写后 WAL 正常回放，1000 行一行不少
    assert _count(db) == ROWS_IN_WAL + 1


@pytest.mark.parametrize("text, reason", [
    ('IO Error: Cannot open file "x.db": 拒绝访问。', "只读"),
    ("IO Error: Cannot open file \"x.db\": Access is denied.", "只读"),
    ("IO Error: Permission denied", "只读"),
    ("[WinError 5] 拒绝访问。: 'x.db.wal'", "只读"),
    ("IO Error: Could not write file: No space left on device", "磁盘"),
    ("[WinError 112] 磁盘空间不足。", "磁盘"),
    ("Out of Memory Error: could not allocate block of size 256KB", "内存"),
    ("Failure while replaying WAL file \"x.db.wal\": IO Error: Access is denied", "只读"),
    ("Serialization Error: Trying to read a database file with version number 64, "
     "but we can only read version 39", "版本"),
    ("something nobody has ever seen", "未识别"),
])
def test_environment_and_unknown_errors_never_touch_files(tmp_path, monkeypatch, text, reason):
    root = tmp_path / "data"
    db = str(root / "a.db")
    _make_plain_db(db)
    with open(db + ".wal", "wb") as f:
        f.write(b"pending-wal")

    def failing_checkpoint(db_path, memory_limit):
        raise Exception(text)

    def forbidden(*args, **kwargs):
        raise AssertionError("环境类错误不应走到删除/移动/重建")

    monkeypatch.setattr(wal_repair, "checkpoint_db", failing_checkpoint)
    monkeypatch.setattr(wal_repair, "remove_sidecars", forbidden)
    monkeypatch.setattr(wal_repair, "move_if_exists", forbidden)
    monkeypatch.setattr(wal_repair, "rebuild_empty_db", forbidden)
    monkeypatch.setattr(wal_repair, "backup_db_files", forbidden)

    result = wal_repair.repair_data_root(str(root))

    assert result["skipped"] == 1
    assert len(result["failed"]) == 1
    assert reason in result["failed"][0][1]
    assert _read_bytes(db + ".wal") == b"pending-wal"
    assert not os.path.exists(result["quarantine_root"])


# ---------- (b) 真需要修复/重建：先备份主库和 WAL ----------

def test_corrupt_wal_is_backed_up_before_removal(tmp_path):
    root = tmp_path / "data"
    db = str(root / "SZ" / "b.db")
    _make_db_with_wal(db)
    with open(db + ".wal", "r+b") as f:
        f.seek(40)
        f.write(b"\xff" * 200)
    db_bytes = _read_bytes(db)
    wal_bytes = _read_bytes(db + ".wal")

    result = wal_repair.repair_data_root(str(root))

    assert result["fixed"] == 1
    assert result["rebuilt"] == 0
    assert result["removed_sidecars"] == 1
    assert len(result["backups"]) == 1
    src, backup_db = result["backups"][0]
    assert src == db
    assert backup_db.startswith(result["backup_root"])
    assert result["backup_root"].startswith(result["quarantine_root"])
    # 备份是删 WAL 之前的原样副本
    assert _read_bytes(backup_db) == db_bytes
    assert _read_bytes(backup_db + ".wal") == wal_bytes
    assert not os.path.exists(db + ".wal")


def test_unreadable_main_db_is_backed_up_then_rebuilt(tmp_path):
    root = tmp_path / "data"
    db = str(root / "SH" / "c.db")
    _make_db_with_wal(db)
    with open(db, "r+b") as f:
        f.write(b"\x00" * 8192)
    db_bytes = _read_bytes(db)
    wal_bytes = _read_bytes(db + ".wal")

    result = wal_repair.repair_data_root(str(root))

    assert result["rebuilt"] == 1
    assert result["failed"] == []
    _, backup_db = result["backups"][0]
    assert _read_bytes(backup_db) == db_bytes
    assert _read_bytes(backup_db + ".wal") == wal_bytes
    # 原位置已是可用的空库，坏库进了隔离目录
    assert os.path.exists(db)
    assert os.path.exists(os.path.join(result["quarantine_root"], "SH", "c.db"))
    # 备份目录在隔离目录下，再次修复不会被扫描到
    assert list(wal_repair.iter_db_files(str(root))) == [db]


def test_backup_failure_aborts_without_deleting(tmp_path, monkeypatch):
    root = tmp_path / "data"
    db = str(root / "a.db")
    _make_plain_db(db)
    with open(db + ".wal", "wb") as f:
        f.write(b"pending-wal")

    def corrupt_checkpoint(db_path, memory_limit):
        raise Exception("Failure while replaying WAL file: bad array new length")

    def broken_copy(src, dst, *args, **kwargs):
        raise OSError("[WinError 112] 磁盘空间不足。")

    def forbidden(*args, **kwargs):
        raise AssertionError("备份失败后不应再做任何破坏性操作")

    monkeypatch.setattr(wal_repair, "checkpoint_db", corrupt_checkpoint)
    monkeypatch.setattr(wal_repair.shutil, "copy2", broken_copy)
    monkeypatch.setattr(wal_repair, "remove_sidecars", forbidden)
    monkeypatch.setattr(wal_repair, "move_if_exists", forbidden)
    monkeypatch.setattr(wal_repair, "rebuild_empty_db", forbidden)

    result = wal_repair.repair_data_root(str(root))

    assert result["backups"] == []
    assert len(result["failed"]) == 1
    assert "备份失败" in result["failed"][0][1]
    assert _read_bytes(db + ".wal") == b"pending-wal"


def test_backup_size_mismatch_aborts(tmp_path, monkeypatch):
    db = str(tmp_path / "a.db")
    _make_plain_db(db)
    real_getsize = os.path.getsize

    def lying_getsize(path):
        size = real_getsize(path)
        return size + 1 if "BKDIR" in str(path) else size

    monkeypatch.setattr(wal_repair.os.path, "getsize", lying_getsize)
    backup = str(tmp_path / "BKDIR" / "a.db")
    with pytest.raises(IOError):
        wal_repair.backup_db_files(db, backup)
    # 不完整的备份不留
    assert not os.path.exists(backup)


def test_env_error_on_retry_puts_wal_back(tmp_path, monkeypatch):
    root = tmp_path / "data"
    db = str(root / "a.db")
    _make_plain_db(db)
    with open(db + ".wal", "wb") as f:
        f.write(b"pending-wal")
    calls = []

    def checkpoint(db_path, memory_limit):
        calls.append(db_path)
        if len(calls) == 1:
            raise Exception("Corrupt database file")
        raise Exception('IO Error: Cannot open file "a.db": 拒绝访问。')

    def forbidden(*args, **kwargs):
        raise AssertionError("环境类错误不应隔离/重建")

    monkeypatch.setattr(wal_repair, "checkpoint_db", checkpoint)
    monkeypatch.setattr(wal_repair, "move_if_exists", forbidden)
    monkeypatch.setattr(wal_repair, "rebuild_empty_db", forbidden)

    result = wal_repair.repair_data_root(str(root))

    assert result["rebuilt"] == 0
    assert result["skipped"] == 1
    assert "备份" in result["failed"][0][1]
    # WAL 已放回原位，和修复前一致
    assert _read_bytes(db + ".wal") == b"pending-wal"


# ---------- (c) 原有正常路径不变 ----------

def test_healthy_writable_database_unchanged(tmp_path):
    root = tmp_path / "data"
    db = str(root / "SH" / "ok.db")
    _make_db_with_wal(db)

    result = wal_repair.repair_data_root(str(root))

    assert result["ok"] == 1
    assert result["failed"] == []
    assert result["backups"] == []
    assert result["skipped"] == 0
    assert not os.path.exists(result["quarantine_root"])
    assert _count(db) == ROWS_IN_WAL + 1


def test_in_use_database_still_reported_with_in_use_prefix(tmp_path):
    root = tmp_path / "data"
    db = str(root / "metadata.db")
    _make_plain_db(db)
    keeper = duckdb.connect(db, read_only=True)
    try:
        result = wal_repair.repair_data_root(str(root))
    finally:
        keeper.close()

    assert result["rebuilt"] == 0
    assert result["backups"] == []
    assert result["failed"][0][1].startswith("被占用")
    assert not os.path.exists(result["quarantine_root"])


def test_classify_repair_error():
    assert wal_repair.classify_repair_error(Exception(
        'Failure while replaying WAL file "x.db.wal": bad array new length'))[0]
    assert wal_repair.classify_repair_error(Exception(
        'IO Error: The file "x.db" exists, but it is not a valid DuckDB database file!'))[0]
    assert wal_repair.classify_repair_error(Exception("Corrupt database file"))[0]
    assert wal_repair.classify_repair_error(OSError(
        "[WinError 32] 另一个程序正在使用此文件，进程无法访问。")) == (False, "被占用")
    assert not wal_repair.classify_repair_error(Exception("Access is denied"))[0]
    assert not wal_repair.is_repairable_error(Exception("random failure"))


def test_corrupt_wal_length_reported_as_allocation_failure_is_repairable():
    # Linux/macOS 上 DuckDB 1.5 回放长度损坏的 WAL 会报内存申请失败，这是文件损坏不是内存不够
    linux = ('Out of Memory Error: Failure while replaying WAL file '
             '"/data/SZ/b.db.wal": Allocation failure')
    assert wal_repair.classify_repair_error(Exception(linux)) == (True, "WAL回放失败或文件损坏")
    windows = 'Failure while replaying WAL file "x.db.wal": bad array new length'
    assert wal_repair.classify_repair_error(Exception(windows))[0]
    # 与 WAL 回放无关的内存不足仍按环境问题跳过
    plain = "Out of Memory Error: could not allocate block of size 256KB (4GB/4GB used)"
    assert wal_repair.classify_repair_error(Exception(plain)) == (False, "内存不足")
