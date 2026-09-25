# coding: utf-8
"""数据目录归属判断、khDuckWrite 拒绝写入、复制 CS 数据为副本。"""
import hashlib
import json
import os
import subprocess
import sys
import textwrap

import duckdb
import pytest

import kh_data_dir_policy as policy


@pytest.fixture(autouse=True)
def _os_default_dir(tmp_path, monkeypatch):
    """把 OS 默认数据目录指到临时目录，避免碰真实的 %LOCALAPPDATA%。"""
    default_dir = tmp_path / "os_default"
    monkeypatch.setattr(policy, "default_duckdb_dir", lambda: str(default_dir))
    return default_dir


def _make_stock_db(path, rows=5):
    """造一个和 StockDB 结构相近的库：带主键的 K 线表和 stock_info。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE kline_1d (time TIMESTAMP PRIMARY KEY, close DOUBLE, volume DOUBLE)")
    con.execute(
        "INSERT INTO kline_1d SELECT TIMESTAMP '2024-01-02' + INTERVAL (i) DAY, 10 + i, 100 * i "
        f"FROM range({rows}) t(i)"
    )
    con.execute("CREATE TABLE stock_info (key VARCHAR PRIMARY KEY, value VARCHAR)")
    con.execute("INSERT INTO stock_info VALUES ('kline_1d_volume_unit', 'lots')")
    con.close()


def _make_metadata_db(path):
    con = duckdb.connect(str(path))
    con.execute("CREATE SEQUENCE sync_log_seq START 1")
    con.execute("CREATE TABLE stock_list (stock_code VARCHAR PRIMARY KEY, name VARCHAR)")
    con.execute("CREATE TABLE sync_log (id BIGINT DEFAULT nextval('sync_log_seq'), note VARCHAR)")
    con.execute("INSERT INTO stock_list VALUES ('000001.SZ', '平安银行'), ('600000.SH', '浦发银行')")
    con.execute("INSERT INTO sync_log (note) VALUES ('a'), ('b'), ('c')")
    con.close()


def _make_cs_dir(root):
    root.mkdir(parents=True, exist_ok=True)
    _make_metadata_db(root / "metadata.db")
    _make_stock_db(root / "SZ" / "000001.db")
    _make_stock_db(root / "SH" / "600000.db", rows=8)
    (root / "config.json").write_text(
        json.dumps({"data_root": str(root), "default_dividend_type": "none"}), encoding="utf-8"
    )
    return root


def _snapshot(root):
    result = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(base, name)
            with open(full, "rb") as handle:
                result[os.path.relpath(full, root)] = hashlib.sha256(handle.read()).hexdigest()
    return result


def _describe(path):
    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = [row[0] for row in con.execute("select table_name from duckdb_tables() order by 1").fetchall()]
        return {
            "tables": tables,
            "constraints": con.execute(
                "select table_name, constraint_type, constraint_column_names "
                "from duckdb_constraints() order by 1, 2, 3"
            ).fetchall(),
            "sequences": con.execute(
                "select sequence_name, last_value from duckdb_sequences() order by 1"
            ).fetchall(),
            "rows": {t: con.execute(f'select * from "{t}" order by all').fetchall() for t in tables},
        }
    finally:
        con.close()


# ── 归属判断 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [r"D:\khData", "d:/khdata", "D:\\khData\\", r"D:\khData\."])
def test_cs_default_dir_variants(path):
    assert policy.is_cs_default_dir(path)
    assert policy.may_be_shared_with_cs(path)


def test_os_default_dir_is_owned(_os_default_dir):
    assert policy.is_os_owned(str(_os_default_dir))
    assert not policy.may_be_shared_with_cs(str(_os_default_dir))


def test_empty_dir_is_not_shared_and_gets_claimed(tmp_path):
    target = tmp_path / "fresh"
    assert not policy.may_be_shared_with_cs(str(target))
    assert policy.claim_if_new(str(target))
    assert (target / policy.OS_MARKER_NAME).is_file()
    assert policy.is_os_owned(str(target))


def test_dir_with_data_but_no_marker_may_be_shared(tmp_path):
    root = _make_cs_dir(tmp_path / "cs")
    assert policy.has_market_data(str(root))
    assert policy.may_be_shared_with_cs(str(root))
    assert not policy.claim_if_new(str(root))
    assert not (root / policy.OS_MARKER_NAME).exists()
    policy.mark_os_owned(str(root), "测试")
    assert not policy.may_be_shared_with_cs(str(root))


def test_lowercase_market_dir_counts_as_data(tmp_path):
    root = tmp_path / "lower"
    _make_stock_db(root / "sz" / "000001.db")
    assert policy.has_market_data(str(root))


def test_marked_cs_default_dir_is_not_shared(tmp_path, monkeypatch):
    fake_cs = tmp_path / "khData"
    fake_cs.mkdir()
    monkeypatch.setattr(policy, "CS_DEFAULT_DATA_DIRS", (str(fake_cs),))
    assert policy.may_be_shared_with_cs(str(fake_cs))
    policy.mark_os_owned(str(fake_cs))
    assert not policy.may_be_shared_with_cs(str(fake_cs))


# ── khDuckWrite ─────────────────────────────────────────────────────────


def test_refuse_strategy_write_to_shared_dir(tmp_path):
    root = _make_cs_dir(tmp_path / "cs")
    with pytest.raises(policy.SharedDataDirWriteRefused) as info:
        policy.refuse_strategy_write_if_shared(str(root))
    assert "khDuckWrite" in str(info.value)
    assert isinstance(info.value, PermissionError)


def test_strategy_write_to_new_dir_claims_it(tmp_path):
    target = tmp_path / "mine"
    policy.refuse_strategy_write_if_shared(str(target))
    assert policy.is_os_owned(str(target))


def test_khduckwrite_refuses_before_opening_database(tmp_path):
    import pandas as pd
    import khQTTools

    root = _make_cs_dir(tmp_path / "cs")
    before = _snapshot(root)
    frame = pd.DataFrame({"time": pd.to_datetime(["2024-01-02"]), "my_signal": [1.0]})
    with pytest.raises(policy.SharedDataDirWriteRefused):
        khQTTools.khDuckWrite("000001.SZ", "1d", frame, duckdb_path=str(root))
    assert _snapshot(root) == before


# ── 复制 CS 数据 ────────────────────────────────────────────────────────


def test_copy_snapshot_matches_source_and_leaves_source_untouched(tmp_path):
    src = _make_cs_dir(tmp_path / "cs")
    dst = tmp_path / "os_copy"
    before = _snapshot(src)
    progress = []
    result = policy.copy_data_dir_snapshot(
        str(src), str(dst), progress=lambda done, total, rel: progress.append((done, total, rel))
    )
    assert result["total"] == 3
    assert result["copied"] == 3 and result["skipped"] == 0 and result["failed"] == []
    assert progress[-1][:2] == (3, 3)
    assert _snapshot(src) == before
    for rel in ("metadata.db", os.path.join("SZ", "000001.db"), os.path.join("SH", "600000.db")):
        assert _describe(dst / rel) == _describe(src / rel)
    assert policy.is_os_owned(str(dst))
    config = json.loads((dst / "config.json").read_text(encoding="utf-8"))
    assert os.path.normcase(config["data_root"]) == os.path.normcase(os.path.abspath(str(dst)))
    assert not list(dst.rglob("*.partial"))


def test_copy_resumes_and_can_overwrite(tmp_path):
    src = _make_cs_dir(tmp_path / "cs")
    dst = tmp_path / "os_copy"
    policy.copy_data_dir_snapshot(str(src), str(dst))
    again = policy.copy_data_dir_snapshot(str(src), str(dst))
    assert again["copied"] == 0 and again["skipped"] == 3
    forced = policy.copy_data_dir_snapshot(str(src), str(dst), overwrite=True)
    assert forced["copied"] == 3 and forced["skipped"] == 0


def test_copy_stops_when_asked(tmp_path):
    src = _make_cs_dir(tmp_path / "cs")
    dst = tmp_path / "os_copy"
    result = policy.copy_data_dir_snapshot(str(src), str(dst), should_stop=lambda: True)
    assert result["stopped"] and result["copied"] == 0
    assert not (dst / "config.json").exists()


@pytest.mark.parametrize("case", ["same", "nested", "foreign", "empty_src"])
def test_copy_rejects_bad_dirs(tmp_path, case):
    src = _make_cs_dir(tmp_path / "cs")
    if case == "same":
        dst = src
    elif case == "nested":
        dst = src / "inner"
    elif case == "foreign":
        dst = _make_cs_dir(tmp_path / "other")
    else:
        src = tmp_path / "nothing"
        src.mkdir()
        dst = tmp_path / "os_copy"
    with pytest.raises(ValueError):
        policy.copy_data_dir_snapshot(str(src), str(dst))


def test_copy_skips_database_locked_by_another_process(tmp_path):
    """CS 正在写某个库时，这个库记为失败，其余库照常复制。"""
    src = _make_cs_dir(tmp_path / "cs")
    dst = tmp_path / "os_copy"
    locked = src / "SZ" / "000001.db"
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, duckdb
            con = duckdb.connect({str(locked)!r})
            con.execute("select 1").fetchall()
            print("ready", flush=True)
            sys.stdin.readline()
            con.close()
        """)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        result = policy.copy_data_dir_snapshot(str(src), str(dst))
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=30)
    failed = [rel for rel, _error in result["failed"]]
    assert failed == ["SZ/000001.db"]
    assert result["copied"] == 2
    assert not (dst / "SZ" / "000001.db").exists()
    retry = policy.copy_data_dir_snapshot(str(src), str(dst))
    assert retry["copied"] == 1 and retry["skipped"] == 2 and retry["failed"] == []
