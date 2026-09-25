# coding: utf-8
"""回测记录用到的 xtquant 常量（xtconstant）真实取值。

开源版不依赖 xtquant，但回测内核写入委托、成交、持仓记录时沿用 xtconstant
的数值，策略可以通过 __positions__ / __account__ 读到它们。这里写死与
xtquant 一致的值：若用占位 0，买卖方向等字段会全部变成 0，和 CS 不一致。
取值已在装有 xtquant 的 Python 3.11 环境核对。
"""

SECURITY_ACCOUNT = 2
STOCK_BUY = 23
STOCK_SELL = 24
FIX_PRICE = 11
ORDER_SUCCEEDED = 56
DIRECTION_FLAG_LONG = 48
OFFSET_FLAG_OPEN = 48
OFFSET_FLAG_CLOSE = 49


class StockAccount:
    """回测用的账户对象，只保存账号与类型，不连接任何交易接口。"""

    def __init__(self, account_id, account_type="STOCK"):
        self.account_id = account_id
        self.account_type = account_type
