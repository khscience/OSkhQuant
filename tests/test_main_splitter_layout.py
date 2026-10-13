# coding: utf-8
"""主窗口三栏可拖动、按默认比例分配宽度；BaoStock 默认单进程下载、最多 4 个进程。"""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module_defs(filename, names, namespace):
    source = ROOT / filename
    tree = ast.parse(source.read_text(encoding="utf-8"))
    body = [
        node for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in names)
        or (isinstance(node, ast.Assign) and any(getattr(t, "id", None) in names for t in node.targets))
    ]
    exec(compile(ast.Module(body=body, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


def _method(filename, class_name, method_name, namespace):
    source = ROOT / filename
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name)
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[method_name]


def _layout_namespace():
    return _module_defs(
        "GUIkhQuant.py",
        {"MAIN_SPLITTER_RATIOS", "MAIN_SPLITTER_STATE_KEY", "main_splitter_default_sizes"},
        {},
    )


def test_default_ratios_give_left_panel_the_most_room():
    ns = _layout_namespace()
    left, middle, right = ns["MAIN_SPLITTER_RATIOS"]
    assert left > right > middle


@pytest.mark.parametrize("width", [1504, 1920, 2000, 2560])
def test_default_sizes_follow_ratios_and_fill_width(width):
    ns = _layout_namespace()
    sizes = ns["main_splitter_default_sizes"](width)
    ratios = ns["MAIN_SPLITTER_RATIOS"]
    assert sum(sizes) == width
    for size, ratio in zip(sizes, ratios):
        assert abs(size / width - ratio / sum(ratios)) < 0.01


class _Settings(dict):
    def value(self, key, default=None):
        return self.get(key, default)

    def setValue(self, key, value):
        self[key] = value


def _splitter_gui(saved=None):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication, QSplitter, QWidget
    app = QApplication.instance() or QApplication([])
    ns = _layout_namespace()
    splitter = QSplitter(Qt.Horizontal)
    for ratio in ns["MAIN_SPLITTER_RATIOS"]:
        splitter.addWidget(QWidget())
    splitter.resize(2000, 600)
    splitter.show()
    app.processEvents()
    settings = _Settings()
    if saved is not None:
        settings[ns["MAIN_SPLITTER_STATE_KEY"]] = saved
    gui = SimpleNamespace(main_splitter=splitter, settings=settings)
    init = _method("GUIkhQuant.py", "KhQuantGUI", "_init_main_splitter_sizes", ns)
    return app, ns, gui, init


def _expected_defaults(ns, splitter):
    usable = splitter.width() - splitter.handleWidth() * (splitter.count() - 1)
    return ns["main_splitter_default_sizes"](usable)


def test_first_launch_uses_default_ratios():
    app, ns, gui, init = _splitter_gui()
    init(gui)
    app.processEvents()
    assert gui.main_splitter.sizes() == _expected_defaults(ns, gui.main_splitter)
    gui.main_splitter.close()


def test_dragged_sizes_are_restored_from_settings():
    app, ns, gui, init = _splitter_gui()
    gui.main_splitter.setSizes([500, 900, 600])
    app.processEvents()
    dragged = gui.main_splitter.sizes()
    gui.settings.setValue(ns["MAIN_SPLITTER_STATE_KEY"], gui.main_splitter.saveState())
    gui.main_splitter.setSizes(ns["main_splitter_default_sizes"](gui.main_splitter.width()))
    init(gui)
    app.processEvents()
    assert gui.main_splitter.sizes() == dragged
    gui.main_splitter.close()


@pytest.mark.parametrize("saved", [b"", b"garbage", "not-bytes"])
def test_invalid_saved_state_falls_back_to_defaults(saved):
    app, ns, gui, init = _splitter_gui(saved)
    init(gui)
    app.processEvents()
    assert gui.main_splitter.sizes() == _expected_defaults(ns, gui.main_splitter)
    gui.main_splitter.close()


def test_main_window_builds_splitter_and_saves_it_on_close():
    tree = ast.parse((ROOT / "GUIkhQuant.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "KhQuantGUI")
    sources = {n.name: ast.unparse(n) for n in cls.body if isinstance(n, ast.FunctionDef)}
    assert "self.main_splitter = QSplitter(Qt.Horizontal)" in sources["init_ui"]
    assert "main_layout.addWidget(self.main_splitter)" in sources["init_ui"]
    assert "self.main_splitter.saveState()" in sources["closeEvent"]


def _default_of(filename, class_name, method_name, arg):
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == class_name)
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name)
    args = fn.args.args
    defaults = dict(zip([a.arg for a in args[len(args) - len(fn.args.defaults):]], fn.args.defaults))
    return ast.literal_eval(defaults[arg])


def test_baostock_defaults_to_one_download_process():
    assert _default_of("duckdb_storage/viewer.py", "BaoStockImportThread", "__init__", "num_workers") == 1
    assert _default_of("duckdb_storage/baostock_import_worker.py", "MultiProcessBaoStockImporter",
                       "__init__", "num_workers") == 1
    viewer = (ROOT / "duckdb_storage" / "viewer.py").read_text(encoding="utf-8")
    assert "self.baostock_workers_spin.setValue(1)" in viewer
    assert "self.baostock_workers_spin.setRange(1, 4)" in viewer


def test_trigger_descriptions_wrap_when_middle_panel_is_narrow():
    """中栏默认收窄、还能拖得更窄：触发方式说明必须自动换行，不能被截断。"""
    from PyQt5.QtWidgets import QApplication, QLabel
    app = QApplication.instance() or QApplication([])
    ns = _module_defs("GUIkhQuant.py", {"wrapped_label"}, {"QLabel": QLabel})
    label = ns["wrapped_label"]("日K线触发无需额外配置，每个交易日按一根日K线触发一次策略")
    assert label.wordWrap()
    source = (ROOT / "GUIkhQuant.py").read_text(encoding="utf-8")
    assert source.count('wrapped_label("') >= 4
    assert 'addWidget(QLabel("日K线触发无需额外配置' not in source
