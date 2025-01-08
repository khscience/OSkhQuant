import os
import csv
import os
import time
from datetime import datetime, timedelta
import pandas as pd
from xtquant import xtdata
# from xtquant.xtdata import get_client
import glob
import numpy as np
import logging
import ast
from PyQt5.QtCore import QThread, pyqtSignal



def read_stock_csv(file_path):
    """
    读取股票CSV文件，支持多种编码格式，并进行错误处理。
    
    参数:
    - file_path: CSV文件路径
    
    返回:
    - tuple: (股票代码列表, 股票名称列表)
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    # 尝试的编码列表
    encodings = ['utf-8', 'gb18030', 'gbk', 'gb2312', 'utf-16', 'ascii']
    
    # 存储结果
    stock_codes = []
    stock_names = []
    
    # 尝试不同的编码
    for encoding in encodings:
        try:
            with open(file_path, 'r', encoding=encoding) as file:
                # 先读取少量内容来验证编码是否正确
                file.read(1024)
                file.seek(0)  # 重置文件指针到开始
                
                csv_reader = csv.reader(file)
                
                # 检查是否有BOM
                first_row = next(csv_reader)
                if first_row and first_row[0].startswith('\ufeff'):
                    first_row[0] = first_row[0][1:]  # 删除BOM
                
                # 处理第一行
                process_row(first_row, stock_codes, stock_names)
                
                # 处理剩余行
                for row in csv_reader:
                    process_row(row, stock_codes, stock_names)
                
                # 如果成功读取，跳出循环
                break
                
        except UnicodeDecodeError:
            # 如果是最后一个编码仍然失败，则抛出异常
            if encoding == encodings[-1]:
                raise Exception(f"无法读取文件 {file_path}，已尝试以下编码：{', '.join(encodings)}")
            continue
            
        except Exception as e:
            # 处理其他可能的异常
            raise Exception(f"读取文件 {file_path} 时发生错误: {str(e)}")

    return stock_codes, stock_names

def process_row(row, stock_codes, stock_names):
    """
    处理CSV的单行数据，处理带有交易所后缀的股票代码
    
    参数:
    - row: CSV行数据
    - stock_codes: 股票代码列表（会被修改）
    - stock_names: 股票名称列表（会被修改）
    """
    if len(row) >= 2:
        stock_code = row[0].strip()
        stock_name = row[1].strip()
        
        logging.info(f"处理股票: {stock_code} - {stock_name}")

        # 检查股票代码格式
        if '.' in stock_code:  # 已经包含后缀
            # 处理A股、ETF和指数
            if ((stock_code.startswith(('600', '601', '603', '605', '688')) and stock_code.endswith('.SH')) or  # 上海所有板块
                (stock_code.startswith(('000', '002', '300', '301')) and stock_code.endswith('.SZ')) or  # 深圳所有板块
                (stock_code.startswith(('51', '58')) and stock_code.endswith('.SH')) or  # 上海ETF
                (stock_code.startswith('15') and stock_code.endswith('.SZ')) or  # 深圳ETF
                # 增加对指数的支持
                (stock_code.startswith(('000', '399')) and stock_code.endswith(('.SH', '.SZ')))):  # 主要指数
                stock_codes.append(stock_code)
                stock_names.append(stock_name)
                logging.info(f"添加股票/ETF/指数: {stock_code} - {stock_name}")
            else:
                logging.info(f"跳过股票（代码格式不匹配）: {stock_code}")
        else:
            logging.info(f"跳过股票（无交易所后缀）: {stock_code}")

def download_and_store_data(local_data_path, stock_files, field_list, period_type, start_date, end_date, dividend_type='none', time_range='all', progress_callback=None, log_callback=None):
    """
    下载并存储指定股票、字段、周期类型和时间段的数据到文件。

    函数功能:
        1. 从指定的股票代码列表文件中读取股票代码。
        2. 创建本地数据存储目录(如果不存在)。
        3. 对于每只股票:
           - 下载指定周期类型的数据到本地。
           - 从本地读取指定字段的数据。
           - 将 "time" 列转换为日期时间格式。
           - 如果指定了时间段,则筛选出指定时间段内的数据。
           - 将筛选出的数据添加到结果 DataFrame 中。
           - 将结果 DataFrame 存储到本地文件。
        4. 输出数据读取和存储完成的提示信息。

    文件命名规则:
        - 存储的文件名格式: "{股票代码}_{周期类型}_{起始日期}_{结束日期}_{时间段}_{复权方式}.csv"
        - 示例1: "000001.SZ_tick_20240101_20240430_all_none.csv"
          - 股票代码: 000001.SZ
          - 周期类型: tick
          - 起始日期: 20240101
          - 结束日期: 20240430
          - 时间段: all (表示全部时间段)
          - 复权方式: none (表示不复权)
        - 示例2: "000001.SZ_1d_20240101_20240430_all_front.csv"
          - 复权方式: front (表示前复权)
        - 如果指定了具体的时间段,时间段部分将替换为 "HH_MM-HH_MM" 的格式
          - 示例: "000001.SZ_1m_20240101_20240430_09_30-11_30_none.csv"
          - 时间段: 09_30-11_30 (表示 09:30 到 11:30 的时间段)

    参数:
    - local_data_path (str): 本地数据存储路径。
      - 该参数指定存储数据的本地目录路径。
      - 如果目录不存在，会自动创建。
      - 示例: "I:/stock_data_all_2"
      
    - stock_files (list): 股票代码列表文件路径列表。
      - 该参数指定包含股票代码的文件路径列表。
      - 每个文件应包含股票代码和名称两列。
      - 支持的股票类型：
        - A股：上海（600/601/603/605/688）、深圳（000/002/300/301）
        - 指数：上证（000）、深证（399）
      - 示例: ["HS300idx.csv", "otheridx.csv"]
      
    - field_list (list): 要存储的字段列表。
      - 该参数指定要下载和存储的股票数据字段列表。
      - 常用字段包括：open（开盘价）、high（最高价）、low（最低价）、close（收盘价）、
                    volume（成交量）、amount（成交额）等。
      - 示例: ["open", "high", "low", "close", "volume"]
      
    - period_type (str): 要读取的周期类型。
      - 该参数指定要下载和存储的数据周期类型。
      - 可选值: 
        - 'tick': 逐笔数据
        - '1m': 1分钟线
        - '5m': 5分钟线
        - '1d': 日线数据
      - 示例: "1d"
      
    - start_date (str): 起始日期。
      - 该参数指定数据的起始日期。
      - 格式为 "YYYYMMDD"。
      - 示例: "20240101"
      
    - end_date (str): 结束日期。
      - 该参数指定数据的结束日期。
      - 格式为 "YYYYMMDD"。
      - 示例: "20240430"

    - dividend_type (str, optional): 复权方式。
      - 该参数指定数据的复权方式，默认为'none'。
      - 可选值:
        - 'none': 不复权，使用原始价格
        - 'front': 前复权，基于最新价格进行前复权计算
        - 'back': 后复权，基于首日价格进行后复权计算
        - 'front_ratio': 等比前复权，基于最新价格进行等比前复权计算
        - 'back_ratio': 等比后复权，基于首日价格进行等比后复权计算
      - 注意：复权设置仅对股票价格数据有效，对指数和成交量等数据无影响
      - 示例: "front"
      
    - time_range (str, optional): 要读取的时间段。
      - 该参数指定要筛选的数据时间段。
      - 格式为 "HH:MM-HH:MM"。
      - 如果指定为 "all"，则不进行时间段筛选，保留全部时间段的数据。
      - 仅对分钟和tick级别数据有效。
      - 示例: "09:30-11:30" 或 "all"
      
    - progress_callback (function, optional): 进度回调函数。
      - 该函数用于更新下载进度。
      - 接受一个整数参数，表示完成百分比（0-100）。
      - 可用于更新GUI进度条等。
      
    - log_callback (function, optional): 日志回调函数。
      - 该函数用于记录处理过程中的日志信息。
      - 接受一个字符串参数，表示日志消息。
      - 可用于在GUI中显示处理状态等。

    返回值:
    - 无返回值，数据直接保存到指定目录。

    异常:
    - 如果股票代码文件不存在或格式错误，会记录警告并跳过。
    - 如果数据下载失败，会记录错误并继续处理下一只股票。
    - 如果保存文件失败，会记录错误信息。
    """
    try:
        # 获取所有股票代码
        stocks = []
        for stock_file in stock_files:
            if os.path.exists(stock_file):
                logging.info(f"读取股票文件: {stock_file}")
                codes, names = read_stock_csv(stock_file)
                stocks.extend(codes)
        
        logging.info(f"股票列表: {stocks}")
        
        if not os.path.exists(local_data_path):
            os.makedirs(local_data_path)

        total_stocks = len(stocks)
        for index, stock in enumerate(stocks, 1):
            try:
                if log_callback:
                    log_callback(f"正在处理 {stock} ({index}/{total_stocks})")

                # 判断是否为指数
                is_index = stock in ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH", 
                                   "000300.SH", "000905.SH", "000852.SH"]

                try:
                    if is_index:
                        # 指数数据处理
                        logging.info(f"获取指数数据: {stock}")
                        xtdata.download_history_data(stock, period=period_type, 
                                                   start_time=start_date, end_time=end_date)
                        data = xtdata.get_market_data_ex(
                            field_list=['time'] + field_list,
                            stock_list=[stock],
                            period=period_type,
                            start_time=start_date,
                            end_time=end_date,
                            count=-1,
                            dividend_type=dividend_type,  # 添加复权参数
                            fill_data=True
                        )
                        if data and stock in data:
                            df = data[stock]
                            logging.info(f"成功获取指数数据: {stock}")
                        else:
                            raise Exception(f"未能获取指数数据: {stock}")
                    else:
                        # 普通股票数据处理
                        logging.info(f"获取股票数据: {stock}")
                        xtdata.download_history_data(stock, period=period_type, 
                                                   start_time=start_date, end_time=end_date)
                        data = xtdata.get_local_data(  
                            field_list=['time'] + field_list,
                            stock_list=[stock],
                            period=period_type,
                            start_time=start_date,
                            end_time=end_date,
                            dividend_type=dividend_type,  # 添加复权参数
                            fill_data=True
                        )
                        df = data[stock]

                    # 开始数据处理和保存
                    logging.debug(f"准备处理数据 - 股票代码: {stock}")
                    logging.debug(f"原始数据形状: {df.shape}")
                    logging.debug(f"原始数据列: {df.columns.tolist()}")
                    
                    # 统一的数据处理逻辑
                    df["time"] = pd.to_datetime(df["time"].astype(float), unit='ms') + pd.Timedelta(hours=8)
                    logging.debug(f"时间列转换后的前5行:\n{df['time'].head()}")

                    if period_type == '1d':
                        df["date"] = df["time"].dt.strftime("%Y-%m-%d")
                        df = df[["date"] + field_list]
                    else:
                        if time_range != 'all':
                            start_time, end_time = time_range.split('-')
                            start_time = datetime.strptime(start_time, "%H:%M").time()
                            end_time = datetime.strptime(end_time, "%H:%M").time()
                            df["time_obj"] = df["time"].dt.time
                            mask = (df["time_obj"] >= start_time) & (df["time_obj"] <= end_time)
                            df = df.loc[mask].copy()
                            df.drop(columns=["time_obj"], inplace=True)
                        
                        df["date"] = df["time"].dt.strftime("%Y-%m-%d")
                        df["time"] = df["time"].dt.strftime("%H:%M:%S")
                        df = df[["date", "time"] + field_list]

                    # 保存数据
                    logging.debug(f"准备保存数据 - 股票代码: {stock}")
                    logging.debug(f"处理后数据形状: {df.shape}")
                    logging.debug(f"处理后数据列: {df.columns.tolist()}")
                    logging.debug(f"处理后前5行数据:\n{df.head()}")
                    
                    if not df.empty:
                        time_range_filename = time_range.replace(":", "_")
                        # 在文件名中添加复权信息
                        file_name = f"{stock}_{period_type}_{start_date}_{end_date}_{time_range_filename}_{dividend_type}.csv"
                        file_path = os.path.join(local_data_path, file_name)
                        
                        logging.info(f"保存文件 - 路径: {file_path}")
                        df.to_csv(file_path, index=False)
                        logging.info(f"文件保存成功: {file_path}")
                        
                        # 验证文件是否成功保存
                        if os.path.exists(file_path):
                            file_size = os.path.getsize(file_path)
                            logging.info(f"已保存文件大小: {file_size} 字节")
                        else:
                            logging.error(f"文件保存失败: {file_path}")
                            
                        if log_callback:
                            log_callback(f"{stock} {period_type} 数据已存储到: {file_path}")
                    else:
                        logging.warning(f"股票 {stock} 的数据为空，跳过保存")

                except Exception as e:
                    logging.error(f"处理{stock}时出错: {str(e)}", exc_info=True)
                    raise

                if progress_callback:
                    progress_callback(int(index / total_stocks * 100))
                
                time.sleep(1)  # 添加延迟避免请求过快

            except Exception as e:
                logging.error(f"处理股票 {stock} 时出错: {str(e)}", exc_info=True)
                raise
        
        if log_callback:
            log_callback("数据下载和存储完成.")

    except Exception as e:
        logging.error(f"下载存储数据时出错: {str(e)}", exc_info=True)
        raise

def calculate_intraday_features(file_path, sample_file_name, daily_file_name_pattern, feature_types, output_path, output_file_name, trading_minutes=240):
    """
    计算股票的日内特征,并将结果保存到csv文件中。

    参数:
    - file_path: str
        股票数据文件所在的目录路径。
    - sample_file_name: str
        样本文件名,用于提取周类型、起始日期和束日期。
        样文件名应该遵循以下格式: "股票代码_周期类型_起始日_结束日期_时间范围.csv"
        例如: "000001.SZ_1m_20240101_20240430_09_30-11_30.csv"
    - daily_file_name_pattern: str
        日数据文件名的模式,用构造与分钟数据对应的日数据文件名。
        模式中应该包含股票代码的占位符,例如: "000001.SZ_1d_20240101_20240430_all.csv"
    - feature_types: list
        要计算的特征类型列表,可选值包括: 'volume_ratio', 'return_rate'。
    - output_path: str
        输出文件的目录路径。
    - output_file_name: str
        输出文件名。
    - trading_minutes: int, 可选, 默认为240
        每个交易日的交易分钟数,用于计算成交量比例。默认为240分钟(4小时)

    函数功能:
    1. 根据样本文件名提取周期类型、起始日期和结束日期。
    2. 获取与样本文件名格式相同的所有文件。
    3. 对每个文件:
       - 从文件路径中提取股票代码。
       - 读取逐分钟数据文件。
       - 构造正确的日数据文件名,并读取日数据文件。
       - 计算过去5天的平均交易量。
       - 获取前一天的收盘价。
       - 将分钟数据和日数据按日期合并。
       - 根据指定的特征类型计算相应的特征值。
       - 删除不要的列,并添加股票代码列。
       - 去掉前5天(包括第5天)和最后一天的数据。
    4. 如果输出路径不存在,则创建文件夹。
    5. 将计算结果保存到csv文件中,如果文件已经存在,则追加数据。

    返回值:
    无返回值,计算结果直接保存到指定的输出文件中。
    """

    # 从样本文件名中提取周期类型、起始日期和结束日期
    file_name_parts = sample_file_name.split('_')
    data_type = file_name_parts[1]
    start_date = file_name_parts[2]
    end_date = file_name_parts[3]

    # 获取与样本文件名格式相同的所有文件
    file_pattern = f"*_{data_type}_{start_date}_{end_date}_*.csv"
    file_list = glob.glob(os.path.join(file_path, file_pattern))

    # 理每文件
    for idx, minute_file_path in enumerate(file_list):
        # 从文件路径中提取股票代码
        stock_code = os.path.basename(minute_file_path).split('_')[0]

        # 读取逐分钟数据文件
        minute_data = pd.read_csv(minute_file_path)

        # 构造正确的日数据文件名
        stock_code_example = daily_file_name_pattern.split('_')[0]
        daily_file_name = daily_file_name_pattern.replace(stock_code_example, stock_code)
        daily_file_path = os.path.join(file_path, daily_file_name)
        daily_data = pd.read_csv(daily_file_path)

        # 计算过去5天的平均交易量
        daily_data['past_avg_volume'] = daily_data['volume'].rolling(window=5).mean().shift(1)

        # 获取前一天的收盘价
        daily_data['prev_close'] = daily_data['close'].shift(1)

        minute_data['date'] = pd.to_datetime(minute_data['date'])
        daily_data['date'] = pd.to_datetime(daily_data['date'])

        # 检查分钟数据否有 'close' 列,如果没有,则使用 'price' 列
        if 'close' not in minute_data.columns:
            minute_data['price'] = minute_data['price']
        else:
            minute_data['price'] = minute_data['close']

        # 按日期合并分钟据和日数据
        merged_data = pd.merge(minute_data, daily_data[['date', 'past_avg_volume', 'prev_close']], on='date', how='left')

        eps = 1e-8  # 添加一个小的常数

        # 计算特征
        for feature_type in feature_types:
            if feature_type == 'volume_ratio':
                merged_data['volume_ratio'] = merged_data.apply(lambda x: x['volume'] / (x['past_avg_volume'] / trading_minutes + eps) if pd.notna(x['past_avg_volume']) else np.nan, axis=1)
            elif feature_type == 'return_rate':
                merged_data['return_rate'] = merged_data.apply(lambda x: (x['price'] - x['prev_close']) / x['prev_close'] if pd.notna(x['prev_close']) else np.nan, axis=1)

        # 删除不要的列
        merged_data = merged_data[['date', 'time'] + feature_types]
        merged_data['stock_code'] = stock_code  # 添加股票代码列

        # 去掉前6天(包括第6天)和最后一天的数据
        min_date = merged_data['date'].min()
        max_date = merged_data['date'].max()
        merged_data = merged_data[(merged_data['date'] > min_date + pd.Timedelta(days=6)) & (merged_data['date'] < max_date)]

        # 如果输出路径不存在,则创建文件夹
        if not os.path.exists(output_path):
            os.makedirs(output_path)

        # 保存结果到csv文件,如果文件已经存在,则追加数据
        output_file_path = os.path.join(output_path, output_file_name)
        header = idx == 0  # 如果是第一个文件,则写入表头,否则不写入
        mode = 'w' if idx == 0 else 'a'  # 如果是第一个文件,则写入模式为'w',否则为'a'(追加)
        merged_data.to_csv(output_file_path, index=False, header=header, mode=mode)

def calculate_next_day_return(file_path, sample_file_name, feature_types, output_path, output_file_name):
    """
    计算股票的下一个交易日收益率,并将结果保存到csv文件中。

    参数:
    - file_path: str
        股票数据文件所在的目录路径。
    - sample_file_name: str
        样本文件名,用于提取起始日期和结束日期。
        样本文件名应该遵循以下格式: "股票代码_1d_起始日期_结束日期_all.csv"
        例如: "000001.SZ_1d_20240101_20240430_all.csv"
    - feature_types: list
        要计算的特征类型列表,目前支持: 'next_day_return_rate' (下一个交易日收益率)。
    - output_path: str
        输出文件的目录路径。
    - output_file_name: str
        输出文件名。

    函数功能:
    1. 根据样本文件名提取起始日期和结束日期
    2. 获取与样本文件名格式相同的所有文件。
    3. 对每个文件:
       - 从文件路径中提取股票代码。
       - 取日数据文件。
       - 将期列转换为日期时间类型。
       - 如果 'next_day_return_rate' 在特征类型列表中,计算下一个交易日的收盘价收益率,并将其记录到当前交易日。
       - 提取日级别的数据,每个日期只保留一条记录,包括日期和指定的特征。
       - 添加股票代码列。
       - 去掉前6天(包括第6天)和最后一天的数据。
    4. 如果输出路径不存在,则创建文件夹。
    5. 将计算结果保存到csv文件中,如果文件已经存在,则追加数据。

    返回值:
    无返回值,计算结果直接保存到指定的输出文件中。
    """
    # 从样本文件名中提取起始日期和结束日期
    file_name_parts = sample_file_name.split('_')
    start_date = file_name_parts[2]
    end_date = file_name_parts[3]

    # 获取与样本文件名格式相同的所有文件
    file_pattern = f"*_1d_{start_date}_{end_date}_all.csv"
    file_list = glob.glob(os.path.join(file_path, file_pattern))

    # 处理每个文件
    for idx, daily_file_path in enumerate(file_list):
        # 从文路径中提取股票代码
        stock_code = os.path.basename(daily_file_path).split('_')[0]

        # 读取日数据文件
        daily_data = pd.read_csv(daily_file_path)

        # 将日期列转换为日期时间类型
        daily_data['date'] = pd.to_datetime(daily_data['date'])

        # 计算第二天的收盘收益率,并将其记录到当天
        if 'next_day_return_rate' in feature_types:
            daily_data['next_day_return_rate'] = daily_data['close'].pct_change().shift(-1)

        # 提取日级别的数据,每个日期只保留一条记录
        daily_data = daily_data[['date'] + [feature for feature in feature_types if feature in daily_data.columns]].dropna().drop_duplicates(subset='date')
        daily_data['stock_code'] = stock_code  # 添加股票代码列

        # 去掉前6天(包括第6天)和最后一天的数据
        min_date = daily_data['date'].min()
        max_date = daily_data['date'].max()
        second_last_date = max_date  
        daily_data = daily_data[(daily_data['date'] > min_date + pd.Timedelta(days=6)) & (daily_data['date'] <= second_last_date)]

        # 如果输出路径不存在,则创建文件夹
        if not os.path.exists(output_path):
            os.makedirs(output_path)

        # 保存结果到csv文件,如果文件已经存在,则追加数据
        output_file_path = os.path.join(output_path, output_file_name)
        header = idx == 0  # 如果是第一个文件,则写入表头,否则不写入
        mode = 'w' if idx == 0 else 'a'  # 如果是第一个文件,则写入模式为'w',否则为'a'(追加)
        daily_data.to_csv(output_file_path, index=False, header=header, mode=mode)

def get_available_sectors():
    """获取所有可用的板块代码"""
    try:
        # 获取 miniQMT 客户端连接
        # c = get_client()
        # # 确保客户端已连接
        # if not c.connect():
        #     raise Exception("无法连接到 miniQMT 客户端")
        
        # 获取所有板块
        sectors = xtdata.get_sector_list()
        
        logging.info("可用的板块列表：")
        for sector in sectors:
            # 尝试获取该板块的成分股
            components = xtdata.get_stock_list_in_sector(sector)
            count = len(components) if components else 0
            logging.info(f"板块: {sector}, 成分股数量: {count}")
        
        return sectors
    except Exception as e:
        logging.error(f"获取板块列表时出错: {str(e)}")
        return []

def get_stock_list():
    """获取所有股票代码和名称，包括上证A股、创业板、沪深A股、深证A股、科创板、指数及其集合，以及重要指数的成分股"""
    try:
        # 获取 miniQMT 客户端连接
        # c = get_client()
        # # 确保客户端已连接
        # if not c.connect():
        #     raise Exception("无法连接到 miniQMT 客户端")
        
        xtdata.download_sector_data()

        logging.info("开始获取股票列表...")
        
        # 初始化返回的字典
        stock_dict = {
            'sh_a': [],      # 上证A股
            'sz_a': [],      # 深证A股
            'gem': [],       # 创业板
            'sci': [],       # 科创板
            'hs_a': [],      # 沪深A股
            'indices': [],   # 指数
            'all_stocks': [], # 所有股票的集合
            'hs300_components': [],  # 沪深300成分股
            'zz500_components': [],  # 中证500成分股
            'sz50_components': [],   # 上证50成分股
        }
        
        # 重要指数列表
        important_indices = [
            {'code': '000001.SH', 'name': '上证指数'},
            {'code': '399001.SZ', 'name': '深证成指'},
            {'code': '399006.SZ', 'name': '创业板指'},
            {'code': '000688.SH', 'name': '科创50'},
            {'code': '000300.SH', 'name': '沪深300'},
            {'code': '000905.SH', 'name': '中证500'},
            {'code': '000852.SH', 'name': '中证1000'}
        ]

        # 板块映射
        sector_mapping = {
            '上证A股': 'sh_a',
            '深证A股': 'sz_a',
            '创业板': 'gem',
            '科创板': 'sci',
            '沪深A股': 'hs_a'
        }
        
        # 指数成分股映射
        index_components_mapping = {
            '沪深300': 'hs300_components',
            '中证500': 'zz500_components',
            '上证50': 'sz50_components'
        }

        # 获取各个板块的股票
        for sector_name, dict_key in sector_mapping.items():
            try:
                logging.info(f"获取{sector_name}股票列表...")
                stocks = xtdata.get_stock_list_in_sector(sector_name)
                if stocks:
                    logging.info(f"获取到 {len(stocks)} 只{sector_name}股票")
                    for code in stocks:
                        try:
                            detail = xtdata.get_instrument_detail(code)
                            if detail:
                                if isinstance(detail, str):
                                    detail = ast.literal_eval(detail)
                                name = detail.get('InstrumentName', '')
                                if name:
                                    stock_info = {
                                        'code': code,
                                        'name': name
                                    }
                                    stock_dict[dict_key].append(stock_info)
                                    # 将所有股票（除了沪深A股）添加到all_stocks中
                                    if dict_key != 'hs_a':  # 不添加沪深A股，因为它包含了其他所有股票
                                        stock_dict['all_stocks'].append(stock_info)
                        except Exception as e:
                            logging.error(f"处理股票 {code} 时出错: {str(e)}")
                            continue
                    logging.info(f"成功添加 {len(stock_dict[dict_key])} 只{sector_name}股票")
                else:
                    logging.warning(f"未获取到{sector_name}股票")
            except Exception as e:
                logging.error(f"获取{sector_name}股票列表时出错: {str(e)}")

        # 获取指数成分股
        for index_name, dict_key in index_components_mapping.items():
            try:
                logging.info(f"获取{index_name}成分股...")
                components = xtdata.get_stock_list_in_sector(index_name)
                if components:
                    logging.info(f"获取到 {len(components)} 只{index_name}成分股")
                    for code in components:
                        try:
                            detail = xtdata.get_instrument_detail(code)
                            if detail:
                                if isinstance(detail, str):
                                    detail = ast.literal_eval(detail)
                                name = detail.get('InstrumentName', '')
                                if name:
                                    stock_info = {
                                        'code': code,
                                        'name': name
                                    }
                                    stock_dict[dict_key].append(stock_info)
                                    # 将成分股也添加到all_stocks中
                                    stock_dict['all_stocks'].append(stock_info)
                        except Exception as e:
                            logging.error(f"处理{index_name}成分股 {code} 时出错: {str(e)}")
                            continue
                    logging.info(f"成功添加 {len(stock_dict[dict_key])} 只{index_name}成分股")
                else:
                    logging.warning(f"未获取到{index_name}成分股")
            except Exception as e:
                logging.error(f"获取{index_name}成分股列表时出错: {str(e)}")

        # 添加指数并同时添加到all_stocks
        stock_dict['indices'] = important_indices
        for index in important_indices:
            stock_dict['all_stocks'].append(index)
        
        # 对每个板块按照代码排序并去重
        for board in stock_dict:
            if board == 'all_stocks':
                # 对集合进行去重
                unique_stocks = {stock['code']: stock for stock in stock_dict[board]}.values()
                stock_dict[board] = sorted(unique_stocks, key=lambda x: x['code'])
            else:
                stock_dict[board].sort(key=lambda x: x['code'])
            logging.info(f"{board} 数量: {len(stock_dict[board])}")
        
        return stock_dict
        
    except Exception as e:
        logging.error(f"获取股票列表时出错: {str(e)}", exc_info=True)
        raise

def save_stock_list_to_csv(stock_dict, output_dir):
    """将股票列表保存为CSV文件"""
    try:
        os.makedirs(output_dir, exist_ok=True)
        logging.info(f"创建输出目录: {output_dir}")
        
        # 定义板块中文名称
        board_names = {
            'sh_a': '上证A股',
            'sz_a': '深证A股',
            'gem': '创业板',
            'sci': '科创板',
            'hs_a': '沪深A股',
            'indices': '指数',
            'all_stocks': '全部股票',
            'hs300_components': '沪深300成分股',
            'zz500_components': '中证500成分股',
            'sz50_components': '上证50成分股'
        }
        
        # 为每个板块创建CSV文件
        for board, stocks in stock_dict.items():
            # 保存单个板块文件
            file_path = os.path.join(output_dir, f"{board_names[board]}_股票列表.csv")
            with open(file_path, 'w', encoding='utf-8-sig') as f:
                for stock in stocks:
                    f.write(f"{stock['code']},{stock['name']}\n")
                    
        logging.info(f"股票列表已保存到目录: {output_dir}")
        logging.info(f"总共生成了 {len(board_names)} 个列表文件")
        
    except Exception as e:
        logging.error(f"保存股票列表时出错: {str(e)}", exc_info=True)
        raise

def get_and_save_stock_list(output_dir):

    """获取并保存股票列表的便捷函数，返回更新线程实例"""
    update_thread = StockListUpdateThread(output_dir)
    update_thread.start()
    return update_thread
class StockListUpdateThread(QThread):
    """股票列表更新线程"""
    progress = pyqtSignal(str)  # 用于发送进度信息
    finished = pyqtSignal(bool, str)  # 用于发送完成状态和消息

    def __init__(self, output_dir):
        super().__init__()
        self.output_dir = output_dir
        self.running = True

    def run(self):
        try:
            if not self.running:
                return

            self.progress.emit("正在初始化客户端连接...")
            # c = get_client()
            # if not c.connect():
            #     raise Exception("无法连接到 miniQMT 客户端")

            self.progress.emit("正在下载板块数据...")
            xtdata.download_sector_data()

            self.progress.emit("正在获取股票列表...")
            stock_dict = self.get_stock_list()

            self.progress.emit("正在保存股票列表...")
            self.save_stock_list_to_csv(stock_dict)

            if self.running:
                self.finished.emit(True, "股票列表更新成功！")

        except Exception as e:
            error_msg = f"更新股票列表时出错: {str(e)}"
            logging.error(error_msg, exc_info=True)
            if self.running:
                self.finished.emit(False, error_msg)

    def stop(self):
        self.running = False

    def get_stock_list(self):
        """获取所有股票列表"""
        stock_dict = {
            'sh_a': [],      # 上证A股
            'sz_a': [],      # 深证A股
            'gem': [],       # 创业板
            'sci': [],       # 科创板
            'hs_a': [],      # 沪深A股
            'indices': [],   # 指数
            'all_stocks': [], # 所有股票的集合
            'hs300_components': [],  # 沪深300成分股
            'zz500_components': [],  # 中证500成分股
            'sz50_components': [],   # 上证50成分股
        }

        # 重要指数列表
        important_indices = [
            {'code': '000001.SH', 'name': '上证指数'},
            {'code': '399001.SZ', 'name': '深证成指'},
            {'code': '399006.SZ', 'name': '创业板指'},
            {'code': '000688.SH', 'name': '科创50'},
            {'code': '000300.SH', 'name': '沪深300'},
            {'code': '000905.SH', 'name': '中证500'},
            {'code': '000852.SH', 'name': '中证1000'}
        ]

        # 板块映射
        sector_mapping = {
            '上证A股': 'sh_a',
            '深证A股': 'sz_a',
            '创业板': 'gem',
            '科创板': 'sci',
            '沪深A股': 'hs_a'
        }

        # 指数成分股映射
        index_components_mapping = {
            '沪深300': 'hs300_components',
            '中证500': 'zz500_components',
            '上证50': 'sz50_components'
        }

        # 获取各个板块的股票
        for sector_name, dict_key in sector_mapping.items():
            if not self.running:
                return stock_dict

            self.progress.emit(f"正在获取{sector_name}股票列表...")
            try:
                stocks = xtdata.get_stock_list_in_sector(sector_name)
                if stocks:
                    for code in stocks:
                        if not self.running:
                            return stock_dict
                        try:
                            detail = xtdata.get_instrument_detail(code)
                            if detail:
                                if isinstance(detail, str):
                                    detail = ast.literal_eval(detail)
                                name = detail.get('InstrumentName', '')
                                if name:
                                    stock_info = {'code': code, 'name': name}
                                    stock_dict[dict_key].append(stock_info)
                                    if dict_key != 'hs_a':
                                        stock_dict['all_stocks'].append(stock_info)
                        except Exception as e:
                            logging.error(f"处理股票 {code} 时出错: {str(e)}")
                            continue

            except Exception as e:
                logging.error(f"获取{sector_name}股票列表时出错: {str(e)}")

        # 获取指数成分股
        for index_name, dict_key in index_components_mapping.items():
            if not self.running:
                return stock_dict

            self.progress.emit(f"正在获取{index_name}成分股...")
            try:
                components = xtdata.get_stock_list_in_sector(index_name)
                if components:
                    for code in components:
                        if not self.running:
                            return stock_dict
                        try:
                            detail = xtdata.get_instrument_detail(code)
                            if detail:
                                if isinstance(detail, str):
                                    detail = ast.literal_eval(detail)
                                name = detail.get('InstrumentName', '')
                                if name:
                                    stock_info = {'code': code, 'name': name}
                                    stock_dict[dict_key].append(stock_info)
                                    stock_dict['all_stocks'].append(stock_info)
                        except Exception as e:
                            logging.error(f"处理{index_name}成分股 {code} 时出错: {str(e)}")
                            continue
            except Exception as e:
                logging.error(f"获取{index_name}成分股列表时出错: {str(e)}")

        # 添加指数
        stock_dict['indices'] = important_indices
        for index in important_indices:
            stock_dict['all_stocks'].append(index)

        # 对每个板块去重并排序
        for board in stock_dict:
            if board == 'all_stocks':
                unique_stocks = {stock['code']: stock for stock in stock_dict[board]}.values()
                stock_dict[board] = sorted(unique_stocks, key=lambda x: x['code'])
            else:
                stock_dict[board].sort(key=lambda x: x['code'])

        return stock_dict

    def save_stock_list_to_csv(self, stock_dict):
        """将股票列表保存为CSV文件"""
        os.makedirs(self.output_dir, exist_ok=True)

        board_names = {
            'sh_a': '上证A股',
            'sz_a': '深证A股',
            'gem': '创业板',
            'sci': '科创板',
            'hs_a': '沪深A股',
            'indices': '指数',
            'all_stocks': '全部股票',
            'hs300_components': '沪深300成分股',
            'zz500_components': '中证500成分股',
            'sz50_components': '上证50成分股'
        }

        for board, stocks in stock_dict.items():
            if not self.running:
                return
            self.progress.emit(f"正在保存{board_names[board]}列表...")
            file_path = os.path.join(self.output_dir, f"{board_names[board]}_股票列表.csv")
            with open(file_path, 'w', encoding='utf-8-sig') as f:
                for stock in stocks:
                    f.write(f"{stock['code']},{stock['name']}\n")

def supplement_history_data(stock_files, field_list, period_type, start_date, end_date, dividend_type='none', time_range='all', progress_callback=None, log_callback=None):
    """
    补充历史行情数据。

    参数:
    - stock_files (list): 股票代码列表文件路径列表
    - field_list (list): 要存储的字段列表
    - period_type (str): 要读取的周期类型 ('tick', '1m', '5m', '1d')
    - start_date (str): 起始日期,格式为 "YYYYMMDD"
    - end_date (str): 结束日期,格式为 "YYYYMMDD"
    - dividend_type (str): 复权方式，可选值：
        - 'none': 不复权
        - 'front': 前复权
        - 'back': 后复权
        - 'front_ratio': 等比前复权
        - 'back_ratio': 等比后复权
    - time_range (str): 要读取的时间段,格式为 "HH:MM-HH:MM"，默认为 "all"
    - progress_callback (function): 用于更新进度的回调函数
    - log_callback (function): 用于记录日志的回调函数
    """
    try:
        # 获取所有股票代码
        stocks = []
        for stock_file in stock_files:
            if os.path.exists(stock_file):
                logging.info(f"读取股票文件: {stock_file}")
                codes, names = read_stock_csv(stock_file)
                stocks.extend(codes)
        
        if not stocks:
            if log_callback:
                log_callback("没有找到需要补充数据的股票")
            return

        total_stocks = len(stocks)
        for index, stock in enumerate(stocks, 1):
            try:
                if log_callback:
                    log_callback(f"正在补充 {stock} 的数据 ({index}/{total_stocks})")

                # 调用download_history_data进行数据补充
                xtdata.download_history_data(
                    stock,
                    period=period_type,
                    start_time=start_date,
                    end_time=end_date,
                    incrementally=True
                )

                # 获取数据（带复权参数）
                data = xtdata.get_market_data(
                    field_list=field_list,
                    stock_list=[stock],
                    period=period_type,
                    start_time=start_date,
                    end_time=end_date,
                    dividend_type=dividend_type,
                    fill_data=True
                )

                if progress_callback:
                    progress = int((index / total_stocks) * 100)
                    progress_callback(progress)

            except Exception as e:
                error_msg = f"补充 {stock} 数据时出错: {str(e)}"
                logging.error(error_msg)
                if log_callback:
                    log_callback(error_msg)

    except Exception as e:
        error_msg = f"补充数据时出错: {str(e)}"
        logging.error(error_msg, exc_info=True)
        if log_callback:
            log_callback(error_msg)
        raise

# 使用示例
if __name__ == '__main__':
    from PyQt5.QtWidgets import QApplication
    import sys
    
    # 配置日志
    logging.basicConfig(level=logging.INFO,
                      format='%(asctime)s - %(levelname)s - %(message)s')
    
    # 创建Qt应用（必需的，即使是命令行运行）
    app = QApplication(sys.argv)
    
    # 设置输出目录
    output_dir = "stock_lists"
    
    # 创建更新线程
    update_thread = get_and_save_stock_list(output_dir)
    
    # 添加进度显示
    def show_progress(msg):
        print(f"进度: {msg}")
    
    def handle_finished(success, message):
        print(f"完成: {'成功' if success else '失败'} - {message}")
        app.quit()  # 完成后退出应用
    
    # 连接信号
    update_thread.progress.connect(show_progress)
    update_thread.finished.connect(handle_finished)
    
    # 启动事件循环
    sys.exit(app.exec_())