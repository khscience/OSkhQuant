# coding: utf-8
"""开源版必须和 CS 版保持一致的内核约定（改动这些会让同一策略两边结果不同）。

完整的一致性由主库 scripts/os_build/golden_runner.py 用冻结数据逐笔比对；
这里把最容易被「顺手清理」误改的几处钉成单元测试。
"""
import inspect

import pytest


def test_xtconstant_real_values():
    """策略里常写 xtconstant.STOCK_BUY 等常量，必须是 xtquant 的真实取值。"""
    import kh_constants as c

    assert (c.SECURITY_ACCOUNT, c.STOCK_BUY, c.STOCK_SELL, c.FIX_PRICE) == (2, 23, 24, 11)
    assert c.ORDER_SUCCEEDED == 56
    assert (c.DIRECTION_FLAG_LONG, c.OFFSET_FLAG_OPEN, c.OFFSET_FLAG_CLOSE) == (48, 48, 49)


def test_legacy_call_helper_kept():
    import khQTTools

    helper = khQTTools._legacy_call_with_supported_kwargs

    def target(a, b=0):
        return a, b

    assert helper(target, {"a": 1, "b": 2, "unknown": 3}) == (1, 2)


def test_khhistory_keeps_force_download():
    import khQTTools

    assert "force_download" in inspect.signature(khQTTools.khHistory).parameters


def test_khkline_calls_duckdb_with_cs_kwargs():
    import khQTTools

    captured = {}

    class FakeManager:
        def get_market_data_ex(self, field_list, stock_list, period, start_time="", end_time="",
                               count=-1, dividend_type="none", fill_data=True):
            captured.update(count=count, fill_data=fill_data, dividend_type=dividend_type, period=period)
            return {}

    khQTTools._kline_get_market_data_ex(
        FakeManager(), ["close"], ["000001.SZ"], "1d", "20250101", "20250110", "front",
    )
    assert captured == {"count": -1, "fill_data": True, "dividend_type": "front", "period": "1d"}


def test_both_duckdb_load_paths_and_get_market_data_kept():
    from khDataSource import DataSourceManager

    for name in ("get_market_data_ex", "get_market_data_ex_framework_raw", "get_market_data"):
        assert callable(getattr(DataSourceManager, name, None)), name


def test_gui_pins_hash_seed():
    source = open("GUIkhQuant.py", encoding="utf-8").read()
    assert 'os.environ["PYTHONHASHSEED"] = "0"' in source


@pytest.mark.parametrize("key, expected", [
    ("volume_limit_enabled", False),
    ("participation_rate", 0.1),
    ("allow_partial_fill", True),
    ("risk_free_rate", 0.03),
    ("backtest_data_source", "duckdb"),
])
def test_result_affecting_defaults_match_cs(key, expected):
    import kh_settings

    assert kh_settings.DEFAULTS[key] == expected


def test_only_documented_default_differs_from_cs():
    """开源版有意改动的默认值只有 khHistory 缺数据提示（不影响结果）。"""
    import kh_settings

    assert kh_settings.DEFAULTS["performance_khhistory_missing_data_prompt"] is True
    import performance_config

    # 回测性能的内核默认值仍然是 CS 的：无界面时不弹窗
    assert performance_config.DEFAULT_PERFORMANCE_CONFIG["khhistory_missing_data_prompt"] is False


def test_match_config_uses_cs_defaults():
    source = open("khFrame.py", encoding="utf-8").read()
    assert "self.ui_settings.get('volume_limit_enabled', False)" in source
    assert "self.ui_settings.get('participation_rate', 0.1)" in source
    assert "self.ui_settings.get('allow_partial_fill', True)" in source
