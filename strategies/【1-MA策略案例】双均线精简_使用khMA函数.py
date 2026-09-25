# coding: utf-8
# 策略说明：
# - 策略名称：双均线精简（使用 khMA）
# - 功能：单只股票，比较当日短期均线与长期均线；短>长 买入，短<长 卖出
# - 指标来源：使用 khQTTools 中的 khMA（内部封装的行情获取 + 移动平均）
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
    """策略初始化"""
    logging.info(f"=== 双均线策略启动 ===")
    logging.info(f"随机参数: 短期均线={params['ma_short']}日, 长期均线={params['ma_long']}日")

def khHandlebar(data: Dict) -> List[Dict]:  # 主策略函数
    """策略主逻辑，在每个K线或Tick数据到来时执行"""
    signals = []  # 初始化信号列表
    stock_code = khGet(data, "first_stock")  # 获取第一只股票代码
    current_price = khPrice(data, stock_code, "open")  # 获取当前开盘价
    current_date_str = khGet(data, "date_num")  # 获取当前日期数字格式
    
    ma_s = params['ma_short']
    ma_l = params['ma_long']
  
    ma_short = khMA(stock_code, ma_s, end_time=current_date_str)  # 计算短期均线
    ma_long = khMA(stock_code, ma_l, end_time=current_date_str)   # 计算长期均线
      
    has_position = khHas(data, stock_code)  # 检查是否持有该股票
  
    if ma_short > ma_long and not has_position:  # 金叉且无持仓
        signals = generate_signal(data, stock_code, current_price, 1.0, 'buy', f"{ma_s}日线({ma_short:.2f}) 上穿 {ma_l}日线({ma_long:.2f})，全仓买入")  # 生成买入信号

    elif ma_short < ma_long and has_position:  # 死叉且有持仓
        signals = generate_signal(data, stock_code, current_price, 1.0, 'sell', f"{ma_s}日线({ma_short:.2f}) 下穿 {ma_l}日线({ma_long:.2f})，全仓卖出")  # 生成卖出信号

    return signals  # 返回交易信号

# khPreMarket 和 khPostMarket 函数省略，本次策略未使用