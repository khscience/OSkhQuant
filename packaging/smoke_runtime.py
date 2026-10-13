"""Isolate an explicitly requested smoke run before GUI imports.

Normal launches return without importing Qt or changing settings. The controller
creates the marker and matching run id; incomplete test setups fail closed.
"""
import json
import os
from pathlib import Path
import re


def configure_smoke_runtime():
    value = os.environ.get("KHQUANT_SMOKE_ROOT")
    if not value:
        return
    root = Path(value)
    run_id = os.environ.get("KHQUANT_SMOKE_RUN_ID", "")
    if (
        not re.fullmatch(r"[0-9a-f]{32}", run_id)
        or not root.is_absolute()
        or not root.is_dir()
        or not root.name.startswith("khos_smoke_" + run_id + "_")
        or (root / "run-id.txt").read_text(encoding="utf-8") != run_id
        or os.environ.get("PYTHONHASHSEED") != "0"
    ):
        raise RuntimeError("Invalid isolated smoke runtime configuration")
    root = root.resolve()
    for name, sub in {
        "USERPROFILE": "home", "HOME": "home", "LOCALAPPDATA": "local",
        "APPDATA": "roaming", "TEMP": "tmp", "TMP": "tmp",
    }.items():
        destination = root / sub
        destination.mkdir(exist_ok=True)
        os.environ[name] = str(destination)
    (root / "home/Documents").mkdir(exist_ok=True)
    os.environ["KHQUANT_SINGLE_INSTANCE_KEY"] = "KhQuantOS.Smoke." + run_id

    from PyQt5 import QtCore

    real = QtCore.QSettings
    ini_root = root / "qsettings"
    for scope, directory in ((real.UserScope, "user"), (real.SystemScope, "system")):
        path = ini_root / directory
        path.mkdir(parents=True, exist_ok=True)
        real.setPath(real.IniFormat, scope, str(path))
    real.setDefaultFormat(real.IniFormat)

    class SmokeQSettings(real):
        def __init__(self, *args, **kwargs):
            if kwargs:
                raise TypeError("Smoke QSettings requires positional arguments")
            if not args:
                args = (real.IniFormat, real.UserScope, "KHQuantSmoke", "default")
            elif isinstance(args[0], str):
                if len(args) > 1 and isinstance(args[1], real.Format):
                    filename = Path(args[0]).resolve()
                    if root not in filename.parents:
                        raise RuntimeError("Smoke settings file is outside the test directory")
                    args = (str(filename), real.IniFormat) + args[2:]
                else:
                    args = (real.IniFormat, real.UserScope) + args
            elif isinstance(args[0], real.Scope):
                args = (real.IniFormat,) + args
            elif isinstance(args[0], real.Format):
                args = (real.IniFormat,) + args[1:]
            else:
                raise TypeError("Unsupported smoke QSettings constructor")
            super().__init__(*args)
            self.setFallbacksEnabled(False)

    QtCore.QSettings = SmokeQSettings
    (root / "runtime.json").write_text(json.dumps({
        "run_id": run_id, "pid": os.getpid(), "settings_format": "INI",
        "settings_root": str(ini_root), "instance_key": os.environ["KHQUANT_SINGLE_INSTANCE_KEY"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


configure_smoke_runtime()
