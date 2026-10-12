# -*- coding: utf-8 -*-
"""WAL 修复确认框的说明要和 wal_repair 的实际行为一致：先备份、环境问题跳过。"""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "duckdb_storage" / "viewer.py").read_text(encoding="utf-8")


def _confirm_text():
    start = SRC.index('"确认修复",')
    return SRC[start:SRC.index("QMessageBox.Yes | QMessageBox.No", start)]


def test_confirm_text_mentions_backup_and_skip():
    text = _confirm_text()
    assert "备份" in text
    assert "跳过" in text
    assert "最后一批还没写完的数据会丢失" not in text
