# coding: utf-8
"""窗口标题只写版本号；主窗口工具栏最右边有「了解 V3」按钮，点开是 V3 介绍与版本对比。"""
import os

os.environ.setdefault("PYTHONHASHSEED", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication

import kh_app_identity
import kh_v3_intro
from version import get_version

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def qt_app():
    return QApplication.instance() or QApplication([])


def test_titles_show_version_not_edition():
    major, minor = get_version().split(".")[:2]
    assert kh_app_identity.SHORT_VERSION == f"{major}.{minor}"
    assert kh_app_identity.APP_TITLE == f"看海量化回测平台 V{major}.{minor}"
    import GUIkhQuant

    assert GUIkhQuant.WINDOW_TITLE == f"看海量化回测系统 V{major}.{minor}"
    assert "开源版" not in GUIkhQuant.WINDOW_TITLE
    source = open(os.path.join(ROOT, "kh_first_run.py"), encoding="utf-8").read()
    assert 'setWindowTitle(f"欢迎使用{APP_TITLE}")' in source


def test_v3_button_is_rightmost_in_toolbar():
    source = open(os.path.join(ROOT, "GUIkhQuant.py"), encoding="utf-8").read()
    start = source.index("def create_toolbar(self):")
    end = source.index("\n    def ", start + 10)
    body = source[start:end]
    assert body.index("toolbar.addWidget(self._help_btn)") < body.index("toolbar.addWidget(v3_container)")
    assert body.count("toolbar.add") == body[: body.index("toolbar.addWidget(v3_container)")].count("toolbar.add") + 1
    assert '"✨ 了解 V3"' in body


def test_v3_dialog_highlights_then_comparison(qt_app, monkeypatch):
    opened = []
    monkeypatch.setattr(kh_v3_intro.QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString())))
    dialog = kh_v3_intro.V3IntroDialog()
    try:
        assert dialog.windowTitle() == "看海量化回测平台 V3"
        # 第一块三端通用，第二块 CLI 命令行，各带详情链接
        heads = [h[1] for h in kh_v3_intro.HIGHLIGHTS]
        assert "三端通用" in heads[0] and "Linux" in heads[0] and "macOS" in heads[0]
        assert "CLI" in heads[1]
        urls = [label.property("v3_url") for label in dialog.link_labels]
        assert urls == [kh_v3_intro.LINUX_URL, kh_v3_intro.MACOS_URL, kh_v3_intro.CLI_URL]
        for label in dialog.link_labels:
            label.linkActivated.emit(label.property("v3_url"))
        assert opened == urls
        # 其他区别的对比表
        text = dialog.table.toPlainText()
        assert f"V{kh_app_identity.SHORT_VERSION}（本软件）" in text
        for item, ours, v3 in kh_v3_intro.COMPARISON_ROWS:
            assert item in text and ours in text and v3 in text
        assert f"V{kh_app_identity.SHORT_VERSION} 与 V3 使用同一套回测内核" in kh_v3_intro.intro_text()
        opened.clear()
        dialog.visit_btn.click()
        assert opened == [kh_v3_intro.V3_PAGE_URL]
    finally:
        dialog.deleteLater()


def test_bundled_examples_match_comparison_row():
    """开源版只带 4 个基础案例（双均线 3 个、RSI 1 个），对比表里写的数目要和实际一致。"""
    names = os.listdir(os.path.join(ROOT, "strategies"))
    examples = sorted(name for name in names if name.endswith(".py") and not name.startswith("__"))
    assert len(examples) == 4
    assert all(name.startswith(("【1-MA策略案例】", "【2-RSI策略案例】")) for name in examples)
    assert sorted(name[:-3] for name in examples) == sorted(name[:-3] for name in names if name.endswith(".kh"))
    row = next(row for row in kh_v3_intro.COMPARISON_ROWS if row[0] == "策略案例")
    assert row[1].startswith(f"{len(examples)} 个基础案例")


def test_first_run_dialog_links_to_v3(qt_app, monkeypatch):
    import kh_first_run

    shown = []
    monkeypatch.setattr(kh_first_run, "v21_settings_available", lambda: False)
    monkeypatch.setattr(kh_v3_intro.V3IntroDialog, "exec_", lambda self: shown.append(self.windowTitle()) or 0)
    dialog = kh_first_run.FirstRunDialog(settings=None)
    try:
        assert "了解 V3" in dialog.v3_link.text()
        dialog.v3_link.linkActivated.emit("v3")
        assert shown == [kh_v3_intro.V3_TITLE]
    finally:
        dialog.deleteLater()


def test_strategy_code_error_hints_v3_debugging_once(tmp_path):
    from types import SimpleNamespace

    import GUIkhQuant

    strategy = tmp_path / "我的策略.py"
    strategy.write_text("def khHandlebar(data):\n    return data['close']\n", encoding="utf-8")
    namespace = {}
    exec(compile(strategy.read_text(encoding="utf-8"), str(strategy), "exec"), namespace)
    try:
        namespace["khHandlebar"]({})
    except KeyError as exc:
        try:
            # 框架会把行情字段 KeyError 换成「缺数据」提示再抛出，原异常挂在 __cause__ 上
            raise RuntimeError("缺少 close 数据") from exc
        except RuntimeError as wrapped:
            strategy_error = wrapped

    logs = []
    harness = SimpleNamespace(
        strategy_thread=SimpleNamespace(strategy_file=str(strategy), framework=None),
        log_message=lambda message, level="INFO": logs.append(message),
    )
    harness._error_from_strategy_code = lambda error: GUIkhQuant.KhQuantGUI._error_from_strategy_code(harness, error)

    # 不是策略代码抛出的错误（比如数据问题）不提示
    GUIkhQuant.KhQuantGUI._maybe_hint_v3_debugging(harness, RuntimeError("没有找到任何有效的时间点，无法进行回测"))
    assert logs == []
    GUIkhQuant.KhQuantGUI._maybe_hint_v3_debugging(harness, strategy_error)
    GUIkhQuant.KhQuantGUI._maybe_hint_v3_debugging(harness, strategy_error)
    assert len(logs) == 1 and "V3" in logs[0] and "断点" in logs[0]


def test_comparison_focuses_on_v3_features_and_cases_disclaimer(qt_app):
    items = [row[0] for row in kh_v3_intro.COMPARISON_ROWS]
    assert "获取方式" not in items
    assert "Web 回测" in items and "包管理" in items
    data_row = next(row for row in kh_v3_intro.COMPARISON_ROWS if row[0] == "数据来源")
    for word in ("miniQMT", "QMT", "tx"):
        assert word not in data_row[2]
    assert "更多数据源" in data_row[2]
    for word in ("不指定交易标的", "随机数", "算法", "不构成任何策略或投资建议"):
        assert word in kh_v3_intro.STRATEGY_CASES_NOTE
    dialog = kh_v3_intro.V3IntroDialog()
    try:
        text = dialog.table.toPlainText()
        assert "策略案例 ※" in text
        assert kh_v3_intro.STRATEGY_CASES_NOTE in text
        # 字号比主界面大一号，跟主界面一起缩放
        assert dialog.font_px == kh_v3_intro.dialog_font_px(1.0) == 17
        assert kh_v3_intro.dialog_font_px(1.5) == 25
    finally:
        dialog.deleteLater()


def test_bundled_example_configs_require_users_to_choose_stocks():
    """随包配置不替用户指定交易标的；教程引导用户加载后自行填写。"""
    import json

    folder = os.path.join(ROOT, "strategies")
    for name in os.listdir(folder):
        if not name.endswith(".kh"):
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as handle:
            config = json.load(handle)
        stocks = config["data"]["stock_list"]
        assert stocks == [], (name, stocks)
