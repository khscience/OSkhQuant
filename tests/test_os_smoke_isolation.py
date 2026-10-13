"""Exercise native process-tree cleanup and Qt registry isolation in subprocesses."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows packaged smoke runtime")


def runner_module():
    spec = importlib.util.spec_from_file_location("os_smoke_runner", ROOT / "packaging/smoke_runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def child_script(tmp_path, code, env):
    script = tmp_path / "probe.py"
    script.write_text(code, encoding="utf-8")
    return subprocess.run([sys.executable, str(script)], env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=20,
                          creationflags=subprocess.CREATE_NO_WINDOW)


def test_normal_runtime_does_not_load_qt_or_change_environment(tmp_path):
    env = dict(os.environ)
    env.pop("KHQUANT_SMOKE_ROOT", None)
    code = f"""import os, runpy, sys
before = dict(os.environ)
runpy.run_path({str(ROOT / 'packaging/smoke_runtime.py')!r})
assert dict(os.environ) == before
assert 'PyQt5.QtCore' not in sys.modules
"""
    result = child_script(tmp_path, code, env)
    assert result.returncode == 0, result.stderr


def test_smoke_qsettings_stays_in_ini_for_all_product_constructors(tmp_path):
    runner = runner_module()
    before = runner.registry_fingerprint()
    run_id = uuid.uuid4().hex
    root = tmp_path / ("khos_smoke_" + run_id + "_probe")
    root.mkdir()
    (root / "run-id.txt").write_text(run_id, encoding="utf-8")
    env = dict(os.environ, KHQUANT_SMOKE_ROOT=str(root), KHQUANT_SMOKE_RUN_ID=run_id, PYTHONHASHSEED="0")
    code = f"""import os, runpy
from pathlib import Path
runpy.run_path({str(ROOT / 'packaging/smoke_runtime.py')!r})
from PyQt5.QtCore import QSettings
root = Path({str(root)!r}).resolve()
for args in [(), ('KHQuant', 'StockAnalyzerOS'), (QSettings.UserScope, 'KHQuant', 'StockAnalyzerOS'),
             (QSettings.NativeFormat, QSettings.UserScope, 'KHQuant', 'StockAnalyzerOS')]:
    settings = QSettings(*args)
    assert settings.format() == QSettings.IniFormat
    assert root in Path(settings.fileName()).resolve().parents
    settings.setValue('smoke_probe', 'isolated')
    settings.sync()
    assert settings.status() == QSettings.NoError
legacy = QSettings('KHQuant', 'StockAnalyzer')
assert legacy.allKeys() == []
assert os.environ['KHQUANT_SINGLE_INSTANCE_KEY'].endswith({run_id!r})
try:
    QSettings('HKEY_CURRENT_USER\\\\Software\\\\KHQuant\\\\StockAnalyzerOS', QSettings.NativeFormat)
except RuntimeError:
    pass
else:
    raise AssertionError('Native registry filename must be rejected')
"""
    result = child_script(tmp_path, code, env)
    assert result.returncode == 0, result.stderr
    assert runner.registry_fingerprint() == before
    assert list((root / "qsettings").rglob("*.ini"))


def test_incomplete_smoke_setup_fails_before_loading_qt(tmp_path):
    env = dict(os.environ, KHQUANT_SMOKE_ROOT=str(tmp_path), KHQUANT_SMOKE_RUN_ID="bad")
    code = f"""import runpy, sys
try:
    runpy.run_path({str(ROOT / 'packaging/smoke_runtime.py')!r})
except RuntimeError:
    assert 'PyQt5.QtCore' not in sys.modules
else:
    raise AssertionError('Invalid smoke configuration was accepted')
"""
    result = child_script(tmp_path, code, env)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("parent_exits", [False, True])
def test_job_cleans_descendants_and_preserves_unrelated_process(tmp_path, parent_exits):
    runner = runner_module()
    script = tmp_path / "process_tree.py"
    result_file = tmp_path / "pids.json"
    script.write_text("""import json, os, pathlib, subprocess, sys, time
if sys.argv[1] == 'child':
    time.sleep(60)
else:
    child = subprocess.Popen([sys.executable, __file__, 'child'], creationflags=subprocess.CREATE_NO_WINDOW)
    pathlib.Path(sys.argv[1]).write_text(json.dumps({'parent': os.getpid(), 'child': child.pid}))
    if sys.argv[2] != 'exit':
        time.sleep(60)
""", encoding="utf-8")
    unrelated = subprocess.Popen([sys.executable, str(script), "child"], creationflags=subprocess.CREATE_NO_WINDOW)
    job = None
    try:
        job = runner.ProcessJob([sys.executable, str(script), str(result_file), "exit" if parent_exits else "wait"],
                                dict(os.environ), tmp_path)
        deadline = time.monotonic() + 10
        while not result_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        pids = json.loads(result_file.read_text())
        assert psutil.pid_exists(pids["child"])
        if parent_exits:
            while job.exit_code() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert job.exit_code() == 0
        assert job.close() == 0
        # Windows can retain a terminated process object briefly after the job's
        # active count reaches zero. Wait for exit, not PID-object reclamation.
        try:
            psutil.Process(pids["child"]).wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
        assert unrelated.poll() is None
    finally:
        if job:
            job.close()
        unrelated.terminate()
        unrelated.wait(timeout=10)
