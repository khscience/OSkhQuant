# coding: utf-8
"""回测历史还原、最大回撤起始峰值、个股分析K线过滤的回归测试（缺陷 5/6/8/9/25，V2）。

全部在无 GUI 主窗口条件下运行：GUI 方法用 SimpleNamespace 作为 self 调用。
"""
import copy
import json
import os
import time
from types import SimpleNamespace

import pandas as pd
import pytest

import khQTTools
from BacktestHistoryManager import BacktestHistoryManager, find_run_config_kh_files
from backtest_result_window import BacktestResultWindow, compute_drawdown_pct
from stock_analysis_window import StockAnalysisWindow, _filter_daily_kline_range


# ───────────────────────── 【5】最大回撤起始峰值 ─────────────────────────

ASSETS = [900000.0, 800000.0, 850000.0]


class _HistoryDrawdownHarness:
    _to_float_optional = staticmethod(BacktestHistoryManager._to_float_optional)
    calculate_max_drawdown = BacktestHistoryManager.calculate_max_drawdown


def test_history_manager_drawdown_uses_init_capital_as_first_peak():
    manager = _HistoryDrawdownHarness()
    series = pd.Series(ASSETS)
    assert manager.calculate_max_drawdown(series, init_capital=1000000) == pytest.approx(20.0)
    # 取不到初始资金时保持原行为
    assert manager.calculate_max_drawdown(series) == pytest.approx(100000 / 900000 * 100)
    assert manager.calculate_max_drawdown(series, init_capital=None) == pytest.approx(11.1111111)


def test_history_manager_parse_fallback_passes_init_capital(tmp_path):
    import csv

    def _write_csv(path, fieldnames, row):
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(row)

    class _HistoryParserHarness:
        extract_backtest_time = BacktestHistoryManager.extract_backtest_time
        extract_strategy_name = BacktestHistoryManager.extract_strategy_name
        strategy_display_name = BacktestHistoryManager.strategy_display_name
        _read_first_csv_row = staticmethod(BacktestHistoryManager._read_first_csv_row)
        _to_float = staticmethod(BacktestHistoryManager._to_float)
        _to_float_optional = staticmethod(BacktestHistoryManager._to_float_optional)
        calculate_max_drawdown = BacktestHistoryManager.calculate_max_drawdown
        parse_result_directory = BacktestHistoryManager.parse_result_directory

    result_dir = tmp_path / "strategy_demo_20250101_20250131_20260730_010203"
    result_dir.mkdir()
    _write_csv(
        result_dir / "config.csv",
        ["start_time", "end_time", "init_capital"],
        {"start_time": "20250101", "end_time": "20250131", "init_capital": "1000000"},
    )
    pd.DataFrame(
        {"date": ["2025-01-02", "2025-01-03", "2025-01-06"], "total_asset": ASSETS}
    ).to_csv(result_dir / "daily_stats.csv", index=False, encoding="utf-8-sig")

    result = _HistoryParserHarness().parse_result_directory(str(result_dir), result_dir.name)

    assert result["max_drawdown"] == pytest.approx(20.0)


def test_result_window_max_drawdown_and_curve_use_init_capital():
    window = SimpleNamespace()
    series = pd.Series(ASSETS)
    assert BacktestResultWindow.calculate_max_drawdown(window, series, 1000000) == pytest.approx(20.0)
    assert BacktestResultWindow.calculate_max_drawdown(window, series) == pytest.approx(11.1111111)
    # 单日亏损在有初始资金时也是回撤
    assert BacktestResultWindow.calculate_max_drawdown(
        window, pd.Series([950000.0]), 1000000
    ) == pytest.approx(5.0)

    curve = compute_drawdown_pct(series, 1000000)
    # 曲线与输入逐点对齐，不补点
    assert len(curve) == len(series)
    assert list(curve.index) == list(series.index)
    assert curve.tolist() == pytest.approx([10.0, 20.0, 15.0])
    assert compute_drawdown_pct(series).tolist() == pytest.approx(
        [0.0, 100000 / 900000 * 100, 50000 / 900000 * 100]
    )
    # 资产超过初始资金后以新高为峰值
    rising = compute_drawdown_pct(pd.Series([1100000.0, 990000.0]), 1000000)
    assert rising.tolist() == pytest.approx([0.0, 10.0])


# ───────────────────── 【8】【9】个股分析 K 线过滤与复权 ─────────────────────

def test_daily_kline_filter_keeps_end_day_with_0930_timestamp():
    df = pd.DataFrame(
        {
            "time": pd.to_datetime(
                ["2025-01-01 09:30:00", "2025-01-02 09:30:00", "2025-01-03 09:30:00", "2025-01-06 09:30:00"]
            ),
            "close": [1.0, 2.0, 3.0, 4.0],
        }
    )
    from datetime import datetime

    filtered = _filter_daily_kline_range(df, datetime(2025, 1, 2), datetime(2025, 1, 3))
    assert filtered["close"].tolist() == [2.0, 3.0]


def test_load_daily_kline_includes_end_day_0930_bar(monkeypatch):
    def fake_history(**kwargs):
        return {
            "000001.SZ": pd.DataFrame(
                {
                    "time": ["2025-01-02 09:30:00", "2025-01-03 09:30:00"],
                    "open": [10.0, 10.1],
                    "high": [10.2, 10.3],
                    "low": [9.9, 10.0],
                    "close": [10.1, 10.2],
                    "volume": [100, 200],
                }
            )
        }

    monkeypatch.setattr(khQTTools, "khHistory", fake_history)
    window = StockAnalysisWindow.__new__(StockAnalysisWindow)
    window.kline_cache = {}
    window.kline_load_error = None
    window.start_time = "20250102"
    window.end_time = "20250103"
    window.dividend_type = "front"

    frame = window._load_daily_kline("000001.SZ")

    assert len(frame) == 2
    assert frame["time"].max() == pd.Timestamp("2025-01-03 09:30:00")


@pytest.mark.parametrize(
    "dividend_type, expected_fq",
    [("front", "pre"), ("back", "post"), ("back_ratio", "post"), ("none", "none")],
)
def test_intraday_kline_uses_dividend_type_and_caches_per_fq(monkeypatch, dividend_type, expected_fq):
    calls = []

    def fake_history(**kwargs):
        calls.append(kwargs)
        return {
            "000001.SZ": pd.DataFrame(
                {
                    "time": ["2025-01-03 09:31:00", "2025-01-03 15:00:00"],
                    "open": [10.0, 10.1],
                    "high": [10.2, 10.3],
                    "low": [9.9, 10.0],
                    "close": [10.1, 10.2],
                    "volume": [100, 200],
                }
            )
        }

    monkeypatch.setattr(khQTTools, "khHistory", fake_history)
    window = StockAnalysisWindow.__new__(StockAnalysisWindow)
    window.kline_cache = {}
    window.intraday_period = "1m"
    window.dividend_type = dividend_type

    frame = window._load_intraday_kline("000001.SZ", "20250103")

    assert len(frame) == 2
    assert calls[-1]["fq"] == expected_fq
    assert any(key.endswith(f"_{expected_fq}") for key in window.kline_cache)

    # 切换复权方式后不能命中旧缓存
    window.dividend_type = "back" if expected_fq != "post" else "front"
    window._load_intraday_kline("000001.SZ", "20250103")
    assert len(calls) == 2
    assert calls[-1]["fq"] != expected_fq


# ───────────────────────── 【25】full_*.kh 选择 ─────────────────────────

def _write_json(path, payload, mtime=None):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_find_run_config_prefers_newest_full_kh(tmp_path):
    now = time.time()
    _write_json(tmp_path / "demo.kh", {"name": "strategy-copy"}, now)
    _write_json(tmp_path / "full__tmp_gui_retry_low_temp_running_config.kh", {"name": "retry"}, now - 10)
    _write_json(tmp_path / "full__tmp_cli_abc.kh", {"name": "cli"}, now)

    files = find_run_config_kh_files(str(tmp_path), r"C:\strategies\demo.py")

    names = [os.path.basename(path) for path in files]
    assert names == [
        "full__tmp_cli_abc.kh",
        "full__tmp_gui_retry_low_temp_running_config.kh",
        "demo.kh",
    ]


def test_find_run_config_falls_back_to_strategy_kh(tmp_path):
    _write_json(tmp_path / "demo.kh", {"name": "strategy-copy"})
    files = find_run_config_kh_files(str(tmp_path), "strategies/demo.py")
    assert [os.path.basename(path) for path in files] == ["demo.kh"]
    assert find_run_config_kh_files(str(tmp_path / "missing"), "") == []


def _history_run_config(strategy_file):
    return {
        "strategy_file": strategy_file,
        "run_mode": "backtest",
        "data_mode": "single_quote",
        "backtest": {
            "start_time": "20240102",
            "end_time": "20240329",
            "init_capital": 500000.0,
            "benchmark": "000905.SH",
            "min_volume": 200,
            "trigger": {"type": "1d"},
            "trade_cost": {"min_commission": 5.0, "commission_rate": 0.0003,
                           "stamp_tax_rate": 0.001, "flow_fee": 0.0},
        },
        "data": {
            "kline_period": "1d",
            "dividend_type": "back",
            "fields": ["open", "close", "amount"],
            "stock_list": ["600000.SH", "000001.SZ"],
        },
        "market_callback": {"pre_market_enabled": False},
        "performance": {"memory_profile": "low", "memory_profile_effective": "low",
                        "memory_profile_decision": {"source": "retry"}},
    }


def test_restore_strategy_config_reads_full_kh_and_passes_run_config(tmp_path):
    result_dir = tmp_path / "result"
    result_dir.mkdir()
    strategy_file = str(tmp_path / "strategies" / "demo.py")
    pd.DataFrame(
        [{"strategy_file": strategy_file, "start_time": "20240102", "end_time": "20240329",
          "init_capital": 500000, "kline_period": "1d", "dividend_type": "back",
          "stock_list": "600000.SH,000001.SZ"}]
    ).to_csv(result_dir / "config.csv", index=False, encoding="utf-8-sig")
    run_config = _history_run_config(strategy_file)
    _write_json(result_dir / "full__tmp_gui_retry_low_temp_running_config.kh", run_config)
    _write_json(result_dir / "demo.kh", {"data": {"kline_period": "1m"}, "backtest": {"start_time": "20200101"}})

    received = {}

    class _Parent:
        def restore_config_from_history(self, config_dict, run_config=None, run_config_path=None):
            received.update(config_dict=config_dict, run_config=run_config, run_config_path=run_config_path)

    harness = SimpleNamespace(parent=_Parent())
    message = BacktestHistoryManager.restore_strategy_config(harness, {"path": str(result_dir)})

    assert received["run_config"] == run_config
    assert os.path.basename(received["run_config_path"]) == "full__tmp_gui_retry_low_temp_running_config.kh"
    assert received["config_dict"]["kline_period"] == "1d"
    assert received["config_dict"]["backtest"]["start_time"] == "20240102"
    assert "full__tmp_gui_retry_low_temp_running_config.kh" in message


# ─────────────────────── 【6】历史还原后运行配置来源 ───────────────────────

class _FakeText:
    def __init__(self, value=""):
        self.value = value

    def setText(self, value):
        self.value = value

    def text(self):
        return self.value


class _FakeDateEdit:
    def __init__(self):
        self.value = None

    def setDate(self, value):
        self.value = value


def _restore_harness(tmp_path, monkeypatch, old_config_path):
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    from GUIkhQuant import KhQuantGUI

    ui_configs = []
    harness = SimpleNamespace(
        config={"strategy_file": "old_A.py", "backtest": {"start_time": "20200101"},
                "data": {"fields": ["close"], "kline_period": "1m"}},
        current_config_file=old_config_path,
        strategy_path=_FakeText("old_A.py"),
        start_date=_FakeDateEdit(),
        end_date=_FakeDateEdit(),
        initial_cash=_FakeText(),
        benchmark_input=_FakeText(),
        min_volume=_FakeText(),
        titles=[],
        logs=[],
        remembered=[],
        _ui_loaded_state_snapshot={"marker": True},
    )
    harness._resolve_strategy_file_path = lambda raw=None, config_path=None: raw
    harness.update_ui_from_config = lambda: ui_configs.append(copy.deepcopy(harness.config))
    harness._sanitize_runtime_slippage_config = KhQuantGUI._sanitize_runtime_slippage_config
    harness._remember_loaded_config_state = lambda: harness.remembered.append(copy.deepcopy(harness.config))
    harness.setWindowTitle = harness.titles.append
    harness.log_message = lambda message, level="INFO": harness.logs.append((level, message))
    return KhQuantGUI, harness, ui_configs


def test_restore_from_history_switches_runtime_source_to_history_config(tmp_path, monkeypatch):
    old_config_path = tmp_path / "A.kh"
    old_a = {"strategy_file": "old_A.py", "backtest": {"start_time": "20200101", "end_time": "20200301"},
             "data": {"kline_period": "1m", "fields": ["close"], "stock_list": ["000002.SZ"]}}
    _write_json(old_config_path, old_a)
    strategy_file = tmp_path / "demo.py"
    strategy_file.write_text("# demo\n", encoding="utf-8")
    run_config = _history_run_config(str(strategy_file))
    config_dict = dict(copy.deepcopy(run_config), kline_period="1d", dividend_type="back",
                       stock_list="600000.SH,000001.SZ")

    KhQuantGUI, harness, ui_configs = _restore_harness(tmp_path, monkeypatch, str(old_config_path))
    KhQuantGUI.restore_config_from_history(
        harness, config_dict, run_config=copy.deepcopy(run_config),
        run_config_path=str(tmp_path / "full__tmp.kh"),
    )

    # 界面还原了整个 data 块（含 fields）
    assert ui_configs[-1]["data"]["fields"] == ["open", "close", "amount"]
    assert ui_configs[-1]["data"]["stock_list"] == ["600000.SH", "000001.SZ"]
    assert ui_configs[-1]["data"]["dividend_type"] == "back"
    # 运行配置来源切换为历史完整配置，不再指向先前的 A.kh
    assert harness.current_config_file is None
    runtime_base = KhQuantGUI._load_strategy_config_for_runtime(harness)
    assert runtime_base["backtest"]["start_time"] == "20240102"
    assert runtime_base["data"]["fields"] == ["open", "close", "amount"]
    assert runtime_base["strategy_file"] == os.path.abspath(str(strategy_file))
    # 运行期字段被剥离，显式的策略级 memory_profile 保留
    assert runtime_base["performance"] == {"memory_profile": "low"}
    # 深拷贝：修改运行配置不影响传入的历史配置
    runtime_base["data"]["fields"].append("x")
    assert harness.config["data"]["fields"] == ["open", "close", "amount"]
    # A.kh 未被改写
    with open(old_config_path, encoding="utf-8") as handle:
        assert json.load(handle) == old_a
    assert harness.remembered, "还原后应记录新的加载基准"
    assert "full__tmp.kh" in harness.titles[-1]


def test_restore_without_full_config_runs_with_ui_values(tmp_path, monkeypatch):
    KhQuantGUI, harness, ui_configs = _restore_harness(tmp_path, monkeypatch, str(tmp_path / "A.kh"))
    config_dict = {"strategy_file": "missing.py", "start_time": "20240102", "end_time": "20240329",
                   "kline_period": "1d", "dividend_type": "front", "stock_list": "600000.SH"}

    KhQuantGUI.restore_config_from_history(harness, config_dict)

    assert harness.current_config_file is None
    assert ui_configs[-1]["data"]["stock_list"] == ["600000.SH"]
    assert harness.config["strategy_file"] == "missing.py"
    # 无完整配置副本时清空界面基准，运行时以界面参数全部覆盖
    assert harness._ui_loaded_state_snapshot is None
