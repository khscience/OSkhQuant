# -*- coding: utf-8 -*-
"""
【重要提示】
1. 本文件不是交易策略文件，不可在策略回测框架中直接加载。
2. 本文件是一个独立的数据处理程序，需要直接在编辑器下运行。
3. 运行前，请务必将代码中的 DuckDB 数据库路径（duckdb_path）修改为你本地实际的数据存放路径。
"""

import os
import sys
import pandas as pd

# 为了能导入上一级目录（也就是项目根目录）的 khQTTools 模块，需要将上一级目录加入系统路径
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from khQTTools import khDuckDB, khDuckWrite

def calculate_macd(df, close_col="close", fastperiod=12, slowperiod=26, signalperiod=9):
    """
    使用 pandas 原生计算 MACD 指标，避免依赖外部库(如 talib)。
    计算公式:
    EMA(12) = 前一日EMA(12) X 11/13 + 今日收盘价 X 2/13
    EMA(26) = 前一日EMA(26) X 25/27 + 今日收盘价 X 2/27
    DIF = EMA(12) - EMA(26)
    DEA = （前一日DEA X 8/10 + 今日DIF X 2/10）
    MACD = (DIF - DEA) * 2
    """
    df = df.copy()
    
    # 计算快速和慢速 EMA
    ema_fast = df[close_col].ewm(span=fastperiod, adjust=False).mean()
    ema_slow = df[close_col].ewm(span=slowperiod, adjust=False).mean()
    
    # 计算 DIF (差离值)
    df["DIF"] = ema_fast - ema_slow
    
    # 计算 DEA (差离平均值)
    df["DEA"] = df["DIF"].ewm(span=signalperiod, adjust=False).mean()
    
    # 计算 MACD (柱状图)
    df["MACD"] = (df["DIF"] - df["DEA"]) * 2.0
    
    return df

def main():
    """
    这是一个使用 khDuckDB 读取行情、计算 MACD 并使用 khDuckWrite 存入本地的示例。
    """
    
    # ---------------------------------------------------------
    # 第一步：基础配置
    # ---------------------------------------------------------
    # 设定本地 DuckDB 数据库的存放根目录
    duckdb_path = os.environ.get("DUCKDB_DATA_ROOT", r"D:\khData")
    
    # 设定数据周期。本例以日线("1d")为例
    period = "1d"

    # =========================================================
    # 【标的设置 - 模式一】：处理单只股票（默认启用）
    # =========================================================
    # 如果你要处理单只股票，保留以下这行代码，并注释掉【模式二】
    # stock_list = ["000001.SZ"]

    # =========================================================
    # 【标的设置 - 模式二】：处理股票池中的多只股票
    # =========================================================
    # 如果你需要批量处理多只股票（比如沪深300成分股）：
    # 1. 请把上面的【模式一】的代码注释掉
    # 2. 将下面这段代码取消注释即可
    # 
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "沪深300成分股_股票列表.csv")
    df_pool = pd.read_csv(csv_path, header=None, names=["stock_code", "stock_name"], encoding="utf-8")
    stock_list = df_pool["stock_code"].tolist()
    # 为了防止初次测试时间过长，可以切片只取前3只股票。确认没问题后，再把切片去掉处理全部股票。
    stock_list = stock_list[:] 
    # =========================================================

    print(f"总计需要处理 {len(stock_list)} 只标的, 周期: {period}, 数据库路径: {duckdb_path}")

    for stock in stock_list:
        print(f"\n" + "="*50)
        print(f" 开始处理标的: {stock} ".center(50, "="))
        print("="*50)

        # ---------------------------------------------------------
        # 第二步：获取基础数据
        # ---------------------------------------------------------
        print("\n[1/4] 正在从 DuckDB 读取基础数据 (time, close)...")
        result = khDuckDB(
            stock_list=[stock],
            period=period,
            fields=["time", "close"], # 只需要时间和收盘价来计算MACD
            duckdb_path=duckdb_path
        )
        df = result.get(stock)
        
        if df is None or df.empty:
            print("错误: 无可用行情数据，请先确保基础数据已下载。")
            continue
            
        print(f"-> 成功读取到 {len(df)} 条基础数据。")

        # ---------------------------------------------------------
        # 第三步：数据处理与加工（计算 MACD 指标）
        # ---------------------------------------------------------
        print("\n[2/4] 正在计算 MACD (DIF, DEA, MACD)...")
        df = calculate_macd(df, close_col="close")
        
        # 提取我们需要写入的字段，必须保留 "time" 字段！
        # 去除早期的空值（由于EMA计算的初期波动，可自行决定是否 dropna）
        write_df = df[["time", "DIF", "DEA", "MACD"]].dropna()
        print(f"-> MACD 计算完毕，准备写入 {len(write_df)} 条指标数据。")

        # ---------------------------------------------------------
        # 第四步：将 MACD 指标写入本地 DuckDB
        # ---------------------------------------------------------
        print("\n[3/4] 正在将指标数据写入 DuckDB 对应表结构中...")
        write_result = khDuckWrite(
            stock_list=[stock],
            period=period,
            data=write_df,
            fields=["DIF", "DEA", "MACD"], # 告诉写入工具，你要写入这三个新字段
            duckdb_path=duckdb_path
        )
        print("-> 写入操作返回结果:", write_result)

        # ---------------------------------------------------------
        # 第五步：验证写入结果
        # ---------------------------------------------------------
        print("\n[4/4] 验证刚才写入的数据...")
        verify = khDuckDB(
            stock_list=[stock],
            period=period,
            fields=["time", "close", "DIF", "DEA", "MACD"], # 读取收盘价和刚刚写入的MACD指标
            duckdb_path=duckdb_path
        )
        verify_df = verify.get(stock)
        
        if verify_df is not None and not verify_df.empty:
            print("-> 验证成功！获取到的最新 5 条完整数据如下：")
            # 格式化打印，让输出更美观
            pd.set_option('display.max_columns', None)
            pd.set_option('display.width', 1000)
            print(verify_df.tail(5).to_string(index=False))
        else:
            print("-> 验证失败：未能读取到新写入的数据。")

if __name__ == "__main__":
    main()
