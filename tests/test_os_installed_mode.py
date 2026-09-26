# coding: utf-8
"""安装版以普通用户运行（不要求管理员）时不能往安装目录写文件；BaoStock 不支持的代码要跳过。"""
import os
import sys

import pytest


def _source(name):
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name), encoding="utf-8") as handle:
        return handle.read()


def test_temp_run_config_goes_to_local_appdata():
    source = _source("GUIkhQuant.py")
    assert 'config_dir = local_appdata_dir("configs")' in source
    assert 'os.path.join(os.path.dirname(__file__), "configs")' not in source


def test_frozen_app_switches_to_writable_work_dir():
    source = _source("GUIkhQuant.py")
    assert "def use_writable_work_dir():" in source
    main_body = source.split("def main():", 1)[1]
    assert main_body.lstrip().startswith("try:\n        use_writable_work_dir()")


def test_frozen_parquet_cache_root_is_user_writable(tmp_path, monkeypatch):
    import kh_app_identity
    from duckdb_storage import parquet_cache_pack

    monkeypatch.setattr(kh_app_identity, "local_appdata_dir",
                        lambda *parts: str(tmp_path.joinpath("KhQuantOS", *parts)))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    root = parquet_cache_pack.default_pack_root()
    assert str(root).startswith(str(tmp_path))


def test_installer_reads_v21_real_uninstall_key():
    # V2.1 和 CS 的安装脚本写的是 AppId={{GUID}}，Inno 实际建的卸载键是 {GUID}}_is1，
    # 在 WOW6432Node 下（Windows 沙盒装 V2.1.4 实测）；手写的“看海量化回测平台_is1”
    # 卸载 V2.1 后还留着，不能用来判断
    source = _source("installer.iss")
    assert "'{#LegacyAppId}}_is1'" in source
    assert "ReadLegacyVersion(HKLM32, Version)" in source
    lookups = [line for line in source.splitlines() if "RegQueryStringValue" in line]
    assert lookups and not any("看海量化回测平台_is1" in line for line in lookups)


@pytest.mark.parametrize("code, unsupported", [
    ("510300.SH", True), ("513050.SH", True), ("159915.SZ", True), ("161725.SZ", True),
    ("113050.SH", True), ("110059.SH", True), ("123107.SZ", True), ("127056.SZ", True),
    ("600000.SH", False), ("000001.SZ", False), ("300750.SZ", False), ("688981.SH", False),
    ("000300.SH", False), ("399006.SZ", False), ("830799.BJ", False),
])
def test_baostock_skips_funds_and_convertible_bonds(code, unsupported):
    from duckdb_storage.viewer import _baostock_unsupported_code

    assert _baostock_unsupported_code(code) is unsupported


def test_shared_dir_message_keeps_long_path_readable():
    import kh_data_dir_policy as policy

    long_path = "C:\\" + "很长的目录名\\" * 30 + "khData"
    message = policy.shared_dir_write_message(long_path, "打开 BaoStock 导入")
    first_lines = message.split("\n")[:2]
    assert first_lines[0].endswith("：")
    assert len(first_lines[1]) <= 60 and "…" in first_lines[1]
    assert policy.shared_dir_write_message("D:\\khData", "WAL 修复").split("\n")[1] == "D:\\khData"
