# coding: utf-8
"""回测主循环 / 成交记录 / 日统计 / 定时触发 修复回归测试 (2026-10-04)

覆盖:
  [11] 挂单成交后本 bar 被跳过(风控失败)时成交不丢, 只记一次
  [14] 日线一天多轮: 挂单成交只记一次, 日统计/基准每天一行, 收益相对上一交易日
  [15] 自定义定时日末估值用当日日线收盘价; 最后设定点无数据时换日/结束补记
  [5]  summary.max_drawdown 以初始资金为起始峰值
  [24] 结果目录唯一(同秒不复用), benchmark 属于本次运行, 目录名仍可被历史管理器解析
  [1]  最后一天盘后信号转为挂单, 不直接成交
  [19] 盘前回调只看到上一交易日最后一根 bar, 首日无个股行情
  [22][23] 自定义定时(tick): 去重, 全量与分段时刻一致, [T-5,T] 最新一笔, datetime time 列不崩

全部使用内存行情 (FakeDataManager), 不读写用户真实行情库。
"""
import datetime
import json
import os

import numpy as np
import pandas as pd
import pytest

import khFrame
from khFrame import CustomTimeTrigger, KhQuantFramework

CODE = "000001.SZ"
BENCH = "000300.SH"
DAYS = ["2024-01-02", "2024-01-03", "2024-01-04"]


def _ms(day, hms):
    return int(datetime.datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S").timestamp() * 1000)


def _ms_f(day, hms, frac):
    return int((datetime.datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S").timestamp() + frac) * 1000)


class FakeTools:
    def is_trade_day(self, date_str=None):
        d = str(date_str).replace("-", "")
        return datetime.datetime.strptime(d, "%Y%m%d").weekday() < 5

    def get_trade_days_count(self, start_date, end_date):
        s = pd.Timestamp(start_date)
        e = pd.Timestamp(end_date)
        return int(sum(1 for d in pd.date_range(s, e) if d.weekday() < 5))


class FakeDataManager:
    """内存行情: frames[(code, period)] = DataFrame(time ms + 字段); daily_close[(code, YYYYMMDD)] = close"""

    data_source = "memory"
    source_name = "memory"
    duckdb_path = ""

    def __init__(self, frames, daily_close=None):
        self.frames = frames
        self.daily_close = daily_close or {}
        self.daily_queries = []

    def download_history_data(self, *args, **kwargs):
        return None

    def get_market_data_ex(self, field_list=None, stock_list=None, period="1d", start_time="", end_time="", **kwargs):
        out = {}
        for code in stock_list or []:
            df = self.frames.get((code, period))
            if df is None:
                continue
            out[code] = df.copy()
        return out

    def get_market_data(self, field_list=None, stock_list=None, period="1d", start_time="", end_time="", **kwargs):
        self.daily_queries.append((tuple(stock_list or []), start_time, kwargs.get("dividend_type")))
        codes = [c for c in stock_list or [] if (c, str(start_time)) in self.daily_close]
        if not codes:
            # 收尾保存 benchmark.csv 走这里(区间查询)
            rows = {}
            for (c, d), v in self.daily_close.items():
                if c in (stock_list or []) and str(start_time) <= d <= str(end_time):
                    rows.setdefault(c, {})[d] = v
            if not rows:
                return {}
            return {"close": pd.DataFrame(rows).T}
        return {"close": pd.DataFrame({str(start_time): {c: self.daily_close[(c, str(start_time))] for c in codes}})}


def _bench_frame():
    return pd.DataFrame({
        "time": [_ms(d, "00:00:00") for d in DAYS],
        "close": [3000.0, 3010.0, 3020.0],
    })


def _minute_frame(prices_by_day, times=("09:31:00", "09:32:00", "15:00:00")):
    rows = []
    for day, prices in prices_by_day.items():
        for hms, p in zip(times, prices):
            rows.append({"time": _ms(day, hms), "open": p, "high": p + 0.1, "low": p - 0.1,
                         "close": p, "volume": 100000.0, "amount": p * 100000})
    return pd.DataFrame(rows)


def _daily_frame(prices):
    rows = []
    for day, p in zip(DAYS, prices):
        rows.append({"time": _ms(day, "00:00:00"), "open": p, "high": p + 0.2, "low": p - 0.2,
                     "close": p, "volume": 1e7, "amount": p * 1e7})
    return pd.DataFrame(rows)


def _write_strategy(tmp_path, body):
    path = tmp_path / "strat_under_test.py"
    path.write_text("# coding: utf-8\nfrom khQuantImport import *\n" + body, encoding="utf-8")
    return str(path)


def _write_config(tmp_path, strategy_path, *, kline_period, trigger, pre=False, post=False,
                  start="20240102", end="20240104", extra=None):
    cfg = {
        "run_mode": "backtest",
        "account": {"account_id": "test", "account_type": "STOCK"},
        "strategy_file": strategy_path,
        "backtest": {
            "start_time": start,
            "end_time": end,
            "init_capital": 100000,
            "benchmark": BENCH,
            "trigger": trigger,
            "trade_cost": {
                "min_commission": 0.0, "commission_rate": 0.0, "stamp_tax_rate": 0.0, "flow_fee": 0.0,
                "slippage": {"type": "ratio", "ratio": 0.0, "tick_size": 0.01, "tick_count": 0},
            },
        },
        "data": {
            "kline_period": kline_period,
            "stock_list": [CODE],
            "fields": ["open", "high", "low", "close", "volume", "amount"],
            "dividend_type": "none",
        },
        "market_callback": {
            "pre_market_enabled": pre, "pre_market_time": "08:30:00",
            "post_market_enabled": post, "post_market_time": "15:30:00",
        },
        "risk": {"position_limit": 0.95, "order_limit": 100, "loss_limit": 0.1},
    }
    for key, value in (extra or {}).items():
        cfg[key] = value
    path = tmp_path / "strat_under_test.kh"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _build_framework(monkeypatch, tmp_path, cfg_path, strat_path, mgr):
    monkeypatch.setattr(KhQuantFramework, "_init_data_source",
                        lambda self: setattr(self, "data_source_mgr", mgr))
    results_dir = tmp_path / "results"
    results_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(KhQuantFramework, "_get_writable_backtest_dir",
                        lambda self: str(results_dir))
    fw = KhQuantFramework(
        config_path=cfg_path,
        strategy_file=strat_path,
        trader_callback=None,
        ui_settings={"init_data_enabled": False, "backtest_data_source": "duckdb",
                     "duckdb_data_path": str(tmp_path)},
    )
    fw.tools = FakeTools()
    return fw, results_dir


def _run(fw):
    fw.run()
    return fw.backtest_records


def _strategy_module(fw):
    return fw.strategy_module


# ───────────────────────── [11] 挂单成交不丢 ─────────────────────────
def test_pending_fill_kept_when_bar_skipped_by_risk(monkeypatch, tmp_path, capsys):
    strat = _write_strategy(tmp_path, (
        "def init(stocks=None, data=None): pass\n"
        "def khHandlebar(data): return []\n"
        "def khPostMarket(data):\n"
        "    if data['__current_time__']['date'] == '2024-01-02':\n"
        "        return [{'code': '000001.SZ', 'action': 'buy', 'price': 11.0, 'volume': 100, 'reason': 'post'}]\n"
        "    return []\n"
    ))
    cfg = _write_config(tmp_path, strat, kline_period="1m", trigger={"type": "1m"}, post=True,
                        end="20240103")
    frames = {
        (CODE, "1m"): _minute_frame({DAYS[0]: [10.0, 10.0, 10.0], DAYS[1]: [10.0, 10.2, 10.3]}),
        (BENCH, "1d"): _bench_frame(),
    }
    fw, _ = _build_framework(monkeypatch, tmp_path, cfg, strat, FakeDataManager(frames))
    real_check = fw.risk_mgr.check_risk

    def _risk(data):
        info = data.get("__current_time__", {})
        if info.get("date") == DAYS[1] and info.get("time") == "09:31:00":
            return False  # 挂单在这根 bar 成交, 随后被风控跳过
        return real_check(data)

    fw.risk_mgr.check_risk = _risk
    records = _run(fw)
    capsys.readouterr()
    buys = [t for t in records["trades"] if t["action"] == "buy"]
    assert len(buys) == 1, records["trades"]
    assert buys[0]["datetime"].strftime("%Y-%m-%d") == DAYS[1]
    assert fw._unrecorded_fills == []
    # 两个交易日各一行日统计
    assert [r["date"] for r in records["daily_stats"]] == DAYS[:2]


def test_record_results_flushes_buffer_exactly_once():
    fw = KhQuantFramework.__new__(KhQuantFramework)
    fw._unrecorded_fills = [{"code": CODE, "action": "buy", "price": 10.0, "actual_price": 10.0,
                             "volume": 100, "timestamp": _ms(DAYS[0], "09:31:00")}]
    fw._is_ms_timestamp = True
    fw._is_custom_trigger = False
    fw._cached_trade_days = {}
    fw._cached_daily_times = {f"daily_times_{datetime.date(2024, 1, 2)}": [_ms(DAYS[0], "15:00:00")]}
    fw._etf_total_return_enabled = False
    fw.tools = FakeTools()
    fw.trader_callback = None
    fw.config = type("C", (), {"config_dict": {"backtest": {"trigger": {"type": "1m"}}}})()

    class _TM:
        positions = {}
        assets = {"cash": 99000.0, "total_asset": 100000.0, "market_value": 0.0}

        def calculate_commission(self, *a): return 0.0
        def calculate_stamp_tax(self, *a): return 0.0
        def calculate_transfer_fee(self, *a): return 0.0
        def calculate_flow_fee(self, *a): return 0.0

    fw.trade_mgr = _TM()
    fw.backtest_records = {"trades": [], "daily_stats": [], "benchmark_data": [], "init_capital": 100000}
    info = {"_dt": datetime.datetime(2024, 1, 2, 9, 32), "date": DAYS[0], "datetime": "", "time": "09:32:00"}
    fw.record_results(_ms(DAYS[0], "09:32:00"), {}, [], info)
    fw.record_results(_ms(DAYS[0], "09:33:00"), {}, [], info)
    assert len(fw.backtest_records["trades"]) == 1
    assert fw._unrecorded_fills == []


# ───────────────────────── [14] 日线一天多轮 ─────────────────────────
def test_daily_multi_round_records_fill_and_stats_once(monkeypatch, tmp_path, capsys):
    strat = _write_strategy(tmp_path, (
        "ROUNDS = []\n"
        "def init(stocks=None, data=None): pass\n"
        "def khHandlebar(data):\n"
        "    ROUNDS.append(data['__current_time__']['date'])\n"
        "    data['__framework__'].request_next_daily_trigger()\n"
        "    return []\n"
        "def khPostMarket(data):\n"
        "    if data['__current_time__']['date'] == '2024-01-02':\n"
        "        return [{'code': '000001.SZ', 'action': 'buy', 'price': 12.0, 'volume': 1000, 'reason': 'post'}]\n"
        "    return []\n"
    ))
    cfg = _write_config(tmp_path, strat, kline_period="1d",
                        trigger={"type": "1d", "daily_trigger_cap": 3}, post=True)
    frames = {(CODE, "1d"): _daily_frame([10.0, 11.0, 12.0]), (BENCH, "1d"): _bench_frame()}
    fw, _ = _build_framework(monkeypatch, tmp_path, cfg, strat, FakeDataManager(frames))
    records = _run(fw)
    capsys.readouterr()

    rounds = _strategy_module(fw).ROUNDS
    assert rounds.count(DAYS[1]) == 3  # 一天确实跑了 3 轮
    buys = [t for t in records["trades"] if t["action"] == "buy"]
    assert len(buys) == 1, records["trades"]
    stats = records["daily_stats"]
    assert [r["date"] for r in stats] == DAYS
    assert [r["date"] for r in records["benchmark_data"]] == DAYS
    for prev, cur in zip(stats, stats[1:]):
        assert cur["daily_return"] == pytest.approx((cur["total_asset"] - prev["total_asset"]) / prev["total_asset"])


def test_record_daily_stats_same_day_overwrites():
    fw = KhQuantFramework.__new__(KhQuantFramework)
    fw.trade_mgr = type("TM", (), {"assets": {"cash": 100000.0}, "positions": {}})()
    fw.backtest_records = {"daily_stats": [{"date": "2024-01-01", "total_asset": 90000.0}],
                           "benchmark_data": [], "init_capital": 100000}
    fw.config = type("C", (), {"config_dict": {"backtest": {"benchmark": BENCH}, "data": {}}})()
    fw._cached_benchmark_close = {f"benchmark_20240102_{BENCH}": 3000.0}
    fw._benchmark_initial_price = None
    fw._etf_total_return_enabled = False
    fw._is_custom_trigger = False
    fw.trader_callback = None
    fw._record_daily_stats(DAYS[0], None, {})
    fw.trade_mgr.assets["cash"] = 99000.0
    fw._record_daily_stats(DAYS[0], None, {})
    stats = fw.backtest_records["daily_stats"]
    assert [r["date"] for r in stats] == ["2024-01-01", DAYS[0]]
    assert stats[-1]["total_asset"] == 99000.0
    assert stats[-1]["daily_return"] == pytest.approx(99000.0 / 90000.0 - 1)
    assert len(fw.backtest_records["benchmark_data"]) == 1


# ───────────────────────── [1][19] 盘前/最后一天盘后 ─────────────────────────
def test_premarket_sees_previous_bar_and_last_day_post_market_is_queued(monkeypatch, tmp_path, capsys):
    strat = _write_strategy(tmp_path, (
        "PRE = []\n"
        "def init(stocks=None, data=None): pass\n"
        "def khHandlebar(data): return []\n"
        "def khPreMarket(data):\n"
        "    row = data.get('000001.SZ')\n"
        "    close = None\n"
        "    if row is not None and len(row):\n"
        "        close = float(row.get('close'))\n"
        "    PRE.append((data['__current_time__']['date'], data['__current_time__']['time'], close))\n"
        "    return []\n"
        "def khPostMarket(data):\n"
        "    if data['__current_time__']['date'] == '2024-01-04':\n"
        "        return [{'code': '000001.SZ', 'action': 'buy', 'price': 20.0, 'volume': 100, 'reason': 'last'}]\n"
        "    return []\n"
    ))
    cfg = _write_config(tmp_path, strat, kline_period="1m", trigger={"type": "1m"}, pre=True, post=True)
    frames = {
        (CODE, "1m"): _minute_frame({DAYS[0]: [10.0, 10.1, 10.9], DAYS[1]: [11.5, 11.6, 11.9],
                                     DAYS[2]: [12.5, 12.6, 12.9]}),
        (BENCH, "1d"): _bench_frame(),
    }
    fw, _ = _build_framework(monkeypatch, tmp_path, cfg, strat, FakeDataManager(frames))
    records = _run(fw)
    capsys.readouterr()

    pre = _strategy_module(fw).PRE
    assert [p[0] for p in pre] == DAYS
    assert all(p[1] == "08:30:00" for p in pre)
    assert pre[0][2] is None            # 首日没有上一根 bar
    assert pre[1][2] == pytest.approx(10.9)  # 上一交易日 15:00 收盘, 不是当天 09:31
    assert pre[2][2] == pytest.approx(11.9)
    # 最后一天盘后买单转为挂单, 没有成交, 资金不变
    assert records["trades"] == []
    assert fw.trade_mgr.assets["cash"] == pytest.approx(100000)
    pending = fw.pending_order_mgr.get_pending_orders()
    assert len(pending) == 1 and pending[0]["code"] == CODE


def test_premarket_lazy_container_reads_previous_bar(monkeypatch, tmp_path, capsys):
    strat = _write_strategy(tmp_path, (
        "PRE = []\n"
        "def init(stocks=None, data=None): pass\n"
        "def khHandlebar(data): return []\n"
        "def khPreMarket(data):\n"
        "    row = data.get('000001.SZ')\n"
        "    close = None\n"
        "    if row is not None and len(row):\n"
        "        close = float(row.get('close'))\n"
        "    PRE.append(close)\n"
        "    return []\n"
    ))
    cfg = _write_config(tmp_path, strat, kline_period="1m", trigger={"type": "1m"}, pre=True,
                        extra={"performance": {"current_data_container_mode": "lazy",
                                               "current_data_row_mode": "lazy"}})
    frames = {
        (CODE, "1m"): _minute_frame({DAYS[0]: [10.0, 10.1, 10.9], DAYS[1]: [11.5, 11.6, 11.9],
                                     DAYS[2]: [12.5, 12.6, 12.9]}),
        (BENCH, "1d"): _bench_frame(),
    }
    fw, _ = _build_framework(monkeypatch, tmp_path, cfg, strat, FakeDataManager(frames))
    _run(fw)
    capsys.readouterr()
    assert _strategy_module(fw).PRE == [None, pytest.approx(10.9), pytest.approx(11.9)]


# ───────────────────────── [5][24] summary / 结果目录 ─────────────────────────
def test_summary_drawdown_uses_init_capital_and_unique_dirs(monkeypatch, tmp_path, capsys):
    strat = _write_strategy(tmp_path, (
        "def init(stocks=None, data=None): pass\n"
        "def khHandlebar(data):\n"
        "    if data['__current_time__']['date'] == '2024-01-02':\n"
        "        return [{'code': '000001.SZ', 'action': 'buy', 'price': 10.0, 'volume': 5000, 'reason': 'b'}]\n"
        "    return []\n"
    ))
    cfg = _write_config(tmp_path, strat, kline_period="1d", trigger={"type": "1d"})
    # 买入后一路下跌: 首日即亏损
    frames = {(CODE, "1d"): _daily_frame([10.0, 9.0, 8.0]), (BENCH, "1d"): _bench_frame()}
    fixed_now = datetime.datetime(2026, 10, 4, 12, 0, 0)

    class _FixedDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    dirs = []
    for _ in range(2):
        fw, results_dir = _build_framework(monkeypatch, tmp_path, cfg, strat, FakeDataManager(frames))
        monkeypatch.setattr(khFrame.datetime, "datetime", _FixedDT)
        try:
            _run(fw)
        finally:
            monkeypatch.setattr(khFrame.datetime, "datetime", datetime.datetime)
        capsys.readouterr()
    dirs = sorted(os.listdir(results_dir))
    assert len(dirs) == 2, dirs  # 同一秒两次回测不复用目录
    from BacktestHistoryManager import BacktestHistoryManager
    mgr = BacktestHistoryManager.__new__(BacktestHistoryManager)
    for name in dirs:
        assert mgr.extract_backtest_time(name) == "2026-10-04 12:00:00"
        assert name.startswith("strategy_")
        assert os.path.exists(os.path.join(results_dir, name, "benchmark.csv"))
        summary = pd.read_csv(os.path.join(results_dir, name, "summary.csv"))
        stats = pd.read_csv(os.path.join(results_dir, name, "daily_stats.csv"))
        expected = (100000 - stats["total_asset"].min()) / 100000 * 100
        assert summary["max_drawdown"].iloc[0] == pytest.approx(expected)


# ───────────────────────── [22][23] 自定义定时 tick ─────────────────────────
def test_custom_trigger_dedups_per_setpoint_per_day():
    fw = type("F", (), {})()
    trig = CustomTimeTrigger(fw, ["10:00:00", "14:00:30"])
    fired = []
    for day in ("2024-01-02", "2024-01-03"):
        for hms, frac in [("09:59:54", 0), ("09:59:55", 0), ("09:59:58", 0.5), ("10:00:00", 0),
                          ("10:00:04", 0.9), ("10:00:05", 0), ("14:00:27", 0), ("14:00:33", 0)]:
            fw._current_bar_dt = datetime.datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S") \
                + datetime.timedelta(seconds=frac)
            if trig.should_trigger(0, {}):
                fired.append((day, hms))
    assert fired == [("2024-01-02", "09:59:55"), ("2024-01-02", "14:00:27"),
                     ("2024-01-03", "09:59:55"), ("2024-01-03", "14:00:27")]


def test_custom_tick_asof_index_picks_latest_tick_not_after_setpoint():
    day = DAYS[0]
    times = np.array([
        _ms(day, "09:59:54"),          # 窗口外
        _ms_f(day, "09:59:57", 0.5),   # 窗口内
        _ms_f(day, "09:59:59", 0.5),   # 窗口内最新 ← 选它
        _ms_f(day, "10:00:00", 0.5),   # 晚于设定点(未来) 不选
    ], dtype=np.int64)
    idx = khFrame._custom_tick_asof_index(times, [10 * 3600])
    key = _ms(day, "10:00:00") // 1000
    assert idx == {key: 2}
    # datetime64 time 列也能处理(#23: float(datetime64) 会越界)
    dt_values = pd.to_datetime([f"{day} 09:59:57.5", f"{day} 09:59:59.5", f"{day} 10:00:00.5"]).values
    assert khFrame._custom_tick_asof_index(dt_values, [10 * 3600]) == {key: 1}
    mask = khFrame._custom_trigger_keep_mask(dt_values, [10 * 3600], tick_mode=True)
    assert mask.tolist() == [True, True, False]


def _tick_frame():
    rows = []
    for day, base in zip(DAYS[:2], (10.0, 11.0)):
        for hms, frac, px in [("09:59:50", 0, base - 0.5), ("09:59:57", 0.5, base),
                              ("10:00:02", 0, base + 1.0),          # 设定点之后, 不应被用到
                              ("14:00:27", 0.5, base + 0.1), ("14:00:33", 0, base + 2.0),
                              ("14:59:57", 0, base + 0.3)]:
            rows.append({"time": _ms_f(day, hms, frac), "lastPrice": px, "open": base, "high": base + 3,
                         "low": base - 1, "close": px, "volume": 1000.0, "amount": px * 1000})
    df = pd.DataFrame(rows)
    # 模拟 DuckDB tick 读出的 datetime 列(#13/#23)
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    return df


_TICK_STRATEGY = (
    "SEEN = []\n"
    "def init(stocks=None, data=None): pass\n"
    "def khHandlebar(data):\n"
    "    t = data['__current_time__']\n"
    "    row = data.get('000001.SZ')\n"
    "    px = None\n"
    "    if row is not None and len(row):\n"
    "        px = float(row.get('lastPrice'))\n"
    "    SEEN.append((t['date'], t['time'], px))\n"
    "    if t['date'] == '2024-01-02' and t['time'] == '10:00:00':\n"
    "        return [{'code': '000001.SZ', 'action': 'buy', 'price': px, 'volume': 100, 'reason': 'b'}]\n"
    "    return []\n"
)


class _FakeDynamicLoader:
    """分段模式替身: 一个分段装下全部数据, get_all_times 返回全部 tick 时间(模拟原逐 tick 时间轴)。"""

    frames = None

    def __init__(self, **kwargs):
        self.time_segments = [("20240102", "20240104")]

    def get_all_times(self):
        df = self.frames[(CODE, "tick")]
        return sorted((df["time"].dt.tz_localize("Asia/Shanghai").astype("int64") // 1_000_000).tolist())

    def get_chunk_for_time(self, current_time):
        return 0

    def ensure_data_loaded(self, current_time):
        df = self.frames[(CODE, "tick")].copy()
        df["time"] = df["time"].dt.tz_localize("Asia/Shanghai").astype("int64") // 1_000_000
        return {CODE: df}

    def get_memory_stats(self):
        return {"total_chunks": 1, "total_mb": 0.0}

    def cleanup(self):
        pass


@pytest.mark.parametrize("dynamic", [False, True])
def test_custom_tick_trigger_same_moments_full_and_segmented(monkeypatch, tmp_path, capsys, dynamic):
    strat = _write_strategy(tmp_path, _TICK_STRATEGY)
    extra = {"dynamic_load": {"enabled": True, "chunk_size": 30}} if dynamic else {}
    cfg = _write_config(tmp_path, strat, kline_period="tick",
                        trigger={"type": "custom", "custom_times": ["10:00:00", "14:00:30"]},
                        end="20240103", extra=extra)
    frames = {(CODE, "tick"): _tick_frame(), (BENCH, "1d"): _bench_frame()}
    daily_close = {(CODE, "20240102"): 10.8, (CODE, "20240103"): 11.8,
                   (BENCH, "20240102"): 3000.0, (BENCH, "20240103"): 3010.0}
    mgr = FakeDataManager(frames, daily_close)
    if dynamic:
        _FakeDynamicLoader.frames = frames
        monkeypatch.setattr(khFrame, "DynamicDataLoader", _FakeDynamicLoader)
    fw, _ = _build_framework(monkeypatch, tmp_path, cfg, strat, mgr)
    records = _run(fw)
    capsys.readouterr()

    seen = _strategy_module(fw).SEEN
    # 每个交易日每个设定点只触发一次, 时刻=设定点, 数据=[T-5,T] 内最新一笔(无未来数据)
    assert seen == [
        (DAYS[0], "10:00:00", pytest.approx(10.0)), (DAYS[0], "14:00:30", pytest.approx(10.1)),
        (DAYS[1], "10:00:00", pytest.approx(11.0)), (DAYS[1], "14:00:30", pytest.approx(11.1)),
    ]
    # [15] 日末估值使用当日日线收盘价(10.8 / 11.8), 每天一行
    stats = records["daily_stats"]
    assert [r["date"] for r in stats] == DAYS[:2]
    assert stats[0]["market_value"] == pytest.approx(100 * 10.8)
    assert stats[1]["market_value"] == pytest.approx(100 * 11.8)
    assert stats[1]["benchmark_close"] == pytest.approx(3010.0)


def test_custom_trigger_last_setpoint_without_data_still_records_day(monkeypatch, tmp_path, capsys):
    strat = _write_strategy(tmp_path, _TICK_STRATEGY)
    # 第二个设定点 14:30:30 当天没有任何 tick → 旧逻辑整天没有日统计
    cfg = _write_config(tmp_path, strat, kline_period="tick",
                        trigger={"type": "custom", "custom_times": ["10:00:00", "14:30:30"]},
                        end="20240103")
    frames = {(CODE, "tick"): _tick_frame(), (BENCH, "1d"): _bench_frame()}
    daily_close = {(CODE, "20240102"): 10.8, (CODE, "20240103"): 11.8}
    fw, _ = _build_framework(monkeypatch, tmp_path, cfg, strat, FakeDataManager(frames, daily_close))
    records = _run(fw)
    capsys.readouterr()
    stats = records["daily_stats"]
    assert [r["date"] for r in stats] == DAYS[:2]
    assert stats[0]["market_value"] == pytest.approx(100 * 10.8)
