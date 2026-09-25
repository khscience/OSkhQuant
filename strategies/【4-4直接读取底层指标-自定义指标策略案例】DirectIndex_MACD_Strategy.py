import os
import random
import pandas as pd
from khQuantImport import *

# ==================== 策略参数 ====================
# 说明：本策略的 MACD 指标在写入 DuckDB 时已经固定。
# 为了避免构成直接的投资建议，我们将每次买入的资金比例加入随机性。
# 强烈建议读者在研究时，将参数修改为您自己想要测试的固定值
params = {
    'buy_ratio_multiplier': random.uniform(0.5, 1.0) # 随机买入比例乘数
}

# 全局字典，用于记录上一根K线（昨日）的指标值
# 格式: { "stock_code": {"DIF": dif, "DEA": dea, "MACD": macd} }
g_yesterday_indicators = {}

def init(stock_list, data):
    """
    策略初始化函数
    """
    logging.info("初始化：基于 khIndex 读取底层自定义指标的 MACD 策略")
    logging.info(f"随机参数: 买入比例乘数={params['buy_ratio_multiplier']:.2f}")
    logging.info("说明：本策略在 init 阶段注入 MACD 相关的字段，并在 khHandlebar 中直接获取指标进行判断。")
    
    # 动态配置需要额外加载的数据字段，注入 DIF, DEA, MACD
    khAddExtraFields(data, ["DIF", "DEA", "MACD"])

def khHandlebar(data: Dict) -> List[Dict]:
    """
    策略主逻辑，每天触发一次（由于是日线策略），遍历股票池中所有标的
    """
    global g_yesterday_indicators
    signals = []
    
    stock_list = khGet(data, "stocks")
    date_num = khGet(data, 'date_num')
    date_str = khGet(data, 'date_str')
    if not date_num:
        return signals

    # 随机化资金分配比例
    buy_ratio = (1.0 / max(len(stock_list), 1)) * params['buy_ratio_multiplier']

    for sc in stock_list:
        try:
            current_price = khPrice(data, sc)
            if current_price <= 0:
                continue
            has_pos = khHas(data, sc)

            today_dif = khIndex(data, sc, "DIF")
            today_dea = khIndex(data, sc, "DEA")
            today_macd = khIndex(data, sc, "MACD")

            yesterday = g_yesterday_indicators.get(sc, None)
            g_yesterday_indicators[sc] = {"DIF": today_dif, "DEA": today_dea, "MACD": today_macd}

            if yesterday is None:
                continue

            golden_cross = (yesterday["DIF"] < yesterday["DEA"]) and (today_dif > today_dea)
            death_cross = (yesterday["DIF"] > yesterday["DEA"]) and (today_dif < today_dea)

            if not has_pos and golden_cross:
                signals.extend(generate_signal(data, sc, current_price, buy_ratio, "buy", "MACD金叉买入(DIF上穿DEA)"))
                logging.info(f"【{date_str}】[{sc}] 触发金叉，发出买入信号。")
            elif has_pos and death_cross:
                signals.extend(generate_signal(data, sc, current_price, 1.0, "sell", "MACD死叉卖出(DIF下穿DEA)"))
                logging.info(f"【{date_str}】[{sc}] 触发死叉，发出卖出信号。")
        except Exception as e:
            logging.error(f"[{sc}] 策略执行读取指标或判断交叉时出错: {e}")
            continue

    return signals
