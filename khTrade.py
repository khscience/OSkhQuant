# coding: utf-8
from typing import Dict, List, Optional
import datetime
from types import SimpleNamespace

from xtquant.xttrader import XtQuantTraderCallback
from xtquant import xtconstant

class KhTradeManager:
    """交易管理类"""
    
    def __init__(self, config, callback=None):
        self.config = config
        self.callback = callback  # 保存回调对象
        self.orders = {}  # 订单管理
        self.assets = {}  # 资产管理
        self.trades = {}  # 成交管理
        self.positions = {}  # 持仓管理
        
        # 获取交易成本配置
        trade_cost = self.config.config_dict.get("backtest", {}).get("trade_cost", {})
        
        # 设置交易成本参数
        self.min_commission = trade_cost.get("min_commission", 5.0)  # 最低佣金（元）
        self.commission_rate = trade_cost.get("commission_rate", 0.0003)  # 佣金比例
        self.stamp_tax_rate = trade_cost.get("stamp_tax_rate", 0.001)  # 卖出印花税！
        self.flow_fee = trade_cost.get("flow_fee", 0.1)  # 流量费（元），默认0.1元/笔
        
        # 设置滑点参数，支持两种模式：
        # 1. tick模式：按最小变动价跳数计算，如tick_size=0.01表示最小变动价为1分钱，tick_count=2表示跳2个最小单位（即0.02元）
        # 2. ratio模式：按比例计算，如ratio=0.001表示0.1%的滑点（买入时上浮0.1%，卖出时下调0.1%）
        self.slippage = trade_cost.get("slippage", {
            "type": "ratio",  # 默认使用比例模式
            "tick_size": 0.01,  # A股最小变动价（1分钱）
            "tick_count": 2,  # 默认跳数为2，即买入时上浮0.02元，卖出时下调0.02元
            "ratio": 0.001  # 默认滑点比例0.1%
        })
        
        # 价格精度设置（小数位数），默认为2（股票），ETF为3
        self.price_decimals = 2
        
        # T+0交易模式标识（默认关闭）
        self.t0_mode = False
    
    def set_price_decimals(self, decimals: int):
        """设置价格精度
        
        Args:
            decimals: 小数位数，股票为2，ETF为3
        """
        self.price_decimals = decimals
    
    def set_t0_mode(self, enabled: bool):
        """设置T+0交易模式
        
        Args:
            enabled: True启用T+0模式（当天买入可当天卖出），False使用T+1模式
        """
        self.t0_mode = enabled
        if enabled:
            print("T+0交易模式已启用：当天买入的股票可当天卖出")
        else:
            print("T+1交易模式：当天买入的股票需等下一交易日才能卖出")

    def init(self):
        """初始化交易管理"""
        # 初始化逻辑可以放在这里
        print("交易管理初始化完成")
        print(f"交易成本设置:")
        print(f"  最低佣金: {self.min_commission}元")
        print(f"  佣金比例: {self.commission_rate*100}%")
        print(f"  印花税率: {self.stamp_tax_rate*100}%")
        print(f"  流量费: {self.flow_fee}元/笔")
        print(f"  滑点类型: {self.slippage['type']}")
        if self.slippage['type'] == 'tick':
            print(f"  最小变动价: {self.slippage['tick_size']}元（{self.slippage['tick_count']}跳）")
            print(f"  实际滑点值: {self.slippage['tick_size'] * self.slippage['tick_count']}元")
        else:
            print(f"  滑点比例: {self.slippage['ratio']*100}%")
        
    def calculate_slippage(self, price, direction):
        """
        计算滑点后的价格
        
        Args:
            price: float, 原始价格
            direction: str, 交易方向 'buy' 或 'sell'
            
        Returns:
            float: 考虑滑点后的价格
        """
        slippage_type = self.slippage["type"]
        decimals = self.price_decimals  # 使用动态精度
        
        if slippage_type == "tick":
            # 按最小变动价跳数计算
            tick_size = self.slippage["tick_size"]  # 最小变动价
            tick_count = self.slippage["tick_count"]  # 跳数
            slippage = tick_size * tick_count
            
            if direction == "buy":
                return round(price + slippage, decimals)
            else:  # sell
                return round(price - slippage, decimals)
                
        elif slippage_type == "ratio":
            # 按可变滑点百分比计算
            ratio = self.slippage["ratio"] / 2  # 滑点比例除以2
            
            if direction == "buy":
                return round(price * (1 + ratio), decimals)
            else:  # sell
                return round(price * (1 - ratio), decimals)
        
        return round(price, decimals)  # 如果没有设置滑点，返回原价格

    def calculate_commission(self, price, volume):
        """计算佣金"""
        # 如果数量为0，不收取佣金
        if volume <= 0:
            return 0.0
            
        commission = price * volume * self.commission_rate
        if commission < self.min_commission:
            commission = self.min_commission
        return commission

    def calculate_stamp_tax(self, price, volume, direction):
        """计算印花税"""
        # 如果数量为0，不收取印花税
        if volume <= 0:
            return 0.0
            
        if direction == "sell":
            return price * volume * self.stamp_tax_rate
        return 0.0

    def calculate_transfer_fee(self, stock_code, price, volume):
        """计算过户费（仅沪市股票收取）
        
        Args:
            stock_code: str, 股票代码
            price: float, 交易价格
            volume: int, 交易数量
            
        Returns:
            float: 过户费金额
        """
        # 如果数量为0，不收取过户费
        if volume <= 0:
            return 0.0
            
        if stock_code.startswith("sh."):
            return price * volume * 0.00001  # 成交金额的0.001%
        return 0.0

    def calculate_flow_fee(self):
        """计算流量费（每笔交易固定收取）"""
        return self.flow_fee

    def calculate_trade_cost(self, price, volume, direction, stock_code):
        """
        计算交易成本
        
        Args:
            price: float, 交易价格
            volume: int, 交易数量
            direction: str, 交易方向 'buy' 或 'sell'
            stock_code: str, 股票代码
            
        Returns:
            tuple: (实际成交价格, 总交易成本)
        """
        # 如果数量为0，不产生交易成本
        if volume <= 0:
            return price, 0.0
            
        # 计算滑点后的价格
        actual_price = self.calculate_slippage(price, direction)
        
        # 计算佣金
        commission = self.calculate_commission(actual_price, volume)
        
        # 计算印花税（只收取卖出印花税）
        stamp_tax = self.calculate_stamp_tax(actual_price, volume, direction)
        
        # 计算过户费（沪市股票）
        transfer_fee = self.calculate_transfer_fee(stock_code, actual_price, volume)
        
        # 计算流量费（每笔交易固定收取）
        flow_fee = self.calculate_flow_fee()
        
        # 总交易成本
        total_cost = commission + stamp_tax + transfer_fee + flow_fee
        
        return actual_price, total_cost

    def process_signals(self, signals: List[Dict]):
        """处理交易信号
        
        Args:
            signals: 交易信号列表，每个信号字典包含以下字段：
            {
                "code": str,       # 股票代码
                "action": str,     # 交易动作，可选值："buy"(买入) | "sell"(卖出)
                "price": float,    # 委托价格
                "volume": int,     # 委托数量，单位：股
                "reason": str,     # 交易原因说明
                "order_type": str, # 可选，委托类型，默认为"limit"：
                                  # "limit"(限价) | "market"(市价) | "best"(最优价)
                "position_type": str,  # 可选，持仓方向，默认为"long"：
                                      # "long"(多头) | "short"(空头)
                "order_time": str, # 可选，委托时间，格式"HH:MM:SS"
                "remark": str      # 可选，备注信息
            }
        """
        for signal in signals:
            # 跳过数量为0的交易信号
            if signal["volume"] <= 0:
                error_msg = f"交易数量为0或负数，忽略交易信号 - 股票: {signal['code']}, 方向: {signal['action']}, 数量: {signal['volume']}"
                print(f"[WARNING] {error_msg}")
                if self.callback:
                    self.callback.gui.log_message(error_msg, "WARNING")
                continue
                
            # 计算交易成本
            direction = "buy" if signal["action"].lower() == "buy" else "sell"
            actual_price, trade_cost = self.calculate_trade_cost(
                signal["price"],
                signal["volume"],
                direction,
                signal["code"]
            )
            
            # 添加交易成本信息
            signal["trade_cost"] = trade_cost
            signal["actual_price"] = actual_price
            
            # 执行下单
            self.place_order(signal)
            
    def place_order(self, signal: Dict):
        """下单
        
        Args:
            signal: 交易信号
        """
        # 根据运行模式选择不同的下单逻辑
        if self.config.run_mode == "live":
            self._place_order_live(signal)
        elif self.config.run_mode == "simulate":
            self._place_order_simulate(signal)
        else:
            self._place_order_backtest(signal)
        
    def _place_order_live(self, signal: Dict):
        """实盘下单逻辑"""
        # 调用miniQMT的交易接口
        print(f"实盘下单信号: {signal}")
        # 这里需要调用实际的交易接口
        
    def _place_order_simulate(self, signal: Dict):
        """模拟下单逻辑"""
        # 模拟下单逻辑
        print(f"模拟下单信号: {signal}")
        # 更新模拟数据字典
        self.update_dic(signal)
        
    def _place_order_backtest(self, signal: Dict):
        """回测下单逻辑"""
        try:
            # 生成订单ID
            order_id = len(self.orders) + 1
            
            # -- 提前计算交易成本和实际价格 --
            actual_price, trade_cost = self.calculate_trade_cost(
                signal["price"],
                signal["volume"],
                signal["action"],
                signal["code"]
            )
            
            # 计算买入所需的总资金（包括交易成本）
            if signal["action"] == "buy":
                required_cash = actual_price * signal["volume"] + trade_cost
            
            # 买入时检查资金是否足够 (使用所需总资金进行检查)
            if signal["action"] == "buy":
                if self.assets["cash"] < required_cash: # 使用 required_cash 进行比较
                    decimals = self.price_decimals
                    error_msg = (
                        f"资金不足 - "
                        f"所需资金: {required_cash:.{decimals}f} (含成本:{trade_cost:.{decimals}f}) | "
                        f"可用资金: {self.assets['cash']:.{decimals}f}"
                    )
                    # 记录错误信息到日志
                    print(f"[ERROR] {error_msg}")
                    if self.callback:
                        self.callback.gui.log_message(error_msg, "ERROR")
                        # 触发委托错误回调
                        self.callback.on_order_error(SimpleNamespace(
                            stock_code=signal["code"],
                            error_id=-1, # 自定义错误代码，表示资金不足
                            error_msg=error_msg,
                            order_remark=signal.get("remark", "资金不足")
                        ))
                    return  # 资金不足，立即返回，不执行后续交易操作
            
            # 卖出时检查持仓是否足够
            elif signal["action"] == "sell":
                # 获取可用持仓，如果股票不在持仓中，则可用为0
                available_volume = self.positions.get(signal["code"], {}).get('can_use_volume', 0)
                if available_volume < signal["volume"]:
                    error_msg = f"可用持仓不足 - 需要: {signal['volume']}股, 可用: {available_volume}股"
                    # 记录错误信息到日志
                    print(f"[ERROR] {error_msg}")
                    if self.callback:
                        self.callback.gui.log_message(error_msg, "ERROR")
                        # 触发委托错误回调
                        self.callback.on_order_error(SimpleNamespace(
                            stock_code=signal["code"],
                            error_id=-2, # 自定义错误代码，表示持仓不足
                            error_msg=error_msg,
                            order_remark=signal.get("remark", "持仓不足")
                        ))
                    return  # 持仓不足，立即返回，不执行后续交易操作
            
            # -- 资金/持仓检查通过后，继续执行交易 --
            
            # 创建委托订单 (使用原始信号价格作为委托价)
            decimals = self.price_decimals
            order = {
                "account_type": xtconstant.SECURITY_ACCOUNT,
                "account_id": self.config.account_id,
                "stock_code": signal["code"],
                "order_id": order_id,
                "order_sysid": str(order_id),  # 模拟柜台编号
                "order_time": signal.get("timestamp", int(datetime.datetime.now().timestamp())), # 使用回测时间戳
                "order_type": xtconstant.STOCK_BUY if signal["action"] == "buy" else xtconstant.STOCK_SELL,
                "order_volume": signal["volume"],
                "price_type": xtconstant.FIX_PRICE,  # 默认限价单
                "price": round(signal["price"], decimals), # 委托价格使用信号中的价格
                "traded_volume": signal["volume"],  # 回测假设全部成交
                "traded_price": round(actual_price, decimals), # 成交价格使用计算出的实际价格
                "order_status": xtconstant.ORDER_SUCCEEDED,  # 回测假设立即成交
                "status_msg": signal.get("reason", "策略交易"),
                "strategy_name": signal.get("strategy_name", "backtest"),
                "order_remark": signal.get("remark", ""),
                "direction": xtconstant.DIRECTION_FLAG_LONG,  # 股票默认多头
                "offset_flag": xtconstant.OFFSET_FLAG_OPEN if signal["action"] == "buy" else xtconstant.OFFSET_FLAG_CLOSE
            }
            
            # 更新委托字典
            self.orders[order_id] = order
            
            # 创建成交记录
            trade = {
                "account_type": xtconstant.SECURITY_ACCOUNT,
                "account_id": self.config.account_id,
                "stock_code": signal["code"],
                "order_type": order["order_type"],
                "traded_id": f"T{order_id}",
                "traded_time": order["order_time"],  # 使用相同的时间戳
                "traded_price": round(actual_price, decimals),  # 使用考虑了滑点的实际价格
                "traded_volume": signal["volume"],
                "traded_amount": round(actual_price * signal["volume"], decimals),  # 使用实际价格计算成交金额
                "order_id": order_id,
                "order_sysid": order["order_sysid"],
                "strategy_name": order["strategy_name"],
                "order_remark": order["order_remark"],
                "direction": order["direction"],
                "offset_flag": order["offset_flag"]
            }
            
            # 更新成交字典
            self.trades[trade["traded_id"]] = trade
            
            # 更新资产
            if signal["action"] == "buy":
                # 买入：减少现金 (减少的是 required_cash，包含了成本)
                self.assets["cash"] -= required_cash
                # 注意：回测中冻结资金和在途资金通常不模拟，简化处理
                # self.assets["frozen_cash"] += actual_price * signal["volume"]
                # self.assets["market_value"] += actual_price * signal["volume"] # 市值更新在record_results中处理
                
                # 更新或创建持仓
                if signal["code"] not in self.positions:
                    # T+0模式下当天买入可当天卖出，T+1模式下当天买入不可卖
                    can_use_vol = signal["volume"] if self.t0_mode else 0
                    self.positions[signal["code"]] = {
                        "account_type": xtconstant.SECURITY_ACCOUNT,
                        "account_id": self.config.account_id,
                        "stock_code": signal["code"],
                        "volume": signal["volume"],
                        "can_use_volume": can_use_vol,
                        "open_price": round(actual_price, decimals), # 记录开仓时的实际成交价
                        "market_value": round(actual_price * signal["volume"], decimals), # 初始市值
                        "frozen_volume": 0,
                        "on_road_volume": 0,
                        "yesterday_volume": 0,
                        "avg_price": round(actual_price, decimals), # 初始持仓均价
                        "current_price": round(actual_price, decimals), # 当前价格
                        "direction": xtconstant.DIRECTION_FLAG_LONG
                    }
                    # 新建仓位时触发持仓变动回调
                    if self.callback:
                        self.callback.on_stock_position(SimpleNamespace(**self.positions[signal["code"]]))
                else:
                    pos = self.positions[signal["code"]]
                    old_volume = pos["volume"]
                    # 计算新的持仓均价
                    total_cost_value = pos["avg_price"] * pos["volume"] + actual_price * signal["volume"] # 注意：这里用的是成交金额，不是包含费用的成本
                    total_volume = pos["volume"] + signal["volume"]
                    pos["avg_price"] = round(total_cost_value / total_volume if total_volume > 0 else 0, decimals)
                    pos["volume"] += signal["volume"]
                    # T+0模式下当天买入可当天卖出，T+1模式下当天买入不可卖
                    if self.t0_mode:
                        pos["can_use_volume"] += signal["volume"]  # T+0：买入即可卖出
                    # T+1模式下不增加can_use_volume，需等待框架在下一交易日更新
                    pos["market_value"] = round(pos["volume"] * actual_price, decimals) # 更新市值
                    pos["current_price"] = round(actual_price, decimals) # 更新当前价
                    
                    # 持仓数量变化时触发回调
                    if pos["volume"] != old_volume and self.callback:
                        self.callback.on_stock_position(SimpleNamespace(**pos))
                    
            else:  # sell
                # 卖出：增加现金 (增加的是成交金额减去交易成本)
                cash_increase = actual_price * signal["volume"] - trade_cost
                self.assets["cash"] += cash_increase
                # self.assets["market_value"] -= actual_price * signal["volume"] # 市值更新在record_results中处理
                
                # 更新持仓
                pos = self.positions[signal["code"]]
                old_volume = pos["volume"]
                pos["volume"] -= signal["volume"]
                pos["can_use_volume"] -= signal["volume"] # 可用数量减少
                # pos["market_value"] = pos["volume"] * actual_price # 更新市值
                pos["current_price"] = round(actual_price, decimals) # 更新当前价
                
                # 持仓数量变化时触发回调
                if pos["volume"] != old_volume and self.callback:
                     # 如果持仓清零，也需要触发回调
                    if pos["volume"] == 0:
                         # 创建一个代表已清仓状态的持仓对象
                        cleared_position = pos.copy()
                        cleared_position['volume'] = 0
                        cleared_position['can_use_volume'] = 0
                        cleared_position['market_value'] = 0
                        self.callback.on_stock_position(SimpleNamespace(**cleared_position))
                    else:
                        self.callback.on_stock_position(SimpleNamespace(**pos))

                # 如果持仓为0，删除持仓记录
                if pos["volume"] == 0:
                    del self.positions[signal["code"]]
            
            # 更新总资产 (总资产 = 现金 + 持仓市值)
            # 持仓市值会在 record_results 中根据最新价格更新，这里暂时不计算以避免重复
            # self.assets["total_asset"] = self.assets["cash"] + self.assets["market_value"]
            # 仅在成交回报后，让 record_results 去计算最新的总资产
            
            # 输出交易成本信息到GUI日志
            if self.callback:
                commission = self.calculate_commission(actual_price, signal["volume"])
                stamp_tax = self.calculate_stamp_tax(actual_price, signal["volume"], signal["action"])
                transfer_fee = self.calculate_transfer_fee(signal["code"], actual_price, signal["volume"])
                flow_fee = self.calculate_flow_fee()
                
                cost_msg = (
                    f"交易成本 - "
                    f"股票代码: {signal['code']} | "
                    f"交易方向: {'买入' if signal['action'] == 'buy' else '卖出'} | "
                    f"成交数量: {signal['volume']} | "
                    f"成交价格: {actual_price:.{decimals}f} | "
                    f"交易金额: {actual_price * signal['volume']:.{decimals}f} | "
                    f"佣金: {commission:.2f} | "
                    f"印花税: {stamp_tax:.2f} | "
                    f"过户费: {transfer_fee:.2f} | "
                    f"流量费: {flow_fee:.2f} | "
                    f"总成本: {trade_cost:.2f}"
                )
                self.callback.gui.log_message(cost_msg, "TRADE")
            
            print(f"回测下单完成: {signal}")
            print(f"交易成本: {trade_cost:.2f}")
            print(f"当前资产 (现金): {self.assets['cash']:.{decimals}f}") # 只打印现金，总资产依赖市值
            print(f"当前持仓: {self.positions}")
            
            # 触发回调 (委托和成交)
            if self.callback:
                # 使用SimpleNamespace包装字典，模拟对象属性访问
                self.callback.on_stock_order(SimpleNamespace(**order))
                self.callback.on_stock_trade(SimpleNamespace(**trade))
                # 资产和持仓回调在资产/持仓实际变化时触发
                
        except Exception as e:
            print(f"回测下单异常: {str(e)}")
            if self.callback:
                # 触发委托错误回调
                self.callback.on_order_error(SimpleNamespace(
                    stock_code=signal["code"],
                    error_id=-99, # 通用错误代码
                    error_msg=f"下单执行异常: {str(e)}",
                    order_remark=signal.get("remark", "")
                ))
        
    def update_dic(self, signal: Dict):
        """更新数据字典"""
        # 更新资产、委托、成交和持仓数据字典
        print(f"更新数据字典: {signal}")
        
    def on_order(self, order):
        """委托回报处理"""
        print(f"委托回报: {order}")
        self.orders[order.order_id] = order
        
    def on_trade(self, trade):
        """成交回报处理"""
        print(f"成交回报: {trade}")
        self.trades[trade.trade_id] = trade
        
    def on_order_error(self, error):
        """委托错误处理"""
        print(f"[ERROR] Order Error: {error.error_msg}")
        
    def on_cancel_error(self, cancel_error):
        """撤单错误处理"""
        print(f"[ERROR] Cancel Error: {cancel_error.error_msg}")
        
    def on_order_stock_async_response(self, response):
        """异步下单回报处理"""
        print(f"异步下单回报: {response}")

    def process_trade_signal(self, signal):
        """处理交易信号"""
        try:
            # ... 现有的交易处理代码 ...
            
            # 创建委托对象并触发回调
            order = {
                "account_type": xtconstant.SECURITY_ACCOUNT,
                "account_id": self.config.account_id,
                "stock_code": signal["code"],
                "order_type": signal["order_type"],
                "order_id": order_id,
                "order_time": signal["time"],
                "price": signal["price"],
                "order_volume": signal["volume"],
                "order_status": "FILLED",  # 回测模式下假设立即成交
                "order_direction": "STOCK_BUY" if signal["action"] == "buy" else "STOCK_SELL",
                "strategy_name": signal["strategy_name"],
                "order_remark": signal["remark"]
            }
            
            # 触发委托回调
            if self.callback:
                self.callback.on_stock_order(SimpleNamespace(**order))
            
            # 创建成交对象并触发回调
            decimals = self.price_decimals
            trade = {
                "account_type": xtconstant.SECURITY_ACCOUNT,
                "account_id": self.config.account_id,
                "stock_code": signal["code"],
                "trade_id": f"T{order_id}",
                "order_id": order_id,
                "price": round(signal.get("actual_price", signal["price"]), decimals),  # 优先使用实际成交价格
                "volume": signal["volume"],
                "turnover": round(signal.get("actual_price", signal["price"]) * signal["volume"], decimals),  # 使用实际价格计算成交金额
                "order_direction": "STOCK_BUY" if signal["action"] == "buy" else "STOCK_SELL",
                "order_remark": signal["remark"]
            }
            
            # 触发成交回调
            if self.callback:
                self.callback.on_stock_trade(SimpleNamespace(**trade))
                
            # 更新资产后触发资产变动回调
            if self.callback:
                self.callback.on_stock_asset(SimpleNamespace(**self.assets))
                
            # 如果持仓发生变化，触发持仓变动回调
            if signal["code"] in self.positions:
                self.callback.on_stock_position(SimpleNamespace(**self.positions[signal["code"]]))
                
        except Exception as e:
            print(f"处理交易信号时出错: {str(e)}")
            if self.callback:
                self.callback.on_order_error(SimpleNamespace(
                    stock_code=signal["code"],
                    error_id=-1,
                    error_msg=str(e),
                    order_remark=signal.get("remark", "")
                ))