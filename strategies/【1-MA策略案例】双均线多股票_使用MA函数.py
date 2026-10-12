# coding: utf-8  # 源文件编码
# 策略说明：
# - 策略名称：双均线多股票（使用 MyTT.MA）
# - 功能：对股票池内每只股票，比较当日短期均线与长期均线；短>长 买入，短<长 卖出
# - 指标来源：使用 MyTT 库的 MA 函数（对收盘价序列计算均线）
# - 与使用 MyTT.MA 的版本区别：khMA 中内置了行情获取 + 移动平均，更加方便，MyTT.MA 需要在策略文档中先拉取历史行情再计算
from khQuantImport import *  # 统一导入工具与指标
import random

# ==================== 策略参数 ====================
# 参数加入了随机性，避免直接构成投资建议。
# 强烈建议读者在研究时，将这些参数修改为您自己想要测试的固定值
params = {
    'ma_short': random.randint(3, 10),   # 短期均线：在一定范围内随机
    'ma_long': random.randint(15, 30),   # 长期均线：在一定范围内随机
}

def init(stocks=None, data=None):  # 策略初始化（无需特殊处理）
    """本策略不需初始化"""
    logging.info(f"=== 双均线策略启动 ===")
    logging.info(f"随机参数: 短期均线={params['ma_short']}日, 长期均线={params['ma_long']}日")


def khHandlebar(data: Dict) -> List[Dict]:  # 主策略函数
    """多股票双均线（MyTT.MA）策略：短期均线上穿长期均线买入，反向卖出"""
    signals = []  # 信号列表
    stock_list = khGet(data, "stocks")  # 股票池
    dn = khGet(data, "date_num")  # 当前日期(数值格式)
    
    ma_s = params['ma_short']
    ma_l = params['ma_long']

    for sc in stock_list:  # 遍历股票
        try:
            # 拉取足够计算长均线的收盘价；如果某日数据缺失/不足，则跳过该股票本次运算
            hist = khHistory(sc, ["close"], ma_l * 2, "1d", dn, fq="pre", force_download=False)
            if not hist or sc not in hist or "close" not in hist[sc]:
                continue

            closes = hist[sc]["close"].values  # 收盘序列
            if len(closes) < ma_l:  # 数据不足以计算长均线，跳过
                continue

            ma_short_now = float(MA(closes, ma_s)[-1])  # 当日短期均线
            ma_long_now = float(MA(closes, ma_l)[-1])   # 当日长期均线

            price = khPrice(data, sc, "open")  # 当日开盘价
            has_pos = khHas(data, sc)  # 是否持仓

            if ma_short_now > ma_long_now and not has_pos:  # 金叉且无持仓→买入
                signals.extend(generate_signal(data, sc, price, 0.5, "buy", f"{sc[:6]} 金叉买入"))  # 0.5仓
            elif ma_short_now < ma_long_now and has_pos:  # 死叉且有持仓→卖出
                signals.extend(generate_signal(data, sc, price, 1.0, "sell", f"{sc[:6]} 死叉卖出"))  # 全部卖出
        except Exception:
            # 单只股票数据异常时，直接跳过本次回调中的该股票
            continue

    return signals  # 返回信号

