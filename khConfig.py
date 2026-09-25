# coding: utf-8
import copy
import json
from typing import Dict, List, Optional, Any
import time
from performance_config import (
    normalize_memory_profile,
    normalize_performance_config,
    resolve_memory_profile,
    recommend_chunk_size,
    _estimate_trading_days,
)
from backtest_runtime_config import strip_runtime_config


def _is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")

class KhConfig:
    """配置管理类"""
    
    def __init__(self, config_path: str):
        """初始化配置
        
        Args:
            config_path: 配置文件路径
        """
        self.config_path = config_path  # 保存配置文件路径
        # 加载配置文件
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config_dict = json.load(f)

        raw_performance_config = dict(self.config_dict.get("performance") or {})
        raw_dynamic_load_config = dict(self.config_dict.get("dynamic_load") or {})
        self._explicit_performance_keys = set(raw_performance_config.keys())
        self._explicit_dynamic_load_keys = set(raw_dynamic_load_config.keys())
        self.config_dict["performance"] = normalize_performance_config(raw_performance_config)
        self._apply_memory_profile_defaults(raw_performance_config, raw_dynamic_load_config)
        
        # 开源版只有回测：旧配置里的 run_mode（live / simulate）一律忽略
        self.run_mode = "backtest"
        self.userdata_path = self.config_dict.get("system", {}).get("userdata_path", "")
        self.session_id = self.config_dict.get("system", {}).get("session_id", int(time.time()))
        self.check_interval = self.config_dict.get("system", {}).get("check_interval", 3)
        
        # 账户配置，设置默认值
        account_config = self.config_dict.get("account", {})
        self.account_id = account_config.get("account_id", "")
        self.account_type = account_config.get("account_type", "SECURITY_ACCOUNT")
        
        # 回测配置，设置默认值
        backtest_config = self.config_dict.get("backtest", {})
        self.backtest_start = backtest_config.get("start_time", "20240101")
        self.backtest_end = backtest_config.get("end_time", "20241231")
        
        # 从回测配置中获取初始资金
        self.init_capital = backtest_config.get("init_capital", 1000000)
        
        # 数据配置，设置默认值
        data_config = self.config_dict.get("data", {})
        self.kline_period = data_config.get("kline_period", "1d")
        # 优先从stock_list读取，如果没有则使用stock_pool（兼容性）
        self.stock_pool = data_config.get("stock_list", data_config.get("stock_pool", []))
        
        # 风控配置，设置默认值
        risk_config = self.config_dict.get("risk", {})
        self.position_limit = risk_config.get("position_limit", 0.95)
        self.order_limit = risk_config.get("order_limit", 100)
        self.loss_limit = risk_config.get("loss_limit", 0.1)

        # 动态数据加载配置
        dynamic_load_config = self.config_dict.get("dynamic_load", {})
        self.dynamic_load_enabled = dynamic_load_config.get("enabled", False)  # 默认False，禁用动态加载
        self.dynamic_load_chunk_size = dynamic_load_config.get("chunk_size", None)
        self.dynamic_load_unit = dynamic_load_config.get("unit", "day")
        
    @property
    def initial_cash(self):
        """获取初始资金，确保与回测配置中的init_capital保持一致"""
        return self.init_capital

    def get_stock_list(self):
        """获取股票列表"""
        data_config = self.config_dict.get("data", {})
        # 优先从stock_list读取，如果没有则使用stock_pool（兼容性）
        return data_config.get("stock_list", data_config.get("stock_pool", []))
    
    def update_stock_list(self, stock_list: List[str]):
        """更新股票列表
        
        Args:
            stock_list: 股票代码列表
        """
        if "data" not in self.config_dict:
            self.config_dict["data"] = {}
        
        # 将股票列表存储到data.stock_list字段
        self.config_dict["data"]["stock_list"] = stock_list
        # 同时更新内存中的stock_pool以保持兼容性
        self.stock_pool = stock_list
        
        # 移除旧的stock_list_file字段（如果存在）
        if "stock_list_file" in self.config_dict["data"]:
            del self.config_dict["data"]["stock_list_file"]

    def _load_config(self) -> Dict:
        """加载配置文件"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            raise Exception(f"加载配置文件失败: {str(e)}")
            
    def save_config(self):
        """保存配置到文件"""
        try:
            payload = strip_runtime_config(
                self.config_dict,
                explicit_performance_keys=getattr(self, "_explicit_performance_keys", None),
                explicit_dynamic_load_keys=getattr(self, "_explicit_dynamic_load_keys", None),
            )
            current_perf = self.config_dict.get("performance", {}) or {}
            explicit_perf_keys = getattr(self, "_explicit_performance_keys", set()) or set()
            if explicit_perf_keys and isinstance(current_perf, dict):
                perf_payload = {
                    key: copy.deepcopy(value)
                    for key, value in current_perf.items()
                    if key in explicit_perf_keys and key not in {"memory_profile_effective", "memory_profile_decision", "memory_profile_retry_from", "memory_profile_retry_reason", "memory_profile_retry_attempt"}
                }
                if perf_payload:
                    payload["performance"] = perf_payload
                else:
                    payload.pop("performance", None)
            else:
                payload.pop("performance", None)

            current_dynamic = self.config_dict.get("dynamic_load", {}) or {}
            explicit_dynamic_keys = getattr(self, "_explicit_dynamic_load_keys", set()) or set()
            if explicit_dynamic_keys and isinstance(current_dynamic, dict):
                dynamic_payload = {
                    key: copy.deepcopy(value)
                    for key, value in current_dynamic.items()
                    if key in explicit_dynamic_keys
                }
                if dynamic_payload:
                    payload["dynamic_load"] = dynamic_payload
                else:
                    payload.pop("dynamic_load", None)
            else:
                payload.pop("dynamic_load", None)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=4, ensure_ascii=False)
        except Exception as e:
            raise Exception(f"保存配置文件失败: {str(e)}")
            
    def update_config(self, key: str, value: Any):
        """更新配置

        Args:
            key: 配置键
            value: 配置值
        """
        self.config_dict[key] = value
        if key == "performance" and isinstance(value, dict):
            self._explicit_performance_keys = set(value.keys())
        elif key == "dynamic_load" and isinstance(value, dict):
            self._explicit_dynamic_load_keys = set(value.keys())
        self.save_config()

    def _estimate_config_stock_count(self) -> int:
        """Return a best-effort stock count for memory profile auto detection."""
        data_config = self.config_dict.get("data", {}) or {}
        stocks = data_config.get("stock_list", data_config.get("stock_pool", []))
        if isinstance(stocks, (list, tuple, set)):
            return len(stocks)
        if isinstance(stocks, str) and stocks.strip():
            return len([s for s in stocks.replace("\n", ",").split(",") if s.strip()])

        stock_list_file = data_config.get("stock_list_file", "")
        if not stock_list_file:
            return 0
        try:
            import os
            path = str(stock_list_file)
            if not os.path.isabs(path):
                path = os.path.join(os.path.dirname(os.path.abspath(self.config_path)), path)
            if not os.path.exists(path):
                return 0
            count = 0
            with open(path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    text = line.strip()
                    if not text:
                        continue
                    if count == 0 and any(k in text.lower() for k in ("code", "stock", "代码", "证券")):
                        continue
                    count += 1
            return count
        except Exception:
            return 0

    def _apply_memory_profile_defaults(self, raw_performance_config=None, raw_dynamic_load_config=None):
        """Apply optional memory presets after resolving the auto profile."""
        raw_performance_config = raw_performance_config or {}
        raw_dynamic_load_config = raw_dynamic_load_config or {}
        performance = self.config_dict.setdefault("performance", {})
        data_config = self.config_dict.get("data", {}) or {}
        backtest_config = self.config_dict.get("backtest", {}) or {}
        fields = data_config.get("fields", [])
        field_count = len(fields) if isinstance(fields, (list, tuple, set)) else 0
        effective_profile, decision = resolve_memory_profile(
            performance,
            stock_count=self._estimate_config_stock_count(),
            period=data_config.get("kline_period", "1d"),
            start_time=backtest_config.get("start_time", "20240101"),
            end_time=backtest_config.get("end_time", "20241231"),
            field_count=field_count,
        )
        requested_profile = normalize_memory_profile(performance.get("memory_profile", "auto"))
        performance["memory_profile"] = requested_profile
        performance["memory_profile_effective"] = effective_profile
        performance["memory_profile_decision"] = decision

        if effective_profile not in ("low", "ultra_low"):
            return

        dynamic_load = self.config_dict.setdefault("dynamic_load", {})
        if _is_truthy(performance.get("memory_auto_dynamic_load", True)):
            raw_dynamic_enabled = (
                _is_truthy(raw_dynamic_load_config.get("enabled"))
                if "enabled" in raw_dynamic_load_config
                else None
            )
            dynamic_load["enabled"] = True
            if "unit" not in raw_dynamic_load_config or raw_dynamic_enabled is False:
                dynamic_load["unit"] = "day"
            chunk_key = "memory_ultra_low_chunk_size" if effective_profile == "ultra_low" else "memory_low_chunk_size"
            default_chunk = 20  # chunk下限钉20: 实测内存对chunk是U形、谷底~20, 更小既不省内存又更慢(见performance_config注释)
            if "chunk_size" not in raw_dynamic_load_config or raw_dynamic_enabled is False:
                try:  # 钉下限20(内存U形谷底); 兼容旧存档/旧默认里残留的<20值
                    base_chunk = max(20, int(performance.get(chunk_key, default_chunk)))
                except (TypeError, ValueError):
                    base_chunk = default_chunk
                # 增强: 按可用内存把chunk放大到能装下的最大(少分段提速), 内存紧时退回下限20; 仅影响时间/内存
                _td = _estimate_trading_days(
                    backtest_config.get("start_time"), backtest_config.get("end_time")
                ) or 0
                dynamic_load["chunk_size"] = recommend_chunk_size(
                    effective_profile,
                    base_chunk=base_chunk,
                    available_mem_mb=(decision or {}).get("available_mem_mb", 0),
                    estimated_working_set_mb=(decision or {}).get("estimated_working_set_mb", 0),
                    trading_days=_td,
                )

        if "time_index_mode" not in raw_performance_config:
            performance["time_index_mode"] = "searchsorted"
        if "current_data_row_mode" not in raw_performance_config:
            performance["current_data_row_mode"] = "lazy"
        if "current_data_container_mode" not in raw_performance_config:
            performance["current_data_container_mode"] = "lazy"
        if "duckdb_parallel_read_workers" not in raw_performance_config:
            # workers 控制批量"读"的并行线程数(khFrame→set_batch_read_workers→manager.py ThreadPoolExecutor)。
            # ultra_low 原为 1=串行读; 全A 4752股×长周期(多chunk, 每chunk读全4752只)下串行读比并行慢数倍:
            # 一年实测 w1≈3h(曾崩于90%) vs w4≈44min。改为 4 并行读, 内存代价小(多几个读缓冲), 大回测能跑通。
            # (注: 只读连接 close 实测仅~4ms、非瓶颈; 真瓶颈是串行读本身。)
            performance["duckdb_parallel_read_workers"] = 4
        if "duckdb_load_batch_size" not in raw_performance_config:
            performance["duckdb_load_batch_size"] = 100 if effective_profile == "ultra_low" else 200

    def get_dynamic_load_settings(self) -> Dict:
        """获取动态数据加载设置

        注意：动态加载功能默认禁用。
        只有在配置文件中明确设置 "dynamic_load": {"enabled": true} 时才会启用。

        Returns:
            dict: 动态加载配置 {enabled, chunk_size, unit}
        """
        # 获取配置文件中的设置
        dynamic_config = self.config_dict.get("dynamic_load", {})

        # 默认禁用，只有配置文件中明确设置enabled=true时才启用
        enabled = dynamic_config.get('enabled', False)
        chunk_size = dynamic_config.get('chunk_size', 1)  # 默认1天
        unit = dynamic_config.get('unit', 'day')

        return {
            'enabled': enabled,
            'chunk_size': chunk_size,
            'unit': unit
        }
