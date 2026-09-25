# coding: utf-8
# 策略说明：
# - 策略名称：双均线多股票（使用 khMA）
# - 功能：对股票池内每只股票，比较当日短期均线与长期均线；短>长 买入，短<长 卖出
# - 指标来源：使用 khQTTools 中的 khMA（内部封装的行情获取 + 移动平均）
# - 与使用 MyTT.MA 的版本区别：khMA 中内置了行情获取 + 移动平均，更加方便，MyTT.MA 需要在策略文档中先拉取历史行情再计算
from khQuantImport import *  # 导入所有量化工具
import random

# ==================== 策略参数 ====================
# 参数加入了随机性，避免直接构成投资建议。
# 强烈建议读者在研究时，将这些参数修改为您自己想要测试的固定值
params = {
    'ma_short': random.randint(3, 10),   # 短期均线：在一定范围内随机
    'ma_long': random.randint(15, 30),   # 长期均线：在一定范围内随机
}

def init(stocks=None, data=None):  # 策略初始化函数
    """本策略不需初始化"""
    logging.info(f"=== 双均线策略启动 ===")
    logging.info(f"随机参数: 短期均线={params['ma_short']}日, 长期均线={params['ma_long']}日")

def khHandlebar(data: Dict) -> List[Dict]:  # 主策略函数
    """策略主逻辑，支持多只股票的双均线策略"""
    signals = []  # 初始化信号列表
    stock_list = khGet(data, "stocks")  # 获取股票池列表
    current_date_str = khGet(data, "date_num")  # 获取当前日期数字格式
    
    ma_s = params['ma_short']
    ma_l = params['ma_long']

    for stock_code in stock_list:  # 遍历每只股票
        current_price = khPrice(data, stock_code, "open")  # 获取当前开盘价
        ma_short = khMA(stock_code, ma_s, end_time=current_date_str)  # 计算短期均线
        ma_long = khMA(stock_code, ma_l, end_time=current_date_str)   # 计算长期均线
            
        has_position = khHas(data, stock_code)  # 检查是否持有该股票
        
        if ma_short > ma_long and not has_position:  # 金叉且无持仓
            signals.extend(generate_signal(data, stock_code, current_price, 0.5, 'buy', f"{stock_code[:6]} 金叉买入"))  # 单股票20%仓位
            
        elif ma_short < ma_long and has_position:  # 死叉且有持仓
            signals.extend(generate_signal(data, stock_code, current_price, 1, 'sell', f"{stock_code[:6]} 死叉卖出"))  # 全部卖出
    
    return signals  # 返回交易信号

# khPreMarket 和 khPostMarket 函数省略，本次策略未使用 