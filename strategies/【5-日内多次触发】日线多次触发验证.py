import random
from khQuantImport import *

# ==================== 策略参数 ====================
# 参数加入了随机性，避免直接构成投资建议。
# 强烈建议读者在研究时，将这些参数修改为您自己想要测试的固定值
params = {
    'ma_short': random.randint(3, 10),   # 短期均线：在一定范围内随机
    'ma_long': random.randint(15, 30),   # 长期均线：在一定范围内随机
    'buy_ratio': random.uniform(0.1, 0.3) # 买入比例：在一定范围内随机
}

# 使用全局状态记录当天是第几轮触发
STATE = {
    "last_date": None,
    "round_count": 0
}

def init(stock_list, data):
    STATE["last_date"] = None
    STATE["round_count"] = 0
    logging.info("初始化：日线多次触发（先卖后买）验证策略")
    logging.info(f"随机参数: 短期均线={params['ma_short']}, 长期均线={params['ma_long']}, 买入比例={params['buy_ratio']:.2f}")

def khHandlebar(data: Dict) -> List[Dict]:
    signals = []
    date_str = khGet(data, "date")
    if not date_str:
        return signals
        
    # 当日期发生变化时，重置轮次计数器为 1
    if STATE["last_date"] != date_str:
        STATE["last_date"] = date_str
        STATE["round_count"] = 1
    else:
        # 同一天内的后续触发，轮次 +1
        STATE["round_count"] += 1
        
    current_time = khGet(data, 'datetime_str')
    stocks = khGet(data, "stocks")
    
    # ==========================================
    # 第 1 轮触发：只执行“卖出”逻辑，并申请第 2 轮
    # ==========================================
    if STATE["round_count"] == 1:
        logging.info(f"【{date_str}】第 1 轮触发：执行卖出检查...")
        
        for stock in stocks:
            # 只检查当前有持仓的股票
            if khHas(data, stock):
                price = khPrice(data, stock)
                
                # 获取数据计算均线
                hist = khHistory([stock], ["close"], params['ma_long'] + 5, "1d", current_time=current_time)
                if not hist or stock not in hist or len(hist[stock]) < params['ma_long']:
                    continue
                    
                closes = hist[stock]["close"].values
                ma_l = MA(closes, params['ma_long'])[-1]
                
                # 如果当前价格跌破长期均线，执行全部卖出
                if price < ma_l:
                    sell_signals = generate_signal(data, stock, price, 1.0, "sell", f"跌破{params['ma_long']}日均线止损")
                    signals.extend(sell_signals)
                    logging.info(f"[{stock}] 跌破MA{params['ma_long']}({ma_l:.2f})，生成卖出信号。")
                    
        # 第一轮结束后，立刻向框架申请当天的第二次触发（用于买入）
        # 此时框架会在处理完上面的卖出单（释放资金）后，立刻再进一次 khHandlebar
        if khRequestNextDailyTrigger():
            logging.info(f"【{date_str}】已申请开启第 2 轮触发...")
            
    # ==========================================
    # 第 2 轮触发：只执行“买入”逻辑（利用第一轮释放的资金）
    # ==========================================
    elif STATE["round_count"] == 2:
        logging.info(f"【{date_str}】第 2 轮触发：执行买入检查... 当前可用资金: {khGet(data, 'cash'):.2f}")
        
        for stock in stocks:
            # 只检查当前没有持仓的股票
            if not khHas(data, stock):
                price = khPrice(data, stock)
                
                # 获取数据计算均线
                hist = khHistory([stock], ["close"], params['ma_long'] + 5, "1d", current_time=current_time)
                if not hist or stock not in hist or len(hist[stock]) < params['ma_long']:
                    continue
                    
                closes = hist[stock]["close"].values
                ma_s = MA(closes, params['ma_short'])[-1]
                ma_l = MA(closes, params['ma_long'])[-1]
                
                ma_s_prev = MA(closes, params['ma_short'])[-2]
                ma_l_prev = MA(closes, params['ma_long'])[-2]
                
                # 金叉判断：昨天的短均线<长均线，今天的短均线>长均线
                if ma_s_prev <= ma_l_prev and ma_s > ma_l:
                    # 使用随机比例的可用资金买入
                    buy_signals = generate_signal(data, stock, price, params['buy_ratio'], "buy", f"{params['ma_short']}日上穿{params['ma_long']}日金叉买入")
                    signals.extend(buy_signals)
                    logging.info(f"[{stock}] 形成均线金叉，生成买入信号。")
                    
        # 买入动作完成后，不再申请下一次触发，今天的任务结束。
        
    return signals
