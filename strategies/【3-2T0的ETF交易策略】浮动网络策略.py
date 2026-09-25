# -*- coding: utf-8 -*-
"""
策略名称：浮动网格交易策略（ETF专用）
策略逻辑：
    1. 首次运行时，以当前价格为基准建立底仓，并将锚点设在当前价格
    2. 以浮动锚点为中心，向上 grid_gap 为卖出触发线，向下 grid_gap 为买入触发线
    3. 价格跌破买入触发线 → 买入 trade_amount 元，并将锚点漂移到成交价
    4. 价格突破卖出触发线 → 卖出 trade_amount 元，并将锚点漂移到成交价
    5. 锚点每次成交后跟随价格移动（追踪式浮动网格）
注意：
    - 适用于 ETF（无印花税、可 T+0）
    - 与固定网格不同：锚点会漂移，每次来回只跨越 1 个网格间距，利润约为固定网格的一半
    - 趋势行情中比固定网格更不易被单边耗尽（锚点跟随价格移动）
"""

from khQuantImport import *
import math
import random

# ==================== 策略参数 ====================
# 参数加入了随机性，避免直接构成投资建议
params = {
    'init_position': round(random.uniform(0.3, 0.7), 2),    # 初始建仓比例：在一定范围内随机
    'grid_gap': round(random.uniform(0.005, 0.03), 4),      # 网格间距：在一定范围内随机
    'trade_amount': random.choice([10000, 20000, 30000, 40000, 50000]),   # 每次交易金额：在一定范围内随机
}

# ==================== 全局状态变量 ====================
g_state = {
    'anchor_price': 0.0,      # 基准价格（每次成交后漂移到新价格）
    'initialized': False,
}


def init(stock_list, context):
    """策略初始化"""
    global g_state
    g_state = {'anchor_price': 0.0, 'initialized': False}
    logging.info("=== 浮动网格交易策略启动 ===")
    logging.info(f"参数: 网格间距={params['grid_gap']*100:.2f}%, 每次交易={params['trade_amount']}元, 底仓比例={params['init_position']*100:.0f}%")


def khHandlebar(data: Dict) -> List[Dict]:
    """策略主函数，每个交易周期调用一次"""
    global g_state
    signals = []

    try:
        # ========== 1. 获取基础数据 ==========
        stock = khGet(data, 'first_stock')
        if not stock:
            return signals

        price = khPrice(data, stock, 'close')
        if price <= 0:
            return signals

        total_asset = khGet(data, 'total_asset') or 1000000.0

        pos_vol = 0
        positions = khGet(data, 'positions')
        if positions and stock in positions:
            pos_vol = positions[stock].get('volume', 0)

        pos_value = pos_vol * price

        # ========== 2. 初始建仓，确定初始锚点 ==========
        if not g_state['initialized']:
            target_value = total_asset * params['init_position']
            buy_vol = math.floor(target_value / price / 100) * 100

            if buy_vol > 0:
                logging.info(f"[初始建仓] 价格: {price:.3f} | 数量: {buy_vol}股 | 锚点设在: {price:.3f}")
                sigs = generate_signal(data, stock, price, float(buy_vol), 'buy', "初始建仓")
                signals.extend(sigs)

            g_state['anchor_price'] = price
            g_state['initialized'] = True
            return signals

        # ========== 3. 浮动网格交易逻辑 ==========
        anchor = g_state['anchor_price']
        grid_gap = params['grid_gap']
        trade_amount = params['trade_amount']

        buy_trigger = anchor * (1 - grid_gap)
        sell_trigger = anchor * (1 + grid_gap)

        # 向下穿越买入线
        if price <= buy_trigger:
            buy_vol = math.floor(trade_amount / price / 100) * 100
            if buy_vol > 0:
                logging.info(
                    f"[网格买入] 现价: {price:.3f} | 触发价: {buy_trigger:.3f} | 锚点: {anchor:.3f} → {price:.3f}"
                )
                sigs = generate_signal(data, stock, price, float(buy_vol), 'buy', "网格买入")
                signals.extend(sigs)
                g_state['anchor_price'] = price   # 成交后锚点漂移到新价格

        # 向上穿越卖出线
        elif price >= sell_trigger:
            if pos_vol > 0:
                sell_ratio = min(trade_amount / pos_value, 1.0)
                logging.info(
                    f"[网格卖出] 现价: {price:.3f} | 触发价: {sell_trigger:.3f} | 锚点: {anchor:.3f} → {price:.3f} | 卖出比例: {sell_ratio:.4f}"
                )
                sigs = generate_signal(data, stock, price, sell_ratio, 'sell', "网格卖出")
                signals.extend(sigs)
                g_state['anchor_price'] = price   # 成交后锚点漂移到新价格

    except Exception as e:
        logging.error(f"策略异常: {e}")

    return signals
