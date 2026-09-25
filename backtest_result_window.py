import logging
logging.getLogger('matplotlib').setLevel(logging.ERROR)

# 先导入 matplotlib 并设置后端
import matplotlib
matplotlib.use('Qt5Agg')

# 其他导入
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                          QLabel, QTabWidget, QTableWidget, QTableWidgetItem,
                          QGroupBox, QSplitter, QGridLayout, QHeaderView, QSizePolicy,
                          QPushButton, QMessageBox)
from PyQt5.QtCore import Qt, QSettings, QThread, pyqtSignal, QTimer, QRect, QEvent
from PyQt5.QtGui import QPalette, QColor, QIcon
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.dates as mdates
from matplotlib import ticker
import pandas as pd
import os
from datetime import datetime, timedelta
import numpy as np
import sys
import time
from khQTTools import KhQuTools, determine_pool_type
from stock_analysis_window import StockAnalysisWindow
from khUiScale import get_ui_font_scale, force_primary_screen_dpi
from qt_settings_bridge import KhQtSettings

# 设置matplotlib的字体和其他参数
plt.rcParams['font.sans-serif'] = ['PingFang SC', 'Heiti SC', 'STHeiti', 'Arial Unicode MS', 'Microsoft YaHei', 'SimHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False
plt.rcParams['font.family'] = 'sans-serif'

# 设置matplotlib深色主题
plt.style.use('dark_background')


def resolve_benchmark_base_price(benchmark_df, daily_stats_df=None):
    """基于已加载的benchmark数据计算基准初始价，不在GUI线程请求外部数据源。"""
    try:
        if benchmark_df is None or len(benchmark_df) == 0 or 'close' not in benchmark_df.columns:
            return None

        df = benchmark_df.copy()
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date')

        base_price = None
        if (
            daily_stats_df is not None and len(daily_stats_df) > 0
            and 'date' in daily_stats_df.columns and 'date' in df.columns
        ):
            backtest_start = pd.to_datetime(daily_stats_df['date']).min()
            pre_bench = df[df['date'] < backtest_start]
            if len(pre_bench) > 0:
                base_price = float(pre_bench['close'].iloc[-1])
            else:
                bt_bench = df[df['date'] >= backtest_start]
                if len(bt_bench) > 0:
                    base_price = float(bt_bench['close'].iloc[0])

        if base_price is None:
            base_price = float(df['close'].iloc[0])

        if not np.isfinite(base_price) or base_price == 0:
            return None
        return base_price
    except Exception as e:
        print(f"计算基准初始价时出错: {str(e)}")
        return None


def read_backtest_result_data(backtest_dir):
    """读取并预处理回测结果文件，供后台线程调用。"""
    backtest_dir = os.path.abspath(backtest_dir)

    if not os.path.exists(backtest_dir):
        raise FileNotFoundError(f"回测结果目录不存在: {backtest_dir}")

    config_path = os.path.join(backtest_dir, "config.csv")
    trades_path = os.path.join(backtest_dir, "trades.csv")
    daily_stats_path = os.path.join(backtest_dir, "daily_stats.csv")
    benchmark_path = os.path.join(backtest_dir, "benchmark.csv")

    print(f"尝试加载配置文件: {config_path}")
    print(f"文件是否存在: {os.path.exists(config_path)}")

    try:
        print("目录内容:")
        for file in os.listdir(backtest_dir):
            print(f"- {file}")
    except Exception as e:
        print(f"列出回测目录内容失败: {e}")

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    config_df = pd.read_csv(config_path, encoding='utf-8-sig')

    if not os.path.exists(trades_path):
        print(f"警告: 交易记录文件不存在: {trades_path}")
        trades_df = pd.DataFrame(columns=['datetime', 'code', 'action', 'price', 'volume', 'amount', 'commission'])
    else:
        trades_df = pd.read_csv(trades_path, encoding='utf-8-sig')

    price_decimals = 2
    if len(trades_df) > 0 and 'code' in trades_df.columns:
        stock_codes = trades_df['code'].unique().tolist()
        _, price_decimals = determine_pool_type(stock_codes)
        print(f"根据股票池类型设置价格精度: {price_decimals}位小数")

    if not os.path.exists(daily_stats_path):
        print(f"警告: 每日统计文件不存在: {daily_stats_path}")
        daily_stats_df = pd.DataFrame(columns=['date', 'total_asset', 'cash', 'market_value', 'daily_return'])
    else:
        daily_stats_df = pd.read_csv(daily_stats_path, encoding='utf-8-sig')
        if 'daily_return' not in daily_stats_df.columns and 'total_asset' in daily_stats_df.columns:
            print("daily_stats.csv中没有daily_return列，正在计算...")
            daily_stats_df['date'] = pd.to_datetime(daily_stats_df['date'])
            daily_stats_df = daily_stats_df.sort_values('date')
            daily_stats_df['daily_return'] = daily_stats_df['total_asset'].pct_change()
            if len(daily_stats_df) > 0:
                daily_stats_df.loc[daily_stats_df.index[0], 'daily_return'] = 0
            print(f"已计算daily_return列，共{len(daily_stats_df)}条数据")

    if not os.path.exists(benchmark_path):
        print(f"警告: 基准数据文件不存在: {benchmark_path}")
        if len(daily_stats_df) > 0 and 'date' in daily_stats_df.columns:
            daily_stats_df['date'] = pd.to_datetime(daily_stats_df['date'])
            dates = daily_stats_df['date']
            benchmark_df = pd.DataFrame({'date': dates, 'close': np.ones(len(dates))})
            print("创建了替代基准数据")
        else:
            benchmark_df = pd.DataFrame(columns=['date', 'close'])
            print("创建了空的基准数据DataFrame")
    else:
        try:
            benchmark_df = pd.read_csv(benchmark_path, encoding='utf-8-sig')
            if len(benchmark_df) == 0 or 'close' not in benchmark_df.columns or 'date' not in benchmark_df.columns:
                print("基准数据文件为空或缺少必要列")
                if len(daily_stats_df) > 0 and 'date' in daily_stats_df.columns:
                    daily_stats_df['date'] = pd.to_datetime(daily_stats_df['date'])
                    dates = daily_stats_df['date']
                    benchmark_df = pd.DataFrame({'date': dates, 'close': np.ones(len(dates))})
                    print("创建了替代基准数据")
                else:
                    benchmark_df = pd.DataFrame(columns=['date', 'close'])
                    print("创建了空的基准数据Frame")
        except Exception as e:
            print(f"读取基准数据文件时出错: {str(e)}")
            if len(daily_stats_df) > 0 and 'date' in daily_stats_df.columns:
                daily_stats_df['date'] = pd.to_datetime(daily_stats_df['date'])
                dates = daily_stats_df['date']
                benchmark_df = pd.DataFrame({'date': dates, 'close': np.ones(len(dates))})
                print("创建了替代基准数据")
            else:
                benchmark_df = pd.DataFrame(columns=['date', 'close'])
                print("创建了空的基准数据DataFrame")

    required_columns = {
        'daily_stats': ['date', 'total_asset', 'cash', 'market_value', 'daily_return'],
        'benchmark': ['date', 'close'],
        'trades': ['datetime', 'code', 'action', 'price', 'volume', 'amount', 'commission']
    }

    if 'time' in trades_df.columns:
        trades_df = trades_df.rename(columns={'time': 'datetime'})
    if 'type' in trades_df.columns:
        trades_df = trades_df.rename(columns={'type': 'action'})

    print(f"交易数据列名: {trades_df.columns.tolist()}")

    for df_name, columns in required_columns.items():
        df = {'daily_stats': daily_stats_df, 'benchmark': benchmark_df, 'trades': trades_df}[df_name]
        missing_columns = [col for col in columns if col not in df.columns]
        if missing_columns:
            print(f"警告: {df_name} 缺少必要的列: {missing_columns}，尝试调整")
            if df_name == 'trades':
                if 'datetime' not in df.columns and 'time' in df.columns:
                    print("  将'time'列重命名为'datetime'")
                    df = df.rename(columns={'time': 'datetime'})
                if 'action' not in df.columns and 'direction' in df.columns:
                    print("  将'direction'列重命名为'action'")
                    df = df.rename(columns={'direction': 'action'})
                if 'action' not in df.columns and 'type' in df.columns:
                    print("  将'type'列重命名为'action'")
                    df = df.rename(columns={'type': 'action'})
                missing_columns = [col for col in columns if col not in df.columns]
                if missing_columns:
                    print(f"  调整后仍缺少列: {missing_columns}")
                    if set(missing_columns) == {'commission'} and 'amount' in df.columns:
                        print("  添加默认的'commission'列")
                        df['commission'] = 0.0
                        missing_columns = []
                trades_df = df

            if missing_columns:
                raise ValueError(f"{df_name} 缺少必要的列: {missing_columns}")

    trades_raw_df = trades_df.copy()
    daily_stats_original_df = daily_stats_df.copy()
    trades_display_df = trades_df.rename(columns={'datetime': 'time', 'action': 'direction'})

    print(f"重命名后的交易数据列名: {trades_display_df.columns.tolist()}")

    try:
        direction_map = {'buy': '买入', 'sell': '卖出'}
        trades_display_df['direction'] = trades_display_df['direction'].map(
            lambda x: direction_map.get(str(x).lower(), x)
        )
        print("买卖动作映射完成")
    except Exception as e:
        print(f"买卖动作映射出错: {str(e)}")
        print(f"direction列值: {trades_display_df['direction'].unique().tolist() if 'direction' in trades_display_df.columns else 'direction列不存在'}")

    config_row = config_df.iloc[0]
    benchmark_code = config_row.get('benchmark', '000300.SH') if 'benchmark' in config_row.index else '000300.SH'
    print(f"使用基准合约代码: {benchmark_code}")

    # 在后台线程预热交易日缓存：update_basic_info在GUI线程计算年化收益率时
    # 会调用get_trade_days_count，若缓存未覆盖该区间会触发baostock/xtquant
    # 网络请求，把界面卡住。这里提前在本线程完成（失败也只是退回原逻辑）。
    try:
        if len(daily_stats_df) > 0 and 'date' in daily_stats_df.columns:
            from khQTTools import get_trade_days_set
            _first = pd.to_datetime(daily_stats_df['date'].iloc[0]).strftime('%Y%m%d')
            _last = pd.to_datetime(daily_stats_df['date'].iloc[-1]).strftime('%Y%m%d')
            get_trade_days_set(_first, _last)
    except Exception as e:
        print(f"预热交易日缓存失败（不影响加载）: {e}")

    return {
        'config_df': config_df,
        'config_row': config_row,
        'trades_raw_df': trades_raw_df,
        'trades_display_df': trades_display_df,
        'daily_stats_df': daily_stats_df,
        'daily_stats_original_df': daily_stats_original_df,
        'benchmark_df': benchmark_df,
        'benchmark_base_price': resolve_benchmark_base_price(benchmark_df, daily_stats_df),
        'benchmark_code': benchmark_code,
        'price_decimals': price_decimals,
    }


class BacktestResultLoadThread(QThread):
    loaded = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, backtest_dir, parent=None):
        super().__init__(parent)
        self.backtest_dir = backtest_dir

    def run(self):
        try:
            self.loaded.emit(read_backtest_result_data(self.backtest_dir))
        except Exception as e:
            import traceback
            self.error.emit(f"{str(e)}\n{traceback.format_exc()}")


class BacktestResultWindow(QMainWindow):
    def __init__(self, backtest_dir):
        super().__init__()
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.backtest_dir = backtest_dir
        self.stock_analysis_window = None
        self._closing_after_load = False

        # 检测屏幕分辨率并设置字体缩放比例
        self.font_scale = self.detect_screen_resolution()

        # 从设置读取无风险收益率和数据源配置
        settings = KhQtSettings('KHQuant', 'StockAnalyzer')
        self.risk_free_rate = float(settings.value('risk_free_rate', '0.03'))
        print(f"使用无风险收益率: {self.risk_free_rate}")

        # 结果窗口只展示回测目录中的文件，不初始化外部行情数据源，避免打开窗口时阻塞 UI。
        self.benchmark_base_price = None

        # 价格精度设置，默认2位小数（股票），ETF为3位
        self.price_decimals = 2
        
        # 设置窗口标题栏颜色（仅适用于Windows）
        if sys.platform == 'win32':
            try:
                from ctypes import windll, c_int, byref, sizeof
                
                # 定义Windows API常量
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                DWMWA_CAPTION_COLOR = 35
                
                # 启用深色模式
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_USE_IMMERSIVE_DARK_MODE,
                    byref(c_int(2)),  # 2表示启用
                    sizeof(c_int)
                )
                
                # 设置标题栏颜色
                caption_color = c_int(0x333333)  # 使用与主界面相同的颜色
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_CAPTION_COLOR,
                    byref(caption_color),
                    sizeof(caption_color)
                )
            except Exception as e:
                print(f"设置标题栏深色模式失败: {str(e)}")
        
        # 设置窗口标题
        self.setWindowTitle("回测结果分析")
        
        # 加载窗口图标
        self.load_icon()
        
        # 首次显示时是否已约束到主屏
        self._constrained_to_primary = False
        self._normal_restore_geometry = None
        self._restore_geometry_guard_active = False

        # 初始化UI和加载数据
        self.init_ui()
        self.apply_dark_theme()
        self._load_thread = None
        self._set_loading_state(True)
        QTimer.singleShot(0, self.start_async_load)

    def _get_primary_restore_geometry(self, available_geometry):
        """计算报告窗口从最大化还原后的普通窗口尺寸。"""
        try:
            margin_x = max(40, int(available_geometry.width() * 0.04))
            margin_top = max(70, int(available_geometry.height() * 0.07))
            margin_bottom = max(50, int(available_geometry.height() * 0.05))
            usable_width = max(420, available_geometry.width() - margin_x * 2)
            usable_height = max(360, available_geometry.height() - margin_top - margin_bottom)

            target_width = min(max(int(available_geometry.width() * 0.82), 900), usable_width)
            target_height = min(max(int(available_geometry.height() * 0.78), 620), usable_height)

            x = available_geometry.x() + margin_x + (usable_width - target_width) // 2
            y = available_geometry.y() + margin_top + (usable_height - target_height) // 2
            return QRect(x, y, target_width, target_height)
        except Exception:
            return QRect(80, 80, 1200, 800)

    def _apply_safe_normal_geometry(self):
        """最大化还原后强制回到主屏可用区，避免标题栏跑出屏幕。"""
        if self._restore_geometry_guard_active:
            return
        if self.isMaximized() or self.isMinimized() or (self.windowState() & Qt.WindowFullScreen):
            return

        self._restore_geometry_guard_active = True
        try:
            from PyQt5.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
            if screen is None:
                return

            avail = screen.availableGeometry()
            target = self._normal_restore_geometry
            if (
                target is None
                or target.width() >= avail.width()
                or target.height() >= avail.height()
                or not avail.adjusted(0, 0, -1, -1).contains(target.topLeft())
            ):
                target = self._get_primary_restore_geometry(avail)
                self._normal_restore_geometry = target

            self.setGeometry(target)

            # setGeometry 作用在客户区，Windows 的标题栏边框可能仍会让 frame 略微越界；
            # 这里按 frame 再校正一次，确保标题栏留在可见区域。
            frame = self.frameGeometry()
            new_pos = self.pos()
            min_top = avail.top() + 8
            if frame.top() < min_top:
                new_pos.setY(new_pos.y() + (min_top - frame.top()))
            if frame.left() < avail.left() + 8:
                new_pos.setX(new_pos.x() + (avail.left() + 8 - frame.left()))
            if frame.right() > avail.right() - 8:
                new_pos.setX(new_pos.x() - (frame.right() - (avail.right() - 8)))
            if frame.bottom() > avail.bottom() - 8:
                new_pos.setY(new_pos.y() - (frame.bottom() - (avail.bottom() - 8)))
            self.move(new_pos)
        finally:
            self._restore_geometry_guard_active = False

    def _constrain_to_primary_screen(self):
        """在主屏上最大化显示报告窗口。

        先把窗口移动到主屏，再最大化，避免多显示器下报告窗口
        横跨主屏和副屏，或在副屏最大化。
        """
        try:
            from PyQt5.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
            if screen is None:
                self.showMaximized()
                return
            avail = screen.availableGeometry()

            # 先设置一个安全的普通窗口尺寸，再最大化。这样用户点击系统
            # “还原”按钮时会回到可用区内的普通尺寸，而不是仍然占满屏幕。
            self._normal_restore_geometry = self._get_primary_restore_geometry(avail)
            self.setGeometry(self._normal_restore_geometry)
            self.showMaximized()
        except Exception as e:
            print(f"在主屏最大化报告窗口失败: {str(e)}")
            try:
                self.showMaximized()
            except Exception:
                pass

    def showEvent(self, event):
        super().showEvent(event)
        if not self._constrained_to_primary:
            self._constrained_to_primary = True
            self._constrain_to_primary_screen()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            old_state = event.oldState() if hasattr(event, "oldState") else Qt.WindowNoState
            now_state = self.windowState()
            restored_from_maximized = bool(old_state & Qt.WindowMaximized) and not bool(now_state & Qt.WindowMaximized)
            restored_from_fullscreen = bool(old_state & Qt.WindowFullScreen) and not bool(now_state & Qt.WindowFullScreen)
            if restored_from_maximized or restored_from_fullscreen:
                QTimer.singleShot(0, self._apply_safe_normal_geometry)
                QTimer.singleShot(80, self._apply_safe_normal_geometry)

    def load_icon(self):
        """加载窗口图标"""
        try:
            # 首先尝试直接加载相对路径
            icon_path = "./icons/stock_icon.png"
            
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
                print(f"成功加载图标: {icon_path}")
                return True
                
            
        except Exception as e:
            print(f"加载图标时出错: {str(e)}")
            return False
    
    def detect_screen_resolution(self):
        """检测屏幕分辨率并返回字体缩放比例"""
        return get_ui_font_scale()

    def get_scaled_stylesheet(self):
        """获取根据分辨率缩放的样式表"""
        # 基础字体大小
        base_sizes = {
            'small': 12,
            'normal': 14, 
            'large': 16,
            'xl': 18,
            'xxl': 24,
            'xxxl': 30
        }
        
        # 计算缩放后的字体大小
        scaled_sizes = {k: int(v * self.font_scale) for k, v in base_sizes.items()}
        
        return f"""
            QMainWindow, QWidget {{
                background-color: #2b2b2b;
                color: #e8e8e8;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            QLabel {{
                color: #e8e8e8;
                font-size: {scaled_sizes['normal']}px;
                background-color: transparent;
            }}
            
            QTabWidget::pane {{
                border: 1px solid #3c3c3c;
                background-color: #2b2b2b;
            }}
            
            QTabBar::tab {{
                background-color: #3c3c3c;
                color: #e8e8e8;
                padding: 8px 16px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            QTabBar::tab:selected {{
                background-color: #2b2b2b;
                border-bottom: 2px solid #0078d7;
            }}
            
            QTabBar::tab:hover {{
                background-color: #4a4a4a;
            }}
            
            QTableWidget {{
                background-color: #333333;
                alternate-background-color: #383838;
                border: 1px solid #3c3c3c;
                color: #e8e8e8;
                gridline-color: #3c3c3c;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            QTableWidget::item {{
                padding: 5px;
                background-color: transparent;
                border: none;
            }}
            
            QTableWidget::item:selected {{
                background-color: #0078d7;
                color: #ffffff;
            }}
            
            QHeaderView::section {{
                background-color: #3a3a3a;
                color: #e8e8e8;
                padding: 8px;
                border: none;
                border-right: 1px solid #454545;
                border-bottom: 1px solid #454545;
                font-weight: bold;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            QGroupBox {{
                background-color: #333333;
                border: 1px solid #4a4a4a;
                border-radius: 6px;
                margin-top: 1em;
                padding-top: 1em;
                color: #e8e8e8;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: #e8e8e8;
                font-weight: bold;
                background-color: #333333;
                font-size: {scaled_sizes['normal']}px;
            }}
        """
        
    def apply_dark_theme(self):
        """应用深色主题样式"""
        # 使用缩放后的样式表
        self.setStyleSheet(self.get_scaled_stylesheet())

    def apply_ui_scale(self, scale=None):
        """应用界面字号倍率到当前窗口"""
        if scale is None:
            scale = get_ui_font_scale()
        self.font_scale = scale
        self.apply_dark_theme()
        self.update()

    def apply_dark_titlebar(self, widget):
        if sys.platform != 'win32':
            return
        try:
            from ctypes import windll, c_int, byref, sizeof
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            DWMWA_CAPTION_COLOR = 35
            windll.dwmapi.DwmSetWindowAttribute(
                int(widget.winId()),
                DWMWA_USE_IMMERSIVE_DARK_MODE,
                byref(c_int(2)),
                sizeof(c_int)
            )
            caption_color = c_int(0x333333)
            windll.dwmapi.DwmSetWindowAttribute(
                int(widget.winId()),
                DWMWA_CAPTION_COLOR,
                byref(caption_color),
                sizeof(caption_color)
            )
        except Exception:
            pass

    def open_results_folder(self):
        try:
            backtest_dir = os.path.abspath(self.backtest_dir)
            if not os.path.exists(backtest_dir):
                msg_box = QMessageBox(self)
                msg_box.setIcon(QMessageBox.Warning)
                msg_box.setWindowTitle("提示")
                msg_box.setText(f"回测结果目录不存在:\n{backtest_dir}")
                self.apply_dark_titlebar(msg_box)
                msg_box.exec_()
                return

            if sys.platform == 'win32':
                os.startfile(backtest_dir)
            elif sys.platform == 'darwin':
                import subprocess
                subprocess.Popen(['open', backtest_dir])
            else:
                import subprocess
                subprocess.Popen(['xdg-open', backtest_dir])

            file_descriptions = [
                ("config.csv", "回测配置（策略名称、回测区间、初始资金、基准等）"),
                ("trades.csv", "交易明细（每笔成交记录）"),
                ("daily_stats.csv", "每日统计（净值、收益、回撤等）"),
                ("benchmark.csv", "基准指数数据（用于对比）"),
                ("summary.csv", "回测汇总指标（用于快速概览）")
            ]
            lines = []
            for filename, desc in file_descriptions:
                status = "存在" if os.path.exists(os.path.join(backtest_dir, filename)) else "缺失"
                lines.append(f"{filename}：{desc}（{status}）")
            info_box = QMessageBox(self)
            info_box.setIcon(QMessageBox.Information)
            info_box.setWindowTitle("回测结果文件说明")
            info_box.setText("\n".join(lines))
            self.apply_dark_titlebar(info_box)
            info_box.exec_()
        except Exception as e:
            error_box = QMessageBox(self)
            error_box.setIcon(QMessageBox.Critical)
            error_box.setWindowTitle("错误")
            error_box.setText(f"打开回测结果文件夹失败:\n{str(e)}")
            self.apply_dark_titlebar(error_box)
            error_box.exec_()
        
    def init_ui(self):
        # 根据分辨率自适应窗口大小
        base_width, base_height = 1600, 1000
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
        
        toolbar_layout = QHBoxLayout()
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.addStretch()
        self.export_results_btn = QPushButton("导出回测结果")
        
        # macOS 使用深灰色样式，Windows 使用蓝色样式
        if sys.platform == 'darwin':
            self.export_results_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #505050;
                    color: #e8e8e8;
                    border: none;
                    border-radius: {int(6 * self.font_scale)}px;
                    padding: {int(6 * self.font_scale)}px {int(14 * self.font_scale)}px;
                    font-size: {int(14 * self.font_scale)}px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background-color: #606060;
                }}
                QPushButton:pressed {{
                    background-color: #404040;
                }}
            """)
        else:
            self.export_results_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #0078d7;
                    color: #ffffff;
                    border: none;
                    border-radius: {int(6 * self.font_scale)}px;
                    padding: {int(6 * self.font_scale)}px {int(14 * self.font_scale)}px;
                    font-size: {int(14 * self.font_scale)}px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background-color: #1a86d9;
                }}
                QPushButton:pressed {{
                    background-color: #0063b1;
                }}
            """)
        self.export_results_btn.setMinimumHeight(int(30 * self.font_scale))
        self.export_results_btn.clicked.connect(self.open_results_folder)
        toolbar_layout.addWidget(self.export_results_btn)
        main_layout.addLayout(toolbar_layout)

        # 创建上下分割器
        splitter = QSplitter(Qt.Vertical)
        splitter.setHandleWidth(2)  # 设置分割条宽度
        
        # 上部分：基本信息和收益曲线
        top_widget = QWidget()
        top_layout = QHBoxLayout()
        top_layout.setSpacing(20)  # 增加组件之间的间距
        top_layout.setContentsMargins(5, 5, 5, 5)  # 设置外边距
        
        # 基本信息面板
        info_group = QGroupBox("基本信息")
        info_group.setStyleSheet(f"""
            QGroupBox {{
                font-size: {int(16 * self.font_scale)}px;
                font-weight: bold;
                background-color: #2d2d2d;
                border: 2px solid #404040;
                border-radius: 8px;
                padding: {int(5 * self.font_scale)}px;
            }}
            QGroupBox::title {{
                font-size: {int(16 * self.font_scale)}px;
                padding: 0 {int(6 * self.font_scale)}px;
                background-color: #2d2d2d;
            }}
        """)
        info_layout = QGridLayout()
        info_layout.setVerticalSpacing(int(2 * self.font_scale))  # 减小行间距，更紧凑
        info_layout.setHorizontalSpacing(int(8 * self.font_scale))  # 保持合适的列间距
        info_layout.setContentsMargins(int(8 * self.font_scale), int(6 * self.font_scale), 
                                      int(8 * self.font_scale), int(6 * self.font_scale))
        self.info_labels = {}
        info_items = ["策略名称", "回测区间", "初始资金", "最终资金",
                     "总收益率", "年化收益率", "基准收益率", "基准年化收益率", "最大回撤", "夏普比率",
                     "索提诺比率", "阿尔法", "贝塔",
                     "胜率", "盈亏比", "日均交易次数", "最大连续盈利",
                     "最大连续亏损", "最大单笔盈利", "最大单笔亏损", "年化波动率"]

        label_style = f"""
            font-weight: bold;
            font-size: {int(14 * self.font_scale)}px;
            color: #a0a0a0;
            background-color: transparent;
        """
        value_style = f"""
            color: #e8e8e8;
            font-size: {int(14 * self.font_scale)}px;
            font-family: 'Consolas', 'Microsoft YaHei', monospace;
            background-color: transparent;
        """

        # 策略名称单独占首行并横跨两列指标的宽度，长名称自动换行，避免被截断
        name_label = QLabel("策略名称:")
        name_label.setStyleSheet(label_style)
        name_label.setMinimumWidth(int(100 * self.font_scale))
        name_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        name_value = QLabel("--")
        name_value.setStyleSheet(value_style)
        name_value.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        name_value.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        name_value.setWordWrap(True)
        info_layout.addWidget(name_label, 0, 0, Qt.AlignRight)
        info_layout.addWidget(name_value, 0, 1, 1, 4)
        self.info_labels["策略名称"] = name_value

        # 其余指标按两列布局显示（从第1行开始）
        rest_items = info_items[1:]
        col1_items = rest_items[:(len(rest_items) + 1) // 2]
        col2_items = rest_items[(len(rest_items) + 1) // 2:]

        # 调整标签和值的宽度以确保足够的显示空间
        for i, item in enumerate(col1_items):
            label = QLabel(f"{item}:")
            label.setStyleSheet(label_style)
            label.setMinimumWidth(int(100 * self.font_scale))  # 标签固定最小宽度
            label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)  # 标签宽度固定，高度自适应

            value = QLabel("--")
            value.setStyleSheet(value_style)
            value.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)  # 数值宽度自适应内容
            value.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            value.setWordWrap(False)  # 禁用自动换行

            info_layout.addWidget(label, i + 1, 0, Qt.AlignRight)
            info_layout.addWidget(value, i + 1, 1, Qt.AlignLeft)
            self.info_labels[item] = value

        # 确保两列之间有足够的间距
        info_layout.setColumnMinimumWidth(2, int(20 * self.font_scale))  # 增加列间距

        for i, item in enumerate(col2_items):
            label = QLabel(f"{item}:")
            label.setStyleSheet(label_style)
            label.setMinimumWidth(int(100 * self.font_scale))  # 标签固定最小宽度
            label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)  # 标签宽度固定，高度自适应

            value = QLabel("--")
            value.setStyleSheet(value_style)
            value.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)  # 数值宽度自适应内容
            value.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            value.setWordWrap(False)  # 禁用自动换行

            info_layout.addWidget(label, i + 1, 3, Qt.AlignRight)
            info_layout.addWidget(value, i + 1, 4, Qt.AlignLeft)
            self.info_labels[item] = value

        info_group.setLayout(info_layout)
        # 设置基本信息面板的大小策略，限制其宽度
        # 宽度需容纳两组"标签+数值"：之前的400px会把数值列挤压到约70px导致内容被截断
        info_group.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        info_group.setMaximumWidth(int(470 * self.font_scale))  # 限制最大宽度
        top_layout.addWidget(info_group, 0)  # 伸展因子为0，不拉伸
        
        # 收益曲线图表
        chart_group = QGroupBox("收益曲线")
        chart_group.setStyleSheet(f"""
            QGroupBox {{
                font-size: {int(16 * self.font_scale)}px;
                font-weight: bold;
                background-color: #2d2d2d;
                border: 2px solid #404040;
                border-radius: 8px;
                padding: {int(15 * self.font_scale)}px;
            }}
            QGroupBox::title {{
                font-size: {int(16 * self.font_scale)}px;
                padding: 0 {int(8 * self.font_scale)}px;
                background-color: #2d2d2d;
            }}
        """)
        chart_layout = QVBoxLayout()
        chart_layout.setContentsMargins(int(8 * self.font_scale), int(10 * self.font_scale), 
                                       int(8 * self.font_scale), int(8 * self.font_scale))  # 减少图表内边距，让图表更饱满
        
        # 创建图表并保存canvas引用
        canvas = self.create_chart()
        self.chart_view = canvas
        
        # 添加鼠标移动事件处理
        canvas.mpl_connect('motion_notify_event', self.hover)
        
        # 添加窗口大小变化事件处理，确保在窗口调整时重新布局
        canvas.mpl_connect('resize_event', self.on_chart_resize)
        
        chart_layout.addWidget(self.chart_view)
        chart_group.setLayout(chart_layout)
        
        # 设置图表组件的大小策略，让其占据更多空间
        chart_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout_scale = min(self.font_scale, 1.25)
        chart_group.setMinimumSize(int(480 * layout_scale), int(240 * layout_scale))
        
        top_layout.addWidget(chart_group, 1)  # 伸展因子为1，占据剩余空间
        
        top_widget.setLayout(top_layout)
        splitter.addWidget(top_widget)
        
        # 下部分：Tab页面
        tab_widget = QTabWidget()
        tab_widget.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 2px solid #404040;
                border-radius: 8px;
                background: #2d2d2d;
                margin-top: -2px;
            }}
            QTabBar::tab {{
                min-width: {int(100 * self.font_scale)}px;
                padding: {int(8 * self.font_scale)}px {int(12 * self.font_scale)}px;
                margin: 0px 2px;
                color: #a0a0a0;
                font-size: {int(14 * self.font_scale)}px;
                font-weight: bold;
                background: #2d2d2d;
                border: 2px solid #404040;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
            }}
            QTabBar::tab:selected {{
                color: #ffffff;
                background: #333333;
                border-bottom: 2px solid #007acc;
            }}
            QTabBar::tab:hover {{
                background: #353535;
                color: #e8e8e8;
            }}
        """)
        
        # 交易记录标签页
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
                padding: {int(10 * self.font_scale)}px {int(6 * self.font_scale)}px;
                border: none;
                border-right: 1px solid #404040;
                border-bottom: 2px solid #404040;
            }}
            QHeaderView::section:hover {{
                background-color: #383838;
            }}
        """)
        self.trades_table.setColumnCount(10)
        self.trades_table.setHorizontalHeaderLabels(
            ["交易时间", "证券代码", "交易方向", "成交价格", "成交数量", "成交金额", "手续费", "持仓资金", "可用资金", "总资产"]
        )
        self.trades_table.setAlternatingRowColors(True)
        self.trades_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.trades_table.verticalHeader().setVisible(False)
        tab_widget.addTab(self.trades_table, "交易记录")
        
        # 每日收益标签页
        self.daily_stats_table = QTableWidget()
        self.daily_stats_table.setStyleSheet(f"""
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
                padding: {int(10 * self.font_scale)}px {int(6 * self.font_scale)}px;
                border: none;
                border-right: 1px solid #404040;
                border-bottom: 2px solid #404040;
            }}
            QHeaderView::section:hover {{
                background-color: #383838;
            }}
        """)
        self.daily_stats_table.setColumnCount(5)
        self.daily_stats_table.setHorizontalHeaderLabels(
            ["日期", "总资产", "持仓市值", "可用资金", "日收益率"]
        )
        self.daily_stats_table.setAlternatingRowColors(True)
        self.daily_stats_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.daily_stats_table.verticalHeader().setVisible(False)
        tab_widget.addTab(self.daily_stats_table, "日收益")
        
        # 绩效评估标签页
        performance_widget = QWidget()
        performance_layout = QVBoxLayout()
        performance_layout.setContentsMargins(10, 10, 10, 10)
        performance_layout.setSpacing(15)
        
        # 创建多图表布局
        charts_splitter = QSplitter(Qt.Horizontal)
        charts_splitter.setHandleWidth(2)
        
        # 收益分布图
        returns_dist_group = QGroupBox("收益分布")
        returns_dist_group.setStyleSheet(f"""
            QGroupBox {{
                font-size: {int(16 * self.font_scale)}px;
                font-weight: bold;
                background-color: #2d2d2d;
                border: 2px solid #404040;
                border-radius: 8px;
                padding: {int(12 * self.font_scale)}px;
            }}
            QGroupBox::title {{
                font-size: {int(16 * self.font_scale)}px;
                padding: 0 {int(8 * self.font_scale)}px;
                background-color: #2d2d2d;
            }}
        """)
        returns_dist_layout = QVBoxLayout()
        self.returns_dist_figure = Figure(figsize=(5, 4), facecolor='#2d2d2d')
        self.returns_dist_canvas = FigureCanvas(self.returns_dist_figure)
        returns_dist_layout.addWidget(self.returns_dist_canvas)
        returns_dist_group.setLayout(returns_dist_layout)
        
        # 添加月度收益热力图
        monthly_returns_group = QGroupBox("月度收益热力图")
        monthly_returns_group.setStyleSheet(f"""
            QGroupBox {{
                font-size: {int(16 * self.font_scale)}px;
                font-weight: bold;
                background-color: #2d2d2d;
                border: 2px solid #404040;
                border-radius: 8px;
                padding: {int(12 * self.font_scale)}px;
            }}
            QGroupBox::title {{
                font-size: {int(16 * self.font_scale)}px;
                padding: 0 {int(8 * self.font_scale)}px;
                background-color: #2d2d2d;
            }}
        """)
        monthly_returns_layout = QVBoxLayout()
        self.monthly_returns_figure = Figure(figsize=(5, 4), facecolor='#2d2d2d')
        self.monthly_returns_canvas = FigureCanvas(self.monthly_returns_figure)
        monthly_returns_layout.addWidget(self.monthly_returns_canvas)
        monthly_returns_group.setLayout(monthly_returns_layout)
        
        # 添加图表到splitter
        charts_splitter.addWidget(returns_dist_group)
        charts_splitter.addWidget(monthly_returns_group)
        
        # 设置初始大小
        charts_splitter.setSizes([500, 500])
        
        # 将图表添加到总布局
        performance_layout.addWidget(charts_splitter)
        
        performance_widget.setLayout(performance_layout)
        tab_widget.addTab(performance_widget, "绩效分析")

        # 添加个股分析"假标签"（点击时打开窗口而不是切换页面）
        self.stock_analysis_tab_index = tab_widget.addTab(QWidget(), "📊 个股分析")
        self.last_valid_tab_index = 2  # 绩效分析的索引

        # 为个股分析标签设置不同的样式
        if sys.platform == 'darwin':
            tab_widget.tabBar().setStyleSheet(f"""
                QTabBar::tab {{
                    background-color: #404040;
                    color: #e8e8e8;
                    padding: {int(8 * self.font_scale)}px {int(16 * self.font_scale)}px;
                    margin-right: 2px;
                    border-top-left-radius: 4px;
                    border-top-right-radius: 4px;
                    font-size: {int(14 * self.font_scale)}px;
                }}
                QTabBar::tab:selected {{
                    background-color: #333333;
                    border-bottom: 2px solid #505050;
                }}
                QTabBar::tab:hover {{
                    background-color: #505050;
                }}
                QTabBar::tab:last {{
                    background-color: #505050;
                    color: #e8e8e8;
                    font-weight: bold;
                    border: 1px solid #606060;
                    border-bottom: none;
                }}
                QTabBar::tab:last:hover {{
                    background-color: #606060;
                }}
            """)
        else:
            tab_widget.tabBar().setStyleSheet(f"""
                QTabBar::tab {{
                    background-color: #404040;
                    color: #e8e8e8;
                    padding: {int(8 * self.font_scale)}px {int(16 * self.font_scale)}px;
                    margin-right: 2px;
                    border-top-left-radius: 4px;
                    border-top-right-radius: 4px;
                    font-size: {int(14 * self.font_scale)}px;
                }}
                QTabBar::tab:selected {{
                    background-color: #333333;
                    border-bottom: 2px solid #007acc;
                }}
                QTabBar::tab:hover {{
                    background-color: #505050;
                }}
                QTabBar::tab:last {{
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 #4a6fa5, stop:1 #3d5a80);
                    color: #ffffff;
                    font-weight: bold;
                    border: 1px solid #5a7fb5;
                    border-bottom: none;
                }}
                QTabBar::tab:last:hover {{
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 #5a8fc5, stop:1 #4d7aa0);
                }}
            """)

        # 连接标签切换信号
        tab_widget.currentChanged.connect(self._on_tab_changed)
        self.tab_widget = tab_widget  # 保存引用

        splitter.addWidget(tab_widget)
        
        # 设置分割器的初始大小比例
        splitter.setSizes([750, 250])  # 增加上部分比例，减少底部Tab区域占比
        
        # 设置最小高度
        top_widget.setMinimumHeight(int(300 * layout_scale))
        tab_widget.setMinimumHeight(int(180 * layout_scale))
        
        main_layout.addWidget(splitter)
        
    def create_chart(self):
        """创建matplotlib图表，包含收益曲线、回撤曲线、盈亏分析图和成交记录图"""
        # 创建带有四个子图的Figure，共享x轴，设置最小尺寸确保显示完整
        fig = Figure(figsize=(12, 10), facecolor='#2d2d2d')  # 调整比例，确保布局合理
        canvas = FigureCanvas(fig)
        
        # 创建四个子图，高度比例为4:1:1:1
        gs = fig.add_gridspec(7, 1)  # 7行1列的网格
        self.ax = fig.add_subplot(gs[0:4, 0])  # 前4行用于收益曲线
        self.ax_drawdown = fig.add_subplot(gs[4, 0], sharex=self.ax)  # 第5行用于回撤曲线
        self.ax_pnl = fig.add_subplot(gs[5, 0], sharex=self.ax)  # 第6行用于盈亏分析图
        self.ax_trades = fig.add_subplot(gs[6, 0], sharex=self.ax)  # 第7行用于成交记录图
        
        # 根据字体缩放计算图表字体大小（与update_chart保持一致）
        title_fontsize = int(14 * self.font_scale)  # 适中的标题字体
        label_fontsize = int(11 * self.font_scale)  # 适中的标签字体
        tick_fontsize = int(10 * self.font_scale)   # 适中的刻度字体

        # 设置上方子图（收益曲线）
        self.ax.set_title("策略收益与基准对比", color='#e8e8e8', pad=int(10 * self.font_scale),
                         fontsize=title_fontsize, fontweight='bold')
        self.ax.set_facecolor('#2d2d2d')
        self.ax.set_ylabel("净值", color='#a0a0a0', fontsize=label_fontsize)
        
        # 隐藏上方子图的x轴标签，设置y轴刻度字体大小
        self.ax.tick_params(axis='x', labelbottom=False)
        self.ax.tick_params(axis='y', labelsize=tick_fontsize, colors='#a0a0a0')
        
        # 设置回撤曲线子图
        self.ax_drawdown.set_facecolor('#2d2d2d')
        self.ax_drawdown.set_ylabel("回撤率 (%)", color='#a0a0a0', fontsize=label_fontsize)
        # 隐藏回撤子图的x轴标签，设置y轴刻度字体大小
        self.ax_drawdown.tick_params(axis='x', labelbottom=False)
        self.ax_drawdown.tick_params(axis='y', labelsize=tick_fontsize, colors='#a0a0a0')
        
        # 设置盈亏分析图子图
        self.ax_pnl.set_facecolor('#2d2d2d')
        self.ax_pnl.set_ylabel("日盈亏", color='#a0a0a0', fontsize=label_fontsize)
        # 隐藏盈亏分析图子图的x轴标签，设置y轴刻度字体大小
        self.ax_pnl.tick_params(axis='x', labelbottom=False)
        self.ax_pnl.tick_params(axis='y', labelsize=tick_fontsize, colors='#a0a0a0')
        
        # 设置成交记录图子图
        self.ax_trades.set_facecolor('#2d2d2d')
        self.ax_trades.set_ylabel("买入/卖出量", color='#a0a0a0', fontsize=label_fontsize)
        #self.ax_trades.set_xlabel("时间", color='#a0a0a0', fontsize=label_fontsize)
        # 设置成交记录图的刻度字体大小
        self.ax_trades.tick_params(axis='both', labelsize=tick_fontsize, colors='#a0a0a0')
        
        # 设置所有子图的基本样式
        for a in [self.ax, self.ax_drawdown, self.ax_pnl, self.ax_trades]:
            a.tick_params(axis='both', colors='#a0a0a0', labelsize=tick_fontsize)
            a.grid(True, linestyle='--', alpha=0.1, color='#808080')
            
            for spine in a.spines.values():
                spine.set_color('#404040')
        
        # 特别设置：反转回撤图的y轴（使回撤为负值显示在下方）
        self.ax_drawdown.invert_yaxis()
        
        # 调整图表边距，为字体留出适当空间避免遮挡（按画布像素尺寸动态计算）
        self._apply_chart_margins(fig)

        # 初始化图表元素为None，避免悬停事件中的错误
        self.v_line_ax = None
        self.v_line_drawdown = None
        self.v_line_pnl = None
        self.v_line_trades = None
        self.strategy_point = None
        self.benchmark_point = None
        self.drawdown_point = None
        self.pnl_point = None
        self.hover_annotation = None
        
        return canvas
    
    @staticmethod
    def _format_amount_tick(value, pos=None):
        """金额类Y轴刻度缩写：超过万/亿用中文单位，避免"-12000"这类长数字
        把轴标题挤出画布左边界。"""
        av = abs(value)
        if av >= 1e8:
            return f"{value / 1e8:g}亿"
        if av >= 1e4:
            return f"{value / 1e4:g}万"
        return f"{value:g}"

    def _apply_chart_margins(self, fig=None):
        """按画布实际像素尺寸动态计算边距，保证标题、Y轴刻度和底部日期标签完整显示。

        之前使用硬编码比例（top=0.92/0.95），在高分屏字体放大后标题所需的
        像素高度超过预留比例，导致主图标题被裁掉。
        """
        try:
            if fig is None:
                fig = self.chart_view.figure
            size = fig.get_size_inches()
            w_px = size[0] * fig.dpi
            h_px = size[1] * fig.dpi
            if w_px <= 1 or h_px <= 1:
                return
            # 顶部：主图标题字号 + 标题pad + 余量
            top_px = (14 + 10) * self.font_scale + 14
            # 底部：旋转45°的日期刻度标签（"2025-01-01"旋转后垂直跨度约60px*缩放，
            # 再加刻度间距和余量；留太紧会裁掉最深处的首字符）
            bottom_px = 78 * self.font_scale
            # 左侧：Y轴刻度数字 + 轴标题（刻度已用"万/亿"缩写控制宽度）
            left_px = 72 * self.font_scale
            top = 1.0 - min(0.20, top_px / h_px)
            bottom = min(0.28, bottom_px / h_px)
            left = min(0.18, left_px / w_px)
            fig.subplots_adjust(left=left, right=0.97, top=top, bottom=bottom, hspace=0.25)
        except Exception as e:
            print(f"调整图表边距时出错: {str(e)}")

    def on_chart_resize(self, event):
        """处理图表画布大小变化事件，重新调整布局"""
        try:
            if hasattr(self, 'chart_view') and self.chart_view:
                # 重新调整布局以确保标题、坐标轴和标注完整显示
                self._apply_chart_margins()
                # 重绘图表
                self.chart_view.draw_idle()
        except Exception as e:
            print(f"调整图表布局时出错: {str(e)}")

    def _on_tab_changed(self, index):
        """处理标签页切换事件"""
        if index == self.stock_analysis_tab_index:
            # 点击个股分析标签时，打开窗口并切回上一个有效标签
            self.tab_widget.blockSignals(True)  # 阻止信号避免递归
            self.tab_widget.setCurrentIndex(self.last_valid_tab_index)
            self.tab_widget.blockSignals(False)
            self.open_stock_analysis()
        else:
            # 记录当前有效标签索引
            self.last_valid_tab_index = index

    def open_stock_analysis(self):
        """打开个股分析窗口"""
        try:
            # 获取交易数据和每日统计数据
            trades_df = getattr(self, 'trades_df', None)
            daily_stats_df = getattr(self, 'daily_stats_df', None)

            if trades_df is None or trades_df.empty:
                from PyQt5.QtWidgets import QMessageBox
                QMessageBox.warning(self, "提示", "没有交易记录，无法进行个股分析")
                return

            existing = getattr(self, 'stock_analysis_window', None)
            if existing is not None:
                try:
                    if existing.isVisible():
                        existing.showMaximized()
                        existing.raise_()
                        existing.activateWindow()
                        return
                except RuntimeError:
                    self.stock_analysis_window = None

            # 创建并显示个股分析窗口
            self.stock_analysis_window = StockAnalysisWindow(
                self.backtest_dir,
                trades_df,
                daily_stats_df,
                parent=self
            )
            window = self.stock_analysis_window
            window.destroyed.connect(
                lambda _obj=None, target=window: self._forget_stock_analysis_window(target)
            )
            self.stock_analysis_window.show()
        except Exception as e:
            print(f"打开个股分析窗口失败: {str(e)}")
            import traceback
            traceback.print_exc()

    def _forget_stock_analysis_window(self, window):
        if getattr(self, 'stock_analysis_window', None) is window:
            self.stock_analysis_window = None

    def closeEvent(self, event):
        """关闭后销毁所有图表和大表数据，防止主界面长期持有重型结果对象。"""
        thread = getattr(self, '_load_thread', None)
        if thread is not None and thread.isRunning():
            if not self._closing_after_load:
                self._closing_after_load = True
                self.hide()
                thread.requestInterruption()
                thread.finished.connect(self.close)
            event.ignore()
            return

        child = getattr(self, 'stock_analysis_window', None)
        if child is not None:
            try:
                child.close()
            except RuntimeError:
                pass
            self.stock_analysis_window = None

        for canvas_name in (
            'chart_view', 'returns_dist_canvas', 'monthly_returns_canvas',
            'rolling_metrics_canvas',
        ):
            canvas = getattr(self, canvas_name, None)
            if canvas is not None:
                try:
                    canvas.close()
                except Exception:
                    pass

        figures = []
        chart_view = getattr(self, 'chart_view', None)
        if chart_view is not None:
            figures.append(getattr(chart_view, 'figure', None))
        for figure_name in (
            'returns_dist_figure', 'monthly_returns_figure', 'rolling_metrics_figure',
        ):
            figures.append(getattr(self, figure_name, None))
        for figure in figures:
            if figure is None:
                continue
            try:
                figure.clear()
                plt.close(figure)
            except Exception:
                pass

        for table_name in ('trades_table', 'daily_stats_table'):
            table = getattr(self, table_name, None)
            if table is not None:
                table.clearContents()
                table.setRowCount(0)

        for data_name in ('trades_df', 'daily_stats_df', 'benchmark_df'):
            if hasattr(self, data_name):
                setattr(self, data_name, None)
        event.accept()

    def _set_loading_state(self, loading):
        """切换结果窗口加载状态。"""
        try:
            self.export_results_btn.setEnabled(not loading)
            for label in getattr(self, 'info_labels', {}).values():
                label.setText("加载中..." if loading else "--")
            if loading:
                self.trades_table.setRowCount(1)
                self.trades_table.setColumnCount(1)
                self.trades_table.setHorizontalHeaderLabels(["状态"])
                self.trades_table.setItem(0, 0, QTableWidgetItem("正在加载交易记录..."))
                self.daily_stats_table.setRowCount(1)
                self.daily_stats_table.setColumnCount(1)
                self.daily_stats_table.setHorizontalHeaderLabels(["状态"])
                self.daily_stats_table.setItem(0, 0, QTableWidgetItem("正在加载每日统计..."))
        except Exception as e:
            print(f"设置加载状态时出错: {str(e)}")

    def start_async_load(self):
        """异步加载回测数据，避免构造结果窗口时阻塞GUI线程。"""
        try:
            if self._load_thread is not None and self._load_thread.isRunning():
                return

            self._load_thread = BacktestResultLoadThread(self.backtest_dir, self)
            self._load_thread.loaded.connect(self._on_async_data_loaded)
            self._load_thread.error.connect(self._on_async_data_error)
            self._load_thread.finished.connect(self._on_async_load_finished)
            self._load_thread.start()
        except Exception as e:
            self._on_async_data_error(str(e))

    def _on_async_load_finished(self):
        """异步加载线程结束后的引用清理。"""
        try:
            if self._load_thread is not None:
                self._load_thread.deleteLater()
                self._load_thread = None
        except Exception as e:
            print(f"清理结果加载线程时出错: {str(e)}")

    def _on_async_data_error(self, error_text):
        """异步加载失败处理。"""
        print(f"加载回测数据时出错: {error_text}")
        print(f"当前工作目录: {os.getcwd()}")
        self._set_loading_state(False)
        for label in getattr(self, 'info_labels', {}).values():
            label.setText("--")
        self.trades_table.setRowCount(1)
        self.trades_table.setColumnCount(1)
        self.trades_table.setHorizontalHeaderLabels(["错误"])
        self.trades_table.setItem(0, 0, QTableWidgetItem("加载回测数据失败"))
        self.daily_stats_table.setRowCount(1)
        self.daily_stats_table.setColumnCount(1)
        self.daily_stats_table.setHorizontalHeaderLabels(["错误"])
        self.daily_stats_table.setItem(0, 0, QTableWidgetItem("加载回测数据失败"))

    def _on_async_data_loaded(self, data):
        """在GUI线程应用后台加载得到的回测数据。"""
        try:
            self._set_loading_state(False)
            self._apply_loaded_data(data)
            self.export_results_btn.setEnabled(True)
        except Exception as e:
            import traceback
            self._on_async_data_error(f"{str(e)}\n{traceback.format_exc()}")

    def _apply_loaded_data(self, data):
        """将已读取的数据更新到结果窗口（分步进度条提示，避免渲染期界面假死无反馈）。"""
        from PyQt5.QtWidgets import QProgressDialog, QApplication

        self.price_decimals = data['price_decimals']
        self.trades_df = data['trades_raw_df']
        self._chart_trades_df = data['trades_raw_df'].copy()
        self.daily_stats_df = data['daily_stats_original_df']
        self.benchmark_code = data['benchmark_code']
        self.benchmark_base_price = data.get('benchmark_base_price')
        self.benchmark_df = data['benchmark_df']

        config_row = data['config_row']
        daily_stats_df = data['daily_stats_df']
        benchmark_df = data['benchmark_df']
        trades_display_df = data['trades_display_df']

        # 渲染期给用户一个“正在生成结果…”的等待条（分步推进），
        # 配合交易表封顶/固定列宽，避免大数据量下界面长时间无反馈的“假死”。
        trade_cnt = len(trades_display_df) if trades_display_df is not None else 0
        steps = [
            ("正在生成基本信息…", lambda: self.update_basic_info(config_row, daily_stats_df, benchmark_df)),
            ("正在绘制收益曲线…", lambda: self.update_chart(daily_stats_df, benchmark_df)),
            (f"正在填充交易记录（共 {trade_cnt:,} 笔）…", lambda: self.update_trades_table(trades_display_df)),
            ("正在填充每日统计…", lambda: self.update_daily_stats_table(daily_stats_df)),
            ("正在绘制绩效分析图…", lambda: self.update_performance_charts(daily_stats_df, benchmark_df)),
        ]
        progress = QProgressDialog("正在生成回测结果，请稍候…", None, 0, len(steps), self)
        progress.setWindowTitle("生成回测结果")
        progress.setWindowModality(Qt.ApplicationModal)
        progress.setCancelButton(None)            # 不可取消，避免中途打断渲染
        progress.setMinimumDuration(0)            # 立即显示
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        QApplication.processEvents()
        try:
            for idx, (label, fn) in enumerate(steps):
                progress.setLabelText(label)
                QApplication.processEvents()      # 先把提示文字刷出来再做重活
                fn()
                progress.setValue(idx + 1)
                QApplication.processEvents()
        finally:
            progress.close()

    def load_data(self):
        """加载回测数据"""
        try:
            # 使用os.path来规范化路径
            backtest_dir = os.path.abspath(self.backtest_dir)
            
            # 检查目录是否存在
            if not os.path.exists(backtest_dir):
                raise FileNotFoundError(f"回测结果目录不存在: {backtest_dir}")
            
            # 构建配置文件路径
            config_path = os.path.join(backtest_dir, "config.csv")

            # 打印调试信息
            print(f"尝试加载配置文件: {config_path}") 
            print(f"文件是否存在: {os.path.exists(config_path)}")
            
            # 尝试列出目录内容
            print("目录内容:")
            for file in os.listdir(backtest_dir):
                print(f"- {file}")
            
            if not os.path.exists(config_path):
                raise FileNotFoundError(f"配置文件不存在: {config_path}")
            
            # 使用 utf-8-sig 编码读取文件，处理可能的 BOM
            config_df = pd.read_csv(config_path, encoding='utf-8-sig')
            
            # 读取交易记录和每日统计数据
            trades_path = os.path.join(backtest_dir, "trades.csv")
            daily_stats_path = os.path.join(backtest_dir, "daily_stats.csv")
            benchmark_path = os.path.join(backtest_dir, "benchmark.csv")
            
            # 检查文件是否存在
            if not os.path.exists(trades_path):
                print(f"警告: 交易记录文件不存在: {trades_path}")
                trades_df = pd.DataFrame(columns=['datetime', 'code', 'action', 'price', 'volume', 'amount', 'commission'])
            else:
                trades_df = pd.read_csv(trades_path, encoding='utf-8-sig')
            
            # 从交易记录中获取股票代码，判断价格精度
            if len(trades_df) > 0 and 'code' in trades_df.columns:
                stock_codes = trades_df['code'].unique().tolist()
                _, self.price_decimals = determine_pool_type(stock_codes)
                print(f"根据股票池类型设置价格精度: {self.price_decimals}位小数")
            
            if not os.path.exists(daily_stats_path):
                print(f"警告: 每日统计文件不存在: {daily_stats_path}")
                daily_stats_df = pd.DataFrame(columns=['date', 'total_asset', 'cash', 'market_value', 'daily_return'])
            else:
                daily_stats_df = pd.read_csv(daily_stats_path, encoding='utf-8-sig')
                
                # 检查并计算daily_return列
                if 'daily_return' not in daily_stats_df.columns and 'total_asset' in daily_stats_df.columns:
                    print("daily_stats.csv中没有daily_return列，正在计算...")
                    # 确保日期列是日期类型，并按日期排序
                    daily_stats_df['date'] = pd.to_datetime(daily_stats_df['date'])
                    daily_stats_df = daily_stats_df.sort_values('date')
                    # 计算每日收益率
                    daily_stats_df['daily_return'] = daily_stats_df['total_asset'].pct_change()
                    print(f"已计算daily_return列，共{len(daily_stats_df)}条数据")
                    # 替换第一行的NaN值为0
                    daily_stats_df['daily_return'].iloc[0] = 0
            
            # 处理基准数据文件
            if not os.path.exists(benchmark_path):
                print(f"警告: 基准数据文件不存在: {benchmark_path}")
                # 创建一个假的基准数据DataFrame，与daily_stats_df具有相同的日期范围
                if len(daily_stats_df) > 0 and 'date' in daily_stats_df.columns:
                    # 将日期列转换为datetime
                    daily_stats_df['date'] = pd.to_datetime(daily_stats_df['date'])
                    
                    # 创建与策略数据相同日期范围的基准数据
                    dates = daily_stats_df['date']
                    # 创建一个初始值为1，后续值相同的序列
                    closes = np.ones(len(dates))
                    
                    benchmark_df = pd.DataFrame({
                        'date': dates,
                        'close': closes
                    })
                    print("创建了替代基准数据")
                else:
                    # 如果没有每日统计数据，则创建一个空的基准数据DataFrame
                    benchmark_df = pd.DataFrame(columns=['date', 'close'])
                    print("创建了空的基准数据DataFrame")
            else:
                try:
                    benchmark_df = pd.read_csv(benchmark_path, encoding='utf-8-sig')
                    # 检查基准数据是否为空
                    if len(benchmark_df) == 0 or 'close' not in benchmark_df.columns or 'date' not in benchmark_df.columns:
                        print("基准数据文件为空或缺少必要列")
                        # 创建同样的替代数据
                        if len(daily_stats_df) > 0 and 'date' in daily_stats_df.columns:
                            daily_stats_df['date'] = pd.to_datetime(daily_stats_df['date'])
                            dates = daily_stats_df['date']
                            closes = np.ones(len(dates))
                            benchmark_df = pd.DataFrame({
                                'date': dates,
                                'close': closes
                            })
                            print("创建了替代基准数据")
                        else:
                            benchmark_df = pd.DataFrame(columns=['date', 'close'])
                            print("创建了空的基准数据DataFrame")
                except Exception as e:
                    print(f"读取基准数据文件时出错: {str(e)}")
                    # 创建同样的替代数据
                    if len(daily_stats_df) > 0 and 'date' in daily_stats_df.columns:
                        daily_stats_df['date'] = pd.to_datetime(daily_stats_df['date'])
                        dates = daily_stats_df['date']
                        closes = np.ones(len(dates))
                        benchmark_df = pd.DataFrame({
                            'date': dates,
                            'close': closes
                        })
                        print("创建了替代基准数据")
                    else:
                        benchmark_df = pd.DataFrame(columns=['date', 'close'])
                        print("创建了空的基准数据DataFrame")
            
            # 检查必要的列是否存在
            required_columns = {
                'daily_stats': ['date', 'total_asset', 'cash', 'market_value', 'daily_return'],
                'benchmark': ['date', 'close'],
                'trades': ['datetime', 'code', 'action', 'price', 'volume', 'amount', 'commission']
            }
            
            # 检查并重命名列
            if 'time' in trades_df.columns:
                trades_df = trades_df.rename(columns={'time': 'datetime'})
            if 'type' in trades_df.columns:
                trades_df = trades_df.rename(columns={'type': 'action'})
            
            # 输出调试信息
            print(f"交易数据列名: {trades_df.columns.tolist()}")
            
            for df_name, columns in required_columns.items():
                df = locals()[f"{df_name}_df"]
                missing_columns = [col for col in columns if col not in df.columns]
                if missing_columns:
                    print(f"警告: {df_name} 缺少必要的列: {missing_columns}，尝试调整")
                    # 对于trades，尝试修复最常见的列名问题
                    if df_name == 'trades':
                        if 'datetime' not in df.columns and 'time' in df.columns:
                            print(f"  将'time'列重命名为'datetime'")
                            df = df.rename(columns={'time': 'datetime'})
                        if 'action' not in df.columns and 'direction' in df.columns:
                            print(f"  将'direction'列重命名为'action'")
                            df = df.rename(columns={'direction': 'action'})
                        if 'action' not in df.columns and 'type' in df.columns:
                            print(f"  将'type'列重命名为'action'")
                            df = df.rename(columns={'type': 'action'})
                        missing_columns = [col for col in columns if col not in df.columns]
                        if missing_columns:
                            print(f"  调整后仍缺少列: {missing_columns}")
                            if set(missing_columns) == {'commission'} and 'amount' in df.columns:
                                # 如果只缺少commission列，则添加一个全为0的列
                                print(f"  添加默认的'commission'列")
                                df['commission'] = 0.0
                                missing_columns = []
                            trades_df = df
                            
                    if missing_columns:
                        raise ValueError(f"{df_name} 缺少必要的列: {missing_columns}")

            # 保存原始数据供个股分析使用（在重命名列之前）
            self.trades_df = trades_df.copy()
            self.daily_stats_df = daily_stats_df.copy()

            # 重命名 trades_df 的列以匹配期望的列名
            trades_df = trades_df.rename(columns={
                'datetime': 'time',
                'action': 'direction'
            })
            
            # 输出调试信息
            print(f"重命名后的交易数据列名: {trades_df.columns.tolist()}")
            
            try:
                # 将买卖动作映射为中文
                direction_map = {
                    'buy': '买入',
                    'sell': '卖出'
                }
                trades_df['direction'] = trades_df['direction'].map(lambda x: direction_map.get(str(x).lower(), x))
                print("买卖动作映射完成")
            except Exception as e:
                print(f"买卖动作映射出错: {str(e)}")
                print(f"direction列值: {trades_df['direction'].unique().tolist() if 'direction' in trades_df.columns else 'direction列不存在'}")
            
            # 保存基准代码供后续使用（从config.csv读取，默认沪深300）
            config_row = config_df.iloc[0]
            self.benchmark_code = config_row.get('benchmark', '000300.SH') if 'benchmark' in config_row.index else '000300.SH'
            print(f"使用基准合约代码: {self.benchmark_code}")
            self.benchmark_base_price = resolve_benchmark_base_price(benchmark_df, daily_stats_df)
            self.benchmark_df = benchmark_df

            # 更新基本信息
            self.update_basic_info(config_df.iloc[0], daily_stats_df, benchmark_df)

            # 更新图表
            self.update_chart(daily_stats_df, benchmark_df)
            
            # 更新交易记录表格
            self.update_trades_table(trades_df)
            
            # 更新每日统计表格
            self.update_daily_stats_table(daily_stats_df)
            
            # 更新绩效评估图表
            self.update_performance_charts(daily_stats_df, benchmark_df)
            
        except Exception as e:
            print(f"加载回测数据时出错: {str(e)}")
            print(f"当前工作目录: {os.getcwd()}")
            import traceback
            print(traceback.format_exc())

    def set_value_color(self, label, value_str, numeric_value):
        """根据数值正负设置颜色：正数红色，负数绿色"""
        if numeric_value > 0:
            color = "#ff4444"  # 红色 
        elif numeric_value < 0:
            color = "#44ff44"  # 绿色
        else:
            color = "#e8e8e8"  # 中性色（原色）
        
        # 更新标签文本和颜色
        label.setText(value_str)
        label.setStyleSheet(f"""
            color: {color}; 
            font-size: {int(14 * self.font_scale)}px;
            font-family: 'Consolas', 'Microsoft YaHei', monospace;
            background-color: transparent;
        """)

    def update_basic_info(self, config, daily_stats_df, benchmark_df=None):
        """更新基本信息面板"""
        try:
            # 获取策略名称
            strategy_file = config.get('strategy_file', '') if hasattr(config, 'get') else config['strategy_file']
            if pd.isna(strategy_file) or not str(strategy_file).strip():
                strategy_name = os.path.basename(os.path.dirname(os.path.abspath(self.backtest_dir))) or "unknown"
            else:
                strategy_name = os.path.splitext(os.path.basename(str(strategy_file)))[0]
            self.info_labels["策略名称"].setText(strategy_name)
            self.info_labels["策略名称"].setToolTip(strategy_name)

            # 设置回测区间
            start_time = pd.to_datetime(config['start_time']).strftime('%Y-%m-%d')
            end_time = pd.to_datetime(config['end_time']).strftime('%Y-%m-%d')

            # 检查是否有每日统计数据
            if len(daily_stats_df) > 0:
                # 从实际数据中获取起止日期
                actual_start = pd.to_datetime(daily_stats_df['date'].iloc[0]).strftime('%Y-%m-%d')
                actual_end = pd.to_datetime(daily_stats_df['date'].iloc[-1]).strftime('%Y-%m-%d')
                range_start, range_end = actual_start, actual_end
            else:
                # 使用配置中的日期
                range_start, range_end = start_time, end_time
            # 用"~"分隔并换行显示，"至"字在窄列下容易被裁掉
            self.info_labels["回测区间"].setText(f"{range_start} ~\n{range_end}")
            self.info_labels["回测区间"].setToolTip(f"{range_start} 至 {range_end}")
            
            # 设置初始资金
            init_capital = float(config['init_capital'])
            self.info_labels["初始资金"].setText(f"{init_capital:,.2f}")
            
            # 检查是否有每日统计数据
            if len(daily_stats_df) > 0:
                # 计算最终资金
                final_capital = daily_stats_df['total_asset'].iloc[-1]
                self.info_labels["最终资金"].setText(f"{final_capital:,.2f}")
                
                # 计算总收益率
                if init_capital > 0:
                    total_return = (final_capital - init_capital) / init_capital * 100
                    self.set_value_color(self.info_labels["总收益率"], f"{total_return:+.2f}%", total_return)
                else:
                    self.set_value_color(self.info_labels["总收益率"], "0.00%", 0)
                
                # 计算年化收益率
                if len(daily_stats_df) > 1:
                    # 使用daily_stats_df中的实际日期计算天数
                    first_date = pd.to_datetime(daily_stats_df['date'].iloc[0]).strftime('%Y-%m-%d')
                    last_date = pd.to_datetime(daily_stats_df['date'].iloc[-1]).strftime('%Y-%m-%d')
                    
                    # 计算交易日天数
                    tools = KhQuTools()
                    trade_days_count = tools.get_trade_days_count(first_date, last_date)
                    
                    # 如果交易日计算失败，则使用日历天数作为备选方案
                    if trade_days_count <= 0:
                        days = (pd.to_datetime(last_date) - pd.to_datetime(first_date)).days
                        print(f"警告：无法获取交易日天数，使用日历天数 {days} 作为替代")
                    else:
                        days = trade_days_count
                        print(f"使用交易日天数: {days}")
                    
                    if days > 0 and init_capital > 0:
                        # 使用公式 ((1+R)^(250/n)-1)*100% 计算年化收益率
                        # 其中R是总收益率(小数形式)，n是交易日天数，250是一年的交易日数
                        total_return_decimal = (final_capital / init_capital) - 1
                        annual_return = (pow(1 + total_return_decimal, 250/days) - 1) * 100
                        self.set_value_color(self.info_labels["年化收益率"], f"{annual_return:+.2f}%", annual_return)
                    else:
                        self.set_value_color(self.info_labels["年化收益率"], "0.00%", 0)
                else:
                    self.set_value_color(self.info_labels["年化收益率"], "0.00%", 0)
                
                # 计算最大回撤
                max_drawdown = self.calculate_max_drawdown(daily_stats_df['total_asset'])
                self.info_labels["最大回撤"].setText(f"{max_drawdown:.2f}%")
                
                # 计算夏普比率
                if 'daily_return' in daily_stats_df.columns:
                    daily_returns = daily_stats_df['daily_return']
                    
                    # 添加调试输出
                    print(f"daily_returns数据类型: {type(daily_returns)}")
                    print(f"daily_returns长度: {len(daily_returns)}")
                    print(f"daily_returns是否包含NaN: {daily_returns.isna().any()}")
                    print(f"daily_returns非NaN值的数量: {daily_returns.count()}")
                    print(f"daily_returns前5个值: {daily_returns.head(5).tolist()}")
                    
                    sharpe_ratio = self.calculate_sharpe_ratio(daily_returns)
                    self.set_value_color(self.info_labels["夏普比率"], f"{sharpe_ratio:+.2f}", sharpe_ratio)
                else:
                    self.set_value_color(self.info_labels["夏普比率"], "0.00", 0)
                    print("警告: daily_stats_df中没有'daily_return'列")
                
                # 计算索提诺比率
                if 'daily_return' in daily_stats_df.columns:
                    sortino_ratio = self.calculate_sortino_ratio(daily_stats_df['daily_return'])
                    self.set_value_color(self.info_labels["索提诺比率"], f"{sortino_ratio:+.2f}", sortino_ratio)
                else:
                    self.set_value_color(self.info_labels["索提诺比率"], "0.00", 0)
                
                # 计算阿尔法和贝塔
                try:
                    local_benchmark_df = benchmark_df
                    if local_benchmark_df is None:
                        local_benchmark_df = getattr(self, 'benchmark_df', None)
                    if local_benchmark_df is None:
                        benchmark_path = os.path.join(self.backtest_dir, "benchmark.csv")
                        if os.path.exists(benchmark_path):
                            local_benchmark_df = pd.read_csv(benchmark_path, encoding='utf-8-sig')

                    if local_benchmark_df is not None:
                        local_benchmark_df = local_benchmark_df.copy()
                        if len(local_benchmark_df) > 0 and 'date' in local_benchmark_df.columns and 'close' in local_benchmark_df.columns:
                            local_benchmark_df['date'] = pd.to_datetime(local_benchmark_df['date'])
                            local_benchmark_df = local_benchmark_df.sort_values('date')

                            # 拆分 benchmark.csv：前几行是回测前数据，后面是回测期间数据
                            daily_stats_df['date'] = pd.to_datetime(daily_stats_df['date'])
                            backtest_start = daily_stats_df['date'].min()
                            bt_bench = local_benchmark_df[local_benchmark_df['date'] >= backtest_start].copy()

                            base_price = getattr(self, 'benchmark_base_price', None)
                            if base_price is None:
                                base_price = resolve_benchmark_base_price(local_benchmark_df, daily_stats_df)

                            end_price = float(bt_bench['close'].iloc[-1]) if len(bt_bench) > 0 else base_price
                            benchmark_return = ((end_price - base_price) / base_price) * 100 if base_price > 0 else 0

                            if "基准收益率" in self.info_labels:
                                self.set_value_color(self.info_labels["基准收益率"], f"{benchmark_return:+.2f}%", benchmark_return)

                            annualized_benchmark_return = 0
                            if days > 0:
                                annualized_benchmark_return = self.calculate_annualized_benchmark_return(benchmark_return, days)
                                if "基准年化收益率" in self.info_labels:
                                    self.set_value_color(self.info_labels["基准年化收益率"], f"{annualized_benchmark_return:+.2f}%", annualized_benchmark_return)

                            # 用回测期间的数据计算日收益率（含第一天相对base的收益率）
                            bt_bench_ret = bt_bench['close'].pct_change()
                            if len(bt_bench_ret) > 0 and base_price > 0:
                                bt_bench_ret.iloc[0] = (float(bt_bench['close'].iloc[0]) - base_price) / base_price
                            bt_bench = bt_bench.copy()
                            bt_bench['return'] = bt_bench_ret.values
                            merged_df = pd.merge(daily_stats_df[['date', 'daily_return']], 
                                               bt_bench[['date', 'return']], 
                                               on='date', how='inner')
                            
                            if len(merged_df) > 10:
                                _, beta = self.calculate_alpha_beta(merged_df['daily_return'], merged_df['return'])
                                self.info_labels["贝塔"].setText(f"{beta:+.4f}")
                                
                                if days > 0 and init_capital > 0:
                                    alpha = self.calculate_alpha(annual_return, annualized_benchmark_return, beta)
                                    self.set_value_color(self.info_labels["阿尔法"], f"{alpha:+.4f}", alpha)
                                else:
                                    self.info_labels["阿尔法"].setText("0.0000")
                            else:
                                self.info_labels["阿尔法"].setText("0.0000")
                                self.info_labels["贝塔"].setText("0.0000")
                        else:
                            self.info_labels["阿尔法"].setText("0.0000")
                            self.info_labels["贝塔"].setText("0.0000")
                    else:
                        self.info_labels["阿尔法"].setText("0.0000")
                        self.info_labels["贝塔"].setText("0.0000")
                except Exception as e:
                    print(f"计算阿尔法和贝塔时出错: {str(e)}")
                    self.info_labels["阿尔法"].setText("0.0000")
                    self.info_labels["贝塔"].setText("0.0000")
                
                # 计算年化波动率
                if 'daily_return' in daily_stats_df.columns:
                    volatility = self.calculate_volatility(daily_stats_df['daily_return'])  # 使用新的计算方法
                    self.info_labels["年化波动率"].setText(f"{volatility * 100:.2f}%")  # 显示百分比，乘以100并保留2位小数
                else:
                    self.info_labels["年化波动率"].setText("0.00%")
                
                # 尝试加载交易记录，计算交易相关指标
                try:
                    trades_path = os.path.join(self.backtest_dir, "trades.csv")
                    loaded_trades_df = getattr(self, 'trades_df', None)
                    if loaded_trades_df is not None:
                        trades_df = loaded_trades_df.copy()
                    elif os.path.exists(trades_path):
                        trades_df = pd.read_csv(trades_path, encoding='utf-8-sig')
                    else:
                        trades_df = None

                    if trades_df is not None:
                        
                        # 输出调试信息
                        print(f"update_basic_info中的交易数据列名: {trades_df.columns.tolist()}")
                        
                        # 确保交易记录包含必要的列 - 先检查并尝试修复
                        required_columns = ['time', 'code', 'direction', 'price', 'volume', 'amount']
                        
                        # 尝试修复常见的列名问题
                        column_mappings = {
                            'datetime': 'time',
                            'action': 'direction',
                            'type': 'direction'
                        }
                        
                        # 检查是否需要重命名列
                        for old_col, new_col in column_mappings.items():
                            if old_col in trades_df.columns and new_col not in trades_df.columns:
                                print(f"  将'{old_col}'列重命名为'{new_col}'")
                                trades_df = trades_df.rename(columns={old_col: new_col})
                        
                        # 输出修正后的列名
                        print(f"修正后的交易数据列名: {trades_df.columns.tolist()}")
                        
                        if all(col in trades_df.columns for col in required_columns):
                            # 进行必要的数据转换
                            if 'direction' in trades_df.columns:
                                # 将买卖动作映射为中文
                                try:
                                    direction_map = {
                                        'buy': '买入',
                                        'sell': '卖出',
                                        1: '买入',   # 添加数字映射
                                        -1: '卖出'   # 添加数字映射
                                    }
                                    # 输出direction列的值以进行调试
                                    unique_directions = trades_df['direction'].unique()
                                    print(f"交易方向唯一值: {unique_directions}")
                                    
                                    # 确保映射函数适用于各种数据类型
                                    trades_df['direction'] = trades_df['direction'].apply(
                                        lambda x: direction_map.get(x, x) if isinstance(x, (int, float)) 
                                               else direction_map.get(str(x).lower(), x))
                                except Exception as e:
                                    print(f"买卖动作映射出错: {str(e)}")
                            
                            # 计算胜率和盈亏比
                            win_rate, profit_ratio = self.calculate_win_rate_and_profit_ratio(trades_df)
                            self.info_labels["胜率"].setText(f"{win_rate:.2%}")
                            self.info_labels["盈亏比"].setText("∞" if profit_ratio == float('inf') else f"{profit_ratio:.2f}")
                            
                            # 计算交易相关指标
                            daily_trades, max_win_streak, max_loss_streak, max_profit, max_loss = self.calculate_trading_metrics(trades_df, daily_stats_df)
                            self.info_labels["日均交易次数"].setText(f"{daily_trades:.2f}")
                            self.info_labels["最大连续盈利"].setText(f"{max_win_streak}天")
                            self.info_labels["最大连续亏损"].setText(f"{max_loss_streak}天")
                            self.info_labels["最大单笔盈利"].setText(f"{max_profit:,.2f}")
                            self.info_labels["最大单笔亏损"].setText(f"{max_loss:,.2f}")
                        else:
                            # 输出缺少的列
                            missing_columns = [col for col in required_columns if col not in trades_df.columns]
                            print(f"缺少必要的列: {missing_columns}")
                            # 设置默认值
                            self.info_labels["胜率"].setText("0.00%")
                            self.info_labels["盈亏比"].setText("0.00")
                            self.info_labels["日均交易次数"].setText("0.00")
                            self.info_labels["最大连续盈利"].setText("0天")
                            self.info_labels["最大连续亏损"].setText("0天")
                            self.info_labels["最大单笔盈利"].setText("0.00")
                            self.info_labels["最大单笔亏损"].setText("0.00")
                    else:
                        # 设置默认值
                        self.info_labels["胜率"].setText("0.00%")
                        self.info_labels["盈亏比"].setText("0.00")
                        self.info_labels["日均交易次数"].setText("0.00")
                        self.info_labels["最大连续盈利"].setText("0天")
                        self.info_labels["最大连续亏损"].setText("0天")
                        self.info_labels["最大单笔盈利"].setText("0.00")
                        self.info_labels["最大单笔亏损"].setText("0.00")
                except Exception as e:
                    print(f"计算交易指标时出错: {str(e)}")
                    # 设置默认值
                    self.info_labels["胜率"].setText("0.00%")
                    self.info_labels["盈亏比"].setText("0.00")
                    self.info_labels["日均交易次数"].setText("0.00")
                    self.info_labels["最大连续盈利"].setText("0天")
                    self.info_labels["最大连续亏损"].setText("0天")
                    self.info_labels["最大单笔盈利"].setText("0.00")
                    self.info_labels["最大单笔亏损"].setText("0.00")
            else:
                # 如果没有每日统计数据，显示初始值
                self.info_labels["最终资金"].setText(f"{init_capital:,.2f}")
                self.info_labels["总收益率"].setText("0.00%")
                self.info_labels["年化收益率"].setText("0.00%")
                self.info_labels["最大回撤"].setText("0.00%")
                self.info_labels["夏普比率"].setText("0.00")
                self.info_labels["索提诺比率"].setText("0.00")
                self.info_labels["阿尔法"].setText("0.0000")
                self.info_labels["贝塔"].setText("0.0000")
                self.info_labels["胜率"].setText("0.00%")
                self.info_labels["盈亏比"].setText("0.00")
                self.info_labels["日均交易次数"].setText("0.00")
                self.info_labels["最大连续盈利"].setText("0天")
                self.info_labels["最大连续亏损"].setText("0天")
                self.info_labels["最大单笔盈利"].setText("0.00")
                self.info_labels["最大单笔亏损"].setText("0.00")
                self.info_labels["年化波动率"].setText("0.00%")
            
        except Exception as e:
            print(f"更新基本信息时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())
            # 设置所有标签为默认值
            for label in self.info_labels.values():
                label.setText("--")

    def calculate_max_drawdown(self, values):
        """计算最大回撤"""
        try:
            # 检查输入有效性
            if values is None or len(values) < 2:
                return 0.0
            
            # 确保数据是数值类型
            values = pd.to_numeric(values, errors='coerce')
            
            # 移除空值
            values = values.dropna()
            
            if len(values) < 2:
                return 0.0
                
            # 计算累计最大值
            cummax = values.cummax()
            
            # 添加除零保护
            with np.errstate(divide='ignore', invalid='ignore'):
                drawdown = (cummax - values) / cummax * 100
                
            # 替换无效值
            drawdown = drawdown.replace([np.inf, -np.inf], np.nan)
            drawdown = drawdown.fillna(0)
            
            return drawdown.max()
        except Exception as e:
            print(f"计算最大回撤时出错: {str(e)}")
            return 0.0

    def calculate_sharpe_ratio(self, returns, risk_free_rate=None):
        """计算夏普比率
        
        Sharpe Ratio = (R_a - R_f) / δ_p
        
        其中：
        - R_a 为策略年化收益率
        - R_f 为无风险利率（当前十年期国债利率平均值）
        - δ_p 为策略收益波动率
        
        Args:
            returns: 收益率序列
            risk_free_rate: 无风险利率，默认为None（使用内部设置的值）
            
        Returns:
            float: 夏普比率
        """
        try:
            # 如果未提供风险收益率，使用实例变量
            if risk_free_rate is None:
                risk_free_rate = self.risk_free_rate
                
            # 检查是否有足够的数据
            if returns is None:
                print("警告: 收益率数据为None")
                return 0.0
                
            if len(returns) < 2:
                print(f"警告: 收益率数据点不足，当前仅有 {len(returns)} 个点")
                return 0.0
            
            # 确保数据是数值类型
            returns = pd.to_numeric(returns, errors='coerce')
            
            # 检查数据中是否全为NaN
            if returns.isna().all():
                print("警告: 收益率数据全部为NaN")
                return 0.0
            
            # 移除空值
            valid_returns = returns.dropna()
            if len(valid_returns) < 2:
                print(f"警告: 移除NaN后，收益率数据点不足，仅有 {len(valid_returns)} 个点")
                return 0.0
                
            print(f"有效收益率数据点: {len(valid_returns)}/{len(returns)}")
            
            # 计算总收益率
            total_return_decimal = (1 + valid_returns).prod() - 1
            
            # 获取天数
            days = len(valid_returns)
            
            # 计算年化收益率 R_a
            annual_return = (pow(1 + total_return_decimal, 250/days) - 1)
            
            # 计算收益波动率 δ_p
            volatility = self.calculate_volatility(valid_returns)
            
            # 检查波动率是否为0
            if volatility == 0 or pd.isna(volatility):
                print("警告: 收益率波动率为0或NaN")
                return 0.0
            
            # 计算夏普比率 Sharpe Ratio = (R_a - R_f) / δ_p
            sharpe = (annual_return - risk_free_rate) / volatility
            
            # 检查结果是否为有效数值
            if np.isnan(sharpe) or np.isinf(sharpe):
                print(f"警告: 计算出的夏普比率为无效值 {sharpe}")
                return 0.0
            
            print(f"计算得出夏普比率: {sharpe:.4f}")
            return sharpe
        
        except Exception as e:
            print(f"计算夏普比率时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return 0.0

    def update_chart(self, daily_stats_df, benchmark_df):
        """更新收益曲线图表、回撤分析图、盈亏分析图和成交记录图"""
        try:
            # 定义字体大小（与create_chart中保持一致）
            title_fontsize = int(14 * self.font_scale)
            label_fontsize = int(11 * self.font_scale)
            tick_fontsize = int(10 * self.font_scale)
            
            # 清除之前的图表内容
            self.ax.clear()
            self.ax_drawdown.clear()
            self.ax_pnl.clear()  # 清除盈亏分析图
            self.ax_trades.clear()  # 清除成交记录图
            
            # 重新设置样式（因为clear会重置样式）
            self.ax.set_facecolor('#2d2d2d')
            self.ax_drawdown.set_facecolor('#2d2d2d')
            self.ax_pnl.set_facecolor('#2d2d2d')  # 盈亏分析图背景色
            self.ax_trades.set_facecolor('#2d2d2d')  # 成交记录图背景色
            
            # 重新设置标题和轴标签字体
            self.ax.set_title("策略收益与基准对比", color='#e8e8e8', pad=int(10 * self.font_scale),
                             fontsize=title_fontsize, fontweight='bold')
            self.ax.set_ylabel("净值", color='#a0a0a0', fontsize=label_fontsize)
            self.ax_drawdown.set_ylabel("回撤率 (%)", color='#a0a0a0', fontsize=label_fontsize)
            self.ax_pnl.set_ylabel("日盈亏", color='#a0a0a0', fontsize=label_fontsize)
            self.ax_trades.set_ylabel("买入/卖出量", color='#a0a0a0', fontsize=label_fontsize)
            #self.ax_trades.set_xlabel("时间", color='#a0a0a0', fontsize=label_fontsize)
            
            # 设置刻度字体大小
            for ax in [self.ax, self.ax_drawdown, self.ax_pnl, self.ax_trades]:
                ax.tick_params(axis='both', colors='#a0a0a0', labelsize=tick_fontsize)
                ax.grid(True, linestyle='--', alpha=0.1, color='#808080')
                for spine in ax.spines.values():
                    spine.set_color('#404040')

            # clear()会重置刻度可见性：重新隐藏上方三个子图的X轴日期标签，
            # 只在最下方的成交记录子图显示日期，避免子图之间文字重叠
            self.ax.tick_params(axis='x', labelbottom=False)
            self.ax_drawdown.tick_params(axis='x', labelbottom=False)
            self.ax_pnl.tick_params(axis='x', labelbottom=False)

            # 三个小子图高度有限，限制Y轴刻度数量，避免刻度文字拥挤、与相邻子图重叠
            for ax in [self.ax_drawdown, self.ax_pnl, self.ax_trades]:
                ax.yaxis.set_major_locator(ticker.MaxNLocator(nbins=4))

            # 盈亏/成交量子图的刻度用"万/亿"缩写，避免长数字把轴标题挤出画布
            amount_formatter = ticker.FuncFormatter(self._format_amount_tick)
            self.ax_pnl.yaxis.set_major_formatter(amount_formatter)
            self.ax_trades.yaxis.set_major_formatter(amount_formatter)

            # 反转回撤图的y轴
            self.ax_drawdown.invert_yaxis()
            
            # 检查数据有效性
            if len(daily_stats_df) == 0:
                self.ax.text(0.5, 0.5, '没有策略收益数据可显示', 
                           horizontalalignment='center', verticalalignment='center',
                           transform=self.ax.transAxes, fontsize=14)
                self.chart_view.draw()
                return
            
            # 确保日期列存在且格式正确
            daily_stats_df['date'] = pd.to_datetime(daily_stats_df['date'])
            if len(benchmark_df) > 0:
                benchmark_df['date'] = pd.to_datetime(benchmark_df['date'])
            
            # 计算策略净值序列
            initial_value = daily_stats_df['total_asset'].iloc[0]
            if initial_value == 0:
                self.ax.text(0.5, 0.5, '初始资产为0，无法计算收益率', 
                           horizontalalignment='center', verticalalignment='center',
                           transform=self.ax.transAxes, fontsize=14)
                self.chart_view.draw()
                return
                
            strategy_values = daily_stats_df['total_asset'] / initial_value
            
            # 获取日期序列
            dates = daily_stats_df['date']
            
            # 保存日期和策略值以便鼠标悬停时使用
            self.dates = dates
            self.strategy_values = strategy_values
            
            # 设置图表颜色和样式
            strategy_color = '#007acc'  # 策略曲线使用蓝色
            benchmark_color = '#ff9900'  # 基准曲线使用橙色
            drawdown_color = '#ff4444'  # 回撤曲线使用红色
            profit_color = '#ff4444'  # 盈利柱状图使用红色
            loss_color = '#00cc00'  # 亏损柱状图使用绿色
            buy_color = '#ff4444'  # 买入柱状图使用红色
            sell_color = '#007acc'  # 卖出柱状图使用蓝色
            
            # 绘制上方子图的策略曲线
            strategy_line = self.ax.plot(dates, strategy_values, label='策略收益', color=strategy_color, linewidth=2.5)[0]
            
            # 处理基准数据
            if len(benchmark_df) > 0 and 'close' in benchmark_df.columns:
                try:
                    # 计算基准净值序列
                    benchmark_initial = getattr(self, 'benchmark_base_price', None)
                    if benchmark_initial is None:
                        benchmark_initial = resolve_benchmark_base_price(benchmark_df, daily_stats_df)
                    if benchmark_initial is None:
                        benchmark_initial = float(benchmark_df['close'].iloc[0])
                    if not np.isfinite(benchmark_initial) or benchmark_initial == 0:
                        print("警告：基准初始值为0，使用1作为替代值")
                        benchmark_initial = 1.0
                    print(f"使用本地回测结果中的基准初始值 {benchmark_initial} 计算基准净值")
                    
                    # 用基准初始值计算基准净值序列
                    benchmark_values = benchmark_df['close'] / benchmark_initial
                    
                    # 处理基准数据长度不匹配的问题
                    # 找出基准数据和策略数据共同的日期范围
                    common_dates = pd.Series(dates).isin(benchmark_df['date'])
                    if any(common_dates):
                        # 如果有共同日期，使用共同日期绘制基准曲线
                        common_strategy_dates = dates[common_dates]
                        common_strategy_values = strategy_values[common_dates]
                        
                        # 找出基准数据中对应的索引
                        benchmark_indices = benchmark_df['date'].isin(common_strategy_dates)
                        benchmark_common_dates = benchmark_df['date'][benchmark_indices]
                        benchmark_common_values = benchmark_values[benchmark_indices]
                        
                        # 确保日期顺序一致
                        benchmark_data = pd.DataFrame({
                            'date': benchmark_common_dates,
                            'value': benchmark_common_values
                        }).sort_values('date')
                        
                        # 绘制基准曲线
                        benchmark_line = self.ax.plot(
                            benchmark_data['date'], 
                            benchmark_data['value'], 
                            label='基准收益', 
                            color=benchmark_color, 
                            linewidth=2.5
                        )[0]
                        
                        # 存储曲线数据用于后续查找最近点
                        self.benchmark_line = benchmark_line
                        self.benchmark_values = benchmark_data['value'].values
                        self.benchmark_dates = benchmark_data['date'].values
                    else:
                        # 如果没有共同日期但有基准数据，尝试重新对齐日期
                        print("基准数据与策略数据没有共同的日期，尝试重新对齐")
                        
                        # 其他基准处理代码...
                except Exception as e:
                    print(f"处理基准数据时出错: {str(e)}")
            
            # 添加图例
            main_legend_fontsize = int(10 * self.font_scale)  # 适中的主图图例字体
            self.ax.legend(loc='upper left', facecolor='#333333', edgecolor='#404040', framealpha=0.9, fancybox=True, shadow=True, fontsize=main_legend_fontsize)
            # 
            self.ax.text(0.5, 0.5, 'khQuant', 
                        horizontalalignment='center', verticalalignment='center',
                        transform=self.ax.transAxes, fontsize=60, alpha=0.1, 
                        color='#888888', fontweight='bold', 
                        zorder=0)
            # =================== 绘制回撤曲线 ===================
            # 计算回撤序列
            total_assets = daily_stats_df['total_asset']
            cummax = total_assets.cummax()
            
            # 添加除零保护
            with np.errstate(divide='ignore', invalid='ignore'):
                drawdown = (cummax - total_assets) / cummax * 100
            
            # 替换无效值
            drawdown = drawdown.replace([np.inf, -np.inf], np.nan)
            drawdown = drawdown.fillna(0)
            
            # 保存回撤数据以便鼠标悬停时使用
            self.drawdown_values = drawdown.values
            
            # 绘制回撤曲线
            self.ax_drawdown.fill_between(dates, drawdown, 0, alpha=0.3, color=drawdown_color)
            self.ax_drawdown.plot(dates, drawdown, color=drawdown_color, linewidth=1.5, label='回撤')
            
            # 标注最大回撤
            max_dd = drawdown.max()
            max_dd_pos = drawdown.values.argmax()
            max_dd_date = dates[max_dd_pos]

            # 给回撤子图底部留出余量，避免最大回撤点贴着子图边缘
            dd_span = max(max_dd, 0.1)
            self.ax_drawdown.set_ylim(dd_span * 1.25, -dd_span * 0.06)

            self.ax_drawdown.scatter(max_dd_date, max_dd, color='white', s=50, zorder=5)

            # 计算最大回撤点在时间轴上的相对位置
            date_range = dates.max() - dates.min()
            relative_pos = (max_dd_date - dates.min()) / date_range if date_range.total_seconds() > 0 else 0.5

            # 标注文本固定锚在子图内部的下角（与回撤点相反的一侧），
            # 用axes fraction坐标保证无论回撤点在哪里，文字都完整显示在子图内，
            # 不会像之前向下偏移那样画出子图边界、压到下方的盈亏图上
            if relative_pos > 0.5:
                text_pos = (0.02, 0.10)
                ha = 'left'
            else:
                text_pos = (0.98, 0.10)
                ha = 'right'

            annotation_fontsize = int(10 * self.font_scale)  # 适中的标注字体
            self.ax_drawdown.annotate(f"最大回撤: {max_dd:.2f}%",
                                    xy=(mdates.date2num(max_dd_date), max_dd),
                                    xycoords='data',
                                    xytext=text_pos,
                                    textcoords='axes fraction',
                                    bbox=dict(boxstyle='round,pad=0.5', fc='#333333', ec='#404040', alpha=0.9),
                                    color='#e8e8e8',
                                    fontsize=annotation_fontsize,
                                    ha=ha,
                                    va='bottom',
                                    zorder=6,
                                    # 近似直线的箭头：大弧度在长距离下会向下弯出子图边界
                                    arrowprops=dict(arrowstyle='->', color='#a0a0a0', connectionstyle='arc3,rad=0.05'))
            
            # 添加回撤图例
            legend_fontsize = int(10 * self.font_scale)  # 适中的图例字体
            self.ax_drawdown.legend(loc='upper right', facecolor='#333333', edgecolor='#404040', framealpha=0.9, fancybox=True, shadow=True, fontsize=legend_fontsize)
            
            # =================== 绘制盈亏分析图 ===================
            # 计算每日盈亏金额
            daily_stats_df['pnl'] = daily_stats_df['total_asset'].diff()
            daily_stats_df.loc[daily_stats_df.index[0], 'pnl'] = 0  # 第一天的盈亏设为0
            
            # 保存盈亏数据以便鼠标悬停时使用
            self.pnl_values = daily_stats_df['pnl'].values
            
            # 分别绘制盈利和亏损的柱状图
            pnl_positive = daily_stats_df[daily_stats_df['pnl'] > 0].copy()
            pnl_negative = daily_stats_df[daily_stats_df['pnl'] < 0].copy()
            
            # 绘制盈利柱状图（红色）
            if not pnl_positive.empty:
                self.ax_pnl.bar(pnl_positive['date'], pnl_positive['pnl'], 
                              color=profit_color, alpha=0.7, label='盈利',
                              width=0.8)  # 调整宽度使柱状图更加美观
            
            # 绘制亏损柱状图（绿色）
            if not pnl_negative.empty:
                self.ax_pnl.bar(pnl_negative['date'], pnl_negative['pnl'], 
                              color=loss_color, alpha=0.7, label='亏损',
                              width=0.8)  # 调整宽度使柱状图更加美观
            
            # 添加盈亏分析图图例
            pnl_legend_fontsize = int(10 * self.font_scale)  # 适中的盈亏分析图图例字体
            self.ax_pnl.legend(loc='upper right', facecolor='#333333', edgecolor='#404040', framealpha=0.9, fancybox=True, shadow=True, fontsize=pnl_legend_fontsize)
            
            # =================== 绘制成交记录图 ===================
            trades_df = getattr(self, '_chart_trades_df', pd.DataFrame()).copy()

            if len(trades_df) > 0:
                try:
                    # 检查并重命名列
                    if 'time' in trades_df.columns:
                        trades_df['datetime'] = trades_df['time']
                    elif 'datetime' not in trades_df.columns:
                        print("警告: 交易记录缺少时间列")
                        trades_df['datetime'] = pd.NaT
                    
                    if 'action' in trades_df.columns:
                        trades_df['direction'] = trades_df['action']
                    elif 'type' in trades_df.columns:
                        trades_df['direction'] = trades_df['type']
                    elif 'direction' not in trades_df.columns:
                        print("警告: 交易记录缺少交易方向列")
                        trades_df['direction'] = ''
                    
                    if 'volume' not in trades_df.columns:
                        print("警告: 交易记录缺少成交量列")
                        trades_df['volume'] = 0
                    
                    # 转换日期列
                    trades_df['datetime'] = pd.to_datetime(trades_df['datetime'])
                    trades_df['date'] = trades_df['datetime'].dt.date
                    
                    # 计算每日买入和卖出总量
                    daily_trades = trades_df.groupby(['date', 'direction']).agg({'volume': 'sum'}).reset_index()
                    
                    # 转换日期为datetime以便绘图
                    daily_trades['date'] = pd.to_datetime(daily_trades['date'])
                    
                    # 将方向映射为买入和卖出
                    # 定义映射函数
                    def map_direction(direction):
                        direction = str(direction).lower()
                        if direction in ['buy', '买入']:
                            return '买入'
                        elif direction in ['sell', '卖出']:
                            return '卖出'
                        return direction
                    
                    daily_trades['direction'] = daily_trades['direction'].apply(map_direction)
                    
                    # 分别获取买入和卖出成交量
                    buy_trades = daily_trades[daily_trades['direction'] == '买入'].copy()
                    sell_trades = daily_trades[daily_trades['direction'] == '卖出'].copy()
                    
                    # 保存交易数据以便鼠标悬停时使用
                    # 创建日期索引的交易数据字典
                    self.daily_buy_volume = {}
                    self.daily_sell_volume = {}
                    
                    for _, row in buy_trades.iterrows():
                        self.daily_buy_volume[row['date']] = row['volume']
                    
                    for _, row in sell_trades.iterrows():
                        self.daily_sell_volume[row['date']] = row['volume']
                    
                    # 绘制买入柱状图（红色，正值）
                    if not buy_trades.empty:
                        self.ax_trades.bar(buy_trades['date'], buy_trades['volume'], 
                                         color=buy_color, alpha=0.7, label='买入',
                                         width=0.8)  # 调整宽度使柱状图更加美观
                    
                    # 绘制卖出柱状图（绿色，负值，即卖出显示在x轴以下）
                    if not sell_trades.empty:
                        self.ax_trades.bar(sell_trades['date'], -sell_trades['volume'], 
                                         color=sell_color, alpha=0.7, label='卖出',
                                         width=0.8)  # 调整宽度使柱状图更加美观
                    
                    # 添加成交记录图图例
                    trades_legend_fontsize = int(10 * self.font_scale)  # 适中的成交记录图图例字体
                    self.ax_trades.legend(loc='upper right', facecolor='#333333', edgecolor='#404040', framealpha=0.9, fancybox=True, shadow=True, fontsize=trades_legend_fontsize)
                    
                except Exception as e:
                    print(f"处理交易记录时出错: {str(e)}")
                    import traceback
                    print(traceback.format_exc())
            
            # 设置适当的日期定位器和格式化器
            locator = mdates.AutoDateLocator()
            formatter = mdates.DateFormatter('%Y-%m-%d')
            
            for ax in [self.ax_drawdown, self.ax_pnl, self.ax_trades]:
                ax.xaxis.set_major_locator(locator)
                ax.xaxis.set_major_formatter(formatter)
                # 旋转日期标签并设置字体大小
                plt.setp(ax.get_xticklabels(), rotation=45, ha='right', fontsize=tick_fontsize)
            
            # 调整布局以确保标题和坐标轴完整显示（与create_chart/resize事件保持一致）
            self._apply_chart_margins()

            # 重绘图表
            self.chart_view.draw()
            
        except Exception as e:
            print(f"更新图表时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())

    def hover(self, event):
        """处理鼠标悬停事件，显示垂直参考线和数据点"""
        try:
            # 检查鼠标是否在图表区域内
            if not event.inaxes:
                # 如果存在垂直线，则隐藏它们（安全地设置可见性）
                def safe_set_visible(obj, visible=False):
                    if obj:
                        try:
                            obj.set_visible(visible)
                        except Exception as e:
                            print(f"警告: 无法设置对象可见性: {str(e)}")
                
                safe_set_visible(self.v_line_ax)
                safe_set_visible(self.v_line_drawdown)
                safe_set_visible(self.v_line_pnl)
                safe_set_visible(self.v_line_trades)
                safe_set_visible(self.strategy_point)
                safe_set_visible(self.benchmark_point)
                safe_set_visible(self.drawdown_point)
                safe_set_visible(self.pnl_point)
                
                # 隐藏标注点
                if hasattr(self, 'hover_annotation') and self.hover_annotation:
                    try:
                        self.hover_annotation.remove()
                    except (ValueError, AttributeError, TypeError) as e:
                        # 捕获更多可能的异常类型，包括TypeError
                        print(f"警告: 无法移除悬浮窗，可能已经被移除: {str(e)}")
                        # 对于无法移除的情况，将属性设为None以便后续重新创建
                    finally:
                        # 无论成功与否，都将hover_annotation设为None
                        self.hover_annotation = None
                
                # 重绘图表
                self.chart_view.draw_idle()
                return
            
            # 获取当前鼠标的x位置（日期）
            mouse_date = mdates.num2date(event.xdata).replace(tzinfo=None)  # 移除时区信息
            
            # 确保 self.dates 和 self.strategy_values 已经被定义
            if not hasattr(self, 'dates') or not hasattr(self, 'strategy_values'):
                # 这些变量应该在 update_chart 中设置
                print("警告: 图表数据尚未初始化")
                return
            
            # 查找最接近的数据点
            dates_array = []
            for d in self.dates:
                if isinstance(d, pd.Timestamp):
                    dates_array.append(d.to_pydatetime())
                elif isinstance(d, np.datetime64):
                    dates_array.append(pd.Timestamp(d).to_pydatetime())
                else:
                    dates_array.append(d)
            
            # 找到最近的数据点
            idx = min(range(len(dates_array)), key=lambda i: abs((dates_array[i] - mouse_date).total_seconds()))
            
            # 获取对应的日期和数值
            date = dates_array[idx]
            strategy_value = self.strategy_values[idx]
            
            # 获取回撤值
            drawdown_value = self.drawdown_values[idx] if hasattr(self, 'drawdown_values') else 0
            
            # 获取盈亏值
            pnl_value = self.pnl_values[idx] if hasattr(self, 'pnl_values') else 0
            
            # 获取交易数据
            buy_volume = 0
            sell_volume = 0
            
            if hasattr(self, 'daily_buy_volume') and hasattr(self, 'daily_sell_volume'):
                # 将日期转换为pandas Timestamp，以便在字典中查找
                pd_date = pd.Timestamp(date)
                
                # 寻找当天的买入和卖出量
                if pd_date in self.daily_buy_volume:
                    buy_volume = self.daily_buy_volume[pd_date]
                if pd_date in self.daily_sell_volume:
                    sell_volume = self.daily_sell_volume[pd_date]
            
            # 获取基准值
            benchmark_value = None
            if hasattr(self, 'benchmark_dates') and hasattr(self, 'benchmark_values'):
                if len(self.benchmark_dates) > 0 and len(self.benchmark_values) > 0:
                    # 找到基准数据中对应的点
                    benchmark_dates_array = []
                    for d in self.benchmark_dates:
                        if isinstance(d, pd.Timestamp):
                            benchmark_dates_array.append(d.to_pydatetime())
                        elif isinstance(d, np.datetime64):
                            benchmark_dates_array.append(pd.Timestamp(d).to_pydatetime())
                        else:
                            benchmark_dates_array.append(d)
                    
                    # 找到基准数据中最接近的点
                    if len(benchmark_dates_array) > 0:
                        b_idx = min(range(len(benchmark_dates_array)), 
                                    key=lambda i: abs((benchmark_dates_array[i] - mouse_date).total_seconds()))
                        benchmark_value = self.benchmark_values[b_idx]
            
            # 在所有子图中绘制垂直线
            x_date = mdates.date2num(date)
            
            # 定义一个安全地更新垂直线的函数
            def safe_update_vline(vline, ax, x_date):
                if vline:
                    try:
                        vline.set_xdata([x_date, x_date])
                        vline.set_visible(True)
                        return vline
                    except Exception as e:
                        print(f"警告: 无法更新垂直线: {str(e)}")
                # 创建新的垂直线
                try:
                    return ax.axvline(x=x_date, color='#ffffff', linestyle='--', alpha=0.5, zorder=10)
                except Exception as e:
                    print(f"警告: 无法创建新的垂直线: {str(e)}")
                    return None
            
            # 安全地更新所有垂直线
            self.v_line_ax = safe_update_vline(self.v_line_ax, self.ax, x_date)
            self.v_line_drawdown = safe_update_vline(self.v_line_drawdown, self.ax_drawdown, x_date)
            self.v_line_pnl = safe_update_vline(self.v_line_pnl, self.ax_pnl, x_date)
            self.v_line_trades = safe_update_vline(self.v_line_trades, self.ax_trades, x_date)
            
            # 设置标注点 - 注意：scatter 返回的是 PathCollection 对象，需要重新创建而不是更新
            # 安全地移除和创建点
            def safe_remove_point(point_attr):
                if hasattr(self, point_attr) and getattr(self, point_attr):
                    try:
                        point = getattr(self, point_attr)
                        # 检查point是否为Path/PathCollection类型（scatter点）
                        if hasattr(point, 'remove'):
                            try:
                                point.remove()
                            except ValueError:
                                # 捕获"x not in list"错误并记录
                                import traceback
                                print(f"警告: 无法移除 {point_attr}，可能已经被移除")
                                if self.debug_mode:
                                    print(traceback.format_exc())
                            # 无论成功与否，将点设为None
                            setattr(self, point_attr, None)
                        else:
                            # 如果point对象没有remove方法，直接设置为None
                            setattr(self, point_attr, None)
                    except Exception as e:
                        print(f"警告: 移除 {point_attr} 时发生未知错误: {str(e)}")
                        setattr(self, point_attr, None)
            
            # 安全地移除各个点
            safe_remove_point('strategy_point')
            safe_remove_point('benchmark_point')
            safe_remove_point('drawdown_point')
            safe_remove_point('pnl_point')
            
            # 创建新的点
            self.strategy_point = self.ax.scatter([x_date], [strategy_value], color='#007acc', s=50, zorder=15)
            
            # 基准收益点
            if benchmark_value is not None:
                self.benchmark_point = self.ax.scatter([x_date], [benchmark_value], color='#ff9900', s=50, zorder=15)
            
            # 创建新的点
            self.drawdown_point = self.ax_drawdown.scatter([x_date], [drawdown_value], color='#ff4444', s=50, zorder=15)
            
            # 盈亏点（如果有值）
            if pnl_value != 0:
                pnl_color = '#ff4444' if pnl_value > 0 else '#00cc00'  # 盈利红色，亏损绿色
                self.pnl_point = self.ax_pnl.scatter([x_date], [pnl_value], color=pnl_color, s=50, zorder=15)
            
            # 计算标注文本位置
            # 为避免标注文本重叠或超出边界，判断鼠标在图表中的相对位置
            # 获取x轴范围
            x_min, x_max = self.ax.get_xlim()
            x_rel_pos = (x_date - x_min) / (x_max - x_min)
            
            # 设置标注文本位置
            x_offset = -120 if x_rel_pos > 0.7 else 15  # 如果在右侧，标注向左偏移

            # 根据数据点的纵向位置决定悬浮框显示在点的上方还是下方，避免超出图表边界
            y_min, y_max = self.ax.get_ylim()
            y_rel_pos = (strategy_value - y_min) / (y_max - y_min) if y_max > y_min else 0.5
            if y_rel_pos > 0.5:
                y_offset, v_align = -15, 'top'     # 点在上半部，悬浮框放点下方
            else:
                y_offset, v_align = 15, 'bottom'   # 点在下半部，悬浮框放点上方
            
            # 创建或更新标注文本
            formatted_date = date.strftime("%Y-%m-%d")
            
            # 构建统一的悬浮窗文本内容
            hover_text = f"日期: {formatted_date}\n策略: {strategy_value:.4f}"
            
            if benchmark_value is not None:
                hover_text += f"\n基准: {benchmark_value:.4f}"
            
            hover_text += f"\n回撤: {drawdown_value:.2f}%"
            
            # 只有当有盈亏时才显示盈亏信息，并根据正负值设置颜色提示
            if pnl_value != 0:
                if pnl_value > 0:
                    hover_text += f"\n盈亏: +{pnl_value:.2f} ↗"  # 正数用上箭头表示
                else:
                    hover_text += f"\n盈亏: {pnl_value:.2f} ↘"   # 负数用下箭头表示
            
            # 只有当有成交记录时才显示成交信息
            if buy_volume > 0 or sell_volume > 0:
                if buy_volume > 0:
                    hover_text += f"\n买入: {buy_volume}"
                if sell_volume > 0:
                    hover_text += f"\n卖出: {sell_volume}"
            
            # 如果已存在悬浮窗，则移除
            if hasattr(self, 'hover_annotation') and self.hover_annotation:
                try:
                    self.hover_annotation.remove()
                except (ValueError, AttributeError, TypeError) as e:
                    # 捕获更多可能的异常类型，包括TypeError
                    print(f"警告: 无法移除悬浮窗，可能已经被移除: {str(e)}")
                    # 对于无法移除的情况，将属性设为None以便后续重新创建
                finally:
                    # 无论成功与否，都将hover_annotation设为None
                    self.hover_annotation = None
            
            # 创建新的悬浮窗（放在策略收益图上）
            hover_fontsize = int(10 * self.font_scale)  # 适中的悬浮框字体
            self.hover_annotation = self.ax.annotate(
                hover_text,
                xy=(x_date, strategy_value),
                xytext=(x_offset, y_offset),
                textcoords="offset points",
                bbox=dict(boxstyle='round,pad=0.5', fc='#333333', ec='#404040', alpha=0.9),
                fontsize=hover_fontsize,
                color='#e8e8e8',
                ha='left' if x_rel_pos <= 0.7 else 'right',
                va=v_align
            )
            
            # 重绘图表
            self.chart_view.draw_idle()
            
        except Exception as e:
            print(f"处理鼠标悬停事件时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())

    # 交易表最多渲染的行数：超过则只显示前 N 行（完整数据仍在 trades.csv），
    # 避免几十万笔交易逐行 setItem 把主线程冻死。
    MAX_TRADE_DISPLAY_ROWS = 10000

    def update_trades_table(self, trades_df):
        """更新交易记录表格（大数据量下封顶显示 + 固定列宽，避免主线程渲染上万行卡死）"""
        from PyQt5.QtWidgets import QApplication
        try:
            table = self.trades_table
            table.setUpdatesEnabled(False)
            table.setSortingEnabled(False)
            total = len(trades_df)
            if total > 0:
                cap = self.MAX_TRADE_DISPLAY_ROWS
                display_df = trades_df.iloc[:cap] if total > cap else trades_df
                n = len(display_df)
                # 封顶时在表头注明，提示完整数据位置
                first_header = "交易时间" if total <= cap else f"交易时间（仅显示前 {cap:,}/共 {total:,} 笔，完整见 trades.csv）"
                headers = [first_header, "证券代码", "交易方向", "成交价格", "成交数量", "成交金额", "手续费", "持仓资金", "可用资金", "总资产"]
                table.clearContents()
                table.setColumnCount(len(headers))
                table.setHorizontalHeaderLabels(headers)
                table.setRowCount(n)

                header = table.horizontalHeader()
                if n > 1000:
                    # 大表：固定列宽。ResizeToContents 会扫描全表算列宽，行数大时近似 O(N^2)，是卡顿主因。
                    header.setSectionResizeMode(QHeaderView.Interactive)
                    default_widths = [160, 90, 80, 90, 90, 130, 80, 130, 130, 130]
                    for col, w in enumerate(default_widths):
                        table.setColumnWidth(col, int(w * self.font_scale))
                    header.setSectionResizeMode(5, QHeaderView.Stretch)
                else:
                    # 小表：保留自适应列宽
                    header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
                    header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
                    header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
                    header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
                    header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
                    header.setSectionResizeMode(5, QHeaderView.Stretch)
                    header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
                    header.setSectionResizeMode(7, QHeaderView.Stretch)
                    header.setSectionResizeMode(8, QHeaderView.Stretch)
                    header.setSectionResizeMode(9, QHeaderView.Stretch)

                buy_color = QColor('#ff4444')   # 买入红色
                sell_color = QColor('#007acc')  # 卖出蓝色
                # 用位置索引(.iloc)迭代，避免 iterrows 的原始索引非 0 起导致 setItem 行号错位
                for r in range(n):
                    row = display_df.iloc[r]
                    time_item = QTableWidgetItem(str(row['time']))
                    time_item.setTextAlignment(Qt.AlignCenter)
                    table.setItem(r, 0, time_item)

                    code_item = QTableWidgetItem(str(row['code']))
                    code_item.setTextAlignment(Qt.AlignCenter)
                    table.setItem(r, 1, code_item)

                    direction_item = QTableWidgetItem(str(row['direction']))
                    direction_item.setTextAlignment(Qt.AlignCenter)
                    direction_item.setForeground(buy_color if row['direction'] == '买入' else sell_color)
                    table.setItem(r, 2, direction_item)

                    price_item = QTableWidgetItem(f"{row['price']:.{self.price_decimals}f}")
                    price_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    table.setItem(r, 3, price_item)

                    volume_item = QTableWidgetItem(str(row['volume']))
                    volume_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    table.setItem(r, 4, volume_item)

                    amount_item = QTableWidgetItem(f"{row['amount']:,.2f}")
                    amount_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    table.setItem(r, 5, amount_item)

                    total_fee = row['commission']
                    if 'stamp_tax' in row:
                        total_fee += row['stamp_tax']
                    if 'transfer_fee' in row:
                        total_fee += row['transfer_fee']
                    if 'flow_fee' in row:
                        total_fee += row['flow_fee']
                    commission_item = QTableWidgetItem(f"{total_fee:.2f}")
                    commission_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    table.setItem(r, 6, commission_item)

                    market_value = row.get('market_value', 0.0) if pd.notna(row.get('market_value')) else 0.0
                    market_value_item = QTableWidgetItem(f"{float(market_value):,.2f}")
                    market_value_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    table.setItem(r, 7, market_value_item)

                    cash = row.get('cash', 0.0) if pd.notna(row.get('cash')) else 0.0
                    cash_item = QTableWidgetItem(f"{float(cash):,.2f}")
                    cash_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    table.setItem(r, 8, cash_item)

                    total_asset = row.get('total_asset', 0.0) if pd.notna(row.get('total_asset')) else 0.0
                    total_asset_item = QTableWidgetItem(f"{float(total_asset):,.2f}")
                    total_asset_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    table.setItem(r, 9, total_asset_item)

                    # 每 1024 行让出一次事件循环，保持界面响应（封顶后通常 < 10 次）
                    if (r & 0x3FF) == 0x3FF:
                        QApplication.processEvents()
            else:
                # 清空交易表格并显示提示信息
                table.clearContents()
                table.setRowCount(1)
                table.setColumnCount(1)
                table.setHorizontalHeaderLabels(["提示"])
                table.setItem(0, 0, QTableWidgetItem("回测期间没有产生交易"))
                table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
            table.setUpdatesEnabled(True)

        except Exception as e:
            try:
                self.trades_table.setUpdatesEnabled(True)
            except Exception:
                pass
            print(f"更新交易记录表格时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())

    def update_daily_stats_table(self, daily_stats_df):
        """更新每日统计表格"""
        try:
            table = self.daily_stats_table
            table.setUpdatesEnabled(False)
            # 如果有每日统计数据，更新每日统计表格和图表
            if len(daily_stats_df) > 0:
                headers = ["日期", "总资产", "持仓市值", "可用资金", "日收益率"]
                table.clearContents()
                table.setColumnCount(len(headers))
                table.setHorizontalHeaderLabels(headers)
                table.setRowCount(len(daily_stats_df))
                
                # 设置列宽度比例
                header = table.horizontalHeader()
                header.setSectionResizeMode(0, QHeaderView.ResizeToContents)  # 日期
                header.setSectionResizeMode(1, QHeaderView.Stretch)          # 总资产
                header.setSectionResizeMode(2, QHeaderView.Stretch)          # 持仓市值
                header.setSectionResizeMode(3, QHeaderView.Stretch)          # 可用资金
                header.setSectionResizeMode(4, QHeaderView.ResizeToContents)  # 日收益率
                
                for i, row in daily_stats_df.iterrows():
                    # 日期
                    date_item = QTableWidgetItem(str(row['date']))
                    date_item.setTextAlignment(Qt.AlignCenter)
                    table.setItem(i, 0, date_item)
                    
                    # 总资产
                    total_asset_item = QTableWidgetItem(f"{row['total_asset']:,.2f}")
                    total_asset_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    table.setItem(i, 1, total_asset_item)
                    
                    # 持仓市值
                    market_value_item = QTableWidgetItem(f"{row['market_value']:,.2f}")
                    market_value_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    table.setItem(i, 2, market_value_item)
                    
                    # 可用资金
                    cash_item = QTableWidgetItem(f"{row['cash']:,.2f}")
                    cash_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    table.setItem(i, 3, cash_item)
                    
                    # 日收益率
                    if 'daily_return' in row:
                        daily_return = row['daily_return']
                        daily_return_item = QTableWidgetItem(f"{daily_return:.2%}")
                        daily_return_item.setTextAlignment(Qt.AlignCenter)
                        
                        # 设置收益率颜色
                        if daily_return > 0:
                            daily_return_item.setForeground(QColor('#ff4444'))  # 盈利红色
                        elif daily_return < 0:
                            daily_return_item.setForeground(QColor('#00cc00'))  # 亏损绿色
                        else:
                            daily_return_item.setForeground(QColor('#e8e8e8'))  # 白色
                            
                        table.setItem(i, 4, daily_return_item)
                    else:
                        table.setItem(i, 4, QTableWidgetItem("--"))
            else:
                # 清空每日统计表格并显示提示信息
                table.clearContents()
                table.setRowCount(1)
                table.setColumnCount(1)
                table.setHorizontalHeaderLabels(["提示"])
                table.setItem(0, 0, QTableWidgetItem("回测期间没有产生每日统计数据"))
                table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
                # 清空图表
                self.ax.clear()
                self.ax.set_title("没有交易数据", color='#e8e8e8')
                self.chart_view.draw()
            table.setUpdatesEnabled(True)
            
        except Exception as e:
            try:
                self.daily_stats_table.setUpdatesEnabled(True)
            except Exception:
                pass
            print(f"更新每日统计表格时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())

    def record_results(self, timestamp, data, signals):
        """记录回测结果"""
        try:
            # 初始化回测记录字典（如果尚未初始化）
            if not hasattr(self, 'backtest_records'):
                self.backtest_records = {
                    'trades': [],  # 交易记录
                    'daily_stats': [],  # 每日统计数据
                    'benchmark_data': [],  # 基准指数数据
                    'start_time': self.config.backtest_start,
                    'end_time': self.config.backtest_end,
                    'init_capital': self.config.config_dict["backtest"]["init_capital"]  # 修改键名为init_capital
                }
            
            # 将回测结果添加到记录中
            self.backtest_records['trades'].append(data['trades'])
            self.backtest_records['daily_stats'].append(data['daily_stats'])
            self.backtest_records['benchmark_data'].append(data['benchmark'])
            
            # 更新回测记录
            self.update_basic_info(self.config.config_dict["backtest"], data['daily_stats'])
            self.update_chart(data['daily_stats'], data['benchmark'])
            self.update_trades_table(data['trades'])
            self.update_daily_stats_table(data['daily_stats'])
            
        except Exception as e:
            print(f"记录回测结果时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())

    def calculate_sortino_ratio(self, returns, risk_free_rate=None, target_return=0.0):
        """计算索提诺比率
        
        Sortino Ratio = (Ra - Rf) / δpd
        
        其中：
        - Ra 为策略年化收益率
        - Rf 无风险利率（当前十年期国债利率平均值）
        - δpd 策略下行波动率，只考虑收益率低于沪深300指数收益率的部分
        
        Args:
            returns: 收益率序列
            risk_free_rate: 无风险利率，默认为None（使用内部设置的值）
            target_return: 不使用此参数，保留是为了兼容性
            
        Returns:
            float: 索提诺比率
        """
        try:
            # 如果未提供风险收益率，使用实例变量
            if risk_free_rate is None:
                risk_free_rate = self.risk_free_rate
                
            # 检查是否有足够的数据
            if returns is None or len(returns) < 2:
                return 0.0
            
            # 确保数据是数值类型
            returns = pd.to_numeric(returns, errors='coerce')
            
            # 移除空值
            returns = returns.dropna()
            
            if len(returns) < 2:
                return 0.0
                
            # 获取交易日数量
            n = len(returns)
            
            # 计算总收益率
            total_return = (1 + returns).prod() - 1
            
            # 计算年化收益率 Ra
            annual_return = (pow(1 + total_return, 250/n) - 1)
            
            # 获取基准日收益率（用于下行风险比较）
            benchmark_returns = None
            try:
                benchmark_file = os.path.join(self.backtest_dir, "benchmark.csv")
                if os.path.exists(benchmark_file):
                    benchmark_df = pd.read_csv(benchmark_file)
                    if len(benchmark_df) > 0 and 'date' in benchmark_df.columns and 'close' in benchmark_df.columns:
                        benchmark_df['date'] = pd.to_datetime(benchmark_df['date'])
                        benchmark_df = benchmark_df.sort_values('date')
                        # benchmark.csv 可能包含回测前的数据，直接用全部 close 做 pct_change
                        # 取最后 n 条作为回测期间的日收益率（含第一天相对前一日的收益）
                        all_returns = benchmark_df['close'].pct_change().dropna()
                        benchmark_returns = all_returns.iloc[-n:].reset_index(drop=True) if len(all_returns) >= n else all_returns.reset_index(drop=True)
            except Exception as e:
                print(f"获取基准收益率时出错: {str(e)}")

            if benchmark_returns is None or len(benchmark_returns) != len(returns):
                benchmark_returns = pd.Series([0.0] * len(returns))
            
            # 计算下行波动率 (Downside Risk)
            # 计算下行风险: √[(250/n) * Σ[(Rp(i) - Rm(i))² * f(i)]]
            # 其中 f(i)=1 如果 Rp(i)<Rm(i)，否则 f(i)=0
            downside_diff_squared = []
            for i in range(len(returns)):
                # 如果策略收益率低于沪深300指数收益率，则计入下行风险
                if returns.iloc[i] < benchmark_returns.iloc[i]:
                    # 计算差值的平方
                    diff_squared = (returns.iloc[i] - benchmark_returns.iloc[i]) ** 2
                    downside_diff_squared.append(diff_squared)
                else:
                    # 如果策略收益率高于沪深300指数收益率，不计入下行风险
                    downside_diff_squared.append(0)
                    
            # 计算下行波动率
            downside_risk = np.sqrt((250/n) * np.sum(downside_diff_squared))
            
            # 检查下行风险是否为0
            if downside_risk == 0 or pd.isna(downside_risk):
                return np.inf if annual_return > risk_free_rate else 0.0
            
            # 计算索提诺比率: (Ra - Rf) / δpd
            sortino = (annual_return - risk_free_rate) / downside_risk
            
            # 检查结果是否为有效数值
            if np.isnan(sortino):
                return 0.0
            
            return sortino
        
        except Exception as e:
            print(f"计算索提诺比率时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return 0.0
    
    def calculate_calmar_ratio(self, returns, max_drawdown):
        """计算卡玛比率"""
        # 不再计算卡玛比率，直接返回0
        return 0.0

    def calculate_alpha_beta(self, strategy_returns, benchmark_returns, risk_free_rate=None):
        """计算阿尔法和贝塔"""
        try:
            # 如果未提供风险收益率，使用实例变量
            if risk_free_rate is None:
                risk_free_rate = self.risk_free_rate
                
            # 检查是否有足够的数据
            if (strategy_returns is None or benchmark_returns is None or 
                len(strategy_returns) < 10 or len(benchmark_returns) < 10):
                return 0.0, 0.0
            
            # 确保数据是数值类型
            strategy_returns = pd.to_numeric(strategy_returns, errors='coerce')
            benchmark_returns = pd.to_numeric(benchmark_returns, errors='coerce')
            
            # 移除空值
            valid_indices = ~(strategy_returns.isna() | benchmark_returns.isna())
            strategy_returns = strategy_returns[valid_indices]
            benchmark_returns = benchmark_returns[valid_indices]
            
            if len(strategy_returns) < 10:
                return 0.0, 0.0
            
            # 计算协方差矩阵和贝塔
            cov_matrix = np.cov(strategy_returns, benchmark_returns)
            beta = cov_matrix[0, 1] / cov_matrix[1, 1] if cov_matrix[1, 1] > 0 else 0.0
            

            alpha = 0
            # 检查结果是否为有效数值
            if np.isnan(alpha) or np.isinf(alpha):
                alpha = 0.0
            if np.isnan(beta) or np.isinf(beta):
                beta = 0.0
            
            return alpha, beta
        
        except Exception as e:
            print(f"计算阿尔法和贝塔时出错: {str(e)}")
            return 0.0, 0.0

    def calculate_win_rate_and_profit_ratio(self, trades_df):
        """计算胜率和盈亏比"""
        try:
            if trades_df is None or len(trades_df) == 0:
                return 0.0, 0.0
            
            # 初始化变量
            total_trades = 0
            winning_trades = 0
            total_profit = 0.0
            total_loss = 0.0
            
            # 添加方向判断辅助函数
            def is_buy(direction):
                if isinstance(direction, str):
                    return direction == '买入' or direction.lower() == 'buy'
                elif isinstance(direction, (int, float)):
                    return direction > 0  # 假设正数表示买入
                return False
                
            def is_sell(direction):
                if isinstance(direction, str):
                    return direction == '卖出' or direction.lower() == 'sell'
                elif isinstance(direction, (int, float)):
                    return direction < 0  # 假设负数表示卖出
                return False
            
            # 按股票代码分组处理交易
            # 用一次 groupby 取代“对每个代码做一次全表布尔筛选”，复杂度从 O(代码数×N) 降到 O(N)；
            # 再用列数组 zip 迭代替代 iterrows，去掉逐行 Series 构造开销。
            for code, code_trades in trades_df.groupby('code', sort=False):
                code_trades = code_trades.sort_values('time')  # 按时间排序

                # 初始化股票持仓
                position = 0
                cost_basis = 0

                # 遍历该股票的所有交易
                for direction, volume, price in zip(
                        code_trades['direction'].values,
                        code_trades['volume'].values,
                        code_trades['price'].values):
                    try:
                        if is_buy(direction):
                            # 更新持仓成本
                            new_position = position + volume
                            new_cost = cost_basis * position + price * volume
                            if new_position > 0:
                                cost_basis = new_cost / new_position
                            position = new_position
                        elif is_sell(direction) and position > 0:
                            # 计算本次卖出的盈亏
                            sell_volume = min(position, volume)
                            profit_loss = (price - cost_basis) * sell_volume

                            # 更新统计数据
                            total_trades += 1
                            if profit_loss > 0:
                                winning_trades += 1
                                total_profit += profit_loss
                            else:
                                total_loss -= profit_loss  # 转换为正数

                            # 更新持仓
                            position -= sell_volume
                    except Exception:
                        continue
            
            # 计算胜率
            win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
            
            # 计算盈亏比 - 使用总盈利金额除以总亏损金额; 全盈利(0亏损)时为∞ (修缺陷#1)
            profit_ratio = total_profit / total_loss if total_loss > 0 else (float('inf') if total_profit > 0 else 0.0)

            return win_rate, profit_ratio
            
        except Exception as e:
            print(f"计算胜率和盈亏比时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return 0.0, 0.0

    def calculate_trading_metrics(self, trades_df, daily_stats_df):
        """计算交易相关指标"""
        try:
            if trades_df is None or len(trades_df) == 0 or daily_stats_df is None or len(daily_stats_df) == 0:
                return 0.0, 0, 0, 0.0, 0.0
            
            # 计算交易天数
            trading_days = len(daily_stats_df)
            
            # 计算日均交易次数
            daily_trades = len(trades_df) / trading_days if trading_days > 0 else 0
            
            # 计算连续盈利和亏损
            daily_returns = daily_stats_df['daily_return'] if 'daily_return' in daily_stats_df.columns else pd.Series([0])
            
            max_win_streak = 0
            max_loss_streak = 0
            current_streak = 0
            current_sign = None
            
            for ret in daily_returns:
                if pd.isna(ret):
                    continue

                # 与 cli/report.py:307-324 同一口径: 翻转从1重计并即时更新max; 平(ret==0)中断
                if ret > 0:
                    current_streak = current_streak + 1 if current_sign == 1 else 1
                    current_sign = 1
                    max_win_streak = max(max_win_streak, current_streak)
                elif ret < 0:
                    current_streak = current_streak + 1 if current_sign == -1 else 1
                    current_sign = -1
                    max_loss_streak = max(max_loss_streak, current_streak)
                else:  # 平: 既非盈也非亏, 中断streak
                    current_streak = 0
                    current_sign = None
            
            # 初始化最大盈亏变量
            max_profit = 0.0
            max_loss = 0.0
            
            # 添加方向判断辅助函数
            def is_buy(direction):
                if isinstance(direction, str):
                    return direction == '买入' or direction.lower() == 'buy'
                elif isinstance(direction, (int, float)):
                    return direction > 0  # 假设正数表示买入
                return False
                
            def is_sell(direction):
                if isinstance(direction, str):
                    return direction == '卖出' or direction.lower() == 'sell'
                elif isinstance(direction, (int, float)):
                    return direction < 0  # 假设负数表示卖出
                return False
            
            # 按股票代码分组计算最大单笔盈亏
            # 同样用 groupby + zip 取代 per-code 全表筛选 + iterrows，O(代码数×N) → O(N)。
            for code, code_trades in trades_df.groupby('code', sort=False):
                code_trades = code_trades.sort_values('time')  # 按时间排序

                # 初始化股票持仓
                position = 0
                cost_basis = 0

                # 遍历该股票的所有交易
                for direction, volume, price in zip(
                        code_trades['direction'].values,
                        code_trades['volume'].values,
                        code_trades['price'].values):
                    try:
                        if is_buy(direction):
                            # 更新持仓成本
                            new_position = position + volume
                            new_cost = cost_basis * position + price * volume
                            if new_position > 0:
                                cost_basis = new_cost / new_position
                            position = new_position
                        elif is_sell(direction) and position > 0:
                            # 计算本次卖出的盈亏
                            sell_volume = min(position, volume)
                            profit_loss = (price - cost_basis) * sell_volume

                            # 更新最大盈亏
                            if profit_loss > 0:
                                max_profit = max(max_profit, profit_loss)
                            else:
                                max_loss = min(max_loss, profit_loss)

                            # 更新持仓
                            position -= sell_volume
                    except Exception:
                        continue
            
            # 返回绝对值的最大亏损（为正数）
            max_loss_abs = abs(max_loss)
            
            return daily_trades, max_win_streak, max_loss_streak, max_profit, max_loss_abs
        
        except Exception as e:
            print(f"计算交易指标时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return 0.0, 0, 0, 0.0, 0.0

    def calculate_volatility(self, returns):
        """计算年化波动率，使用公式 σp = √(250/n·∑(rp - r̄p)²)
        
        参数:
            returns: 策略每日收益率序列
            
        返回:
            float: 年化波动率
        """
        try:
            if returns is None or len(returns) < 2:
                return 0.0
            
            # 确保数据是数值类型
            returns = pd.to_numeric(returns, errors='coerce')
            
            # 移除空值
            returns = returns.dropna()
            
            if len(returns) < 2:
                return 0.0
            
            # 从daily_stats获取起止日期
            daily_stats_path = os.path.join(self.backtest_dir, "daily_stats.csv")
            if os.path.exists(daily_stats_path):
                daily_stats_df = pd.read_csv(daily_stats_path, encoding='utf-8-sig')
                if len(daily_stats_df) > 0 and 'date' in daily_stats_df.columns:
                    # 获取起止日期
                    first_date = pd.to_datetime(daily_stats_df['date'].iloc[0]).strftime('%Y-%m-%d')
                    last_date = pd.to_datetime(daily_stats_df['date'].iloc[-1]).strftime('%Y-%m-%d')
                    
                    # 获取交易日天数
                    tools = KhQuTools()
                    n = tools.get_trade_days_count(first_date, last_date)
                    
                    # 如果获取交易日天数失败，则使用实际数据点数量
                    if n <= 0:
                        n = len(returns)
                        print(f"警告：无法获取交易日天数，使用收益率数据点数量 {n} 作为替代")
                else:
                    # 如果没有日期数据，使用数据点数量
                    n = len(returns)
            else:
                # 如果没有daily_stats文件，使用数据点数量
                n = len(returns)
            
            # 计算平均收益率
            r_p_mean = returns.mean()
            
            # 计算年化波动率 σp = √(250/n·∑(rp - r̄p)²)
            volatility = np.sqrt((250/n) * np.sum((returns - r_p_mean)**2))
            
            # 检查结果是否为有效数值
            if np.isnan(volatility) or np.isinf(volatility):
                return 0.0
            
            return volatility
        
        except Exception as e:
            print(f"计算年化波动率时出错: {str(e)}")
            return 0.0

    def update_performance_charts(self, daily_stats_df, benchmark_df=None):
        """更新绩效评估图表"""
        try:
            # 更新收益分布图
            self.update_returns_distribution_chart(daily_stats_df['daily_return'])
            
            # 更新月度收益热力图
            self.update_monthly_returns_heatmap(daily_stats_df)
            
        except Exception as e:
            print(f"更新绩效评估图表时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())
    
    def update_returns_distribution_chart(self, returns):
        """更新收益分布图"""
        try:
            # 清空图形
            self.returns_dist_figure.clear()
            
            # 创建子图
            ax = self.returns_dist_figure.add_subplot(111)
            
            # 设置样式 - 使用透明背景以匹配深色主题
            ax.set_facecolor('none')
            self.returns_dist_figure.patch.set_facecolor('none')
            
            # 设置标题和标签
            # ax.set_title("收益率分布", fontsize=12, fontweight='bold', color='#e8e8e8', pad=10)
            ax.set_xlabel("日收益率", fontsize=10, color='#a0a0a0')
            ax.set_ylabel("频次", fontsize=10, color='#a0a0a0')
            
            # 检查数据是否足够
            if len(returns) <= 1:
                ax.text(0.5, 0.5, "数据点不足，无法绘制分布图", 
                       transform=ax.transAxes, fontsize=10, color='#e8e8e8',
                       verticalalignment='center', horizontalalignment='center',
                       bbox=dict(boxstyle='round', facecolor='#333333', alpha=0.5))
            else:
                # 绘制直方图
                n, bins, patches = ax.hist(returns, bins=min(50, len(returns) // 2 + 1), alpha=0.75, color='#007acc')
                
                # 计算均值和标准差
                mean = returns.mean()
                std = returns.std()
                
                # 绘制正态分布曲线，不使用冗余的格式字符串
                if max(returns) > min(returns):  # 确保数据有范围
                    x = np.linspace(min(returns), max(returns), 100)
                    # 增加对标准差为0或接近0的保护
                    if std <= 1e-8:  # 使用一个很小的阈值判断是否接近0
                        # 标准差接近0，不绘制正态分布曲线
                        ax.text(0.5, 0.5, "标准差接近0，无法绘制正态分布曲线", 
                               transform=ax.transAxes, fontsize=10, color='#e8e8e8',
                               verticalalignment='center', horizontalalignment='center',
                               bbox=dict(boxstyle='round', facecolor='#333333', alpha=0.5))
                    else:
                        y = ((1 / (np.sqrt(2 * np.pi) * std)) * np.exp(-0.5 * ((x - mean)/std)**2)) * len(returns) * (bins[1] - bins[0])
                        ax.plot(x, y, color='#ff9900', linewidth=2)
                
                # 添加均值和标准差标注
                ax.axvline(mean, color='#ff9900', linestyle='dashed', linewidth=1)
                ax.axvline(0, color='#ffffff', linestyle='dashed', linewidth=1)
                
                # 添加图例
                ax.text(0.95, 0.95, f"均值: {mean:.4f}\n标准差: {std:.4f}", 
                       transform=ax.transAxes, fontsize=10, color='#e8e8e8',
                       verticalalignment='top', horizontalalignment='right',
                       bbox=dict(boxstyle='round', facecolor='#333333', alpha=0.5))
            
            # 设置网格
            ax.grid(True, linestyle='--', alpha=0.1, color='#808080')
            
            # 设置刻度颜色
            ax.tick_params(axis='both', colors='#a0a0a0')
            
            # 设置边框颜色
            for spine in ax.spines.values():
                spine.set_color('#404040')
            
            # 调整布局
            self.returns_dist_figure.tight_layout()
            
            # 重绘图表
            self.returns_dist_canvas.draw()
            
        except Exception as e:
            print(f"更新收益分布图时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())
    
    def update_monthly_returns_heatmap(self, daily_stats_df):
        """更新月度收益热力图"""
        try:
            # 清空图形
            self.monthly_returns_figure.clear()
            
            # 创建子图
            ax = self.monthly_returns_figure.add_subplot(111)
            
            # 设置样式 - 使用透明背景以匹配深色主题
            ax.set_facecolor('none')
            self.monthly_returns_figure.patch.set_facecolor('none')
            
            # 确保日期格式正确
            daily_stats_df['date'] = pd.to_datetime(daily_stats_df['date'])
            
            # 提取收益率数据
            daily_stats_df['year'] = daily_stats_df['date'].dt.year
            daily_stats_df['month'] = daily_stats_df['date'].dt.month
            
            # 计算月度收益率
            monthly_returns = daily_stats_df.groupby(['year', 'month'])['daily_return'].apply(
                lambda x: (1 + x).prod() - 1
            ).reset_index()
            
            # 创建数据透视表形式的月度收益率
            pivot_table = monthly_returns.pivot_table(
                index='year', columns='month', values='daily_return'
            )
            
            # 确保月份列按1-12顺序排列
            all_months = list(range(1, 13))  # 1到12的月份
            for month in all_months:
                if month not in pivot_table.columns:
                    pivot_table[month] = np.nan  # 添加缺失的月份列
            pivot_table = pivot_table.reindex(columns=all_months)  # 按月份顺序重排列
            
            # 绘制热力图
            im = ax.imshow(pivot_table, cmap='RdYlGn_r', aspect='auto')
            
            # 设置标题
            # ax.set_title("月度收益率热力图", fontsize=12, fontweight='bold', color='#e8e8e8', pad=10)
            
            # 设置坐标轴标签
            month_labels = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月']
            
            # 确保刻度与数据对齐
            ax.set_xticks(np.arange(len(month_labels)))
            ax.set_xticklabels(month_labels, color='#a0a0a0')
            
            # 设置年份标签
            years = sorted(pivot_table.index.tolist())
            ax.set_yticks(np.arange(len(years)))
            ax.set_yticklabels(years, color='#a0a0a0')
            
            # 添加网格线
            ax.set_xticks(np.arange(-0.5, len(month_labels), 1), minor=True)
            ax.set_yticks(np.arange(-0.5, len(years), 1), minor=True)
            ax.grid(which="minor", color="#404040", linestyle='-', linewidth=1)
            ax.tick_params(which="minor", bottom=False, left=False)
            
            # 在每个单元格添加文本，确保索引对应正确
            for i, year in enumerate(years):
                for j, month in enumerate(all_months):
                    if month in pivot_table.columns and year in pivot_table.index:
                        value = pivot_table.loc[year, month]
                        if not pd.isna(value):
                            text_color = 'black' if abs(value) > 0.05 else 'white'
                            ax.text(j, i, f"{value:.2%}", ha="center", va="center", 
                                  color=text_color, fontsize=9)
            
            # 设置x轴和y轴标签
            ax.set_xlabel("月份", fontsize=10, color='#a0a0a0')
            ax.set_ylabel("年份", fontsize=10, color='#a0a0a0')
            
            # 添加颜色条
            cbar = self.monthly_returns_figure.colorbar(im, ax=ax)
            cbar.ax.tick_params(colors='#a0a0a0')
            cbar.set_label('月度收益率', color='#a0a0a0')
            
            # 调整布局
            self.monthly_returns_figure.tight_layout()
            
            # 重绘图表
            self.monthly_returns_canvas.draw()
            
        except Exception as e:
            print(f"更新月度收益热力图时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())
    
    def update_rolling_metrics_chart(self, daily_stats_df, benchmark_df=None):
        """更新滚动指标图"""
        try:
            # 清空图形
            self.rolling_metrics_figure.clear()
            
            # 创建两个子图（夏普比率和beta）
            ax1 = self.rolling_metrics_figure.add_subplot(211)
            ax2 = self.rolling_metrics_figure.add_subplot(212, sharex=ax1)
            
            # 设置样式
            ax1.set_facecolor('#2d2d2d')
            ax2.set_facecolor('#2d2d2d')
            self.rolling_metrics_figure.patch.set_facecolor('#2d2d2d')
            
            # 确保日期格式正确
            daily_stats_df['date'] = pd.to_datetime(daily_stats_df['date'])
            
            # 提取收益率数据
            returns = daily_stats_df['daily_return']
            dates = daily_stats_df['date']
            
            # 计算30日和60日滚动夏普比率
            window_size_30 = min(30, len(returns))
            window_size_60 = min(60, len(returns))
            
            if len(returns) >= window_size_30:
                # 计算超额收益的年化值
                rolling_excess_returns_30 = returns - self.risk_free_rate/252  # 使用从设置加载的无风险利率
                rolling_return_30 = rolling_excess_returns_30.rolling(window=window_size_30).mean() * 252
                rolling_std_30 = returns.rolling(window=window_size_30).std() * np.sqrt(252)
                
                # 添加除零保护
                with np.errstate(divide='ignore', invalid='ignore'):
                    rolling_sharpe_30 = rolling_return_30 / rolling_std_30
                
                # 替换无效值
                rolling_sharpe_30 = rolling_sharpe_30.replace([np.inf, -np.inf], np.nan)
                rolling_sharpe_30 = rolling_sharpe_30.fillna(0)
                
                # 绘制30日滚动夏普比率
                valid_start_idx = window_size_30-1
                # 确保索引不越界
                valid_start_idx = min(valid_start_idx, len(dates)-1)
                if valid_start_idx < len(dates):
                    ax1.plot(dates[valid_start_idx:], rolling_sharpe_30[valid_start_idx:], 
                           label='30日滚动夏普比率', color='#007acc', linewidth=1.5)
            
            if len(returns) >= window_size_60:
                # 计算超额收益的年化值
                rolling_excess_returns_60 = returns - self.risk_free_rate/252  # 使用从设置加载的无风险利率
                rolling_return_60 = rolling_excess_returns_60.rolling(window=window_size_60).mean() * 252
                rolling_std_60 = returns.rolling(window=window_size_60).std() * np.sqrt(252)
                
                # 添加除零保护
                with np.errstate(divide='ignore', invalid='ignore'):
                    rolling_sharpe_60 = rolling_return_60 / rolling_std_60
                
                # 替换无效值
                rolling_sharpe_60 = rolling_sharpe_60.replace([np.inf, -np.inf], np.nan)
                rolling_sharpe_60 = rolling_sharpe_60.fillna(0)
                
                # 绘制60日滚动夏普比率
                valid_start_idx = window_size_60-1
                # 确保索引不越界
                valid_start_idx = min(valid_start_idx, len(dates)-1)
                if valid_start_idx < len(dates):
                    ax1.plot(dates[valid_start_idx:], rolling_sharpe_60[valid_start_idx:], 
                           label='60日滚动夏普比率', color='#ff9900', linewidth=1.5)
            
            # 设置标题和标签
            ax1.set_title("滚动夏普比率", fontsize=12, fontweight='bold', color='#e8e8e8', pad=10)
            ax1.set_ylabel("夏普比率", fontsize=10, color='#a0a0a0')
            
            # 添加图例
            ax1.legend(loc='upper left', fancybox=True, framealpha=0.7, fontsize=9)
            
            # 计算30日滚动波动率和最大回撤
            if len(returns) >= window_size_30:
                rolling_vol_30 = returns.rolling(window=window_size_30).std() * np.sqrt(252) * 100  # 转为百分比
                
                # 计算滚动最大回撤
                rolling_max_dd = pd.Series(index=dates)
                for i in range(window_size_30, len(daily_stats_df)+1):
                    window = daily_stats_df['total_asset'].iloc[i-window_size_30:i]
                    cummax = window.cummax()
                    
                    # 添加除零保护
                    with np.errstate(divide='ignore', invalid='ignore'):
                        drawdown = (cummax - window) / cummax * 100
                    
                    # 替换无效值
                    drawdown = drawdown.replace([np.inf, -np.inf], np.nan)
                    drawdown = drawdown.fillna(0)
                    
                    if i-1 < len(rolling_max_dd):
                        rolling_max_dd.iloc[i-1] = drawdown.max()
                
                # 绘制30日滚动波动率
                valid_start_idx = window_size_30-1
                # 确保索引不越界
                valid_start_idx = min(valid_start_idx, len(dates)-1)
                if valid_start_idx < len(dates) and valid_start_idx < len(rolling_vol_30):
                    # 确保数据长度一致
                    valid_length = min(len(dates[valid_start_idx:]), len(rolling_vol_30[valid_start_idx:]))
                    plot_dates = dates[valid_start_idx:valid_start_idx+valid_length]
                    plot_values = rolling_vol_30[valid_start_idx:valid_start_idx+valid_length]
                    ax2.plot(plot_dates, plot_values, 
                           label='30日滚动波动率(%)', color='#007acc', linewidth=1.5)
                
                # 绘制30日滚动最大回撤
                # 确保不是空序列并且索引有效
                if not rolling_max_dd.empty and valid_start_idx < len(rolling_max_dd):
                    # 移除NaN值
                    valid_dd = rolling_max_dd.dropna()
                    if len(valid_dd) > 0:
                        # 获取对应的日期
                        valid_dates = dates[valid_dd.index.intersection(dates.index)]
                        ax2.plot(valid_dates, valid_dd[valid_dates.index], 
                               label='30日滚动最大回撤(%)', color='#ff4444', linewidth=1.5)
            
            # 设置标题和标签
            ax2.set_title("滚动风险指标", fontsize=12, fontweight='bold', color='#e8e8e8', pad=10)
            ax2.set_xlabel("日期", fontsize=10, color='#a0a0a0')
            ax2.set_ylabel("百分比(%)", fontsize=10, color='#a0a0a0')
            
            # 添加图例
            ax2.legend(loc='upper left', fancybox=True, framealpha=0.7, fontsize=9)
            
            # 设置网格
            ax1.grid(True, linestyle='--', alpha=0.1, color='#808080')
            ax2.grid(True, linestyle='--', alpha=0.1, color='#808080')
            
            # 设置刻度颜色
            ax1.tick_params(axis='both', colors='#a0a0a0')
            ax2.tick_params(axis='both', colors='#a0a0a0')
            
            # 设置边框颜色
            for spine in ax1.spines.values():
                spine.set_color('#404040')
            for spine in ax2.spines.values():
                spine.set_color('#404040')
            
            # 设置x轴日期格式
            ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
            ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
            
            # 设置适当的日期定位器
            # 计算合适的刻度数量
            if len(dates) > 1:
                days = (dates.max() - dates.min()).days
                if days <= 7:  # 7天或更少，显示每天
                    locator = mdates.DayLocator()
                elif days <= 60:  # 60天或更少，每周显示一次
                    locator = mdates.WeekdayLocator(byweekday=mdates.MO)
                elif days <= 365:  # 一年或更少，每月显示一次
                    locator = mdates.MonthLocator()
                else:  # 超过一年，每季度显示一次
                    locator = mdates.MonthLocator(bymonth=[1, 4, 7, 10])
                
                ax1.xaxis.set_major_locator(locator)
                ax2.xaxis.set_major_locator(locator)
            else:
                # 如果只有一个数据点，使用自动定位器
                ax1.xaxis.set_major_locator(ticker.AutoLocator())
                ax2.xaxis.set_major_locator(ticker.AutoLocator())
            
            # 旋转日期标签以避免重叠
            plt.setp(ax1.get_xticklabels(), rotation=45, ha='right')
            plt.setp(ax2.get_xticklabels(), rotation=45, ha='right')
            
            # 自动调整x轴刻度
            self.rolling_metrics_figure.autofmt_xdate()
            
            # 调整布局
            self.rolling_metrics_figure.tight_layout()
            
            # 重绘图表
            self.rolling_metrics_canvas.draw()
            
        except Exception as e:
            print(f"更新滚动指标图时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())

    def calculate_benchmark_return(self, benchmark_df):
        """计算基准收益率（兼容旧调用，只使用已加载的本地基准数据）。"""
        try:
            if benchmark_df is None or len(benchmark_df) < 2 or 'close' not in benchmark_df.columns:
                return 0.0
            benchmark_df = benchmark_df.sort_values('date') if 'date' in benchmark_df.columns else benchmark_df
            end_price = float(benchmark_df['close'].iloc[-1])
            daily_stats_df = getattr(self, 'daily_stats_df', None)
            start_price = resolve_benchmark_base_price(benchmark_df, daily_stats_df)
            if start_price is None:
                start_price = float(benchmark_df['close'].iloc[0])
            return ((end_price - start_price) / start_price) * 100 if start_price > 0 else 0.0
        except Exception as e:
            print(f"计算基准收益率时出错: {str(e)}")
            return 0.0

    def calculate_annualized_benchmark_return(self, benchmark_return, days_count):
        """计算基准年化收益率
        
        基准年化收益率的计算公式：
        Annualized Benchmark Returns = [(1 + Rb)^(250/n) - 1] * 100%
        
        其中：
        Rb为基准收益率（小数形式）
        n为回测区间交易日数量
        250为一年的交易日数量
        """
        try:
            # 检查参数有效性
            if days_count <= 0:
                print("警告：交易日数量无效，无法计算基准年化收益率")
                return 0.0
                
            # 将百分比形式的基准收益率转换为小数形式
            benchmark_return_decimal = benchmark_return / 100
            
            # 计算基准年化收益率
            annualized_benchmark_return = (pow(1 + benchmark_return_decimal, 250/days_count) - 1) * 100
            
            print(f"基准收益率：{benchmark_return:.2f}%，交易日数量：{days_count}，基准年化收益率：{annualized_benchmark_return:.2f}%")
            
            return annualized_benchmark_return
            
        except Exception as e:
            print(f"计算基准年化收益率时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return 0.0

    def calculate_alpha(self, strategy_annual_return, benchmark_annual_return, beta, risk_free_rate=None):
        """使用年化收益率直接计算阿尔法
        
        Alpha计算公式：
        Alpha = Ra - [Rf + β * (Rba - Rf)]
        
        其中：
        Ra是策略年化收益率
        Rf是无风险收益率
        β是贝塔系数
        Rba是基准年化收益率
        """
        try:
            # 如果未提供风险收益率，使用实例变量
            if risk_free_rate is None:
                risk_free_rate = self.risk_free_rate
                
            # 确保所有输入都是数值类型
            strategy_annual_return = float(strategy_annual_return)
            benchmark_annual_return = float(benchmark_annual_return)
            beta = float(beta)
            risk_free_rate = float(risk_free_rate) * 100  # 转换为百分比形式，与其他收益率保持一致
            
            # 计算Alpha
            alpha = strategy_annual_return - (risk_free_rate + beta * (benchmark_annual_return - risk_free_rate))
            alpha = alpha/100
            # 输出调试信息
            print(f"Alpha计算: {strategy_annual_return} - ({risk_free_rate} + {beta} * ({benchmark_annual_return} - {risk_free_rate})) = {alpha}")
            
            # 检查结果是否为有效数值
            if np.isnan(alpha) or np.isinf(alpha):
                alpha = 0.0
                
            return alpha
            
        except Exception as e:
            print(f"计算Alpha时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return 0.0

if __name__ == "__main__":
    import sys
    from PyQt5.QtWidgets import QApplication

    # 多显示器环境下，统一以主屏幕缩放比例渲染（必须在 QApplication 之前）
    force_primary_screen_dpi()
    app = QApplication(sys.argv)
    
    # 默认使用当前路径下的backtest_results/ths_20240403_20241101文件夹
    default_path = os.path.join(os.getcwd(), "backtest_results", "ths_20240403_20241101")
    
    # 检查路径是否存在，如果不存在则创建相应提示
    if not os.path.exists(default_path):
        print(f"警告: 默认路径不存在: {default_path}")
        print("将尝试继续使用该路径...")
    
    # 创建并显示回测结果窗口
    window = BacktestResultWindow(default_path)
    window.show()
    
    sys.exit(app.exec_())
