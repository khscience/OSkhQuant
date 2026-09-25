# -*- coding: utf-8 -*-
"""
策略名称：跨层补单网格策略（ETF专用）
策略逻辑：
    基础逻辑与"固定网格策略"完全一致——锚点一次性确定后永不变动，
    以固定锚点为中心、按绝对等宽划分网格层级。

    唯一的改进点（跨层补单）：
        原固定网格：价格触发到新层级时，无论一根 K 线跨了多少层都只成交 1 笔。
        本策略：跨 N 层就补 N 笔（每个被跨过的层级都补一单），
                在 close 价以"单笔金额 × N"的总量一次性成交，
                经济效果等价于把"中间被略过的网格线"补全。
注意：
    - 适用于 ETF（无印花税、可 T+0）
    - 卖出受可用持仓限制：实际卖出量 = min(N × 单笔量, 可卖持仓)
    - 买入受可用现金限制：实际买入量 = min(N × 单笔量, 现金可买上限)
    - 在快速单边行情中，跨层补单能减少"漏单"，让网格成交更贴近理论密度
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
    logging.info("=== 跨层补单网格策略启动 ===")
    logging.info(f"参数: 网格间距={params['grid_gap']*100:.2f}%, 每次交易={params['trade_amount']}元, 底仓比例={params['init_position']*100:.0f}%")
    logging.info("改进点: 跨 N 层时一次性按 N × 单笔量成交（补齐中间被略过的网格线）")


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

        # ========== 3. 跨层补单网格逻辑 ==========
        anchor = g_state['anchor_price']
        grid_gap = params['grid_gap']
        trade_amount = params['trade_amount']
        grid_step = anchor * grid_gap
        base_level = g_state['base_grid_level']

        target_level = math.floor((price - anchor) / grid_step)

        # ===== 卖出（价格涨入更高层级）=====
        if target_level > base_level:
            cross_layers = target_level - base_level  # 跨越层数 N
            filled = False
            if pos_vol > 0:
                per_layer_sell_vol = math.floor(trade_amount / price / 100) * 100
                total_target_sell_vol = per_layer_sell_vol * cross_layers  # N × 单笔
                actual_sell_vol = min(total_target_sell_vol, pos_vol)
                sell_ratio = actual_sell_vol / pos_vol if pos_vol > 0 else 0.0
                if sell_ratio > 0:
                    reason = f"网格卖出 跨{cross_layers}层补单" if cross_layers > 1 else "网格卖出"
                    logging.info(
                        f"[{reason}] 现价: {price:.3f} | 层级: {base_level} → {target_level} | 卖出: {actual_sell_vol}股"
                    )
                    sigs = generate_signal(data, stock, price, sell_ratio, 'sell', reason)
                    if sigs:
                        signals.extend(sigs)
                        pos_vol -= actual_sell_vol
                        filled = True
            if filled:
                base_level = target_level

        # ===== 买入（价格跌入更低层级）=====
        elif target_level < base_level:
            cross_layers = base_level - target_level  # 跨越层数 N
            filled = False
            per_layer_buy_vol = math.floor(trade_amount / price / 100) * 100
            total_target_buy_vol = per_layer_buy_vol * cross_layers  # N × 单笔
            cash = khGet(data, 'cash') or 0
            if total_target_buy_vol > 0:
                max_affordable = math.floor(cash / price / 100) * 100
                actual_buy_vol = min(total_target_buy_vol, max_affordable)
                if actual_buy_vol > 0:
                    reason = f"网格买入 跨{cross_layers}层补单" if cross_layers > 1 else "网格买入"
                    logging.info(
                        f"[{reason}] 现价: {price:.3f} | 层级: {base_level} → {target_level} | 买入: {actual_buy_vol}股"
                    )
                    sigs = generate_signal(data, stock, price, float(actual_buy_vol), 'buy', reason)
                    if sigs:
                        signals.extend(sigs)
                        filled = True
            if filled:
                base_level = target_level

        g_state['base_grid_level'] = base_level

    except Exception as e:
        logging.error(f"策略异常: {e}")

    return signals
