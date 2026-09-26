"""
个股分析窗口模块
用于分析回测期间单个股票的交易明细和收益曲线
"""
import logging
logging.getLogger('matplotlib').setLevel(logging.ERROR)

import matplotlib
matplotlib.use('Qt5Agg')

from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                              QLabel, QComboBox, QTableWidget, QTableWidgetItem,
                              QGroupBox, QSplitter, QHeaderView, QSizePolicy,
                              QMessageBox, QDialog)
from PyQt5.QtCore import Qt, QSettings
from PyQt5.QtGui import QColor, QIcon, QFont
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
import pandas as pd
import numpy as np
import os
import sys
from datetime import datetime, timedelta

# 设置matplotlib的字体和其他参数
plt.rcParams['font.sans-serif'] = ['PingFang SC', 'Heiti SC', 'STHeiti', 'Arial Unicode MS', 'Microsoft YaHei', 'SimHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False
plt.rcParams['font.family'] = 'sans-serif'
plt.style.use('dark_background')


TRADE_FEE_FIELDS = ('commission', 'stamp_tax', 'transfer_fee', 'flow_fee')


def _trade_total_fee(row):
    """返回一笔成交的完整费用，空值和旧报告缺失字段按 0 处理。"""
    total = 0.0
    for field in TRADE_FEE_FIELDS:
        value = row.get(field, 0)
        try:
            if pd.notna(value):
                total += float(value)
        except (TypeError, ValueError):
            continue
    return total


class StockAnalysisWindow(QMainWindow):
    """个股分析窗口"""

    def __init__(self, backtest_dir, trades_df, daily_stats_df, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.backtest_dir = backtest_dir
        self.trades_df = trades_df.copy() if trades_df is not None else pd.DataFrame()
        self.daily_stats_df = daily_stats_df.copy() if daily_stats_df is not None else pd.DataFrame()

        # 读取回测配置
        self._load_backtest_config()

        # K线数据缓存
        self.kline_cache = {}

        # 当前选中的股票
        self.current_stock = None

        # 日K线数据（用于点击交互）
        self.daily_kline_df = None
        self.kline_load_error = None

        # 每日交易汇总
        self.daily_trades = {}

        # 获取有交易的股票列表
        self.stock_list = self._get_traded_stocks()

        # 检测屏幕分辨率并设置字体缩放比例
        self.font_scale = self._detect_screen_resolution()

        # 设置窗口标题栏颜色（Windows）
        self._setup_dark_titlebar()

        self.setWindowTitle("个股分析")
        self._load_icon()
        self._init_ui()
        self._apply_dark_theme()

        # 如果有股票，默认选择第一个
        if self.stock_list:
            self.stock_combo.setCurrentIndex(0)
            self._on_stock_changed(0)

    def closeEvent(self, event):
        """释放日K画布和数据副本，避免反复打开个股分析后资源累积。"""
        try:
            canvas = getattr(self, 'canvas', None)
            figure = getattr(self, 'figure', None)
            if canvas is not None:
                canvas.close()
            if figure is not None:
                figure.clear()
                plt.close(figure)
            if hasattr(self, 'kline_cache'):
                self.kline_cache.clear()
            self.daily_kline_df = None
            self.trades_df = None
            self.daily_stats_df = None
            self.daily_trades = {}
        except Exception as exc:
            logging.warning(f"关闭个股分析窗口时释放资源失败: {exc}")
        event.accept()

    def _load_backtest_config(self):
        """读取回测配置"""
        self.intraday_period = '1m'  # 日内周期，固定使用1分钟线
        self.start_time = ''
        self.end_time = ''
        self.dividend_type = 'front'

        try:
            config_path = os.path.join(self.backtest_dir, "config.csv")
            if os.path.exists(config_path):
                config_df = pd.read_csv(config_path, encoding='utf-8-sig')
                if len(config_df) > 0:
                    row = config_df.iloc[0]
                    # 日内图固定使用1分钟线，不受回测周期影响
                    self.start_time = str(row.get('start_time', ''))
                    self.end_time = str(row.get('end_time', ''))
                    self.dividend_type = str(row.get('dividend_type', 'front'))
        except Exception as e:
            print(f"读取回测配置失败: {e}")
    
    def _detect_screen_resolution(self):
        """检测屏幕分辨率并返回缩放比例"""
        try:
            from PyQt5.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
            if screen:
                dpi = screen.logicalDotsPerInch()
                return max(1.0, dpi / 96.0)
        except:
            pass
        return 1.0
    
    def _setup_dark_titlebar(self):
        """设置深色标题栏"""
        try:
            from ctypes import windll, c_int, byref, sizeof
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            DWMWA_CAPTION_COLOR = 35

            windll.dwmapi.DwmSetWindowAttribute(
                int(self.winId()),
                DWMWA_USE_IMMERSIVE_DARK_MODE,
                byref(c_int(2)),
                sizeof(c_int)
            )

            caption_color = c_int(0x2b2b2b)
            windll.dwmapi.DwmSetWindowAttribute(
                int(self.winId()),
                DWMWA_CAPTION_COLOR,
                byref(caption_color),
                sizeof(caption_color)
            )
        except Exception as e:
            print(f"设置标题栏深色模式失败: {str(e)}")
    
    def _load_icon(self):
        """加载窗口图标"""
        try:
            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons", "stock_icon.png")
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
        except:
            pass
    
    def _get_traded_stocks(self):
        """获取有交易记录的股票列表"""
        if self.trades_df.empty:
            return []
        
        # 获取所有交易过的股票代码
        if 'code' in self.trades_df.columns:
            stocks = self.trades_df['code'].unique().tolist()
            return sorted(stocks)
        return []
    
    def _init_ui(self):
        """初始化UI"""
        # 设置窗口大小
        base_width, base_height = 1200, 800
        scaled_width = int(base_width * self.font_scale)
        scaled_height = int(base_height * self.font_scale)
        self.resize(scaled_width, scaled_height)
        
        # 创建主窗口部件
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout()
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_widget.setLayout(main_layout)
        
        # 顶部：股票选择区域
        top_widget = self._create_stock_selector()
        main_layout.addWidget(top_widget)
        
        # 中部：分割器包含图表和统计信息
        splitter = QSplitter(Qt.Vertical)
        splitter.setHandleWidth(2)
        
        # 图表区域
        chart_widget = self._create_chart_area()
        splitter.addWidget(chart_widget)
        
        # 交易明细表格
        trades_widget = self._create_trades_table()
        splitter.addWidget(trades_widget)
        
        splitter.setSizes([500, 300])
        main_layout.addWidget(splitter)

    def _create_stock_selector(self):
        """创建股票选择区域"""
        widget = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)

        # 股票选择标签
        label = QLabel("选择股票:")
        label.setStyleSheet(f"""
            font-size: {int(14 * self.font_scale)}px;
            font-weight: bold;
            color: #e8e8e8;
        """)
        layout.addWidget(label)

        # 股票下拉框
        self.stock_combo = QComboBox()
        self.stock_combo.setMinimumWidth(int(200 * self.font_scale))
        self.stock_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #505050;
                border-radius: 4px;
                padding: 5px 10px;
                font-size: {int(14 * self.font_scale)}px;
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QComboBox QAbstractItemView {{
                background-color: #3c3c3c;
                color: #e8e8e8;
                selection-background-color: #505050;
            }}
        """)

        # 添加股票到下拉框
        for stock in self.stock_list:
            self.stock_combo.addItem(stock)

        self.stock_combo.currentIndexChanged.connect(self._on_stock_changed)
        layout.addWidget(self.stock_combo)

        # 统计信息标签
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet(f"""
            font-size: {int(14 * self.font_scale)}px;
            color: #a0a0a0;
            margin-left: 20px;
        """)
        layout.addWidget(self.stats_label)

        layout.addStretch()
        widget.setLayout(layout)
        return widget

    def _create_chart_area(self):
        """创建图表区域"""
        group = QGroupBox("收益曲线")
        group.setStyleSheet(f"""
            QGroupBox {{
                font-size: {int(16 * self.font_scale)}px;
                font-weight: bold;
                background-color: #2d2d2d;
                border: 2px solid #404040;
                border-radius: 8px;
                padding: {int(10 * self.font_scale)}px;
            }}
            QGroupBox::title {{
                padding: 0 {int(8 * self.font_scale)}px;
                background-color: #2d2d2d;
            }}
        """)

        layout = QVBoxLayout()

        # 创建matplotlib图表
        self.figure = Figure(figsize=(10, 6), facecolor='#2d2d2d')
        self.canvas = FigureCanvas(self.figure)

        # 创建子图 - 价格图占更大比例 (3:1)
        # 调整布局：增加顶部和左侧边距，确保标题和标签显示完整
        self.ax_price = self.figure.add_subplot(411)  # 价格走势占3份
        self.ax_price.set_position([0.06, 0.28, 0.92, 0.65])  # [left, bottom, width, height]
        self.ax_pnl = self.figure.add_subplot(412)    # 累计盈亏占1份
        self.ax_pnl.set_position([0.06, 0.06, 0.92, 0.15])

        # 设置子图样式
        for ax in [self.ax_price, self.ax_pnl]:
            ax.set_facecolor('#2d2d2d')
            ax.tick_params(axis='both', colors='#a0a0a0')
            ax.grid(True, linestyle='--', alpha=0.1, color='#808080')
            for spine in ax.spines.values():
                spine.set_color('#404040')

        # 保存原始视图范围
        self._original_xlim_price = None
        self._original_ylim_price = None
        self._original_xlim_pnl = None
        self._original_ylim_pnl = None

        # 框选缩放相关变量
        self._zoom_rect = None
        self._zoom_start = None

        # 悬停提示框
        self._hover_annotation = self.ax_price.annotate(
            '', xy=(0, 0), xytext=(10, 10),
            textcoords='offset points',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#1a1a1a', edgecolor='#606060', alpha=0.95),
            fontsize=9, color='#e8e8e8', visible=False, zorder=100
        )
        self._last_hover_idx = None

        # 连接鼠标事件
        self.canvas.mpl_connect('button_press_event', self._on_mouse_press)
        self.canvas.mpl_connect('button_release_event', self._on_mouse_release)
        self.canvas.mpl_connect('motion_notify_event', self._on_mouse_move)
        self.canvas.mpl_connect('resize_event', self._on_canvas_resize)

        layout.addWidget(self.canvas)
        group.setLayout(layout)
        return group

    def _on_canvas_resize(self, event):
        """画布大小改变时调整布局"""
        if event.width > 0 and event.height > 0:
            # 根据画布大小动态调整边距
            left_margin = max(0.05, 50 / event.width)
            right_margin = 0.98
            width = right_margin - left_margin

            # 更新子图位置
            self.ax_price.set_position([left_margin, 0.28, width, 0.65])
            self.ax_pnl.set_position([left_margin, 0.06, width, 0.15])

    def _on_mouse_press(self, event):
        """鼠标按下事件"""
        if event.inaxes not in [self.ax_price, self.ax_pnl]:
            return

        if event.button == 1:  # 左键 - 开始框选
            self._zoom_start = (event.xdata, event.ydata, event.inaxes)
        elif event.button == 3:  # 右键 - 恢复原始视图
            self._reset_zoom()

    def _on_mouse_move(self, event):
        """鼠标移动事件 - 绘制框选矩形和悬停提示"""
        # 处理框选
        if self._zoom_start is not None and event.inaxes == self._zoom_start[2]:
            ax = self._zoom_start[2]
            x0, y0 = self._zoom_start[0], self._zoom_start[1]
            x1, y1 = event.xdata, event.ydata

            if x1 is not None and y1 is not None:
                if self._zoom_rect is not None:
                    self._zoom_rect.remove()
                width = x1 - x0
                height = y1 - y0
                self._zoom_rect = Rectangle((x0, y0), width, height,
                                             fill=False, edgecolor='#00BFFF',
                                             linestyle='--', linewidth=1.5)
                ax.add_patch(self._zoom_rect)
                self.canvas.draw_idle()
            return

        # 处理悬停提示（仅在价格图上且不在框选时）
        if event.inaxes == self.ax_price and self.daily_kline_df is not None and event.xdata is not None:
            idx = int(round(event.xdata))
            if 0 <= idx < len(self.daily_kline_df) and idx != self._last_hover_idx:
                self._last_hover_idx = idx
                row = self.daily_kline_df.iloc[idx]
                date_str = pd.to_datetime(row['time']).strftime('%Y-%m-%d')
                date_key = pd.to_datetime(row['time']).strftime('%Y%m%d')

                # 构建提示信息
                info_lines = [f"日期: {date_str}"]
                info_lines.append(f"开: {row['open']:.3f}  高: {row['high']:.3f}")
                info_lines.append(f"低: {row['low']:.3f}  收: {row['close']:.3f}")
                change = (row['close'] - row['open']) / row['open'] * 100 if row['open'] != 0 else 0
                change_color = '↑' if change >= 0 else '↓'
                info_lines.append(f"涨跌: {change_color} {abs(change):.2f}%")

                # 添加买卖信息
                if date_key in self.daily_trades:
                    trades = self.daily_trades[date_key]
                    if trades.get('buys'):
                        buy_info = [f"{t['volume']}股@{t['price']:.3f}" for t in trades['buys']]
                        info_lines.append(f"买入: {', '.join(buy_info)}")
                    if trades.get('sells'):
                        sell_info = [f"{t['volume']}股@{t['price']:.3f}" for t in trades['sells']]
                        info_lines.append(f"卖出: {', '.join(sell_info)}")

                self._hover_annotation.set_text('\n'.join(info_lines))
                self._hover_annotation.xy = (idx, row['high'])
                self._hover_annotation.set_visible(True)
                self.canvas.draw_idle()
        elif self._hover_annotation.get_visible():
            self._hover_annotation.set_visible(False)
            self._last_hover_idx = None
            self.canvas.draw_idle()

    def _on_mouse_release(self, event):
        """鼠标释放事件 - 完成框选缩放或点击打开日内图"""
        if self._zoom_start is None or event.button != 1:
            self._zoom_start = None
            return

        ax = self._zoom_start[2]
        x0, y0 = self._zoom_start[0], self._zoom_start[1]
        x1, y1 = event.xdata, event.ydata

        # 移除矩形
        if self._zoom_rect is not None:
            self._zoom_rect.remove()
            self._zoom_rect = None

        if x1 is None or y1 is None:
            self._zoom_start = None
            self.canvas.draw_idle()
            return

        # 判断是点击还是拖拽（使用相对偏移量判断）
        y_range = ax.get_ylim()
        price_range = y_range[1] - y_range[0] if y_range[1] != y_range[0] else 1
        is_click = abs(x1 - x0) < 0.5 and abs(y1 - y0) < price_range * 0.02

        if is_click:
            # 点击 - 如果在价格图上，尝试打开日内图
            if ax == self.ax_price and self.daily_kline_df is not None:
                x = int(round(x0))
                if 0 <= x < len(self.daily_kline_df):
                    row = self.daily_kline_df.iloc[x]
                    date_str = pd.to_datetime(row['time']).strftime('%Y%m%d')
                    self._zoom_start = None
                    self._show_intraday_chart(date_str)
                    return
        else:
            # 拖拽 - 框选缩放
            # 保存原始范围（如果还没保存）
            if self._original_xlim_price is None:
                self._original_xlim_price = self.ax_price.get_xlim()
                self._original_ylim_price = self.ax_price.get_ylim()
                self._original_xlim_pnl = self.ax_pnl.get_xlim()
                self._original_ylim_pnl = self.ax_pnl.get_ylim()

            ax.set_xlim(min(x0, x1), max(x0, x1))
            ax.set_ylim(min(y0, y1), max(y0, y1))

        self._zoom_start = None
        self.canvas.draw_idle()

    def _reset_zoom(self):
        """重置缩放到原始范围"""
        if self._original_xlim_price is not None:
            self.ax_price.set_xlim(self._original_xlim_price)
            self.ax_price.set_ylim(self._original_ylim_price)
            self.ax_pnl.set_xlim(self._original_xlim_pnl)
            self.ax_pnl.set_ylim(self._original_ylim_pnl)
            self.canvas.draw_idle()

    def _create_trades_table(self):
        """创建交易明细表格"""
        group = QGroupBox("交易明细")
        group.setStyleSheet(f"""
            QGroupBox {{
                font-size: {int(16 * self.font_scale)}px;
                font-weight: bold;
                background-color: #2d2d2d;
                border: 2px solid #404040;
                border-radius: 8px;
                padding: {int(10 * self.font_scale)}px;
            }}
            QGroupBox::title {{
                padding: 0 {int(8 * self.font_scale)}px;
                background-color: #2d2d2d;
            }}
        """)

        layout = QVBoxLayout()

        self.trades_table = QTableWidget()
        self.trades_table.setStyleSheet(f"""
            QTableWidget {{
                background-color: #2d2d2d;
                gridline-color: #404040;
                color: #e8e8e8;
                font-size: {int(14 * self.font_scale)}px;
            }}
            QTableWidget::item {{
                padding: {int(6 * self.font_scale)}px;
                border-bottom: 1px solid #404040;
            }}
            QHeaderView::section {{
                background-color: #333333;
                color: #e8e8e8;
                font-size: {int(14 * self.font_scale)}px;
                font-weight: bold;
                padding: {int(8 * self.font_scale)}px;
                border: none;
                border-right: 1px solid #404040;
                border-bottom: 2px solid #404040;
            }}
        """)

        self.trades_table.setColumnCount(13)
        self.trades_table.setHorizontalHeaderLabels([
            "交易时间", "交易方向", "成交价格", "成交数量",
            "成交金额", "手续费", "已实现盈亏", "累计已实现", "浮动盈亏", "总盈亏",
            "持仓资金", "可用资金", "总资产"
        ])
        self.trades_table.setAlternatingRowColors(True)
        header = self.trades_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        column_widths = [150, 80, 90, 95, 115, 85, 105, 110, 105, 105, 125, 125, 130]
        for column, width in enumerate(column_widths):
            self.trades_table.setColumnWidth(column, int(width * self.font_scale))
        self.trades_table.setHorizontalScrollMode(QTableWidget.ScrollPerPixel)
        self.trades_table.verticalHeader().setVisible(False)

        layout.addWidget(self.trades_table)
        group.setLayout(layout)
        return group

    def _on_stock_changed(self, index):
        """股票选择变化时的处理"""
        if index < 0 or index >= len(self.stock_list):
            return

        stock_code = self.stock_list[index]
        self._load_stock_data(stock_code)

    def _load_stock_data(self, stock_code):
        """加载指定股票的数据"""
        # 筛选该股票的交易记录
        stock_trades = self.trades_df[self.trades_df['code'] == stock_code].copy()

        if stock_trades.empty:
            self.stats_label.setText("无交易记录")
            return

        # 确保datetime列是datetime类型
        if 'datetime' in stock_trades.columns:
            stock_trades['datetime'] = pd.to_datetime(stock_trades['datetime'])
            stock_trades = stock_trades.sort_values('datetime')

        # 重置索引，避免后续操作出错
        stock_trades = stock_trades.reset_index(drop=True)

        # 计算每笔交易的盈亏，同时获取最终持仓信息
        stock_trades, final_position, final_cost = self._calculate_trade_pnl(stock_trades)

        # 更新统计信息（传入持仓信息用于计算未实现盈亏）
        self._update_stats(stock_trades, stock_code, final_position, final_cost)

        # 更新交易明细表格
        self._update_trades_table(stock_trades)

        # 保存当前股票代码
        self.current_stock = stock_code

        # 汇总每日交易
        self._summarize_daily_trades(stock_trades)

        # 加载日K线数据
        daily_kline_df = self._load_daily_kline(stock_code)
        self.daily_kline_df = daily_kline_df

        # 更新图表（日K蜡烛图）
        self._update_chart(stock_trades, stock_code, daily_kline_df)

    def _summarize_daily_trades(self, stock_trades):
        """汇总每日交易"""
        self.daily_trades = {}
        for _, row in stock_trades.iterrows():
            trade_time = pd.to_datetime(row['datetime'])
            date_str = trade_time.strftime('%Y%m%d')
            if date_str not in self.daily_trades:
                self.daily_trades[date_str] = {'buys': [], 'sells': []}

            trade_info = {
                'time': trade_time,
                'price': row['price'],
                'volume': row['volume'],
                'action': row['action']
            }
            if row['action'] == 'buy':
                self.daily_trades[date_str]['buys'].append(trade_info)
            else:
                self.daily_trades[date_str]['sells'].append(trade_info)

    def _load_daily_kline(self, stock_code):
        """加载日K线数据"""
        cache_key = f"{stock_code}_1d_{self.dividend_type}"
        if cache_key in self.kline_cache:
            return self.kline_cache[cache_key]

        self.kline_load_error = None
        try:
            from khQTTools import khHistory

            # 计算需要的K线数量
            start_dt = datetime.strptime(self.start_time, '%Y%m%d')
            end_dt = datetime.strptime(self.end_time, '%Y%m%d')
            bar_count = (end_dt - start_dt).days + 30  # 多取一些
            # khHistory 的语义是不包含 current_time；报表需要包含回测结束日，
            # 因此查询截止时间向后移动一天，再按原始区间过滤。
            query_end = (end_dt + timedelta(days=1)).strftime('%Y%m%d')
            fq = {
                'front': 'pre',
                'front_ratio': 'pre',
                'back': 'post',
                'back_ratio': 'post',
                'none': 'none',
            }.get(str(self.dividend_type).lower(), 'pre')

            # 使用 khHistory 获取日K线
            data = khHistory(
                symbol_list=stock_code,
                fields=['open', 'high', 'low', 'close', 'volume'],
                bar_count=bar_count,
                fre_step='1d',
                current_time=query_end,
                fq=fq
            )

            if data and stock_code in data:
                df = data[stock_code].copy()
                if not df.empty:
                    # 筛选回测日期范围
                    df['time'] = pd.to_datetime(df['time'])
                    df = df[(df['time'] >= start_dt) & (df['time'] <= end_dt)]
                    df = df.reset_index(drop=True)

                    if not df.empty:
                        self.kline_cache[cache_key] = df
                        return df
                    self.kline_load_error = "数据库中没有回测区间内的日K线"
            else:
                self.kline_load_error = "行情数据源未返回该股票的日K线"
        except Exception as e:
            self.kline_load_error = str(e)
            logging.warning("加载 %s 日K线数据失败: %s", stock_code, e)

        return None

    def _load_intraday_kline(self, stock_code, date_str):
        """加载日内分钟K线数据"""
        cache_key = f"{stock_code}_{date_str}_{self.intraday_period}"
        if cache_key in self.kline_cache:
            return self.kline_cache[cache_key]

        try:
            from khQTTools import khHistory

            # 获取该日的分钟线数据
            # khHistory 不包含 current_time；使用收盘后时间以保留 15:00 K线。
            end_time = f"{date_str} 235959"

            # 1分钟线一天大约240根
            bar_count = 250

            data = khHistory(
                symbol_list=stock_code,
                fields=['open', 'high', 'low', 'close', 'volume'],
                bar_count=bar_count,
                fre_step=self.intraday_period,
                current_time=end_time,
                fq='pre'
            )

            if data and stock_code in data:
                df = data[stock_code].copy()
                if not df.empty:
                    df['time'] = pd.to_datetime(df['time'])
                    # 筛选当天数据
                    target_date = datetime.strptime(date_str, '%Y%m%d').date()
                    df = df[df['time'].dt.date == target_date]
                    df = df.reset_index(drop=True)

                    if not df.empty:
                        self.kline_cache[cache_key] = df
                        return df
        except Exception as e:
            print(f"加载日内K线数据失败: {e}")

        return None

    def _calculate_trade_pnl(self, stock_trades):
        """计算每笔交易的盈亏

        使用成本法计算单只股票的盈亏：
        - 买入时记录成本（金额 + 手续费）
        - 卖出时计算盈亏 = 卖出金额 - 成本 - 手续费
        - 浮动盈亏 = 当前市值 - 持仓成本
        - 总盈亏 = 累计已实现盈亏 + 浮动盈亏

        Returns:
            (stock_trades, position, total_cost): 修改后的DataFrame、最终持仓、持仓成本
        """
        # 初始化盈亏列
        stock_trades['pnl'] = 0.0
        stock_trades['cum_pnl'] = 0.0
        stock_trades['unrealized_pnl'] = 0.0
        stock_trades['total_pnl'] = 0.0

        if len(stock_trades) == 0:
            return stock_trades, 0, 0

        # 使用成本法计算单只股票的盈亏
        position = 0      # 当前持仓数量
        total_cost = 0    # 持仓总成本（金额 + 手续费）
        cum_pnl = 0       # 累计已实现盈亏

        for i in range(len(stock_trades)):
            row = stock_trades.iloc[i]
            action = row.get('action', '')
            volume = int(row.get('volume', 0))
            price = float(row.get('price', 0))
            amount = float(row.get('amount', 0))
            # 计算总手续费（佣金 + 印花税 + 过户费 + 流量费）
            commission = _trade_total_fee(row)

            if action == 'buy':
                # 买入：增加持仓和成本
                position += volume
                total_cost += amount + commission
                pnl = 0  # 买入时盈亏为0
            elif action == 'sell':
                # 卖出：计算盈亏
                if position > 0 and total_cost > 0:
                    # 计算卖出部分的成本（按比例分摊）
                    sell_cost = (total_cost / position) * volume
                    # 盈亏 = 卖出金额 - 卖出成本 - 手续费
                    pnl = amount - sell_cost - commission
                    # 更新剩余持仓成本
                    total_cost -= sell_cost
                else:
                    pnl = 0
                position -= volume
                if position <= 0:
                    position = 0
                    total_cost = 0
            else:
                pnl = 0

            cum_pnl += pnl

            # 计算浮动盈亏（当前市值 - 持仓成本）
            market_value = position * price
            unrealized_pnl = market_value - total_cost if position > 0 else 0

            # 总盈亏 = 累计已实现 + 浮动
            total_pnl = cum_pnl + unrealized_pnl

            stock_trades.loc[i, 'pnl'] = pnl
            stock_trades.loc[i, 'cum_pnl'] = cum_pnl
            stock_trades.loc[i, 'unrealized_pnl'] = unrealized_pnl
            stock_trades.loc[i, 'total_pnl'] = total_pnl

        return stock_trades, position, total_cost

    def _update_stats(self, stock_trades, stock_code, final_position=0, final_cost=0):
        """更新统计信息"""
        import numpy as np

        total_trades = len(stock_trades)
        buy_trades = len(stock_trades[stock_trades['action'] == 'buy'])
        sell_trades = len(stock_trades[stock_trades['action'] == 'sell'])

        # 计算已实现盈亏（累计盈亏的最后一个值）
        if len(stock_trades) > 0 and 'cum_pnl' in stock_trades.columns:
            realized_pnl = stock_trades['cum_pnl'].iloc[-1]
        else:
            realized_pnl = stock_trades['pnl'].sum()

        # 计算未实现盈亏（如果有持仓）
        unrealized_pnl = 0
        if final_position > 0 and self.daily_stats_df is not None and len(self.daily_stats_df) > 0:
            # 从 positions 字段中获取该股票的市值
            try:
                if 'positions' in self.daily_stats_df.columns:
                    last_positions = self.daily_stats_df['positions'].iloc[-1]
                    if isinstance(last_positions, str):
                        pos_dict = eval(last_positions)
                    else:
                        pos_dict = last_positions

                    if stock_code in pos_dict:
                        stock_pos = pos_dict[stock_code]
                        # 使用 market_value - final_cost 计算浮动盈亏（与 summary 一致）
                        if 'market_value' in stock_pos:
                            market_value = float(stock_pos['market_value'])
                            unrealized_pnl = market_value - final_cost
            except Exception:
                pass

        # 如果未能从 positions 获取，使用 K线收盘价计算
        if unrealized_pnl == 0 and final_position > 0:
            if self.daily_kline_df is not None and len(self.daily_kline_df) > 0:
                last_price = self.daily_kline_df['close'].iloc[-1]
                market_value = final_position * last_price
                unrealized_pnl = market_value - final_cost

        # 总盈亏 = 已实现 + 未实现
        total_pnl = realized_pnl + unrealized_pnl

        # 计算胜率 - 只统计卖出交易中盈利的次数
        sell_trades_df = stock_trades[stock_trades['action'] == 'sell']
        if len(sell_trades_df) > 0:
            profitable_sells = len(sell_trades_df[sell_trades_df['pnl'] > 0])
            win_rate = (profitable_sells / len(sell_trades_df) * 100)
        else:
            win_rate = 0

        # 格式化显示 - 红色代表盈利，绿色代表亏损
        pnl_color = "#F44336" if total_pnl >= 0 else "#4CAF50"

        # 构建统计信息文本
        stats_text = f"共 {total_trades} 笔交易 (买入: {buy_trades}, 卖出: {sell_trades}) | "
        stats_text += f"<span style='color: {pnl_color};'>总盈亏: {total_pnl:+,.2f}</span>"

        # 如果有持仓，显示已实现和未实现盈亏
        if final_position > 0:
            realized_color = "#F44336" if realized_pnl >= 0 else "#4CAF50"
            unrealized_color = "#F44336" if unrealized_pnl >= 0 else "#4CAF50"
            stats_text += f" (<span style='color: {realized_color};'>已实现: {realized_pnl:+,.2f}</span>, "
            stats_text += f"<span style='color: {unrealized_color};'>浮动: {unrealized_pnl:+,.2f}</span>)"

        stats_text += f" | 胜率: {win_rate:.1f}%"

        self.stats_label.setText(stats_text)

    def _update_trades_table(self, stock_trades):
        """更新交易明细表格"""
        self.trades_table.setRowCount(len(stock_trades))

        for i, (idx, row) in enumerate(stock_trades.iterrows()):
            # 交易时间
            trade_datetime = pd.to_datetime(row.get('datetime', ''), errors='coerce')
            if pd.isna(trade_datetime):
                datetime_str = str(row.get('datetime', ''))
            elif trade_datetime.time() == datetime.min.time():
                datetime_str = trade_datetime.strftime('%Y-%m-%d')
            else:
                datetime_str = trade_datetime.strftime('%Y-%m-%d %H:%M:%S')
            self.trades_table.setItem(i, 0, QTableWidgetItem(datetime_str))

            # 交易方向 - 红色代表买入，蓝色代表卖出
            action = row.get('action', '')
            action_item = QTableWidgetItem("买入" if action == 'buy' else "卖出")
            action_item.setForeground(QColor("#F44336" if action == 'buy' else "#2196F3"))
            self.trades_table.setItem(i, 1, action_item)

            # 成交价格
            price = float(row.get('price', 0))
            self.trades_table.setItem(i, 2, QTableWidgetItem(f"{price:.3f}"))

            # 成交数量
            volume = int(row.get('volume', 0))
            self.trades_table.setItem(i, 3, QTableWidgetItem(f"{volume:,}"))

            # 成交金额
            amount = float(row.get('amount', 0))
            self.trades_table.setItem(i, 4, QTableWidgetItem(f"{amount:,.2f}"))

            # 手续费
            commission = _trade_total_fee(row)
            self.trades_table.setItem(i, 5, QTableWidgetItem(f"{commission:.2f}"))

            # 已实现盈亏 - 红色代表盈利，绿色代表亏损
            pnl = float(row.get('pnl', 0))
            pnl_item = QTableWidgetItem(f"{pnl:+,.2f}")
            pnl_item.setForeground(QColor("#F44336" if pnl >= 0 else "#4CAF50"))
            self.trades_table.setItem(i, 6, pnl_item)

            # 累计已实现 - 红色代表盈利，绿色代表亏损
            cum_pnl = float(row.get('cum_pnl', 0))
            cum_pnl_item = QTableWidgetItem(f"{cum_pnl:+,.2f}")
            cum_pnl_item.setForeground(QColor("#F44336" if cum_pnl >= 0 else "#4CAF50"))
            self.trades_table.setItem(i, 7, cum_pnl_item)

            # 浮动盈亏 - 红色代表盈利，绿色代表亏损
            unrealized_pnl = float(row.get('unrealized_pnl', 0))
            unrealized_item = QTableWidgetItem(f"{unrealized_pnl:+,.2f}")
            unrealized_item.setForeground(QColor("#F44336" if unrealized_pnl >= 0 else "#4CAF50"))
            self.trades_table.setItem(i, 8, unrealized_item)

            # 总盈亏 - 红色代表盈利，绿色代表亏损
            total_pnl = float(row.get('total_pnl', 0))
            total_pnl_item = QTableWidgetItem(f"{total_pnl:+,.2f}")
            total_pnl_item.setForeground(QColor("#F44336" if total_pnl >= 0 else "#4CAF50"))
            self.trades_table.setItem(i, 9, total_pnl_item)

            # 持仓资金
            market_value = float(row.get('market_value', 0.0)) if pd.notna(row.get('market_value')) else 0.0
            market_value_item = QTableWidgetItem(f"{market_value:,.2f}")
            market_value_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.trades_table.setItem(i, 10, market_value_item)

            # 可用资金
            cash = float(row.get('cash', 0.0)) if pd.notna(row.get('cash')) else 0.0
            cash_item = QTableWidgetItem(f"{cash:,.2f}")
            cash_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.trades_table.setItem(i, 11, cash_item)

            # 总资产
            total_asset = float(row.get('total_asset', 0.0)) if pd.notna(row.get('total_asset')) else 0.0
            total_asset_item = QTableWidgetItem(f"{total_asset:,.2f}")
            total_asset_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.trades_table.setItem(i, 12, total_asset_item)

    def _update_chart(self, stock_trades, stock_code, kline_df=None):
        """更新图表 - 绘制日K蜡烛图"""
        # 清除旧图表
        self.ax_price.clear()
        self.ax_pnl.clear()

        # 重新创建悬停提示框（因为 clear() 会清除它）
        self._hover_annotation = self.ax_price.annotate(
            '', xy=(0, 0), xytext=(10, 10),
            textcoords='offset points',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#1a1a1a', edgecolor='#606060', alpha=0.95),
            fontsize=9, color='#e8e8e8', visible=False, zorder=100
        )
        self._last_hover_idx = None

        if stock_trades.empty:
            self.canvas.draw()
            return

        # 获取交易数据 - 使用总盈亏（已实现+浮动）
        total_pnl = stock_trades['total_pnl'].values

        chart_title = f'{stock_code} 日K线 (框选放大/右键还原/点击日内)'
        if kline_df is not None and not kline_df.empty:
            kline_df = kline_df.reset_index(drop=True)
            n_bars = len(kline_df)

            # 绘制蜡烛图
            self._draw_candlesticks(self.ax_price, kline_df)

            # 设置Y轴范围（需要在标记买卖点之前）
            price_min = kline_df['low'].min()
            price_max = kline_df['high'].max()
            price_range = price_max - price_min if price_max != price_min else 1
            offset = price_range * 0.15  # 留出标注空间
            self.ax_price.set_ylim(price_min - offset, price_max + offset)
            self.ax_price.set_xlim(-0.5, n_bars - 0.5)

            # 标记买卖点
            self._draw_trade_markers(self.ax_price, kline_df, self.daily_trades)

            # 设置X轴标签
            self._set_daily_axis_labels(self.ax_price, kline_df)

            # 计算每日总盈亏曲线（使用每日收盘价计算浮动盈亏）
            daily_total_pnl = self._calculate_daily_total_pnl(stock_trades, kline_df)

            if len(daily_total_pnl) > 0:
                pnl_indices = list(range(len(daily_total_pnl)))
                pnl_values = list(daily_total_pnl.values())

                # 绘制总盈亏
                self.ax_pnl.fill_between(pnl_indices, 0, pnl_values,
                                         where=[p >= 0 for p in pnl_values],
                                         color='#F44336', alpha=0.3, step='post')
                self.ax_pnl.fill_between(pnl_indices, 0, pnl_values,
                                         where=[p < 0 for p in pnl_values],
                                         color='#4CAF50', alpha=0.3, step='post')
                self.ax_pnl.step(pnl_indices, pnl_values, color='#FFA500', linewidth=2,
                                label='总盈亏', where='post')
            self.ax_pnl.axhline(y=0, color='#808080', linestyle='--', alpha=0.5)
            self._set_daily_axis_labels(self.ax_pnl, kline_df)
        else:
            # 降级方案：明确标注只显示成交点，避免两点折线被误认为K线。
            trade_prices = stock_trades['price'].values
            x_indices = list(range(len(trade_prices)))
            colors = ['#F44336' if action == 'buy' else '#2196F3'
                      for action in stock_trades['action']]
            self.ax_price.scatter(x_indices, trade_prices, c=colors, s=48, zorder=3)
            self.ax_pnl.step(x_indices, total_pnl, color='#FFA500', linewidth=2, where='post')
            labels = [pd.to_datetime(value).strftime('%m-%d')
                      for value in stock_trades['datetime']]
            for ax in (self.ax_price, self.ax_pnl):
                ax.set_xticks(x_indices)
                ax.set_xticklabels(labels)
            reason = self.kline_load_error or '未取得回测区间内的日K线'
            if len(reason) > 80:
                reason = reason[:77] + '...'
            self.ax_price.text(
                0.5, 0.08,
                f'日K线加载失败，仅显示成交点\n{reason}',
                transform=self.ax_price.transAxes,
                ha='center', va='bottom', color='#FFB74D', fontsize=9,
                bbox=dict(boxstyle='round,pad=0.45', facecolor='#332B20',
                          edgecolor='#7A5A2A', alpha=0.95),
            )
            chart_title = f'{stock_code} 成交记录（日K线未加载）'

        self.ax_price.set_title(chart_title,
                                color='#e8e8e8', fontsize=int(11 * self.font_scale), fontweight='bold', pad=8)
        self.ax_price.set_ylabel('价格', color='#a0a0a0', fontsize=int(10 * self.font_scale), labelpad=2)

        self.ax_pnl.set_ylabel('盈亏', color='#a0a0a0', fontsize=int(9 * self.font_scale), labelpad=2)
        self.ax_pnl.legend(loc='upper left', fontsize=int(8 * self.font_scale))

        for ax in [self.ax_price, self.ax_pnl]:
            ax.set_facecolor('#2d2d2d')
            ax.tick_params(axis='both', colors='#a0a0a0', labelsize=int(9 * self.font_scale))
            ax.grid(True, linestyle='--', alpha=0.1, color='#808080')
            for spine in ax.spines.values():
                spine.set_color('#404040')

        # 添加水印到价格图
        self.ax_price.text(0.5, 0.5, 'khQuant',
                          horizontalalignment='center', verticalalignment='center',
                          transform=self.ax_price.transAxes, fontsize=60, alpha=0.1,
                          color='#888888', fontweight='bold',
                          zorder=0)

        # 保存原始视图范围
        self._original_xlim_price = self.ax_price.get_xlim()
        self._original_ylim_price = self.ax_price.get_ylim()
        self._original_xlim_pnl = self.ax_pnl.get_xlim()
        self._original_ylim_pnl = self.ax_pnl.get_ylim()

        self.canvas.draw()

    def _draw_candlesticks(self, ax, kline_df):
        """绘制蜡烛图"""
        width = 0.6
        for idx, row in kline_df.iterrows():
            open_p, high, low, close = row['open'], row['high'], row['low'], row['close']

            # 涨跌颜色：红涨绿跌
            if close >= open_p:
                color = '#F44336'  # 红色
                body_bottom = open_p
                body_height = close - open_p
            else:
                color = '#4CAF50'  # 绿色
                body_bottom = close
                body_height = open_p - close

            # 绘制影线
            ax.plot([idx, idx], [low, high], color=color, linewidth=1)

            # 绘制实体
            rect = Rectangle((idx - width/2, body_bottom), width, max(body_height, 0.001),
                             facecolor=color, edgecolor=color)
            ax.add_patch(rect)

    def _draw_trade_markers(self, ax, kline_df, daily_trades):
        """绘制买卖点标记 - 简洁风格：小三角+字母"""
        y_range = ax.get_ylim()
        price_range = y_range[1] - y_range[0] if y_range[1] != y_range[0] else 1
        offset = price_range * 0.03
        text_offset = price_range * 0.015

        # 配色方案：买入橙色，卖出青色
        buy_color = '#FF9800'   # 橙色
        sell_color = '#00BCD4'  # 青色

        for idx, row in kline_df.iterrows():
            date_str = pd.to_datetime(row['time']).strftime('%Y%m%d')

            if date_str in daily_trades:
                trades = daily_trades[date_str]
                low_price = row['low']
                high_price = row['high']

                # 买入标记：向上三角 + B字母
                if trades['buys']:
                    marker_y = low_price - offset
                    ax.plot(idx, marker_y, marker='^', markersize=7,
                           color=buy_color, markeredgecolor='white', markeredgewidth=0.8)
                    ax.text(idx, marker_y - text_offset, 'B', color=buy_color,
                           fontsize=7, fontweight='bold', ha='center', va='top')

                # 卖出标记：向下三角 + S字母
                if trades['sells']:
                    marker_y = high_price + offset
                    ax.plot(idx, marker_y, marker='v', markersize=7,
                           color=sell_color, markeredgecolor='white', markeredgewidth=0.8)
                    ax.text(idx, marker_y + text_offset, 'S', color=sell_color,
                           fontsize=7, fontweight='bold', ha='center', va='bottom')

    def _calculate_daily_total_pnl(self, stock_trades, kline_df):
        """计算每日总盈亏（已实现+浮动），使用每日收盘价"""
        from collections import OrderedDict

        daily_pnl = OrderedDict()  # {kline_idx: total_pnl}

        # 按日期聚合交易，计算每日结束时的持仓和累计已实现盈亏
        position = 0
        total_cost = 0
        cum_realized_pnl = 0

        # 创建日期到K线索引的映射
        kline_date_map = {}
        for idx, row in kline_df.iterrows():
            date_str = pd.to_datetime(row['time']).strftime('%Y%m%d')
            kline_date_map[date_str] = idx

        # 按日期分组交易
        trade_by_date = {}
        for _, trade in stock_trades.iterrows():
            trade_date = pd.to_datetime(trade['datetime']).strftime('%Y%m%d')
            if trade_date not in trade_by_date:
                trade_by_date[trade_date] = []
            trade_by_date[trade_date].append(trade)

        # 遍历每个K线日期计算总盈亏
        for idx, row in kline_df.iterrows():
            date_str = pd.to_datetime(row['time']).strftime('%Y%m%d')
            close_price = row['close']

            # 处理当日交易
            if date_str in trade_by_date:
                for trade in trade_by_date[date_str]:
                    action = trade['action']
                    volume = trade['volume']
                    price = trade['price']
                    amount = trade['amount']
                    commission = _trade_total_fee(trade)

                    if action == 'buy':
                        total_cost += amount + commission
                        position += volume
                    elif action == 'sell':
                        if position > 0:
                            sell_cost = (total_cost / position) * volume
                            pnl = amount - sell_cost - commission
                            cum_realized_pnl += pnl
                            total_cost -= sell_cost
                        position -= volume
                        if position <= 0:
                            position = 0
                            total_cost = 0

            # 计算当日收盘时的浮动盈亏
            if position > 0:
                market_value = position * close_price
                unrealized_pnl = market_value - total_cost
            else:
                unrealized_pnl = 0

            # 总盈亏 = 累计已实现 + 浮动
            daily_pnl[idx] = cum_realized_pnl + unrealized_pnl

        return daily_pnl

    def _set_daily_axis_labels(self, ax, kline_df):
        """设置日K线X轴标签"""
        n_points = len(kline_df)
        if n_points == 0:
            return

        n_labels = min(8, n_points)  # 减少标签数量，避免拥挤
        step = max(1, n_points // n_labels)

        tick_positions = list(range(0, n_points, step))
        tick_labels = [pd.to_datetime(kline_df.iloc[pos]['time']).strftime('%m-%d')
                      for pos in tick_positions if pos < len(kline_df)]

        ax.set_xticks(tick_positions[:len(tick_labels)])
        ax.set_xticklabels(tick_labels, rotation=0, ha='center')  # 水平显示

    def _show_intraday_chart(self, date_str):
        """显示日内分钟图"""
        if not self.current_stock:
            return

        # 加载日内K线数据
        intraday_df = self._load_intraday_kline(self.current_stock, date_str)

        if intraday_df is None or intraday_df.empty:
            QMessageBox.warning(self, "数据缺失",
                f"无法获取 {date_str} 的{self.intraday_period}分钟线数据。\n"
                "请确保已下载该日期的分钟线数据。")
            return

        # 创建日内图窗口
        dialog = IntradayChartDialog(
            self.current_stock, date_str, intraday_df,
            self.daily_trades.get(date_str, {'buys': [], 'sells': []}),
            self.intraday_period, self.font_scale, self
        )
        dialog.exec_()

    def _apply_dark_theme(self):
        """应用深色主题"""
        self.setStyleSheet(f"""
            QMainWindow {{
                background-color: #2b2b2b;
            }}
            QWidget {{
                background-color: #2b2b2b;
                color: #e8e8e8;
            }}
            QLabel {{
                color: #e8e8e8;
            }}
            QGroupBox {{
                color: #e8e8e8;
            }}
            QTableWidget {{
                alternate-background-color: #333333;
            }}
        """)


class IntradayChartDialog(QDialog):
    """日内分钟图对话框"""

    def __init__(self, stock_code, date_str, kline_df, trades, period, font_scale, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.stock_code = stock_code
        self.date_str = date_str
        self.kline_df = kline_df
        self.trades = trades
        self.period = period
        self.font_scale = font_scale

        self._setup_dark_titlebar()
        self._init_ui()

    def _setup_dark_titlebar(self):
        """设置深色标题栏"""
        try:
            from ctypes import windll, c_int, byref, sizeof
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            DWMWA_CAPTION_COLOR = 35
            windll.dwmapi.DwmSetWindowAttribute(
                int(self.winId()), DWMWA_USE_IMMERSIVE_DARK_MODE,
                byref(c_int(2)), sizeof(c_int))
            caption_color = c_int(0x2b2b2b)
            windll.dwmapi.DwmSetWindowAttribute(
                int(self.winId()), DWMWA_CAPTION_COLOR,
                byref(caption_color), sizeof(caption_color))
        except:
            pass

    def _init_ui(self):
        """初始化UI"""
        self.date_display = f"{self.date_str[:4]}-{self.date_str[4:6]}-{self.date_str[6:]}"
        self.setWindowTitle(f"{self.stock_code} {self.date_display} 日内走势 ({self.period})")
        self.resize(int(900 * self.font_scale), int(500 * self.font_scale))

        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)

        # 创建matplotlib图表
        self.figure = Figure(figsize=(10, 5), facecolor='#2d2d2d')
        self.canvas = FigureCanvas(self.figure)

        self.ax = self.figure.add_subplot(111)
        self.ax.set_position([0.06, 0.10, 0.92, 0.82])  # 调整位置确保标题和标签显示完整
        self.ax.set_facecolor('#2d2d2d')

        # 绘制分钟K线
        self._draw_intraday_chart(self.ax)

        # 保存原始视图范围
        self._original_xlim = self.ax.get_xlim()
        self._original_ylim = self.ax.get_ylim()

        # 框选缩放相关变量
        self._zoom_rect = None
        self._zoom_start = None

        # 悬停提示框
        self._hover_annotation = self.ax.annotate(
            '', xy=(0, 0), xytext=(10, 10),
            textcoords='offset points',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#1a1a1a', edgecolor='#606060', alpha=0.95),
            fontsize=9, color='#e8e8e8', visible=False, zorder=100
        )
        self._last_hover_idx = None

        # 保存K线数据用于悬停提示
        self._kline_df_indexed = self.kline_df.reset_index(drop=True)

        # 连接鼠标事件
        self.canvas.mpl_connect('button_press_event', self._on_mouse_press)
        self.canvas.mpl_connect('button_release_event', self._on_mouse_release)
        self.canvas.mpl_connect('motion_notify_event', self._on_mouse_move)
        self.canvas.mpl_connect('resize_event', self._on_canvas_resize)

        layout.addWidget(self.canvas)

        self.setLayout(layout)
        self.setStyleSheet("""
            QDialog { background-color: #2b2b2b; }
            QWidget { background-color: #2b2b2b; color: #e8e8e8; }
        """)

        self.canvas.draw()

    def closeEvent(self, event):
        """分钟图是临时窗口，关闭后立即释放 Matplotlib 资源。"""
        try:
            if getattr(self, 'canvas', None) is not None:
                self.canvas.close()
            if getattr(self, 'figure', None) is not None:
                self.figure.clear()
                plt.close(self.figure)
            self.kline_df = None
            self.trades = {}
        except Exception as exc:
            logging.warning(f"关闭分钟图时释放资源失败: {exc}")
        event.accept()

    def _on_canvas_resize(self, event):
        """画布大小改变时调整布局"""
        if event.width > 0 and event.height > 0:
            left_margin = max(0.05, 50 / event.width)
            right_margin = 0.98
            width = right_margin - left_margin
            self.ax.set_position([left_margin, 0.10, width, 0.82])

    def _on_mouse_press(self, event):
        """鼠标按下事件"""
        if event.inaxes != self.ax:
            return
        if event.button == 1:  # 左键 - 开始框选
            self._zoom_start = (event.xdata, event.ydata)
        elif event.button == 3:  # 右键 - 恢复原始视图
            self.ax.set_xlim(self._original_xlim)
            self.ax.set_ylim(self._original_ylim)
            self.canvas.draw_idle()

    def _on_mouse_move(self, event):
        """鼠标移动事件 - 框选和悬停提示"""
        # 处理框选
        if self._zoom_start is not None and event.inaxes == self.ax:
            x0, y0 = self._zoom_start
            x1, y1 = event.xdata, event.ydata
            if x1 is not None and y1 is not None:
                if self._zoom_rect is not None:
                    self._zoom_rect.remove()
                self._zoom_rect = Rectangle((x0, y0), x1 - x0, y1 - y0,
                                             fill=False, edgecolor='#00BFFF',
                                             linestyle='--', linewidth=1.5)
                self.ax.add_patch(self._zoom_rect)
                self.canvas.draw_idle()
            return

        # 处理悬停提示
        if event.inaxes == self.ax and event.xdata is not None:
            idx = int(round(event.xdata))
            if 0 <= idx < len(self._kline_df_indexed) and idx != self._last_hover_idx:
                self._last_hover_idx = idx
                row = self._kline_df_indexed.iloc[idx]
                time_str = pd.to_datetime(row['time']).strftime('%H:%M')

                # 构建提示信息
                info_lines = [f"时间: {time_str}"]
                info_lines.append(f"开: {row['open']:.3f}  高: {row['high']:.3f}")
                info_lines.append(f"低: {row['low']:.3f}  收: {row['close']:.3f}")
                change = (row['close'] - row['open']) / row['open'] * 100 if row['open'] != 0 else 0
                change_color = '↑' if change >= 0 else '↓'
                info_lines.append(f"涨跌: {change_color} {abs(change):.2f}%")

                # 检查该时间点是否有交易
                kline_time = pd.to_datetime(row['time'])
                for trade in self.trades.get('buys', []):
                    trade_time = pd.to_datetime(trade['time'])
                    if abs((trade_time - kline_time).total_seconds()) < 60:
                        info_lines.append(f"买入: {trade['volume']}股@{trade['price']:.3f}")
                for trade in self.trades.get('sells', []):
                    trade_time = pd.to_datetime(trade['time'])
                    if abs((trade_time - kline_time).total_seconds()) < 60:
                        info_lines.append(f"卖出: {trade['volume']}股@{trade['price']:.3f}")

                self._hover_annotation.set_text('\n'.join(info_lines))
                self._hover_annotation.xy = (idx, row['high'])
                self._hover_annotation.set_visible(True)
                self.canvas.draw_idle()
        elif self._hover_annotation.get_visible():
            self._hover_annotation.set_visible(False)
            self._last_hover_idx = None
            self.canvas.draw_idle()

    def _on_mouse_release(self, event):
        """鼠标释放事件"""
        if self._zoom_start is None or event.button != 1:
            self._zoom_start = None
            return
        x0, y0 = self._zoom_start
        x1, y1 = event.xdata, event.ydata
        if self._zoom_rect is not None:
            self._zoom_rect.remove()
            self._zoom_rect = None
        if x1 is None or y1 is None:
            self._zoom_start = None
            self.canvas.draw_idle()
            return
        if abs(x1 - x0) > 0.5 and abs(y1 - y0) > 0.001:
            self.ax.set_xlim(min(x0, x1), max(x0, x1))
            self.ax.set_ylim(min(y0, y1), max(y0, y1))
        self._zoom_start = None
        self.canvas.draw_idle()

    def _draw_intraday_chart(self, ax):
        """绘制日内分钟图"""
        kline_df = self.kline_df.reset_index(drop=True)
        n_bars = len(kline_df)

        if n_bars == 0:
            return

        # 绘制蜡烛图
        width = 0.6
        for idx, row in kline_df.iterrows():
            open_p, high, low, close = row['open'], row['high'], row['low'], row['close']
            color = '#F44336' if close >= open_p else '#4CAF50'
            body_bottom = min(open_p, close)
            body_height = abs(close - open_p)

            ax.plot([idx, idx], [low, high], color=color, linewidth=0.8)
            rect = Rectangle((idx - width/2, body_bottom), width, max(body_height, 0.0001),
                             facecolor=color, edgecolor=color)
            ax.add_patch(rect)

        # 获取价格范围用于计算标注偏移
        ax.set_xlim(-1, n_bars)
        all_lows = kline_df['low'].values
        all_highs = kline_df['high'].values
        price_min, price_max = all_lows.min(), all_highs.max()
        price_range = price_max - price_min if price_max != price_min else 1
        offset = price_range * 0.08
        ax.set_ylim(price_min - offset * 2, price_max + offset * 2)

        # 标记买卖点 - 简洁风格：小三角+字母
        kline_times = kline_df['time'].values
        buy_color = '#FF9800'   # 橙色
        sell_color = '#00BCD4'  # 青色
        marker_offset = price_range * 0.04
        text_offset = price_range * 0.02

        for trade in self.trades.get('buys', []):
            trade_time = pd.to_datetime(trade['time'])
            time_diffs = abs(pd.to_datetime(kline_times) - trade_time)
            closest_idx = time_diffs.argmin()
            low_price = kline_df.iloc[closest_idx]['low']
            marker_y = low_price - marker_offset
            ax.plot(closest_idx, marker_y, marker='^', markersize=8,
                   color=buy_color, markeredgecolor='white', markeredgewidth=0.8)
            ax.text(closest_idx, marker_y - text_offset, 'B', color=buy_color,
                   fontsize=8, fontweight='bold', ha='center', va='top')

        for trade in self.trades.get('sells', []):
            trade_time = pd.to_datetime(trade['time'])
            time_diffs = abs(pd.to_datetime(kline_times) - trade_time)
            closest_idx = time_diffs.argmin()
            high_price = kline_df.iloc[closest_idx]['high']
            marker_y = high_price + marker_offset
            ax.plot(closest_idx, marker_y, marker='v', markersize=8,
                   color=sell_color, markeredgecolor='white', markeredgewidth=0.8)
            ax.text(closest_idx, marker_y + text_offset, 'S', color=sell_color,
                   fontsize=8, fontweight='bold', ha='center', va='bottom')

        # 设置X轴标签
        n_labels = min(8, n_bars)
        step = max(1, n_bars // n_labels)
        tick_positions = list(range(0, n_bars, step))
        tick_labels = [pd.to_datetime(kline_df.iloc[pos]['time']).strftime('%H:%M')
                      for pos in tick_positions if pos < n_bars]

        ax.set_xticks(tick_positions[:len(tick_labels)])
        ax.set_xticklabels(tick_labels, rotation=0, ha='center')  # 水平显示

        ax.set_title(f'{self.stock_code} {self.date_display} 日内 (框选放大/右键还原)',
                    color='#e8e8e8', fontsize=int(11 * self.font_scale), fontweight='bold', pad=8)
        ax.set_ylabel('价格', color='#a0a0a0', fontsize=int(10 * self.font_scale), labelpad=2)
        ax.set_xlabel('时间', color='#a0a0a0', fontsize=int(10 * self.font_scale), labelpad=2)
        ax.tick_params(axis='both', colors='#a0a0a0', labelsize=int(9 * self.font_scale))
        ax.grid(True, linestyle='--', alpha=0.1, color='#808080')
        for spine in ax.spines.values():
            spine.set_color('#404040')

        # 添加水印
        ax.text(0.5, 0.5, 'khQuant',
               horizontalalignment='center', verticalalignment='center',
               transform=ax.transAxes, fontsize=60, alpha=0.1,
               color='#888888', fontweight='bold',
               zorder=0)
