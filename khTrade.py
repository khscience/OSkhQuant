# coding: utf-8
from typing import Dict, List, Optional
import datetime
import logging
import math
from types import SimpleNamespace
import os

from security_type_utils import is_listed_fund_code

# 开源版不依赖 xtquant：委托/成交/持仓记录沿用 xtconstant 的真实数值
import kh_constants as xtconstant

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
        config_dict = getattr(self.config, "config_dict", {})
        if not isinstance(config_dict, dict):
            config_dict = {}
        backtest_config = config_dict.get("backtest", {})
        if not isinstance(backtest_config, dict):
            backtest_config = {}
        trade_cost = backtest_config.get("trade_cost", {})
        if not isinstance(trade_cost, dict):
            trade_cost = {}

        def safe_number(key, default, lower=0.0, upper=None):
            try:
                value = float(trade_cost.get(key, default))
            except (TypeError, ValueError):
                value = default
            if (
                not math.isfinite(value)
                or value < lower
                or (upper is not None and value > upper)
            ):
                value = default
            return value
        
        # 设置交易成本参数
        self.min_commission = safe_number("min_commission", 5.0)
        self.commission_rate = safe_number("commission_rate", 0.0003, upper=1.0)
        self.stamp_tax_rate = safe_number("stamp_tax_rate", 0.001, upper=1.0)
        # fixed 保持旧配置语义；a_share_legal 按 A 股法定调整日期分段计算。
        # 默认仍是 fixed，因此不会改变既有策略和金标准结果。
        self.stamp_tax_mode = str(trade_cost.get("stamp_tax_mode", "fixed") or "fixed").strip().lower()
        self.flow_fee = safe_number("flow_fee", 0.1, upper=100.0)
        # 实盘和普通回测默认沿用法定过户费。研究型回测可以通过
        # ``transfer_fee_enabled: false`` 显式构造完全零费用口径。
        self.transfer_fee_enabled = bool(trade_cost.get("transfer_fee_enabled", True))
        # 费率按成交日期硬编码（见 _transfer_fee_by_date）。current_backtest_date 由框架每根 bar 喂入。
        self.current_backtest_date = None  # 当前回测成交日期(YYYY-MM-DD)，供过户费按日期分段

        # 设置滑点参数，支持两种模式：
        # 1. tick模式：按最小变动价跳数计算，如tick_size=0.01表示最小变动价为1分钱，tick_count=2表示跳2个最小单位（即0.02元）
        # 2. ratio模式：ratio 是双边总滑点；如 0.001 表示总计0.1%，买卖单边各0.05%。
        raw_slippage = trade_cost.get("slippage", {})
        if not isinstance(raw_slippage, dict):
            raw_slippage = {}
        slippage_type = raw_slippage.get("type", "ratio")
        if slippage_type not in {"tick", "ratio"}:
            slippage_type = "ratio"

        def safe_slippage_number(key, default, lower=0.0, upper=None):
            try:
                value = float(raw_slippage.get(key, default))
            except (TypeError, ValueError):
                value = default
            if (
                not math.isfinite(value)
                or value < lower
                or (upper is not None and value > upper)
            ):
                value = default
            return value

        tick_count_value = safe_slippage_number("tick_count", 2, upper=100)
        if not tick_count_value.is_integer():
            tick_count_value = 2.0
        self.slippage = {
            "type": slippage_type,
            "tick_size": safe_slippage_number(
                "tick_size", 0.01, lower=1e-12, upper=100
            ),
            "tick_count": int(tick_count_value),
            "ratio": safe_slippage_number("ratio", 0.001, upper=0.1),
        }
        
        # 价格精度设置（小数位数），默认为2（股票），ETF为3
        self.price_decimals = 2

        # T+0交易模式标识（默认关闭）
        self.t0_mode = False

        # 撮合引擎配置（用于涨跌停校验等）
        self.match_config = {}
    
    def set_price_decimals(self, decimals: int):
        """设置价格精度
        
        Args:
            decimals: 小数位数，股票为2，ETF为3
        """
        self.price_decimals = decimals

    @staticmethod
    def _is_etf_security(stock_code) -> bool:
        """兼容旧方法名：识别免股票税费且采用三位报价的场内基金。"""
        return bool(stock_code and is_listed_fund_code(stock_code))

    def _price_decimals_for_code(self, stock_code=None) -> int:
        """返回单只证券的报价精度；无代码时保持旧的全局配置语义。"""
        if not stock_code:
            return self.price_decimals
        if self._is_etf_security(stock_code):
            return 3

        code = str(stock_code).upper()
        compact = code.replace("SH.", "").replace("SZ.", "").replace("BJ.", "")
        compact = compact.split(".", 1)[0]
        if (
            code.endswith((".SH", ".SZ", ".BJ"))
            or code.startswith(("SH.", "SZ.", "BJ."))
            or (len(compact) == 6 and compact.isdigit())
        ):
            return 2
        return self.price_decimals
    
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

    def set_match_config(self, config: dict):
        """设置撮合引擎配置

        Args:
            config: 撮合引擎配置字典，包含涨跌停、价格校验、成交量限制等设置
        """
        self.match_config = config

    def _get_limit_rate(self, code: str, market_data: dict = None) -> float:
        """根据股票代码和名称判断涨跌停幅度，支持ST股票5%

        Args:
            code: 股票代码
            market_data: 市场数据（可选，用于获取股票名称判断ST）

        Returns:
            涨跌停幅度（如0.10表示10%）
        """
        code_prefix = code[:3] if len(code) >= 3 else ""
        code_suffix = code[-2:].upper() if len(code) >= 2 else ""

        # 科创板：20%
        if code_prefix in ["688", "689"]:
            return 0.20
        # 创业板：20%
        if code_prefix in ["300", "301"]:
            return 0.20
        # 北交所：30%
        if code_prefix in ["43", "83", "87"] or code_suffix == "BJ":
            return 0.30

        # ST股票：5%（通过股票名称判断）
        if market_data:
            stock_name = market_data.get("stockName", market_data.get("stock_name", ""))
            if stock_name and ("ST" in stock_name.upper()):
                return 0.05

        # 主板/中小板：10%
        return 0.10

    def _check_price_limit(self, signal: dict, market_data: dict) -> tuple:
        """涨跌停校验

        Args:
            signal: 交易信号
            market_data: 当前市场数据（包含preClose, close等字段）

        Returns:
            (是否可交易, 拒绝原因)
        """
        if market_data is None:
            return True, ""

        code = signal["code"]
        action = signal["action"]

        # 获取昨收价
        pre_close = market_data.get("preClose", market_data.get("pre_close", 0))
        if pre_close <= 0:
            return True, ""

        # 获取当前价
        current_price = market_data.get("close", market_data.get("lastPrice", 0))
        if current_price <= 0:
            return True, ""

        # 计算涨跌停价
        limit_rate = self._get_limit_rate(code, market_data)
        limit_up = round(pre_close * (1 + limit_rate), 2)
        limit_down = round(pre_close * (1 - limit_rate), 2)
        limit_down = max(limit_down, 0.01)  # 跌停价最低0.01

        # 判断涨跌停状态（允许0.001的误差）
        is_limit_up = current_price >= limit_up - 0.001
        is_limit_down = current_price <= limit_down + 0.001

        # 检查一字板
        check_one_line = self.match_config.get("price_limit", {}).get("check_one_line", True)
        if check_one_line:
            bar_open = market_data.get("open", 0)
            bar_high = market_data.get("high", 0)
            bar_low = market_data.get("low", 0)

            if bar_open > 0 and bar_high > 0 and bar_low > 0:
                # 一字涨停：开盘=最高=最低=涨停价
                if (abs(bar_open - limit_up) < 0.01 and
                    abs(bar_high - limit_up) < 0.01 and
                    abs(bar_low - limit_up) < 0.01):
                    if action == "buy":
                        return False, f"一字涨停，无法买入 - 涨停价:{limit_up:.2f}"

                # 一字跌停：开盘=最高=最低=跌停价
                if (abs(bar_open - limit_down) < 0.01 and
                    abs(bar_high - limit_down) < 0.01 and
                    abs(bar_low - limit_down) < 0.01):
                    if action == "sell":
                        return False, f"一字跌停，无法卖出 - 跌停价:{limit_down:.2f}"

        # 普通涨跌停校验
        if action == "buy" and is_limit_up:
            return False, f"涨停无法买入 - 当前:{current_price:.2f} 涨停:{limit_up:.2f}"

        if action == "sell" and is_limit_down:
            return False, f"跌停无法卖出 - 当前:{current_price:.2f} 跌停:{limit_down:.2f}"

        return True, ""

    def _log_order_reject(self, signal: dict, reason: str):
        """记录订单拒绝日志

        Args:
            signal: 交易信号
            reason: 拒绝原因
        """
        msg = f"订单被拒绝 - 股票:{signal['code']}, 方向:{signal['action']}, 原因:{reason}"
        print(f"[WARNING] {msg}")
        if self.callback:
            logging.warning(msg)
            # 触发委托错误回调
            self.callback.on_order_error(SimpleNamespace(
                stock_code=signal["code"],
                error_id=-10,  # 撮合引擎拒绝
                error_msg=reason,
                order_remark=signal.get("remark", "")
            ))

    def execute_pending_fill(self, order: dict, market_data: dict):
        """执行挂单成交（供 khFrame 调用）

        Args:
            order: 挂单订单信息
            market_data: 当前市场数据字典
        """
        # 构造信号
        signal = {
            "code": order["code"],
            "action": order["action"],
            "price": order["fill_price"],
            "volume": order["remaining_volume"],
            "reason": f"{order.get('reason', '')} [挂单成交]",
            "remark": order.get("remark", ""),
            "timestamp": order.get("fill_timestamp", 0),
            "_pending_fill": True  # 标记为挂单成交，避免重复计算滑点
        }

        # 获取该股票的市场数据
        code_data = market_data.get(order["code"], {})
        if isinstance(code_data, dict):
            stock_data = code_data
        else:
            # 可能是 pandas Series
            stock_data = code_data.to_dict() if hasattr(code_data, 'to_dict') else {}

        # 调用现有下单逻辑
        return self._place_order_backtest(signal, stock_data)

        
    def calculate_slippage(self, price, direction, stock_code=None):
        """
        计算滑点后的价格
        
        Args:
            price: float, 原始价格
            direction: str, 交易方向 'buy' 或 'sell'
            stock_code: str, 可选证券代码；混合股票/ETF池按代码选择报价精度
            
        Returns:
            float: 考虑滑点后的价格
        """
        slippage_type = self.slippage["type"]
        decimals = self._price_decimals_for_code(stock_code)
        
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
            # 配置中的 ratio 表示买卖双边合计滑点，单边成交价各取一半。
            # 该历史口径已经进入冻结回归基线，界面也会明确提示“总滑点比例”。
            ratio = self.slippage["ratio"] / 2
            
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

    def calculate_stamp_tax(self, price, volume, direction, stock_code=None):
        """计算印花税；ETF/基金卖出免征，省略代码时保持旧行为。"""
        # 如果数量为0，不收取印花税
        if volume <= 0:
            return 0.0
            
        if direction == "sell":
            if stock_code and self._is_etf_security(stock_code):
                return 0.0
            rate = self.stamp_tax_rate
            if self.stamp_tax_mode == "a_share_legal":
                rate = self._a_share_stamp_tax_rate_by_date(self.current_backtest_date)
            return price * volume * rate
        return 0.0

    @staticmethod
    def _a_share_stamp_tax_rate_by_date(trade_date):
        """Return the seller-side A-share stamp-tax rate for modern backtests.

        A-share securities transaction stamp tax was reduced from 0.1% to
        0.05% on 2023-08-28.  The strategies using this mode start in 2020,
        so the modern two-stage schedule is sufficient and explicit.  When a
        date is unavailable, use the latest legal rate rather than silently
        falling back to the caller's fixed research rate.
        """
        date_text = str(trade_date or "").replace("-", "").replace("/", "")
        if len(date_text) >= 8 and date_text[:8] < "20230828":
            return 0.001
        return 0.0005

    @staticmethod
    def _transfer_fee_by_date(price, volume, trade_date, market):
        """A股过户费按"成交日期 × 市场"分段（调用前已判定非ETF）。中国结算法定值：
          沪市 SH : <2015-07-09 按成交面值 0.3‰(A股面值1元/股); 2015-07-09~2022-04-28 成交额 0.02‰; >=2022-04-29 成交额 0.01‰
          深市 SZ : <2015-07-09 按成交金额 0.0255‰;            2015-07-09~2022-04-28 成交额 0.02‰; >=2022-04-29 成交额 0.01‰
          北交所 BJ: <2022-04-29 按成交金额 0.025‰(北交所2021-11才开市);                       >=2022-04-29 成交额 0.01‰
        trade_date 形如 'YYYY-MM-DD' 或 'YYYYMMDD'；无日期信息时按最新(最低)费率。
        """
        d = str(trade_date or "").replace("-", "").replace("/", "")
        if len(d) < 8:
            d = "99999999"  # 无日期 → 按最新费率(0.01‰)，贴近当下
        amt = price * volume
        if d >= "20220429":
            return amt * 0.00001          # 沪深北统一 0.01‰（2022-04-29 减半）
        if market == "BJ":
            return amt * 0.000025         # 北交所 <2022-04-29 : 成交额 0.025‰
        if d >= "20150709":
            return amt * 0.00002          # 沪深 2015-07-09~2022-04-28 : 成交额 0.02‰
        if market == "SZ":
            return amt * 0.0000255        # 深市 <2015-07-09 : 成交额 0.0255‰
        return volume * 1.0 * 0.0003      # 沪市 <2015-07-09 : 成交面值 0.3‰（面值1元/股）

    def calculate_transfer_fee(self, stock_code, price, volume, trade_date=None):
        """计算过户费（沪/深/北市场股票均收取，ETF/基金豁免）。

        默认按中国结算法定值及"成交日期 × 市场"分段（见 _transfer_fee_by_date）。
        研究型零费用回测可在 ``backtest.trade_cost`` 中设置
        ``transfer_fee_enabled: false``；成交日期缺省取 self.current_backtest_date。

        Args:
            stock_code: str, 股票代码（兼容 "600000.SH" 后缀与 "sh.600000" 前缀两种格式）
            price: float, 交易价格
            volume: int, 交易数量
            trade_date: str, 成交日期(可选)，缺省用 self.current_backtest_date

        Returns:
            float: 过户费
        """
        # 零费用研究或数量为 0 时均不收取过户费。
        if not self.transfer_fee_enabled or volume <= 0:
            return 0.0

        # 判市场（兼容 "600000.SH" 后缀与 "sh.600000" 前缀两种格式）
        u = str(stock_code).upper()
        if u.endswith(".SH") or u.startswith("SH."):
            market = "SH"
        elif u.endswith(".SZ") or u.startswith("SZ."):
            market = "SZ"
        elif u.endswith(".BJ") or u.startswith("BJ."):
            market = "BJ"
        else:
            return 0.0  # 未知市场不收
        # ETF、LOF 等场内基金不按 A 股规则收取过户费。
        if is_listed_fund_code(stock_code):
            return 0.0
        d = trade_date if trade_date is not None else self.current_backtest_date
        return self._transfer_fee_by_date(price, volume, d, market)

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
        actual_price = self.calculate_slippage(price, direction, stock_code)
        
        # 计算佣金
        commission = self.calculate_commission(actual_price, volume)
        
        # 计算印花税（只收取卖出印花税）
        stamp_tax = self.calculate_stamp_tax(actual_price, volume, direction, stock_code)
        
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
                if self.callback:
                    logging.warning(error_msg)
                continue
                
            # 执行下单
            self.place_order(signal)
            
    def place_order(self, signal: Dict):
        """下单
        
        Args:
            signal: 交易信号
        """
        # 开源版只有回测
        return self._place_order_backtest(signal)
        
    def _place_order_backtest(self, signal: Dict, market_data: Dict = None):
        """回测下单逻辑

        Args:
            signal: 交易信号
            market_data: 可选的市场数据，用于涨跌停校验
        """
        try:
            # 涨跌停校验（仅在提供market_data且启用校验时执行）
            if market_data and self.match_config.get("price_limit", {}).get("enabled", False):
                can_trade, reason = self._check_price_limit(signal, market_data)
                if not can_trade:
                    self._log_order_reject(signal, reason)
                    return False

            # 生成订单ID
            order_id = len(self.orders) + 1

            # -- 提前计算交易成本和实际价格 --
            # 挂单成交时，fill_price已经是撮合引擎确定的最终成交价，不应再计算滑点
            is_pending_fill = signal.get("_pending_fill", False)

            if is_pending_fill:
                # 挂单成交：直接使用撮合价，只计算交易成本
                actual_price = signal["price"]
                decimals = self._price_decimals_for_code(signal["code"])

                # 计算交易成本（不含滑点）
                commission = self.calculate_commission(actual_price, signal["volume"])
                stamp_tax = self.calculate_stamp_tax(
                    actual_price,
                    signal["volume"],
                    signal["action"],
                    signal["code"],
                )
                transfer_fee = self.calculate_transfer_fee(signal["code"], actual_price, signal["volume"])
                flow_fee = self.calculate_flow_fee()
                trade_cost = commission + stamp_tax + transfer_fee + flow_fee
            else:
                # 普通下单：计算滑点和交易成本
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
                    decimals = self._price_decimals_for_code(signal["code"])
                    error_msg = (
                        f"资金不足 - "
                        f"所需资金: {required_cash:.{decimals}f} (含成本:{trade_cost:.{decimals}f}) | "
                        f"可用资金: {self.assets['cash']:.{decimals}f}"
                    )
                    logging.warning(error_msg)
                    # 实盘/模拟模式才触发委托错误回调
                    if self.callback and self.config.run_mode != "backtest":
                        self.callback.on_order_error(SimpleNamespace(
                            stock_code=signal["code"],
                            error_id=-1,
                            error_msg=error_msg,
                            order_remark=signal.get("remark", "资金不足")
                        ))
                    return False
            
            # 卖出时检查持仓是否足够
            elif signal["action"] == "sell":
                available_volume = self.positions.get(signal["code"], {}).get('can_use_volume', 0)
                if available_volume < signal["volume"]:
                    error_msg = f"可用持仓不足 - 需要: {signal['volume']}股, 可用: {available_volume}股"
                    logging.error(error_msg)
                    if self.callback and self.config.run_mode != "backtest":
                        self.callback.on_order_error(SimpleNamespace(
                            stock_code=signal["code"],
                            error_id=-2,
                            error_msg=error_msg,
                            order_remark=signal.get("remark", "持仓不足")
                        ))
                    return False  # 持仓不足，立即返回，不执行后续交易操作
            
            # -- 资金/持仓检查通过后，继续执行交易 --
            signal["trade_cost"] = trade_cost
            signal["actual_price"] = actual_price
            
            # 创建委托订单 (使用原始信号价格作为委托价)
            decimals = self._price_decimals_for_code(signal["code"])
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
                    # 新建仓位时触发持仓变动回调（回测模式跳过，数据已在 positions 中）
                    if self.callback and self.config.run_mode != "backtest":
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
                    
                    # 持仓数量变化时触发回调（回测模式跳过）
                    if pos["volume"] != old_volume and self.callback and self.config.run_mode != "backtest":
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
                
                # 持仓数量变化时触发回调（回测模式跳过）
                if pos["volume"] != old_volume and self.callback and self.config.run_mode != "backtest":
                    if pos["volume"] == 0:
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
            
            # 输出交易成本信息（回测模式下仅用 DEBUG 级别，避免大量 console flush 拖慢回测）
            if self.callback and logging.getLogger().isEnabledFor(logging.DEBUG):
                commission = self.calculate_commission(actual_price, signal["volume"])
                stamp_tax = self.calculate_stamp_tax(
                    actual_price,
                    signal["volume"],
                    signal["action"],
                    signal["code"],
                )
                transfer_fee = self.calculate_transfer_fee(signal["code"], actual_price, signal["volume"])
                flow_fee = self.calculate_flow_fee()
                cost_msg = (
                    f"交易成本 - {signal['code']} "
                    f"{'买入' if signal['action'] == 'buy' else '卖出'} "
                    f"{signal['volume']}股 @ {actual_price:.{decimals}f} | "
                    f"佣金:{commission:.2f} 印花税:{stamp_tax:.2f} "
                    f"过户费:{transfer_fee:.2f} 总成本:{trade_cost:.2f}"
                )
                logging.debug(cost_msg)
            
            # 累计成交笔数，供回测日志汇总用（避免逐笔 logging.info）
            self._daily_trade_count = getattr(self, '_daily_trade_count', 0) + 1
            if logging.getLogger().isEnabledFor(logging.DEBUG):
                action_str = "买入" if signal["action"] == "buy" else "卖出"
                logging.debug(
                    f"[成交] {signal['code']} {action_str} {signal['volume']}股 "
                    f"@ {actual_price:.{decimals}f} | 成本:{trade_cost:.2f} | "
                    f"可用资金:{self.assets['cash']:.2f}"
                )
            
            # 触发回调（回测模式跳过，数据已在 orders/trades 中）
            if self.callback and self.config.run_mode != "backtest":
                self.callback.on_stock_order(SimpleNamespace(**order))
                self.callback.on_stock_trade(SimpleNamespace(**trade))
            return True
                
        except Exception as e:
            logging.error(f"回测下单异常: {str(e)}")
            if self.callback:
                # 触发委托错误回调
                self.callback.on_order_error(SimpleNamespace(
                    stock_code=signal["code"],
                    error_id=-99, # 通用错误代码
                    error_msg=f"下单执行异常: {str(e)}",
                    order_remark=signal.get("remark", "")
                ))
            return False
        
        
    def on_order_error(self, error):
        """委托错误处理"""
        print(f"[ERROR] Order Error: {error.error_msg}")
        
    def on_cancel_error(self, cancel_error):
        """撤单错误处理"""
        print(f"[ERROR] Cancel Error: {cancel_error.error_msg}")
        

