# coding: utf-8
"""
缠论工具库 - KhChanLunTools
提供缠论相关的通用函数，包括分型判断、趋势分析等

缠论核心概念:
- 分型: K线的局部极值点
  - 底分型: 中间K线是局部最低点
  - 顶分型: 中间K线是局部最高点
- 笔: 由相邻的顶分型和底分型构成
- 线段: 由特定的笔序列构成
- 中枢: 由至少三个连续的笔组成的重叠区域

使用方式:
    from khQuantImport import *
    
    # 获取K线数据
    kline_data = khKline(['000001.SZ'], '15m', 50)['000001.SZ']
    
    # 判断是否为底分型
    if is_bottom_fractal(kline_data):
        print("出现底分型")
"""

import pandas as pd
import numpy as np
import logging
from typing import Optional, Tuple, List


def is_bottom_fractal(kline_data: pd.DataFrame, index: int = -1) -> bool:
    """
    判断指定位置是否为底分型（标准版）
    
    底分型定义: 三根K线，中间一根的高点和低点都要低于左右两根K线
    即: 左K线.high > 中K线.high < 右K线.high
       左K线.low > 中K线.low < 右K线.low
    
    Args:
        kline_data: K线数据DataFrame，必须包含'high'和'low'列
        index: 要判断的K线位置，默认-1（最新一根）
                注意：index指向的是中间K线的位置
    
    Returns:
        bool: 如果是底分型返回True，否则返回False
    
    Example:
        >>> kline = khKline(['000001.SZ'], '15m', 50)['000001.SZ']
        >>> if is_bottom_fractal(kline):
        ...     print("最新K线形成底分型")
    """
    try:
        # 数据有效性检查
        if kline_data is None or len(kline_data) < 3:
            return False
        
        if 'high' not in kline_data.columns or 'low' not in kline_data.columns:
            logging.warning("K线数据缺少'high'或'low'列")
            return False
        
        # 转换负索引为正索引
        if index < 0:
            index = len(kline_data) + index
        
        # 确保有足够的数据（需要左右各一根K线）
        if index < 1 or index >= len(kline_data) - 1:
            return False
        
        # 获取三根K线
        left_high = kline_data.iloc[index - 1]['high']
        mid_high = kline_data.iloc[index]['high']
        right_high = kline_data.iloc[index + 1]['high']
        
        left_low = kline_data.iloc[index - 1]['low']
        mid_low = kline_data.iloc[index]['low']
        right_low = kline_data.iloc[index + 1]['low']
        
        # 标准底分型判断：中间K线的高点和低点都要低于左右K线
        is_fractal = (mid_high < left_high and mid_high < right_high and
                     mid_low < left_low and mid_low < right_low)
        
        return is_fractal
        
    except Exception as e:
        logging.error(f"判断底分型时出错: {str(e)}")
        return False


def is_top_fractal(kline_data: pd.DataFrame, index: int = -1) -> bool:
    """
    判断指定位置是否为顶分型（标准版）
    
    顶分型定义: 三根K线，中间一根的高点和低点都要高于左右两根K线
    即: 左K线.high < 中K线.high > 右K线.high
       左K线.low < 中K线.low > 右K线.low
    
    Args:
        kline_data: K线数据DataFrame，必须包含'high'和'low'列
        index: 要判断的K线位置，默认-1（最新一根）
                注意：index指向的是中间K线的位置
    
    Returns:
        bool: 如果是顶分型返回True，否则返回False
    
    Example:
        >>> kline = khKline(['000001.SZ'], '15m', 50)['000001.SZ']
        >>> if is_top_fractal(kline):
        ...     print("最新K线形成顶分型")
    """
    try:
        # 数据有效性检查
        if kline_data is None or len(kline_data) < 3:
            return False
        
        if 'high' not in kline_data.columns or 'low' not in kline_data.columns:
            logging.warning("K线数据缺少'high'或'low'列")
            return False
        
        # 转换负索引为正索引
        if index < 0:
            index = len(kline_data) + index
        
        # 确保有足够的数据（需要左右各一根K线）
        if index < 1 or index >= len(kline_data) - 1:
            return False
        
        # 获取三根K线
        left_high = kline_data.iloc[index - 1]['high']
        mid_high = kline_data.iloc[index]['high']
        right_high = kline_data.iloc[index + 1]['high']
        
        left_low = kline_data.iloc[index - 1]['low']
        mid_low = kline_data.iloc[index]['low']
        right_low = kline_data.iloc[index + 1]['low']
        
        # 标准顶分型判断：中间K线的高点和低点都要高于左右K线
        is_fractal = (mid_high > left_high and mid_high > right_high and
                     mid_low > left_low and mid_low > right_low)
        
        return is_fractal
        
    except Exception as e:
        logging.error(f"判断顶分型时出错: {str(e)}")
        return False


def find_all_fractals(kline_data: pd.DataFrame) -> Tuple[List[int], List[int]]:
    """
    找出K线数据中所有的顶分型和底分型位置
    
    Args:
        kline_data: K线数据DataFrame
    
    Returns:
        Tuple[List[int], List[int]]: (顶分型位置列表, 底分型位置列表)
    
    Example:
        >>> kline = khKline(['000001.SZ'], '1d', 100)['000001.SZ']
        >>> top_list, bottom_list = find_all_fractals(kline)
        >>> print(f"找到{len(top_list)}个顶分型, {len(bottom_list)}个底分型")
    """
    top_fractals = []
    bottom_fractals = []
    
    try:
        if kline_data is None or len(kline_data) < 3:
            return top_fractals, bottom_fractals
        
        # 遍历所有可能的分型位置（从第2根到倒数第2根）
        for i in range(1, len(kline_data) - 1):
            if is_top_fractal(kline_data, i):
                top_fractals.append(i)
            elif is_bottom_fractal(kline_data, i):
                bottom_fractals.append(i)
        
        return top_fractals, bottom_fractals
        
    except Exception as e:
        logging.error(f"查找所有分型时出错: {str(e)}")
        return top_fractals, bottom_fractals


def get_trend_direction(kline_data: pd.DataFrame, period: int = 20) -> str:
    """
    判断K线的趋势方向
    
    使用多种指标综合判断:
    1. 均线方向（短期均线vs长期均线）
    2. 高低点序列（是否创新高/新低）
    3. 收盘价位置（相对于均线的位置）
    
    Args:
        kline_data: K线数据DataFrame，必须包含'close'列
        period: 判断周期，默认20
    
    Returns:
        str: 'up'(上升趋势), 'down'(下降趋势), 'sideways'(震荡)
    
    Example:
        >>> kline = khKline(['000001.SZ'], '1d', 60)['000001.SZ']
        >>> trend = get_trend_direction(kline)
        >>> if trend == 'up':
        ...     print("当前处于上升趋势")
    """
    try:
        # 数据有效性检查
        if kline_data is None or len(kline_data) < period + 5:
            return 'sideways'
        
        if 'close' not in kline_data.columns:
            logging.warning("K线数据缺少'close'列")
            return 'sideways'
        
        # 计算短期和长期均线
        short_ma = kline_data['close'].rolling(window=period//2).mean()
        long_ma = kline_data['close'].rolling(window=period).mean()
        
        # 获取最新的均线值
        current_short_ma = short_ma.iloc[-1]
        current_long_ma = long_ma.iloc[-1]
        current_close = kline_data['close'].iloc[-1]
        
        # 判断均线趋势
        if pd.isna(current_short_ma) or pd.isna(current_long_ma):
            return 'sideways'
        
        # 获取前期均线值用于判断斜率
        prev_short_ma = short_ma.iloc[-5] if len(short_ma) >= 5 else current_short_ma
        prev_long_ma = long_ma.iloc[-5] if len(long_ma) >= 5 else current_long_ma
        
        # 计算均线斜率
        short_slope = (current_short_ma - prev_short_ma) / prev_short_ma if prev_short_ma > 0 else 0
        long_slope = (current_long_ma - prev_long_ma) / prev_long_ma if prev_long_ma > 0 else 0
        
        # 判断高低点
        recent_high = kline_data['high'].tail(period).max()
        recent_low = kline_data['low'].tail(period).min()
        current_high = kline_data['high'].iloc[-1]
        current_low = kline_data['low'].iloc[-1]
        
        # 综合判断
        up_signals = 0
        down_signals = 0
        
        # 信号1: 短期均线在长期均线上方
        if current_short_ma > current_long_ma:
            up_signals += 1
        else:
            down_signals += 1
        
        # 信号2: 均线斜率向上
        if short_slope > 0.01 and long_slope > 0:
            up_signals += 1
        elif short_slope < -0.01 and long_slope < 0:
            down_signals += 1
        
        # 信号3: 价格在均线上方
        if current_close > current_long_ma:
            up_signals += 1
        else:
            down_signals += 1
        
        # 信号4: 创新高或新低
        if current_high >= recent_high * 0.99:  # 接近或创新高
            up_signals += 1
        if current_low <= recent_low * 1.01:  # 接近或创新低
            down_signals += 1
        
        # 根据信号数量判断趋势
        if up_signals >= 3:
            return 'up'
        elif down_signals >= 3:
            return 'down'
        else:
            return 'sideways'
        
    except Exception as e:
        logging.error(f"判断趋势方向时出错: {str(e)}")
        return 'sideways'


def get_fractal_price(kline_data: pd.DataFrame, index: int, fractal_type: str) -> float:
    """
    获取分型位置的关键价格
    
    Args:
        kline_data: K线数据DataFrame
        index: 分型位置
        fractal_type: 分型类型，'top'或'bottom'
    
    Returns:
        float: 顶分型返回最高价，底分型返回最低价
    """
    try:
        if index < 0:
            index = len(kline_data) + index
        
        if index < 0 or index >= len(kline_data):
            return 0.0
        
        if fractal_type == 'top':
            return kline_data.iloc[index]['high']
        elif fractal_type == 'bottom':
            return kline_data.iloc[index]['low']
        else:
            return kline_data.iloc[index]['close']
            
    except Exception as e:
        logging.error(f"获取分型价格时出错: {str(e)}")
        return 0.0


def check_fractal_break(kline_data: pd.DataFrame, fractal_index: int, 
                       fractal_type: str, current_index: int = -1) -> bool:
    """
    检查分型是否被突破
    
    Args:
        kline_data: K线数据DataFrame
        fractal_index: 分型位置
        fractal_type: 分型类型，'top'或'bottom'
        current_index: 当前检查位置，默认-1（最新）
    
    Returns:
        bool: 如果分型被突破返回True
    
    Example:
        顶分型被突破：后续K线的最高价超过了顶分型的最高价
        底分型被突破：后续K线的最低价跌破了底分型的最低价
    """
    try:
        if current_index < 0:
            current_index = len(kline_data) + current_index
        
        if fractal_index >= current_index:
            return False
        
        fractal_price = get_fractal_price(kline_data, fractal_index, fractal_type)
        
        if fractal_type == 'top':
            # 检查是否向上突破顶分型
            return kline_data.iloc[current_index]['high'] > fractal_price
        elif fractal_type == 'bottom':
            # 检查是否向下跌破底分型
            return kline_data.iloc[current_index]['low'] < fractal_price
        else:
            return False
            
    except Exception as e:
        logging.error(f"检查分型突破时出错: {str(e)}")
        return False


# 导出所有公共函数
__all__ = [
    'is_bottom_fractal',
    'is_top_fractal',
    'find_all_fractals',
    'get_trend_direction',
    'get_fractal_price',
    'check_fractal_break',
]

