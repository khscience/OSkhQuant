# coding: utf-8
"""主界面滑点单位切换与配置往返回归测试。"""

import os
from types import MethodType

os.environ.setdefault("PYTHONHASHSEED", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QComboBox, QLineEdit

from GUIkhQuant import (
    KhQuantGUI,
    _SLIPPAGE_TYPE_TO_LABEL,
    _normalize_trade_cost_config,
    _normalize_slippage_ui_value,
)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


class _Harness:
    pass


def _make_harness(qt_app):
    harness = _Harness()
    harness.slippage_type = QComboBox()
    harness.slippage_value = QLineEdit()
    harness._slippage_value_cache = {"tick": "2", "ratio": "0.1"}
    harness._active_slippage_type = None
    harness.log_message = lambda *_args, **_kwargs: None
    harness.slippage_type.addItems(
        [_SLIPPAGE_TYPE_TO_LABEL["tick"], _SLIPPAGE_TYPE_TO_LABEL["ratio"]]
    )
    harness.slippage_type.setCurrentText(_SLIPPAGE_TYPE_TO_LABEL["ratio"])
    harness._apply_slippage_input_mode = MethodType(
        KhQuantGUI._apply_slippage_input_mode,
        harness,
    )
    harness.slippage_type_changed = MethodType(
        KhQuantGUI.slippage_type_changed,
        harness,
    )
    harness._apply_slippage_input_mode("ratio", save_current=False, log_change=False)
    harness.slippage_type.currentTextChanged.connect(harness.slippage_type_changed)
    return harness


def _switch(harness, slippage_type):
    label = _SLIPPAGE_TYPE_TO_LABEL[slippage_type]
    harness.slippage_type.setCurrentText(label)


def test_slippage_units_keep_independent_values(qt_app):
    harness = _make_harness(qt_app)
    harness.slippage_value.setText("0.25")

    _switch(harness, "tick")
    assert harness.slippage_value.text() == "2"
    harness.slippage_value.setText("7")

    _switch(harness, "ratio")
    assert harness.slippage_value.text() == "0.25"

    settings = KhQuantGUI.get_slippage_settings(harness)
    assert settings == {
        "type": "ratio",
        "tick_size": 0.01,
        "tick_count": 7,
        "ratio": pytest.approx(0.0025),
    }


def test_slippage_keyboard_interaction_does_not_cross_units(qt_app):
    harness = _make_harness(qt_app)
    harness.slippage_value.setText("0.25")

    harness.slippage_type.setFocus()
    QTest.keyClick(harness.slippage_type, Qt.Key_Home)
    qt_app.processEvents()
    assert harness.slippage_value.text() == "2"

    harness.slippage_value.setFocus()
    QTest.keyClick(harness.slippage_value, Qt.Key_A, Qt.ControlModifier)
    QTest.keyClicks(harness.slippage_value, "7")
    harness.slippage_type.setFocus()
    QTest.keyClick(harness.slippage_type, Qt.Key_End)
    qt_app.processEvents()

    assert harness.slippage_value.text() == "0.25"
    settings = KhQuantGUI.get_slippage_settings(harness)
    assert settings["tick_count"] == 7
    assert settings["ratio"] == pytest.approx(0.0025)


def test_zero_slippage_survives_unit_switches(qt_app):
    harness = _make_harness(qt_app)
    harness.slippage_value.setText("0")
    _switch(harness, "tick")
    harness.slippage_value.setText("0")
    _switch(harness, "ratio")

    assert harness.slippage_value.text() == "0"
    settings = KhQuantGUI.get_slippage_settings(harness)
    assert settings["tick_count"] == 0
    assert settings["ratio"] == 0.0


def test_loading_config_restores_active_and_inactive_slippage_values(qt_app):
    harness = _make_harness(qt_app)
    harness._load_slippage_settings = MethodType(
        KhQuantGUI._load_slippage_settings,
        harness,
    )

    harness._load_slippage_settings(
        {"type": "tick", "tick_count": 9, "ratio": 0.0015}
    )
    assert harness.slippage_type.currentText() == _SLIPPAGE_TYPE_TO_LABEL["tick"]
    assert harness.slippage_value.text() == "9"

    _switch(harness, "ratio")
    assert harness.slippage_value.text() == "0.15"
    settings = KhQuantGUI.get_slippage_settings(harness)
    assert settings["tick_count"] == 9
    assert settings["ratio"] == pytest.approx(0.0015)


def test_invalid_slippage_config_falls_back_to_safe_defaults(qt_app):
    harness = _make_harness(qt_app)
    harness._load_slippage_settings = MethodType(
        KhQuantGUI._load_slippage_settings,
        harness,
    )

    harness._load_slippage_settings(
        {"type": "unknown", "tick_count": -3, "ratio": "bad"}
    )

    assert harness.slippage_type.currentText() == _SLIPPAGE_TYPE_TO_LABEL["ratio"]
    assert harness.slippage_value.text() == "0.1"
    settings = KhQuantGUI.get_slippage_settings(harness)
    assert settings["tick_count"] == 2
    assert settings["ratio"] == pytest.approx(0.001)


def test_custom_tick_size_survives_load_switch_and_save(qt_app):
    harness = _make_harness(qt_app)
    harness._load_slippage_settings = MethodType(
        KhQuantGUI._load_slippage_settings,
        harness,
    )

    harness._load_slippage_settings(
        {
            "type": "tick",
            "tick_size": 0.001,
            "tick_count": 3,
            "ratio": 0.002,
        }
    )
    _switch(harness, "ratio")
    _switch(harness, "tick")

    settings = KhQuantGUI.get_slippage_settings(harness)
    assert settings["tick_size"] == pytest.approx(0.001)
    assert settings["tick_count"] == 3


def test_runtime_normalizes_invalid_trade_cost_even_when_ui_is_unchanged():
    raw = {
        "backtest": {
            "trade_cost": {
                "min_commission": "bad",
                "commission_rate": float("inf"),
                "stamp_tax_rate": -1,
                "flow_fee": 101,
                "custom_fee": {"enabled": True},
                "slippage": {
                    "type": "ratio",
                    "ratio": "bad",
                    "tick_size": 0,
                },
            }
        }
    }

    normalized = KhQuantGUI._sanitize_runtime_slippage_config(raw)
    trade_cost = normalized["backtest"]["trade_cost"]

    assert trade_cost == _normalize_trade_cost_config(raw["backtest"]["trade_cost"])
    assert trade_cost["custom_fee"] == {"enabled": True}
    assert trade_cost["slippage"]["ratio"] == pytest.approx(0.001)
    assert trade_cost["slippage"]["tick_size"] == pytest.approx(0.01)


def test_runtime_replaces_non_mapping_backtest_with_safe_empty_config():
    assert KhQuantGUI._sanitize_runtime_slippage_config(
        {"backtest": "broken", "strategy_file": "demo.py"}
    ) == {"backtest": {}, "strategy_file": "demo.py"}


def test_loading_config_without_trade_cost_resets_all_previous_cost_values(qt_app):
    harness = _make_harness(qt_app)
    harness._load_slippage_settings = MethodType(
        KhQuantGUI._load_slippage_settings,
        harness,
    )
    harness.update_realtime_data_group_status = lambda: None
    harness.min_commission = QLineEdit()
    harness.commission_rate = QLineEdit()
    harness.stamp_tax = QLineEdit()
    harness.flow_fee = QLineEdit()

    harness.config = {
        "backtest": {
            "trade_cost": {
                "min_commission": 0.5,
                "commission_rate": 0.00005,
                "stamp_tax_rate": 0,
                "flow_fee": 0,
                "slippage": {
                    "type": "tick",
                    "tick_size": 0.001,
                    "tick_count": 8,
                    "ratio": 0.003,
                },
            }
        }
    }
    KhQuantGUI.update_ui_from_config(harness)
    assert harness.min_commission.text() == "0.5"
    assert harness.slippage_value.text() == "8"

    harness.config = {"backtest": {}}
    KhQuantGUI.update_ui_from_config(harness)

    assert harness.min_commission.text() == "5"
    assert harness.commission_rate.text() == "0.0003"
    assert harness.stamp_tax.text() == "0.001"
    assert harness.flow_fee.text() == "0.1"
    assert harness.slippage_type.currentText() == _SLIPPAGE_TYPE_TO_LABEL["ratio"]
    assert harness.slippage_value.text() == "0.1"


def test_config_save_preserves_non_ui_trade_cost_extensions():
    harness = _Harness()
    harness.config = {
        "backtest": {
            "trade_cost": {
                "stamp_tax_mode": "a_share_legal",
                "transfer_fee_enabled": False,
                "custom_fee_rule": {"enabled": True},
            }
        }
    }
    rebuilt = {
        "backtest": {
            "trade_cost": {
                "min_commission": 0.5,
                "commission_rate": 0.00005,
                "stamp_tax_rate": 0,
                "flow_fee": 0,
                "slippage": {"type": "tick", "tick_count": 3},
            }
        }
    }

    prepared = KhQuantGUI._prepare_config_for_save(harness, rebuilt)
    trade_cost = prepared["backtest"]["trade_cost"]

    assert trade_cost["stamp_tax_mode"] == "a_share_legal"
    assert trade_cost["transfer_fee_enabled"] is False
    assert trade_cost["custom_fee_rule"] == {"enabled": True}
    assert trade_cost["min_commission"] == pytest.approx(0.5)
    assert trade_cost["slippage"]["tick_count"] == 3


@pytest.mark.parametrize(
    ("slippage_type", "value", "expected"),
    [
        ("tick", "0", "0"),
        ("tick", "2.5", "2"),
        ("tick", "101", "2"),
        ("ratio", "0", "0"),
        ("ratio", "0.125", "0.125"),
        ("ratio", "nan", "0.1"),
        ("ratio", "11", "0.1"),
    ],
)
def test_slippage_value_normalization(slippage_type, value, expected):
    assert _normalize_slippage_ui_value(slippage_type, value) == expected
