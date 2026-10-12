# coding: utf-8
"""DuckDB 数据目录配置与不可变快照保护回归测试。"""

import json
from pathlib import Path

import pytest

from duckdb_storage.manager import DuckDBManager


@pytest.fixture(autouse=True)
def reset_manager_instances():
    DuckDBManager.reset_instance()
    yield
    DuckDBManager.reset_instance()


def test_writable_manager_preserves_existing_root_config(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    config_path = data_root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "data_root": str(data_root),
                "default_dividend_type": "front",
                "auto_create_tables": False,
                "batch_size": 4321,
                "version": "custom-version",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manager = DuckDBManager(data_root=str(data_root), read_only=False)
    manager.close_all()
    saved = json.loads(config_path.read_text(encoding="utf-8"))

    assert saved["default_dividend_type"] == "front"
    assert saved["auto_create_tables"] is False
    assert saved["batch_size"] == 4321
    assert saved["version"] == "custom-version"


def test_writable_manager_refuses_immutable_snapshot_without_changes(tmp_path):
    data_root = tmp_path / "frozen"
    data_root.mkdir()
    config_path = data_root / "config.json"
    manifest_path = data_root / "manifest.json"
    config_bytes = b'{"version":"frozen-v1"}\n'
    config_path.write_bytes(config_bytes)
    manifest_path.write_text(
        json.dumps({"immutable": True, "snapshot_id": "test-golden"}),
        encoding="utf-8",
    )

    with pytest.raises(PermissionError, match="不可变回归快照"):
        DuckDBManager(data_root=str(data_root), read_only=False)

    assert config_path.read_bytes() == config_bytes
    assert not (data_root / "SH").exists()
    assert not (data_root / "metadata.db").exists()


def test_read_only_manager_does_not_rewrite_config(tmp_path):
    data_root = tmp_path / "readonly"
    initializer = DuckDBManager(data_root=str(data_root), read_only=False)
    initializer.close_all()
    DuckDBManager.reset_instance()

    config_path = Path(data_root) / "config.json"
    before = config_path.read_bytes()
    reader = DuckDBManager(data_root=str(data_root), read_only=True)
    reader.close_all()

    assert config_path.read_bytes() == before
