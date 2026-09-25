# -*- coding: utf-8 -*-
"""
策略名称：固定网格交易策略（ETF专用）
策略逻辑：
    1. 首次运行时，以当前价格为基准建立底仓，同时确定固定锚点（永不移动）
    2. 以固定锚点为中心，向上/向下按 grid_gap 间距划分网格层级
    3. 价格向下穿越网格层级 → 买入 trade_amount 元
    4. 价格向上穿越网格层级 → 卖出 trade_amount 元
    5. 锚点始终固定在初始价格，网格线永不漂移
注意：
    - 适用于 ETF（无印花税、可 T+0）
    - 震荡行情表现最优；每次来回跨越 2 个网格间距，利润约为浮动网格的 2 倍
    - 单边行情中网格可能被单边耗尽（资金或持仓），需搭配仓位风控
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
    'anchor_price': 0.0,      # 基准价格（初始化后永不变动）
    'base_grid_level': 0,     # 当前基准网格层级（0=锚点，负数=下方，正数=上方）
    'initialized': False,
}


def init(stock_list, context):
    """策略初始化"""
    global g_state
    g_state = {'anchor_price': 0.0, 'base_grid_level': 0, 'initialized': False}
    logging.info("=== 固定网格交易策略启动 ===")
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

        # ========== 2. 初始建仓，确定固定锚点 ==========
        if not g_state['initialized']:
            target_value = total_asset * params['init_position']
            buy_vol = math.floor(target_value / price / 100) * 100

            if buy_vol > 0:
                logging.info(f"[初始建仓] 价格: {price:.3f} | 数量: {buy_vol}股 | 锚点固定在: {price:.3f}")
                sigs = generate_signal(data, stock, price, float(buy_vol), 'buy', "初始建仓")
                signals.extend(sigs)

            g_state['anchor_price'] = price   # 锚点一次性确定，后续永不修改
            g_state['base_grid_level'] = 0
            g_state['initialized'] = True
            return signals

        # ========== 3. 固定网格交易逻辑 ==========
        anchor = g_state['anchor_price']       # 固定基准，永不变
        grid_gap = params['grid_gap']
        trade_amount = params['trade_amount']
        grid_step = anchor * grid_gap
        base_level = g_state['base_grid_level']

        # 维护“当前基准网格”状态机：
        # 核心修复：分离买卖触发线，防止在同一个边界附近微小波动导致反复买卖
        sell_price_threshold = anchor + (base_level + 1) * grid_step
        buy_price_threshold = anchor + (base_level - 1) * grid_step

        # 向上穿越卖出线
        if price >= sell_price_threshold:
            target_level = math.floor((price - anchor) / grid_step)
            filled = False
            
            if pos_vol > 0:
                target_sell_vol = math.floor(trade_amount / price / 100) * 100
                actual_sell_vol = min(target_sell_vol, pos_vol)
                sell_ratio = actual_sell_vol / pos_vol if pos_vol > 0 else 0.0
                
                if sell_ratio > 0:
                    logging.info(
                        f"[网格卖出] 现价: {price:.3f} | 触发价: {sell_price_threshold:.3f} | 层级: {base_level} → {target_level} | 卖出: {actual_sell_vol}股"
                    )
                    sigs = generate_signal(data, stock, price, sell_ratio, 'sell', "网格卖出")
                    if sigs:
                        signals.extend(sigs)
                        pos_vol = pos_vol - actual_sell_vol
                        pos_value = pos_vol * price
                        filled = True
            else:
                logging.info(f"[空仓上行] 现价: {price:.3f} | 触发层级: {target_level} (无票可卖，锁定当前层级 {base_level})")
                filled = False  # 没票时不推进层级，原地等待价格跌回来

            if filled:
                base_level = target_level

        # 向下穿越买入线
        elif price <= buy_price_threshold:
            target_level = math.ceil((price - anchor) / grid_step)
            filled = False
            buy_vol = math.floor(trade_amount / price / 100) * 100
            
            cash = khGet(data, 'cash') or 0
            if buy_vol > 0 and cash >= buy_vol * price:
                logging.info(
                    f"[网格买入] 现价: {price:.3f} | 触发价: {buy_price_threshold:.3f} | 层级: {base_level} → {target_level}"
                )
                sigs = generate_signal(data, stock, price, float(buy_vol), 'buy', "网格买入")
                if sigs:
                    signals.extend(sigs)
                    cash -= buy_vol * price
                    filled = True
            else:
                logging.info(f"[满仓下行] 现价: {price:.3f} | 触发层级: {target_level} (现金不足，锁定当前层级 {base_level})")
                filled = False  # 没钱时不推进层级，原地等待价格涨回来

            if filled:
                base_level = target_level

        g_state['base_grid_level'] = base_level

    except Exception as e:
        logging.error(f"策略异常: {e}")

    return signals
