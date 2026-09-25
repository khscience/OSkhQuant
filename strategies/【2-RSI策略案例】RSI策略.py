# coding: utf-8
# 策略说明：
# - 策略名称：RSI 策略
# - 功能：多只股票，比较当日 RSI 与 阈值；RSI<下限 买入，RSI>上限 卖出
# - 指标来源：使用 MyTT 库的 RSI 函数（对收盘价序列计算RSI）
from khQuantImport import *  # 导入统一工具与指标
import random

# ==================== 策略参数 ====================
# 参数加入了随机性，避免直接构成投资建议。
# 强烈建议读者在研究时，将这些参数修改为您自己想要测试的固定值
params = {
    'rsi_period': random.randint(10, 20),      # RSI周期：在一定范围内随机
    'buy_threshold': random.randint(25, 45),   # 买入下限：在一定范围内随机
    'sell_threshold': random.randint(55, 75),  # 卖出上限：在一定范围内随机
}

def init(stocks=None, data=None):  # 初始化（无需特殊处理）
    """策略初始化（本策略无需特殊初始化）"""
    logging.info(f"=== RSI策略启动 ===")
    logging.info(f"随机参数: RSI周期={params['rsi_period']}, 买入线={params['buy_threshold']}, 卖出线={params['sell_threshold']}")

def khHandlebar(data: Dict) -> List[Dict]:  # 主策略函数
    signals = []  # 信号列表
    #pandas.sf=1
    dn = khGet(data, "date_num")  # 当前日期(数值格式)
    
    period = params['rsi_period']
    buy_th = params['buy_threshold']
    sell_th = params['sell_threshold']
    
    for sc in khGet(data, "stocks"):  # 遍历股票池
        hist = khHistory(sc, ["close"], period * 3, "1d", dn, fq="pre", force_download=False)  # 拉取足够天数的收盘价
        if not hist or sc not in hist or "close" not in hist[sc]:
            continue
            
        closes = hist[sc]["close"].values
        if len(closes) <= period:
            continue
            
        r = RSI(closes, period)  # 计算RSI
        rp, rn = float(r[-2]), float(r[-1])  # 前一日与当日RSI
        p = khPrice(data, sc, "open")  # 当日开盘价
        
        if (rp < buy_th <= rn) and not khHas(data, sc):  # RSI上穿买入线且无持仓→买入
            signals.extend(generate_signal(data, sc, p, 0.1, "buy", f"{sc[:6]} RSI 上穿{buy_th}，{rn:.2f}"))  # 0.1仓
        elif (rp > sell_th >= rn) and khHas(data, sc):  # RSI下穿卖出线且有持仓→卖出
            signals.extend(generate_signal(data, sc, p, 1.0, "sell", f"{sc[:6]} RSI 下穿{sell_th}，{rn:.2f}"))  # 全部卖出
    return signals  # 返回信号

