# -*- coding: utf-8 -*-
"""
策略名称：经典网格交易策略（基础版）
策略逻辑：
    1. 首次运行时，以当前价格为基准建立底仓
    2. 价格每下跌一个网格，买入固定金额
    3. 价格每上涨一个网格，卖出固定金额
    4. 每次成交后更新锚点价格
"""

from khQuantImport import *
import math
import random

# ==================== 策略参数（随机初始化） ====================
params = {
    'init_position': random.uniform(0.4, 0.6),      # 初始建仓比例：在一定范围内随机，如需固定参数，将随机函数替换为具体数值
    'grid_gap': random.uniform(0.005, 0.012),       # 网格间距：在一定范围内随机，如需固定参数，将随机函数替换为具体数值
    'trade_amount': random.randint(30000, 50000),   # 每次交易金额：在一定范围内随机，如需固定参数，将随机函数替换为具体数值
}

# ==================== 全局状态变量 ====================
g_state = {
    'anchor_price': 0.0,          # 锚点价格
    'initialized': False,         # 是否已完成初始建仓
}


def init(stock_list, context):
    """策略初始化"""
    global g_state
    g_state = {'anchor_price': 0.0, 'initialized': False}
    logging.info("=== 经典网格交易策略启动 ===")
    logging.info(f"参数: 网格间距={params['grid_gap']*100}%, 每次交易={params['trade_amount']}元")


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

        # 获取当前持仓
        pos_vol = 0
        positions = khGet(data, 'positions')
        if positions and stock in positions:
            pos_vol = positions[stock].get('volume', 0)

        pos_value = pos_vol * price  # 持仓市值

        # ========== 2. 初始建仓 ==========
        if not g_state['initialized']:
            target_value = total_asset * params['init_position']
            buy_vol = math.floor(target_value / price / 100) * 100

            if buy_vol > 0:
                logging.info(f"🎯 初始建仓 | 价格: {price:.2f} | 数量: {buy_vol}股")
                sigs = generate_signal(data, stock, price, float(buy_vol), 'buy', "初始建仓")
                signals.extend(sigs)

            g_state['anchor_price'] = price
            g_state['initialized'] = True
            return signals

        # ========== 3. 网格交易逻辑 ==========
        anchor = g_state['anchor_price']
        grid_gap = params['grid_gap']
        trade_amount = params['trade_amount']

        # 计算买入/卖出触发价
        buy_trigger = anchor * (1 - grid_gap)
        sell_trigger = anchor * (1 + grid_gap)

        # --- 触发买入：价格跌破下网格 ---
        if price <= buy_trigger:
            buy_vol = math.floor(trade_amount / price / 100) * 100
            if buy_vol > 0:
                logging.info(f"📥 网格买入 | 价格: {price:.2f} (锚点: {anchor:.2f}) | 数量: {buy_vol}股")
                sigs = generate_signal(data, stock, price, float(buy_vol), 'buy', "网格买入")
                signals.extend(sigs)
                g_state['anchor_price'] = price  # 更新锚点

        # --- 触发卖出：价格涨破上网格 ---
        elif price >= sell_trigger:
            if pos_vol > 0:
                # 计算卖出数量
                sell_vol = math.floor(trade_amount / price / 100) * 100
                sell_vol = min(sell_vol, pos_vol)  # 不能超过持仓

                if sell_vol > 0:
                    sell_ratio = sell_vol / pos_vol  # 转换为持仓比例
                    logging.info(f"📤 网格卖出 | 价格: {price:.2f} (锚点: {anchor:.2f}) | 数量: {sell_vol}股")
                    sigs = generate_signal(data, stock, price, sell_ratio, 'sell', "网格卖出")
                    signals.extend(sigs)
                    g_state['anchor_price'] = price  # 更新锚点

    except Exception as e:
        logging.error(f"策略异常: {e}")

    return signals