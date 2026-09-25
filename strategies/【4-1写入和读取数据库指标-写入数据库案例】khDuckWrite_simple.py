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

def main():
    """
    这是一个使用 khDuckDB 和 khDuckWrite 进行本地数据读写与扩展的简单示例。
    
    【使用场景举例】：
    1. 你从外部（如第三方API、CSV文件）获取了一些新的指数数据、宏观数据或自定义因子。
    2. 你需要将这些数据按 QMT/KhQuant 的标准格式存入本地 DuckDB 数据库，以便后续在策略中回测调用。
    3. 或者你像本例一样，读取已有的行情数据，计算出新的指标（如 ma5），再存回数据库。
    """
    
    # ---------------------------------------------------------
    # 第一步：基础配置
    # ---------------------------------------------------------
    # 设定本地 DuckDB 数据库的存放根目录
    # 请确保该目录存在，且你有读写权限。
    duckdb_path = os.environ.get("DUCKDB_DATA_ROOT", r"D:\khData")
    
    # 设定数据周期。支持的周期通常包括 "1d" (日线), "1m" (1分钟) 等
    period = os.environ.get("DUCKDB_TEST_PERIOD", "1d")

    # =========================================================
    # 【标的设置 - 模式一】：处理单只股票（默认启用）
    # =========================================================
    # 如果你要处理单只股票，保留以下这行代码，并注释掉【模式二】
    stock_list = [os.environ.get("DUCKDB_TEST_STOCK", "000001.SZ")]

    # =========================================================
    # 【标的设置 - 模式二】：处理股票池中的多只股票
    # =========================================================
    # 如果你需要批量处理多只股票（比如沪深300成分股）：
    # 1. 请把上面的【模式一】的代码注释掉
    # 2. 将下面这段代码取消注释即可
    # 
    # csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "沪深300成分股_股票列表.csv")
    # df_pool = pd.read_csv(csv_path, header=None, names=["stock_code", "stock_name"], encoding="utf-8")
    # stock_list = df_pool["stock_code"].tolist()
    # # 为了防止初次测试时间过长，可以切片只取前3只股票。确认没问题后，再把切片去掉处理全部股票。
    # stock_list = stock_list[:3] 
    # =========================================================

    print(f"总计需要处理 {len(stock_list)} 只标的, 周期: {period}, 数据库路径: {duckdb_path}")

    # 遍历 stock_list，对每一只股票分别进行读取、计算和写入操作
    for stock in stock_list:
        print(f"\n" + "="*50)
        print(f" 开始处理标的: {stock} ".center(50, "="))
        print("="*50)

        # ---------------------------------------------------------
        # 第二步：获取基础数据（如果是纯外部导入，可跳过此步并直接读取CSV等）
        # ---------------------------------------------------------
        print(f"--- 1. 正在从 DuckDB 读取 {stock} 的基础数据 ---")
        result = khDuckDB(
            stock_list=[stock],
            period=period,
            fields=["time", "close"], # 我们只需要时间和收盘价来计算均线
            duckdb_path=duckdb_path
        )
        df = result.get(stock)
        
        if df is None or df.empty:
            print(f"【跳过】无可用行情数据，请先确保 {stock} 的基础数据已下载。")
            continue
            
        print(f"成功读取到 {len(df)} 条基础数据。")

        # ---------------------------------------------------------
        # 第三步：数据处理与加工（计算你的新指标或对齐外部数据）
        # ---------------------------------------------------------
        # 注意：要写入本地数据库的数据源 DataFrame，必须包含 "time" 字段！
        # "time" 字段通常是类似于 "20230101" (日线) 或 "20230101093100" (分钟线) 的字符串或整数。
        df = df.copy()
        
        # 这里我们演示通过收盘价计算 5日均线 (ma5)
        # 如果你是导入外部的指数数据，你的 df 应该直接从外部文件加载
        # 例：df = pd.read_csv("my_custom_index.csv") -> 确保其包含 "time" 列
        df["ma5"] = df["close"].rolling(5).mean()
        
        # 剔除空值，提取我们需要写入的字段
        # 【必须保留 "time" 字段，以及你想要新增/更新的数据字段】
        write_df = df[["time", "ma5"]].dropna()
        print(f"--- 2. 数据加工完毕，准备写入 {len(write_df)} 条新数据 ---")

        # ---------------------------------------------------------
        # 第四步：将新数据/自定义指数写入本地 DuckDB
        # ---------------------------------------------------------
        print(f"--- 3. 正在将 {stock} 的指标数据写入 DuckDB ---")
        # khDuckWrite 会自动帮你建表或在已有表中新增缺失的字段列
        write_result = khDuckWrite(
            stock_list=[stock],       # 指定数据归属的标的代码列表，通常传单个或多个标的
            period=period,            # 指定写入的周期表 (如 1d, 1m)
            data=write_df,            # 准备好的 DataFrame，必须包含 "time" 列！
            fields=["ma5"],           # 告诉写入工具，你要写入的具体是哪些新字段
            duckdb_path=duckdb_path   # 数据库根目录
        )
        print("写入操作返回结果:", write_result)

        # ---------------------------------------------------------
        # 第五步：验证写入结果（从数据库中重新读取刚刚写入的字段）
        # ---------------------------------------------------------
        print(f"--- 4. 验证 {stock} 刚才写入的数据 ---")
        verify = khDuckDB(
            stock_list=[stock],
            period=period,
            fields=["time", "ma5"],   # 直接请求我们刚刚写入的 "ma5" 字段
            duckdb_path=duckdb_path
        )
        verify_df = verify.get(stock)
        
        if verify_df is not None and not verify_df.empty:
            print(f"【{stock}】验证成功！获取到的最新 5 条数据如下：")
            print(verify_df.tail(5))
        else:
            print(f"【{stock}】验证失败：未能读取到新写入的数据。")


if __name__ == "__main__":
    main()
