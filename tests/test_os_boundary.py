# coding: utf-8
"""发布边界：开源版不含 miniQMT / xtquant、内嵌编辑器、网页端、CLI、定时补充、同花顺等模块，
也不能和 V2.1 / CS 共用设置位置。"""
import os
import re
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 已删除、在开源版里不存在的模块
REMOVED_MODULES = [
    "xtquant", "cli", "editor_debug_modules", "debugpy", "pyautogui", "webapp",
    "GUI", "GUIDataViewer", "GUIScheduler", "GUIScheduledDataSync", "GUIPackageManager",
    "khTheme", "miniQMT_data_viewer", "miniQMT_data_parser",
    "kh_bigqmt_bridge", "kh_qmt_native_bridge",
    "duckdb_storage.import_worker", "duckdb_storage.history_sources",
    "duckdb_storage.history_adapters", "duckdb_storage.ths_config",
    "duckdb_storage.tencent_importer", "duckdb_storage.ths_importer",
]

APP_MODULES = [
    "GUIkhQuant", "SettingsDialog", "backtest_result_window", "BacktestHistoryManager",
    "stock_analysis_window", "update_manager", "kh_first_run", "khFrame", "khQTTools",
    "khQuantImport", "khDataSource", "duckdb_storage.viewer", "duckdb_storage.data_copy_dialog",
    "duckdb_storage.baostock_import_worker", "duckdb_storage.tushare_import_worker",
]


def _source_files():
    """仓库里的 .py 源文件（优先用 git 列表，排除 tests/ 和本地运行产物）。"""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        listed = None
    if listed:
        for rel in listed:
            if rel.startswith("tests/") or not os.path.isfile(os.path.join(ROOT, rel)):
                continue
            yield os.path.join(ROOT, rel)
        return
    for base, dirs, files in os.walk(ROOT):
        rel = os.path.relpath(base, ROOT)
        if rel == ".":
            # 仓库根目录：跳过测试、打包产物和隐藏目录
            dirs[:] = [
                d for d in dirs
                if d not in ("tests", "build", "dist", "Output", "__pycache__") and not d.startswith(".")
            ]
        else:
            dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(base, name)


def test_xtdata_stub_gives_clear_error():
    from kh_xtdata_stub import XtdataUnavailableError, xtdata

    assert not xtdata
    with pytest.raises(XtdataUnavailableError) as info:
        xtdata.get_market_data_ex([], ["000001.SZ"])
    assert "khHistory" in str(info.value)


def test_khquantimport_exports_stub_xtdata():
    import khQuantImport

    assert not khQuantImport.xtdata
    assert khQuantImport.XtQuantTrader is None


FORBIDDEN_PATTERNS = [
    (r"^\s*(import|from)\s+xtquant\b", "导入 xtquant"),
    (r"^\s*(import|from)\s+cli(\.|\s|$)", "导入 CLI 模块"),
    (r"editor_debug_modules|EmbeddedVSCodeManager|DebugModeManager", "内嵌编辑器 / 调试"),
    (r"GUIScheduledDataSync|GUIScheduler|GUIDataViewer|GUIPackageManager|miniQMT_data", "旧 GUI 模块"),
    (r"ths_config|fuyao|扶摇|同花顺", "同花顺数据源"),
    (r"bigqmt|qmt_native|kh_qmt_native_bridge", "大QMT原生桥"),
    (r"series2-update", "CS 的更新通道"),
    (r"国金证券QMT交易端|userdata_mini", "QMT 路径"),
    (r"launch_web_workbench|web_launcher", "网页端"),
]


def test_no_removed_features_in_source():
    problems = []
    for path in _source_files():
        rel = os.path.relpath(path, ROOT)
        text = open(path, encoding="utf-8").read()
        for pattern, label in FORBIDDEN_PATTERNS:
            for match in re.finditer(pattern, text, flags=re.MULTILINE):
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{rel}:{line} {label}: {match.group(0).strip()[:60]}")
    assert not problems, "\n".join(problems)


def test_identity_strings_only_in_identity_module():
    """V2.1 / CS 的 QSettings 名称、设置目录、互斥名只能出现在 kh_app_identity.py 里。"""
    patterns = [r"['\"]StockAnalyzer['\"]", r"\.khquant['\"/\\]", r"KhQuant\.Desktop"]
    problems = []
    for path in _source_files():
        rel = os.path.relpath(path, ROOT)
        if rel == "kh_app_identity.py":
            continue
        text = open(path, encoding="utf-8").read()
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{rel}:{line} {match.group(0)}")
    assert not problems, "\n".join(problems)


def test_all_modules_import_without_removed_modules(tmp_path):
    """把已删除的模块设为不可导入，所有界面和内核模块仍能正常导入。"""
    script = textwrap.dedent(f"""
        import sys
        for name in {REMOVED_MODULES!r}:
            sys.modules[name] = None
        import importlib
        for name in {APP_MODULES!r}:
            importlib.import_module(name)
        print("imported", len({APP_MODULES!r}))
    """)
    env = dict(os.environ)
    env.update({
        "PYTHONHASHSEED": "0",
        "QT_QPA_PLATFORM": "offscreen",
        "USERPROFILE": str(tmp_path / "home"),
        "HOME": str(tmp_path / "home"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "APPDATA": str(tmp_path / "roaming"),
        "PYTHONIOENCODING": "utf-8",
    })
    for key in ("home", "local", "roaming"):
        (tmp_path / key).mkdir()
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-4000:]
    assert f"imported {len(APP_MODULES)}" in result.stdout
