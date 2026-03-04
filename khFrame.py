# coding: utf-8
import time
import datetime
import traceback
import importlib.util
from typing import Dict, List, Optional, Union, Any
import logging
import sys
import shutil
from types import SimpleNamespace
import threading

from xtquant import xtdata
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from xtquant import xtconstant

from khTrade import KhTradeManager
from khRisk import KhRiskManager
from khQTTools import KhQuTools, determine_pool_type, format_price, round_price, get_price_decimals, check_t0_support, get_t0_details
from khConfig import KhConfig

import numpy as np
from PyQt5.QtCore import Qt, QMetaObject, Q_ARG, QObject
from PyQt5.QtWidgets import QMessageBox
import pandas as pd
import os
import holidays

# 简单的GUI类，用于处理日志记录
class DummySignal:
    """虚拟信号类，用于非GUI模式下替代PyQt5信号"""
    def emit(self, *args, **kwargs):
        """空实现，忽略信号发射"""
        pass

class SimpleGUI:
    """简单的GUI类，用于处理日志记录，兼容纯代码运行模式"""
    def __init__(self):
        # 创建虚拟信号对象，避免属性访问错误
        self.progress_signal = DummySignal()

    def log_message(self, message, level="INFO"):
        """记录日志消息"""
        print(f"[{level}] {datetime.datetime.now()} - {message}")

    def on_strategy_finished(self):
        """策略完成回调"""
        print(f"[INFO] {datetime.datetime.now()} - 策略执行完成")

# 触发器基类
class TriggerBase:
    """触发器基类，定义触发机制的通用接口"""
    
    def __init__(self, framework):
        """初始化触发器
        
        Args:
            framework: KhQuantFramework实例
        """
        self.framework = framework
        
    def initialize(self):
        """初始化触发器"""
        pass
        
    def should_trigger(self, timestamp, data):
        """判断是否应该触发策略
        
        Args:
            timestamp: 当前时间戳
            data: 当前市场数据
            
        Returns:
            bool: 是否触发策略
        """
        return False
        
    def get_data_period(self):
        """获取数据周期，用于数据加载
        
        Returns:
            str: 数据周期，如"tick", "1m", "5m"等
        """
        return "tick"

# Tick触发器
class TickTrigger(TriggerBase):
    """Tick触发器，每个Tick都触发策略"""
    
    def should_trigger(self, timestamp, data):
        """判断是否应该触发策略
        
        Args:
            timestamp: 当前时间戳
            data: 当前市场数据
            
        Returns:
            bool: 是否触发策略
        """
        # Tick触发方式下，每个Tick都触发
        return True
        
    def get_data_period(self):
        """获取数据周期
        
        Returns:
            str: 数据周期
        """
        return "tick"

# K线触发器
class KLineTrigger(TriggerBase):
    """K线触发器，在K线形成时触发策略"""
    
    def __init__(self, framework, period):
        """初始化K线触发器
        
        Args:
            framework: KhQuantFramework实例
            period: K线周期，如"1m", "5m", "1d"等
        """
        super().__init__(framework)
        self.period = period  # "1m", "5m" 或 "1d"
        self.last_trigger_time = {}  # 记录每个股票上次触发时间
        self.last_trigger_date = None  # 记录上次触发的日期（用于日K线）
        
    def should_trigger(self, timestamp, data):
        """判断是否应该触发策略
        
        Args:
            timestamp: 当前时间戳
            data: 当前市场数据
            
        Returns:
            bool: 是否触发策略
        """
        # 获取当前时间
        if isinstance(timestamp, str):
            try:
                current_time = datetime.datetime.strptime(timestamp, "%Y%m%d%H%M%S")
            except:
                current_time = datetime.datetime.now()
        else:
            try:
                timestamp = float(timestamp)
                if timestamp > 1e10:  # 如果是毫秒级时间戳
                    timestamp = timestamp / 1000
                current_time = datetime.datetime.fromtimestamp(timestamp)
            except:
                current_time = datetime.datetime.now()
        
        # 对于1分钟K线，在每分钟的开始触发
        if self.period == "1m":
            return current_time.second == 0
            
        # 对于5分钟K线，在每5分钟的开始触发
        elif self.period == "5m":
            return current_time.minute % 5 == 0 and current_time.second == 0
            
        # 对于日K线，每个交易日触发一次
        elif self.period == "1d":
            current_date = current_time.date()
            # 检查是否是新的一天（日K线只需要基于日期判断，无需考虑具体时间）
            if self.last_trigger_date != current_date:
                self.last_trigger_date = current_date
                return True
            return False
            
        return False
        
    def get_data_period(self):
        """获取数据周期
        
        Returns:
            str: 数据周期
        """
        return self.period


# 自定义定时触发器
class CustomTimeTrigger(TriggerBase):
    """自定义定时触发器，在指定的时间点触发策略"""
    
    def __init__(self, framework, custom_times):
        """初始化自定义定时触发器
        
        Args:
            framework: KhQuantFramework实例
            custom_times: 自定义触发时间点列表，格式为["09:30:00", "09:45:00", ...]
        """
        super().__init__(framework)
        # 解析时间字符串为秒数（从午夜开始）
        self.trigger_seconds = []
        for time_str in custom_times:
            h, m, s = map(int, time_str.split(':'))
            seconds = h * 3600 + m * 60 + s
            self.trigger_seconds.append(seconds)
        self.trigger_seconds.sort()
        
    def should_trigger(self, timestamp, data):
        """判断是否应该触发策略
        
        Args:
            timestamp: 当前时间戳
            data: 当前市场数据
            
        Returns:
            bool: 是否触发策略
        """
        # 获取当前时间
        if isinstance(timestamp, str):
            try:
                current_time = datetime.datetime.strptime(timestamp, "%Y%m%d%H%M%S")
            except:
                current_time = datetime.datetime.now()
        else:
            try:
                timestamp = float(timestamp)
                if timestamp > 1e10:  # 如果是毫秒级时间戳
                    timestamp = timestamp / 1000
                current_time = datetime.datetime.fromtimestamp(timestamp)
            except:
                current_time = datetime.datetime.now()
        
        # 计算当前时间的秒数（从午夜开始）
        current_seconds = current_time.hour * 3600 + current_time.minute * 60 + current_time.second
        
        # 检查是否接近任一触发时间点（允许5秒误差）
        for trigger_second in self.trigger_seconds:
            if abs(current_seconds - trigger_second) < 5:
                return True
                
        return False
        
    def get_data_period(self):
        """获取数据周期
        
        Returns:
            str: 数据周期
        """
        # 自定义触发使用1秒级数据
        return "1s"

# 触发器工厂
class TriggerFactory:
    """触发器工厂，用于创建不同类型的触发器"""
    
    @staticmethod
    def create_trigger(framework, config):
        """创建触发器
        
        Args:
            framework: KhQuantFramework实例
            config: 配置字典
            
        Returns:
            TriggerBase: 触发器实例
        """
        trigger_type = config.get("backtest", {}).get("trigger", {}).get("type", "tick")
        
        if trigger_type == "tick":
            return TickTrigger(framework)
        elif trigger_type == "1m":
            return KLineTrigger(framework, "1m")
        elif trigger_type == "5m":
            return KLineTrigger(framework, "5m")
        elif trigger_type == "1d":
            return KLineTrigger(framework, "1d")
        elif trigger_type == "custom":
            custom_times = config.get("backtest", {}).get("trigger", {}).get("custom_times", [])
            return CustomTimeTrigger(framework, custom_times)
        else:
            # 默认使用Tick触发
            return TickTrigger(framework)

class MyTraderCallback(XtQuantTraderCallback):
    def __init__(self, gui):
        super().__init__()
        self.gui = gui
        self.price_decimals = 2  # 默认价格精度，会在回测开始时根据股票池类型更新
        self.gui.log_message("交易回调已初始化", "INFO")
    
    def set_price_decimals(self, decimals: int):
        """设置价格精度"""
        self.price_decimals = decimals
    
    def on_stock_order(self, order):
        """委托回报推送"""
        try:
            direction_map = {
                xtconstant.STOCK_BUY: '买入',
                xtconstant.STOCK_SELL: '卖出'
            }
            
            status_map = {
                0: '已提交',
                1: '已接受',
                2: '已拒绝',
                3: '已撤销',
                4: '已成交',
                5: '部分成交'
            }
            
            # 格式化时间戳 (从order对象获取, 通常是Unix时间戳)
            formatted_time = "未知"
            order_time_val = getattr(order, 'order_time', None)
            if order_time_val:
                try:
                    timestamp = float(order_time_val)
                    # 检查并转换毫秒级时间戳
                    if timestamp > 1e10:
                        timestamp = timestamp / 1000
                    formatted_time = datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
                except ValueError:
                    # 如果不是数字时间戳，尝试解析字符串
                    try:
                        formatted_time = datetime.datetime.strptime(str(order_time_val), '%Y%m%d%H%M%S').strftime('%Y-%m-%d %H:%M:%S')
                    except:
                        formatted_time = str(order_time_val) # 解析失败则直接显示原始值
                except Exception as e:
                    formatted_time = f"时间转换错误: {e}"
            
            decimals = self.price_decimals
            order_msg = (
                f"委托信息 - "
                f"时间: {formatted_time} | "
                f"股票代码: {order.stock_code} | "
                f"方向: {direction_map.get(order.order_type, '未知')} | "
                f"委托价格: {order.price:.{decimals}f} | "
                f"数量: {order.order_volume} | "
                f"委托编号: {order.order_id} | "
                f"原因: {order.status_msg or '策略交易'}"
            )
            
            self.gui.log_message(order_msg, "TRADE")
            print(datetime.datetime.now(), '委托回调', order.order_remark)
            
        except Exception as e:
            self.gui.log_message(f"处理委托回报时出错: {str(e)}", "ERROR")

    def on_stock_trade(self, trade):
        """成交回报推送"""
        try:
            direction_map = {
                xtconstant.STOCK_BUY: '买入',
                xtconstant.STOCK_SELL: '卖出'
            }
            
            # 获取实际成交价格
            actual_price = getattr(trade, 'actual_price', trade.traded_price)
            
            # 格式化时间戳 (从trade对象获取, 通常是Unix时间戳)
            formatted_time = "未知"
            traded_time_val = getattr(trade, 'traded_time', None)
            if traded_time_val:
                try:
                    timestamp = float(traded_time_val)
                    # 检查并转换毫秒级时间戳
                    if timestamp > 1e10:
                        timestamp = timestamp / 1000
                    formatted_time = datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
                except ValueError:
                    # 如果不是数字时间戳，尝试解析字符串
                    try:
                        formatted_time = datetime.datetime.strptime(str(traded_time_val), '%Y%m%d%H%M%S').strftime('%Y-%m-%d %H:%M:%S')
                    except:
                        formatted_time = str(traded_time_val) # 解析失败则直接显示原始值
                except Exception as e:
                    formatted_time = f"时间转换错误: {e}"
            
            decimals = self.price_decimals
            trade_msg = (
                f"成交信息 - "
                f"时间: {formatted_time} | "
                f"股票代码: {trade.stock_code} | "
                f"方向: {direction_map.get(trade.order_type, '未知')} | "
                f"实际成交价: {actual_price:.{decimals}f} | "
                f"成交数量: {trade.traded_volume} | "
                f"成交金额: {trade.traded_amount:.{decimals}f} | "
                f"成交编号: {trade.traded_id} | "
                f"原因: {trade.order_remark or '策略交易'}"
            )
            
            self.gui.log_message(trade_msg, "TRADE")
            print(datetime.datetime.now(), '成交回调', trade.order_remark)
            
        except Exception as e:
            self.gui.log_message(f"处理成交回报时出错: {str(e)}", "ERROR")
    
    def on_order_error(self, order_error):
        """委托错误回报推送"""
        try:
            error_msg = (
                f"委托错误 - "
                f"股票代码: {order_error.stock_code} | "
                f"错误代码: {order_error.error_id} | "
                f"错误信息: {order_error.error_msg} | "
                f"备注: {order_error.order_remark}"
            )
            
            self.gui.log_message(error_msg, "ERROR")
            print(f"委托报错回调 {order_error.order_remark} {order_error.error_msg}")
            
        except Exception as e:
            self.gui.log_message(f"处理委托错误时出错: {str(e)}", "ERROR")
    
    def on_cancel_error(self, cancel_error):
        """撤单错误回报推送"""
        try:
            error_msg = (
                f"撤单错误 - "
                f"委托编号: {cancel_error.order_id} | "
                f"错误代码: {cancel_error.error_id} | "
                f"错误信息: {cancel_error.error_msg}"
            )
            
            self.gui.log_message(error_msg, "ERROR")
            print(datetime.datetime.now(), sys._getframe().f_code.co_name)
            
        except Exception as e:
            self.gui.log_message(f"处理撤单错误时出错: {str(e)}", "ERROR")
            
    def on_disconnected(self):
        """连接断开"""
        self.gui.log_message("交易连接已断开", "WARNING")
        print(datetime.datetime.now(),'连接断开回调')

    def on_order_stock_async_response(self, response):
        """异步下单回报推送"""
        try:
            msg = f"异步委托回调 - 备注: {response.order_remark}"
            self.gui.log_message(msg, "TRADE")
            print(f"异步委托回调 {response.order_remark}")
        except Exception as e:
            self.gui.log_message(f"处理异步下单回报时出错: {str(e)}", "ERROR")

    def on_cancel_order_stock_async_response(self, response):
        """撤单异步回报推送"""
        try:
            msg = f"撤单异步回报 - 委托编号: {response.order_id}"
            self.gui.log_message(msg, "TRADE")
            print(datetime.datetime.now(), sys._getframe().f_code.co_name)
        except Exception as e:
            self.gui.log_message(f"处理撤单异步回报时出错: {str(e)}", "ERROR")

    def on_account_status(self, status):
        """账户状态变动推送"""
        try:
            msg = f"账户状态变动 - 账户: {status.account_id} | 状态: {status.status}"
            self.gui.log_message(msg, "INFO")
            print(datetime.datetime.now(), sys._getframe().f_code.co_name)
        except Exception as e:
            self.gui.log_message(f"处理账户状态变动时出错: {str(e)}", "ERROR")

    def on_stock_position(self, position):
        """持仓变动推送"""
        try:
            # 只记录重要的持仓变动
            decimals = self.price_decimals
            msg = (
                f"持仓变动 - "
                f"股票代码: {position.stock_code} | "
                f"持仓数量: {position.volume} | "
                f"最新价格: {getattr(position, 'current_price', 0):.{decimals}f} | "
                f"持仓市值: {getattr(position, 'market_value', 0):.{decimals}f} | "
                f"持仓盈亏: {getattr(position, 'profit', 0):.{decimals}f}"
            )
            self.gui.log_message(msg, "INFO")
        except Exception as e:
            self.gui.log_message(f"处理持仓变动时出错: {str(e)}", "ERROR")

    def on_connected(self):
        """连接成功推送"""
        self.gui.log_message("交易连接成功", "INFO")

    def on_stock_asset(self, asset):
        """资金变动推送"""
        '''
        try:
            decimals = self.price_decimals
            msg = (
                f"资金变动 - "
                f"账户: {asset.account_id} | "
                f"可用资金: {asset.cash:.{decimals}f} | "
                f"总资产: {asset.total_asset:.{decimals}f}"
            )
            self.gui.log_message(msg, "INFO")
            print("资金变动推送on asset callback")
            print(asset.account_id, asset.cash, asset.total_asset)
        except Exception as e:
            self.gui.log_message(f"处理资金变动时出错: {str(e)}", "ERROR")
            '''

class KhQuantFramework:
    """量化交易框架主类"""
    
    def __init__(self, config_path: str, strategy_file: str, trader_callback=None):
        """初始化框架
        
        Args:
            config_path: 配置文件路径
            strategy_file: 策略文件路径
            trader_callback: 交易回调函数
        """
        print(f"[DEBUG] KhQuantFramework.__init__ 开始")
        print(f"[DEBUG] config_path: {config_path}")
        print(f"[DEBUG] strategy_file: {strategy_file}")
        
        self.config_path = config_path
        self.config = KhConfig(config_path)
        self.is_running = False  # 运行状态标识
        self.qmt_path = self.config.config_dict.get("qmt", {}).get("path", "") # QMT客户端路径
        self.account = None  # 账户对象
        self.trader = None  # 交易API实例
        self.strategy_module = None  # 策略模块
        self.trade_mgr = KhTradeManager(self.config)  # 交易管理器
        self.risk_mgr = KhRiskManager(self.config)  # 风险管理器
        self.tools = KhQuTools()  # 工具类
        self.backtest_records = {}  # 回测记录
        self.daily_price_cache = {}  # 日线价格缓存，用于存储所有股票的日线数据
        self._cached_benchmark_close = {}  # 基准指数收盘价缓存
        
        # T+0交易模式标识（默认关闭，在run()中根据股票池判断）
        self.t0_mode = False
        
        # 添加运行时间记录变量
        self.start_time = None  # 策略开始运行时间
        self.end_time = None    # 策略结束运行时间
        self.total_runtime = 0  # 总运行时间（秒）
        
        # 添加简单的GUI属性用于日志记录
        self.gui = SimpleGUI()
        
        # 加载策略模块
        print(f"[DEBUG] 准备加载策略模块: {strategy_file}")
        try:
            self.strategy_module = self.load_strategy(strategy_file)
            print(f"[DEBUG] 策略模块加载成功")
        except Exception as e:
            print(f"[DEBUG] 策略模块加载失败: {str(e)}")
            import traceback
            traceback.print_exc()
            raise
        
        # 当前运行模式
        self.run_mode = self.config.run_mode
        
        self.trader_callback = trader_callback  # 保存交易回调函数
        
        # 创建触发器
        self.trigger = TriggerFactory.create_trigger(self, self.config.config_dict)
        
        # 初始化各个模块
        self.trade_mgr = KhTradeManager(self.config)
        self.risk_mgr = KhRiskManager(self.config) 
        self.tools = KhQuTools()
        
        # 初始化QMT客户端路径，优先使用system.userdata_path
        self.qmt_path = self.config.config_dict.get("system", {}).get("userdata_path", "")
        if not self.qmt_path:
            self.qmt_path = self.config.config_dict.get("qmt", {}).get("path", "")
        
        # 交易账户
        self.account = None
        # 交易API
        self.trader = None
        # 交易回调
        self.callback = None
        
        # 初始化交易管理器
        self.trade_mgr = KhTradeManager(self.config, self)
        
        # 清除可能存在的历史数据缓存，确保每次运行都是干净的状态
        if hasattr(self, 'historical_data_ref'):
            delattr(self, 'historical_data_ref')
        if hasattr(self, 'time_field_cache'):
            delattr(self, 'time_field_cache')
        if hasattr(self, 'time_idx_cache'):
            delattr(self, 'time_idx_cache')
        
        # 初始化风控管理器
        self.risk_mgr = KhRiskManager(self.config)
        
    def _log(self, message, level="INFO"):
        """根据是否存在回调函数选择日志记录方式"""
        if self.trader_callback and hasattr(self.trader_callback, 'gui'):
            self.trader_callback.gui.log_message(message, level)
        else:
            # 在调试模式下，trader_callback可能不存在，使用print输出
            print(f"[{level}] {datetime.datetime.now()} - {message}")

    def _should_log(self):
        """检查是否应该输出日志（用于性能优化）

        始终返回True，保持兼容性
        """
        return True

    def _cache_should_log(self):
        """在回测开始时缓存日志开关状态（保持兼容性）"""
        pass

    def load_strategy(self, strategy_file: str):
        """动态加载策略模块

        Args:
            strategy_file: 策略文件路径

        Returns:
            module: 策略模块
        """
        print(f"[DEBUG] load_strategy 被调用，参数: {strategy_file}")
        import importlib.util
        import sys
        import os

        # 获取策略文件的绝对路径
        strategy_file = os.path.abspath(strategy_file)
        print(f"[DEBUG] 策略文件绝对路径: {strategy_file}")

        # 使用策略文件的实际文件名作为模块名（不含.py扩展名）
        # 这样debugpy可以正确识别模块
        module_name = os.path.splitext(os.path.basename(strategy_file))[0]
        print(f"[DEBUG] 模块名: {module_name}")

        # 创建模块规范
        spec = importlib.util.spec_from_file_location(module_name, strategy_file)
        print(f"[DEBUG] spec 创建成功")

        # 创建模块对象
        strategy_module = importlib.util.module_from_spec(spec)
        print(f"[DEBUG] 模块对象创建成功")

        # 将模块添加到sys.modules，这样debugpy可以找到它
        # 这是让VSCode断点生效的关键！
        sys.modules[module_name] = strategy_module
        print(f"[DEBUG] 模块已添加到sys.modules: {module_name}")

        # 确保模块的__file__属性指向正确的源文件
        strategy_module.__file__ = strategy_file
        print(f"[DEBUG] 模块__file__属性: {strategy_module.__file__}")

        # 执行模块代码
        print(f"[DEBUG] 准备执行模块代码")
        spec.loader.exec_module(strategy_module)
        print(f"[DEBUG] 模块代码执行完成")

        return strategy_module
        
    def init_trader_and_account(self):
        """初始化交易接口和账户"""
        # 固定为回测模式，只进行虚拟账户初始化
        self._init_virtual_account()
        # 在回测模式下也设置回调
        if self.trader_callback:
            self.trade_mgr.callback = self.trader_callback
        
    def _init_virtual_account(self):
        """初始化虚拟账户"""
        # 创建虚拟账户对象
        self.account = StockAccount(
            self.config.account_id,
            self.config.account_type
        )
        
        # 获取基准合约并转换格式
        original_benchmark = self.config.config_dict["backtest"].get("benchmark", "sh.000300")
        if original_benchmark == "sh.000300":
            self.benchmark = "000300.SH"
        else:
            self.benchmark = original_benchmark
        
        # 更新配置字典中的基准指数代码
        self.config.config_dict["backtest"]["benchmark"] = self.benchmark
        
        # 从回测配置中获取初始资金
        init_capital = self.config.config_dict["backtest"]["init_capital"]
        
        # 初始化资产字典
        self.trade_mgr.assets = {
            "account_type": xtconstant.SECURITY_ACCOUNT,
            "account_id": self.config.account_id,
            "cash": init_capital,
            "frozen_cash": 0.0,
            "market_value": 0.0,
            "total_asset": init_capital,
            "benchmark": self.benchmark
        }
        
        # 初始化持仓字典
        self.trade_mgr.positions = {}  # 初始持仓为空
        
        # 初始化委托字典
        self.trade_mgr.orders = {}  # 初始委托为空
        
        # 初始化成交字典
        self.trade_mgr.trades = {}  # 初始成交为空
        
        print(f"虚拟账户初始化完成: {self.config.account_id}")
        print(f"初始资产: {self.trade_mgr.assets}")
        print(f"基准合约: {self.benchmark}")
        
    def create_callback(self) -> XtQuantTraderCallback:
        """创建交易回调对象"""
        return MyTraderCallback(self)
        
    def init_data(self):
        """初始化行情数据"""
        # 固定为回测模式，批量下载历史数据（增量下载）
        download_complete = False
        
        def download_progress(progress):
            nonlocal download_complete
            print(f"下载进度: {progress}")
            if progress['finished'] >= progress['total']:
                download_complete = True
        
        # 获取股票列表
        stock_codes = self.get_stock_list()
        
        if not stock_codes:
            if self.trader_callback:
                self.trader_callback.gui.log_message("警告: 股票池为空，无法下载历史数据", "WARNING")
            return
            
        if self.trader_callback:
            self.trader_callback.gui.log_message(f"开始下载{len(stock_codes)}只股票的历史数据...", "INFO")
        
        xtdata.download_history_data2(
            stock_codes,
            period=self.config.kline_period,
            start_time=self.config.backtest_start,
            end_time=self.config.backtest_end,  # 添加结束时间参数
            incrementally=True,
            callback=download_progress
        )
        
        # 等待下载完成
        while not download_complete:
            time.sleep(1)
            

            
    def on_quote_callback(self, data: Dict):
        """行情数据回调处理"""
        try:
            # 提取时间信息
            timestamp = data.get("timestamp", int(time.time()))
            # 如果时间戳是字符串，尝试转换为整数
            if isinstance(timestamp, str):
                try:
                    timestamp = int(timestamp)
                except:
                    timestamp = int(time.time())
            
            # 创建时间信息
            if timestamp > 1e10:  # 毫秒级时间戳
                dt = datetime.datetime.fromtimestamp(timestamp / 1000)
            else:  # 秒级时间戳
                dt = datetime.datetime.fromtimestamp(timestamp)
            
            # 构建时间信息字典
            time_info = {
                "timestamp": timestamp,
                "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "date": dt.strftime("%Y-%m-%d"),
                "time": dt.strftime("%H:%M:%S")
            }
            
            # 检查是否是交易日
            if not self.tools.is_trade_day(time_info["date"]):
                # 如果不是交易日，则跳过策略调用
                if self.trader_callback:
                    self.trader_callback.gui.log_message(f"日期 {time_info['date']} 不是交易日，跳过策略执行", "INFO")
                return
            
            # 创建新的数据字典，包含时间信息
            data_with_time = {"__current_time__": time_info}
            
            # 将其他行情数据添加到data_with_time
            for key, value in data.items():
                if key != "__current_time__":
                    data_with_time[key] = value
            
            # 使用触发器判断是否应该触发策略
            if not self.trigger.should_trigger(timestamp, data_with_time):
                # 对于K线周期触发，需要特殊处理
                trigger_type = self.config.config_dict.get("backtest", {}).get("trigger", {}).get("type", "tick")
                if trigger_type == "1m" or trigger_type == "5m":
                    # 当前时间
                    current_time_str = time_info["time"]
                    dt_time = datetime.datetime.strptime(current_time_str, "%H:%M:%S")
                    
                    # 对于1分钟K线，检查是否接近每分钟的结束(57秒以后)
                    if trigger_type == "1m" and dt_time.second >= 57:
                        # 允许触发
                        pass
                    # 对于5分钟K线，检查是否接近每5分钟的结束(当前分钟为4、9、14...且秒数>=57)
                    elif trigger_type == "5m" and dt_time.minute % 5 == 4 and dt_time.second >= 57:
                        # 允许触发
                        pass
                    else:
                        # 不是K线周期结束，不触发
                        return
                elif trigger_type == "1d":
                    # 日K线触发已经在DailyTrigger中处理了逻辑
                    # 如果触发器返回False，说明不应该触发
                    return
                else:
                    # 触发器返回False，不触发策略
                    return
                
            # 风控检查
            if not self.risk_mgr.check_risk(data_with_time):
                return
                
            # 添加当前时间信息到数据字典
            current_time = datetime.datetime.fromtimestamp(timestamp)
            time_data = {
                "__current_time__": {
                    "timestamp": timestamp,
                    "datetime": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "date": current_time.strftime("%Y-%m-%d"),
                    "time": current_time.strftime("%H:%M:%S")
                }
            }
            # 将时间信息合并到数据字典中
            data_with_time = {**data_with_time, **time_data}
            
            # 添加账户和持仓信息到数据字典
            if hasattr(self, 'trade_mgr') and self.trade_mgr:
                # 添加账户资产信息
                account_data = {
                    "__account__": self.trade_mgr.assets
                }
                # 添加持仓信息
                positions_data = {
                    "__positions__": self.trade_mgr.positions
                }
                # 添加股票池信息
                stock_list_data = {
                    "__stock_list__": self.get_stock_list()
                }
                # 合并所有信息
                data_with_time.update(account_data)
                data_with_time.update(positions_data)
                data_with_time.update(stock_list_data)
                
                # 添加框架实例到数据字典
                data_with_time["__framework__"] = self
            
            # 检查股票数据是否为空
            stock_data_empty = True
            empty_stocks = []
            for key, value in data_with_time.items():
                # 跳过框架内部字段
                if key.startswith("__"):
                    continue
                # 检查股票数据是否为空
                if isinstance(value, pd.Series) and not value.empty:
                    stock_data_empty = False
                elif isinstance(value, pd.Series) and value.empty:
                    empty_stocks.append(key)
                elif not value:  # 处理其他空值情况
                    empty_stocks.append(key)
            
            # 如果所有股票数据都为空，记录错误并跳过策略调用
            if stock_data_empty:
                current_time_str = data_with_time.get("__current_time__", {}).get("datetime", "未知时间")
                if self.trader_callback:
                    self.trader_callback.gui.log_message(
                        f"警告: 时间点 {current_time_str} 的所有股票数据为空，跳过策略调用", 
                        "WARNING"
                    )
                    if empty_stocks:
                        self.trader_callback.gui.log_message(
                            f"空数据股票列表: {', '.join(empty_stocks[:10])}" + 
                            (f" 等{len(empty_stocks)}只股票" if len(empty_stocks) > 10 else ""),
                            "WARNING"
                        )
                return
            
            # 如果有部分股票数据为空，记录警告但继续执行
            if empty_stocks:
                current_time_str = data_with_time.get("__current_time__", {}).get("datetime", "未知时间")
                if self.trader_callback:
                    self.trader_callback.gui.log_message(
                        f"警告: 时间点 {current_time_str} 有 {len(empty_stocks)} 只股票数据为空: {', '.join(empty_stocks[:5])}" + 
                        (f" 等" if len(empty_stocks) > 5 else ""),
                        "WARNING"
                    )
            
            # 调用策略处理
            signals = self.strategy_module.khHandlebar(data_with_time)
            
            # 处理信号中的价格精度
            if signals:
                for signal in signals:
                    if 'price' in signal:
                        # 使用动态精度
                        signal['price'] = round(float(signal['price']), self.price_decimals)
            
            # 发送交易指令
            if signals:
                self.trade_mgr.process_signals(signals)
                
        except Exception as e:
            self.log_error(f"行情处理异常: {str(e)}")
            traceback.print_exc()
            
    def run(self):
        """启动框架"""
        # 记录策略开始运行时间
        self.start_time = time.time()
        start_datetime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if self.trader_callback:
            self.trader_callback.gui.log_message(f"策略开始运行时间: {start_datetime}", "INFO")
            self.trader_callback.gui.log_message("开始初始化交易接口和数据...", "INFO")
        
        try:
            # 初始化
            init_start = time.time()
            self.init_trader_and_account() # 初始化交易接口和账户
            init_time = time.time() - init_start
            
            if self.trader_callback:
                self.trader_callback.gui.log_message(f"交易接口初始化耗时: {init_time:.2f}秒", "INFO")
            
            # 初始化缓存
            self.daily_price_cache = {}
            self._cached_benchmark_close = {}
            
            # 直接从设置界面读取是否初始化数据的配置
            from PyQt5.QtCore import QSettings
            settings = QSettings('KHQuant', 'StockAnalyzer')
            init_data_enabled = settings.value('init_data_enabled', True, type=bool)
            
            if self.trader_callback:
                self.trader_callback.gui.log_message(f"数据初始化设置: {'启用' if init_data_enabled else '禁用'}", "INFO")
            
            if init_data_enabled:
                data_init_start = time.time()
                if self.trader_callback:
                    self.trader_callback.gui.log_message("开始初始化行情数据...", "INFO")
                self.init_data() # 初始化行情数据
                data_init_time = time.time() - data_init_start
                
                if self.trader_callback:
                    self.trader_callback.gui.log_message(f"数据初始化耗时: {data_init_time:.2f}秒", "INFO")
            else:
                if self.trader_callback:
                    self.trader_callback.gui.log_message("跳过数据初始化（根据设置禁用）", "INFO")
            
            # 读取股票列表
            stock_list_start = time.time()
            stock_codes = self.get_stock_list()
            stock_list_time = time.time() - stock_list_start
            
            if self.trader_callback:
                self.trader_callback.gui.log_message(f"股票列表加载耗时: {stock_list_time:.2f}秒", "INFO")
            
            # 判断股票池类型并设置价格精度
            self.pool_type, self.price_decimals = determine_pool_type(stock_codes)
            # 将精度设置传递给交易管理器
            self.trade_mgr.set_price_decimals(self.price_decimals)
            # 将精度设置传递给回调对象（用于日志格式化）
            if self.trader_callback and hasattr(self.trader_callback, 'set_price_decimals'):
                self.trader_callback.set_price_decimals(self.price_decimals)
            
            # 记录股票池类型信息
            pool_type_names = {
                'stock_only': '纯股票',
                'etf_only': '纯ETF',
                'mixed': '股票+ETF混合'
            }
            if self.trader_callback:
                self.trader_callback.gui.log_message(
                    f"股票池类型: {pool_type_names.get(self.pool_type, self.pool_type)}, "
                    f"价格精度: {self.price_decimals}位小数", "INFO"
                )
            
            # ==================== T+0模式检验 ====================
            t0_support_type, self.t0_mode = check_t0_support(stock_codes)
            
            # 将T+0模式设置传递给交易管理器
            self.trade_mgr.set_t0_mode(self.t0_mode)
            
            if t0_support_type == 'all_t0':
                # 全部支持T+0，进入T+0模式
                if self.trader_callback:
                    self.trader_callback.gui.log_message(
                        "T+0交易模式已启用 - 股票池中全部为T0型ETF，支持当日买入当日卖出", "INFO"
                    )
                    # 通知GUI更新显示（使用try-except防止跨线程调用导致崩溃）
                    try:
                        if hasattr(self.trader_callback.gui, 'set_t0_mode_display'):
                            self.trader_callback.gui.set_t0_mode_display(True)
                    except Exception as e:
                        logging.warning(f"更新T+0模式显示时出错: {e}")
            elif t0_support_type == 'mixed':
                # 混合池，弹窗提醒
                t0_details = get_t0_details(stock_codes)
                warning_msg = (
                    f"股票池中包含混合品种：\n"
                    f"- 支持T+0的ETF: {t0_details['t0_count']}只\n"
                    f"- 不支持T+0的品种: {len(t0_details['non_t0_stocks'])}只\n\n"
                    f"系统将使用T+1模式运行。如需使用T+0模式，请确保股票池中只包含T0型ETF。"
                )
                if self.trader_callback:
                    self.trader_callback.gui.log_message(
                        f"T+0模式未启用：股票池包含{t0_details['t0_count']}只T0型ETF和{len(t0_details['non_t0_stocks'])}只非T0品种", 
                        "WARNING"
                    )
                    # 显示警告弹窗（使用try-except防止跨线程调用导致崩溃）
                    try:
                        if hasattr(self.trader_callback.gui, 'show_t0_warning'):
                            self.trader_callback.gui.show_t0_warning(warning_msg)
                    except Exception as e:
                        logging.warning(f"显示T+0警告弹窗时出错: {e}")
            else:
                # 全部不支持T+0，正常T+1模式
                if self.trader_callback:
                    self.trader_callback.gui.log_message("交易模式: T+1（标准A股交易规则）", "INFO")
            
            # 准备初始化数据结构，包含时间、账户、持仓、股票池等信息
            init_data = {
                "__current_time__": {
                    "timestamp": int(time.time()),
                    "datetime": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "date": datetime.datetime.now().strftime("%Y-%m-%d"),
                    "time": datetime.datetime.now().strftime("%H:%M:%S")
                },
                "__account__": self.trade_mgr.assets,
                "__positions__": self.trade_mgr.positions,
                "__stock_list__": stock_codes,
                "__framework__": self
            }
            
            # 调用策略初始化函数，并传递完整数据结构
            strategy_init_start = time.time()
            self.strategy_module.init(stock_codes, init_data)
            strategy_init_time = time.time() - strategy_init_start
            
            if self.trader_callback:
                self.trader_callback.gui.log_message(f"策略初始化耗时: {strategy_init_time:.2f}秒", "INFO")
            
            self.is_running = True
            
            # 记录预处理总耗时
            preprocess_time = time.time() - self.start_time
            if self.trader_callback:
                self.trader_callback.gui.log_message(f"预处理阶段总耗时: {preprocess_time:.2f}秒", "INFO")
                self.trader_callback.gui.log_message("开始执行策略主逻辑...", "INFO")
            
            # 固定运行回测模式
            strategy_start = time.time()
            self._run_backtest()
            strategy_time = time.time() - strategy_start
            
            if self.trader_callback:
                self.trader_callback.gui.log_message(f"策略主逻辑执行耗时: {strategy_time:.2f}秒", "INFO")
                
            # 保持程序运行
            while self.is_running:
                time.sleep(1)
                
        except Exception as e:
            error_msg = "框架运行异常: " + str(e)
            logging.error(error_msg, exc_info=True)
            # 调用错误回调函数
            if self.trader_callback:
                self.trader_callback.gui.log_message(error_msg, "ERROR")
            raise  # 重新抛出异常
            
        finally:
            # 记录策略结束运行时间并计算总耗时
            self.end_time = time.time()
            self.total_runtime = self.end_time - self.start_time
            end_datetime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            if self.trader_callback:
                self.trader_callback.gui.log_message(f"策略结束运行时间: {end_datetime}", "INFO")
                self.trader_callback.gui.log_message(f"策略总运行时长: {self.total_runtime:.2f}秒", "INFO")
                
                # 转换为更易读的格式
                hours = int(self.total_runtime // 3600)
                minutes = int((self.total_runtime % 3600) // 60)
                seconds = self.total_runtime % 60
                
                if hours > 0:
                    self.trader_callback.gui.log_message(f"策略运行时长: {hours}小时{minutes}分钟{seconds:.2f}秒", "INFO")
                elif minutes > 0:
                    self.trader_callback.gui.log_message(f"策略运行时长: {minutes}分钟{seconds:.2f}秒", "INFO")
                else:
                    self.trader_callback.gui.log_message(f"策略运行时长: {seconds:.2f}秒", "INFO")
            
            self.stop()

    def get_stock_list(self):
        """获取股票列表"""
        stock_codes = []
        try:
            # 优先从配置文件中的stock_list读取
            stock_codes = self.config.get_stock_list()
            if stock_codes:
                if self.trader_callback:
                    self.trader_callback.gui.log_message(f"从配置文件读取到 {len(stock_codes)} 支股票", "INFO")
            else:
                # 兼容性处理：尝试从stock_list_file文件读取（如果存在）
                stock_list_file = self.config.config_dict["data"].get("stock_list_file", "")
                if stock_list_file and os.path.exists(stock_list_file):
                    with open(stock_list_file, 'r', encoding='utf-8') as f:
                        stock_codes = [line.strip() for line in f if line.strip()]
                        if self.trader_callback:
                            self.trader_callback.gui.log_message(f"从兼容文件 {stock_list_file} 读取到 {len(stock_codes)} 支股票", "INFO")
                        # 将读取到的股票列表保存到配置文件中
                        self.config.update_stock_list(stock_codes)
                        self.config.save_config()
                else:
                    # 如果都没有，使用默认股票
                    stock_codes = ["000001.SZ"]
                    if self.trader_callback:
                        self.trader_callback.gui.log_message("股票列表为空，使用默认股票: 000001.SZ", "WARNING")

        except Exception as e:
            # 出现异常时使用默认股票
            stock_codes = ["000001.SZ"]
            if self.trader_callback:
                self.trader_callback.gui.log_message(f"读取股票列表出错: {str(e)}，使用默认股票: 000001.SZ", "ERROR")
        
        return stock_codes
    
    def _check_period_consistency(self):
        """检查数据周期和触发周期的一致性"""
        try:
            # 获取数据设置中的周期
            data_period = self.config.kline_period
            
            # 获取触发器类型
            trigger_type = self.config.config_dict.get("backtest", {}).get("trigger", {}).get("type", "tick")
            
            # 如果是自定义触发，则不需要检查
            if trigger_type == "custom":
                return
            
            # 定义周期映射关系
            period_consistency_map = {
                "tick": "tick",
                "1m": "1m", 
                "5m": "5m",
                "1d": "1d"
            }
            
            # 获取触发器对应的期望数据周期
            expected_data_period = period_consistency_map.get(trigger_type, "tick")
            
            # 检查是否一致
            if data_period != expected_data_period:
                # 构建提醒消息
                trigger_type_names = {
                    "tick": "Tick触发",
                    "1m": "1分钟K线触发",
                    "5m": "5分钟K线触发", 
                    "1d": "日K线触发"
                }
                
                data_period_names = {
                    "tick": "tick数据",
                    "1m": "1分钟K线",
                    "5m": "5分钟K线",
                    "1d": "日K线"
                }
                
                trigger_name = trigger_type_names.get(trigger_type, trigger_type)
                data_name = data_period_names.get(data_period, data_period)
                expected_name = data_period_names.get(expected_data_period, expected_data_period)
                
                message = f"""数据周期与触发类型不匹配！

当前配置：
• 数据设置周期：{data_name}
• 触发类型：{trigger_name}

建议配置：
• 数据设置周期：{expected_name}
• 触发类型：{trigger_name}

不匹配可能导致：
- 性能问题（数据精度过高或过低）
- 触发精度问题（错过关键时间点）
- 策略执行异常

是否继续运行回测？"""

                # 使用QMetaObject.invokeMethod在主线程中显示弹窗
                if self.trader_callback and hasattr(self.trader_callback, 'gui'):
                    # 创建一个标志变量来存储用户选择
                    user_choice = [None]  # 使用列表以便在lambda中修改
                    
                    def show_dialog():
                        msg_box = QMessageBox(self.trader_callback.gui)
                        msg_box.setIcon(QMessageBox.Warning)
                        msg_box.setWindowTitle("周期不匹配警告")
                        msg_box.setText(message)
                        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
                        msg_box.setDefaultButton(QMessageBox.No)
                        
                        # 设置按钮文本
                        yes_button = msg_box.button(QMessageBox.Yes)
                        no_button = msg_box.button(QMessageBox.No)
                        yes_button.setText("继续运行")
                        no_button.setText("停止运行")
                        
                        user_choice[0] = msg_box.exec_()
                    
                    # 在主线程中执行弹窗显示
                    QMetaObject.invokeMethod(
                        self.trader_callback.gui,
                        "invoke",
                        Qt.BlockingQueuedConnection,
                        Q_ARG("PyQt_PyObject", show_dialog)
                    )
                    
                    # 处理用户选择
                    if user_choice[0] == QMessageBox.No:
                        # 用户选择停止运行
                        if self.trader_callback:
                            self.trader_callback.gui.log_message("用户取消运行：数据周期与触发类型不匹配", "WARNING")
                        self.is_running = False
                        return
                    else:
                        # 用户选择继续运行，记录警告
                        if self.trader_callback:
                            self.trader_callback.gui.log_message(f"警告：继续运行不匹配配置 - 数据周期:{data_name}, 触发类型:{trigger_name}", "WARNING")
                else:
                    # 没有GUI回调的情况，直接在日志中记录警告
                    print(f"警告：数据周期({data_period})与触发类型({trigger_type})不匹配")
                    
        except Exception as e:
            # 检查过程中出现异常，记录但不影响回测继续运行
            if self.trader_callback:
                self.trader_callback.gui.log_message(f"周期一致性检查时出错: {str(e)}", "WARNING") 
            else:
                print(f"周期一致性检查时出错: {str(e)}")
        
    def _run_backtest(self):
        """回测模式"""
        try:
            # 检查数据周期和触发周期的一致性
            self._check_period_consistency()
            
            # 初始化回测记录字典
            self.backtest_records = {
                'trades': [],  # 交易记录
                'daily_stats': [],  # 每日统计数据
                'benchmark_data': [],  # 基准指数数据
                'start_time': self.config.backtest_start,
                'end_time': self.config.backtest_end,
                'init_capital': self.config.config_dict["backtest"]["init_capital"]
            }
            
            # 缓存日志开关状态，避免在回测循环中重复检查
            self._cache_should_log()

            if self.trader_callback:
                self.trader_callback.gui.log_message("开始回测...", "INFO")

            # 获取股票列表
            stock_codes = self.get_stock_list()

            # 单独处理基准指数数据
            benchmark_code = self.config.config_dict["backtest"]["benchmark"]

            # 获取策略文件名（不含路径和扩展名）
            strategy_file = self.config.config_dict.get("strategy_file", "")
            strategy_name = os.path.splitext(os.path.basename(strategy_file))[0] if strategy_file else "unknown"
            
            self._log(f"策略文件路径: {strategy_file}", "INFO")
            self._log(f"解析的策略名称: {strategy_name}", "INFO")

            # 生成回测时间戳
            backtest_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # 生成回测目录名（使用策略名称、回测时间范围和时间戳）
            # 为了避免调试器和中文路径的问题，直接使用ASCII安全的名称
            try:
                # 直接使用ASCII安全的策略名
                import hashlib
                strategy_hash = hashlib.md5(strategy_name.encode('utf-8')).hexdigest()[:8]
                backtest_dir_name = f"strategy_{strategy_hash}_{self.config.backtest_start}_{self.config.backtest_end}_{backtest_timestamp}"
                self._log(f"使用安全目录名: {backtest_dir_name} (原始策略名: {strategy_name})", "INFO")
            except Exception as e:
                self._log(f"生成目录名时出错: {str(e)}", "ERROR")
                # 使用默认名称
                backtest_dir_name = f"unknown_{self.config.backtest_start}_{self.config.backtest_end}_{backtest_timestamp}"
                self._log(f"使用默认目录名: {backtest_dir_name}", "INFO")

            # 确保backtest_results基础目录存在
            base_results_dir = "backtest_results"
            self._log(f"检查基础目录是否存在: {base_results_dir}", "INFO")
            try:
                if not os.path.exists(base_results_dir):
                    os.makedirs(base_results_dir, exist_ok=True)
                    self._log(f"创建基础回测目录: {base_results_dir}", "INFO")
                else:
                    self._log(f"基础目录已存在: {base_results_dir}", "INFO")
            except Exception as e:
                self._log(f"检查/创建基础目录时出错: {str(e)}", "ERROR")
                raise
            
            # 构建回测结果目录路径
            backtest_dir = os.path.join(
                base_results_dir,
                backtest_dir_name
            )
            self._log(f"完整回测结果目录路径: {os.path.abspath(backtest_dir)}", "INFO")
            
            # 尝试规范化路径，处理可能的编码问题
            try:
                backtest_dir = os.path.normpath(backtest_dir)
                self._log(f"规范化后的路径: {backtest_dir}", "INFO")
            except Exception as e:
                self._log(f"路径规范化失败: {str(e)}", "ERROR")

            # 确保目录存在，增强错误处理
            try:
                # 强制使用绝对路径以增加稳健性
                if not os.path.isabs(backtest_dir):
                    backtest_dir = os.path.abspath(backtest_dir)
                    self._log(f"已将回测目录转换为绝对路径: {backtest_dir}", "INFO")

                self._log(f"准备检查目录是否存在: {backtest_dir}", "INFO")
                
                # 检测是否在调试模式下
                import sys
                # 优先使用环境变量检测，这在编辑器调试模块中更可靠
                is_debugging = os.environ.get('KHQUANT_DEBUG_MODE') == '1' or (hasattr(sys, 'gettrace') and sys.gettrace() is not None)
                
                if is_debugging:
                    self._log(f"检测到调试模式，使用传统方法创建目录", "INFO")
                    # 在调试模式下，直接尝试创建目录，不检查是否存在
                    try:
                        # 使用exist_ok=True，如果目录已存在也不会报错
                        os.makedirs(backtest_dir, exist_ok=True)
                        self._log(f"已使用 os.makedirs 创建目录（或目录已存在）", "INFO")
                        time.sleep(0.2)
                    except Exception as e:
                        self._log(f"创建目录时出错: {str(e)}", "ERROR")
                        # 尝试使用绝对路径
                        abs_backtest_dir = os.path.abspath(backtest_dir)
                        self._log(f"尝试使用绝对路径: {abs_backtest_dir}", "INFO")
                        os.makedirs(abs_backtest_dir, exist_ok=True)
                        backtest_dir = abs_backtest_dir
                else:
                    # 非调试模式下可以使用pathlib
                    from pathlib import Path
                    backtest_path = Path(backtest_dir)
                    self._log(f"使用 pathlib.Path 处理路径: {backtest_path}", "INFO")
                    
                    if not backtest_path.exists():
                        self._log(f"目录不存在，尝试创建: {backtest_path}", "INFO")
                        backtest_path.mkdir(parents=True, exist_ok=True)
                        self._log(f"已使用 Path.mkdir 创建目录", "INFO")
                        time.sleep(0.2)
                    else:
                        self._log(f"目录已存在: {backtest_path}", "INFO")
                    
                    # 将路径转回字符串格式供后续使用
                    backtest_dir = str(backtest_path)
                
                # 再次验证目录是否存在（调试模式下跳过）
                if not is_debugging:
                    if not os.path.exists(backtest_dir):
                        self._log(f"目录创建后仍然不存在！", "ERROR")
                        raise FileNotFoundError(f"无法创建或访问回测结果目录: {backtest_dir}")
                    self._log(f"回测结果目录确认存在: {backtest_dir}", "INFO")
                else:
                    self._log(f"调试模式下跳过目录存在性验证", "INFO")
                    self._log(f"假定回测结果目录已创建: {backtest_dir}", "INFO")
                    
            except Exception as e:
                error_msg = f"创建回测结果目录时失败: {str(e)}"
                self._log(error_msg, "ERROR")
                raise Exception(error_msg)

            benchmark_file = os.path.join(backtest_dir, "benchmark.csv")

            if not os.path.exists(benchmark_file):
                if self.trader_callback:
                    self.trader_callback.gui.log_message(f"开始获取基准指数 {benchmark_code} 的每日数据", "INFO")
                
                try:
                    # 先下载数据
                    xtdata.download_history_data(
                        stock_code=benchmark_code,
                        period="1d",
                        start_time=self.config.backtest_start,
                        end_time=self.config.backtest_end,
                    )
                    
                    benchmark_data = xtdata.get_market_data_ex(
                        field_list=['time', 'close'],
                        stock_list=[benchmark_code],
                        period='1d',
                        start_time=self.config.backtest_start,
                        end_time=self.config.backtest_end,
                    )
                    
                    if self.trader_callback:
                        self.trader_callback.gui.log_message(
                            f"获取到的数据结构: {benchmark_data.keys()}", 
                            "INFO"
                        )
                        if benchmark_code in benchmark_data:
                            self.trader_callback.gui.log_message(
                                f"数据字段: {benchmark_data[benchmark_code].columns.tolist()}", 
                                "INFO"
                            )
                    
                    if benchmark_data and benchmark_code in benchmark_data:
                        df = benchmark_data[benchmark_code]
                        
                        if self.trader_callback:
                            self.trader_callback.gui.log_message(
                                f"基准数据形状: {df.shape}",
                                "INFO"
                            )
                        
                        # 确保有时间和收盘价列
                        if 'time' in df.columns and 'close' in df.columns:
                            # 转换时间戳为日期
                            try:
                                df['date'] = pd.to_datetime(df['time'], unit='ms')
                            except:
                                # 如果转换失败，尝试其他单位
                                try:
                                    df['date'] = pd.to_datetime(df['time'], unit='s')
                                except:
                                    try:
                                        df['date'] = pd.to_datetime(df['time'])
                                    except Exception as e:
                                        if self.trader_callback:
                                            self.trader_callback.gui.log_message(f"时间戳转换失败: {str(e)}", "ERROR")
                            
                            # 选择需要的列并保存
                            if 'date' in df.columns:
                                result_df = df[['date', 'close']].copy()
                                
                                if len(result_df) > 0:
                                    # 确保保存目录存在
                                    os.makedirs(os.path.dirname(benchmark_file), exist_ok=True)
                                    result_df.to_csv(benchmark_file, index=False)
                                    if self.trader_callback:
                                        self.trader_callback.gui.log_message(
                                            f"基准指数数据已保存到 {benchmark_file}, 共 {len(result_df)} 条记录",
                                            "INFO"
                                        )
                                        
                                    # 预先缓存所有基准指数数据，提高性能
                                    for _, row in result_df.iterrows():
                                        date_str = row['date'].strftime('%Y%m%d')
                                        cache_key = f"benchmark_{date_str}_{benchmark_code}"
                                        self._cached_benchmark_close[cache_key] = row['close']
                                    
                                    if self.trader_callback:
                                        self.trader_callback.gui.log_message(
                                            f"已预缓存 {len(self._cached_benchmark_close)} 条基准指数数据",
                                            "INFO"
                                        )
                except Exception as e:
                    if self.trader_callback:
                        self.trader_callback.gui.log_message(f"获取和保存基准指数数据失败: {str(e)}", "ERROR")
                    logging.error(f"获取和保存基准指数数据失败: {str(e)}", exc_info=True)
            
            # 获取数据周期
            data_period = self.trigger.get_data_period()
            
            # 一次性加载所有股票的历史数据
            historical_data = {}
            for code in stock_codes:
                if not self.is_running:
                    break
                    
                # 确保field_list中包含time和close字段
                field_list = self.config.config_dict["data"]["fields"]
                if "time" not in field_list:
                    field_list = ["time"] + field_list
                if "close" not in field_list:
                    field_list.append("close")
                    
                if self.trader_callback:
                    self.trader_callback.gui.log_message(f"加载{code}的历史数据...", "INFO")
                    
                # 根据触发器的数据周期加载对应的历史数据
                period = data_period
                
                # 对于自定义定时触发，需要特殊处理数据周期
                if isinstance(self.trigger, CustomTimeTrigger):
                    # 检查所有触发时间点是否都是整分钟（秒数为0）
                    all_whole_minutes = True
                    for seconds in self.trigger.trigger_seconds:
                        # 计算秒数部分
                        seconds_part = seconds % 60
                        if seconds_part != 0:
                            all_whole_minutes = False
                            break
                    
                    if all_whole_minutes:
                        # 如果所有时间点都是整分钟，使用1m数据
                        period = "1m"
                        if self.trader_callback:
                            self.trader_callback.gui.log_message(f"所有自定义时间点都是整分钟，使用1分钟K线数据", "INFO")
                    else:
                        # 如果有不是整分钟的时间点，使用tick数据
                        period = "tick"
                        if self.trader_callback:
                            self.trader_callback.gui.log_message(f"存在非整分钟的自定义时间点，使用tick数据", "INFO")
                
                # 对于其他触发器类型，直接使用触发器返回的数据周期
                if self.trader_callback:
                    self.trader_callback.gui.log_message(f"使用{period}数据周期进行回测", "INFO")
                
                data = xtdata.get_market_data_ex(
                    field_list=field_list,
                    stock_list=[code],
                    period=period,
                    start_time=self.config.backtest_start,
                    end_time=self.config.backtest_end,
                    dividend_type=self.config.config_dict["data"]["dividend_type"],
                    fill_data=True
                )
                if data and code in data:
                    # 判断是否为自定义时间触发
                    if isinstance(self.trigger, CustomTimeTrigger):
                        # 对于自定义时间触发，只保留触发时间点附近的数据
                        df = data[code]
                        if 'time' in df.columns:
                            # 获取所有时间戳
                            all_timestamps = df['time'].values
                            # 转换为秒级时间戳进行比较
                            filtered_rows = []
                            
                            for ts in all_timestamps:
                                # 转换时间戳为秒级
                                ts_seconds = float(ts) / 1000 if float(ts) > 1e10 else float(ts)
                                ts_dt = datetime.datetime.fromtimestamp(ts_seconds)
                                
                                # 计算当前时间点的秒数（从午夜开始）
                                current_seconds = ts_dt.hour * 3600 + ts_dt.minute * 60 + ts_dt.second
                                
                                # 检查是否接近任一触发时间点（允许1秒误差）
                                for trigger_second in self.trigger.trigger_seconds:
                                    if abs(current_seconds - trigger_second) <= 1:
                                        filtered_rows.append(ts)
                                        break
                            
                            # 只保留触发时间点附近的数据
                            if filtered_rows:
                                filtered_df = df[df['time'].isin(filtered_rows)]
                                historical_data[code] = filtered_df
                                if self.trader_callback:
                                    self.trader_callback.gui.log_message(
                                        f"自定义时间触发: {code}过滤后保留{len(filtered_df)}个时间点，原始数据有{len(df)}个时间点", 
                                        "INFO"
                                    )
                            else:
                                # 如果没有找到匹配的时间点，仍然保存原始数据
                                historical_data[code] = df
                                if self.trader_callback:
                                    self.trader_callback.gui.log_message(
                                        f"警告: {code}没有找到匹配的自定义时间点，使用原始数据", 
                                        "WARNING"
                                    )
                        else:
                            # 如果没有time列，使用原始数据
                            historical_data[code] = data[code]
                            if self.trader_callback:
                                self.trader_callback.gui.log_message(
                                    f"警告: {code}的数据中没有time列，无法按自定义时间过滤", 
                                    "WARNING"
                                )
                    else:
                        # 非自定义时间触发，直接存储DataFrame
                        historical_data[code] = data[code]
            
            if not self.is_running:
                if self.trader_callback:
                    self.trader_callback.gui.log_message("回测被中止", "WARNING")
                return
                    
            # 获取所有时间点
            all_times = []

            # 对于自定义时间触发，使用不同的方式获取时间点
            if isinstance(self.trigger, CustomTimeTrigger):
                # 获取回测日期范围内的所有交易日
                start_date = datetime.datetime.strptime(self.config.backtest_start, "%Y%m%d").date()
                end_date = datetime.datetime.strptime(self.config.backtest_end, "%Y%m%d").date()
                
                # 获取交易日历（使用KhQuTools的真实交易日判断）
                current_date = start_date
                trading_days = []
                while current_date <= end_date:
                    # 使用KhQuTools判断是否为真实交易日（排除节假日）
                    date_str = current_date.strftime("%Y-%m-%d")
                    if self.tools.is_trade_day(date_str):
                        trading_days.append(current_date)
                    current_date += datetime.timedelta(days=1)
                
                if self.trader_callback:
                    self.trader_callback.gui.log_message(f"回测期间共有{len(trading_days)}个交易日", "INFO")
                
                # 为每个交易日生成自定义触发时间点
                for day in trading_days:
                    for seconds in self.trigger.trigger_seconds:
                        # 将秒数转换为时分秒
                        h = seconds // 3600
                        m = (seconds % 3600) // 60
                        s = seconds % 60
                        
                        # 创建完整的datetime对象
                        dt = datetime.datetime.combine(day, datetime.time(h, m, s))
                        
                        # 转换为时间戳（秒级）
                        timestamp = int(dt.timestamp())
                        all_times.append(timestamp)
                
                if self.trader_callback:
                    self.trader_callback.gui.log_message(f"自定义时间触发模式：生成了{len(all_times)}个时间点", "INFO")
            else:
                # 非自定义时间触发模式，使用原来的方式获取时间点
                for code, df in historical_data.items():
                    if self.trader_callback:
                        self.trader_callback.gui.log_message(f"处理{code}的数据...", "INFO")
                    
                    # 检查数据结构
                    if isinstance(df, pd.DataFrame):
                        # DataFrame结构处理
                        if 'time' in df.columns:
                            times = df['time'].values
                            all_times.extend(times)
                            if self.trader_callback:
                                self.trader_callback.gui.log_message(f"从{code}的DataFrame中提取了{len(times)}个时间点", "INFO")
                        else:
                            # 尝试使用其他可能的时间字段
                            time_field = None
                            for field in ['timestamp', 'date', 'datetime']:
                                if field in df.columns:
                                    time_field = field
                                    break
                            
                            if time_field is None:
                                # 如果没有找到任何时间字段，尝试使用索引
                                if isinstance(df.index, pd.DatetimeIndex):
                                    times = df.index.astype(np.int64) // 10**9  # 转换为秒级时间戳
                                    all_times.extend(times)
                                    if self.trader_callback:
                                        self.trader_callback.gui.log_message(f"从{code}的DataFrame索引中提取了{len(times)}个时间点", "INFO")
                                else:
                                    # 如果没有找到任何时间字段，跳过这个股票
                                    if self.trader_callback:
                                        self.trader_callback.gui.log_message(f"错误: {code}的数据中没有找到任何时间字段，跳过该股票", "ERROR")
                                    continue
                            else:
                                times = df[time_field].values
                                all_times.extend(times)
                                if self.trader_callback:
                                    self.trader_callback.gui.log_message(f"从{code}的DataFrame的{time_field}字段中提取了{len(times)}个时间点", "INFO")
                    else:
                        # 处理其他可能的数据结构
                        if self.trader_callback:
                            self.trader_callback.gui.log_message(f"警告: {code}的数据不是DataFrame格式，跳过该股票", "WARNING")
                        continue
            
            # 去重并排序
            all_times = sorted(list(set(all_times)))
            
            if len(all_times) == 0:
                if self.trader_callback:
                    self.trader_callback.gui.log_message("错误: 没有找到任何有效的时间点，无法进行回测", "ERROR")
                return
            
            if self.trader_callback:
                self.trader_callback.gui.log_message(f"共找到{len(all_times)}个时间点", "INFO")
                self.trader_callback.gui.log_message(f"第一个时间点: {all_times[0]}", "INFO")
                self.trader_callback.gui.log_message(f"最后一个时间点: {all_times[-1]}", "INFO")
            
            # 保存所有时间点到实例变量，供record_results使用
            self.all_times = all_times
            
            total_times = len(all_times)
            processed_times = 0
            
            # 计算进度显示增量（至少为1，最多为总数/100向上取整）
            if total_times > 100:
                progress_increment = max(1, int(total_times / 100))
            else:
                # 如果时间点太少，则每处理一个点都显示一次进度
                progress_increment = 1
                
            # 显示开始进度
            if self.trader_callback:
                self.trader_callback.gui.log_message("回测进度: 0.00%", "INFO")
                # 强制发送0%进度信号，确保进度条立即显示
                self.trader_callback.gui.progress_signal.emit(0)
                # 注意：不要在子线程中调用 QApplication.processEvents()
                # 这会导致GUI线程阻塞和潜在的线程安全问题
            
            # 预先构建数据缓存（避免在循环中重复构建）
            if not hasattr(self, 'historical_data_ref'):
                if self.trader_callback:
                    self.trader_callback.gui.log_message("首次运行，正在构建数据缓存...", "INFO")
                
                # 创建包含原始DataFrame引用的字典
                self.historical_data_ref = {}
                
                # 创建时间字段到索引的映射，用于快速查找
                self.time_field_cache = {}
                self.time_idx_cache = {}
                
                # 创建基于当前时间点的数据引用
                for code, df in historical_data.items():
                    # 找到时间字段
                    for field in ['time', 'timestamp', 'date', 'datetime']:
                        if field in df.columns:
                            self.time_field_cache[code] = field
                            # 保存原始DataFrame引用
                            self.historical_data_ref[code] = df
                            
                            # 预先创建时间值到索引的映射（只计算一次）
                            time_values = df[field].values
                            time_idx_map = {}
                            for i, tv in enumerate(time_values):
                                time_idx_map[tv] = i
                            self.time_idx_cache[code] = time_idx_map
                            break
                
                if self.trader_callback:
                    self.trader_callback.gui.log_message("数据缓存构建完成", "INFO")
            
            # 按时间顺序模拟
            current_date = None
            day_start_time = None
            day_data = {}
            
            # 获取盘前盘后回调设置
            pre_market_enabled = self.config.config_dict.get("market_callback", {}).get("pre_market_enabled", False)
            pre_market_time = self.config.config_dict.get("market_callback", {}).get("pre_market_time", "08:30:00")
            post_market_enabled = self.config.config_dict.get("market_callback", {}).get("post_market_enabled", False)
            post_market_time = self.config.config_dict.get("market_callback", {}).get("post_market_time", "15:30:00")
            
            if pre_market_enabled and self.trader_callback:
                self.trader_callback.gui.log_message(f"已启用盘前回调，将在每个交易日 {pre_market_time} 执行", "INFO")
                # 检查策略是否实现了盘前回调方法
                if not hasattr(self.strategy_module, 'khPreMarket'):
                    self.trader_callback.gui.log_message("警告: 策略模块未实现 khPreMarket 方法，盘前回调将不会执行", "WARNING")
            if post_market_enabled and self.trader_callback:
                self.trader_callback.gui.log_message(f"已启用盘后回调，将在每个交易日 {post_market_time} 执行", "INFO")
                # 检查策略是否实现了盘后回调方法
                if not hasattr(self.strategy_module, 'khPostMarket'):
                    self.trader_callback.gui.log_message("警告: 策略模块未实现 khPostMarket 方法，盘后回调将不会执行", "WARNING")
            
            # 获取唯一的交易日列表
            trading_days = set()
            for time_point in all_times:
                try:
                    timestamp = int(time_point)
                    # 判断时间戳精度（秒级或毫秒级）
                    if timestamp > 1e10:  # 毫秒级时间戳
                        dt = datetime.datetime.fromtimestamp(timestamp / 1000)
                    else:  # 秒级时间戳
                        dt = datetime.datetime.fromtimestamp(timestamp)
                    trading_days.add(dt.strftime("%Y-%m-%d"))
                except:
                    pass
            
            trading_days = sorted(list(trading_days))
            if self.trader_callback:
                self.trader_callback.gui.log_message(f"回测期间共有 {len(trading_days)} 个交易日", "INFO")
            
            # 初始化时间统计变量
            time_stats = {
                "构造数据": 0,
                "构造时间信息": 0,
                "检查新日期": 0,
                "盘后回调": 0,
                "盘前回调": 0,
                "触发器检查": 0,
                "风控检查": 0,
                "策略处理": 0,
                "处理信号": 0,
                "交易指令": 0,
                "记录结果": 0,
                "总时间": 0
            }
            
            for current_time in all_times:
                loop_start_time = time.time()
                
                if not self.is_running:
                    if self.trader_callback:
                        self.trader_callback.gui.log_message("回测被中止", "WARNING")
                    break
                    
                processed_times += 1
                # 根据计算的增量显示进度，但确保前几次都显示
                should_show_progress = False
                if processed_times <= 5:  # 前5次都显示
                    should_show_progress = True
                elif processed_times % progress_increment == 0:  # 按增量显示
                    should_show_progress = True
                elif processed_times == total_times:  # 最后一次也显示
                    should_show_progress = True
                
                if should_show_progress and self.trader_callback:
                    progress = (processed_times / total_times) * 100
                    # 直接发送进度信号更新进度条（高效，不走日志系统）
                    if hasattr(self.trader_callback.gui, 'progress_signal'):
                        self.trader_callback.gui.progress_signal.emit(int(progress))
                    # 只在需要输出日志时才记录进度文本
                    if self._should_log():
                        self.trader_callback.gui.log_message(f"回测进度: {progress:.2f}%", "INFO")
                
                # 进一步优化的构造数据代码
                data_start_time = time.time()
                
                # 创建包含__current_time__的字典结构
                try:
                    timestamp = int(current_time)
                    # 判断时间戳精度（秒级或毫秒级）
                    if timestamp > 1e10:  # 毫秒级时间戳
                        dt = datetime.datetime.fromtimestamp(timestamp / 1000)
                    else:  # 秒级时间戳
                        dt = datetime.datetime.fromtimestamp(timestamp)
                        
                    time_info = {
                        "timestamp": timestamp,
                        "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "date": dt.strftime("%Y-%m-%d"),
                        "time": dt.strftime("%H:%M:%S"),
                        "raw_time": current_time
                    }
                except Exception as e:
                    # 如果转换失败，使用原始时间戳
                    time_info = {
                        "timestamp": current_time,
                        "datetime": str(current_time),
                        "date": str(current_time),
                        "time": str(current_time),
                        "raw_time": current_time
                    }
                
                # 创建当前时间点的数据视图
                current_data = {"__current_time__": time_info}
                
                # 直接添加数据引用，而不是转换为字典
                for code in self.historical_data_ref:
                    if code in self.time_field_cache and code in self.time_idx_cache:
                        time_field = self.time_field_cache[code]
                        time_idx_map = self.time_idx_cache[code]
                        df = self.historical_data_ref[code]
                        
                        # 尝试直接匹配当前时间
                        if current_time in time_idx_map:
                            idx = time_idx_map[current_time]
                            # 直接存储行引用，而不是转换为字典
                            current_data[code] = df.iloc[idx]
                        else:
                            # 尝试处理精度不一致问题
                            matched = False
                            idx = -1
                            
                            if isinstance(current_time, (int, float)):
                                # 处理毫秒/秒的转换
                                if current_time > 1e10:  # 毫秒级
                                    sec_time = current_time // 1000
                                    if sec_time in time_idx_map:
                                        idx = time_idx_map[sec_time]
                                        matched = True
                                else:  # 秒级
                                    ms_time = current_time * 1000
                                    if ms_time in time_idx_map:
                                        idx = time_idx_map[ms_time]
                                        matched = True
                            
                            if matched:
                                # 直接存储行引用
                                current_data[code] = df.iloc[idx]
                            else:
                                # 没有匹配的数据，存储空Series
                                current_data[code] = pd.Series({})
                    else:
                        # 没有时间字段的情况
                        current_data[code] = pd.Series({})
                
                time_stats["构造数据"] += time.time() - data_start_time
                
                # 添加日志，显示第一个股票的数据示例（仅在需要输出日志时执行）
                if processed_times == 1 and self.trader_callback and current_data and self._should_log():
                    # 获取第一个股票代码
                    first_stock = None
                    for code in current_data:
                        if code != "__current_time__":
                            first_stock = code
                            break

                    if first_stock:
                        sample_data = current_data[first_stock]
                        self.trader_callback.gui.log_message(f"数据样例 - 股票: {first_stock}, 字段: {list(sample_data.keys())}", "INFO")
                        # 打印每个字段的值（最多显示5个字段）
                        sample_str = ""
                        count = 0
                        for key, value in sample_data.items():
                            if count < 5:
                                sample_str += f"{key}: {value}, "
                                count += 1
                        if sample_str:
                            self.trader_callback.gui.log_message(f"部分字段值: {sample_str[:-2]}", "INFO")
                
                # 构造时间信息
                time_info_start = time.time()
                try:
                    timestamp = int(current_time)
                    # 判断时间戳精度（秒级或毫秒级）
                    if timestamp > 1e10:  # 毫秒级时间戳
                        dt = datetime.datetime.fromtimestamp(timestamp / 1000)
                    else:  # 秒级时间戳
                        dt = datetime.datetime.fromtimestamp(timestamp)
                        
                    time_info = {
                        "timestamp": timestamp,
                        "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "date": dt.strftime("%Y-%m-%d"),
                        "time": dt.strftime("%H:%M:%S"),
                        "raw_time": current_time
                    }
                except Exception as e:
                    # 如果转换失败，使用原始时间戳
                    time_info = {
                        "timestamp": current_time,
                        "datetime": str(current_time),
                        "date": str(current_time),
                        "time": str(current_time),
                        "raw_time": current_time
                    }
                
                # 添加当前时间信息到数据中
                # 添加时间信息到数据中
                current_data["__current_time__"] = time_info
                time_stats["构造时间信息"] += time.time() - time_info_start
                
                # 添加账户和持仓信息到数据字典
                account_data = {
                    "__account__": self.trade_mgr.assets
                }
                # 添加持仓信息
                positions_data = {
                    "__positions__": self.trade_mgr.positions
                }
                # 添加股票池信息
                stock_list_data = {
                    "__stock_list__": stock_codes
                }
                # 合并所有信息
                current_data.update(account_data)
                current_data.update(positions_data)
                current_data.update(stock_list_data)
                
                # 检查是否是新的一天
                new_day_start = time.time()
                if current_date != time_info["date"]:
                    # 如果有前一天的数据，执行盘后回调
                    post_market_start = time.time()
                    if current_date is not None and post_market_enabled and hasattr(self.strategy_module, 'khPostMarket'):
                        # 执行盘后回调
                        try:
                            if self.trader_callback:
                                self.trader_callback.gui.log_message(f"执行盘后回调 - 日期: {current_date}", "INFO")
                            
                            # 设置时间信息为盘后时间
                            post_time_info = time_info.copy()
                            post_time_info["time"] = post_market_time
                            post_time_info["datetime"] = f"{current_date} {post_market_time}"
                            
                            # 使用最后一个时间点的数据或创建一个完整的数据结构
                            post_data = day_data.copy() if day_data else {}
                            post_data["__current_time__"] = post_time_info
                            
                            # 添加账户和持仓信息到数据字典
                            post_data["__account__"] = self.trade_mgr.assets
                            post_data["__positions__"] = self.trade_mgr.positions
                            post_data["__stock_list__"] = stock_codes
                            
                            # 添加框架实例到数据字典
                            post_data["__framework__"] = self
                            
                            # 执行盘后回调
                            post_signals = self.strategy_module.khPostMarket(post_data)
                            
                            # 处理盘后回调产生的信号
                            if post_signals:
                                for signal in post_signals:
                                    if 'price' in signal:
                                        signal['price'] = round(float(signal['price']), self.price_decimals)
                                    signal['timestamp'] = time_info["timestamp"]
                                
                                # 发送交易指令
                                self.trade_mgr.process_signals(post_signals)
                        except Exception as e:
                            if self.trader_callback:
                                self.trader_callback.gui.log_message(f"执行盘后回调时出错: {str(e)}", "ERROR")
                    time_stats["盘后回调"] += time.time() - post_market_start

                    # 更新当前日期
                    current_date = time_info["date"]
                    day_start_time = time_info["timestamp"]
                    day_data = current_data

                    # T+1模式下，新交易日将 can_use_volume 更新为 volume
                    if not self.trade_mgr.t0_mode:
                        for code, pos in self.trade_mgr.positions.items():
                            if pos.get("volume", 0) > 0:
                                pos["can_use_volume"] = pos["volume"]

                    # 检查是否需要执行盘前回调
                    pre_market_start = time.time()
                    if pre_market_enabled and hasattr(self.strategy_module, 'khPreMarket'):
                        # 执行盘前回调
                        try:
                            if self.trader_callback:
                                self.trader_callback.gui.log_message(f"执行盘前回调 - 日期: {current_date}", "INFO")
                            
                            # 设置时间信息为盘前时间
                            pre_time_info = time_info.copy()
                            pre_time_info["time"] = pre_market_time
                            pre_time_info["datetime"] = f"{current_date} {pre_market_time}"
                            
                            # 使用当前时间点的数据或创建一个完整的数据结构
                            pre_data = current_data.copy()
                            pre_data["__current_time__"] = pre_time_info
                            
                            # 确保包含账户和持仓信息
                            pre_data["__account__"] = self.trade_mgr.assets
                            pre_data["__positions__"] = self.trade_mgr.positions
                            pre_data["__stock_list__"] = stock_codes
                            
                            # 添加框架实例到数据字典
                            pre_data["__framework__"] = self
                            
                            # 执行盘前回调
                            pre_signals = self.strategy_module.khPreMarket(pre_data)
                            
                            # 处理盘前回调产生的信号
                            if pre_signals:
                                for signal in pre_signals:
                                    if 'price' in signal:
                                        signal['price'] = round(float(signal['price']), self.price_decimals)
                                    signal['timestamp'] = time_info["timestamp"]
                                
                                # 发送交易指令
                                self.trade_mgr.process_signals(pre_signals)
                        except Exception as e:
                            if self.trader_callback:
                                self.trader_callback.gui.log_message(f"执行盘前回调时出错: {str(e)}", "ERROR")
                    time_stats["盘前回调"] += time.time() - pre_market_start
                else:
                    # 更新当天的数据
                    day_data = current_data
                time_stats["检查新日期"] += time.time() - new_day_start
                
                # 使用触发器判断是否应该触发策略
                trigger_start = time.time()
                if not self.trigger.should_trigger(current_time, current_data):
                    time_stats["触发器检查"] += time.time() - trigger_start
                    continue
                time_stats["触发器检查"] += time.time() - trigger_start
                
                # 风控检查
                risk_start = time.time()
                if not self.risk_mgr.check_risk(current_data):
                    time_stats["风控检查"] += time.time() - risk_start
                    continue
                time_stats["风控检查"] += time.time() - risk_start
                
                # 检查是否是交易日
                current_date_str = current_data.get("__current_time__", {}).get("date", "")
                if current_date_str and not self.tools.is_trade_day(current_date_str):
                    # 如果不是交易日，跳过策略调用
                    continue
                
                # 添加框架实例到数据字典
                current_data["__framework__"] = self
                
                # 检查股票数据是否为空
                stock_data_empty = True
                empty_stocks = []
                for key, value in current_data.items():
                    # 跳过框架内部字段
                    if key.startswith("__"):
                        continue
                    # 检查股票数据是否为空
                    if isinstance(value, pd.Series) and not value.empty:
                        stock_data_empty = False
                    elif isinstance(value, pd.Series) and value.empty:
                        empty_stocks.append(key)
                    elif not value:  # 处理其他空值情况
                        empty_stocks.append(key)
                
                # 如果所有股票数据都为空，记录错误并跳过策略调用
                if stock_data_empty:
                    current_time_str = current_data.get("__current_time__", {}).get("datetime", str(current_time))
                    if self.trader_callback:
                        self.trader_callback.gui.log_message(
                            f"警告: 时间点 {current_time_str} 的所有股票数据为空，跳过策略调用", 
                            "WARNING"
                        )
                        if empty_stocks:
                            self.trader_callback.gui.log_message(
                                f"空数据股票列表: {', '.join(empty_stocks[:10])}" + 
                                (f" 等{len(empty_stocks)}只股票" if len(empty_stocks) > 10 else ""),
                                "WARNING"
                            )
                    continue
                
                # 如果有部分股票数据为空，记录警告但继续执行
                if empty_stocks:
                    current_time_str = current_data.get("__current_time__", {}).get("datetime", str(current_time))
                    if self.trader_callback:
                        self.trader_callback.gui.log_message(
                            f"警告: 时间点 {current_time_str} 有 {len(empty_stocks)} 只股票数据为空: {', '.join(empty_stocks[:5])}" + 
                            (f" 等" if len(empty_stocks) > 5 else ""),
                            "WARNING"
                        )
                
                # 调用策略处理
                strategy_start = time.time()
                signals = self.strategy_module.khHandlebar(current_data)
                time_stats["策略处理"] += time.time() - strategy_start
                
                # 处理信号中的价格精度
                signal_process_start = time.time()
                if signals:
                    for signal in signals:
                        if 'price' in signal:
                            # 使用动态精度
                            signal['price'] = round(float(signal['price']), self.price_decimals)
                        # 添加当前回测时间戳
                        signal['timestamp'] = current_time
                time_stats["处理信号"] += time.time() - signal_process_start
                
                # 发送交易指令
                trade_start = time.time()
                if signals:
                    self.trade_mgr.process_signals(signals)
                time_stats["交易指令"] += time.time() - trade_start
                
                # 记录结果
                record_start = time.time()
                self.record_results(current_time, current_data, signals)
                time_stats["记录结果"] += time.time() - record_start
                
                # 累计总时间
                time_stats["总时间"] += time.time() - loop_start_time
            
            # 输出时间统计信息
            if self.trader_callback:
                total_time = time_stats["总时间"]
                if total_time > 0:
                    self.trader_callback.gui.log_message("回测各部分执行时间统计:", "INFO")
                    for key, value in time_stats.items():
                        if key != "总时间":
                            percentage = (value / total_time) * 100
                            self.trader_callback.gui.log_message(f"{key}: {value:.4f}秒 ({percentage:.2f}%)", "INFO")
                    self.trader_callback.gui.log_message(f"总执行时间: {total_time:.4f}秒", "INFO")
            
            # 处理最后一天的盘后回调
            if current_date is not None and post_market_enabled and hasattr(self.strategy_module, 'khPostMarket'):
                try:
                    if self.trader_callback:
                        self.trader_callback.gui.log_message(f"执行最后一天的盘后回调 - 日期: {current_date}", "INFO")
                    
                    # 设置时间信息为盘后时间
                    time_info = (day_data.get("__current_time__", {}) if day_data else {}).copy()
                    if not time_info:
                        # 如果没有时间信息，创建一个默认的
                        time_info = {
                            "timestamp": int(time.time()),
                            "date": current_date,
                            "time": post_market_time,
                            "datetime": f"{current_date} {post_market_time}"
                        }
                    else:
                        time_info["time"] = post_market_time
                        time_info["datetime"] = f"{current_date} {post_market_time}"
                    
                    # 使用最后一个时间点的数据或创建一个完整的数据结构
                    post_data = day_data.copy() if day_data else {}
                    post_data["__current_time__"] = time_info
                    
                    # 添加账户和持仓信息到数据字典
                    post_data["__account__"] = self.trade_mgr.assets
                    post_data["__positions__"] = self.trade_mgr.positions
                    post_data["__stock_list__"] = self.get_stock_list()
                    
                    # 添加框架实例到数据字典
                    post_data["__framework__"] = self
                    
                    # 执行盘后回调
                    post_signals = self.strategy_module.khPostMarket(post_data)
                    
                    # 处理盘后回调产生的信号
                    if post_signals:
                        for signal in post_signals:
                            if 'price' in signal:
                                signal['price'] = round(float(signal['price']), self.price_decimals)
                            signal['timestamp'] = time_info["timestamp"]
                        
                        # 发送交易指令
                        self.trade_mgr.process_signals(post_signals)
                except Exception as e:
                    if self.trader_callback:
                        self.trader_callback.gui.log_message(f"执行最后一天的盘后回调时出错: {str(e)}", "ERROR")
                
            # 回测完成后发送信号
            if self.trader_callback:
                # 先停止策略并更新状态
                self.is_running = False
                try:
                    from PyQt5.QtCore import QMetaObject, Qt
                    QMetaObject.invokeMethod(
                        self.trader_callback.gui,
                        "on_strategy_finished",
                        Qt.QueuedConnection
                    )
                except Exception:
                    # 如果invoke失败，退回到直接调用，但至少捕获异常
                    self.trader_callback.gui.on_strategy_finished()
                
                # 显示100%进度
                self.trader_callback.gui.log_message("回测进度: 100.00%", "INFO")
                self.trader_callback.gui.log_message("回测完成", "INFO")
                
            # 在回测完成后保存回测记录
            try:
                # 获取策略文件名（不含路径和扩展名）
                strategy_file = self.config.config_dict.get("strategy_file", "")
                strategy_name = os.path.splitext(os.path.basename(strategy_file))[0] if strategy_file else "unknown"
                
                # 生成回测时间戳
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                
                # 创建当前回测的子目录（包含策略名）
                backtest_dir = os.path.join(
                    "backtest_results",
                    backtest_dir_name
                )

                # 创建新目录（由于包含时间戳，目录名唯一，无需删除）
                os.makedirs(backtest_dir, exist_ok=True)

                def _safe_to_csv(df: pd.DataFrame, target_path: str, desc: str):
                    """处理共享冲突时的容错写入"""
                    for attempt in range(1, 4):
                        try:
                            df.to_csv(target_path, index=False, encoding='utf-8-sig')
                            return
                        except PermissionError:
                            wait_time = 0.2 * attempt
                            if self.trader_callback:
                                self.trader_callback.gui.log_message(
                                    f"{desc}保存失败（文件被占用），{wait_time:.1f}s后重试({attempt}/3)",
                                    "WARNING"
                                )
                            time.sleep(wait_time)
                        except Exception as e:
                            self._log(f"{desc}保存失败: {e}", "ERROR")
                            raise

                # 保存交易记录
                trades_df = pd.DataFrame(self.backtest_records['trades'])
                if len(trades_df) == 0:
                    trades_df = pd.DataFrame(columns=[
                        'datetime', 'code', 'action', 'price', 'volume', 'amount',
                        'commission', 'stamp_tax', 'transfer_fee', 'flow_fee',
                        'total_asset', 'cash', 'market_value'
                    ])
                    if self.trader_callback:
                        self.trader_callback.gui.log_message("回测期间没有产生交易记录", "WARNING")
                _safe_to_csv(trades_df, os.path.join(backtest_dir, "trades.csv"), "交易记录")

                # 保存每日统计数据
                daily_stats_df = pd.DataFrame(self.backtest_records['daily_stats'])
                if len(daily_stats_df) == 0:
                    daily_stats_df = pd.DataFrame(columns=[
                        'date', 'total_asset', 'cash', 'market_value', 
                        'daily_return', 'benchmark_close', 'positions'
                    ])
                    if self.trader_callback:
                        self.trader_callback.gui.log_message("回测期间没有产生每日统计数据", "WARNING")
                _safe_to_csv(daily_stats_df, os.path.join(backtest_dir, "daily_stats.csv"), "每日统计数据")

                # 保存回测汇总指标
                try:
                    if len(daily_stats_df) >= 2:
                        init_capital = self.backtest_records['init_capital']
                        final_capital = daily_stats_df['total_asset'].iloc[-1]
                        trade_days = len(daily_stats_df)

                        # 计算总收益率
                        total_return = (final_capital - init_capital) / init_capital * 100 if init_capital > 0 else 0

                        # 计算年化收益率: ((1+R)^(250/n)-1)*100%
                        if trade_days > 0 and init_capital > 0:
                            total_return_decimal = (final_capital / init_capital) - 1
                            annual_return = (pow(1 + total_return_decimal, 250/trade_days) - 1) * 100
                        else:
                            annual_return = 0

                        # 计算最大回撤
                        cummax = daily_stats_df['total_asset'].cummax()
                        drawdown = (cummax - daily_stats_df['total_asset']) / cummax * 100
                        max_drawdown = drawdown.max()

                        # 保存汇总指标
                        summary = {
                            'init_capital': init_capital,
                            'final_capital': final_capital,
                            'total_return': total_return,
                            'annual_return': annual_return,
                            'max_drawdown': max_drawdown,
                            'trade_days': trade_days
                        }
                        _safe_to_csv(pd.DataFrame([summary]), os.path.join(backtest_dir, "summary.csv"), "回测汇总")
                except Exception as e:
                    logging.warning(f"保存回测汇总指标时出错: {str(e)}")

                # 保存基准指数数据
                benchmark_code = self.config.config_dict["backtest"]["benchmark"]
                try:
                    # 先下载数据确保可用
                    xtdata.download_history_data(
                        stock_code=benchmark_code,
                        period="1d",
                        start_time=self.config.backtest_start,
                        end_time=self.config.backtest_end,
                    )
                    
                    benchmark_data = xtdata.get_market_data(
                        field_list=['close'],
                        stock_list=[benchmark_code],
                        period='1d',
                        start_time=self.config.backtest_start,
                        end_time=self.config.backtest_end,
                    )
                    
                    if self.trader_callback:
                        self.trader_callback.gui.log_message(
                            f"基准数据获取结果: {benchmark_data.keys()}", 
                            "INFO"
                        )

                    if benchmark_data and 'close' in benchmark_data and len(benchmark_data['close']) > 0:
                        closes = benchmark_data['close'].values[0]  # 获取收盘价数据
                        if len(closes) > 0:
                            # 直接从benchmark_data中获取日期数据
                            # 获取交易日期索引
                            if 'date' in benchmark_data:
                                dates = benchmark_data['date'][0]  # 使用benchmark_data中的日期
                            elif hasattr(benchmark_data, 'index') and benchmark_data.index is not None and not isinstance(benchmark_data.index, pd.RangeIndex):
                                dates = benchmark_data.index  # 有些情况下日期可能在索引中
                            elif hasattr(benchmark_data['close'], 'columns') and len(benchmark_data['close'].columns) > 0:
                                # 从columns中获取日期（日期作为列名出现的情况）
                                date_cols = [col for col in benchmark_data['close'].columns if str(col).isdigit()]
                                if date_cols:
                                    # 将列名转换为日期对象
                                    dates = pd.to_datetime(date_cols, format='%Y%m%d')
                                    # 确保closes的顺序与dates匹配
                                    closes = np.array([benchmark_data['close'].iloc[0][col] for col in date_cols])
                                else:
                                    # 如果没有日期数据，才使用日期范围（不推荐）
                                    self.trader_callback.gui.log_message(
                                        "警告：基准数据中没有日期信息，将使用日期范围替代，可能不准确",
                                        "WARNING"
                                    )
                                    dates = pd.date_range(
                                        start=pd.to_datetime(self.config.backtest_start, format='%Y%m%d'),
                                        end=pd.to_datetime(self.config.backtest_end, format='%Y%m%d'),
                                        freq='B'  # 使用工作日频率
                                    )
                                
                                # 创建包含日期和收盘价的DataFrame
                                df = pd.DataFrame({
                                    'date': dates,
                                    'close': closes
                                })
                                
                                # 保存到benchmark.csv
                                benchmark_file = os.path.join(backtest_dir, "benchmark.csv")
                                _safe_to_csv(df, benchmark_file, "基准数据")
                                
                                if self.trader_callback:
                                    self.trader_callback.gui.log_message(
                                        f"基准指数数据已保存到 {benchmark_file}, 共 {len(df)} 条记录",
                                        "INFO"
                                    )
                        else:
                            if self.trader_callback:
                                self.trader_callback.gui.log_message(f"基准指数 {benchmark_code} 收盘价数据为空", "WARNING")
                    else:
                        if self.trader_callback:
                            self.trader_callback.gui.log_message(f"基准指数 {benchmark_code} 数据获取失败", "WARNING")
                except Exception as e:
                    if self.trader_callback:
                        self.trader_callback.gui.log_message(f"获取基准指数数据时出错: {str(e)}", "ERROR")
                    logging.error(f"获取基准指数数据时出错: {str(e)}", exc_info=True)
                
                # 保存策略文件副本
                strategy_file_path = self.config.config_dict.get("strategy_file", "")
                if strategy_file_path and os.path.exists(strategy_file_path):
                    try:
                        # 保存.py文件
                        strategy_filename = os.path.basename(strategy_file_path)
                        strategy_backup_path = os.path.join(backtest_dir, strategy_filename)
                        shutil.copy2(strategy_file_path, strategy_backup_path)
                        
                        # 查找并保存对应的.kh文件
                        kh_file_path = os.path.splitext(strategy_file_path)[0] + ".kh"
                        if os.path.exists(kh_file_path):
                            kh_filename = os.path.basename(kh_file_path)
                            kh_backup_path = os.path.join(backtest_dir, kh_filename)
                            shutil.copy2(kh_file_path, kh_backup_path)
                            if self.trader_callback:
                                self.trader_callback.gui.log_message(f"策略配置文件已保存: {kh_filename}", "INFO")
                        
                        if self.trader_callback:
                            self.trader_callback.gui.log_message(f"策略文件已保存: {strategy_filename}", "INFO")
                            
                    except Exception as e:
                        if self.trader_callback:
                            self.trader_callback.gui.log_message(f"保存策略文件时出错: {str(e)}", "ERROR")
                        logging.error(f"保存策略文件时出错: {str(e)}", exc_info=True)
                
                # 保存完整的配置文件副本
                try:
                    config_file_path = getattr(self.config, 'config_path', None)
                    if config_file_path and os.path.exists(config_file_path):
                        config_filename = os.path.basename(config_file_path)
                        config_backup_path = os.path.join(backtest_dir, f"full_{config_filename}")
                        shutil.copy2(config_file_path, config_backup_path)
                        if self.trader_callback:
                            self.trader_callback.gui.log_message(f"完整配置文件已保存: full_{config_filename}", "INFO")
                except Exception as e:
                    if self.trader_callback:
                        self.trader_callback.gui.log_message(f"保存完整配置文件时出错: {str(e)}", "ERROR")
                    logging.error(f"保存完整配置文件时出错: {str(e)}", exc_info=True)
                
                # 保存回测配置信息
                config_info = {
                    'start_time': self.backtest_records['start_time'],
                    'end_time': self.backtest_records['end_time'],
                    'init_capital': self.backtest_records['init_capital'],
                    'benchmark': self.config.config_dict["backtest"]["benchmark"],
                    'strategy_file': self.config.config_dict.get("strategy_file", ""),  # 从配置字典中获取策略文件路径
                    'actual_start_time': datetime.datetime.fromtimestamp(self.start_time).strftime("%Y-%m-%d %H:%M:%S") if self.start_time else "",
                    'actual_end_time': datetime.datetime.fromtimestamp(self.end_time).strftime("%Y-%m-%d %H:%M:%S") if self.end_time else "",
                    'total_runtime_seconds': self.total_runtime,
                    'total_runtime_formatted': self._format_runtime(self.total_runtime),
                    # 保存股票池信息
                    'stock_list': ','.join(self.get_stock_list()) if hasattr(self, 'get_stock_list') else '',
                    'min_volume': self.config.config_dict["backtest"].get("min_volume", 100),
                    'kline_period': self.config.config_dict["data"].get("kline_period", "1m"),
                    'dividend_type': self.config.config_dict["data"].get("dividend_type", "front")
                }
                _safe_to_csv(pd.DataFrame([config_info]), os.path.join(backtest_dir, "config.csv"), "配置数据")
                
                if self.trader_callback:
                    self.trader_callback.gui.log_message(
                        f"回测记录已保存到目录: {backtest_dir}", 
                        "INFO"
                    )
                    # 记录回测总耗时
                    self.trader_callback.gui.log_message(
                        f"回测总耗时: {self._format_runtime(self.total_runtime)}",
                        "INFO"
                    )
                    # 只有在GUI模式下（gui是QObject）才调用显示结果窗口
                    if isinstance(self.trader_callback.gui, QObject):
                        QMetaObject.invokeMethod(
                            self.trader_callback.gui,
                            "show_backtest_result",
                            Qt.QueuedConnection,
                            Q_ARG(str, backtest_dir)
                        )
                
            except Exception as e:
                if self.trader_callback:
                    self.trader_callback.gui.log_message(f"保存回测记录时出错: {str(e)}", "ERROR")
                logging.error(f"保存回测记录时出错: {str(e)}", exc_info=True)
                
        except Exception as e:
            error_msg = "回测运行异常: " + str(e)
            logging.error(error_msg, exc_info=True)
            # 调用错误回调函数
            if self.trader_callback:
                self.trader_callback.gui.log_message(error_msg, "ERROR")
                import traceback
                self.trader_callback.gui.log_message(f"错误详情:\n{traceback.format_exc()}", "ERROR")
            raise  # 重新抛出异常

    def record_results(self, timestamp, data, signals):
        """记录回测结果
        
        Args:
            timestamp: 当前时间戳
            data: 当前市场数据
            signals: 交易信号列表
        """
        try:
            # 获取当前时间信息
            current_time_info = data.get("__current_time__", {})
            current_ts = current_time_info.get("timestamp", timestamp)
            current_datetime = current_time_info.get("datetime", "")
            current_date = current_time_info.get("date", "")
            current_time = current_time_info.get("time", "")
            
            # 检查是否是交易日
            is_trading_day = self.tools.is_trade_day(current_date)
            if not is_trading_day:
                # 如果不是交易日，则跳过策略调用
                if self.trader_callback and self._should_log():
                    self.trader_callback.gui.log_message(f"日期 {current_date} 不是交易日，跳过策略执行", "INFO")
                return
            
            # 记录交易信号
            if signals:
                for signal in signals:
                    if 'action' not in signal or 'code' not in signal:
                        continue
                        
                    # 记录时间戳
                    timestamp_ms = signal.get('timestamp', current_ts)
                    # 如果时间戳是秒级，转换为毫秒级
                    if timestamp_ms < 1e10:
                        timestamp_ms *= 1000
            
            # 1. 时间戳处理优化 - 使用缓存和类型检查优化
            if isinstance(timestamp, str):
                if hasattr(self, '_cached_timestamp') and self._cached_timestamp.get('str') == timestamp:
                    current_time = self._cached_timestamp.get('datetime')
                    current_date = self._cached_timestamp.get('date')
                    current_ts_seconds = self._cached_timestamp.get('ts_seconds')
                else:
                    current_time = datetime.datetime.strptime(timestamp, "%Y%m%d%H%M%S")
                    current_date = current_time.date()
                    current_ts_seconds = current_time.timestamp()
                    self._cached_timestamp = {
                        'str': timestamp,
                        'datetime': current_time,
                        'date': current_date,
                        'ts_seconds': current_ts_seconds
                    }
            else:
                # 数字时间戳处理
                ts_float = float(timestamp)
                # 统一转换为秒级时间戳
                ts_seconds = ts_float / 1000 if ts_float > 1e10 else ts_float
                
                # 使用缓存检查是否与上次时间戳相近（避免重复转换相近时间戳）
                if hasattr(self, '_cached_timestamp') and abs(self._cached_timestamp.get('ts_seconds', 0) - ts_seconds) < 0.1:
                    current_time = self._cached_timestamp.get('datetime')
                    current_date = self._cached_timestamp.get('date')
                    current_ts_seconds = self._cached_timestamp.get('ts_seconds')
                else:
                    current_time = datetime.datetime.fromtimestamp(ts_seconds)
                    current_date = current_time.date()
                    current_ts_seconds = ts_seconds
                    self._cached_timestamp = {
                        'ts_seconds': ts_seconds,
                        'datetime': current_time,
                        'date': current_date
                    }
            
            # 2. 交易日检查优化 - 使用缓存避免重复查询
            cache_key = f"trade_day_{current_date}"
            if hasattr(self, '_cached_trade_days') and cache_key in self._cached_trade_days:
                is_trading_day = self._cached_trade_days[cache_key]
            else:
                if not hasattr(self, '_cached_trade_days'):
                    self._cached_trade_days = {}
                    
                try:
                    # 使用KhQuTools的is_trade_day方法进行统一的交易日判断
                    date_str = current_date.strftime("%Y-%m-%d")
                    is_trading_day = self.tools.is_trade_day(date_str)
                    # 缓存结果
                    self._cached_trade_days[cache_key] = is_trading_day
                except Exception as e:
                    logging.warning(f"检查交易日失败: {str(e)}")
                    is_trading_day = True  # 出错默认为交易日
                    
            # 3. 持仓更新优化 - 预先获取并缓存持仓列表
            positions = self.trade_mgr.positions
            position_codes = list(positions.keys())
            
            # 4. 非交易日处理优化
            if not is_trading_day:
                # 非交易日情况下，不更新持仓市值
                # 只记录每日统计数据，使用前一个交易日的市值数据
                total_market_value = 0.0
                for code, position in positions.items():
                    # 使用已记录的市值，不从当天数据获取
                    if 'market_value' in position and position['market_value'] > 0:
                        total_market_value += position['market_value']
            else:
                # 5. 交易日市值计算优化 - 批量获取价格并一次性更新
                # 预先创建价格字典并批量填充
                prices = {}
                
                # 一次性从数据中提取所有价格
                for code in position_codes:
                    # 使用条件短路避免不必要的检查
                    # 先检查lastPrice判断是否是tick数据（tick数据的close字段值为nan）
                    if code in data:
                        if 'lastPrice' in data[code]:
                            # Tick数据：优先使用lastPrice字段
                            prices[code] = data[code]['lastPrice']
                        elif 'close' in data[code]:
                            # K线数据：使用close字段
                            prices[code] = data[code]['close']
                        elif code in positions and 'current_price' in positions[code] and positions[code]['current_price'] > 0:
                            prices[code] = positions[code]['current_price']
                        elif code in positions and 'avg_price' in positions[code]:
                            prices[code] = positions[code]['avg_price']
                    elif code in positions and 'current_price' in positions[code] and positions[code]['current_price'] > 0:
                        prices[code] = positions[code]['current_price']
                    elif code in positions and 'avg_price' in positions[code]:
                        prices[code] = positions[code]['avg_price']
                
                # 一次性计算所有持仓的市值和盈亏
                total_market_value = 0.0
                for code in position_codes:
                    if code in prices:
                        position = positions[code]
                        current_price = prices[code]
                        volume = position['volume']
                        avg_price = position['avg_price']
                        
                        # 计算市值和盈亏
                        market_value = current_price * volume
                        position['market_value'] = market_value
                        position['current_price'] = current_price
                        position['profit'] = (current_price - avg_price) * volume
                        position['profit_ratio'] = (current_price - avg_price) / avg_price if avg_price != 0 else 0
                        
                        total_market_value += market_value
            
            # 6. 资产更新优化
            assets = self.trade_mgr.assets
            old_total_asset = assets.get('total_asset', 0)
            old_market_value = assets.get('market_value', 0)
            
            # 非交易日且没有交易信号时，市值保持不变
            if not is_trading_day and not signals and old_market_value > 0:
                total_market_value = old_market_value
            
            # 更新资产信息
            assets['market_value'] = total_market_value
            assets['total_asset'] = assets['cash'] + total_market_value
            
            # 只在资产变化显著时触发回调，减少不必要的回调
            if abs(assets['total_asset'] - old_total_asset) > 0.01 and self.trader_callback:
                self.trader_callback.on_stock_asset(SimpleNamespace(**assets))
            
            # 7. 交易信号处理优化
            if signals:
                trade_mgr = self.trade_mgr
                # 预先创建信号记录列表以避免多次append
                signal_records = []
                
                # 提前获取资产数据
                total_asset = assets['total_asset']
                cash = assets['cash']
                market_value = assets['market_value']
                
                # 使用列表推导式批量处理信号
                self.backtest_records['trades'].extend([
                    {
                        'datetime': current_time,
                        'code': signal['code'],
                        'action': signal['action'],
                        'price': signal.get('actual_price', signal['price']),
                        'volume': signal['volume'],
                        'amount': signal.get('actual_price', signal['price']) * signal['volume'],
                        'commission': (
                            signal.get('trade_cost', 0) * (trade_mgr.calculate_commission(signal.get('actual_price', signal['price']), signal['volume']) /
                            (trade_mgr.calculate_commission(signal.get('actual_price', signal['price']), signal['volume']) +
                            trade_mgr.calculate_stamp_tax(signal.get('actual_price', signal['price']), signal['volume'], signal['action']) +
                            trade_mgr.calculate_transfer_fee(signal['code'], signal.get('actual_price', signal['price']), signal['volume']) +
                            trade_mgr.calculate_flow_fee()))
                            if 'trade_cost' in signal else
                            trade_mgr.calculate_commission(signal.get('actual_price', signal['price']), signal['volume'])
                        ),
                        'stamp_tax': (
                            signal.get('trade_cost', 0) * (trade_mgr.calculate_stamp_tax(signal.get('actual_price', signal['price']), signal['volume'], signal['action']) /
                            (trade_mgr.calculate_commission(signal.get('actual_price', signal['price']), signal['volume']) +
                            trade_mgr.calculate_stamp_tax(signal.get('actual_price', signal['price']), signal['volume'], signal['action']) +
                            trade_mgr.calculate_transfer_fee(signal['code'], signal.get('actual_price', signal['price']), signal['volume']) +
                            trade_mgr.calculate_flow_fee()))
                            if 'trade_cost' in signal and signal['action'] == 'sell' else
                            trade_mgr.calculate_stamp_tax(signal.get('actual_price', signal['price']), signal['volume'], signal['action'])
                        ),
                        'transfer_fee': (
                            signal.get('trade_cost', 0) * (trade_mgr.calculate_transfer_fee(signal['code'], signal.get('actual_price', signal['price']), signal['volume']) /
                            (trade_mgr.calculate_commission(signal.get('actual_price', signal['price']), signal['volume']) +
                            trade_mgr.calculate_stamp_tax(signal.get('actual_price', signal['price']), signal['volume'], signal['action']) +
                            trade_mgr.calculate_transfer_fee(signal['code'], signal.get('actual_price', signal['price']), signal['volume']) +
                            trade_mgr.calculate_flow_fee()))
                            if 'trade_cost' in signal and signal['code'].startswith("sh.") else
                            trade_mgr.calculate_transfer_fee(signal['code'], signal.get('actual_price', signal['price']), signal['volume'])
                        ),
                        'flow_fee': (
                            signal.get('trade_cost', 0) * (trade_mgr.calculate_flow_fee() /
                            (trade_mgr.calculate_commission(signal.get('actual_price', signal['price']), signal['volume']) +
                            trade_mgr.calculate_stamp_tax(signal.get('actual_price', signal['price']), signal['volume'], signal['action']) +
                            trade_mgr.calculate_transfer_fee(signal['code'], signal.get('actual_price', signal['price']), signal['volume']) +
                            trade_mgr.calculate_flow_fee()))
                            if 'trade_cost' in signal else
                            trade_mgr.calculate_flow_fee()
                        ),
                        'total_asset': total_asset,
                        'cash': cash,
                        'market_value': market_value
                    }
                    for signal in signals
                ])
            
            # 8. 最后时间点判断优化
            # 使用函数字典替代if-else判断
            is_last_time_point = False
            
            if isinstance(self.trigger, CustomTimeTrigger):
                # 对于自定义时间触发，使用缓存优化
                trigger_seconds = self.trigger.trigger_seconds
                if trigger_seconds:
                    # 缓存当天的触发时间点
                    cache_key = f"time_points_{current_date}"
                    if not hasattr(self, '_cached_time_points') or cache_key not in self._cached_time_points:
                        if not hasattr(self, '_cached_time_points'):
                            self._cached_time_points = {}
                            
                        # 获取当天所有触发时间点并缓存
                        today_times = []
                        max_trigger_second = max(trigger_seconds)
                        
                        # 使用列表推导式优化循环
                        today_times = [
                            int(datetime.datetime.combine(current_date, datetime.time(
                                seconds // 3600,
                                (seconds % 3600) // 60,
                                seconds % 60
                            )).timestamp())
                            for seconds in trigger_seconds
                        ]
                        
                        # 缓存计算结果
                        self._cached_time_points[cache_key] = {
                            'times': sorted(today_times),
                            'max_second': max_trigger_second
                        }
                    
                    # 使用缓存数据
                    max_trigger_second = self._cached_time_points[cache_key]['max_second']
                    
                    # 计算当前时间点的秒数(使用已有变量避免重复计算)
                    current_ts_dt = current_time
                    current_seconds = current_ts_dt.hour * 3600 + current_ts_dt.minute * 60 + current_ts_dt.second
                    
                    # 检查是否是当天最后一个触发点
                    is_last_time_point = abs(current_seconds - max_trigger_second) < 0.1
            else:
                # 非自定义时间触发，使用all_times缓存优化
                cache_key = f"daily_times_{current_date}"
                if not hasattr(self, '_cached_daily_times') or cache_key not in self._cached_daily_times:
                    if not hasattr(self, '_cached_daily_times'):
                        self._cached_daily_times = {}
                        
                    # 获取当天的时间点，使用生成器表达式优化
                    today_times = []
                    
                    # 使用列表推导式和异常处理优化
                    try:
                        today_times = [
                            t for t in self.all_times
                            if datetime.datetime.fromtimestamp(
                                float(t) / 1000 if float(t) > 1e10 else float(t)
                            ).date() == current_date
                        ]
                    except Exception:
                        # 出错时使用传统循环方式作为备选
                        for t in self.all_times:
                            try:
                                t_float = float(t)
                                t_seconds = t_float / 1000 if t_float > 1e10 else t_float
                                t_time = datetime.datetime.fromtimestamp(t_seconds)
                                
                                if t_time.date() == current_date:
                                    today_times.append(t)
                            except Exception:
                                continue
                    
                    # 缓存结果
                    self._cached_daily_times[cache_key] = sorted(today_times)
                
                # 使用缓存的时间点
                today_times = self._cached_daily_times[cache_key]
                
                # 检查是否是最后时间点
                if today_times:
                    # 直接使用数值比较，避免创建新的datetime对象
                    last_time = float(today_times[-1])
                    last_time_seconds = last_time / 1000 if last_time > 1e10 else last_time
                    is_last_time_point = abs(last_time_seconds - current_ts_seconds) < 0.1
            
            # 9. 每日统计记录优化 - 只在最后时间点记录
            if is_last_time_point and is_trading_day:
                self._record_daily_stats(current_date, current_time, data)
            
        except Exception as e:
            if self.trader_callback:
                self.trader_callback.gui.log_message(f"记录回测结果时出错: {str(e)}", "ERROR")
            logging.error(f"记录回测结果时出错: {str(e)}", exc_info=True)
    
    def _record_daily_stats(self, current_date, current_time, data):
        """记录每日统计数据（从record_results中分离出来的功能）
        
        Args:
            current_date: 当前日期
            current_time: 当前时间对象
            data: 市场数据
        """
        # 获取必要的资产数据
        assets = self.trade_mgr.assets
        cash = assets['cash']
        
        # 确保日期是字符串格式
        date_str = current_date
        if not isinstance(current_date, str):
            try:
                date_str = current_date.strftime("%Y-%m-%d") if hasattr(current_date, 'strftime') else str(current_date)
            except:
                date_str = str(current_date)
        
        # 重新计算一天结束时的市值
        positions = self.trade_mgr.positions
        position_codes = list(positions.keys())
        day_end_market_value = 0.0
        
        # 转换日期为YYYYMMDD格式，用于获取日线数据
        yyyymmdd_date = date_str.replace('-', '') if '-' in date_str else date_str
        
        # 批量获取收盘价
        daily_prices = {}
        if position_codes:
            # 检查缓存中是否已有当日数据
            cache_date_key = f"daily_prices_{yyyymmdd_date}"
            if cache_date_key in self.daily_price_cache:
                # 直接使用缓存中的数据
                daily_prices = self.daily_price_cache[cache_date_key]
                if self.trader_callback:
                    self.trader_callback.gui.log_message(f"使用缓存的日线数据，日期: {yyyymmdd_date}", "INFO")
            else:
                try:
                    # 一次性获取所有持仓股票的日线数据
                    daily_data = xtdata.get_market_data(
                        field_list=['close'],
                        stock_list=position_codes,
                        period='1d',
                        start_time=yyyymmdd_date,
                        end_time=yyyymmdd_date,
                        # 与回测数据保持一致的复权方式，避免"下单用复权价、估值用未复权价"的不一致
                        dividend_type=self.config.config_dict["data"].get("dividend_type", "none")
                    )
                    
                    # 优化数据提取逻辑
                    if daily_data is not None and isinstance(daily_data, dict) and 'close' in daily_data:
                        close_data = daily_data['close']
                        # 检查close_data的类型
                        if isinstance(close_data, pd.DataFrame):
                            # 使用向量化操作处理DataFrame
                            if any(code in close_data.index for code in position_codes):
                                latest_date = close_data.columns[-1]
                                # 使用向量化操作获取所有股票的价格
                                valid_codes = [code for code in position_codes if code in close_data.index]
                                daily_prices.update({
                                    code: close_data.loc[code, latest_date]
                                    for code in valid_codes
                                    if close_data.loc[code, latest_date] is not None and close_data.loc[code, latest_date] > 0
                                })
                    
                    # 缓存获取的数据，避免同一天重复请求
                    self.daily_price_cache[cache_date_key] = daily_prices
                except Exception as e:
                    logging.error(f"获取日线数据失败: {e}")
        
        # 批量计算持仓市值
        for code in position_codes:
            # 优先使用日线收盘价
            if code in daily_prices and daily_prices[code] > 0:
                current_price = daily_prices[code]
            # 备选方案：使用触发数据中的价格
            # 先检查lastPrice判断是否是tick数据（tick数据的close字段值为nan）
            elif code in data and 'lastPrice' in data[code]:
                # Tick数据：优先使用lastPrice字段
                current_price = data[code]['lastPrice']
            elif code in data and 'close' in data[code]:
                # K线数据：使用close字段
                current_price = data[code]['close']
            # 备选方案：使用持仓记录的价格
            elif 'current_price' in positions[code] and positions[code]['current_price'] > 0:
                current_price = positions[code]['current_price']
            # 最后备选：使用持仓均价
            else:
                current_price = positions[code]['avg_price']
            
            # 计算市值
            volume = positions[code]['volume']
            market_value = current_price * volume
            day_end_market_value += market_value
            
            # 更新持仓信息
            positions[code]['current_price'] = current_price
            positions[code]['market_value'] = market_value
            
            # 计算盈亏
            avg_price = positions[code]['avg_price']
            positions[code]['profit'] = (current_price - avg_price) * volume
            positions[code]['profit_ratio'] = (current_price - avg_price) / avg_price if avg_price > 0 else 0
        
        # 计算总资产
        total_asset = cash + day_end_market_value
        
        # 获取基准指数收盘价 - 使用缓存优化
        benchmark_code = self.config.config_dict["backtest"]["benchmark"]
        benchmark_close = None
        
        # 使用缓存避免重复获取基准数据
        cache_key = f"benchmark_{yyyymmdd_date}_{benchmark_code}"
        if cache_key in self._cached_benchmark_close:
            benchmark_close = self._cached_benchmark_close[cache_key]
        else:
            try:
                # 备选：使用触发数据中的价格
                # 先检查lastPrice判断是否是tick数据（tick数据的close字段值为nan）
                if benchmark_code in data:
                    if 'lastPrice' in data[benchmark_code]:
                        # Tick数据：优先使用lastPrice字段
                        benchmark_close = data[benchmark_code]['lastPrice']
                        # 缓存结果
                        self._cached_benchmark_close[cache_key] = benchmark_close
                    elif 'close' in data[benchmark_code]:
                        # K线数据：使用close字段
                        benchmark_close = data[benchmark_code]['close']
                        # 缓存结果
                        self._cached_benchmark_close[cache_key] = benchmark_close
            except Exception as e:
                logging.error(f"获取基准指数数据失败: {e}")
                # 备选方案：先检查lastPrice判断是否是tick数据
                if benchmark_code in data:
                    if 'lastPrice' in data[benchmark_code]:
                        benchmark_close = data[benchmark_code]['lastPrice']
                    elif 'close' in data[benchmark_code]:
                        benchmark_close = data[benchmark_code]['close']
        
        # 计算当日收益率
        daily_stats = self.backtest_records['daily_stats']
        if daily_stats:
            prev_asset = daily_stats[-1]['total_asset']
            daily_return = (total_asset - prev_asset) / prev_asset if prev_asset != 0 else 0
        else:
            init_capital = self.backtest_records['init_capital']
            daily_return = (total_asset - init_capital) / init_capital if init_capital != 0 else 0
        
        # 创建持仓快照
        positions_snapshot = {
            code: {
                'volume': pos['volume'],
                'price': pos['current_price'],
                'avg_price': pos['avg_price'],
                'market_value': pos['market_value'],
                'profit': pos['profit'],
                'profit_ratio': pos['profit_ratio']
            }
            for code, pos in self.trade_mgr.positions.items()
        }
        
        # 记录每日统计数据
        daily_stat = {
            'date': current_date,
            'total_asset': total_asset,
            'cash': cash,
            'market_value': day_end_market_value,
            'daily_return': daily_return,
            'benchmark_close': benchmark_close,
            'positions': positions_snapshot
        }
        self.backtest_records['daily_stats'].append(daily_stat)
        
        # 记录基准指数数据
        if benchmark_close is not None:
            self.backtest_records['benchmark_data'].append({
                'date': current_date,
                'close': benchmark_close
            })
        
        # 输出日志（性能优化：检查是否需要输出）
        if self.trader_callback and self._should_log():
            decimals = self.price_decimals
            self.trader_callback.gui.log_message(
                f"每日统计 - 日期: {daily_stat['date']} | "
                f"总资产: {total_asset:.{decimals}f} | "
                f"日收益率: {daily_return*100:.2f}% | "
                f"持仓市值: {day_end_market_value:.{decimals}f} | "
                f"可用资金: {cash:.{decimals}f}",
                "INFO"
            )

    def _run_simulate(self):
        """模拟模式"""
        # 模拟相关逻辑实现
        pass



    def stop(self):
        """停止框架"""
        self.is_running = False
        
        # 记录结束时间（如果还没有记录的话）
        if self.end_time is None:
            self.end_time = time.time()
            if self.start_time is not None:
                self.total_runtime = self.end_time - self.start_time
                
                # 记录停止日志
                if self.trader_callback:
                    end_datetime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.trader_callback.gui.log_message(f"策略手动停止时间: {end_datetime}", "INFO")
                    self.trader_callback.gui.log_message(f"策略总运行时长: {self._format_runtime(self.total_runtime)}", "INFO")
        
        if self.trader:
            self.trader.stop()
            
    def check_connection(self) -> bool:
        """检查连接状态"""
        # 实现连接检查逻辑
        pass
        
    def reconnect(self):
        """重新连接"""
        try:
            self.init_trader_and_account()
        except Exception as e:
            self.log_error(f"重连失败: {str(e)}")
            
    def log_error(self, msg: str):
        """错误日志"""
        print(f"[ERROR] {datetime.datetime.now()} - {msg}")

    def _format_runtime(self, seconds):
        """格式化运行时间"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        seconds = int(seconds % 60)
        return f"{hours}小时{minutes}分钟{seconds}秒"
    
    def on_stock_position(self, position):
        """持仓变动回调"""
        try:
            if self.trader_callback and hasattr(self.trader_callback, 'gui'):
                decimals = self.price_decimals
                position_msg = (
                    f"持仓变动 - "
                    f"股票代码: {position.stock_code} | "
                    f"持仓数量: {position.volume} | "
                    f"可用数量: {position.can_use_volume} | "
                    f"持仓均价: {position.avg_price:.{decimals}f} | "
                    f"市值: {position.market_value:.{decimals}f}"
                )
                self.trader_callback.gui.log_message(position_msg, "TRADE")
            print(f"持仓变动回调: {position.stock_code}")
        except Exception as e:
            print(f"处理持仓变动回调时出错: {str(e)}")
    
    def on_order_error(self, error):
        """委托错误回调"""
        try:
            if self.trader_callback and hasattr(self.trader_callback, 'gui'):
                error_msg = (
                    f"委托错误 - "
                    f"股票代码: {error.stock_code} | "
                    f"错误代码: {error.error_id} | "
                    f"错误信息: {error.error_msg} | "
                    f"备注: {error.order_remark}"
                )
                self.trader_callback.gui.log_message(error_msg, "ERROR")
            print(f"[ERROR] 委托错误: {error.error_msg}")
        except Exception as e:
            print(f"处理委托错误回调时出错: {str(e)}")
    
    def on_stock_order(self, order):
        """委托回报回调"""
        try:
            if self.trader_callback and hasattr(self.trader_callback, 'gui'):
                order_msg = (
                    f"委托回报 - "
                    f"股票代码: {order.stock_code} | "
                    f"委托编号: {getattr(order, 'order_id', 'N/A')} | "
                    f"状态: {getattr(order, 'order_status', 'N/A')} | "
                    f"委托价格: {getattr(order, 'price', 'N/A')} | "
                    f"委托数量: {getattr(order, 'order_volume', 'N/A')}"
                )
                self.trader_callback.gui.log_message(order_msg, "TRADE")
            print(f"委托回报: {order.stock_code}")
        except Exception as e:
            print(f"处理委托回报时出错: {str(e)}")
    
    def on_stock_trade(self, trade):
        """成交回报回调"""
        try:
            if self.trader_callback and hasattr(self.trader_callback, 'gui'):
                trade_msg = (
                    f"成交回报 - "
                    f"股票代码: {trade.stock_code} | "
                    f"成交价格: {getattr(trade, 'traded_price', 'N/A')} | "
                    f"成交数量: {getattr(trade, 'traded_volume', 'N/A')} | "
                    f"成交金额: {getattr(trade, 'traded_amount', 'N/A')}"
                )
                self.trader_callback.gui.log_message(trade_msg, "TRADE")
            print(f"成交回报: {trade.stock_code}")
        except Exception as e:
            print(f"处理成交回报时出错: {str(e)}")
    
    def on_stock_asset(self, asset):
        """资产变动回调"""
        try:
            if self.trader_callback and hasattr(self.trader_callback, 'gui'):
                asset_msg = (
                    f"资产变动 - "
                    f"总资产: {getattr(asset, 'total_asset', 'N/A')} | "
                    f"现金: {getattr(asset, 'cash', 'N/A')} | "
                    f"市值: {getattr(asset, 'market_value', 'N/A')}"
                )
                self.trader_callback.gui.log_message(asset_msg, "INFO")
            print(f"资产变动: 总资产={getattr(asset, 'total_asset', 'N/A')}")
        except Exception as e:
            print(f"处理资产变动时出错: {str(e)}")