# coding: utf-8
"""配置位置警告必须使用当前版本实际选出的用户目录。"""
import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def _warning_method(message_box):
    # 只执行原方法，避免为一条保存提示启动主界面和生产服务。
    source = Path(__file__).resolve().parents[1] / "GUIkhQuant.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    gui = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "KhQuantGUI")
    method = next(node for node in gui.body if isinstance(node, ast.FunctionDef) and node.name == "show_internal_dir_warning")
    namespace = {"os": os, "QMessageBox": message_box}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[method.name]


def _message_box_factory(shown):
    class MessageBox:
        Warning = 1
        Ok = 2

        def __init__(self, _parent):
            self.details = None

        def setDetailedText(self, text):
            self.details = text

        def exec_(self):
            shown.append(self.details)

        def __getattr__(self, _name):
            return lambda *_args: None

    return MessageBox


@pytest.mark.parametrize("folder", ["Documents/KhQuant_OS", "Documents/KhQuant", "中文 路径/备用工作区"])
def test_config_warning_uses_selected_user_directory(tmp_path, folder):
    shown, lookups = [], []
    selected = tmp_path / folder / "strategies"

    def selected_directory():
        lookups.append(True)
        return str(selected)

    harness = SimpleNamespace(
        check_file_in_internal_dir=lambda path: bool(path),
        log_message=lambda *_args: None,
        apply_dark_titlebar=lambda *_args: None,
        get_user_strategies_dir=selected_directory,
    )
    _warning_method(_message_box_factory(shown))(harness, "config.kh", "strategy.py")
    assert lookups == [True]
    assert len(shown) == 1
    assert f"• 用户文档目录: {selected.parent}\n" in shown[0]
    assert "config.kh" in shown[0] and "strategy.py" in shown[0]
    assert not selected.exists(), "显示提示不应自行创建或迁移用户文件"


def test_safe_config_does_not_open_warning_or_query_directory():
    shown = []
    harness = SimpleNamespace(check_file_in_internal_dir=lambda _path: False)
    _warning_method(_message_box_factory(shown))(harness, "safe.kh", "safe.py")
    assert shown == []
