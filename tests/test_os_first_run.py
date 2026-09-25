# coding: utf-8
"""首次启动引导：V2.1 设置只读导入、OneDrive / 系统盘判断、身份隔离。"""
import os

import pytest
from PyQt5.QtCore import QSettings

import kh_first_run as first_run
import qt_settings_bridge
from kh_app_identity import LEGACY_V21_QT_APP, LEGACY_V21_QT_ORG, QT_APP, QT_ORG


@pytest.fixture
def ini_settings(tmp_path, monkeypatch):
    """所有 QSettings(org, app) 改成临时 ini，测试绝不碰真实注册表。"""
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path / "qsettings"))

    def factory(org, app, *args):
        return QSettings(QSettings.IniFormat, QSettings.UserScope, org, app)

    monkeypatch.setattr(first_run, "QSettings", factory)
    monkeypatch.setattr(qt_settings_bridge, "QSettings", factory)
    return factory


class _MemorySettings:
    def __init__(self):
        self.values = {}

    def value(self, key, default=None, type=None):  # noqa: A002 - 与 QSettings 接口一致
        value = self.values.get(key, default)
        if type is bool and isinstance(value, str):
            return value.lower() == "true"
        return value

    def setValue(self, key, value):
        self.values[key] = value


def test_import_v21_settings_is_read_only(ini_settings, tmp_path):
    legacy = ini_settings(LEGACY_V21_QT_ORG, LEGACY_V21_QT_APP)
    config = tmp_path / "old.kh"
    config.write_text("{}", encoding="utf-8")
    legacy.setValue("risk_free_rate", "0.025")
    legacy.setValue("max_log_lines", 2345)
    legacy.setValue("last_config_path", str(config))
    legacy.setValue("last_strategy_path", str(tmp_path / "missing.py"))  # 不存在的路径不导入
    legacy.setValue("qmt_path", r"D:\QMT\userdata_mini")  # 开源版不用的键不导入
    legacy.sync()
    before = open(legacy.fileName(), "rb").read()

    assert first_run.v21_settings_available()
    target = _MemorySettings()
    imported = first_run.import_v21_settings(target)

    assert sorted(imported) == ["last_config_path", "max_log_lines", "risk_free_rate"]
    assert target.values["risk_free_rate"] == "0.025"
    assert "qmt_path" not in target.values
    assert open(legacy.fileName(), "rb").read() == before


def test_no_v21_settings(ini_settings):
    assert not first_run.v21_settings_available()
    assert first_run.import_v21_settings(_MemorySettings()) == []


def test_needs_first_run_flag():
    settings = _MemorySettings()
    assert first_run.needs_first_run(settings)
    settings.setValue(first_run.FIRST_RUN_DONE_KEY, True)
    assert not first_run.needs_first_run(settings)


def test_onedrive_and_system_drive(monkeypatch, tmp_path):
    onedrive = tmp_path / "OneDrive"
    monkeypatch.setenv("OneDrive", str(onedrive))
    assert first_run.is_under_onedrive(str(onedrive / "khData"))
    assert not first_run.is_under_onedrive(str(tmp_path / "data"))
    monkeypatch.setenv("SystemDrive", "C:")
    assert first_run.is_on_system_drive(r"C:\Users\me\khData")
    assert not first_run.is_on_system_drive(r"D:\KhQuantOS_Data")


def test_kh_qt_settings_never_uses_v21_or_cs_location(ini_settings):
    """漏改的旧调用传入 V2.1 / CS 的名称，也只会落到开源版自己的位置。"""
    bridged = qt_settings_bridge.KhQtSettings(LEGACY_V21_QT_ORG, LEGACY_V21_QT_APP)
    own = ini_settings(QT_ORG, QT_APP)
    assert os.path.normcase(bridged.qsettings.fileName()) == os.path.normcase(own.fileName())
    assert (QT_ORG, QT_APP) != (LEGACY_V21_QT_ORG, LEGACY_V21_QT_APP)
