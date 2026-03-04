# coding: utf-8
import json
from typing import Dict, List, Optional, Any
import time

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
        
        # 从根级别或system配置中读取run_mode
        self.run_mode = self.config_dict.get("run_mode") or \
                       self.config_dict.get("system", {}).get("run_mode", "backtest")
        self.userdata_path = self.config_dict.get("system", {}).get("userdata_path", "")
        self.session_id = self.config_dict.get("system", {}).get("session_id", int(time.time()))
        self.check_interval = self.config_dict.get("system", {}).get("check_interval", 3)
        
        # 账户配置，设置默认值
        account_config = self.config_dict.get("account", {})
        self.account_id = account_config.get("account_id", "test_account")
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
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config_dict, f, indent=4, ensure_ascii=False)
        except Exception as e:
            raise Exception(f"保存配置文件失败: {str(e)}")
            
    def update_config(self, key: str, value: Any):
        """更新配置
        
        Args:
            key: 配置键
            value: 配置值
        """
        self.config_dict[key] = value
        self.save_config() 