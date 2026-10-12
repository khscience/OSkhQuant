# coding: utf-8
"""测试隔离：测试不读写真实用户目录下的开源版设置、数据目录和注册表。

- 用户目录、%LOCALAPPDATA%、%APPDATA% 在任何被测模块导入之前就指向临时目录
  （kh_app_identity 在导入时算出 ~/.khquant_os，之后再改环境变量就来不及了）；
- QSettings 的原生格式（注册表 HKCU\\Software\\KHQuant\\StockAnalyzerOS）在每个测试里
  换成临时目录下的 ini 文件。
"""
import atexit
import os
import shutil
import sys
import tempfile

import pytest

_ROOT = tempfile.mkdtemp(prefix="khos_tests_")
atexit.register(shutil.rmtree, _ROOT, True)
for _name, _sub in (("USERPROFILE", "home"), ("HOME", "home"),
                    ("LOCALAPPDATA", os.path.join("AppData", "Local")),
                    ("APPDATA", os.path.join("AppData", "Roaming"))):
    _path = os.path.join(_ROOT, _sub)
    os.makedirs(_path, exist_ok=True)
    os.environ[_name] = _path

_REAL_QSETTINGS = None
_ISOLATED_QSETTINGS = None


def _isolated_qsettings_class(real):
    """把 QSettings 的原生格式（注册表）换成 IniFormat，文件路径由 setPath 决定。"""

    class _IsolatedQSettings(real):
        def __init__(self, *args, **kwargs):
            if args and isinstance(args[0], str):
                # (organization, application="") → 原生格式；(fileName, format) 保持不变
                if not (len(args) > 1 and isinstance(args[1], real.Format)) and "format" not in kwargs:
                    args = (real.IniFormat, real.UserScope) + args
            elif args and isinstance(args[0], real.Scope):
                if len(args) > 1 and isinstance(args[1], str):
                    args = (real.IniFormat,) + args
            elif args and isinstance(args[0], real.Format) and args[0] == real.NativeFormat:
                args = (real.IniFormat,) + args[1:]
            super().__init__(*args, **kwargs)

    return _IsolatedQSettings


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path_factory, monkeypatch):
    global _REAL_QSETTINGS, _ISOLATED_QSETTINGS
    from PyQt5 import QtCore

    if _REAL_QSETTINGS is None:
        _REAL_QSETTINGS = QtCore.QSettings
        _ISOLATED_QSETTINGS = _isolated_qsettings_class(_REAL_QSETTINGS)
    real = _REAL_QSETTINGS
    directory = str(tmp_path_factory.mktemp("qsettings"))
    for scope in (real.UserScope, real.SystemScope):
        real.setPath(real.IniFormat, scope, directory)
    previous_format = real.defaultFormat()
    real.setDefaultFormat(real.IniFormat)
    monkeypatch.setattr(QtCore, "QSettings", _ISOLATED_QSETTINGS)
    # 已经 `from PyQt5.QtCore import QSettings` 的模块持有原类，一并替换
    for module in list(sys.modules.values()):
        namespace = getattr(module, "__dict__", None)
        if isinstance(namespace, dict) and namespace.get("QSettings") is real:
            monkeypatch.setattr(module, "QSettings", _ISOLATED_QSETTINGS)
    yield
    real.setDefaultFormat(previous_format)
