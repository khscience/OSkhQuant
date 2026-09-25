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

_g_duckdb_path = r"D:\khData"
_g_buy_ratio   = 0.1

def init(stock_list, data):
    global _g_duckdb_path, _g_buy_ratio
    logging.info("初始化：基于 DuckDB 预计算 MACD 指标的交易策略")
    logging.info(f"随机参数: 买入比例乘数={params['buy_ratio_multiplier']:.2f}")
    logging.info("说明：本策略直接从本地 DuckDB 读取之前计算并写入的 MACD 字段进行交易判断。")
    clear_khDuckDB_cache()
    _g_duckdb_path = os.environ.get("DUCKDB_DATA_ROOT", r"D:\khData")
    _g_buy_ratio   = (1.0 / max(len(stock_list), 1)) * params['buy_ratio_multiplier']


def khHandlebar(data: Dict) -> List[Dict]:
    signals = []

    stock_list = khGet(data, "stocks")
    date_num = khGet(data, 'date_num')
    date_str = khGet(data, 'date_str')
    if not date_num:
        return signals

    for sc in stock_list:
        try:
            current_price = khPrice(data, sc)
            has_pos = khHas(data, sc)
            if current_price <= 0:
                continue

            result = khDuckDB(
                stock_list=[sc], period="1d",
                fields=["time", "close", "DIF", "DEA", "MACD"],
                end_time=date_num, duckdb_path=_g_duckdb_path
            )

            df = result.get(sc)
            if df is None or len(df) < 2:
                continue

            today_data     = df.iloc[-1]
            yesterday_data = df.iloc[-2]
            if pd.isna(today_data.get("MACD")) or pd.isna(yesterday_data.get("MACD")):
                continue

            golden_cross = (yesterday_data["DIF"] < yesterday_data["DEA"]) and (today_data["DIF"] > today_data["DEA"])
            death_cross  = (yesterday_data["DIF"] > yesterday_data["DEA"]) and (today_data["DIF"] < today_data["DEA"])

            if not has_pos and golden_cross:
                signals.extend(generate_signal(data, sc, current_price, _g_buy_ratio, "buy", "MACD金叉买入(DIF上穿DEA)"))
                logging.info(f"【{date_str}】[{sc}] 触发金叉，发出买入信号。")
            elif has_pos and death_cross:
                signals.extend(generate_signal(data, sc, current_price, 1.0, "sell", "MACD死叉卖出(DIF下穿DEA)"))
                logging.info(f"【{date_str}】[{sc}] 触发死叉，发出卖出信号。")

        except Exception as e:
            logging.error(f"[{sc}] 策略执行读取DuckDB或计算时出错: {e}")
            continue

    return signals
