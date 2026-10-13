# coding: utf-8
"""空股票池必须在启动前失败，不能偷偷替换为示例股票。"""
import ast
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
EMPTY_MESSAGE = "请先在股票池设置里添加股票"


def _method(filename, class_name, method_name, namespace):
    source = ROOT / filename
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name)
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[method_name]


def _framework(data, tmp_path):
    from khConfig import KhConfig
    path = tmp_path / "input.kh"
    path.write_text(json.dumps({"data": data}), encoding="utf-8")
    obj = SimpleNamespace(config=KhConfig(str(path)))
    fn = _method("khFrame.py", "KhQuantFramework", "get_stock_list", {"logging": logging})
    obj.get_stock_list = lambda: fn(obj)
    return obj


@pytest.mark.parametrize("data", [{}, {"stock_list": []}, {"stock_list": None}, {"stock_list": [" ", "", None]}, {"stock_pool": []}])
def test_framework_empty_pool_fails_before_runtime_initialization(data, tmp_path):
    fw = _framework(data, tmp_path)
    # 原始 run 方法第一步必须失败，后续账户/行情/策略初始化依赖均不应访问。
    run = _method("khFrame.py", "KhQuantFramework", "run", {})
    with pytest.raises(ValueError, match=EMPTY_MESSAGE):
        run(fw)
    assert not hasattr(fw, "start_time")


@pytest.mark.parametrize("field", ["stock_list", "stock_pool"])
def test_framework_nonempty_pool_preserves_user_selection(field, tmp_path):
    stocks = ["600036.SH", "513050.SH"]
    fw = _framework({field: stocks}, tmp_path)
    assert fw.get_stock_list() == stocks


def test_legacy_file_is_read_without_rewriting_config(tmp_path):
    file = tmp_path / "股票 清单.txt"
    file.write_text("\ufeff600036.SH\n\n513050.SH\n", encoding="utf-8")
    fw = _framework({"stock_list_file": str(file)}, tmp_path)
    before = Path(fw.config.config_path).read_bytes()
    assert fw.get_stock_list() == ["600036.SH", "513050.SH"]
    assert Path(fw.config.config_path).read_bytes() == before


@pytest.mark.parametrize("field", ["stock_list", "stock_pool"])
def test_explicit_empty_pool_is_not_replaced_by_legacy_file(field, tmp_path):
    file = tmp_path / "old.txt"
    file.write_text("000001.SZ\n", encoding="utf-8")
    fw = _framework({field: [], "stock_list_file": str(file)}, tmp_path)
    with pytest.raises(ValueError, match=EMPTY_MESSAGE):
        fw.get_stock_list()


@pytest.mark.parametrize("kind", ["missing", "directory", "bad_encoding", "empty"])
def test_legacy_read_failure_or_empty_file_never_selects_default(kind, tmp_path):
    file = tmp_path / "stocks.txt"
    if kind == "directory":
        file.mkdir()
    elif kind == "bad_encoding":
        file.write_bytes(b"\xff\xfe\xff")
    elif kind == "empty":
        file.write_text("\n \n", encoding="utf-8")
    fw = _framework({"stock_list_file": str(file)}, tmp_path)
    if kind == "empty":
        with pytest.raises(ValueError, match=EMPTY_MESSAGE):
            fw.get_stock_list()
    else:
        with pytest.raises(RuntimeError, match="读取股票池失败") as caught:
            fw.get_stock_list()
        assert caught.value.__cause__ is not None


def test_config_read_error_is_preserved(tmp_path):
    fw = _framework({"stock_list": ["600036.SH"]}, tmp_path)
    fw.config.get_stock_list = MagicMock(side_effect=OSError("配置读取失败"))
    with pytest.raises(RuntimeError, match="配置读取失败") as caught:
        fw.get_stock_list()
    assert isinstance(caught.value.__cause__, OSError)


def _click_start(tmp_path, ui_stocks, runtime_stocks, read_error=None, runtime_error=None):
    from PyQt5.QtWidgets import QApplication, QPushButton
    from PyQt5.QtCore import Qt
    app = QApplication.instance() or QApplication([])
    box = SimpleNamespace(warning=MagicMock())
    thread = MagicMock()
    thread_factory = MagicMock(return_value=thread)
    callback = MagicMock()
    runtime = {"data": {"stock_list": runtime_stocks}, "strategy_file": "selected.py"}
    gui = SimpleNamespace(
        get_stock_list=MagicMock(return_value=ui_stocks, side_effect=read_error),
        _build_runtime_config_for_run=MagicMock(return_value=runtime, side_effect=runtime_error),
        log_message=MagicMock(), log_error=MagicMock(),
        get_run_mode=lambda: "backtest", _check_data_integrity_before_backtest=MagicMock(return_value=True),
        _resolve_strategy_file_path=lambda path: path,
        debug_mode_enabled=False, strategy_is_running=False,
        on_strategy_error=MagicMock(), update_status=MagicMock(), on_strategy_finished=MagicMock(), on_debug_server_started=MagicMock(),
        start_action=MagicMock(), stop_action=MagicMock(), progress_bar=MagicMock(), progress_text=MagicMock(), progress_container=MagicMock(), status_label=MagicMock(),
    )
    namespace = {"QMessageBox": box, "MyTraderCallback": callback, "StrategyThread": thread_factory,
                 "Qt": Qt, "os": os, "json": json, "__file__": str(tmp_path / "GUIkhQuant.py"),
                 "local_appdata_dir": lambda _name: str(tmp_path / "configs")}
    start = _method("GUIkhQuant.py", "KhQuantGUI", "start_strategy", namespace)
    button = QPushButton("开始运行")
    button.clicked.connect(lambda: start(gui))
    button.click()
    app.processEvents()
    button.close()
    return gui, box, callback, thread_factory, thread


@pytest.mark.parametrize("stocks", [[], None, [" ", "", None]])
def test_gui_start_button_empty_pool_shows_dialog_without_side_effects(stocks, tmp_path):
    gui, box, callback, factory, thread = _click_start(tmp_path, stocks, ["600036.SH"])
    box.warning.assert_called_once_with(gui, "无法开始回测", EMPTY_MESSAGE)
    gui._build_runtime_config_for_run.assert_not_called()
    callback.assert_not_called()
    factory.assert_not_called()
    gui._check_data_integrity_before_backtest.assert_not_called()
    assert not gui.strategy_is_running
    assert not (tmp_path / "configs").exists()


@pytest.mark.parametrize("failure", ["read_error", "runtime_error"])
def test_gui_read_errors_are_visible_and_do_not_start(failure, tmp_path):
    gui, box, callback, factory, _ = _click_start(tmp_path, ["600036.SH"], ["600036.SH"], **{failure: OSError("股票池读取失败")})
    box.warning.assert_called_once_with(gui, "无法开始回测", "股票池读取失败")
    callback.assert_not_called()
    factory.assert_not_called()
    assert not (tmp_path / "configs").exists()


def test_gui_runtime_empty_pool_is_also_blocked(tmp_path):
    gui, box, callback, factory, _ = _click_start(tmp_path, ["600036.SH"], [])
    box.warning.assert_called_once_with(gui, "无法开始回测", EMPTY_MESSAGE)
    callback.assert_not_called()
    factory.assert_not_called()


def test_gui_nonempty_pool_starts_selected_stocks(tmp_path):
    stocks = ["600036.SH", "513050.SH"]
    gui, box, callback, factory, thread = _click_start(tmp_path, stocks, stocks)
    box.warning.assert_not_called()
    gui.log_error.assert_not_called()
    factory.assert_called_once()
    thread.start.assert_called_once()
    assert gui.strategy_is_running
    saved = json.loads(Path(gui.temp_config_path).read_text(encoding="utf-8"))
    assert saved["data"]["stock_list"] == stocks


def test_headless_empty_pool_exits_nonzero_with_clear_error(tmp_path):
    # 无 GUI/CLI 包装器的真实框架 run 也必须让错误到达进程边界。
    script = tmp_path / "headless.py"
    script.write_text('''import sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from khFrame import KhQuantFramework
framework = KhQuantFramework.__new__(KhQuantFramework)
framework.config = SimpleNamespace(config_dict={"data": {"stock_list": []}}, get_stock_list=lambda: [])
framework.run()
''', encoding="utf-8")
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONHASHSEED="0")
    p = subprocess.run([sys.executable, str(script), str(ROOT)], cwd=tmp_path, env=env, capture_output=True, encoding="utf-8", timeout=60)
    assert p.returncode != 0
    assert EMPTY_MESSAGE in p.stderr
    assert not (tmp_path / "backtest_results").exists()
