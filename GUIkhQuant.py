import sys
import os
import logging
import psutil
import time
import traceback
import json
import subprocess
import shutil
from datetime import datetime
from PyQt5.QtCore import (
    Qt,
    QSettings,
    QTimer,
    QThread,
    pyqtSignal,
    QMetaType,
    pyqtSlot,
    QDateTime,
    QDate,
    Q_ARG,
    QTime,
    QEvent,
    QUrl,
    QMetaObject,
)
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
                           QTableWidget, QTableWidgetItem, QMenu, QAction, QFileDialog, QMessageBox, QSplitter,
                           QTabWidget, QTextEdit, QComboBox, QGroupBox, QLineEdit, QDateEdit, QCheckBox, QProgressDialog,
                           QSizePolicy, QScrollArea, QTreeWidget, QTreeWidgetItem, QStackedWidget, QDialog, QListWidget,
                           QListWidgetItem, QSlider, QFrame, QToolBar, QButtonGroup, QRadioButton, QSpinBox, QDoubleSpinBox,
                           QCalendarWidget, QTimeEdit, QFormLayout, QSpacerItem, QGridLayout, QStatusBar, QInputDialog,
                           QHeaderView, QStyleFactory, QGraphicsDropShadowEffect, QProgressBar, QSplashScreen, QToolButton,
                           QDesktopWidget)
from PyQt5.QtGui import QIcon, QCursor, QFont, QColor, QPainter, QPen, QBrush, QPixmap, QTextCursor, QPalette, QDoubleValidator, QIntValidator, QDesktopServices

# 导入GUI模块中的StockDataProcessorGUI类
try:
    from GUI import StockDataProcessorGUI, setup_logging
except ImportError:
    logging.error("无法导入GUI模块中的StockDataProcessorGUI类")
    StockDataProcessorGUI = None
    setup_logging = None

# 导入数据管理模块
try:
    from GUIDataViewer import GUIDataViewer  # 数据本地数据管理模块
except ImportError:
    logging.error("无法导入数据查看器模块")
    GUIDataViewer = None

try:
    from GUIScheduler import GUIScheduler  # 数据定时补充模块
except ImportError:
    logging.error("无法导入数据定时补充模块")
    GUIScheduler = None



# 导入其他必要的模块
try:
    from khFrame import KhQuantFramework, MyTraderCallback
    from khQTTools import get_stock_names
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
except ImportError as e:
    logging.error(f"导入必要模块失败: {str(e)}")

from SettingsDialog import SettingsDialog
from PyQt5.QtCore import QSettings
from update_manager import UpdateManager  # 导入UpdateManager类
from version import get_version_info  # 导入版本信息

# 配置日志系统
def get_logs_dir():
    """获取日志目录的正确路径"""
    base_dir = os.path.dirname(__file__)
    logs_dir = os.path.join(base_dir, 'logs')

    try:
        os.makedirs(logs_dir, exist_ok=True)
        print(f"使用日志目录: {logs_dir}")
        return logs_dir
    except (OSError, PermissionError) as e:
        print(f"无法使用日志目录 {logs_dir}: {e}")
        # 备用目录
        import tempfile
        logs_dir = os.path.join(tempfile.gettempdir(), 'KhQuant_logs')
        os.makedirs(logs_dir, exist_ok=True)
        print(f"使用临时日志目录: {logs_dir}")
        return logs_dir

LOGS_DIR = get_logs_dir()

# 配置日志，添加异常处理
try:
    logging.basicConfig(
        filename=os.path.join(LOGS_DIR, 'app.log'),
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filemode='w',
        encoding='utf-8'
    )
    print(f"日志文件配置成功: {os.path.join(LOGS_DIR, 'app.log')}")
except Exception as e:
    # 如果文件日志配置失败，只使用控制台日志
    print(f"配置文件日志失败: {e}")
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

# 添加控制台日志处理器（仅在标准错误可用时）
_stderr_stream = getattr(sys, 'stderr', None)
if _stderr_stream and hasattr(_stderr_stream, 'write'):
    console_handler = logging.StreamHandler(_stderr_stream)
    console_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(formatter)
    logging.getLogger('').addHandler(console_handler)

# 定义StockAccount类
class StockAccount:
    """账户类"""
    def __init__(self, account_id, account_type="STOCK"):
        self.account_id = account_id
        self.account_type = account_type
        self.total_asset = 0.0
        self.cash = 0.0
        self.market_value = 0.0
        self.positions = []

class StrategyThread(QThread):
    """策略运行线程"""
    # 定义信号
    error_signal = pyqtSignal(str, Exception)  # 错误信号
    status_signal = pyqtSignal(str)  # 状态信号
    finished_signal = pyqtSignal()  # 完成信号

    def __init__(self, config_path, strategy_file, trader_callback):
        super().__init__()
        self.config_path = config_path
        self.strategy_file = strategy_file
        self.trader_callback = trader_callback
        self.framework = None
        self._is_running = True

    def run(self):
        """线程运行函数"""
        try:
            # 创建框架实例
            self.framework = KhQuantFramework(
                self.config_path,
                self.strategy_file,
                trader_callback=self.trader_callback
            )

            # 发送状态信号
            self.status_signal.emit("框架实例创建成功")

            # 运行策略
            self.framework.run()

        except Exception as e:
            # 发送错误信号
            self.error_signal.emit("策略运行异常", e)
            import traceback
            self.trader_callback.gui.log_message(f"错误详情:\n{traceback.format_exc()}", "ERROR")
        finally:
            # 发送完成信号（在设置_is_running=False之前）
            self.finished_signal.emit()
            # 现在设置运行状态为False
            self._is_running = False

    def stop(self):
        """停止策略"""
        self._is_running = False
        if self.framework:
            self.framework.stop()

    @property
    def is_running(self):
        return self._is_running

class GUILogHandler(logging.Handler):
    """自定义日志处理器，将日志信息显示在GUI的运行日志表格中"""
    def __init__(self, gui):
        super().__init__()
        self.gui = gui

    def emit(self, record):
        try:
            msg = self.format(record)
            # 使用Qt的信号槽机制来更新GUI
            self.gui.log_signal.emit(msg, record.levelname)
        except Exception:
            self.handleError(record)

class KhQuantGUI(QMainWindow):
    # 添加Qt信号
    log_signal = pyqtSignal(str, str)
    update_status_signal = pyqtSignal(str, str)
    show_backtest_result_signal = pyqtSignal(str)  # 添加新信号
    progress_signal = pyqtSignal(int)  # 添加进度条信号
    
    def __init__(self):
        super().__init__()
        
        # 记录程序启动时间
        self.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 初始化设置
        self.settings = QSettings('KHQuant', 'StockAnalyzer')
        
        # 初始化延迟日志显示相关属性（需要在早期初始化，避免AttributeError）
        self.delay_log_display = self.settings.value('delay_log_display', True, type=bool)
        self.delayed_logs = []
        self.strategy_is_running = False
        self.max_log_lines = self.settings.value('max_log_lines', 1000, type=int)  # 最大日志显示行数

        # 检测屏幕分辨率并设置字体缩放
        self.font_scale = self.detect_screen_resolution()
        
        # 设置应用样式表
        self.setStyleSheet(self.get_scaled_stylesheet())
        
        # 初始化属性
        self.config = {}
        self.trader = None
        self.trader_callback = None
        self.stock_data_manager = None
        
        # 日志过滤器设置
        self.filter_log_levels = {"INFO": True, "DEBUG": True, "WARNING": True, "ERROR": True}
        self.current_config_file = None  # 当前加载的配置文件路径
        
        # 设置窗口属性
        self.setWindowTitle("看海量化回测系统")
        # 设置窗口图标
        self.setWindowIcon(QIcon(self.get_icon_path("stock_icon.ico")))
        
        # 边框样式已在init_ui中设置，这里不再重复设置
        
        # 初始化更新管理器
        self.initialize_update_manager()
        
        # 初始化UI组件
        self.init_ui()
        
        # 初始化配置
        self.init_config()
        
        # 记录日志
        logging.info("GUI初始化完成")
        
        # 初始化属性
        self.strategy_thread = None
        self.log_handler = GUILogHandler(self)
        self.log_handler.setLevel(logging.INFO)
        
        # 日志存储
        self.log_entries = []
        
        # 更新实盘数据获取模块状态
        self.update_realtime_data_group_status()
        
        # 连接信号到槽（使用QueuedConnection确保跨线程调用不阻塞）
        self.log_signal.connect(self._log_message, Qt.QueuedConnection)
        self.update_status_signal.connect(self._update_status_table, Qt.QueuedConnection)
        self.show_backtest_result_signal.connect(self.show_backtest_result, Qt.QueuedConnection)
        self.progress_signal.connect(self.update_progress_bar, Qt.QueuedConnection)
        
        # 设置定时器定期刷新日志缓冲区
        self.log_flush_timer = QTimer()
        self.log_flush_timer.timeout.connect(self.flush_logs)
        self.log_flush_timer.start(5000)  # 每5秒刷新一次日志
        
        # 初始化代码编辑器模块（已废弃，现在使用EmbeddedVSCodeManager）
        self.editor_module = None
        # if get_editor_module is not None:
        #     try:
        #         self.editor_module = get_editor_module(self)
        #         logging.info("代码编辑器模块初始化成功")
        # 调试模式相关属性（开源版本不支持内置调试）
        self.debug_mode_enabled = False
        self.debug_manager = None

        # 记录启动信息到日志
        logging.info(f"软件启动时间: {self.start_time}")
        logging.info(f"当前版本: {get_version_info()['version']}")
        logging.info(f"日志文件路径: {os.path.join(LOGS_DIR, 'app.log')}")
        
        # 最后确保窗口在主屏幕居中显示（放在初始化的最末尾）
        self.center_window()
        self.show()


        # 初始化数据管理窗口实例变量
        self.csv_manager_window = None
        self.data_viewer_window = None
        self.scheduler_window = None

    def get_icon_path(self, icon_name):
        """获取图标文件的正确路径"""
        return os.path.join(os.path.dirname(__file__), 'icons', icon_name)

    def get_data_path(self, filename):
        """获取数据文件的正确路径"""
        return os.path.join(os.path.dirname(__file__), 'data', filename)

    def detect_screen_resolution(self):
        """检测屏幕分辨率并返回字体缩放比例"""
        from PyQt5.QtWidgets import QApplication
        screen = QApplication.desktop().screenGeometry()
        width = screen.width()
        height = screen.height()
        
        # 根据屏幕宽度确定字体缩放比例
        if width >= 3840:  # 4K及以上分辨率
            return 1.8
        elif width >= 2560:  # 2K分辨率
            return 1.4
        elif width >= 1920:  # 1080P分辨率
            return 1.0
        else:  # 低分辨率
            return 0.8

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
        
        # 计算缩放后的复选框指示器大小
        checkbox_indicator_size = max(20, int(20 * self.font_scale))
        
        return f"""
            /* 主窗口和基础样式 */
            QMainWindow {{
                background-color: #2b2b2b;
                color: #e8e8e8;
                border: 3px solid #c0c0c0;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            QWidget {{
                background-color: #2b2b2b;
                color: #e8e8e8;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            /* 分组框样式 */
            QGroupBox {{
                background-color: #333333;
                border: 1px solid #404040;
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
            
            /* 标签样式 */
            QLabel {{
                color: #e8e8e8;
                background-color: transparent;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            /* 链接样式 */
            QLabel[linkEnabled="true"] {{
                color: #a0a0a0;
                font-size: {scaled_sizes['normal']}px;
            }}
            QLabel[linkEnabled="true"]:hover {{
                color: #ffffff;
            }}
            
            /* 输入框样式 */
            QLineEdit {{
                background-color: #404040;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                padding: 5px;
                color: #e8e8e8;
                selection-background-color: #666666;
                font-size: {scaled_sizes['normal']}px;
            }}
            QLineEdit:focus {{
                border: 1px solid #737373;
                background-color: #454545;
            }}
            
            /* 按钮样式 */
            QPushButton {{
                background-color: #505050;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                color: #e8e8e8;
                min-width: 80px;
                font-weight: bold;
                font-size: {scaled_sizes['normal']}px;
            }}
            QPushButton:hover {{
                background-color: #606060;
            }}
            QPushButton:pressed {{
                background-color: #454545;
            }}
            QPushButton:disabled {{
                background-color: #404040;
                color: #808080;
            }}
            
            /* 下拉框样式 */
            QComboBox {{
                background-color: #404040;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                padding: 5px;
                color: #e8e8e8;
                min-width: 100px;
                font-size: {scaled_sizes['normal']}px;
            }}
            QComboBox:hover {{
                border: 1px solid #666666;
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
                background-color: transparent;
            }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 6px solid transparent;
                border-right: 6px solid transparent;
                border-top: 8px solid #e8e8e8;
                margin-right: 6px;
                margin-top: 2px;
            }}
            QComboBox::down-arrow:hover {{
                border-top: 8px solid #ffffff;
            }}
            QComboBox QAbstractItemView {{
                background-color: #404040;
                border: 1px solid #4d4d4d;
                selection-background-color: #666666;
                selection-color: #ffffff;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            /* 表格样式 */
            QTableWidget {{
                background-color: #333333;
                alternate-background-color: #383838;
                border: 1px solid #404040;
                color: #e8e8e8;
                gridline-color: #404040;
                font-size: {scaled_sizes['normal']}px;
            }}
            QTableWidget::item {{
                padding: 5px;
                background-color: transparent;
            }}
            QTableWidget::item:selected {{
                background-color: #505050;
                color: #ffffff;
            }}
            QHeaderView::section {{
                background-color: #404040;
                color: #e8e8e8;
                padding: 8px;
                border: none;
                border-right: 1px solid #4d4d4d;
                border-bottom: 1px solid #4d4d4d;
                font-weight: bold;
                font-size: {scaled_sizes['normal']}px;
            }}
            QTableCornerButton::section {{
                background-color: #404040;
                border: none;
                border-right: 1px solid #4d4d4d;
                border-bottom: 1px solid #4d4d4d;
            }}
            QTableCornerButton::section:pressed {{
                background-color: #505050;
            }}
            QHeaderView::section:vertical {{
                background-color: #404040;
                color: #e8e8e8;
                padding: 5px;
                border: none;
                border-right: 1px solid #4d4d4d;
                border-bottom: 1px solid #4d4d4d;
                font-size: {scaled_sizes['normal']}px;
            }}
            QHeaderView::section:vertical:hover {{
                background-color: #454545;
            }}
            QHeaderView::section:vertical:pressed {{
                background-color: #505050;
            }}
            
            /* 滚动条样式 */
            QScrollBar:vertical {{
                background-color: #3a3a3a;
                width: 15px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background-color: #5a5a5a;
                border-radius: 7px;
                min-height: 20px;
            }}
            QScrollBar::handle:vertical:hover {{
                background-color: #6a6a6a;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                border: none;
                background: none;
            }}
            QScrollBar:horizontal {{
                background-color: #3a3a3a;
                height: 15px;
                border: none;
            }}
            QScrollBar::handle:horizontal {{
                background-color: #5a5a5a;
                border-radius: 7px;
                min-width: 20px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background-color: #6a6a6a;
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                border: none;
                background: none;
            }}
            
            /* 复选框样式 */
            QCheckBox {{
                color: #e8e8e8;
                spacing: 5px;
                font-size: {scaled_sizes['normal']}px;
                background-color: transparent;
            }}
            QCheckBox::indicator {{
                width: {checkbox_indicator_size}px;
                height: {checkbox_indicator_size}px;
                border: 1px solid #666666;
                border-radius: 3px;
                background-color: #404040;
            }}
            QCheckBox::indicator:checked {{
                background-color: #007acc;
                border: 1px solid #007acc;
                image: none;
            }}
            QCheckBox::indicator:hover {{
                border: 1px solid #666666;
            }}
            
            /* 单选按钮样式 */
            QRadioButton {{
                color: #e8e8e8;
                spacing: 5px;
                font-size: {scaled_sizes['normal']}px;
            }}
            QRadioButton::indicator {{
                width: 18px;
                height: 18px;
                border: 1px solid #4d4d4d;
                border-radius: 9px;
                background-color: #404040;
            }}
            QRadioButton::indicator:checked {{
                background: qradialgradient(cx:0.5, cy:0.5, radius:0.4, 
                    stop:0 white, stop:0.4 white, stop:0.5 #007acc, stop:1 #007acc);
                border: 1px solid #007acc;
            }}
            QRadioButton::indicator:hover {{
                border: 1px solid #666666;
            }}
            
            /* 旋转框样式 */
            QSpinBox, QDoubleSpinBox {{
                background-color: #404040;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                padding: 5px;
                color: #e8e8e8;
                font-size: {scaled_sizes['normal']}px;
            }}
            QSpinBox:focus, QDoubleSpinBox:focus {{
                border: 1px solid #737373;
                background-color: #454545;
            }}
            QSpinBox::up-button, QDoubleSpinBox::up-button {{
                subcontrol-origin: border;
                subcontrol-position: top right;
                width: 16px;
                border-left: 1px solid #4d4d4d;
                background-color: #505050;
            }}
            QSpinBox::down-button, QDoubleSpinBox::down-button {{
                subcontrol-origin: border;
                subcontrol-position: bottom right;
                width: 16px;
                border-left: 1px solid #4d4d4d;
                background-color: #505050;
            }}
            QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-bottom: 6px solid #e8e8e8;
                margin-left: 4px;
            }}
            QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 6px solid #e8e8e8;
                margin-left: 4px;
            }}
            
            /* 日期时间编辑器样式 */
            QDateEdit, QTimeEdit, QDateTimeEdit {{
                background-color: #404040;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                padding: 5px;
                color: #e8e8e8;
                font-size: {scaled_sizes['normal']}px;
            }}
            QDateEdit:focus, QTimeEdit:focus, QDateTimeEdit:focus {{
                border: 1px solid #737373;
                background-color: #454545;
            }}
            QDateEdit::drop-down, QTimeEdit::drop-down, QDateTimeEdit::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 20px;
                border-left: 1px solid #4d4d4d;
                background-color: #505050;
            }}
            QDateEdit::down-arrow, QTimeEdit::down-arrow, QDateTimeEdit::down-arrow {{
                image: none;
                border-left: 6px solid transparent;
                border-right: 6px solid transparent;
                border-top: 8px solid #e8e8e8;
                margin-right: 2px;
            }}
            
            /* 文本编辑器样式 */
            QTextEdit, QPlainTextEdit {{
                background-color: #2b2b2b;
                border: 1px solid #404040;
                border-radius: 4px;
                color: #e8e8e8;
                selection-background-color: #505050;
                font-family: "Consolas", "Microsoft YaHei", monospace;
                font-size: {scaled_sizes['large']}px;
            }}
            
            /* 进度条样式 */
            QProgressBar {{
                background-color: #404040;
                border: 1px solid #4d4d4d;
                border-radius: 5px;
                text-align: center;
                font-size: {scaled_sizes['normal']}px;
            }}
            QProgressBar::chunk {{
                background-color: #007acc;
                border-radius: 4px;
            }}
            
            /* 状态栏样式 */
            QStatusBar {{
                background-color: #3a3a3a;
                color: #e8e8e8;
                border-top: 1px solid #4d4d4d;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            /* 菜单栏样式 */
            QMenuBar {{
                background-color: #333333;
                color: #e8e8e8;
                border-bottom: 1px solid #404040;
                font-size: {scaled_sizes['normal']}px;
            }}
            QMenuBar::item {{
                background-color: transparent;
                padding: 4px 8px;
            }}
            QMenuBar::item:selected {{
                background-color: #505050;
            }}
            
            /* 菜单样式 */
            QMenu {{
                background-color: #333333;
                border: 1px solid #404040;
                color: #e8e8e8;
                font-size: {scaled_sizes['normal']}px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                background-color: transparent;
            }}
            QMenu::item:selected {{
                background-color: #505050;
            }}
            QMenu::separator {{
                height: 1px;
                background-color: #404040;
                margin: 2px 0px;
            }}
            
            /* 工具栏样式 */
            QToolBar {{
                background-color: #333333;
                border: none;
                spacing: 2px;
                font-size: {scaled_sizes['normal']}px;
            }}
            QToolBar::separator {{
                background-color: #404040;
                width: 1px;
                margin: 2px;
            }}
            
            /* 工具提示样式 */
            QToolTip {{
                background-color: #555555;
                color: #e8e8e8;
                border: 1px solid #666666;
                padding: 4px;
                border-radius: 3px;
                font-size: {scaled_sizes['small']}px;
            }}
            
            /* Tab样式 */
            QTabWidget::pane {{
                border: 1px solid #404040;
                background-color: #333333;
            }}
            QTabBar::tab {{
                background-color: #404040;
                color: #e8e8e8;
                padding: 8px 16px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                font-size: {scaled_sizes['normal']}px;
            }}
            QTabBar::tab:selected {{
                background-color: #333333;
                border-bottom: 2px solid #007acc;
            }}
            QTabBar::tab:hover {{
                background-color: #505050;
            }}
            
            /* 分割器样式 */
            QSplitter::handle {{
                background-color: #404040;
            }}
            QSplitter::handle:horizontal {{
                width: 2px;
            }}
            QSplitter::handle:vertical {{
                height: 2px;
            }}
            
            /* 自定义信号指示器样式 */
            .signal-indicator {{
                border-radius: 10px;
                font-size: {scaled_sizes['small']}px;
                font-weight: bold;
            }}
            
            /* 自定义logo文本样式 */
            .logo-text {{
                font-size: {scaled_sizes['normal']}px;
            }}
            
            /* 启动画面样式 */
            .splash-screen {{
                background-color: #2b2b2b;
                border: 2px solid #404040;
                font-size: {scaled_sizes['large']}px;
                font-weight: bold;
            }}
            
            /* 进度文本样式 */
            .progress-text {{
                font-size: {scaled_sizes['normal']}px;
            }}
        """

    def log_message(self, message, level="INFO"):
        """发送日志信号"""
        self.log_signal.emit(message, level)
        
    def log_error(self, error_msg, error):
        """记录错误日志"""
        message = f"{error_msg}: {str(error)}"
        self.log_message(message, "ERROR")
        import traceback
        self.log_message(f"错误详情:\n{traceback.format_exc()}", "ERROR")
    
    def flush_logs(self):
        """强制刷新日志缓冲区，确保日志及时写入文件"""
        try:
            for handler in logging.getLogger().handlers:
                if hasattr(handler, 'flush'):
                    handler.flush()
        except Exception as e:
            # 避免在日志刷新时产生新的异常循环
            print(f"刷新日志时出错: {e}")
        
    def showEvent(self, event):
        """窗口显示事件"""
        super().showEvent(event)
        # 移除强制最大化，让窗口保持居中显示
        
    def changeEvent(self, event):
        """窗口状态变化事件，处理窗口还原时居中显示"""
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            if not self.isMaximized() and not self.isMinimized():
                # 窗口处于正常状态（非最大化非最小化），进行居中显示
                self.center_window()
                
    def center_window(self):
        """将窗口居中显示在主屏幕"""
        # 设置窗口大小为屏幕尺寸的80%
        # 使用主屏幕而不是跟随鼠标位置
        desktop = QDesktopWidget()
        primary_screen = desktop.primaryScreen()
        screen = desktop.availableGeometry(primary_screen)
        width = int(screen.width() * 0.7)
        height = int(screen.height() * 0.7)
        self.resize(width, height)
        
        # 计算居中位置（基于主屏幕）
        qr = self.frameGeometry()
        cp = screen.center()
        qr.moveCenter(cp)
        self.move(qr.topLeft())
        
        # 边框样式已在init_ui中设置，这里不再重复设置
        
    def init_ui(self):
        # 设置窗口标题栏颜色（仅适用于Windows）
        if sys.platform == 'win32':
            try:
                from ctypes import windll, c_int, byref, sizeof, create_string_buffer, create_unicode_buffer, Structure, POINTER
                from ctypes.wintypes import DWORD, HWND, BOOL

                # 定义必要的Windows API常量和结构
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                DWMWA_CAPTION_COLOR = 35  # 标题栏颜色
                
                # 启用深色模式
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_USE_IMMERSIVE_DARK_MODE,
                    byref(c_int(2)),  # 2 means true
                    sizeof(c_int)
                )
                
                # 设置标题栏颜色
                caption_color = DWORD(0x2b2b2b)  # 使用与主界面相同的颜色
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_CAPTION_COLOR,
                    byref(caption_color),
                    sizeof(caption_color)
                )

            except Exception as e:
                logging.warning(f"设置标题栏深色模式失败: {str(e)}")
        
        # 设置根据分辨率缩放的深色主题样式表
        self.setStyleSheet(self.get_scaled_stylesheet())
        
        # 创建自定义日志处理器（移到最前面）
        self.log_handler = GUILogHandler(self)
        self.log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logging.getLogger('').addHandler(self.log_handler)
        
        # 设置窗口标题
        self.setWindowTitle("看海量化回测系统")
        
        # 设置窗口图标
        logo_path = self.get_icon_path("stock_icon.ico")
        if os.path.exists(logo_path):
            self.setWindowIcon(QIcon(logo_path))
        else:
            # 尝试png格式
            logo_path_png = self.get_icon_path("stock_icon.png")
            if os.path.exists(logo_path_png):
                self.setWindowIcon(QIcon(logo_path_png))
            else:
                self.log_message(f"图标文件不存在: {logo_path}", "WARNING")
        
        # 创建工具栏
        self.create_toolbar()
        
        # 创建主窗口部件和布局
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        
        # 添加状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        
        # 添加状态标签（放在右侧）
        self.status_label = QLabel("就绪")
        self.status_label.setFixedWidth(100)
        self.status_bar.addPermanentWidget(self.status_label)
        
        # 进度条容器：叠放进度条与文本，保证文本浮在上方
        class ProgressOverlay(QWidget):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                layout = QHBoxLayout(self)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(6)
                # 进度条（左侧）
                self.bar = QProgressBar(self)
                self.bar.setTextVisible(False)
                self.bar.setRange(0, 100)
                self.bar.setValue(0)
                self.bar.setFormat("")  # 禁止进度条自绘文字
                self.bar.setFixedHeight(16)
                self.bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                layout.addWidget(self.bar, 1)
                # 右侧文本
                self.label = QLabel("回测进度: 0%", self)
                self.label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
                self.label.setStyleSheet("""
                    QLabel {
                        color: white;
                        font-weight: bold;
                        background: transparent;
                        padding-left: 2px;
                    }
                """)
                self.label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
                layout.addWidget(self.label, 0)
        
        # 使用自定义覆盖控件
        self.progress_container = ProgressOverlay()
        self.progress_bar = self.progress_container.bar
        self.progress_text = self.progress_container.label
        
        # 添加到状态栏
        self.status_bar.addWidget(self.progress_container, 1)
         
         # 默认隐藏进度条
        self.progress_container.hide()
        
        # 设置状态栏样式
        self.status_bar.setStyleSheet("""
            QStatusBar {
                background-color: #333333;
                color: #e8e8e8;
                padding: 2px;
                border-top: 1px solid #404040;
            }
            QStatusBar::item {
                border: none;
            }
        """)
        
        # 确保状态栏可见
        self.status_bar.setVisible(True)
        
        main_layout = QHBoxLayout()  # 水平布局，包含三列
        main_widget.setLayout(main_layout)
        
        # 创建三个面板
        left_panel = QWidget()
        middle_panel = QWidget()
        right_panel = QWidget()
        
        self.left_layout = QVBoxLayout()
        self.middle_layout = QVBoxLayout()
        self.right_layout = QVBoxLayout()
        
        left_panel.setLayout(self.left_layout)
        middle_panel.setLayout(self.middle_layout)
        right_panel.setLayout(self.right_layout)
        
        # 设置三个面板的最小宽度
        left_panel.setMinimumWidth(500)
        middle_panel.setMinimumWidth(500)
        right_panel.setMinimumWidth(500)
        
        # 添加三个面板到主布局
        main_layout.addWidget(left_panel)
        main_layout.addWidget(middle_panel)
        main_layout.addWidget(right_panel)
        
        # 调整大小以适应内容
        # self.adjustSize()  # 删除此行，因为它会覆盖最大化设置
        
        # 设置三个面板的内容
        self.setup_left_panel()
        self.setup_middle_panel()  # 新增中间面板设置方法
        self.setup_right_panel()
        
        # 连接信号（运行模式已固定为回测，无需连接信号）
        
        # 初始化用户策略目录
        self.init_user_strategies()
        
        # 初始化配置
        self.init_config()
        
        # 记录日志
        logging.info("GUI初始化完成")
        # 最后执行窗口最大化（确保在所有UI设置完成后再最大化）
        # self.showMaximized()  # 删除此行，已移至__init__方法末尾

    def create_toolbar(self):
        """创建工具栏"""
        toolbar = self.addToolBar("工具栏")
        toolbar.setObjectName("mainToolBar")  # 添加objectName属性
        toolbar.setMovable(False)  # 设置工具栏不可移动
        
        # 添加加载配置按钮
        load_config_action = toolbar.addAction("加载配置")
        load_config_action.triggered.connect(self.load_config)
        
        # 添加保存配置按钮
        save_config_action = toolbar.addAction("保存配置")
        save_config_action.triggered.connect(self.save_config)
        
        # 添加配置另存为按钮
        save_config_as_action = toolbar.addAction("配置另存为")
        save_config_as_action.triggered.connect(self.save_config_as)
        
        # 添加分隔符
        toolbar.addSeparator()
        
        # 添加开始运行按钮
        self.start_action = toolbar.addAction("开始运行")
        self.start_action.triggered.connect(self.start_strategy)
        
        # 添加停止运行按钮
        self.stop_action = toolbar.addAction("停止运行")
        self.stop_action.triggered.connect(self.stop_strategy)
        self.stop_action.setEnabled(False)  # 初始状态禁用
        
        # 为运行和停止按钮设置特定颜色样式
        self.set_button_colors()
        
        # 添加分隔符
        toolbar.addSeparator()
        
        # 添加本地数据管理按钮
        data_viewer_action = toolbar.addAction("本地数据管理")
        data_viewer_action.setToolTip("查看和分析本地存储的股票数据")
        data_viewer_action.triggered.connect(self.open_data_viewer)
        
        # 添加定时补充按钮
        scheduler_action = toolbar.addAction("定时补充数据")
        scheduler_action.setToolTip("设置和管理数据定时补充任务")
        scheduler_action.triggered.connect(self.open_scheduler)
        
        # 添加CSV数据管理按钮
        data_module_action = toolbar.addAction("CSV数据管理")
        data_module_action.setToolTip("打开CSV数据下载、清洗和管理界面")
        data_module_action.triggered.connect(self.open_data_module)
        

        
        # 添加分隔符
        toolbar.addSeparator()

        # 添加设置按钮
        settings_action = toolbar.addAction("设置")
        settings_action.triggered.connect(self.show_settings)

        # 添加弹性空间
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        toolbar.addWidget(spacer)

        # 创建状态指示灯
        self.status_indicator = QLabel()
        self.status_indicator.setFixedSize(16, 16)
        self.status_indicator.setToolTip("MiniQMT状态")

        # 创建一个容器来包装状态指示灯，并添加边距
        indicator_container = QWidget()
        indicator_layout = QHBoxLayout(indicator_container)
        indicator_layout.setContentsMargins(0, 0, 10, 0)  # 右边距为10像素
        indicator_layout.addWidget(self.status_indicator)
        toolbar.addWidget(indicator_container)

        # 添加帮助按钮
        help_btn = QToolButton()
        help_btn.setText("?")
        help_btn.setToolTip("打开使用教程")
        help_btn.setStyleSheet("""
            QToolButton {
                background-color: #505050;
                color: #e8e8e8;
                border: none;
                border-radius: 10px;
                font-weight: bold;
                min-width: 20px;
                max-width: 20px;
                min-height: 20px;
                max-height: 20px;
            }
            QToolButton:hover {
                background-color: #606060;
            }
        """)
        help_btn.clicked.connect(self.open_help_tutorial)
        toolbar.addWidget(help_btn)
        
        # 添加定时器来检查软件状态
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.check_software_status)
        self.status_timer.start(5000)  # 每5秒检查一次
        
        # 初始检查
        self.check_software_status()
        
        # 设置工具栏样式
        toolbar.setStyleSheet("""
            QToolBar {
                background-color: #333333;
                border: none;
                spacing: 10px;
                padding: 5px;
            }
            QToolButton {
                background-color: #505050;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                color: #e8e8e8;
                min-width: 80px;
                font-weight: bold;
            }
            QToolButton:hover {
                background-color: #606060;
            }
            QToolButton:pressed {
                background-color: #454545;
            }
            QToolButton:disabled {
                background-color: #404040;
                color: #808080;
            }
        """)

    def set_button_colors(self):
        """为运行和停止按钮设置特定颜色样式"""
        # 获取工具栏中的按钮widget
        toolbar = self.findChild(QToolBar, "mainToolBar")
        if toolbar:
            # 为开始运行按钮设置绿色样式
            for action in toolbar.actions():
                widget = toolbar.widgetForAction(action)
                if widget and action == self.start_action:
                    widget.setStyleSheet("""
                        QToolButton {
                            background-color: #2d7a2d;
                            border: none;
                            border-radius: 4px;
                            padding: 8px 16px;
                            color: white;
                            min-width: 80px;
                            font-weight: bold;
                        }
                        QToolButton:hover {
                            background-color: #3d8a3d;
                        }
                        QToolButton:pressed {
                            background-color: #1d6a1d;
                        }
                        QToolButton:disabled {
                            background-color: #404040;
                            color: #808080;
                        }
                    """)
                # 为停止运行按钮设置红色样式
                elif widget and action == self.stop_action:
                    widget.setStyleSheet("""
                        QToolButton {
                            background-color: #8b2635;
                            border: none;
                            border-radius: 4px;
                            padding: 8px 16px;
                            color: white;
                            min-width: 80px;
                            font-weight: bold;
                        }
                        QToolButton:hover {
                            background-color: #9b3645;
                        }
                        QToolButton:pressed {
                            background-color: #7b1625;
                        }
                        QToolButton:disabled {
                            background-color: #404040;
                            color: #808080;
                        }
                    """)

    def check_software_status(self):
        """检查MiniQMT软件状态"""
        try:
            # 检查进程是否存在
            is_running = self.is_software_running("XtMiniQmt.exe")

            if is_running:
                self.update_status_indicator("green", "MiniQMT已启动")
            else:
                self.update_status_indicator("red", "MiniQMT未启动")

        except Exception as e:
            logging.error(f"检查软件状态时出错: {str(e)}")
            self.update_status_indicator("red", "状态检查失败")

    def is_software_running(self, process_name):
        """检查指定的进程是否正在运行"""
        import psutil
        for proc in psutil.process_iter(['name']):
            if proc.info['name'].lower() == process_name.lower():
                return True
        return False

    def update_status_indicator(self, color, tooltip):
        """更新状态指示器"""
        try:
            pixmap = QPixmap(16, 16)
            pixmap.fill(Qt.transparent)
            
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            
            # 设置颜色
            if color == "green":
                painter.setBrush(QColor("#00FF00"))
            else:
                painter.setBrush(QColor("#FF0000"))
            
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(0, 0, 16, 16)
            painter.end()
            
            self.status_indicator.setPixmap(pixmap)
            self.status_indicator.setToolTip(tooltip)
            
        except Exception as e:
            logging.error(f"更新状态指示器时出错: {str(e)}")

    def init_config(self):
        """初始化配置"""
        self.config = {
            "run_mode": "backtest",  # 固定为回测模式
            "account": {"account_id": "", "account_type": "STOCK"},
            "system": {
                "userdata_path": "",
                "session_id": int(datetime.now().timestamp()),
                "check_interval": 3,
                "init_data_enabled": False
            },
            "data": {
                "kline_period": "1m",
                "stock_list_file": "",
                "fields": ["time", "open", "high", "low", "close", "volume", "amount"],
                "dividend_type": "front"
            },
            "backtest": {
                "start_time": "",
                "end_time": "",
                "init_capital": 1000000,  # 修改回init_capital
                "benchmark": "sh.000300",  # 基准合约，默认沪深300
                "min_volume": 100,  # 最小交易量，移到backtest配置中
                "trade_cost": {
                    "min_commission": 5.0,  # 最低佣金（元）
                    "commission_rate": 0.0001,  # 佣金比例，修改为0.0001
                    "stamp_tax_rate": 0.001,  # 卖出印花税，修改为0.0005！
                    "flow_fee": 0.0,  # 流量费（元/笔），修改为0
                    "slippage": {
                        "type": "ratio",  # tick(最小变动价跳数) 或 ratio(可变滑点百分比)
                        "tick_size": 0.01,  # A股最小变动价（1分钱）
                        "tick_count": 2,  # 跳数（用于tick类型，表示跳2个最小单位，即0.02元）
                        "ratio": 0.001  # 滑点比例（用于ratio类型，0.001表示0.1%）
                    }
                },
                "risk": {
                    "position_limit": 0.95,
                    "order_limit": 100,
                    "loss_limit": 0.1
                },
                "strategy_file": ""
            }
        }

    def setup_left_panel(self):
        """设置左侧配置面板"""
        left_scroll_area = QScrollArea()
        left_scroll_area.setWidgetResizable(True)
        left_scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }") # 融入整体风格

        left_scroll_content_widget = QWidget()
        left_scroll_layout = QVBoxLayout(left_scroll_content_widget) # 内容的布局

        # 策略配置组
        strategy_group = QGroupBox("策略配置")
        strategy_layout = QVBoxLayout()
        
        # 策略文件选择
        file_layout = QHBoxLayout()
        self.strategy_path = QLineEdit()
        select_btn = QPushButton("选择策略文件")
        select_btn.clicked.connect(self.select_strategy_file)
        file_layout.addWidget(QLabel("策略文件:"))
        file_layout.addWidget(self.strategy_path)
        file_layout.addWidget(select_btn)
        strategy_layout.addLayout(file_layout)
        
        # 运行模式选择（固定为回测）
        mode_layout = QHBoxLayout()
        self.mode_selector = QLabel("回测")  # 固定为回测模式
        self.mode_selector.setStyleSheet("QLabel { padding: 3px; border: 1px solid #666666; background-color: #333333; color: #e8e8e8; }")
        mode_layout.addWidget(QLabel("运行模式:"))
        mode_layout.addWidget(self.mode_selector)
        strategy_layout.addLayout(mode_layout)
        
        strategy_group.setLayout(strategy_layout)
        left_scroll_layout.addWidget(strategy_group)
        
        # 回测参数配置组
        backtest_group = QGroupBox("回测参数")
        backtest_layout = QVBoxLayout()
        
        # 基准合约设置
        benchmark_layout = QHBoxLayout()
        self.benchmark_input = QLineEdit()
        self.benchmark_input.setText("sh.000300")  # 默认沪深300
        self.benchmark_input.setPlaceholderText("请输入基准合约代码")
        benchmark_layout.addWidget(QLabel("基准合约:"))
        benchmark_layout.addWidget(self.benchmark_input)
        backtest_layout.addLayout(benchmark_layout)
        
        # 交易成本设置组
        cost_group = QGroupBox("交易成本设置")
        cost_layout = QGridLayout()
        
        # 最低佣金
        self.min_commission = QLineEdit()
        self.min_commission.setValidator(QDoubleValidator())
        self.min_commission.setText("5.0")
        cost_layout.addWidget(QLabel("最低佣金(元):"), 0, 0)
        cost_layout.addWidget(self.min_commission, 0, 1)
        
        # 佣金比例
        self.commission_rate = QLineEdit()
        self.commission_rate.setValidator(QDoubleValidator(0.0, 1.0, 7))
        self.commission_rate.setText("0.0001")
        cost_layout.addWidget(QLabel("佣金比例:"), 1, 0)
        cost_layout.addWidget(self.commission_rate, 1, 1)
        
        # 印花税
        self.stamp_tax = QLineEdit()
        self.stamp_tax.setValidator(QDoubleValidator(0.0, 1.0, 7))
        self.stamp_tax.setText("0.0005")
        cost_layout.addWidget(QLabel("卖出印花税:"), 2, 0)
        cost_layout.addWidget(self.stamp_tax, 2, 1)
        
        # 流量费
        self.flow_fee = QLineEdit()
        self.flow_fee.setValidator(QDoubleValidator(0.0, 100.0, 2))
        self.flow_fee.setText("0.0")
        cost_layout.addWidget(QLabel("流量费(元/笔):"), 3, 0)
        cost_layout.addWidget(self.flow_fee, 3, 1)
        
        # 滑点设置
        slippage_type_label = QLabel("滑点类型:")
        self.slippage_type = NoWheelComboBox()
        self.slippage_type.addItems(["按最小变动价跳数", "按成交金额比例"])
        self.slippage_type.currentTextChanged.connect(self.slippage_type_changed)
        cost_layout.addWidget(slippage_type_label, 4, 0)
        cost_layout.addWidget(self.slippage_type, 4, 1)
        
        self.slippage_value = QLineEdit()
        self.slippage_value.setValidator(QDoubleValidator())
        self.slippage_value.setText("0.0")
        cost_layout.addWidget(QLabel("滑点值:"), 5, 0)
        cost_layout.addWidget(self.slippage_value, 5, 1)
        
        cost_group.setLayout(cost_layout)
        backtest_layout.addWidget(cost_group)
        
        # 直接添加时间范围设置（移除了账户信息部分）
        time_group = QGroupBox("回测时间设置")
        time_layout = QGridLayout()
        
        # 开始日期选择
        self.start_date = NoWheelDateEdit()
        self.start_date.setDisplayFormat("yyyy-MM-dd")  # 修改这里的显示格式
        self.start_date.setCalendarPopup(True)
        self.start_date.setMinimumDate(QDate(2000, 1, 1))
        self.start_date.setMaximumDate(QDate.currentDate())
        time_layout.addWidget(QLabel("开始日期:"), 0, 0)
        time_layout.addWidget(self.start_date, 0, 1)
        
        # 结束日期选择
        self.end_date = NoWheelDateEdit()
        self.end_date.setDisplayFormat("yyyy-MM-dd")  # 修改这里的显示格式
        self.end_date.setCalendarPopup(True)
        self.end_date.setMinimumDate(QDate(2000, 1, 1))
        self.end_date.setMaximumDate(QDate.currentDate())
        time_layout.addWidget(QLabel("结束日期:"), 1, 0)
        time_layout.addWidget(self.end_date, 1, 1)
        
        # 连接开始日期变化信号
        self.start_date.dateChanged.connect(self.on_start_date_changed)
        
        time_group.setLayout(time_layout)
        backtest_layout.addWidget(time_group)
        
        # 数据设置组
        data_group = QGroupBox("数据设置")
        data_layout = QGridLayout()
        
        # 复权方式选择
        adjust_layout = QHBoxLayout()
        self.adjust_selector = NoWheelComboBox()
        self.adjust_selector.addItems(["不复权", "前复权", "后复权", "等比前复权", "等比后复权"])
        self.adjust_selector.setCurrentText("等比前复权")  # 设置默认值
        data_layout.addWidget(QLabel("复权方式:"), 0, 0)
        data_layout.addWidget(self.adjust_selector, 0, 1)
        
        # 周期类型选择
        period_layout = QHBoxLayout()
        self.period_selector = NoWheelComboBox()
        self.period_selector.addItems(["tick", "1m", "5m", "1d"])
        self.period_selector.setCurrentText("1m")  # 设置默认值
        self.period_selector.currentTextChanged.connect(self.on_period_changed)
        data_layout.addWidget(QLabel("周期类型:"), 1, 0)
        data_layout.addWidget(self.period_selector, 1, 1)
        
        # 字段列表选择
        fields_layout = QVBoxLayout()
        fields_label = QLabel("数据字段:")
        fields_layout.addWidget(fields_label)
        
        # 创建字段选择的复选框组
        self.fields_checkboxes = {}
        # 分笔数据字段（tick）
        self.tick_fields = {
            "lastPrice": "最新价",
            "open": "开盘价",
            "high": "最高价",
            "low": "最低价",
            "lastClose": "前收盘价",
            "amount": "成交总额",
            "volume": "成交总量",
            "pvolume": "原始成交总量",
            "stockStatus": "证券状态",
            "openInt": "持仓量",
            "lastSettlementPrice": "前结算",
            "askPrice": "委卖价",
            "bidPrice": "委买价",
            "askVol": "委卖量",
            "bidVol": "委买量"
        }
        
        # K线数据字段
        self.kline_fields = {
            "open": "开盘价",
            "high": "最高价",
            "low": "最低价",
            "close": "收盘价",
            "volume": "成交量",
            "amount": "成交额",
            "settelementPrice": "今结算",
            "openInterest": "持仓量",
            "preClose": "前收价",
            "suspendFlag": "停牌标记"
        }
        
        # 创建字段选择的网格布局
        self.fields_grid = QGridLayout()
        data_layout.addLayout(self.fields_grid, 2, 0, 1, 2)
        
        data_group.setLayout(data_layout)
        backtest_layout.addWidget(data_group)
        
        # 初始化字段列表
        self.update_fields_list("1m")
        
        # 在回测参数配置组中添加股票池设置
        # 股票池设置
        self.stock_pool_group = QGroupBox("股票池设置")
        self.stock_pool_group_default_title = "股票池设置"  # 保存默认标题
        stock_pool_layout = QVBoxLayout()
        
        # 常用股票池选择
        common_pool_layout = QGridLayout()
        self.pool_checkboxes = {}
        common_pools = {
            "上证50": "sh.000016",
            "沪深300": "sh.000300",
            "中证500": "sh.000905",
            "创业板指": "sz.399006",
            "沪深A股": "all_a",
            "科创板": "sci_tech",
            "上证A股": "sh_a"
        }
        
        # 添加其他股票池的复选框
        row = 0
        col = 0
        for name, code in common_pools.items():
            # 创建水平布局来放置复选框和标签
            item_layout = QHBoxLayout()
            
            # 创建复选框
            cb = QCheckBox()
            cb.stateChanged.connect(lambda state, code=code: self.on_pool_changed(code, state))
            self.pool_checkboxes[code] = cb
            
            # 创建标签并关联到复选框
            label = QLabel(name)
            label.mousePressEvent = lambda event, checkbox=cb: checkbox.setChecked(not checkbox.isChecked())
            # 设置鼠标样式为手型
            label.setCursor(Qt.PointingHandCursor)
            
            # 添加到布局
            item_layout.addWidget(cb)
            item_layout.addWidget(label)
            item_layout.addStretch()
            
            # 将整个布局添加到网格中
            common_pool_layout.addLayout(item_layout, row, col)
            col += 1
            if col > 2:  # 每行3个复选框
                col = 0
                row += 1
        
        # 先添加其他股票池
        stock_pool_layout.addLayout(common_pool_layout)
        
        # 添加自选清单标签和复选框
        custom_list_layout = QHBoxLayout()
        
        # 先添加复选框
        custom_list_cb = QCheckBox()
        custom_list_cb.stateChanged.connect(lambda state: self.on_pool_changed("custom", state))
        self.pool_checkboxes["custom"] = custom_list_cb
        custom_list_layout.addWidget(custom_list_cb)
        
        # 再添加可点击的标签
        custom_list_label = QLabel('<a href="custom" style="color: #e8e8e8; text-decoration: underline;">自选清单</a>')
        custom_list_label.setOpenExternalLinks(False)
        custom_list_label.linkActivated.connect(self.open_custom_list)
        custom_list_layout.addWidget(custom_list_label)
        
        custom_list_layout.addStretch()
        
        # 再添加自选清单
        stock_pool_layout.addLayout(custom_list_layout)
        
        # 自定义股票列表
        custom_pool_layout = QVBoxLayout()
        self.stock_list = QTableWidget(0, 2)
        self.stock_list.setHorizontalHeaderLabels(["股票代码", "股票名称"])
        self.stock_list.horizontalHeader().setStretchLastSection(True)
        
        # 设置最小高度，确保股票列表有足够的显示空间
        self.stock_list.setMinimumHeight(200)
        
        # 设置大小策略，让股票列表能够随窗口大小变化而自适应调整
        from PyQt5.QtWidgets import QSizePolicy
        self.stock_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        # 为股票列表分配伸展因子，让它在垂直方向上占据更多空间
        custom_pool_layout.addWidget(self.stock_list, 1)  # 伸展因子为1
        
        # 添加按钮
        btn_layout = QHBoxLayout()
        add_stock_btn = QPushButton("添加股票")
        import_btn = QPushButton("导入股票")
        delete_btn = QPushButton("删除选中")
        clear_btn = QPushButton("清空列表")
        
        add_stock_btn.clicked.connect(self.add_single_stock)
        import_btn.clicked.connect(self.import_stocks)
        delete_btn.clicked.connect(self.delete_selected_stocks)
        clear_btn.clicked.connect(self.clear_stock_list)
        
        btn_layout.addWidget(add_stock_btn)
        btn_layout.addWidget(import_btn)
        btn_layout.addWidget(delete_btn)
        btn_layout.addWidget(clear_btn)
        custom_pool_layout.addLayout(btn_layout)
        
        stock_pool_layout.addLayout(custom_pool_layout)
        self.stock_pool_group.setLayout(stock_pool_layout)
        
        # 为股票池组设置大小策略，让它能够扩展
        self.stock_pool_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        # 添加股票池组到回测布局时，为其分配更大的伸展因子
        backtest_layout.addWidget(self.stock_pool_group, 1)  # 伸展因子为1，让它占据更多空间
        
        backtest_group.setLayout(backtest_layout)
        
        # 为回测组设置大小策略，让它能够扩展
        backtest_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        # 添加回测组到滚动布局时，为其分配伸展因子
        left_scroll_layout.addWidget(backtest_group, 1)  # 伸展因子为1，让它占据更多空间
        
        # 添加弹性空间（伸展因子设为0，避免与回测组竞争空间）
        left_scroll_layout.addStretch(0)

        left_scroll_area.setWidget(left_scroll_content_widget)
        self.left_layout.addWidget(left_scroll_area) # 将滚动区域添加到原左侧面板的布局中

    def setup_middle_panel(self):
        """设置中间面板，包含触发方式设置和账户信息"""
        # 创建触发方式设置组
        trigger_group = QGroupBox("触发方式设置")
        trigger_layout = QVBoxLayout()
        
        # 触发类型选择
        trigger_type_layout = QHBoxLayout()
        trigger_type_layout.addWidget(QLabel("触发类型:"))
        self.trigger_type_combo = NoWheelComboBox()
        self.trigger_type_combo.addItems(["Tick触发", "1分钟K线触发", "5分钟K线触发", "日K线触发", "自定义定时触发"])
        self.trigger_type_combo.currentIndexChanged.connect(self.trigger_type_changed)
        trigger_type_layout.addWidget(self.trigger_type_combo)
        trigger_layout.addLayout(trigger_type_layout)
        
        # 创建堆叠小部件用于不同触发类型的配置
        self.trigger_stack = QStackedWidget()
        
        # Tick触发配置页面（无需额外配置）
        tick_page = QWidget()
        tick_layout = QVBoxLayout()
        tick_layout.addWidget(QLabel("Tick触发无需额外配置，每个Tick都会触发策略"))
        tick_layout.addStretch()
        tick_page.setLayout(tick_layout)
        
        # 1分钟K线触发配置页面
        k1_page = QWidget()
        k1_layout = QVBoxLayout()
        k1_layout.addWidget(QLabel("1分钟K线触发无需额外配置，每形成一个1分钟K线就会触发策略"))
        k1_layout.addStretch()
        k1_page.setLayout(k1_layout)
        
        # 5分钟K线触发配置页面
        k5_page = QWidget()
        k5_layout = QVBoxLayout()
        k5_layout.addWidget(QLabel("5分钟K线触发无需额外配置，每形成一个5分钟K线就会触发策略"))
        k5_layout.addStretch()
        k5_page.setLayout(k5_layout)
        
        # 日K线触发配置页面
        daily_page = QWidget()
        daily_layout = QVBoxLayout()
        daily_layout.addWidget(QLabel("日K线触发无需额外配置，每个交易日开盘后触发一次策略"))
        daily_layout.addStretch()
        daily_page.setLayout(daily_layout)
        
        # 自定义定时触发配置页面
        custom_page = QWidget()
        custom_layout = QVBoxLayout()
        
        # 时间点列表
        custom_layout.addWidget(QLabel("时间点列表（每行一个时间点，格式：HH:MM:SS）:"))
        self.custom_times_edit = QTextEdit()
        self.custom_times_edit.setPlaceholderText("09:30:00\n10:00:00\n10:30:00\n...")
        custom_layout.addWidget(self.custom_times_edit)
        
        # 时间规则生成器
        generator_group = QGroupBox("时间点生成器")
        generator_layout = QGridLayout()
        
        # 生成器类型
        generator_type_layout = QHBoxLayout()
        generator_type_layout.addWidget(QLabel("生成器类型:"))
        self.generator_type_combo = NoWheelComboBox()
        self.generator_type_combo.addItems(["均匀分布", "整点分布", "自定义间隔"])
        generator_type_layout.addWidget(self.generator_type_combo)
        generator_layout.addLayout(generator_type_layout, 0, 0, 1, 2)
        
        # 开始时间
        start_time_layout = QHBoxLayout()
        start_time_layout.addWidget(QLabel("开始时间:"))
        self.start_time_edit = NoWheelTimeEdit()
        self.start_time_edit.setDisplayFormat("HH:mm:ss")
        self.start_time_edit.setTime(QTime(9, 30, 0))
        start_time_layout.addWidget(self.start_time_edit)
        generator_layout.addLayout(start_time_layout, 1, 0)
        
        # 结束时间
        end_time_layout = QHBoxLayout()
        end_time_layout.addWidget(QLabel("结束时间:"))
        self.end_time_edit = NoWheelTimeEdit()
        self.end_time_edit.setDisplayFormat("HH:mm:ss")
        self.end_time_edit.setTime(QTime(15, 0, 0))
        end_time_layout.addWidget(self.end_time_edit)
        generator_layout.addLayout(end_time_layout, 1, 1)
        
        # 时间间隔
        interval_layout = QHBoxLayout()
        interval_layout.addWidget(QLabel("时间间隔(秒):"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setMinimum(3)
        self.interval_spin.setMaximum(3600)
        self.interval_spin.setValue(300)  # 默认5分钟
        self.interval_spin.setSingleStep(3)
        interval_layout.addWidget(self.interval_spin)
        generator_layout.addLayout(interval_layout, 2, 0)
        
        # 生成按钮
        generate_btn = QPushButton("生成时间点")
        generate_btn.clicked.connect(self.generate_time_points)
        generator_layout.addWidget(generate_btn, 2, 1)
        
        generator_group.setLayout(generator_layout)
        custom_layout.addWidget(generator_group)
        
        custom_page.setLayout(custom_layout)
        
        # 将页面添加到堆叠小部件
        self.trigger_stack.addWidget(tick_page)
        self.trigger_stack.addWidget(k1_page)
        self.trigger_stack.addWidget(k5_page)
        self.trigger_stack.addWidget(daily_page)
        self.trigger_stack.addWidget(custom_page)
        
        trigger_layout.addWidget(self.trigger_stack)
        trigger_group.setLayout(trigger_layout)
        
        # 添加到中间面板
        self.middle_layout.addWidget(trigger_group)
        
        # 创建实盘数据获取组
        self.realtime_data_group = QGroupBox("实盘数据获取")
        self.realtime_data_layout = QVBoxLayout()
        
        # 创建单选按钮组
        self.realtime_data_radio_group = QButtonGroup(self)
        self.full_quote_radio = QRadioButton("订阅全推行情")
        self.single_quote_radio = QRadioButton("单股订阅")
        self.realtime_data_radio_group.addButton(self.full_quote_radio)
        self.realtime_data_radio_group.addButton(self.single_quote_radio)
        
        # 添加单选按钮到布局
        self.realtime_data_layout.addWidget(self.full_quote_radio)
        self.realtime_data_layout.addWidget(self.single_quote_radio)
        
        # 添加提示标签
        self.custom_data_label = QLabel("请在策略中获取数据，可以使用单股订阅subscribe_quote+get_market_data_ex、获取全推数据 get_full_tick等方式实现")
        self.custom_data_label.setWordWrap(True)
        self.realtime_data_layout.addWidget(self.custom_data_label)
        self.custom_data_label.hide()  # 默认隐藏
        
        self.realtime_data_group.setLayout(self.realtime_data_layout)
        
        # 默认选择单股订阅
        self.single_quote_radio.setChecked(True)
        
        # 添加到中间面板
        self.middle_layout.addWidget(self.realtime_data_group)
        
        # 默认禁用实盘数据获取组件
        self.realtime_data_group.setEnabled(False)
        
        # 创建账户信息组
        account_group = QGroupBox("账户信息")
        account_layout = QGridLayout()
        
        # 初始资金输入
        self.initial_cash = QLineEdit()
        self.initial_cash.setValidator(QDoubleValidator())
        self.initial_cash.setText("1000000")
        account_layout.addWidget(QLabel("初始资金:"), 0, 0)
        account_layout.addWidget(self.initial_cash, 0, 1)
        
        # 最小交易量输入
        self.min_volume = QLineEdit()
        self.min_volume.setValidator(QIntValidator())
        self.min_volume.setText("100")
        account_layout.addWidget(QLabel("最小交易量:"), 1, 0)
        account_layout.addWidget(self.min_volume, 1, 1)
        
        account_group.setLayout(account_layout)
        
        # 添加到中间面板
        self.middle_layout.addWidget(account_group)
        
        # 创建盘前盘后触发设置组
        pre_post_group = QGroupBox("盘前盘后触发设置")
        pre_post_layout = QVBoxLayout()
        
        # 盘前触发设置
        pre_trigger_layout = QHBoxLayout()
        self.pre_trigger_checkbox = QCheckBox("触发盘前回调")
        self.pre_trigger_time = NoWheelTimeEdit()
        self.pre_trigger_time.setDisplayFormat("HH:mm:ss")
        self.pre_trigger_time.setTime(QTime(8, 30, 0))
        pre_trigger_layout.addWidget(self.pre_trigger_checkbox)
        pre_trigger_layout.addWidget(QLabel("运行时间:"))
        pre_trigger_layout.addWidget(self.pre_trigger_time)
        pre_post_layout.addLayout(pre_trigger_layout)
        
        # 盘后触发设置
        post_trigger_layout = QHBoxLayout()
        self.post_trigger_checkbox = QCheckBox("触发盘后回调")
        self.post_trigger_time = NoWheelTimeEdit()
        self.post_trigger_time.setDisplayFormat("HH:mm:ss")
        self.post_trigger_time.setTime(QTime(15, 30, 0))
        post_trigger_layout.addWidget(self.post_trigger_checkbox)
        post_trigger_layout.addWidget(QLabel("运行时间:"))
        post_trigger_layout.addWidget(self.post_trigger_time)
        pre_post_layout.addLayout(post_trigger_layout)
        
        pre_post_group.setLayout(pre_post_layout)
        self.middle_layout.addWidget(pre_post_group)
        
    def setup_right_panel(self):
        """设置右侧面板，只包含系统日志"""
        # 创建系统日志组
        log_group = QGroupBox("系统日志")
        log_layout = QVBoxLayout()
        
        # 创建日志文本框
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)  # 设置为只读
        self.log_text.setLineWrapMode(QTextEdit.WidgetWidth)  # 自动换行
        
        # 设置日志文本框的样式
        self.log_text.setStyleSheet("""
            QTextEdit {
                background-color: #2b2b2b;
                color: #e8e8e8;
                border: 1px solid #404040;
                border-radius: 4px;
                padding: 5px;
                font-family: "Consolas", "Microsoft YaHei", monospace;
                font-size: 16px;
            }
            QTextEdit:focus {
                border: 1px solid #666666;
            }
        """)
        
        # 创建日志类型过滤复选框
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("日志类型过滤:"))
        
        # 初始化日志类型复选框字典
        self.log_filters = {}
        log_types = ["DEBUG", "INFO", "WARNING", "ERROR", "TRADE"]
        
        # 日志级别颜色映射
        color_map = {
            "DEBUG": "#BB8FCE",    # 浅紫色
            "INFO": "#e8e8e8",     # 白色
            "WARNING": "#FFA500",  # 橙色
            "ERROR": "#FF0000",    # 红色
            "TRADE": "#007acc"     # 蓝色（用于交易信息）
        }
        
        for log_type in log_types:
            checkbox = QCheckBox(log_type)
            checkbox.setChecked(True)  # 默认全部选中
            checkbox.stateChanged.connect(self.on_log_filter_changed)
            
            # 设置复选框文本颜色
            color = color_map.get(log_type, "#e8e8e8")
            checkbox.setStyleSheet(f"QCheckBox {{ color: {color}; background-color: transparent; }}")
            
            self.log_filters[log_type] = checkbox
            filter_layout.addWidget(checkbox)
        
        filter_layout.addStretch()
        
        # 创建按钮布局
        button_layout = QHBoxLayout()
        clear_log_btn = QPushButton("清空日志")
        clear_log_btn.clicked.connect(self.clear_log)
        save_log_btn = QPushButton("保存日志")
        save_log_btn.clicked.connect(self.save_log)
        test_log_btn = QPushButton("测试日志")
        test_log_btn.clicked.connect(self.test_log)
        
        button_layout.addWidget(clear_log_btn)
        button_layout.addWidget(save_log_btn)
        button_layout.addWidget(test_log_btn)
        button_layout.addStretch()

        # 将组件添加到日志布局
        log_layout.addWidget(self.log_text)
        log_layout.addLayout(filter_layout)
        log_layout.addLayout(button_layout)
        log_group.setLayout(log_layout)
        
        # 将日志组件添加到右侧布局，并设置为占据所有可用空间
        self.right_layout.addWidget(log_group)
        
        # 初始化最近回测结果目录
        self.last_backtest_dir = None

    def select_strategy_file(self):
        """选择策略文件"""
        # 获取上一次使用的策略文件路径，如果没有则使用用户策略目录
        last_strategy_path = self.settings.value('last_strategy_path', '')
        if last_strategy_path and os.path.exists(os.path.dirname(last_strategy_path)):
            default_dir = os.path.dirname(last_strategy_path)
        else:
            # 首先初始化用户策略目录
            default_dir = self.init_user_strategies()
        
        # 从记录的路径开始选择文件
        file_name, _ = QFileDialog.getOpenFileName(
            self, 
            "选择策略文件", 
            default_dir,  # 使用上一次的路径或用户策略目录
            "Python Files (*.py)"
        )
        if file_name:
            self.strategy_path.setText(file_name)
            self.config["strategy_file"] = file_name
            # 保存此次选择的路径
            self.settings.setValue('last_strategy_path', file_name)
            logging.info(f"已选择策略文件: {file_name}")
            
            # 首先检查是否在危险的_internal目录内
            if self.check_file_in_internal_dir(file_name):
                self.show_internal_dir_warning("", file_name)
            else:
                # 如果不在_internal目录，再检查是否在用户策略目录中
                user_strategies_dir = self.init_user_strategies()
                if not file_name.startswith(user_strategies_dir):
                    from PyQt5.QtWidgets import QMessageBox
                    QMessageBox.information(
                        self, 
                        "提示", 
                        f"建议将策略文件放在用户策略目录中：\n{user_strategies_dir}\n\n"
                        "这样可以避免软件升级时策略文件丢失。"
                    )

    def update_config(self):
        """更新配置信息"""
        try:
            # 更新策略文件路径
            self.config["strategy_file"] = self.strategy_path.text()
            
            # 更新回测时间和模式
            self.config["backtest"]["start_time"] = self.start_date.date().toString("yyyyMMdd")
            self.config["backtest"]["end_time"] = self.end_date.date().toString("yyyyMMdd")
            
            # 运行模式固定为回测
            self.config["run_mode"] = "backtest"
            
            # 更新账户设置 - 从设置中读取
            if "account" not in self.config:
                self.config["account"] = {}
            self.config["account"]["account_id"] = self.settings.value('account_id', '8888888888')
            self.config["account"]["account_type"] = self.settings.value('account_type', 'STOCK')
            
            # 更新初始资金和最小交易量 - 从虚拟账户设置中获取
            initial_capital = float(self.initial_cash.text())
            min_volume = int(self.min_volume.text())
            self.config["backtest"]["init_capital"] = initial_capital
            self.config["backtest"]["min_volume"] = min_volume
            
            # 更新基准合约
            self.config["backtest"]["benchmark"] = self.benchmark_input.text()
            
            # 更新交易成本设置
            self.config["backtest"]["trade_cost"] = {
                "min_commission": float(self.min_commission.text()),  # 最低佣金
                "commission_rate": float(self.commission_rate.text()),  # 佣金比例
                "stamp_tax_rate": float(self.stamp_tax.text()),  # 卖出印花税
                "flow_fee": float(self.flow_fee.text()),  # 流量费
                "slippage": {
                    "type": "tick" if self.slippage_type.currentText() == "按最小变动价跳数" else "ratio",  # 滑点类型
                    "tick_size": 0.01,  # A股最小变动价（1分钱）
                    "tick_count": int(float(self.slippage_value.text())) if self.slippage_type.currentText() == "按最小变动价跳数" else 2,  # 跳数
                    "ratio": float(self.slippage_value.text()) / 100 if self.slippage_type.currentText() == "按成交金额比例" else 0.001  # 滑点比例
                }
            }
            
            # 更新触发方式配置
            trigger_type_map = {
                0: "tick",  # Tick触发
                1: "1m",    # 1分钟K线触发
                2: "5m",    # 5分钟K线触发
                3: "1d",    # 日K线触发
                4: "custom" # 自定义定时触发
            }
            
            # 添加实盘数据获取模式配置
            self.config["data_mode"] = self.get_realtime_data_mode()
            
            self.config["backtest"]["trigger"] = {
                "type": trigger_type_map[self.trigger_type_combo.currentIndex()],
                "custom_times": self.get_custom_time_points(),
                "start_time": self.start_time_edit.time().toString("HH:mm:ss"),
                "end_time": self.end_time_edit.time().toString("HH:mm:ss"),
                "interval": self.interval_spin.value()
            }
            
            # 更新数据相关的配置
            if "data" not in self.config:
                self.config["data"] = {}
            
            # 直接使用 period_selector 的值，因为它已经是正确的格式
            self.config["data"]["kline_period"] = self.period_selector.currentText()
            
            # 更新复权方式
            adjust_map = {
                "不复权": "none",
                "前复权": "front",
                "后复权": "back",
                "等比前复权": "front_ratio",
                "等比后复权": "back_ratio"
            }
            self.config["data"]["dividend_type"] = adjust_map[self.adjust_selector.currentText()]
            
            # 更新选中的字段
            selected_fields = []
            for field_code, cb in self.fields_checkboxes.items():
                if cb.isChecked():
                    selected_fields.append(field_code)
            self.config["data"]["fields"] = selected_fields
            
            # 更新盘前盘后回调设置
            if "market_callback" not in self.config:
                self.config["market_callback"] = {}
            self.config["market_callback"]["pre_market_enabled"] = self.pre_trigger_checkbox.isChecked()
            self.config["market_callback"]["pre_market_time"] = self.pre_trigger_time.time().toString("HH:mm:ss")
            self.config["market_callback"]["post_market_enabled"] = self.post_trigger_checkbox.isChecked()
            self.config["market_callback"]["post_market_time"] = self.post_trigger_time.time().toString("HH:mm:ss")
            
            # 更新QMT路径
            if "system" not in self.config:
                self.config["system"] = {}
            self.config["system"]["userdata_path"] = self.settings.value('qmt_path', 'D:\\国金证券QMT交易端\\userdata_mini')
            
            # 更新股票池配置
            stock_codes = []
            
            # 添加选中的常用股票池中的股票代码
            for code, cb in self.pool_checkboxes.items():
                if cb.isChecked():
                    pool_file = self._get_pool_file(code)
                    if pool_file:
                        file_path = self.get_data_path(pool_file)
                        if os.path.exists(file_path):
                            with open(file_path, 'r', encoding='utf-8') as f:
                                for line in f:
                                    if line.strip():
                                        parts = line.strip().split(',')
                                        if len(parts) >= 1:
                                            stock_code = parts[0].strip().replace('\ufeff', '')
                                            if stock_code not in stock_codes:
                                                stock_codes.append(stock_code)
            
            # 添加自定义股票列表中的股票代码
            for row in range(self.stock_list.rowCount()):
                code = self.stock_list.item(row, 0).text()
                if code and code not in stock_codes:
                    stock_codes.append(code)

            # 将股票列表直接保存到配置文件中，不再生成单独的csv文件
            self.config["data"]["stock_list"] = stock_codes
            
            # 移除旧的stock_list_file字段（如果存在）
            if "stock_list_file" in self.config["data"]:
                del self.config["data"]["stock_list_file"]
                
            self.log_message(f"股票列表已更新到配置文件，共 {len(stock_codes)} 支股票", "INFO")
            
            # 更新初始化行情数据设置 - 这个设置只存在于QSettings中，不保存到配置文件
            # 记录日志，显示当前的设置值
            init_data_enabled = self.settings.value('init_data_enabled', False, type=bool)
            logging.info(f"从设置界面读取到 init_data_enabled = {init_data_enabled}")
            self.log_message(f"数据初始化设置: {'启用' if init_data_enabled else '禁用'}", "INFO")
            
            # 更新盘前盘后触发设置
            self.config["backtest"]["pre_trigger"] = {
                "enabled": self.pre_trigger_checkbox.isChecked(),
                "time": self.pre_trigger_time.time().toString("HH:mm:ss")
            }
            self.config["backtest"]["post_trigger"] = {
                "enabled": self.post_trigger_checkbox.isChecked(),
                "time": self.post_trigger_time.time().toString("HH:mm:ss")
            }
            
            logging.info("配置信息已更新")
            
        except Exception as e:
            QMessageBox.warning(self, "错误", f"更新配置时出错: {str(e)}")

    def set_t0_mode_display(self, enabled: bool):
        """跨线程安全地更新T+0模式显示"""
        if QThread.currentThread() != self.thread():
            QMetaObject.invokeMethod(
                self,
                "_apply_t0_mode_display",
                Qt.QueuedConnection,
                Q_ARG(bool, enabled)
            )
            return
        self._apply_t0_mode_display(enabled)

    @pyqtSlot(bool)
    def _apply_t0_mode_display(self, enabled: bool):
        """真正执行界面更新的槽函数"""
        if not hasattr(self, 'stock_pool_group'):
            logging.warning("stock_pool_group 属性不存在，跳过T+0模式显示更新")
            return
        try:
            if enabled:
                self.stock_pool_group.setTitle("股票池设置 - T+0交易模式")
                self.stock_pool_group.setStyleSheet("""
                    QGroupBox {
                        background-color: #332010;
                        border: 2px solid #5a3d1f;
                        border-radius: 5px;
                        margin-top: 10px;
                        font-weight: bold;
                    }
                    QGroupBox::title {
                        subcontrol-origin: margin;
                        left: 10px;
                        padding: 0 5px;
                        color: #ffa726;
                        background-color: #332010;
                    }
                """)
            else:
                default_title = getattr(self, 'stock_pool_group_default_title', '股票池设置')
                self.stock_pool_group.setTitle(default_title)
                self.stock_pool_group.setStyleSheet("")
        except Exception as e:
            logging.error(f"设置T+0模式显示时出错: {str(e)}")
    
    def show_t0_warning(self, message: str):
        """显示T+0模式混合池警告弹窗
        
        Args:
            message: 警告信息
        """
        if QThread.currentThread() != self.thread():
            QMetaObject.invokeMethod(
                self,
                "_show_t0_warning",
                Qt.QueuedConnection,
                Q_ARG(str, message)
            )
            return
        self._show_t0_warning(message)

    @pyqtSlot(str)
    def _show_t0_warning(self, message: str):
        """在GUI线程中显示T+0提示"""
        try:
            QMessageBox.warning(
                self,
                "T+0模式提醒",
                message,
                QMessageBox.Ok
            )
        except Exception as e:
            logging.error(f"显示T+0警告弹窗时出错: {str(e)}")

    def update_status(self, message):
        """更新状态栏信息"""
        try:
            # 记录状态消息到日志
            if message:
                logging.info(message)
        except Exception as e:
            print(f"状态栏更新失败: {message}")  # 错误时至少输出到控制台

    def start_strategy(self):
        try:
            self.log_message("开始启动策略...", "INFO")
            
            # 创建交易回调实例
            self.trader_callback = MyTraderCallback(self)
            
            # 更新并保存配置到临时文件
            try:
                self.update_config()
                
                # 确保配置目录存在
                config_dir = os.path.join(os.path.dirname(__file__), "configs")
                os.makedirs(config_dir, exist_ok=True)
                
                # 创建临时配置文件，使用固定名称而不是时间戳
                # 这样每次都会覆盖之前的临时文件，避免产生大量临时文件
                self.temp_config_path = os.path.join(config_dir, "temp_running_config.kh")
                
                # 删除可能存在的旧临时文件
                if os.path.exists(self.temp_config_path):
                    try:
                        os.remove(self.temp_config_path)
                    except Exception as e:
                        self.log_message(f"删除旧临时配置文件失败: {str(e)}", "WARNING")
                
                # 保存配置到临时文件
                with open(self.temp_config_path, "w", encoding="utf-8") as f:
                    json.dump(self.config, f, indent=4, ensure_ascii=False)
                
            except Exception as e:
                self.log_error("保存配置文件失败", e)
                return
            
            # 创建并启动策略线程
            self.strategy_thread = StrategyThread(
                self.temp_config_path,
                self.config["strategy_file"],
                self.trader_callback
            )
            
            # 注册元类型
            from PyQt5.QtGui import QTextCursor
            from PyQt5.QtCore import QMetaType
            QMetaType.type("QTextCursor")
            
            # 连接信号（使用QueuedConnection确保跨线程调用不阻塞GUI）
            self.strategy_thread.error_signal.connect(self.on_strategy_error, Qt.QueuedConnection)
            self.strategy_thread.status_signal.connect(self.update_status, Qt.QueuedConnection)
            self.strategy_thread.finished_signal.connect(self.on_strategy_finished, Qt.QueuedConnection)
            
            # 启动线程
            self.strategy_thread.start()  # 正确使用start()启动子线程
            
            # 更新界面状态
            self.start_action.setEnabled(False)
            self.stop_action.setEnabled(True)
            
            # 设置策略运行状态标志
            self.strategy_is_running = True

            # 显示并重置进度条 (只在回测模式下)
            if self.get_run_mode() == "backtest":
                self.progress_bar.setValue(0)
                # 记录回测开始时间
                import time
                self.backtest_start_time = time.time()
                self.progress_text.setText("回测进度: 0%")
                self.progress_container.show()
                # 强制更新UI
                QApplication.processEvents()
                # 更新状态标签
                self.status_label.setText("回测进行中...")
            else:
                self.progress_container.hide()
            
            self.log_message("策略启动完成", "INFO")
            
        except Exception as e:
            self.log_error("策略启动失败", e)
            # 确保在启动失败时重置界面状态
            self.start_action.setEnabled(True)
            self.stop_action.setEnabled(False)
            # 清除策略运行状态标志
            self.strategy_is_running = False

    @pyqtSlot()
    def on_strategy_finished(self):
        """策略完成回调"""
        try:
            self.log_message("策略运行完成", "INFO")
            # 恢复界面状态
            self.start_action.setEnabled(True)
            self.stop_action.setEnabled(False)
            
            # 处理进度条 - 确保设置为100%并更新状态标签
            if self.get_run_mode() == "backtest":
                self.progress_bar.setValue(100)
                self.status_label.setText("回测完成")
                # 延迟隐藏进度条，让用户看到100%完成状态
                QTimer.singleShot(2000, lambda: self.hide_progress())
            else:
                # 非回测模式直接隐藏
                self.hide_progress()
                self.status_label.setText("策略运行完成")
            
            # 如果启用了延迟显示，提示用户正在收集日志
            if self.delay_log_display:
                self.log_message("延迟显示模式已启用，正在收集所有日志，请稍候...", "INFO")
            
            # 延迟处理策略结束逻辑，等待所有后续日志产生
            def finalize_strategy():
                # 检查是否还在等待延迟处理（避免重复处理）
                if not self.strategy_is_running:
                    return
                    
                # 清除策略运行状态标志
                self.strategy_is_running = False

                # 清除回测开始时间记录
                if hasattr(self, 'backtest_start_time'):
                    delattr(self, 'backtest_start_time')
                
                # 如果启用了延迟显示，现在显示所有延迟的日志
                if self.delay_log_display and self.delayed_logs:
                    # 再次延迟一点时间确保所有日志都已收集
                    QTimer.singleShot(200, self.display_delayed_logs)
                
                # 清理临时配置文件
                if hasattr(self, 'temp_config_path') and os.path.exists(self.temp_config_path):
                    try:
                        os.remove(self.temp_config_path)
                    except Exception as e:
                        self.log_message(f"清理临时配置文件失败: {str(e)}", "WARNING")
            
            # 延迟2秒执行最终处理，给策略后续日志留出时间
            QTimer.singleShot(2000, finalize_strategy)
                    
        except Exception as e:
            self.log_error("处理策略完成回调时出错", e)

    def stop_strategy(self):
        """停止策略运行"""
        try:
            # 清除回测开始时间记录
            if hasattr(self, 'backtest_start_time'):
                delattr(self, 'backtest_start_time')
            
            if getattr(self, 'strategy_thread', None) is not None and self.strategy_thread.isRunning():
                # 设置停止标志
                if hasattr(self.strategy_thread, 'framework') and self.strategy_thread.framework:
                    self.strategy_thread.framework.is_running = False

                # 等待线程结束
                self.strategy_thread.stop()
                self.strategy_thread.wait()

                # 清理临时配置文件
                if hasattr(self, 'temp_config_path') and os.path.exists(self.temp_config_path):
                    try:
                        os.remove(self.temp_config_path)
                    except Exception as e:
                        self.log_message(f"清理临时配置文件失败: {str(e)}", "WARNING")

                self.update_status("策略已停止运行")
                self.stop_action.setEnabled(False)
                self.start_action.setEnabled(True)

                # 清除策略运行状态标志
                self.strategy_is_running = False

                # 隐藏进度条
                self.hide_progress()
                
                # 重置T+0模式显示
                self.set_t0_mode_display(False)

                self.log_message("策略已停止", "INFO")

        except Exception as e:
            error_msg = f"停止策略时出错: {str(e)}"
            self.update_status(error_msg)
            logging.error(error_msg, exc_info=True)

    def closeEvent(self, event):
        """窗口关闭事件处理"""
        try:
            # 停止策略线程，如果存在的话
            if self.strategy_thread and self.strategy_thread.is_running:
                reply = QMessageBox.question(
                    self, '关闭确认',
                    "策略正在运行中，确定要关闭吗?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No
                )
                
                if reply == QMessageBox.Yes:
                    self.stop_strategy()
                else:
                    event.ignore()
                    return
            
            # 恢复窗口标题
            self.setWindowTitle("看海量化回测系统")
            
            # 保存窗口状态和位置
            self.settings.setValue("windowState", self.saveState())
            self.settings.setValue("geometry", self.saveGeometry())
            
            # 记录关闭时间
            end_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.log_message(f"软件关闭时间: {end_time}", "INFO")
            
            # 关闭数据管理模块窗口
            if hasattr(self, 'data_viewer_window') and self.data_viewer_window:
                self.data_viewer_window.close()
                self.data_viewer_window = None
                
            if hasattr(self, 'scheduler_window') and self.scheduler_window:
                self.scheduler_window.close()
                self.scheduler_window = None
                
            if hasattr(self, 'csv_manager_window') and self.csv_manager_window:
                self.csv_manager_window.close()
                self.csv_manager_window = None
                
            if hasattr(self, 'history_manager_window') and self.history_manager_window:
                self.history_manager_window.close()
                self.history_manager_window = None
            
            # 停止日志刷新定时器
            if hasattr(self, 'log_flush_timer'):
                self.log_flush_timer.stop()
            
            # 最后一次刷新日志，确保所有日志都写入文件
            self.flush_logs()
            
            # 接受关闭事件
            event.accept()
            
        except Exception as e:
            logging.error(f"程序退出时出错: {str(e)}", exc_info=True)
            # 确保日志写入
            self.flush_logs()
            # 即使出错也接受事件，确保程序能够退出
            event.accept()

    def mode_changed(self):
        """运行模式改变时的处理（固定为回测模式）"""
        # 固定为回测模式，启用所有相关设置
        self.initial_cash.setEnabled(True)
        self.commission_rate.setEnabled(True)
        self.stamp_tax.setEnabled(True)
        self.min_volume.setEnabled(True)
        self.start_date.setEnabled(True)
        self.end_date.setEnabled(True)
        
        # 隐藏实盘数据获取模块
        self.update_realtime_data_group_status()
        
        # 更新状态
        self.update_status("当前模式：回测模式")

    def load_config(self):
        """加载配置
        
        注意：.kh文件本质是JSON格式，仅使用自定义扩展名
        """
        # 获取上一次使用的配置文件路径
        last_config_path = self.settings.value('last_config_path', '')
        if last_config_path and os.path.exists(os.path.dirname(last_config_path)):
            default_dir = os.path.dirname(last_config_path)
        else:
            default_dir = ""
        
        options = QFileDialog.Options()
        file_path, _ = QFileDialog.getOpenFileName(
            self, "加载配置文件", default_dir, "看海配置文件 (*.kh)", options=options
        )
        
        if not file_path:
            return
            
        try:
            # 从JSON文件加载配置
            with open(file_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                
            # 保存到实例变量
            self.config = config
            self.current_config_file = file_path  # 记录当前配置文件路径
            # 保存此次选择的配置文件路径
            self.settings.setValue('last_config_path', file_path)
            
            # 更新UI
            self.update_ui_from_config()
            
            # 更新窗口标题，显示当前配置文件名
            file_name = os.path.basename(file_path)
            self.setWindowTitle(f"看海量化回测系统 - {file_name}")
            
            # 在日志中记录成功加载
            self.log_message(f"配置已从以下位置加载: {file_path}", "INFO")
            
            # 检查加载的配置中是否有文件在危险位置
            strategy_file_path = config.get("strategy_file", "")
            if strategy_file_path:
                self.show_internal_dir_warning(file_path, strategy_file_path)
            
        except Exception as e:
            QMessageBox.critical(self, "加载失败", f"加载配置文件时出错: {str(e)}")

    def update_ui_from_config(self):
        """根据已加载的配置更新UI"""
        if not hasattr(self, 'config') or not self.config:
            return
            
        # 更新策略文件路径
        if "strategy_file" in self.config:
            self.strategy_path.setText(self.config["strategy_file"])
            
        # 运行模式固定为回测，无需更新选择器
        self.config["run_mode"] = "backtest"
            
        # 更新实盘数据获取模式
        if "data_mode" in self.config:
            data_mode = self.config["data_mode"]
            if data_mode == "full_quote":
                self.full_quote_radio.setChecked(True)
            elif data_mode == "single_quote":
                self.single_quote_radio.setChecked(True)
            # 如果是自定义模式(custom)，当触发类型设置为3(自定义)时会自动处理
        
        # 更新回测参数
        if "backtest" in self.config:
            backtest_config = self.config["backtest"]
            if "start_time" in backtest_config:
                self.start_date.setDate(QDate.fromString(str(backtest_config["start_time"]), "yyyyMMdd"))
            if "end_time" in backtest_config:
                self.end_date.setDate(QDate.fromString(str(backtest_config["end_time"]), "yyyyMMdd"))
            if "init_capital" in backtest_config:
                self.initial_cash.setText(str(backtest_config["init_capital"]))
            if "min_volume" in backtest_config:
                self.min_volume.setText(str(backtest_config["min_volume"]))
            if "benchmark" in backtest_config:
                self.benchmark_input.setText(backtest_config["benchmark"])
            
            # 更新触发器设置
            if "trigger" in backtest_config:
                trigger_config = backtest_config["trigger"]
                trigger_type_map = {
                    "tick": 0,
                    "1m": 1,
                    "5m": 2,
                    "1d": 3,
                    "custom": 4
                }
                if "type" in trigger_config:
                    self.trigger_type_combo.setCurrentIndex(trigger_type_map.get(trigger_config["type"], 0))
                    
                # 更新自定义时间点
                if "custom_times" in trigger_config and trigger_config["custom_times"]:
                    time_points_text = "\n".join(trigger_config["custom_times"])
                    self.custom_times_edit.setText(time_points_text)
                
                # 更新触发时间设置
                if "start_time" in trigger_config:
                    self.start_time_edit.setTime(QTime.fromString(trigger_config["start_time"], "HH:mm:ss"))
                if "end_time" in trigger_config:
                    self.end_time_edit.setTime(QTime.fromString(trigger_config["end_time"], "HH:mm:ss"))
                if "interval" in trigger_config:
                    self.interval_spin.setValue(int(trigger_config["interval"]))
        
        # 更新交易成本设置
        if "trade_cost" in backtest_config:
            trade_cost = backtest_config["trade_cost"]
            if "min_commission" in trade_cost:
                self.min_commission.setText(str(trade_cost["min_commission"]))
            if "commission_rate" in trade_cost:
                self.commission_rate.setText(f"{trade_cost['commission_rate']:.7g}")
            if "stamp_tax_rate" in trade_cost:
                self.stamp_tax.setText(f"{trade_cost['stamp_tax_rate']:.7g}")
            if "flow_fee" in trade_cost:
                self.flow_fee.setText(str(trade_cost["flow_fee"]))
            
            # 添加滑点设置的读取
            if "slippage" in trade_cost:
                slippage = trade_cost["slippage"]
                # 设置滑点类型
                if "type" in slippage:
                    slippage_type = "按最小变动价跳数" if slippage["type"] == "tick" else "按成交金额比例"
                    self.slippage_type.setCurrentText(slippage_type)
                    
                # 设置滑点值
                if slippage["type"] == "tick" and "tick_count" in slippage:
                    self.slippage_value.setText(str(slippage["tick_count"]))
                elif slippage["type"] == "ratio" and "ratio" in slippage:
                    # 比例值需要转换为百分比显示
                    self.slippage_value.setText(str(slippage["ratio"] * 100))
        
        # 更新市场回调设置
        if "market_callback" in self.config:
            market_callback = self.config["market_callback"]
            if "pre_market_enabled" in market_callback:
                self.pre_trigger_checkbox.setChecked(market_callback["pre_market_enabled"])
            if "pre_market_time" in market_callback:
                self.pre_trigger_time.setTime(QTime.fromString(market_callback["pre_market_time"], "HH:mm:ss"))
            if "post_market_enabled" in market_callback:
                self.post_trigger_checkbox.setChecked(market_callback["post_market_enabled"])
            if "post_market_time" in market_callback:
                self.post_trigger_time.setTime(QTime.fromString(market_callback["post_market_time"], "HH:mm:ss"))
                
        # 更新数据设置
        if "data" in self.config:
            data_config = self.config["data"]
            if "kline_period" in data_config:
                self.period_selector.setCurrentText(data_config["kline_period"])
            if "dividend_type" in data_config:
                dividend_map = {
                    "none": "不复权",
                    "front": "前复权",
                    "back": "后复权",
                    "front_ratio": "等比前复权",
                    "back_ratio": "等比后复权"
                }
                self.adjust_selector.setCurrentText(dividend_map.get(data_config["dividend_type"], "前复权"))
            if "fields" in data_config:
                for field, cb in self.fields_checkboxes.items():
                    cb.setChecked(field in data_config["fields"])
            # 优先从stock_list加载，兼容stock_list_file
            if "stock_list" in data_config:
                self.load_stock_list_from_config(data_config["stock_list"])
            elif "stock_list_file" in data_config:
                # 兼容性处理：从旧的股票列表文件加载
                try:
                    if os.path.exists(data_config["stock_list_file"]):
                        with open(data_config["stock_list_file"], 'r', encoding='utf-8') as f:
                            stock_codes = [line.strip() for line in f if line.strip()]
                        self.load_stock_list_from_config(stock_codes)
                except Exception as e:
                    self.log_message(f"加载兼容性股票列表文件失败: {str(e)}", "WARNING")
                
        # 更新实盘数据获取模式 - 放在触发类型设置之后，因为需要根据触发类型调整界面
        if "data_mode" in self.config:
            data_mode = self.config["data_mode"]
            # 根据数据模式设置相应的UI元素
            if data_mode == "custom":
                # 自定义模式不需要设置单选按钮
                pass
            elif data_mode == "full_quote":
                self.full_quote_radio.setChecked(True)
            else:  # single_quote
                self.single_quote_radio.setChecked(True)
        
        # 处理系统设置 - init_data_enabled不存在于配置文件中，只存在于QSettings
        # 移除相关处理代码，因为init_data_enabled只通过设置界面管理
        
        # 更新实盘数据获取模块的状态 - 需要在运行模式和触发类型都设置好后调用
        self.update_realtime_data_group_status()

    def update_account_display(self, account_info):
        """更新账户信息显示"""
        self.account_id_label.setText(f"账户号: {account_info['account_id']}")
        self.account_type_label.setText(f"账户类型: {account_info['account_type']}")
        self.total_asset_label.setText(f"总资产: {account_info['total_asset']:.2f}")
        self.available_cash_label.setText(f"可用资金: {account_info['cash']:.2f}")
        self.market_value_label.setText(f"持仓市值: {account_info['market_value']:.2f}")

    def on_start_date_changed(self, date):
        """开始日期变化时更新结束日期的最小值"""
        self.end_date.setMinimumDate(date)

    def add_single_stock(self):
        """手动添加单只股票"""
        try:
            # 弹出输入对话框
            stock_code, ok = QInputDialog.getText(
                self, 
                "添加股票", 
                "请输入股票代码（例如：000001.SZ 或 600000.SH）:",
                text=""
            )
            
            if ok and stock_code.strip():
                code = stock_code.strip().upper()
                
                # 简单的股票代码格式验证
                if not self.validate_stock_code(code):
                    QMessageBox.warning(self, "格式错误", 
                        "股票代码格式不正确！\n"
                        "请使用以下格式：\n"
                        "• 000001.SZ（深圳）\n"
                        "• 600000.SH（上海）\n"
                        "• 002001.SZ（深圳中小板）\n"
                        "• 300001.SZ（创业板）\n"
                        "• 688001.SH（科创板）")
                    return
                
                # 检查是否已存在
                for row in range(self.stock_list.rowCount()):
                    if self.stock_list.item(row, 0).text() == code:
                        QMessageBox.information(self, "提示", f"股票 {code} 已存在于列表中")
                        return
                
                # 获取股票名称（尝试从系统数据文件中查找）
                name = ""
                try:
                    # 尝试从全部股票列表文件中获取股票名称
                    all_stocks_file = self.get_data_path("全部股票_股票列表.csv")
                    if os.path.exists(all_stocks_file):
                        with open(all_stocks_file, 'r', encoding='utf-8-sig') as f:
                            for line in f:
                                if line.strip():
                                    parts = line.strip().split(',')
                                    if len(parts) >= 2 and parts[0].strip() == code:
                                        name = parts[1].strip()
                                        break
                except Exception:
                    pass
                
                # 如果没有找到名称，让用户输入
                if not name:
                    input_name, ok_name = QInputDialog.getText(
                        self,
                        "股票名称",
                        f"未找到股票 {code} 的名称，请输入股票名称（可选）:",
                        text=""
                    )
                    if ok_name:
                        name = input_name.strip()
                
                # 添加到表格
                row = self.stock_list.rowCount()
                self.stock_list.insertRow(row)
                self.stock_list.setItem(row, 0, QTableWidgetItem(code))
                self.stock_list.setItem(row, 1, QTableWidgetItem(name))
                
                # 选中新添加的行
                self.stock_list.selectRow(row)
                
                self.update_status(f"已添加股票: {code}")
                
        except Exception as e:
            error_msg = f"添加股票时出错: {str(e)}"
            self.update_status(error_msg)
            logging.error(error_msg)
            QMessageBox.critical(self, "错误", error_msg)

    def validate_stock_code(self, code):
        """验证股票代码格式"""
        import re
        # 股票代码格式：6位数字.交易所代码
        pattern = r'^\d{6}\.(SH|SZ)$'
        return re.match(pattern, code) is not None

    def import_stocks(self):
        """导入股票列表"""
        try:
            # 设置默认目录为data
            default_dir = os.path.join(os.path.dirname(__file__), 'data')
            
            file_name, _ = QFileDialog.getOpenFileName(
                self,
                "选择股票列表文件",
                default_dir,  # 设置默认目录
                "CSV Files (*.csv);;Text Files (*.txt)"
            )
            
            if file_name:
                # 读取文件
                with open(file_name, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                
                # 解析并添加股票
                for line in lines:
                    line = line.strip()
                    if line:
                        parts = line.split(',')
                        code = parts[0].strip()
                        name = parts[1].strip() if len(parts) > 1 else ""
                        
                        # 添加到表格
                        row = self.stock_list.rowCount()
                        self.stock_list.insertRow(row)
                        self.stock_list.setItem(row, 0, QTableWidgetItem(code))
                        self.stock_list.setItem(row, 1, QTableWidgetItem(name))
                
                self.update_status(f"已导入股票列表: {os.path.basename(file_name)}")
                
        except Exception as e:
            error_msg = f"导入股票列表时出错: {str(e)}"
            self.update_status(error_msg)
            logging.error(error_msg)
            QMessageBox.critical(self, "错误", error_msg)

    def delete_selected_stocks(self):
        """删除选中的股票"""
        selected_rows = set(item.row() for item in self.stock_list.selectedItems())
        for row in sorted(selected_rows, reverse=True):
            self.stock_list.removeRow(row)
            
        # 添加以下代码：更新股票清单文件
        # 生成新的股票清单文件
        stock_codes = []
        
        # 添加选中的常用股票池中的股票代码
        for code, cb in self.pool_checkboxes.items():
            if cb.isChecked():
                pool_file = self._get_pool_file(code)
                if pool_file:
                    file_path = self.get_data_path(pool_file)
                    if os.path.exists(file_path):
                        with open(file_path, 'r', encoding='utf-8') as f:
                            for line in f:
                                if line.strip():
                                    parts = line.strip().split(',')
                                    if len(parts) >= 1:
                                        stock_code = parts[0].strip().replace('\ufeff', '')
                                        if stock_code not in stock_codes:
                                            stock_codes.append(stock_code)
        
        # 添加剩余的自定义股票列表中的股票代码
        for row in range(self.stock_list.rowCount()):
            code = self.stock_list.item(row, 0).text()
            if code and code not in stock_codes:
                stock_codes.append(code)
                
        if stock_codes:
            # 生成股票清单文件
            stock_list_dir = os.path.join(os.path.dirname(__file__), 'data', 'stock_list')
            os.makedirs(stock_list_dir, exist_ok=True)
            stock_list_file = os.path.join(stock_list_dir, f"stock_list_{int(time.time())}.csv")
            
            with open(stock_list_file, 'w', encoding='utf-8') as f:
                for code in stock_codes:
                    f.write(f"{code}\n")
            
            # 更新配置
            if "data" not in self.config:
                self.config["data"] = {}
            self.config["data"]["stock_list_file"] = stock_list_file
            
            self.update_status(f"已删除选中股票并更新股票清单，保留{len(stock_codes)}只股票")
        else:
            # 清空股票清单文件路径
            if "data" in self.config:
                self.config["data"]["stock_list_file"] = ""
            self.update_status("已删除所有股票")

    def clear_stock_list(self):
        """清空股票列表和取消所有股票池的勾选"""
        # 清空股票列表
        self.stock_list.setRowCount(0)
        
        # 取消所有股票池的勾选
        for checkbox in self.pool_checkboxes.values():
            checkbox.setChecked(False)
        
        self.update_status("已清空股票列表和股票池选择")

    def on_pool_changed(self, code, state):
        """股票池选择变化时的处理"""
        try:
            if state == Qt.Checked:
                # 获取对应的股票列表文件
                pool_file = self._get_pool_file(code)
                if pool_file:
                    file_path = self.get_data_path(pool_file)
                    if os.path.exists(file_path):
                        # 读取文件中的股票
                        with open(file_path, 'r', encoding='utf-8-sig') as f:  # 使用 utf-8-sig 编码处理BOM
                            added_count = 0
                            for line in f:
                                if line.strip():
                                    parts = line.strip().split(',')
                                    if len(parts) >= 2:  # 确保有代码和名称
                                        stock_code = parts[0].strip().replace('\ufeff', '')  # 移除BOM字符
                                        stock_name = parts[1].strip()
                                        
                                        # 检查是否已存在
                                        exists = False
                                        for row in range(self.stock_list.rowCount()):
                                            if self.stock_list.item(row, 0).text() == stock_code:
                                                exists = True
                                                break
                                        
                                        # 如果不存在则添加
                                        if not exists:
                                            row = self.stock_list.rowCount()
                                            self.stock_list.insertRow(row)
                                            self.stock_list.setItem(row, 0, QTableWidgetItem(stock_code))
                                            self.stock_list.setItem(row, 1, QTableWidgetItem(stock_name))
                                            added_count += 1
                        
                        self.update_status(f"已添加{added_count}只股票")
                    else:
                        self.update_status(f"找不到股票列表文件: {file_path}")
                        logging.warning(f"找不到股票列表文件: {file_path}")
            else:
                # 获取所有选中的股票池中的股票
                stocks_to_keep = set()
                
                # 遍历所有选中的股票池
                for pool_code, checkbox in self.pool_checkboxes.items():
                    if checkbox.isChecked():
                        pool_file = self._get_pool_file(pool_code)
                        if pool_file:
                            file_path = self.get_data_path(pool_file)
                            if os.path.exists(file_path):
                                with open(file_path, 'r', encoding='utf-8') as f:
                                    for line in f:
                                        if line.strip():
                                            parts = line.strip().split(',')
                                            if len(parts) >= 2:
                                                stocks_to_keep.add((parts[0].strip(), parts[1].strip()))
                
                # 清空并重新填充表格
                self.stock_list.setRowCount(0)
                for code, name in sorted(stocks_to_keep):
                    row = self.stock_list.rowCount()
                    self.stock_list.insertRow(row)
                    self.stock_list.setItem(row, 0, QTableWidgetItem(code))
                    self.stock_list.setItem(row, 1, QTableWidgetItem(name))
                
                self.update_status(f"更新后保留{len(stocks_to_keep)}只股票")
                
        except Exception as e:
            error_msg = f"更新股票池时出错: {str(e)}"
            self.update_status(error_msg)
            logging.error(error_msg)

    def _get_pool_file(self, code):
        """获取股票池对应的文件名"""
        if code == "sh.000016":
            return "上证50成分股_股票列表.csv"
        elif code == "sh.000300":
            return "沪深300成分股_股票列表.csv"
        elif code == "sh.000905":
            return "中证500成分股_股票列表.csv"
        elif code == "sz.399006":
            return "创业板_股票列表.csv"
        elif code == "sh.000688":
            return "科创板_股票列表.csv"
        elif code == "all_a":
            return "沪深A股_股票列表.csv"
        elif code == "sci_tech":
            return "科创板_股票列表.csv"
        elif code == "sh_a":
            return "上证A股_股票列表.csv"
        elif code == "custom":
            return "otheridx.csv"  # 添加自选清单文件
        return None

    def open_custom_list(self):
        """打开自选清单文件"""
        try:
            file_path = self.get_data_path("otheridx.csv")
            
            # 如果文件不存在，创建一个示例文件
            if not os.path.exists(file_path):
                # 确保目录存在
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                
                # 创建示例自选清单文件
                sample_content = """股票代码,股票名称
000001.SZ,平安银行
000002.SZ,万科A
600000.SH,浦发银行
600036.SH,招商银行
000858.SZ,五粮液"""
                try:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(sample_content)
                    self.log_message(f"已创建示例自选清单文件: {file_path}", "INFO")
                except Exception as create_error:
                    self.log_message(f"创建自选清单文件失败: {str(create_error)}", "ERROR")
                    return
            
            if os.path.exists(file_path):
                # 使用系统默认程序打开文件
                if sys.platform == 'win32':
                    os.startfile(file_path)
                elif sys.platform == 'darwin':  # macOS
                    subprocess.call(['open', file_path])
                else:  # linux
                    subprocess.call(['xdg-open', file_path])
                self.update_status(f"已打开自选清单文件: {file_path}")
            else:
                self.update_status("找不到自选清单文件")
                
        except Exception as e:
            error_msg = f"打开自选清单文件时出错: {str(e)}"
            self.update_status(error_msg)
            logging.error(error_msg)
            QMessageBox.critical(self, "错误", error_msg)

    def on_period_changed(self, period):
        """周期类型改变时更新可选字段"""
        self.update_fields_list(period)

    def update_fields_list(self, period):
        """更新字段列表"""
        # 清空现有的字段复选框
        for cb in self.fields_checkboxes.values():
            cb.setParent(None)
        self.fields_checkboxes.clear()
        
        # 根据周期类型选择可用字段
        fields = self.tick_fields if period == "tick" else self.kline_fields
        
        # 创建新的字段复选框
        row = 0
        col = 0
        for field_code, field_name in fields.items():
            cb = QCheckBox(field_name)  # 使用中文显示
            cb.setChecked(True)  # 默认全选
            
            self.fields_checkboxes[field_code] = cb  # 使用英文代码作为key
            self.fields_grid.addWidget(cb, row, col)
            
            col += 1
            if col > 2:  # 每行3个复选框
                col = 0
                row += 1

    def update_status_table(self, status_item, status_value):
        """更新状态表格"""
        try:
            if not hasattr(self, 'status_table'):
                return
            
            # 在表格顶部插入新行
            self.status_table.insertRow(0)
            self.status_table.setItem(0, 0, QTableWidgetItem(str(status_item)))
            self.status_table.setItem(0, 1, QTableWidgetItem(str(status_value)))
            
            # 如果行数超过100，删除最后一行
            if self.status_table.rowCount() > 100:
                self.status_table.removeRow(self.status_table.rowCount() - 1)
            
            # 自动滚动到顶部
            self.status_table.scrollToTop()
            
            # 调整列宽以适应内容
            self.status_table.resizeColumnsToContents()
            
        except Exception as e:
            print(f"更新状态表格时出错: {str(e)}")

    def select_qmt_path(self):
        """QMT路径设置已移动到设置界面"""
        pass

    def init_trader_and_account(self):
        """初始化交易接口并获取账户信息（回测模式不需要真实交易接口）"""
        # 回测模式不需要初始化交易接口
        logging.info("回测模式无需初始化交易接口")



    def slippage_type_changed(self, text):
        """滑点类型变更时的处理"""
        try:
            # 清空当前滑点值
            current_value = self.slippage_value.text()
            
            if text == "按最小变动价跳数":
                # 如果切换到跳数模式，设置默认值为2跳
                # 如果当前有比例值，尝试转换为整数跳数
                if current_value and (not current_value.isdigit() or float(current_value) < 1):
                    self.slippage_value.setText("2")  # 默认值
                
                # 设置整数输入验证器
                self.slippage_value.setValidator(QIntValidator(1, 100))
                
                # 设置提示文本
                self.slippage_value.setPlaceholderText("请输入跳数(1-100)")
                self.log_message("已切换到按最小变动价跳数模式，请输入整数跳数", "INFO")
                
            else:  # "按成交金额比例"
                # 如果切换到比例模式，设置默认比例为0.1%
                # 如果当前有整数跳数，尝试保留数值
                if current_value and current_value.isdigit() and int(current_value) > 0:
                    # 如果是较大的数值，可能需要缩小到合理范围
                    if int(current_value) > 20:
                        self.slippage_value.setText("0.1")  # 默认值
                    else:
                        # 否则可以保留数值作为百分比值
                        self.slippage_value.setText(current_value)
                elif not current_value or current_value == "0" or current_value == "0.0":
                    self.slippage_value.setText("0.1")  # 默认值
                
                # 设置浮点数输入验证器，限制在0-10之间，2位小数
                self.slippage_value.setValidator(QDoubleValidator(0.0, 10.0, 2))
                
                # 设置提示文本
                self.slippage_value.setPlaceholderText("请输入比例(0-10)%")
                self.log_message("已切换到按成交金额比例模式，请输入百分比值", "INFO")
                
        except Exception as e:
            self.log_message(f"滑点类型变更处理出错: {str(e)}", "ERROR")

    def update_trade_log(self, trade_info):
        """更新交易日志"""
        try:
            # 格式化交易信息
            direction_map = {
                'STOCK_BUY': '买入',
                'STOCK_SELL': '卖出',
                'FUTURE_OPEN_LONG': '开多',
                'FUTURE_CLOSE_LONG': '平多',
                'FUTURE_OPEN_SHORT': '开空',
                'FUTURE_CLOSE_SHORT': '平空'
            }
            
            status_map = {
                'SUBMITTED': '已提交',
                'ACCEPTED': '已接受',
                'REJECTED': '已拒绝',
                'CANCELLED': '已撤销',
                'FILLED': '已成交',
                'PARTIALLY_FILLED': '部分成交'
            }
            
            # 构建交易日志消息
            message = (
                f"交易信息 - "
                f"代码: {trade_info.get('stock_code', '')} | "
                f"方向: {direction_map.get(trade_info.get('direction', ''), '未知')} | "
                f"价格: {trade_info.get('price', 0):.3f} | "
                f"数量: {trade_info.get('volume', 0)} | "
                f"状态: {status_map.get(trade_info.get('status', ''), '未知')}"
            )
            
            # 添加到日志显示
            self.log_message(message, "TRADE")
            
        except Exception as e:
            self.log_message(f"更新交易日志时出错: {str(e)}", "ERROR")

    def on_stock_order(self, order):
        """委托回报推送回调"""
        trade_info = {
            'time': datetime.now().strftime('%H:%M:%S'),
            'stock_code': order.stock_code,
            'direction': order.direction,
            'price': order.price,
            'volume': order.order_volume,
            'status': order.order_status
        }
        self.update_trade_log(trade_info)

    @pyqtSlot(str, str)
    def _log_message(self, message, level="INFO"):
        """实际的日志处理函数（在GUI线程中执行）"""
        try:
            # 获取当前时间
            current_time = datetime.now().strftime("%H:%M:%S")
            
            # 检查是否是进度消息（仅更新进度条，不显示在日志中）
            # 仅当策略实际运行时才处理进度条相关的日志消息
            if hasattr(self, 'strategy_is_running') and self.strategy_is_running and "进度" in message and "%" in message:
                try:
                    # 尝试提取百分比数值
                    import re
                    progress_matches = re.findall(r'(\d+\.?\d*)%', message)
                    if progress_matches:
                        progress_value = int(float(progress_matches[0]))
                        # 确保值在有效范围内
                        if 0 <= progress_value <= 100:
                            # 发射进度信号
                            self.progress_signal.emit(progress_value)
                            # 确保进度条可见
                            if self.get_run_mode() == "backtest" and not self.progress_container.isVisible():
                                self.progress_container.show()
                    # 直接返回，不将进度消息添加到日志
                    return
                except (IndexError, ValueError):
                    pass
            
            # 根据日志级别设置颜色
            color_map = {
                "DEBUG": "#BB8FCE",    # 浅紫色
                "INFO": "#e8e8e8",     # 白色
                "WARNING": "#FFA500",  # 橙色
                "ERROR": "#FF0000",    # 红色
                "TRADE": "#007acc"     # 蓝色（用于交易信息）
            }
            
            # 获取颜色
            color = color_map.get(level, "#e8e8e8")
            
            # 格式化日志消息
            formatted_message = f'<span style="color: {color}">[{current_time}] [{level}] {message}</span><br>'
            
            # 在终端输出纯文本格式的日志
            print(f"[{current_time}] [{level}] {message}")
            
            # 过滤不需要在界面显示的系统和更新相关的日志
            should_skip_gui_log = False
            system_log_keywords = [
                "主窗口创建成功", 
                "加载进度", 
                "初始化系统", 
                "检查更新", 
                "加载组件", 
                "准备用户界面", 
                "启动完成",
                "启动画面",
                "软件更新",
                "服务器",
                "版本",
                "HTTP",
                "当前已是最新版本",
                "QSettings",
                "Unknown property cursor",
                "状态指示器状态更新",
                "更新检查完成",
                "libpng warning",
                "iCCP",
                "开始解析文件名",
                "文件名解析结果",
                "update_chart called with args",
                "findfont: score",
                "findfont:",
                "matplotlib",
                "FontProperties",
                "font_manager",
                "Folio Lt BT",
                "Bodoni MT",
                "Snap ITC",
                "High Tower Text",
                ".ttf"
            ]
            
            # 特例：允许"软件准备就绪"消息显示在GUI上
            if message == "软件准备就绪":
                should_skip_gui_log = False
            else:
                for keyword in system_log_keywords:
                    if keyword in message:
                        should_skip_gui_log = True
                        break
            
            # 如果是需要跳过的日志，只存储但不显示在GUI上
            if not should_skip_gui_log:
                # 存储日志条目
                log_entry = {
                    'time': current_time,
                    'level': level,
                    'message': message,
                    'formatted': formatted_message
                }
                self.log_entries.append(log_entry)
                
                # 如果启用了延迟显示模式且策略正在运行，则添加到延迟日志队列
                if hasattr(self, 'delay_log_display') and self.delay_log_display and hasattr(self, 'strategy_is_running') and self.strategy_is_running:
                    self.delayed_logs.append(log_entry)
                    return  # 不立即显示
                
                # 检查是否应该显示这条日志（根据过滤器设置）
                if hasattr(self, 'log_filters') and level in self.log_filters and self.log_filters[level].isChecked():
                    # 添加到文本框
                    self.log_text.moveCursor(self.log_text.textCursor().End)
                    self.log_text.insertHtml(formatted_message)

                    # 检查并限制日志行数
                    self._trim_log_lines()

                    # 滚动到底部
                    self.log_text.verticalScrollBar().setValue(
                        self.log_text.verticalScrollBar().maximum()
                    )
            else:
                # 即使是被跳过的系统日志，如果启用了延迟显示模式且策略正在运行，也要保存
                if hasattr(self, 'delay_log_display') and self.delay_log_display and hasattr(self, 'strategy_is_running') and self.strategy_is_running:
                    log_entry = {
                        'time': current_time,
                        'level': level,
                        'message': message,
                        'formatted': formatted_message
                    }
                    self.delayed_logs.append(log_entry)
                
        except Exception as e:
            print(f"记录日志时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())

    def _trim_log_lines(self):
        """限制日志显示行数，删除最旧的日志行（批量处理以提高性能）"""
        try:
            # 获取最大行数设置
            max_lines = getattr(self, 'max_log_lines', 1000)

            # 使用计数器，避免每次日志都进行检查
            # 每100条日志检查一次，减少性能开销
            if not hasattr(self, '_log_trim_counter'):
                self._log_trim_counter = 0
            self._log_trim_counter += 1

            # 每100条日志才检查一次（或者在第一条日志时检查）
            if self._log_trim_counter < 100 and self._log_trim_counter > 1:
                return
            self._log_trim_counter = 0

            # 获取当前文档的行数
            document = self.log_text.document()
            current_lines = document.blockCount()

            # 设置缓冲区，超过最大行数+缓冲区时才删除
            buffer_lines = 100
            if current_lines > max_lines + buffer_lines:
                # 一次性删除多余的行（包括缓冲区），避免频繁删除
                lines_to_remove = current_lines - max_lines

                # 获取文档游标
                cursor = QTextCursor(document)
                cursor.movePosition(QTextCursor.Start)

                # 选择需要删除的行
                for _ in range(lines_to_remove):
                    cursor.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor)
                cursor.movePosition(QTextCursor.StartOfLine, QTextCursor.KeepAnchor)

                # 删除选中的内容
                cursor.removeSelectedText()

                # 同时限制内存中的日志条目
                if len(self.log_entries) > max_lines:
                    self.log_entries = self.log_entries[-max_lines:]
        except Exception as e:
            # 静默处理错误，避免影响正常日志显示
            pass

    def clear_log(self):
        """清空日志"""
        self.log_text.clear()
        self.log_entries = []
        self.log_message("日志已清空", "INFO")

    def save_log(self):
        """保存日志到文件"""
        try:
            # 选择保存路径
            file_name, _ = QFileDialog.getSaveFileName(
                self,
                "保存日志",
                os.path.join(os.path.dirname(__file__), "logs", f"log_{int(time.time())}.txt"),
                "Text Files (*.txt);;All Files (*)"
            )
            
            if file_name:
                # 获取纯文本内容
                log_content = self.log_text.toPlainText()
                
                # 保存到文件
                with open(file_name, 'w', encoding='utf-8') as f:
                    f.write(log_content)
                
                self.log_message(f"日志已保存到: {file_name}", "INFO")
                
        except Exception as e:
            self.log_message(f"保存日志失败: {str(e)}", "ERROR")

    def show_error_dialog(self, title, message, details=None):
        """显示错误弹窗"""
        msg_box = QMessageBox(self)
        msg_box.setIcon(QMessageBox.Critical)
        msg_box.setWindowTitle(title)
        msg_box.setText(message)
        
        if details:
            msg_box.setDetailedText(details)
        
        # 设置弹窗样式
        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #2b2b2b;
                color: #e8e8e8;
            }
            QMessageBox QLabel {
                color: #e8e8e8;
            }
            QPushButton {
                background-color: #505050;
                border: none;
                border-radius: 4px;
                padding: 5px 15px;
                color: #e8e8e8;
                min-width: 80px;
            }
            QPushButton:hover {
                background-color: #606060;
            }
            QPushButton:pressed {
                background-color: #404040;
            }
            QTextEdit {
                background-color: #333333;
                color: #e8e8e8;
                border: 1px solid #404040;
            }
        """)
        
        msg_box.exec_()

    def on_strategy_error(self, error_msg, error):
        """策略错误处理"""
        try:
            self.log_error(error_msg, error)
            # 恢复界面状态
            self.start_action.setEnabled(True)
            self.stop_action.setEnabled(False)
            
            # 隐藏进度条
            self.hide_progress()
            self.status_label.setText("策略出错")
            
            # 延迟处理策略结束逻辑，即使出错也要等待可能的清理日志
            def finalize_strategy_error():
                # 检查是否还在等待延迟处理
                if not self.strategy_is_running:
                    return
                    
                # 清除策略运行状态标志
                self.strategy_is_running = False
                
                # 如果启用了延迟显示，显示延迟的日志
                if self.delay_log_display and self.delayed_logs:
                    QTimer.singleShot(200, self.display_delayed_logs)
                
                # 清理临时配置文件
                if hasattr(self, 'temp_config_path') and os.path.exists(self.temp_config_path):
                    try:
                        os.remove(self.temp_config_path)
                    except Exception as e:
                        self.log_message(f"清理临时配置文件失败: {str(e)}", "WARNING")
            
            # 延迟1秒执行，出错时的后续日志通常较少
            QTimer.singleShot(1000, finalize_strategy_error)
                    
        except Exception as e:
            self.log_error("处理策略错误回调时出错", e)

    @pyqtSlot("PyQt_PyObject")
    def invoke(self, func):
        """在主线程中调用指定函数（用于跨线程安全调用）"""
        try:
            func()
        except Exception as e:
            self.log_message(f"执行跨线程调用时出错: {str(e)}", "ERROR")

    @pyqtSlot(str, str)
    def _update_status_table(self, time_str, content):
        """实际的状态表更新函数（在GUI线程中执行）"""
        try:
            if not hasattr(self, 'status_table'):
                return
            
            # 在表格顶部插入新行
            self.status_table.insertRow(0)
            self.status_table.setItem(0, 0, QTableWidgetItem(str(time_str)))
            self.status_table.setItem(0, 1, QTableWidgetItem(str(content)))
            
            # 如果行数超过100，删除最后一行
            if self.status_table.rowCount() > 100:
                self.status_table.removeRow(self.status_table.rowCount() - 1)
            
            # 自动滚动到顶部
            self.status_table.scrollToTop()
            
            # 调整列宽以适应内容
            self.status_table.resizeColumnsToContents()
            
        except Exception as e:
            print(f"更新状态表格时出错: {str(e)}")

    def update_status_table(self, time_str, content):
        """发送状态更新信号"""
        self.update_status_signal.emit(time_str, content)

    def test_log(self):
        """测试日志系统"""
        # 测试不同级别的日志
        self.log_message("这是一条测试信息", "INFO")
        self.log_message("这是一条调试信息", "DEBUG")
        self.log_message("这是一条警告信息", "WARNING")
        self.log_message("这是一条错误信息", "ERROR")
        
        # 测试交易信息
        test_trade_info = {
            'stock_code': 'sz.000001',
            'direction': 'STOCK_BUY',
            'price': 10.5,
            'volume': 100,
            'status': 'FILLED'
        }
        self.update_trade_log(test_trade_info)
        
        # 测试状态更新
        self.update_status("测试状态更新")
        
        # 测试延迟显示功能
        self.log_message(f"当前延迟显示状态: {'启用' if self.delay_log_display else '禁用'}", "INFO")
        
        if self.delay_log_display:
            self.log_message("开始测试延迟显示功能", "INFO")
            self.log_message("注意：接下来的模拟日志将被延迟显示，不会立即出现在日志窗口中", "WARNING")
            
            # 模拟策略运行状态
            original_state = self.strategy_is_running
            self.strategy_is_running = True
            
            # 清空之前的延迟日志
            self.delayed_logs.clear()
            
            # 发送一些测试日志，模拟策略运行中的日志
            self.log_message("模拟策略运行日志1 - 数据加载完成", "INFO")
            self.log_message("模拟策略运行日志2 - 开始处理股票数据", "INFO")
            self.log_message("模拟策略运行日志3 - 发现交易信号", "WARNING")
            self.log_message("模拟策略运行日志4 - 执行交易指令", "INFO")
            self.log_message("模拟策略运行日志5 - 交易完成", "INFO")
            
            # 模拟策略完成后的统计日志
            def simulate_post_strategy_logs():
                self.log_message("模拟回测统计 - 总收益率: +15.23%", "INFO")
                self.log_message("模拟回测统计 - 最大回撤: -3.45%", "INFO")
                self.log_message("模拟回测统计 - 交易次数: 25次", "INFO")
                
                # 恢复原始状态并显示延迟日志
                self.strategy_is_running = original_state
                if self.delayed_logs:
                    self.log_message(f"测试完成，收集到{len(self.delayed_logs)}条延迟日志，将在2秒后显示", "INFO")
                    QTimer.singleShot(2000, self.display_delayed_logs)
                else:
                    self.log_message("测试完成，但没有收集到延迟日志", "WARNING")
            
            # 延迟1秒模拟策略后续处理
            QTimer.singleShot(1000, simulate_post_strategy_logs)
        else:
            self.log_message("延迟显示功能未启用", "WARNING")
            self.log_message("如需测试延迟显示功能，请先在设置中启用'延迟显示日志'选项", "INFO")
        
        # 测试HTML格式
        current_time = datetime.now().strftime("%H:%M:%S")
        self.log_text.moveCursor(self.log_text.textCursor().End)

    @pyqtSlot(str)
    def show_backtest_result(self, backtest_dir):
        """显示回测结果窗口"""
        try:
            from backtest_result_window import BacktestResultWindow
            # 记录最近的回测目录
            self.last_backtest_dir = backtest_dir
            # 确保窗口在主线程创建
            self.result_window = BacktestResultWindow(backtest_dir)
            
            # 先显示窗口，让Qt完成窗口的初始化
            self.result_window.show()
            # 强制处理事件队列，确保窗口完全初始化
            QApplication.processEvents()
            
            # 获取屏幕和窗口的实际大小，然后居中
            screen = QDesktopWidget().availableGeometry()
            window_geometry = self.result_window.frameGeometry()
            x = (screen.width() - window_geometry.width()) // 2
            y = (screen.height() - window_geometry.height()) // 2
            self.result_window.move(x, y)
            
            self.log_message("回测结果窗口已打开", "INFO")
        except Exception as e:
            self.log_message(f"显示回测结果窗口时出错: {str(e)}", "ERROR")
            import traceback
            self.log_message(traceback.format_exc(), "ERROR")

    def open_backtest_result(self):
        """重新打开回测指标窗口"""
        if self.last_backtest_dir and os.path.exists(self.last_backtest_dir):
            self.show_backtest_result(self.last_backtest_dir)
        else:
            self.log_message("没有找到最近的回测结果", "WARNING")

    def show_loading(self, message):
        self.loading_dialog = QProgressDialog(message, None, 0, 0, self)
        self.loading_dialog.setWindowModality(Qt.WindowModal)
        self.loading_dialog.setCancelButton(None)
        self.loading_dialog.show()

    def hide_loading(self):
        if self.loading_dialog:
            self.loading_dialog.close()

    def trigger_type_changed(self, index):
        """处理触发类型变更"""
        # 设置堆叠小部件的当前页面
        self.trigger_stack.setCurrentIndex(index)
        
        # 更新实盘数据获取模块的内容
        if index == 0:  # Tick触发
            self.full_quote_radio.show()
            self.single_quote_radio.show()
            self.custom_data_label.hide()
        elif index == 1 or index == 2 or index == 3:  # 1分钟、5分钟或日K线触发
            self.full_quote_radio.hide()
            self.single_quote_radio.show()
            self.single_quote_radio.setChecked(True)
            self.custom_data_label.hide()
        elif index == 4:  # 自定义定时触发
            self.full_quote_radio.hide()
            self.single_quote_radio.hide()
            self.custom_data_label.show()
        
        # 检查是否需要启用实盘数据获取模块
        self.update_realtime_data_group_status()

    def is_in_trading_hours(self, seconds):
        """检查时间是否在交易时段内"""
        # A股交易时段：
        # 上午：9:30-11:30 (34200-41400秒)
        # 下午：13:00-15:00 (46800-54000秒)
        
        # 上午交易时段：9:30-11:30
        morning_start = 9 * 3600 + 30 * 60  # 9:30
        morning_end = 11 * 3600 + 30 * 60   # 11:30
        
        # 下午交易时段：13:00-15:00
        afternoon_start = 13 * 3600  # 13:00
        afternoon_end = 15 * 3600    # 15:00
        
        return (morning_start <= seconds <= morning_end) or (afternoon_start <= seconds <= afternoon_end)
    
    def generate_time_points(self):
        """生成符合条件的时间点列表"""
        # 获取开始和结束时间
        start_time = self.start_time_edit.time()
        end_time = self.end_time_edit.time()
        
        # 转换为秒数
        start_seconds = start_time.hour() * 3600 + start_time.minute() * 60 + start_time.second()
        end_seconds = end_time.hour() * 3600 + end_time.minute() * 60 + end_time.second()
        
        if start_seconds >= end_seconds:
            QMessageBox.warning(self, "生成失败", "结束时间必须晚于开始时间")
            return
        
        # 获取间隔，从spin控件获取
        interval = self.interval_spin.value()
        
        # 确保间隔是3的整数倍
        if interval < 3:
            interval = 3
            self.interval_spin.setValue(3)
        elif interval % 3 != 0:
            interval = (interval // 3) * 3
            self.interval_spin.setValue(interval)
        
        # 生成时间点前先清空文本编辑框
        self.custom_times_edit.clear()
        
        # 默认使用均匀分布
        generator_type = "均匀分布"
        if hasattr(self, 'generator_type_combo'):
            generator_type = self.generator_type_combo.currentText()
        
        time_points_text = ""
        total_generated = 0
        valid_points = 0
        
        if generator_type == "均匀分布" or generator_type == "自定义间隔":
            # 均匀分布或自定义间隔的时间点
            current_seconds = start_seconds
            while current_seconds <= end_seconds:
                total_generated += 1
                # 检查是否在交易时段内
                if self.is_in_trading_hours(current_seconds):
                    time_points_text += self.seconds_to_time(current_seconds) + "\n"
                    valid_points += 1
                current_seconds += interval
        
        elif generator_type == "整点分布":
            # 每小时的整点
            hour_start = start_time.hour()
            hour_end = end_time.hour()
            if end_time.minute() > 0 or end_time.second() > 0:
                hour_end += 1
                
            for hour in range(hour_start, hour_end + 1):
                hour_seconds = hour * 3600
                if start_seconds <= hour_seconds <= end_seconds:
                    total_generated += 1
                    # 检查是否在交易时段内
                    if self.is_in_trading_hours(hour_seconds):
                        time_points_text += f"{hour:02d}:00:00\n"
                        valid_points += 1
        
        # 设置生成的时间点到文本编辑框
        self.custom_times_edit.setText(time_points_text.strip())
        
        # 显示生成结果
        if total_generated == 0:
            QMessageBox.information(self, "生成完成", "未生成任何时间点")
        elif valid_points == 0:
            QMessageBox.warning(self, "生成警告", 
                              f"共生成{total_generated}个时间点，但均不在交易时段内（9:30-11:30, 13:00-15:00）\n"
                              f"请调整时间范围或间隔设置")
        elif valid_points < total_generated:
            QMessageBox.information(self, "生成完成", 
                                  f"共生成{total_generated}个时间点，其中{valid_points}个在交易时段内\n"
                                  f"已自动过滤掉{total_generated - valid_points}个非交易时段的时间点")
        else:
            QMessageBox.information(self, "生成成功", f"已生成{valid_points}个时间点，均在交易时段内")

    def get_custom_time_points(self):
        """从文本编辑框获取时间点列表"""
        text = self.custom_times_edit.toPlainText().strip()
        if not text:
            return []
        return [line.strip() for line in text.split('\n') if line.strip()]

    def get_run_mode(self):
        """获取当前运行模式（固定为回测）"""
        return "backtest"
        
    def get_slippage_settings(self):
        """获取滑点设置"""
        slippage_type = "tick" if self.slippage_type.currentText() == "按最小变动价跳数" else "ratio"
        
        if slippage_type == "tick":
            return {
                "type": "tick",
                "tick_size": 0.01,  # A股最小变动价（1分钱）
                "tick_count": int(float(self.slippage_value.text())),
                "ratio": 0.001
            }
        else:
            return {
                "type": "ratio",
                "tick_size": 0.01,
                "tick_count": 2,
                "ratio": float(self.slippage_value.text()) / 100  # 转换为小数
            }
    
    def get_dividend_type(self):
        """获取复权类型"""
        adjust_map = {
            "不复权": "none",
            "前复权": "front",
            "后复权": "back",
            "等比前复权": "front_ratio",
            "等比后复权": "back_ratio"
        }
        return adjust_map[self.adjust_selector.currentText()]
    
    def get_selected_fields(self):
        """获取选中的数据字段"""
        selected_fields = []
        for field_code, cb in self.fields_checkboxes.items():
            if cb.isChecked():
                selected_fields.append(field_code)
        return selected_fields
    
    def get_stock_list(self):
        """获取当前股票列表"""
        stock_codes = []
        
        # 添加选中的常用股票池中的股票代码
        for code, cb in self.pool_checkboxes.items():
            if cb.isChecked():
                pool_file = self._get_pool_file(code)
                if pool_file:
                    file_path = self.get_data_path(pool_file)
                    if os.path.exists(file_path):
                        with open(file_path, 'r', encoding='utf-8') as f:
                            for line in f:
                                if line.strip():
                                    parts = line.strip().split(',')
                                    if len(parts) >= 1:
                                        stock_code = parts[0].strip().replace('\ufeff', '')
                                        if stock_code not in stock_codes:
                                            stock_codes.append(stock_code)
        
        # 添加自定义股票列表中的股票代码
        for row in range(self.stock_list.rowCount()):
            code = self.stock_list.item(row, 0).text()
            if code and code not in stock_codes:
                stock_codes.append(code)
                
        return stock_codes
    
    def get_trigger_type(self):
        """获取触发类型"""
        trigger_type_map = {
            0: "tick",   # Tick触发
            1: "1m",     # 1分钟K线触发
            2: "5m",     # 5分钟K线触发
            3: "1d",     # 日K线触发
            4: "custom"  # 自定义定时触发
        }
        return trigger_type_map[self.trigger_type_combo.currentIndex()]
        
    def load_stock_list_from_config(self, stock_list):
        """从配置加载股票列表"""
        if not stock_list:
            return
            
        # 清空当前显示
        self.stock_list.setRowCount(0)
        for cb in self.pool_checkboxes.values():
            cb.setChecked(False)
            
        # 获取股票名称
        from khQTTools import get_stock_names
        stock_names = get_stock_names(stock_list, self.get_data_path("全部股票_股票列表.csv"))
        
        # 添加到表格中
        for code in stock_list:
            row = self.stock_list.rowCount()
            self.stock_list.insertRow(row)
            self.stock_list.setItem(row, 0, QTableWidgetItem(code))
            self.stock_list.setItem(row, 1, QTableWidgetItem(stock_names.get(code, "--")))

    def update_realtime_data_group_status(self):
        """更新实盘数据获取模块的显示和启用状态（固定隐藏）"""
        # 回测模式下始终隐藏实盘数据获取模块
        self.realtime_data_group.hide()

    def get_realtime_data_mode(self):
        """获取实盘数据获取模式"""
        # 检查触发方式
        trigger_type = self.trigger_type_combo.currentIndex()
        
        # 对于自定义定时触发，返回None或特殊值
        if trigger_type == 3:
            return "custom"
            
        # 对于其他触发方式，检查单选按钮状态
        if self.full_quote_radio.isChecked():
            return "full_quote"
        else:
            return "single_quote"

    def seconds_to_time(self, seconds):
        """将秒数转换为时间字符串"""
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def on_log_filter_changed(self, state):
        """处理日志类型过滤复选框的变化"""
        self.refresh_log_display()

    def refresh_log_display(self):
        """根据过滤设置重新显示日志"""
        # 清空当前显示
        self.log_text.clear()
        
        # 重新显示符合过滤条件的日志
        for entry in self.log_entries:
            level = entry['level']
            if level in self.log_filters and self.log_filters[level].isChecked():
                self.log_text.moveCursor(self.log_text.textCursor().End)
                self.log_text.insertHtml(entry['formatted'])
        
        # 滚动到底部
        self.log_text.verticalScrollBar().setValue(
            self.log_text.verticalScrollBar().maximum()
        )

    def show_settings(self):
        """显示设置对话框"""
        try:
            # 创建并显示设置对话框
            settings_dialog = SettingsDialog(self)
            result = settings_dialog.exec_()
            
            # 如果用户点击了保存按钮，更新延迟显示设置并强制更新配置
            if result == QDialog.Accepted:
                self.update_delay_log_setting()

                # 更新最大日志行数设置
                self.max_log_lines = self.settings.value('max_log_lines', 1000, type=int)

                # 记录设置变更日志 - init_data_enabled只存在于QSettings中
                init_data_enabled = self.settings.value('init_data_enabled', False, type=bool)
                self.log_message(f"设置已更新 - 数据初始化: {'启用' if init_data_enabled else '禁用'}, 最大日志行数: {self.max_log_lines}", "INFO")
            
            # 如果需要，可以在这里处理设置对话框关闭后的操作
            self.check_software_status()
            
            # 加载可能更新的配置
            qsettings = QSettings('KHQuant', 'StockAnalyzer')
            client_path = qsettings.value('client_path', '')
            if client_path:
                # 更新配置中的客户端路径
                if 'client_path' not in self.config:
                    self.config['client_path'] = client_path
                
                # 记录日志
                logging.info(f"已更新miniQMT客户端路径: {client_path}")
            
        except Exception as e:
            logging.error(f"显示设置对话框时出错: {str(e)}")
            self.show_error_dialog("设置错误", f"显示设置对话框时出错: {str(e)}")
    
    def update_delay_log_setting(self):
        """更新延迟显示日志设置"""
        try:
            # 从设置中重新读取延迟显示状态
            old_setting = self.delay_log_display
            self.delay_log_display = self.settings.value('delay_log_display', True, type=bool)
            
            # 记录设置变更
            if self.delay_log_display:
                self.log_message("延迟显示日志已启用", "INFO")
                if not old_setting:
                    self.log_message("提示：下次运行策略时，日志将在策略完成后统一显示", "INFO")
            else:
                self.log_message("延迟显示日志已禁用", "INFO")
                if old_setting:
                    self.log_message("提示：策略运行时的日志将立即显示", "INFO")
                
        except Exception as e:
            logging.error(f"更新延迟显示设置时出错: {str(e)}")
    
    def initialize_update_manager(self):
        """初始化更新管理器"""
        self.update_manager = UpdateManager(self)
        self.update_manager.check_finished.connect(self.handle_update_check_finished)
        
        # 加载更新设置
        self.set_update_config()
        
    def set_update_config(self):
        """设置更新配置"""
        settings = QSettings('KHQuant', 'StockAnalyzer')
        self.update_manager.auto_check = settings.value('auto_check_update', True, type=bool)
        self.update_manager.update_channel = settings.value('update_channel', 'stable', type=str)
    
    def check_for_updates(self):
        """检查软件更新"""
        try:
            logging.info("开始检查软件更新")
            # 确保发送当前版本号
            current_version = get_version_info()['version']
            
            # 调用更新检查，只传递版本号
            self.update_manager.check_for_updates(current_version)
        except Exception as e:
            logging.error(f"检查更新时发生错误: {str(e)}", exc_info=True)
            QMessageBox.warning(self, "更新检查失败", f"检查更新时发生错误: {str(e)}")

    def handle_update_check_finished(self, success, message):
        """处理更新检查完成的回调"""
        logging.info(f"更新检查完成: 成功={success}, 消息={message}")
        
        # 添加软件准备就绪的日志
        self.log_message("软件准备就绪", "INFO")
    
    def delayed_update_check(self):
        """延迟执行更新检查"""
        try:
            self.check_for_updates()
        except Exception as e:
            logging.error(f"延迟更新检查时出错: {str(e)}", exc_info=True)
    
    def show_current_version(self):
        """显示当前版本信息"""
        # 直接使用UpdateManager的方法
        self.update_manager.show_current_version()

    def open_data_module(self):
        """打开CSV数据管理模块"""
        try:
            # 记录日志
            self.log_message("正在打开CSV数据管理模块...", "INFO")
            
            # 检查是否已经创建了CSV数据管理窗口
            if hasattr(self, 'csv_manager_window') and self.csv_manager_window:
                # 如果窗口已存在，最大化显示并激活它
                self.csv_manager_window.showMaximized()
                self.csv_manager_window.raise_()
                self.csv_manager_window.activateWindow()
                self.log_message("CSV数据管理模块窗口已激活", "INFO")
                return
            
            # 创建新的CSV数据管理窗口
            if StockDataProcessorGUI is not None:
                try:
                    self.csv_manager_window = StockDataProcessorGUI()
                    # 连接窗口关闭信号，当窗口被关闭时清除引用
                    self.csv_manager_window.destroyed.connect(lambda: setattr(self, 'csv_manager_window', None))
                    self.csv_manager_window.showMaximized()  # 最大化显示
                    self.log_message("CSV数据管理模块已成功打开", "INFO")
                    return
                except Exception as e:
                    logging.error(f"使用导入方式打开CSV数据管理模块失败: {str(e)}", exc_info=True)
                    # 继续尝试方法二
            # 方法二：使用子进程运行GUI.py
            # 确定GUI.py的路径 - 开发环境
            base_dir = os.path.dirname(os.path.abspath(__file__))
            gui_path = os.path.join(base_dir, 'GUI.py')

            if os.path.exists(gui_path):
                self.log_message(f"找到GUI.py文件，路径: {gui_path}", "INFO")

                # 使用子进程启动GUI.py
                import subprocess
                python_executable = sys.executable
                subprocess.Popen([python_executable, gui_path])
                self.log_message("CSV数据管理模块已在新进程中启动", "INFO")
            else:
                self.log_message(f"未找到GUI.py文件: {gui_path}", "ERROR")
                QMessageBox.critical(self, "错误", f"无法找到GUI.py文件: {gui_path}")
            
        except Exception as e:
            error_message = f"打开CSV数据管理模块时出错: {str(e)}"
            self.log_message(error_message, "ERROR")
            logging.error(error_message, exc_info=True)
            QMessageBox.critical(self, "错误", f"打开CSV数据管理模块时出错:\n{str(e)}")

    def open_data_viewer(self):
        """打开数据查看器"""
        try:
            # 记录日志
            self.log_message("正在打开数据查看器...", "INFO")
            
            # 检查是否已经创建了数据查看器窗口
            if hasattr(self, 'data_viewer_window') and self.data_viewer_window:
                # 如果窗口已存在，重新加载配置并最大化显示并激活它
                self.data_viewer_window.reload_config_and_data()
                self.data_viewer_window.showMaximized()
                self.data_viewer_window.raise_()
                self.data_viewer_window.activateWindow()
                self.log_message("数据查看器窗口已激活并更新配置", "INFO")
                return
            
            # 创建新的数据查看器窗口
            if GUIDataViewer is not None:
                self.data_viewer_window = GUIDataViewer()
                # 连接窗口关闭信号，当窗口被关闭时清除引用
                self.data_viewer_window.destroyed.connect(lambda: setattr(self, 'data_viewer_window', None))
                self.data_viewer_window.showMaximized()  # 最大化显示
                self.log_message("数据查看器已成功打开", "INFO")
            else:
                error_message = "数据查看器模块未正确导入"
                self.log_message(error_message, "ERROR")
                QMessageBox.critical(self, "错误", error_message)
                
        except Exception as e:
            error_message = f"打开数据查看器时出错: {str(e)}"
            self.log_message(error_message, "ERROR")
            logging.error(error_message, exc_info=True)
            QMessageBox.critical(self, "错误", f"打开数据查看器时出错:\n{str(e)}")

    def open_scheduler(self):
        """打开数据定时补充模块"""
        try:
            # 记录日志
            self.log_message("正在打开数据定时补充模块...", "INFO")
            
            # 检查是否已经创建了定时补充器窗口
            if hasattr(self, 'scheduler_window') and self.scheduler_window:
                # 如果窗口已存在，最大化显示并激活它
                self.scheduler_window.showMaximized()
                self.scheduler_window.raise_()
                self.scheduler_window.activateWindow()
                self.log_message("数据定时补充模块窗口已激活", "INFO")
                return
            
            # 创建新的定时补充器窗口
            if GUIScheduler is not None:
                self.scheduler_window = GUIScheduler()
                # 连接窗口关闭信号，当窗口被关闭时清除引用
                self.scheduler_window.destroyed.connect(lambda: setattr(self, 'scheduler_window', None))
                self.scheduler_window.showMaximized()  # 最大化显示
                self.log_message("数据定时补充模块已成功打开", "INFO")
            else:
                error_message = "数据定时补充模块未正确导入"
                self.log_message(error_message, "ERROR")
                QMessageBox.critical(self, "错误", error_message)
                
        except Exception as e:
            error_message = f"打开数据定时补充模块时出错: {str(e)}"
            self.log_message(error_message, "ERROR")
            logging.error(error_message, exc_info=True)
            QMessageBox.critical(self, "错误", f"打开数据定时补充模块时出错:\n{str(e)}")
    
    def _create_strategy_template(self, file_path: str):
        """创建策略文件模板"""
        try:
            from datetime import datetime
            
            template = '''# -*- coding: utf-8 -*-
"""
策略名称：新策略
创建时间：{datetime}
"""

from khQuantImport import *  # 导入统一工具与指标


def init(stocks=None, data=None):
    """策略初始化（本策略无需特殊初始化）"""
    pass  # 占位


def khHandlebar(data: Dict) -> List[Dict]:
    """
    主策略函数
    
    参数:
        data: 包含当前K线数据的字典
    
    返回:
        signals: 信号列表
    """
    signals = []  # 信号列表
    
    # 在这里编写你的策略逻辑
    
    return signals  # 返回信号


def khPreMarket(context: Dict) -> List[Dict]:
    """
    盘前处理函数（可选）
    在每日开盘前的指定时间点调用
    
    参数:
        context: 上下文字典
    
    返回:
        signals: 信号列表
    """
    signals = []
    return signals


def khPostMarket(context: Dict) -> List[Dict]:
    """
    盘后处理函数（可选）
    在每日收盘后的指定时间点调用
    
    参数:
        context: 上下文字典
    
    返回:
        signals: 信号列表
    """
    signals = []
    return signals
'''
            
            content = template.format(datetime=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            
            # 确保目录存在
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            # 写入文件
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            self.log_message(f"已创建策略文件: {file_path}", "INFO")
            
        except Exception as e:
            self.log_message(f"创建策略文件失败: {str(e)}", "ERROR")
            logging.error(f"创建策略文件失败: {e}", exc_info=True)
            raise

    def get_strategies_directory(self):
        """获取策略文件目录"""
        # 获取用户数据目录
        if os.name == 'nt':  # Windows
            user_data_dir = os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'KhQuant')
        else:  # Linux/Mac
            user_data_dir = os.path.join(os.path.expanduser('~'), '.khquant')

        # 策略文件目录
        strategies_dir = os.path.join(user_data_dir, 'strategies')
        return strategies_dir

    def open_code_editor(self):
        """打开代码编辑器"""
        try:
            if self.editor_module is not None:
                # 获取当前选择的策略文件路径
                strategy_file_path = self.config.get("strategy_file", "")
                
                # 检查策略文件是否存在
                if strategy_file_path and os.path.exists(strategy_file_path):
                    # 如果策略文件存在，直接打开编辑器并加载文件
                    self.editor_module.open_editor(strategy_file_path)
                    self.log_message(f"代码编辑器已打开，加载策略文件: {strategy_file_path}", "INFO")
                else:
                    # 如果没有设置策略文件或文件不存在，提示用户选择或创建
                    msg_box = QMessageBox(self)
                    msg_box.setWindowTitle("策略文件")
                    msg_box.setText("当前未设置策略文件或文件不存在。\n\n请选择操作:")
                    msg_box.addButton("选择现有文件", QMessageBox.YesRole)
                    msg_box.addButton("创建新文件", QMessageBox.NoRole)
                    msg_box.addButton("取消", QMessageBox.RejectRole)
                    reply = msg_box.exec_()
                    
                    if reply == 0:  # 选择现有文件
                        # 选择现有策略文件
                        file_path, _ = QFileDialog.getOpenFileName(
                            self, "选择策略文件", 
                            os.path.expanduser("~/Documents"), 
                            "Python文件 (*.py);;所有文件 (*.*)"
                        )
                        if file_path:
                            # 更新配置中的策略文件路径
                            self.config["strategy_file"] = file_path
                            self.strategy_path.setText(file_path)
                            self.save_config()
                            # 打开编辑器并加载文件
                            self.editor_module.open_editor(file_path)
                            self.log_message(f"代码编辑器已打开，加载策略文件: {file_path}", "INFO")
                        else:
                            return  # 用户取消了文件选择
                    elif reply == 1:  # 创建新文件
                        # 创建新策略文件
                        file_path, _ = QFileDialog.getSaveFileName(
                            self, "创建新策略文件", 
                            os.path.expanduser("~/Documents/new_strategy.py"), 
                            "Python文件 (*.py);;所有文件 (*.*)"
                        )
                        if file_path:
                            # 创建基础策略文件模板
                            template_content = '''# -*- coding: utf-8 -*-
"""策略文件模板"""

def init(context):
    """初始化函数"""
    pass

def khHandlebar(context):
    """主要策略逻辑函数"""
    pass
'''
                            try:
                                with open(file_path, 'w', encoding='utf-8') as f:
                                    f.write(template_content)
                                
                                # 更新配置中的策略文件路径
                                self.config["strategy_file"] = file_path
                                self.strategy_path.setText(file_path)
                                self.save_config()
                                # 打开编辑器并加载文件
                                self.editor_module.open_editor(file_path)
                                self.log_message(f"新策略文件已创建并打开: {file_path}", "INFO")
                            except Exception as e:
                                error_message = f"创建策略文件失败: {str(e)}"
                                self.log_message(error_message, "ERROR")
                                QMessageBox.critical(self, "错误", error_message)
                                return
                        else:
                            return  # 用户取消了文件保存
                    else:  # reply == 2 或其他值，表示取消
                        # 用户选择取消
                        return
            else:
                error_message = "代码编辑器模块未正确初始化"
                self.log_message(error_message, "ERROR")
                QMessageBox.critical(self, "错误", error_message)
        except Exception as e:
            error_message = f"打开代码编辑器时出错: {str(e)}"
            self.log_message(error_message, "ERROR")
            logging.error(error_message, exc_info=True)
            QMessageBox.critical(self, "错误", f"打开代码编辑器时出错:\n{str(e)}")

    def paintEvent(self, event):
        """绘制窗口边框"""
        super().paintEvent(event)
        '''
        # 绘制边框
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)  # 启用抗锯齿
        
        # 使用5像素宽的更明显的边框
        pen = QPen(QColor("#e0e0e0"), 5)  # 更亮的灰色，更宽的线条
        pen.setJoinStyle(Qt.MiterJoin)  # 设置连接风格
        painter.setPen(pen)
        
        # 绘制矩形边框，稍微内缩以避免被裁剪
        painter.drawRect(self.rect().adjusted(2, 2, -2, -2))
        '''

    def open_help_tutorial(self):
        """打开在线教程页面"""
        try:
            from PyQt5.QtCore import QUrl
            from PyQt5.QtGui import QDesktopServices
            
            # 打开教程网址
            url = QUrl("https://khsci.com/khQuant/tutorial/")
            QDesktopServices.openUrl(url)
            
            # 记录日志
            self.log_message("已打开在线教程页面", "INFO")
        except Exception as e:
            error_msg = f"打开教程页面失败: {str(e)}"
            self.log_message(error_msg, "ERROR")
            QMessageBox.critical(self, "错误", error_msg)

    @pyqtSlot(int)
    def update_progress_bar(self, value):
        """更新进度条的值"""
        if self.progress_bar:
            # 确保值在0-100之间
            value = max(0, min(value, 100))
            self.progress_bar.setValue(value)
            if hasattr(self, 'progress_text') and self.progress_text:
                # 计算预计剩余时间
                remaining_time_text = ""
                if value > 0 and hasattr(self, 'backtest_start_time'):
                    import time
                    elapsed_time = time.time() - self.backtest_start_time
                    # 根据已完成百分比计算预计总时间
                    estimated_total_time = elapsed_time / (value / 100.0)
                    # 计算剩余时间
                    remaining_time = estimated_total_time - elapsed_time
                    
                    # 格式化剩余时间
                    if remaining_time > 0:
                        hours = int(remaining_time // 3600)
                        minutes = int((remaining_time % 3600) // 60)
                        seconds = int(remaining_time % 60)
                        remaining_time_text = f" | 预计剩余: {hours:02d}:{minutes:02d}:{seconds:02d}"
                
                # 更新文本
                self.progress_text.setText(f"回测进度: {value}%{remaining_time_text}")
            
            # 确保进度条在回测模式下可见
            if self.get_run_mode() == "backtest" and not self.progress_container.isVisible():
                self.progress_container.show()

    def save_config_as(self):
        """配置另存为
        
        注意：.kh文件本质是JSON格式，仅使用自定义扩展名
        """
        # 获取上一次使用的配置文件路径作为默认目录
        last_config_path = self.settings.value('last_config_path', '')
        if last_config_path and os.path.exists(os.path.dirname(last_config_path)):
            default_dir = os.path.dirname(last_config_path)
        else:
            default_dir = ""
        
        options = QFileDialog.Options()
        file_path, _ = QFileDialog.getSaveFileName(
            self, "配置另存为", default_dir, "看海配置文件 (*.kh)", options=options
        )
        
        if not file_path:
            return
            
        # 确保文件有.kh扩展名
        if not file_path.endswith('.kh'):
            file_path += '.kh'
            
        try:
            # 构建配置字典
            config = {
                "system": {
                    "userdata_path": self.settings.value('qmt_path', 'D:\\国金证券QMT交易端\\userdata_mini')
                },
                "run_mode": self.get_run_mode(),
                "account": {
                    "account_id": self.settings.value('account_id', '8888888888'),
                    "account_type": self.settings.value('account_type', 'STOCK')
                },
                "strategy_file": self.strategy_path.text().strip(),
                # 添加实盘数据获取模式配置
                "data_mode": self.get_realtime_data_mode(),
                "backtest": {
                    "start_time": self.start_date.date().toString("yyyyMMdd"),
                    "end_time": self.end_date.date().toString("yyyyMMdd"),
                    "init_capital": float(self.initial_cash.text()),
                    "min_volume": int(self.min_volume.text()),
                    "benchmark": self.benchmark_input.text().strip(),
                    "trade_cost": {
                        "min_commission": float(self.min_commission.text()),
                        "commission_rate": float(self.commission_rate.text()),
                        "stamp_tax_rate": float(self.stamp_tax.text()),
                        "flow_fee": float(self.flow_fee.text()),
                        "slippage": self.get_slippage_settings()
                    },
                    "trigger": {
                        "type": self.get_trigger_type(),
                        "custom_times": self.get_custom_time_points(),
                        "start_time": self.start_time_edit.time().toString("HH:mm:ss"),
                        "end_time": self.end_time_edit.time().toString("HH:mm:ss"),
                        "interval": self.interval_spin.value()
                    }
                },
                "data": {
                    "kline_period": self.period_selector.currentText(),
                    "dividend_type": self.get_dividend_type(),
                    "fields": self.get_selected_fields(),
                    "stock_list": self.get_stock_list()
                },
                "market_callback": {
                    "pre_market_enabled": self.pre_trigger_checkbox.isChecked(),
                    "pre_market_time": self.pre_trigger_time.time().toString("HH:mm:ss"),
                    "post_market_enabled": self.post_trigger_checkbox.isChecked(),
                    "post_market_time": self.post_trigger_time.time().toString("HH:mm:ss")
                },
                "risk": {
                    "position_limit": 0.95,
                    "order_limit": 100,
                    "loss_limit": 0.1
                }
            }
            
            # 保存为JSON文件
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
                
            # 保存到实例变量中
            self.config = config
            self.current_config_file = file_path  # 记录当前配置文件路径
            # 保存此次选择的配置文件路径
            self.settings.setValue('last_config_path', file_path)
            
            # 更新窗口标题，显示当前配置文件名
            file_name = os.path.basename(file_path)
            self.setWindowTitle(f"看海量化回测系统 - {file_name}")
            
            # 记录日志
            self.log_message(f"配置已保存到: {file_path}", "INFO")
            
            # 检测文件是否在危险位置
            strategy_file_path = self.strategy_path.text().strip()
            self.show_internal_dir_warning(file_path, strategy_file_path)
            
            # 显示成功消息
            QMessageBox.information(self, "保存成功", f"配置已保存到: {file_path}")
            
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"保存配置文件时出错: {str(e)}")

    def save_config(self):
        """保存配置
        如果已经加载了配置文件，则覆盖当前文件；
        否则执行配置另存为操作
        
        注意：.kh文件本质是JSON格式，仅使用自定义扩展名
        """
        # 检查是否已经加载了配置文件
        if hasattr(self, 'current_config_file') and self.current_config_file:
            # 添加确认对话框
            file_name = os.path.basename(self.current_config_file)
            reply = QMessageBox.question(
                self, '保存确认',
                f"确定要覆盖当前配置文件 '{file_name}' 吗?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            
            if reply != QMessageBox.Yes:
                return
                
            try:
                # 构建配置字典
                config = {
                    "system": {
                        "userdata_path": self.settings.value('qmt_path', 'D:\\国金证券QMT交易端\\userdata_mini')
                    },
                    "run_mode": self.get_run_mode(),
                    "account": {
                        "account_id": self.settings.value('account_id', '8888888888'),
                        "account_type": self.settings.value('account_type', 'STOCK')
                    },
                    "strategy_file": self.strategy_path.text().strip(),
                    # 添加实盘数据获取模式配置
                    "data_mode": self.get_realtime_data_mode(),
                    "backtest": {
                        "start_time": self.start_date.date().toString("yyyyMMdd"),
                        "end_time": self.end_date.date().toString("yyyyMMdd"),
                        "init_capital": float(self.initial_cash.text()),
                        "min_volume": int(self.min_volume.text()),
                        "benchmark": self.benchmark_input.text().strip(),
                        "trade_cost": {
                            "min_commission": float(self.min_commission.text()),
                            "commission_rate": float(self.commission_rate.text()),
                            "stamp_tax_rate": float(self.stamp_tax.text()),
                            "flow_fee": float(self.flow_fee.text()),
                            "slippage": self.get_slippage_settings()
                        },
                        "trigger": {
                            "type": self.get_trigger_type(),
                            "custom_times": self.get_custom_time_points(),
                            "start_time": self.start_time_edit.time().toString("HH:mm:ss"),
                            "end_time": self.end_time_edit.time().toString("HH:mm:ss"),
                            "interval": self.interval_spin.value()
                        }
                    },
                    "data": {
                        "kline_period": self.period_selector.currentText(),
                        "dividend_type": self.get_dividend_type(),
                        "fields": self.get_selected_fields(),
                        "stock_list": self.get_stock_list()
                    },
                    "market_callback": {
                        "pre_market_enabled": self.pre_trigger_checkbox.isChecked(),
                        "pre_market_time": self.pre_trigger_time.time().toString("HH:mm:ss"),
                        "post_market_enabled": self.post_trigger_checkbox.isChecked(),
                        "post_market_time": self.post_trigger_time.time().toString("HH:mm:ss")
                    },
                    "risk": {
                        "position_limit": 0.95,
                        "order_limit": 100,
                        "loss_limit": 0.1
                    }
                }
                
                # 保存到当前配置文件
                with open(self.current_config_file, 'w', encoding='utf-8') as f:
                    json.dump(config, f, indent=4, ensure_ascii=False)
                    
                # 保存到实例变量
                self.config = config
                
                # 记录日志
                self.log_message(f"配置已保存到: {self.current_config_file}", "INFO")
                
                # 检测文件是否在危险位置
                strategy_file_path = self.strategy_path.text().strip()
                self.show_internal_dir_warning(self.current_config_file, strategy_file_path)
                
                # 显示成功消息
                QMessageBox.information(self, "保存成功", f"配置已保存到: {self.current_config_file}")
                
            except Exception as e:
                QMessageBox.critical(self, "保存失败", f"保存配置文件时出错: {str(e)}")
        else:
            # 如果没有加载配置文件，则执行另存为操作
            self.save_config_as()

    def toggle_delay_log(self, state):
        """切换日志延迟显示模式 - 已废弃，现在通过设置界面管理"""
        # 该方法已废弃，延迟显示现在通过设置界面管理
        # self.delay_log_display = state == Qt.Checked
        # if self.delay_log_display:
        #     self.log_message("已启用日志延迟显示模式，策略执行完成后将显示日志", "INFO")
        # else:
        #     self.log_message("已禁用日志延迟显示模式", "INFO")
        pass

    def display_delayed_logs(self):
        """显示所有延迟的日志（分批加载，避免界面卡顿）"""
        try:
            if not self.delayed_logs:
                self.log_message("没有延迟日志需要显示", "INFO")
                return

            log_count = len(self.delayed_logs)

            # 统计各种级别的日志数量
            level_counts = {}
            for log_entry in self.delayed_logs:
                level = log_entry['level']
                level_counts[level] = level_counts.get(level, 0) + 1

            # 显示开始信息和统计
            self.log_message(f"开始显示{log_count}条延迟日志（分批加载中...）", "INFO")
            stats_msg = "延迟日志统计: " + ", ".join([f"{level}={count}" for level, count in sorted(level_counts.items())])
            self.log_message(stats_msg, "INFO")

            # 保存日志副本并清空原队列
            logs_to_display = self.delayed_logs.copy()
            self.delayed_logs = []

            # 应用最大日志行数限制
            max_lines = getattr(self, 'max_log_lines', 1000)
            if len(logs_to_display) > max_lines:
                # 只保留最新的max_lines条日志
                skipped_count = len(logs_to_display) - max_lines
                logs_to_display = logs_to_display[-max_lines:]
                self.log_message(f"为减轻UI负担，已跳过最早的 {skipped_count} 条日志，仅显示最新 {max_lines} 条", "WARNING")
                log_count = max_lines

            # 分批显示参数
            batch_size = 500  # 每批显示的日志数量
            self._delayed_logs_queue = logs_to_display
            self._delayed_logs_index = 0
            self._delayed_logs_total = log_count

            # 添加分隔线标识延迟日志开始
            separator_msg = f'<span style="color: #00FF00">[======== 以下是{log_count}条延迟显示的日志 ========]</span><br>'
            self.log_text.moveCursor(QTextCursor.End)
            self.log_text.insertHtml(separator_msg)

            # 使用定时器分批加载
            self._display_delayed_logs_batch(batch_size)

        except Exception as e:
            self.log_error("显示延迟日志时出错", e)

    def _display_delayed_logs_batch(self, batch_size=500):
        """分批显示延迟日志"""
        try:
            if not hasattr(self, '_delayed_logs_queue') or self._delayed_logs_index >= len(self._delayed_logs_queue):
                # 所有日志已显示完成
                self._finish_delayed_logs_display()
                return

            # 获取当前批次的日志
            start_idx = self._delayed_logs_index
            end_idx = min(start_idx + batch_size, len(self._delayed_logs_queue))

            # 禁用更新以提高性能
            self.log_text.setUpdatesEnabled(False)

            # 构建当前批次的HTML
            html_content = ""
            for i in range(start_idx, end_idx):
                html_content += self._delayed_logs_queue[i]['formatted']

            # 插入内容
            cursor = self.log_text.textCursor()
            cursor.movePosition(QTextCursor.End)
            cursor.insertHtml(html_content)

            # 重新启用更新
            self.log_text.setUpdatesEnabled(True)

            # 更新索引
            self._delayed_logs_index = end_idx

            # 计算进度
            progress = int((end_idx / self._delayed_logs_total) * 100)

            # 如果还有更多日志，使用定时器继续下一批
            if self._delayed_logs_index < len(self._delayed_logs_queue):
                # 更新状态（每隔几批更新一次，避免频繁更新）
                if progress % 20 == 0 or end_idx == self._delayed_logs_total:
                    self.status_label.setText(f"加载日志中... {progress}%")
                # 使用定时器延迟执行下一批，让UI有机会响应
                QTimer.singleShot(10, lambda: self._display_delayed_logs_batch(batch_size))
            else:
                self._finish_delayed_logs_display()

        except Exception as e:
            self.log_error("分批显示延迟日志时出错", e)
            self._finish_delayed_logs_display()

    def _finish_delayed_logs_display(self):
        """完成延迟日志显示"""
        try:
            # 添加分隔线标识延迟日志结束
            end_separator_msg = f'<span style="color: #00FF00">[======== 延迟日志显示完成 ========]</span><br>'
            self.log_text.moveCursor(QTextCursor.End)
            self.log_text.insertHtml(end_separator_msg)

            # 滚动到底部
            self.log_text.verticalScrollBar().setValue(
                self.log_text.verticalScrollBar().maximum()
            )

            # 显示完成信息
            total = getattr(self, '_delayed_logs_total', 0)
            self.log_message(f"延迟日志显示完成，共显示{total}条日志", "INFO")
            self.status_label.setText("日志加载完成")

            # 清理临时变量
            if hasattr(self, '_delayed_logs_queue'):
                delattr(self, '_delayed_logs_queue')
            if hasattr(self, '_delayed_logs_index'):
                delattr(self, '_delayed_logs_index')
            if hasattr(self, '_delayed_logs_total'):
                delattr(self, '_delayed_logs_total')

        except Exception as e:
            self.log_error("完成延迟日志显示时出错", e)
            
        except Exception as e:
            print(f"显示延迟日志时出错: {str(e)}")
            import traceback
            print(traceback.format_exc())
            self.log_message(f"显示延迟日志时出错: {str(e)}", "ERROR")

    def hide_progress(self):
        """隐藏进度条"""
        self.progress_container.hide()

    def check_file_in_internal_dir(self, file_path):
        """检测文件是否保存在软件安装目录的_internal文件夹内

        Args:
            file_path: 要检测的文件路径

        Returns:
            bool: 如果文件在_internal目录内返回True，否则返回False
        """
        if not file_path:
            return False

        # 只在打包环境下检测
        if not getattr(sys, 'frozen', False):
            return False

        try:
            # 获取打包环境的_internal目录
            if hasattr(sys, '_MEIPASS'):
                internal_dir = sys._MEIPASS
            else:
                # 如果没有_MEIPASS，使用可执行文件所在目录的_internal子目录
                exe_dir = os.path.dirname(sys.executable)
                internal_dir = os.path.join(exe_dir, '_internal')

            # 标准化路径进行比较
            file_path_normalized = os.path.normpath(os.path.abspath(file_path)).lower()
            internal_dir_normalized = os.path.normpath(os.path.abspath(internal_dir)).lower()

            # 检查文件是否在_internal目录下
            return file_path_normalized.startswith(internal_dir_normalized)
        except Exception:
            return False

    def show_internal_dir_warning(self, config_file_path, strategy_file_path):
        """显示文件保存在_internal目录的警告对话框
        
        Args:
            config_file_path: 配置文件路径
            strategy_file_path: 策略文件路径
        """
        warnings = []
        
        if self.check_file_in_internal_dir(config_file_path):
            warnings.append(f"• 配置文件: {config_file_path}")
            
        if self.check_file_in_internal_dir(strategy_file_path):
            warnings.append(f"• 策略文件: {strategy_file_path}")
        
        if warnings:
            # 记录警告到日志
            self.log_message("⚠️ 检测到文件保存在危险位置！", "WARNING")
            for warning in warnings:
                self.log_message(warning, "WARNING")
            self.log_message("建议立即将文件移动到安全位置以避免更新时丢失", "WARNING")
            
            warning_text = "⚠️ 检测到以下文件保存在软件安装目录内：\n\n" + "\n".join(warnings)
            warning_text += "\n\n🔥 风险警告：\n"
            warning_text += "• 软件更新时会完全删除并重建安装目录\n"
            warning_text += "• 保存在安装目录内的文件将被永久删除\n"
            warning_text += "• 这可能导致您的策略和配置文件丢失\n\n"
            warning_text += "💡 解决方案：\n"
            warning_text += "• 立即将这些文件移动到安全位置\n"
            warning_text += "• 重新选择策略文件和保存配置文件\n\n"
            warning_text += "📁 推荐保存位置：\n"
            warning_text += f"• 用户文档目录: {os.path.expanduser('~/Documents/KHQuant/')}\n"
            warning_text += f"• 桌面目录: {os.path.expanduser('~/Desktop/')}\n"
            warning_text += "• 用户策略目录: 点击[选择策略文件]时的默认目录\n"
            warning_text += "• 或任何您熟悉的其他文件夹"
            
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("🚨 文件位置安全警告")
            msg_box.setIcon(QMessageBox.Warning)
            msg_box.setText("检测到重要文件存在丢失风险！")
            msg_box.setDetailedText(warning_text)
            msg_box.setStandardButtons(QMessageBox.Ok)
            
            # 设置警告对话框的样式
            msg_box.setStyleSheet("""
                QMessageBox {
                    background-color: #2b2b2b;
                    color: #ffffff;
                }
                QMessageBox QLabel {
                    color: #ffffff;
                    font-size: 11px;
                }
                QMessageBox QPushButton {
                    background-color: #ff6b35;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 8px 16px;
                    font-size: 12px;
                    font-weight: bold;
                }
                QMessageBox QPushButton:hover {
                    background-color: #ff5722;
                }
            """)
            
            msg_box.exec_()

    def get_user_strategies_dir(self):
        """获取用户策略文件目录路径"""
        # 获取用户数据目录
        if os.name == 'nt':  # Windows
            user_data_dir = os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'KhQuant')
        else:  # Linux/Mac
            user_data_dir = os.path.join(os.path.expanduser('~'), '.khquant')
        
        # 策略文件目录
        strategies_dir = os.path.join(user_data_dir, 'strategies')
        
        # 确保目录存在
        os.makedirs(strategies_dir, exist_ok=True)
        
        return strategies_dir

    def init_user_strategies(self):
        """初始化用户策略目录，复制默认策略文件"""
        user_strategies_dir = self.get_user_strategies_dir()

        # 获取程序内置的默认策略文件路径
        if getattr(sys, 'frozen', False):
            # 打包环境 - 使用_internal目录
            default_strategies_dir = os.path.join(sys._MEIPASS, 'strategies')
        else:
            # 开发环境 - 使用项目目录
            default_strategies_dir = os.path.join(os.path.dirname(__file__), 'strategies')

        # 如果用户策略目录为空，复制默认策略文件
        if os.path.exists(default_strategies_dir):
            for file_name in os.listdir(default_strategies_dir):
                if file_name.endswith(('.py', '.kh')):
                    src_file = os.path.join(default_strategies_dir, file_name)
                    dst_file = os.path.join(user_strategies_dir, file_name)

                    # 只有文件不存在时才复制（避免覆盖用户修改的文件）
                    if not os.path.exists(dst_file):
                        try:
                            import shutil
                            shutil.copy2(src_file, dst_file)
                            self.log_message(f"复制默认策略文件: {file_name}", "INFO")
                        except Exception as e:
                            self.log_message(f"复制策略文件失败 {file_name}: {str(e)}", "WARNING")

        return user_strategies_dir

class CustomSplashScreen(QSplashScreen):
    """自定义启动画面"""
    def __init__(self, icon_path):
        # 创建启动画面图像
        splash_img = QPixmap(os.path.join(icon_path, 'splash.png'))  # 确保有这个图片
        super().__init__(splash_img)
        
        # 设置窗口标志
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        
        # 创建进度条
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setGeometry(
            10,                                    # x position
            splash_img.height() - 20,              # y position
            splash_img.width() - 20,               # width
            10                                     # height
        )
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid #2196F3;
                border-radius: 5px;
                background-color: #1E1E1E;
                text-align: center;
            }
            QProgressBar::chunk {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #2196F3,
                    stop:1 #64B5F6
                );
                border-radius: 3px;
            }
        """)
        self.progress_bar.setTextVisible(False)
        
        # 获取版本信息
        version_info = get_version_info()
        self.version_label = QLabel(f"V{version_info['version']}", self)
        self.version_label.setStyleSheet("""
            color: white;
            font-size: 12px;
            font-weight: bold;
        """)
        self.version_label.setGeometry(
            splash_img.width() - 60,  # x position
            splash_img.height() - 40,  # y position
            50,                       # width
            20                        # height
        )
        
        # 添加提示文本
        self.loading_label = QLabel("正在启动...", self)
        self.loading_label.setStyleSheet("""
            color: white;
            font-size: 14px;
        """)
        self.loading_label.setGeometry(
            10,                       # x position
            splash_img.height() - 40, # y position
            200,                      # width
            20                        # height
        )
        
        # 居中显示
        self.center_on_screen()
        
    def center_on_screen(self):
        """将启动画面居中显示在主屏幕"""
        frame_geo = self.frameGeometry()
        # 使用主屏幕而不是跟随鼠标位置
        desktop = QApplication.desktop()
        primary_screen = desktop.primaryScreen()
        center_point = desktop.screenGeometry(primary_screen).center()
        frame_geo.moveCenter(center_point)
        self.move(frame_geo.topLeft())
    
    def set_progress(self, value, message=""):
        """更新进度条和消息"""
        self.progress_bar.setValue(value)
        if message:
            self.loading_label.setText(message)
        
    def mousePressEvent(self, event):
        """重写鼠标点击事件，防止点击关闭启动画面"""
        pass

# 自定义QComboBox类，禁用滚轮事件
class NoWheelComboBox(QComboBox):
    """禁用滚轮事件的QComboBox"""
    def wheelEvent(self, event):
        # 忽略滚轮事件，不调用父类的wheelEvent
        event.ignore()

# 自定义QDateEdit类，禁用滚轮事件并修复中文显示
class NoWheelDateEdit(QDateEdit):
    """禁用滚轮事件的QDateEdit，修复中文显示问题"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_font()
        
    def setup_font(self):
        """设置字体，解决中文显示问题"""
        try:
            from PyQt5.QtGui import QFont, QFontDatabase
            # 尝试设置支持中文的字体
            font_families = ["Microsoft YaHei", "SimHei", "SimSun", "Arial Unicode MS"]
            for family in font_families:
                if QFontDatabase().hasFamily(family):
                    font = QFont(family, 9)
                    font.setStyleHint(QFont.SansSerif)
                    self.setFont(font)
                    break
        except Exception as e:
            print(f"设置DateEdit字体时出错: {str(e)}")
    
    def wheelEvent(self, event):
        # 忽略滚轮事件，不调用父类的wheelEvent
        event.ignore()

# 自定义QTimeEdit类，禁用滚轮事件并修复中文显示
class NoWheelTimeEdit(QTimeEdit):
    """禁用滚轮事件的QTimeEdit，修复中文显示问题"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_font()
        
    def setup_font(self):
        """设置字体，解决中文显示问题"""
        try:
            from PyQt5.QtGui import QFont, QFontDatabase
            # 尝试设置支持中文的字体
            font_families = ["Microsoft YaHei", "SimHei", "SimSun", "Arial Unicode MS"]
            for family in font_families:
                if QFontDatabase().hasFamily(family):
                    font = QFont(family, 9)
                    font.setStyleHint(QFont.SansSerif)
                    self.setFont(font)
                    break
        except Exception as e:
            print(f"设置TimeEdit字体时出错: {str(e)}")
    
    def wheelEvent(self, event):
        # 忽略滚轮事件，不调用父类的wheelEvent
        event.ignore()

class DisclaimerDialog(QDialog):
    """免责声明弹窗"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("看海量化回测系统 - 免责声明")
        self.setModal(True)
        self.setFixedSize(800, 600)
        self.center_on_screen()

        # 设置窗口标题栏颜色（仅适用于Windows）
        if sys.platform == 'win32':
            try:
                from ctypes import windll, c_int, byref, sizeof
                from ctypes.wintypes import DWORD
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                DWMWA_CAPTION_COLOR = 35
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_USE_IMMERSIVE_DARK_MODE,
                    byref(c_int(2)),
                    sizeof(c_int)
                )
                caption_color = DWORD(0x2b2b2b)
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_CAPTION_COLOR,
                    byref(caption_color),
                    sizeof(caption_color)
                )
            except Exception as e:
                logging.warning(f"设置标题栏深色模式失败: {str(e)}")

        # 设置样式
        self.setStyleSheet("""
            QDialog {
                background-color: #2b2b2b;
                color: #ffffff;
            }
            QTextEdit {
                background-color: #3c3c3c;
                color: #ffffff;
                border: 1px solid #555555;
                border-radius: 5px;
                padding: 15px;
                font-size: 18px;
                line-height: 1.8;
            }
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 16px 32px;
                font-size: 18px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:pressed {
                background-color: #005a9e;
            }
            QLabel {
                color: #ffffff;
                font-size: 22px;
                font-weight: bold;
                margin-bottom: 20px;
            }
        """)
        
        self.init_ui()
        
    def center_on_screen(self):
        """将对话框居中显示在主屏幕"""
        from PyQt5.QtWidgets import QApplication
        # 使用主屏幕而不是跟随鼠标位置
        desktop = QApplication.desktop()
        primary_screen = desktop.primaryScreen()
        screen = desktop.screenGeometry(primary_screen)
        size = self.geometry()
        self.move(
            (screen.width() - size.width()) // 2,
            (screen.height() - size.height()) // 2
        )
    
    def init_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # 标题
        title_label = QLabel("权责说明")
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setStyleSheet("font-size: 28px; font-weight: bold; margin-bottom: 30px;")
        layout.addWidget(title_label)
        
        # 免责声明内容
        disclaimer_text = """在使用"看海量化回测系统"（以下简称"本系统"）之前，请务必仔细阅读并充分理解本文的全部条款。这些条款构成了您与本系统作者之间关于使用本软件的重要约定。


第一章  系统依赖与免责声明

■ 对MiniQMT的依赖
本系统的行情数据获取与交易执行功能，完全依赖于您本地安装的MiniQMT客户端。为了实现回测功能，本系统会在本地存储和处理从MiniQMT下载的行情数据，但系统本身不生产任何原始数据。

■ 数据验证与检验机制
本系统在运行过程中包含了数据有效性检验功能，会对从MiniQMT获取的数据进行基础的完整性和格式校验。但需要明确的是，这些检验仅为程序正常运行的技术保障，不能等同于对数据准确性的担保。市场数据的准确性和及时性完全取决于券商MiniQMT及其上游数据源。

■ 核心功能定位
请注意，当前版本的"看海量化回测系统"是一款策略回测与研究平台，其核心功能是历史数据验证，当前官方版本不包含任何直接执行实盘交易的功能。

■ 全面责任界定
本系统作者的责任仅限于提供软件工具本身。使用本软件过程中遇到的任何问题，包括但不限于系统故障、数据错误、策略失效、操作失误、电脑故障等，均由用户自行承担全部责任。对于因以下原因导致的任何直接或间接损失，作者不承担任何形式的法律或经济责任：

    • 券商MiniQMT客户端或其服务器的任何故障、错误、延迟或数据偏差
    • 网络连接问题、运营商服务中断等第三方因素
    • 用户自行修改代码以启用实盘交易功能后，所产生的一切后果（包括但不限于任何资金损失）
    • 本软件自身的任何漏洞、错误、兼容性问题或运行异常
    • 用户操作不当、配置错误或对软件功能理解偏差


第二章  开源承诺与维护责任

■ 免费与开源
本系统是一款免费且开放源代码的软件，旨在为A股量化爱好者提供一个高效、便利的研究工具。

■ 维护责任限制
作者会尽力维护系统的稳定性并进行功能迭代，但无法承诺对每一位用户的特定需求提供即时支持。具体而言：
    • Bug修复：将根据严重程度与影响范围进行排序并择机处理
    • 功能开发：新功能请求将被纳入待办池，作者会进行评估规划，但无法保证实现时间与具体方案
    • 代码讲解：由于精力所限，作者不提供针对开源代码的任何个人化、一对一的教学服务

■ 鼓励自主创新
本系统完全开源，对于有特殊或紧急功能需求的用户，我们鼓励并支持您在许可协议范围内，利用源代码自行修改、定制和实现。


第三章  使用许可协议

本系统的源代码及相关文档遵循 CC BY-NC 4.0 (署名-非商业性使用 4.0 国际) 许可协议。

■ 您可以自由地：
    • 分享 — 在任何媒介以任何形式复制、分享本作品
    • 演绎 — 修改、转换或以本作品为基础进行创作

■ 但必须遵守以下条款：
    • 署名 (BY) — 您必须给出适当的署名，提供指向本许可协议的链接，并标明是否对作品作出了修改
    • 非商业性使用 (NC) — 您不得将本作品用于任何商业目的
    • 无附加限制 — 您不得附加任何法律条款或技术措施，从而限制他人行使本许可协议所允许的权利

■ 严正声明：关于商业使用的规定
任何个人或实体均可在协议范围内，使用本系统代码进行学习研究与自用修改。

严禁将本系统及其任何衍生版本用于任何形式的商业目的，包括但不限于：出售软件、以本系统为核心提供任何形式的付费服务、搭建商业化平台等。

任何违反此声明的商业行为所引发的一切法律纠纷、商业风险及经济损失，均由该使用者自行承担。作者保留对所有侵权行为进行法律追究的权利。


第四章  内部交流群说明

■ 加入方式与条件
通过作者提供的推荐渠道开通MiniQMT账户的用户，可以联系作者加入内部交流群。(加群免费)

■ 群成员专享权益
内部群成员可以享受以下权益：
    • 内测版本优先体验权：最新功能的内测版本优先推送，可比公开发布提前体验新特性
    • 版本抢先获取：软件正式版本发布后，群成员可通过内部渠道更早获得下载链接和更新包，无需等待公开发布
    • 问题优先支持：在使用过程中遇到的技术问题，能够得到更优先、更及时的响应和技术支持
    • 策略思路分享：群内会不定期分享一些原创的策略思路、编程技巧或市场分析心得
    • 内部策略代码：群成员可获得一些未公开发布的实用策略代码示例，用于学习参考
    • 直接反馈通道：可以直接向作者反馈建议和需求，影响软件未来的开发方向
    • 同好交流平台：与其他量化爱好者深度交流，分享经验，共同进步

■ 群规与维护
内部群主要用于技术交流和软件支持，请遵守基本的讨论秩序。群内严禁任何形式的广告、推销或与量化交易无关的内容。


第五章  投资风险免责声明

■ 重要提示：本系统不构成任何投资建议

■ 教育与研究目的
"看海量化回测系统"及其所有相关内容（包括示例策略、代码、文档、社区讨论等）的唯一目的，是进行量化编程技术交流、策略思想探讨和金融市场研究。

■ 非投资顾问
本系统的任何功能、输出信息（如回测报告、性能指标）及示例代码，均不应被解释为任何形式的投资建议或交易推荐。历史回测表现不代表未来实际收益，过往的业绩无法预示未来的结果。

■ 用户责任自负
您必须基于自身的专业知识、风险承受能力和独立判断来做出投资决策。任何因使用本系统或参考其内容而进行的投资行为，所产生的一切盈利或亏损，均由您自行承担全部责任，与本系统作者无任何关系。

投资有风险，入市需谨慎。"""
        
        # 创建文本编辑器显示免责声明
        text_edit = QTextEdit()
        text_edit.setPlainText(disclaimer_text.strip())
        text_edit.setReadOnly(True)
        layout.addWidget(text_edit)
        
        # 按钮布局
        button_layout = QHBoxLayout()
        
        # 同意按钮
        agree_button = QPushButton("我已阅读并同意")
        agree_button.clicked.connect(self.accept)
        
        # 退出按钮
        exit_button = QPushButton("退出程序")
        exit_button.clicked.connect(self.reject)
        exit_button.setStyleSheet("""
            QPushButton {
                background-color: #d13438;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 16px 32px;
                font-size: 18px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #b71c1c;
            }
            QPushButton:pressed {
                background-color: #8f1419;
            }
        """)
        
        button_layout.addStretch()
        button_layout.addWidget(exit_button)
        button_layout.addWidget(agree_button)
        
        layout.addLayout(button_layout)
        self.setLayout(layout)
    
    def accept(self):
        """用户同意免责声明"""
        super().accept()
    
    def reject(self):
        """用户拒绝免责声明，退出程序"""
        super().reject()

def main():
    try:
        app = QApplication(sys.argv)
        
        # 设置字体和编码，解决时间选择器乱码问题
        try:
            # 设置应用程序的默认字体
            from PyQt5.QtGui import QFont, QFontDatabase
            
            # 添加系统字体
            font_families = ["Microsoft YaHei", "SimHei", "SimSun", "Arial Unicode MS", "DejaVu Sans"]
            default_font = None
            
            for family in font_families:
                if QFontDatabase().hasFamily(family):
                    default_font = QFont(family, 9)
                    break
            
            if default_font:
                app.setFont(default_font)
                print(f"已设置应用字体: {default_font.family()}")
            else:
                # 使用系统默认字体
                default_font = QFont()
                default_font.setPointSize(9)
                app.setFont(default_font)
                print("使用系统默认字体")
            
            # 设置Qt的本地化
            from PyQt5.QtCore import QLocale, QTranslator
            
            # 设置中文本地化
            locale = QLocale(QLocale.Chinese, QLocale.China)
            QLocale.setDefault(locale)
            
            # 创建并安装Qt翻译器
            qt_translator = QTranslator()
            # Qt标准控件的中文翻译
            if qt_translator.load(locale, "qt", "_", ":/translations/"):
                app.installTranslator(qt_translator)
            elif qt_translator.load("qt_zh_CN", ":/translations/"):
                app.installTranslator(qt_translator)
            
            print("已设置中文本地化")
            
        except Exception as e:
            print(f"设置字体和本地化时出错: {str(e)}")
        
        # 禁用LibPNG警告消息
        os.environ["QT_IMAGEIO_MAXALLOC"] = "0"  # 禁用图像大小限制警告
        os.environ["QT_LOGGING_RULES"] = "qt.svg.warning=false;qt.png.warning=false"  # 禁用SVG和PNG相关警告
        
        # 禁用matplotlib字体查找的调试日志
        try:
            import matplotlib
            # 设置matplotlib日志级别为WARNING，忽略DEBUG和INFO信息
            matplotlib.set_loglevel('WARNING')
            
            # 也可以完全关闭特定的日志
            logging.getLogger('matplotlib.font_manager').setLevel(logging.WARNING)
            logging.getLogger('matplotlib').setLevel(logging.WARNING)
        except ImportError:
            # 如果没有安装matplotlib，忽略此步骤
            pass
        
        # 注册QTextCursor类型
        from PyQt5.QtGui import QTextCursor
        from PyQt5.QtCore import QMetaType
        QMetaType.type("QTextCursor")
        
        # 设置应用程序名称和组织名称
        app.setApplicationName("KhQuant")
        app.setOrganizationName("KhQuant")
        
        # 获取图标路径函数
        def get_app_icon_path(icon_name):
            if getattr(sys, 'frozen', False):
                # 打包环境 - 使用_internal目录
                return os.path.join(sys._MEIPASS, 'icons', icon_name)
            else:
                # 开发环境 - 使用项目目录
                return os.path.join(os.path.dirname(__file__), 'icons', icon_name)

        # 设置应用程序图标
        icon_file = get_app_icon_path('stock_icon.ico')
        if os.path.exists(icon_file):
            app_icon = QIcon(icon_file)
            app.setWindowIcon(app_icon)
            logging.info(f"成功加载应用图标: {icon_file}")
        else:
            # 尝试png格式
            icon_file_png = get_app_icon_path('stock_icon.png')
            if os.path.exists(icon_file_png):
                app_icon = QIcon(icon_file_png)
                app.setWindowIcon(app_icon)
                logging.info(f"成功加载应用图标(PNG): {icon_file_png}")
            else:
                logging.warning(f"图标文件不存在: {icon_file} 和 {icon_file_png}")

        # 获取图标目录路径（用于启动画面）
        if getattr(sys, 'frozen', False):
            # 打包环境 - 使用_internal目录
            icon_path = os.path.join(sys._MEIPASS, 'icons')
        else:
            # 开发环境 - 使用项目目录
            icon_path = os.path.join(os.path.dirname(__file__), 'icons')
            
        logging.info(f"图标目录路径: {icon_path}")
            
        # 创建并显示启动画面
        splash = None
        window = None
        
        try:
            splash_img = os.path.join(icon_path, 'splash.png')
            if os.path.exists(splash_img):
                splash = CustomSplashScreen(icon_path)
                splash.show()
                app.processEvents()
                logging.info("启动画面显示成功")
            else:
                logging.warning("未找到启动画面图片，跳过启动画面显示")
                
            # 创建主窗口
            window = KhQuantGUI()
            logging.info("主窗口创建成功")
            
            if splash:
                # 模拟加载过程
                loading_steps = [
                    (20, "正在初始化系统..."),
                    (40, "正在检查更新..."),
                    (60, "正在加载组件..."),
                    (80, "正在准备用户界面..."),
                    (100, "启动完成")
                ]
                
                for progress, message in loading_steps:
                    logging.info(f"加载进度: {progress}% - {message}")
                    splash.set_progress(progress, message)
                    app.processEvents()
                    time.sleep(0.05)  # 短暂延迟以显示进度
                
                # 关闭启动画面
                splash.close()
                logging.info("启动画面已关闭")
                app.processEvents()
            
            # 使用短延时确保启动画面完全关闭后再显示主窗口
            def show_main_window():
                try:
                    # 显示免责声明弹窗
                    disclaimer_dialog = DisclaimerDialog()
                    result = disclaimer_dialog.exec_()
                    
                    if result == QDialog.Rejected:
                        # 用户拒绝免责声明，退出程序
                        QApplication.quit()
                        return
                    
                    # 显示主窗口在主屏幕居中
                    window.center_window()  # 先居中
                    window.show()  # 然后显示
                    window.raise_()
                    window.activateWindow()
                except Exception as e:
                    logging.error(f"显示主窗口时出错: {str(e)}", exc_info=True)
                    QMessageBox.critical(None, "错误", f"显示主窗口时出错: {str(e)}")
                    QApplication.quit()
            
            QTimer.singleShot(100, show_main_window)
            
            # 添加全局异常处理和日志记录
            def exception_hook(exctype, value, tb):
                """全局异常钩子，确保所有未捕获的异常都被记录到日志文件"""
                import traceback as tb_module
                
                # 格式化异常信息
                error_msg = f"程序发生未捕获异常: {exctype.__name__}: {value}"
                tb_str = ''.join(tb_module.format_exception(exctype, value, tb))
                
                # 记录到日志文件（使用最高级别确保被记录）
                logging.critical(f"[CRASH] {error_msg}")
                logging.critical(f"[CRASH] 异常堆栈:\n{tb_str}")
                
                # 强制刷新日志缓冲区
                for handler in logging.getLogger().handlers:
                    if hasattr(handler, 'flush'):
                        handler.flush()
                
                # 打印到控制台（开发环境用）
                print(f'[CRITICAL ERROR] {error_msg}')
                print(f'[TRACEBACK]\n{tb_str}')
                
                # 调用默认异常钩子
                sys.__excepthook__(exctype, value, tb)
                
            sys.excepthook = exception_hook
            
            # 延迟执行更新检查
            QTimer.singleShot(2000, window.delayed_update_check)
            
            # 运行事件循环
            return app.exec_()
            
        except Exception as e:
            logging.error(f"初始化过程中出错: {str(e)}", exc_info=True)
            if window:
                window.close()
            if splash:
                splash.close()
            QMessageBox.critical(None, "初始化错误", 
                             f"程序初始化过程中出错:\n{str(e)}\n\n详细信息已写入日志文件")
            return 1
            
    except Exception as e:
        print(f"程序启动失败: {str(e)}")
        logging.critical(f"程序异常退出: {str(e)}", exc_info=True)
        return 1


if __name__ == "__main__":
    # 多进程保护必须最先执行
    import multiprocessing
    multiprocessing.freeze_support()  # Windows多进程支持

    # 设置多进程的启动方法
    try:
        multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        # 如果启动方法已经设置过，则跳过
        pass

    sys.exit(main())