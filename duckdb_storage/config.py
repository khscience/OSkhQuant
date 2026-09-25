# -*- coding: utf-8 -*-
"""
DuckDB存储配置模块
"""

import os
import json
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime


def resolve_market_dir(data_root: str, market: str, *, create: bool = False) -> str:
    """返回市场目录；Linux 兼容历史数据库的大小写目录。"""
    normalized_market = str(market or "").strip().upper()
    exact = os.path.join(data_root, normalized_market)
    if os.path.isdir(exact) or not sys.platform.startswith("linux"):
        if create:
            os.makedirs(exact, exist_ok=True)
        return exact
    try:
        for name in os.listdir(data_root):
            candidate = os.path.join(data_root, name)
            if os.path.isdir(candidate) and name.upper() == normalized_market:
                return candidate
    except OSError:
        pass
    if create:
        os.makedirs(exact, exist_ok=True)
    return exact


def resolve_stock_db_path(
    data_root: str,
    stock_code: str,
    *,
    create_market: bool = False,
) -> str:
    raw = str(stock_code or "").strip().upper()
    if "." in raw:
        code, market = raw.split(".", 1)
    else:
        code = raw
        if code.startswith(("6", "5")):
            market = "SH"
        elif code.startswith(("4", "8")):
            market = "BJ"
        else:
            market = "SZ"
    return os.path.join(
        resolve_market_dir(data_root, market, create=create_market),
        f"{code}.db",
    )


@dataclass
class DuckDBConfig:
    """DuckDB存储配置类"""
    
    # 数据根目录，可自定义
    data_root: str = "./stock_data"
    
    # 默认复权类型
    default_dividend_type: str = "none"
    
    # 是否自动创建表
    auto_create_tables: bool = True
    
    # 批量写入大小
    batch_size: int = 10000
    
    # 版本号
    version: str = "1.0"
    
    # 创建时间
    created_time: str = field(default_factory=lambda: datetime.now().isoformat())
    
    # 最后修改时间
    last_modified: str = field(default_factory=lambda: datetime.now().isoformat())
    
    @classmethod
    def load(cls, config_path: str) -> 'DuckDBConfig':
        """从配置文件加载"""
        if config_path and os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 只取有效字段
                    valid_fields = {k: v for k, v in data.items() 
                                   if k in cls.__dataclass_fields__}
                    return cls(**valid_fields)
            except Exception as e:
                print(f"加载配置失败: {e}")
        return cls()
    
    @classmethod
    def load_from_root(cls, data_root: str) -> 'DuckDBConfig':
        """从数据根目录加载配置"""
        config_path = os.path.join(data_root, 'config.json')
        config = cls.load(config_path)
        config.data_root = data_root
        return config
    
    def save(self, config_path: str = None):
        """保存配置到文件"""
        if config_path is None:
            config_path = os.path.join(self.data_root, 'config.json')
        
        self.last_modified = datetime.now().isoformat()
        
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(asdict(self), f, indent=4, ensure_ascii=False)
    
    def get_market_dir(self, market: str) -> str:
        """获取市场目录路径"""
        return resolve_market_dir(self.data_root, market)
    
    def get_db_path(self, stock_code: str) -> str:
        """
        根据股票代码获取数据库文件路径
        
        Args:
            stock_code: 股票代码，如 '000001.SZ'
            
        Returns:
            数据库文件路径
        """
        return resolve_stock_db_path(self.data_root, stock_code)


# 默认配置实例
DEFAULT_CONFIG = DuckDBConfig()
