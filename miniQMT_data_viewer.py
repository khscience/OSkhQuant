import sys
import os
import struct
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QMainWindow, QHBoxLayout, QVBoxLayout, 
                             QWidget, QTreeWidget, QTreeWidgetItem, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QMessageBox, QLabel,
                             QSplitter, QProgressBar, QStatusBar, QPushButton, QSizePolicy, QDialog, QDesktopWidget,
                             QComboBox, QDateEdit, QGroupBox, QCheckBox, QGridLayout, QTextEdit, QFileDialog, QInputDialog)
from PyQt5.QtCore import Qt, QSettings, QThread, pyqtSignal, QEvent, QDate, QMutex
from PyQt5.QtGui import QIcon, QFont, QColor
import pandas as pd
import logging


class LoadingDialog(QDialog):
    """加载进度对话框"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.initUI()
        
    def initUI(self):
        self.setWindowTitle("正在加载...")
        self.setFixedSize(300, 120)
        self.setModal(True)  # 设置为模态对话框
        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)  # 移除关闭按钮
        
        # 设置对话框样式
        self.setStyleSheet("""
            QDialog {
                background-color: #2b2b2b;
                border: 2px solid #4d4d4d;
                border-radius: 8px;
            }
            QLabel {
                color: #e8e8e8;
                font-size: 14px;
                font-weight: bold;
            }
            QProgressBar {
                background-color: #404040;
                border: 1px solid #4d4d4d;
                border-radius: 5px;
                text-align: center;
                font-size: 12px;
                color: #e8e8e8;
                height: 20px;
            }
            QProgressBar::chunk {
                background-color: #007acc;
                border-radius: 4px;
            }
        """)
        
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # 加载信息标签
        self.info_label = QLabel("正在加载数据，请稍候...")
        self.info_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.info_label)
        
        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # 设置为循环模式
        layout.addWidget(self.progress_bar)
        
        # 居中显示
        self.center_on_screen()
        
    def center_on_screen(self):
        """在主屏幕中央显示"""
        desktop = QDesktopWidget()
        # 使用主屏幕而不是跟随鼠标位置
        primary_screen = desktop.primaryScreen()
        screen = desktop.screenGeometry(primary_screen)
        x = (screen.width() - self.width()) // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)
        
    def set_message(self, message):
        """设置加载信息"""
        self.info_label.setText(message)
        
    def show_loading(self, message="正在加载数据，请稍候..."):
        """显示加载对话框"""
        self.set_message(message)
        self.center_on_screen()  # 每次显示时重新居中
        # 设置为模态对话框
        self.setWindowModality(Qt.ApplicationModal)
        self.show()
        self.raise_()
        self.activateWindow()
        QApplication.processEvents()  # 立即刷新界面
        print(f"Loading dialog shown: {message}")  # 调试信息
        
    def hide_loading(self):
        """隐藏加载对话框"""
        print("Loading dialog hidden")  # 调试信息
        self.hide()


# 自定义控件类，禁用滚轮事件
class NoWheelComboBox(QComboBox):
    """禁用滚轮事件的QComboBox"""
    def wheelEvent(self, event):
        event.ignore()


class NoWheelDateEdit(QDateEdit):
    """禁用滚轮事件的QDateEdit"""
    def wheelEvent(self, event):
        event.ignore()


class SupplementThread(QThread):
    """数据补充线程"""
    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str)
    error = pyqtSignal(str)
    status_update = pyqtSignal(str)

    def __init__(self, params, parent=None):
        super().__init__(parent)
        self.params = params
        self.running = True
        self.mutex = QMutex()
        self.supplement_stats = {
            'success_count': 0,
            'empty_data_count': 0,
            'error_count': 0,
            'empty_stocks': []
        }

    def run(self):
        try:
            if not self.isRunning():
                return
                
            # 参数验证
            if not self.params.get('stock_files'):
                raise ValueError("股票代码列表为空")

            def progress_callback(percent):
                if not self.isRunning():
                    raise InterruptedError("数据补充被中断")
                self.progress.emit(percent)
                    
            def log_callback(message):
                if not self.isRunning():
                    raise InterruptedError("用户中断")
                self.status_update.emit(message)

            def check_interrupt():
                return not self.isRunning()

            # 导入数据补充功能
            from khQTTools import supplement_history_data
            
            # 使用默认字段列表
            default_fields = ["open", "high", "low", "close", "volume", "amount"]
            
            supplement_history_data(
                stock_files=self.params['stock_files'],
                field_list=default_fields,
                period_type=self.params['period_type'],
                start_date=self.params['start_date'],
                end_date=self.params['end_date'],
                time_range='all',
                dividend_type='none',  # 固定不复权
                progress_callback=progress_callback,
                log_callback=log_callback,
                check_interrupt=check_interrupt
            )

            if self.isRunning():
                self.finished.emit(True, "数据补充完成！")
                
        except Exception as e:
            error_msg = f"补充数据过程中发生错误: {str(e)}"
            logging.error(error_msg, exc_info=True)
            if self.isRunning():
                self.error.emit(error_msg)
                self.finished.emit(False, error_msg)

    def stop(self):
        self.mutex.lock()
        self.running = False
        self.mutex.unlock()
        
    def isRunning(self):
        self.mutex.lock()
        result = self.running
        self.mutex.unlock()
        return result


from khQTTools import get_stock_names
from miniQMT_data_parser import MiniQMTDataParser


class DataLoadThread(QThread):
    """数据加载线程"""
    data_loaded = pyqtSignal(object)  # 传递加载的数据
    progress_updated = pyqtSignal(str)  # 传递进度信息
    error_occurred = pyqtSignal(str)  # 传递错误信息
    
    def __init__(self, file_path, data_type, data_dir=None):
        super().__init__()
        self.file_path = file_path
        self.data_type = data_type
        self.parser = MiniQMTDataParser(data_dir=data_dir)
        
    def run(self):
        try:
            self.progress_updated.emit("正在加载数据...")
            
            # 确保加载对话框有足够时间显示
            import time
            time.sleep(0.5)
            
            self.progress_updated.emit("正在解析数据文件...")
            
            if self.data_type == "tick":
                data = self.parser.parse_tick_data(self.file_path, 50000)
            elif self.data_type in ["1m", "5m", "1d"]:
                data = self.parser.parse_kline_data(self.file_path, self.data_type, 20000)
            else:
                raise ValueError(f"不支持的数据类型: {self.data_type}")
            
            self.progress_updated.emit("数据加载完成，正在准备显示...")
            time.sleep(0.2)  # 稍微延时以确保用户能看到进度
            
            self.data_loaded.emit(data)
            
        except Exception as e:
            import traceback
            error_msg = f"数据加载失败: {str(e)}\n{traceback.format_exc()}"
            print(f"DataLoadThread error: {error_msg}")  # 调试信息
            self.error_occurred.emit(str(e))


class MiniQMTDataViewer(QMainWindow):
    """miniQMT数据查看器"""
    
    def __init__(self):
        super().__init__()
        # 使用与主界面相同的QSettings参数
        self.settings = QSettings('KHQuant', 'StockAnalyzer')
        self.stock_names_cache = {}  # 股票名称缓存
        self.data_thread = None
        self.qmt_path = ''  # miniQMT路径
        
        # 数据补充相关
        self.supplement_thread = None
        
        # 创建加载对话框
        self.loading_dialog = LoadingDialog(self)
        
        # 保存当前数据状态用于刷新
        self.current_data_state = None
        
        self.initUI()
        self.load_data_structure()
    
    def center_main_window(self):
        """将主窗口居中显示在主屏幕上"""
        desktop = QDesktopWidget()
        # 使用主屏幕而不是跟随鼠标位置
        primary_screen = desktop.primaryScreen()
        screen = desktop.screenGeometry(primary_screen)
        x = (screen.width() - self.width()) // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)

    def initUI(self):
        """初始化用户界面"""
        # 设置窗口标题栏颜色（仅适用于Windows）
        if sys.platform == 'win32':
            try:
                from ctypes import windll, c_int, byref, sizeof
                from ctypes.wintypes import DWORD

                # 定义必要的Windows API常量
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
        
        self.setWindowTitle("miniQMT数据查看器 - 看海量化交易系统")
        self.setGeometry(100, 100, 1540, 800)  # 进一步增加宽度以适应优化后的左侧面板
        
        # 将窗口居中显示
        self.center_main_window()
        
        # 设置窗口图标
        try:
            self.setWindowIcon(QIcon("icons/stock_icon.png"))
        except:
            pass
        
        # 创建中心部件
        central_widget = QWidget()
        central_widget.setObjectName("centralWidget")
        central_widget.setStyleSheet("background-color: #2b2b2b;")
        self.setCentralWidget(central_widget)
        
        # 创建主布局
        main_layout = QHBoxLayout(central_widget)
        
        # 创建分割器
        splitter = QSplitter(Qt.Horizontal)
        splitter.setStyleSheet("""
            QSplitter {
                background-color: #2b2b2b;
            }
            QSplitter::handle {
                background-color: #404040;
                width: 3px;
                margin: 2px;
                border-radius: 1px;
            }
            QSplitter::handle:hover {
                background-color: #505050;
            }
            QSplitter::handle:pressed {
                background-color: #606060;
            }
        """)
        
        # 创建左侧数据补充工具
        supplement_widget = self.create_supplement_widget()
        supplement_widget.setMinimumWidth(300)  # 设置最小宽度而不是固定宽度，允许拖动调节
        supplement_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        
        # 创建中间树形控件
        self.tree_widget = QTreeWidget()
        self.tree_widget.setHeaderLabel("数据结构")
        self.tree_widget.setMinimumWidth(280)  # 设置最小宽度而不是固定宽度，允许拖动调节
        self.tree_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.tree_widget.itemClicked.connect(self.on_tree_item_clicked)
        self.tree_widget.setRootIsDecorated(True)  # 显示根节点装饰
        self.tree_widget.setItemsExpandable(True)  # 允许展开
        self.tree_widget.setExpandsOnDoubleClick(True)  # 双击展开
        self.tree_widget.setAnimated(True)  # 启用动画效果
        
        # 设置树形控件的默认展开状态
        # self.tree_widget.expandAll()  # 默认收起所有节点，让用户手动展开
        
        # 设置表头样式
        header = self.tree_widget.header()
        header.setStyleSheet("""
            QHeaderView::section {
                background-color: #404040;
                color: #e8e8e8;
                padding: 12px;
                border: none;
                border-right: 1px solid #4d4d4d;
                border-bottom: 1px solid #4d4d4d;
                font-weight: bold;
                font-size: 16px;
            }
        """)
        
        # 设置树形控件样式
        self.tree_widget.setStyleSheet("""
            QTreeWidget {
                background-color: #333333;
                color: #e8e8e8;
                border: 1px solid #404040;
                font-size: 16px;
                outline: 0;
                margin: 0px;
                padding: 0px;
            }
            QTreeWidget::item {
                padding: 12px 10px;
                background-color: transparent;
                border: none;
                border-bottom: 1px solid #3a3a3a;
                font-size: 16px;
            }
            QTreeWidget::item:hover {
                background-color: #454545;
            }
            QTreeWidget::item:selected {
                background-color: #505050;
                color: #ffffff;
            }
            QTreeWidget::item:selected:active {
                background-color: #555555;
            }
            QTreeWidget::branch {
                background-color: transparent;
                width: 20px;
                height: 20px;
            }
            QTreeWidget::branch:hover {
                background-color: #454545;
                border-radius: 3px;
            }
            /* 使用三角形箭头作为展开/收起指示器 */
            QTreeWidget::branch:has-children:!has-siblings:closed,
            QTreeWidget::branch:closed:has-children:has-siblings {
                border-image: none;
                image: none;
                background: transparent;
                width: 16px;
                height: 16px;
                margin: 2px;
                border-left: 4px solid #aaaaaa;
                border-top: 3px solid transparent;
                border-bottom: 3px solid transparent;
                border-right: none;
            }
            QTreeWidget::branch:has-children:!has-siblings:closed:hover,
            QTreeWidget::branch:closed:has-children:has-siblings:hover {
                border-left-color: #cccccc;
            }
            QTreeWidget::branch:open:has-children:!has-siblings,
            QTreeWidget::branch:open:has-children:has-siblings {
                border-image: none;
                image: none;
                background: transparent;
                width: 16px;
                height: 16px;
                margin: 2px;
                border-top: 4px solid #aaaaaa;
                border-left: 3px solid transparent;
                border-right: 3px solid transparent;
                border-bottom: none;
            }
            QTreeWidget::branch:open:has-children:!has-siblings:hover,
            QTreeWidget::branch:open:has-children:has-siblings:hover {
                border-top-color: #cccccc;
            }
            QTreeWidget QScrollBar {
                background-color: #333333;
            }
            QTreeWidget QHeaderView {
                background-color: #333333;
            }
            QTreeWidget QHeaderView::section {
                background-color: #404040;
                color: #e8e8e8;
                padding: 12px;
                border: none;
                border-right: 1px solid #4d4d4d;
                border-bottom: 1px solid #4d4d4d;
                font-weight: bold;
                font-size: 16px;
            }
            QTreeWidget QHeaderView::section:hover {
                background-color: #454545;
            }
        """)
        

        
        # 创建右侧区域
        right_widget = QWidget()
        right_widget.setStyleSheet("background-color: #2b2b2b;")
        right_layout = QVBoxLayout(right_widget)
        
        # 创建面包屑导航栏
        self.breadcrumb_widget = QWidget()
        self.breadcrumb_widget.setStyleSheet("""
            QWidget {
                background-color: #404040;
                border: 1px solid #4d4d4d;
                border-radius: 6px;
                padding: 8px;
                margin-bottom: 8px;
            }
        """)
        self.breadcrumb_layout = QHBoxLayout(self.breadcrumb_widget)
        self.breadcrumb_layout.setContentsMargins(12, 8, 12, 8)
        self.breadcrumb_layout.setSpacing(8)
        
        # 添加默认的"首页"标签
        self.breadcrumb_items = []
        self.update_breadcrumb([])
        
        right_layout.addWidget(self.breadcrumb_widget)
        
        # 创建信息面板
        info_panel = QWidget()
        info_panel.setStyleSheet("background-color: #2b2b2b;")
        info_layout = QHBoxLayout(info_panel)
        info_layout.setContentsMargins(0, 0, 0, 0)
        
        # 创建信息标签
        self.info_label = QLabel("请选择要查看的数据")
        self.info_label.setStyleSheet("""
            QLabel {
                color: #e8e8e8;
                font-size: 16px;
                font-weight: bold;
                padding: 12px;
                background-color: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #404040, stop:1 #4a4a4a
                );
                border: 1px solid #4d4d4d;
                border-radius: 6px;
            }
        """)
        info_layout.addWidget(self.info_label)
        
        # 创建统计信息标签
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("""
            QLabel {
                color: #b0b0b0;
                font-size: 14px;
                padding: 8px;
                background-color: #3a3a3a;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                min-width: 150px;
            }
        """)
        info_layout.addWidget(self.stats_label)
        
        # 创建数据类型说明标签
        self.data_type_label = QLabel("说明: 蓝色列名 = 二次计算数据，彩色数据是二次计算的")
        self.data_type_label.setStyleSheet("""
            QLabel {
                color: #2E6DA4;
                font-size: 13px;
                font-weight: bold;
                padding: 8px;
                background-color: #2a3a4a;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                min-width: 220px;
            }
        """)
        self.data_type_label.setVisible(False)  # 初始隐藏，有计算字段时显示
        info_layout.addWidget(self.data_type_label)
        
        # 创建刷新按钮
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setVisible(False)  # 初始隐藏，有数据时显示
        self.refresh_button.setStyleSheet("""
            QPushButton {
                background-color: #007acc;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 14px;
                font-weight: bold;
                min-width: 60px;
            }
            QPushButton:hover {
                background-color: #0088dd;
            }
            QPushButton:pressed {
                background-color: #0066aa;
            }
        """)
        self.refresh_button.clicked.connect(self.refresh_current_data)
        info_layout.addWidget(self.refresh_button)
        
        right_layout.addWidget(info_panel)
        
        # 创建数据表格
        self.table_widget = QTableWidget()
        self.table_widget.setAlternatingRowColors(True)  # 启用交替行颜色
        self.table_widget.setSelectionBehavior(QTableWidget.SelectRows)  # 选择整行
        self.table_widget.setSelectionMode(QTableWidget.ExtendedSelection)  # 多选模式
        self.table_widget.setSortingEnabled(True)  # 启用排序
        self.table_widget.setShowGrid(True)  # 显示网格线
        self.table_widget.setEditTriggers(QTableWidget.NoEditTriggers)  # 禁用编辑
        self.table_widget.setStyleSheet("""
            QTableWidget {
                background-color: #333333;
                alternate-background-color: #383838;
                color: #e8e8e8;
                gridline-color: #404040;
                border: 1px solid #404040;
                selection-background-color: #505050;
                font-size: 14px;
            }
            QTableWidget::item {
                padding: 10px;
                background-color: transparent;
                border: none;
            }
            QTableWidget::item:selected {
                background-color: #505050;
                color: #ffffff;
            }
            QHeaderView::section {
                background-color: #404040;
                color: #e8e8e8;
                padding: 12px;
                border: none;
                border-right: 1px solid #4d4d4d;
                border-bottom: 1px solid #4d4d4d;
                font-weight: bold;
                font-size: 14px;
            }
            QHeaderView::section:hover {
                background-color: #454545;
            }
            QHeaderView::section:pressed {
                background-color: #505050;
            }
            /* 表格左上角按钮样式 */
            QTableCornerButton::section {
                background-color: #404040;
                border: none;
                border-right: 1px solid #4d4d4d;
                border-bottom: 1px solid #4d4d4d;
            }
            QTableCornerButton::section:hover {
                background-color: #454545;
            }
            QTableCornerButton::section:pressed {
                background-color: #505050;
            }
        """)
        right_layout.addWidget(self.table_widget)
        
        # 设置表格的鼠标事件
        self.setup_table_mouse_events()
        
        # 添加到分割器
        splitter.addWidget(supplement_widget)
        splitter.addWidget(self.tree_widget)
        splitter.addWidget(right_widget)
        
        # 设置初始比例和拉伸因子，使三个区域都可以调节大小
        splitter.setSizes([460, 280, 800])  # 设置初始宽度比例
        splitter.setStretchFactor(0, 1)  # 左侧数据补充工具可调节
        splitter.setStretchFactor(1, 1)  # 中间树形控件可调节  
        splitter.setStretchFactor(2, 2)  # 右侧数据展示区拉伸因子更大，优先伸缩
        
        main_layout.addWidget(splitter)
        
        # 创建状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        
        # 创建进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)
        
        # 设置整体样式
        self.setStyleSheet("""
            /* 主窗口和基础样式 */
            QMainWindow {
                background-color: #2b2b2b;
                color: #e8e8e8;
            }
            
            QWidget {
                background-color: #2b2b2b;
                color: #e8e8e8;
            }
            
            /* 确保所有子组件背景色 */
            QWidget#centralWidget {
                background-color: #2b2b2b;
            }
            
            /* 强制所有QWidget使用深色背景 */
            QWidget * {
                background-color: transparent;
            }
            
            /* 布局背景色 */
            QHBoxLayout, QVBoxLayout {
                background-color: #2b2b2b;
            }
            
            /* 分割器背景色 */
            QSplitter {
                background-color: #2b2b2b;
            }
            

            /* 状态栏样式 */
            QStatusBar {
                background-color: #3a3a3a;
                color: #e8e8e8;
                border-top: 1px solid #4d4d4d;
                padding: 3px;
                font-size: 14px;
            }
            
            /* 进度条样式 */
            QProgressBar {
                background-color: #404040;
                border: 1px solid #4d4d4d;
                border-radius: 5px;
                text-align: center;
                font-size: 14px;
                color: #e8e8e8;
            }
            QProgressBar::chunk {
                background-color: #007acc;
                border-radius: 4px;
            }
            
            /* 滚动条样式 */
            QScrollBar:vertical {
                background-color: #3a3a3a;
                width: 15px;
                border: none;
            }
            QScrollBar::handle:vertical {
                background-color: #5a5a5a;
                border-radius: 7px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #6a6a6a;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                border: none;
                background: none;
            }
            QScrollBar:horizontal {
                background-color: #3a3a3a;
                height: 15px;
                border: none;
            }
            QScrollBar::handle:horizontal {
                background-color: #5a5a5a;
                border-radius: 7px;
                min-width: 20px;
            }
            QScrollBar::handle:horizontal:hover {
                background-color: #6a6a6a;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                border: none;
                background: none;
            }
            
            /* 分割器样式 */
            QSplitter::handle {
                background-color: #404040;
            }
            QSplitter::handle:horizontal {
                width: 2px;
            }
            QSplitter::handle:vertical {
                height: 2px;
            }
            
            /* 工具提示样式 */
            QToolTip {
                background-color: #555555;
                color: #e8e8e8;
                border: 1px solid #666666;
                padding: 4px;
                border-radius: 3px;
                font-size: 14px;
            }
            
            /* 表格角落按钮全局样式 */
            QTableCornerButton::section {
                background-color: #404040 !important;
                border: none !important;
                border-right: 1px solid #4d4d4d !important;
                border-bottom: 1px solid #4d4d4d !important;
            }
            QTableCornerButton::section:hover {
                background-color: #454545 !important;
            }
            QTableCornerButton::section:pressed {
                background-color: #505050 !important;
            }
            
            /* 树形控件表头样式 */
            QTreeWidget QHeaderView::section {
                background-color: #404040 !important;
                color: #e8e8e8 !important;
                padding: 12px !important;
                border: none !important;
                border-right: 1px solid #4d4d4d !important;
                border-bottom: 1px solid #4d4d4d !important;
                font-weight: bold !important;
                font-size: 16px !important;
            }
            QTreeWidget QHeaderView::section:hover {
                background-color: #454545 !important;
            }
            QTreeWidget QHeaderView::section:pressed {
                background-color: #505050 !important;
            }
        """)
        
    def load_data_structure(self):
        """加载数据结构"""
        # 获取miniQMT路径（从主界面设置中读取）
        self.qmt_path = self.settings.value('qmt_path', '')
        
        if not self.qmt_path:
            self.status_bar.showMessage("未设置miniQMT路径，请先在主界面设置中配置miniQMT路径")
            QMessageBox.information(self, "提示", 
                "请先在主界面的设置中配置miniQMT路径后再使用数据查看器。\n\n"
                "设置路径：主界面 → 设置按钮 → 客户端设置 → miniQMT路径")
            return
        
        # 调试信息：显示读取到的路径
        print(f"读取到的miniQMT路径: {self.qmt_path}")
        logging.info(f"读取到的miniQMT路径: {self.qmt_path}")
            
        # 检查datadir文件夹
        datadir_path = os.path.join(self.qmt_path, 'datadir')
        if not os.path.exists(datadir_path):
            QMessageBox.warning(self, "错误", f"未找到datadir文件夹：{datadir_path}")
            return
            
        self.datadir_path = datadir_path
        self.status_bar.showMessage(f"数据路径：{datadir_path}")
        
        # 加载股票名称
        self.load_stock_names()
        
        # 构建树形结构
        self.build_tree_structure()
        
    def load_stock_names(self):
        """加载股票名称映射"""
        try:
            stock_list_file = os.path.join(os.path.dirname(__file__), 'data', '全部股票_股票列表.csv')
            if os.path.exists(stock_list_file):
                with open(stock_list_file, 'r', encoding='utf-8-sig') as f:
                    for line in f:
                        if line.strip():
                            parts = line.strip().split(',')
                            if len(parts) >= 2:
                                code = parts[0].strip()
                                name = parts[1].strip()
                                self.stock_names_cache[code] = name
        except Exception as e:
            logging.warning(f"加载股票名称失败: {e}")
    
    def format_file_size(self, size_bytes):
        """格式化文件大小为MB"""
        if size_bytes == 0:
            return "0 MB"
        size_mb = size_bytes / (1024 * 1024)
        if size_mb < 0.01:
            return "< 0.01 MB"
        return f"{size_mb:.2f} MB"
            
    def get_stock_name(self, stock_code):
        """获取股票名称"""
        # 去掉后缀
        code_without_suffix = stock_code.split('.')[0]
        
        # 尝试不同的格式
        possible_codes = [
            stock_code,  # 原始代码
            code_without_suffix + '.SH',  # 上海
            code_without_suffix + '.SZ',  # 深圳
            code_without_suffix + '.BJ',  # 北京
            code_without_suffix,  # 纯数字
        ]
        
        for code in possible_codes:
            if code in self.stock_names_cache:
                return self.stock_names_cache[code]
                
        return stock_code  # 如果找不到名称，返回原始代码
    
    def set_clickable_style(self, item):
        """设置可点击项的样式"""
        if item:
            # 设置超链接样式
            font = item.font()
            font.setUnderline(True)
            item.setFont(font)
            
            # 设置颜色
            item.setForeground(QColor('#4A90E2'))  # 蓝色
            
            # 设置鼠标指针样式
            item.setData(Qt.UserRole + 1, True)  # 标记为可点击
    
    def setup_table_mouse_events(self):
        """设置表格的鼠标事件"""
        # 启用鼠标跟踪
        self.table_widget.setMouseTracking(True)
        
        # 安装事件过滤器
        self.table_widget.installEventFilter(self)
    
    def eventFilter(self, source, event):
        """事件过滤器，处理鼠标悬停"""
        if source == self.table_widget:
            if event.type() == QEvent.MouseMove:
                # 获取鼠标位置的项
                item = self.table_widget.itemAt(event.pos())
                if item and item.data(Qt.UserRole + 1):
                    # 这是可点击项，设置手型指针
                    self.table_widget.setCursor(Qt.PointingHandCursor)
                else:
                    # 恢复默认指针
                    self.table_widget.setCursor(Qt.ArrowCursor)
        
        return super().eventFilter(source, event)
    
    def update_breadcrumb(self, path_items):
        """更新面包屑导航"""
        # 清空现有的面包屑项
        for item in self.breadcrumb_items:
            self.breadcrumb_layout.removeWidget(item)
            item.deleteLater()
        self.breadcrumb_items.clear()
        
        # 添加"首页"
        is_home = len(path_items) == 0
        home_btn = self.create_breadcrumb_item("首页", None, is_home)
        self.breadcrumb_layout.addWidget(home_btn)
        self.breadcrumb_items.append(home_btn)
        
        # 添加路径项
        for i, path_item in enumerate(path_items):
            # 添加分隔符
            separator = QLabel(" > ")
            separator.setStyleSheet("color: #888888; font-size: 14px;")
            self.breadcrumb_layout.addWidget(separator)
            self.breadcrumb_items.append(separator)
            
            # 添加路径项
            is_last = (i == len(path_items) - 1)
            item_btn = self.create_breadcrumb_item(path_item['name'], path_item['data'], is_last)
            self.breadcrumb_layout.addWidget(item_btn)
            self.breadcrumb_items.append(item_btn)
        
        # 添加伸缩项
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.breadcrumb_layout.addWidget(spacer)
        self.breadcrumb_items.append(spacer)
    
    def create_breadcrumb_item(self, text, data, is_current=False):
        """创建面包屑项"""
        if is_current:
            # 当前项显示为普通标签
            label = QLabel(text)
            label.setStyleSheet("""
                QLabel {
                    color: #ffffff;
                    font-size: 14px;
                    font-weight: bold;
                    padding: 6px 10px;
                    background-color: #555555;
                    border-radius: 4px;
                }
            """)
            return label
        else:
            # 可点击的项显示为按钮
            btn = QPushButton(text)
            btn.setStyleSheet("""
                QPushButton {
                    color: #4A90E2;
                    font-size: 14px;
                    border: none;
                    padding: 6px 10px;
                    background-color: transparent;
                    text-decoration: underline;
                }
                QPushButton:hover {
                    background-color: #505050;
                    border-radius: 4px;
                }
                QPushButton:pressed {
                    background-color: #555555;
                }
            """)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda: self.on_breadcrumb_clicked(data))
            return btn
    
    def on_breadcrumb_clicked(self, data):
        """面包屑点击事件"""
        if data is None:
            # 点击"首页"，回到初始状态
            self.update_breadcrumb([])
            self.info_label.setText("请选择要查看的数据")
            self.table_widget.setRowCount(0)
            self.table_widget.setColumnCount(0)
            self.stats_label.setText("")
            self.data_type_label.setVisible(False)  # 隐藏说明标签
            self.refresh_button.setVisible(False)  # 隐藏刷新按钮
            self.current_data_state = None  # 清除当前数据状态
            self.scroll_to_top()  # 滚动条回到顶部
            return
        
        # 根据数据类型进行相应的操作
        data_type = data.get('type')
        if data_type == 'exchange':
            # 回到交易所级别，显示该交易所的数据
            self.update_breadcrumb([{'name': data.get('name'), 'data': data}])
            self.info_label.setText(f"已选择：{data.get('name')}")
            self.table_widget.setRowCount(0)
            self.table_widget.setColumnCount(0)
            self.stats_label.setText("")
            self.refresh_button.setVisible(False)  # 隐藏刷新按钮
            self.current_data_state = None  # 清除当前数据状态
        elif data_type == 'period':
            # 回到周期级别，显示该周期的数据
            exchange_name = self.get_exchange_name(data.get('exchange'))
            period_name = self.get_period_name(data.get('period'))
            breadcrumb_path = [
                {'name': exchange_name, 'data': {'type': 'exchange', 'name': exchange_name}},
                {'name': period_name, 'data': data}
            ]
            self.update_breadcrumb(breadcrumb_path)
            
            # 重新加载该周期的数据
            if data.get('period') == '0':
                self.show_tick_stock_list(data)
            else:
                self.show_period_files(data)
        elif data_type == 'stock':
            # 回到股票级别，显示该股票的数据
            exchange_name = self.get_exchange_name(data.get('exchange'))
            period_name = self.get_period_name(data.get('period'))
            stock_name = f"{data.get('stock_code')} - {self.get_stock_name(data.get('full_code'))}"
            breadcrumb_path = [
                {'name': exchange_name, 'data': {'type': 'exchange', 'name': exchange_name}},
                {'name': period_name, 'data': {'type': 'period', 'exchange': data.get('exchange'), 'period': data.get('period'), 'path': data.get('period_path')}},
                {'name': stock_name, 'data': data}
            ]
            self.update_breadcrumb(breadcrumb_path)
            
            # 重新加载该股票的数据
            if data.get('period') == '0':
                self.show_tick_date_files(data)
            else:
                # K线数据直接加载
                self.load_kline_data_file(data.get('file_path'), self.get_period_name(data.get('period')), data.get('stock_code'))
    
    def get_exchange_name(self, exchange_code):
        """获取交易所名称"""
        exchange_map = {'BJ': '北交所', 'SH': '上交所', 'SZ': '深交所'}
        return exchange_map.get(exchange_code, exchange_code)
    
    def get_period_name(self, period_code):
        """获取周期名称"""
        period_map = {'0': 'tick数据', '60': '1m数据', '300': '5m数据', '86400': '日线数据'}
        return period_map.get(period_code, period_code)
        
    def build_tree_structure(self):
        """构建树形结构"""
        self.tree_widget.clear()
        
        # 交易所映射
        exchanges = {
            'BJ': '北交所',
            'SH': '上交所', 
            'SZ': '深交所'
        }
        
        # 数据周期映射
        periods = {
            '0': 'tick数据',
            '60': '1m数据',
            '300': '5m数据',
            '86400': '日线数据'
        }
        
        for exchange_code, exchange_name in exchanges.items():
            exchange_path = os.path.join(self.datadir_path, exchange_code)
            if not os.path.exists(exchange_path):
                continue
                
            # 创建交易所节点
            exchange_item = QTreeWidgetItem([exchange_name])
            exchange_item.setData(0, Qt.UserRole, {'type': 'exchange', 'code': exchange_code})
            self.tree_widget.addTopLevelItem(exchange_item)
            
            # 遍历数据周期
            for period_code, period_name in periods.items():
                period_path = os.path.join(exchange_path, period_code)
                if not os.path.exists(period_path):
                    continue
                    
                # 创建周期节点
                period_item = QTreeWidgetItem([period_name])
                period_item.setData(0, Qt.UserRole, {
                    'type': 'period', 
                    'exchange': exchange_code,
                    'period': period_code,
                    'path': period_path
                })
                exchange_item.addChild(period_item)
                
                # 移除tick数据的特殊处理，统一在右侧显示股票列表
            
    def on_tree_item_clicked(self, item, column):
        """树形控件点击事件"""
        data = item.data(0, Qt.UserRole)
        if not data:
            return
            
        item_type = data.get('type')
        
        if item_type == 'period':
            # 统一处理：所有周期数据都在右侧显示股票列表
            if data.get('period') == '0':
                # tick数据
                self.show_tick_stock_list(data)
            else:
                # K线数据
                self.show_period_files(data)
        elif item_type == 'exchange':
            # 交易所级别的点击 - 切换展开/收起状态
            if item.isExpanded():
                item.setExpanded(False)
            else:
                item.setExpanded(True)
            
            # 更新面包屑和界面
            exchange_name = item.text(0)
            self.update_breadcrumb([{'name': exchange_name, 'data': {'type': 'exchange', 'name': exchange_name}}])
            self.info_label.setText(f"已选择：{exchange_name}")
            self.table_widget.setRowCount(0)
            self.table_widget.setColumnCount(0)
            self.stats_label.setText("")
            self.data_type_label.setVisible(False)  # 隐藏说明标签
            self.refresh_button.setVisible(False)  # 隐藏刷新按钮
            self.current_data_state = None  # 清除当前数据状态
            self.scroll_to_top()  # 滚动条回到顶部
        else:
            # 其他情况 - 可能是顶级节点
            exchange_name = item.text(0)
            
            # 如果有子项，切换展开/收起状态
            if item.childCount() > 0:
                if item.isExpanded():
                    item.setExpanded(False)
                else:
                    item.setExpanded(True)
            
            self.update_breadcrumb([{'name': exchange_name, 'data': {'type': 'exchange', 'name': exchange_name}}])
            self.info_label.setText(f"已选择：{exchange_name}")
            self.table_widget.setRowCount(0)
            self.table_widget.setColumnCount(0)
            self.stats_label.setText("")
            self.data_type_label.setVisible(False)  # 隐藏说明标签
            self.refresh_button.setVisible(False)  # 隐藏刷新按钮
            self.current_data_state = None  # 清除当前数据状态
            self.scroll_to_top()  # 滚动条回到顶部
            
    def show_period_files(self, data):
        """显示周期数据文件列表"""
        period_path = data.get('path')
        period_code = data.get('period')
        exchange_code = data.get('exchange')
        
        # 显示加载对话框
        self.loading_dialog.show_loading(f"正在扫描{self.get_period_name(period_code)}数据文件...")
        
        # 映射周期代码到数据类型
        period_type_map = {
            '60': '1m',
            '300': '5m', 
            '86400': '1d'
        }
        period_type = period_type_map.get(period_code, period_code)
        
        # 更新面包屑导航
        exchange_name = self.get_exchange_name(exchange_code)
        period_name = self.get_period_name(period_code)
        breadcrumb_path = [
            {'name': exchange_name, 'data': {'type': 'exchange', 'name': exchange_name}},
            {'name': period_name, 'data': data}
        ]
        self.update_breadcrumb(breadcrumb_path)
        
        # 使用QTimer延时处理，避免阻塞主线程
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(50, lambda: self._do_show_period_files(period_path, period_code, exchange_code, period_type))
    
    def _do_show_period_files(self, period_path, period_code, exchange_code, period_type):
        """实际执行文件扫描的方法"""
        # 显示该周期下的所有数据文件
        try:
            parser = MiniQMTDataParser()
            # 对于K线数据，查找.DAT文件（不区分大小写）
            files_info = parser.get_data_files(period_path, '.DAT')
            
            self.info_label.setText(f"找到{len(files_info)}个股票数据文件 - 单击股票代码查看数据内容")
            
            # 在表格中显示文件列表
            self.table_widget.setRowCount(len(files_info))
            self.table_widget.setColumnCount(4)
            self.table_widget.setHorizontalHeaderLabels(['股票代码', '股票名称', '文件大小', '修改时间'])
            
            # 保存文件信息供双击使用
            self.current_files_info = []
            self.current_period_type = period_type
            
            for i, file_info in enumerate(files_info):
                # 从文件名提取股票代码
                filename = file_info['filename']
                stock_code = filename.replace('.DAT', '').replace('.dat', '')
                
                # 构造完整的股票代码
                if exchange_code == 'SH':
                    full_code = stock_code + '.SH'
                elif exchange_code == 'SZ':
                    full_code = stock_code + '.SZ'
                else:
                    full_code = stock_code + '.BJ'
                
                # 获取股票名称
                stock_name = self.get_stock_name(full_code)
                
                # 股票代码
                code_item = QTableWidgetItem(stock_code)
                code_item.setData(Qt.UserRole, {
                    'file_path': file_info['path'],
                    'period_type': period_type,
                    'period': period_code,  # 添加period字段，用于面包屑导航
                    'exchange': exchange_code,
                    'stock_code': stock_code,
                    'full_code': full_code
                })
                self.table_widget.setItem(i, 0, code_item)
                
                # 股票名称
                self.table_widget.setItem(i, 1, QTableWidgetItem(stock_name))
                
                # 文件大小
                size_str = self.format_file_size(file_info['size'])
                self.table_widget.setItem(i, 2, QTableWidgetItem(size_str))
                
                # 修改时间
                self.table_widget.setItem(i, 3, QTableWidgetItem(file_info['mtime_str']))
                
                # 注释掉记录数检测，避免阻塞主线程
                # format_info = parser.detect_file_format(file_info['path'])
                # record_count = format_info.get('record_count', 0)
                # self.table_widget.setItem(i, 4, QTableWidgetItem(str(record_count)))
                
                self.current_files_info.append(file_info)
            
            # 调整列宽
            self.table_widget.resizeColumnsToContents()
            
            # 断开之前的点击事件连接，避免重复连接
            try:
                self.table_widget.itemClicked.disconnect()
            except:
                pass
            
            # 连接单击事件
            self.table_widget.itemClicked.connect(self.on_file_clicked)
            
            # 为可点击的单元格设置超链接样式
            for i in range(self.table_widget.rowCount()):
                # 股票代码列设置为超链接样式
                item = self.table_widget.item(i, 0)
                if item:
                    self.set_clickable_style(item)
            
            # 隐藏加载对话框
            self.loading_dialog.hide_loading()
            
            # 保存当前数据状态
            self.current_data_state = {
                'type': 'period_files',
                'data': {
                    'path': period_path,
                    'period': period_code,
                    'exchange': exchange_code
                }
            }
            
            # 显示刷新按钮
            self.refresh_button.setVisible(True)
            
            # 滚动条回到顶部
            self.scroll_to_top()
            
        except Exception as e:
            # 隐藏加载对话框
            self.loading_dialog.hide_loading()
            self.info_label.setText(f"读取文件列表失败: {e}")
    
    def show_tick_stock_list(self, data):
        """显示tick数据的股票列表"""
        period_path = data.get('path')
        exchange_code = data.get('exchange')
        
        # 显示加载对话框
        self.loading_dialog.show_loading("正在扫描tick数据股票列表...")
        
        # 更新面包屑导航
        exchange_name = self.get_exchange_name(exchange_code)
        period_name = self.get_period_name('0')  # tick数据
        breadcrumb_path = [
            {'name': exchange_name, 'data': {'type': 'exchange', 'name': exchange_name}},
            {'name': period_name, 'data': data}
        ]
        self.update_breadcrumb(breadcrumb_path)
        
        # 使用QTimer延时处理，避免阻塞主线程
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(50, lambda: self._do_show_tick_stock_list(period_path, exchange_code))
    
    def _do_show_tick_stock_list(self, period_path, exchange_code):
        """实际执行tick股票扫描的方法"""
        try:
            # 查找股票代码文件夹
            stock_folders = []
            if os.path.exists(period_path):
                for item in os.listdir(period_path):
                    item_path = os.path.join(period_path, item)
                    if os.path.isdir(item_path) and item.isdigit() and len(item) == 6:
                        # 检查文件夹内是否有数据文件
                        has_data = False
                        for dat_file in os.listdir(item_path):
                            if dat_file.endswith('.dat'):
                                has_data = True
                                break
                        if has_data:
                            stock_folders.append({
                                'code': item,
                                'path': item_path,
                                'exchange': exchange_code
                            })
            
            # 排序股票代码
            stock_folders.sort(key=lambda x: x['code'])
            
            # 显示全部股票
            display_count = len(stock_folders)
            
            self.info_label.setText(f"找到{len(stock_folders)}只股票的tick数据 - 单击股票代码查看数据内容")
            
            # 在表格中显示股票列表
            self.table_widget.setRowCount(display_count)
            self.table_widget.setColumnCount(4)
            self.table_widget.setHorizontalHeaderLabels(['股票代码', '股票名称', '数据文件数', '最新日期'])
            
            # 保存文件信息供双击使用
            self.current_files_info = []
            self.current_period_type = 'tick'
            
            for i, stock_info in enumerate(stock_folders):
                stock_code = stock_info['code']
                stock_path = stock_info['path']
                exchange = stock_info['exchange']
                
                # 构造完整的股票代码
                if exchange == 'SH':
                    full_code = stock_code + '.SH'
                elif exchange == 'SZ':
                    full_code = stock_code + '.SZ'
                else:
                    full_code = stock_code + '.BJ'
                
                # 获取股票名称
                stock_name = self.get_stock_name(full_code)
                
                # 简化文件统计，避免阻塞主线程
                dat_files = []
                file_count = 0
                latest_date = '无'
                
                if os.path.exists(stock_path):
                    try:
                        # 快速扫描文件夹，只统计数量和最新日期
                        all_files = os.listdir(stock_path)
                        dat_files = [f for f in all_files if f.endswith('.dat')]
                        file_count = len(dat_files)
                        
                        if dat_files:
                            dat_files.sort()
                            latest_date = dat_files[-1].replace('.dat', '')
                    except:
                        pass
                
                # 股票代码
                code_item = QTableWidgetItem(stock_code)
                code_item.setData(Qt.UserRole, {
                    'file_path': stock_path,
                    'period_type': 'tick',
                    'exchange': exchange,
                    'stock_code': stock_code,
                    'full_code': full_code,
                    'dat_files': dat_files
                })
                self.table_widget.setItem(i, 0, code_item)
                
                # 股票名称
                self.table_widget.setItem(i, 1, QTableWidgetItem(stock_name))
                
                # 数据文件数
                self.table_widget.setItem(i, 2, QTableWidgetItem(str(file_count)))
                
                # 最新日期
                self.table_widget.setItem(i, 3, QTableWidgetItem(latest_date))
                
                # 不再显示文件夹大小，避免阻塞主线程
                # self.table_widget.setItem(i, 4, QTableWidgetItem("-"))
                
                self.current_files_info.append(stock_info)
            
            # 调整列宽
            self.table_widget.resizeColumnsToContents()
            
            # 断开之前的点击事件连接，避免重复连接
            try:
                self.table_widget.itemClicked.disconnect()
            except:
                pass
            
            # 连接单击事件
            self.table_widget.itemClicked.connect(self.on_file_clicked)
            
            # 为可点击的单元格设置超链接样式
            for i in range(self.table_widget.rowCount()):
                # 股票代码列设置为超链接样式
                item = self.table_widget.item(i, 0)
                if item:
                    self.set_clickable_style(item)
            
            # 隐藏加载对话框
            self.loading_dialog.hide_loading()
            
            # 保存当前数据状态
            self.current_data_state = {
                'type': 'tick_stock_list',
                'data': {
                    'path': period_path,
                    'exchange': exchange_code,
                    'period': '0'
                }
            }
            
            # 显示刷新按钮
            self.refresh_button.setVisible(True)
            
            # 滚动条回到顶部
            self.scroll_to_top()
            
        except Exception as e:
            # 隐藏加载对话框
            self.loading_dialog.hide_loading()
            self.info_label.setText(f"读取tick股票列表失败: {e}")
    
    def show_tick_date_files(self, stock_data):
        """显示tick数据的日期文件列表"""
        stock_path = stock_data.get('file_path')
        stock_code = stock_data.get('stock_code')
        full_code = stock_data.get('full_code')
        exchange = stock_data.get('exchange')
        
        stock_name = self.get_stock_name(full_code)
        
        # 显示加载对话框
        self.loading_dialog.show_loading(f"正在加载 {stock_code} - {stock_name} 的日期文件列表...")
        
        # 更新面包屑导航
        exchange_name = self.get_exchange_name(exchange)
        period_name = self.get_period_name('0')  # tick数据
        stock_display_name = f"{stock_code} - {stock_name}"
        breadcrumb_path = [
            {'name': exchange_name, 'data': {'type': 'exchange', 'name': exchange_name}},
            {'name': period_name, 'data': {'type': 'period', 'exchange': exchange, 'period': '0', 'path': os.path.dirname(stock_path)}},
            {'name': stock_display_name, 'data': {
                'type': 'stock', 
                'stock_code': stock_code,
                'full_code': full_code,
                'exchange': exchange,
                'period': '0',
                'file_path': stock_path,
                'period_path': os.path.dirname(stock_path)
            }}
        ]
        self.update_breadcrumb(breadcrumb_path)
        
        try:
            # 获取该股票文件夹内的所有日期文件
            date_files = []
            if os.path.exists(stock_path):
                for file in os.listdir(stock_path):
                    if file.endswith('.dat'):
                        file_path = os.path.join(stock_path, file)
                        file_size = os.path.getsize(file_path)
                        file_mtime = os.path.getmtime(file_path)
                        
                        # 解析日期
                        date_str = file.replace('.dat', '')
                        try:
                            # 格式化日期显示
                            if len(date_str) == 8:
                                formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                            else:
                                formatted_date = date_str
                        except:
                            formatted_date = date_str
                        
                        date_files.append({
                            'date': date_str,
                            'formatted_date': formatted_date,
                            'filename': file,
                            'path': file_path,
                            'size': file_size,
                            'mtime': file_mtime
                        })
            
            # 按日期排序（最新的在前）
            date_files.sort(key=lambda x: x['date'], reverse=True)
            
            self.info_label.setText(f"{stock_code} - {stock_name} 的tick数据文件 (共{len(date_files)}个) - 单击日期查看数据内容")
            
            # 在表格中显示日期文件列表
            self.table_widget.setRowCount(len(date_files))
            self.table_widget.setColumnCount(4)
            self.table_widget.setHorizontalHeaderLabels(['日期', '文件名', '文件大小', '修改时间'])
            
            for i, file_info in enumerate(date_files):
                # 日期
                date_item = QTableWidgetItem(file_info['formatted_date'])
                date_item.setData(Qt.UserRole, {
                    'file_path': file_info['path'],
                    'period_type': 'tick',
                    'exchange': exchange,
                    'stock_code': stock_code,
                    'full_code': full_code,
                    'date': file_info['date'],
                    'is_date_file': True  # 标记这是日期文件
                })
                self.table_widget.setItem(i, 0, date_item)
                
                # 文件名
                self.table_widget.setItem(i, 1, QTableWidgetItem(file_info['filename']))
                
                # 文件大小
                size_str = self.format_file_size(file_info['size'])
                self.table_widget.setItem(i, 2, QTableWidgetItem(size_str))
                
                # 修改时间
                import time
                mtime_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(file_info['mtime']))
                self.table_widget.setItem(i, 3, QTableWidgetItem(mtime_str))
            
            # 调整列宽
            self.table_widget.resizeColumnsToContents()
            
            # 断开之前的点击事件连接，避免重复连接
            try:
                self.table_widget.itemClicked.disconnect()
            except:
                pass
            
            # 连接单击事件
            self.table_widget.itemClicked.connect(self.on_file_clicked)
            
            # 为可点击的单元格设置超链接样式
            for i in range(self.table_widget.rowCount()):
                # 日期列设置为超链接样式
                item = self.table_widget.item(i, 0)
                if item:
                    self.set_clickable_style(item)
            
            # 隐藏加载对话框
            self.loading_dialog.hide_loading()
            
            # 保存当前数据状态
            self.current_data_state = {
                'type': 'tick_date_files',
                'data': stock_data
            }
            
            # 显示刷新按钮
            self.refresh_button.setVisible(True)
            
            # 滚动条回到顶部
            self.scroll_to_top()
            
        except Exception as e:
            # 隐藏加载对话框
            self.loading_dialog.hide_loading()
            self.info_label.setText(f"读取日期文件列表失败: {e}")
    
    def load_tick_data_file(self, file_path, stock_code, full_code):
        """加载特定日期的tick数据文件"""
        stock_name = self.get_stock_name(full_code)
        filename = os.path.basename(file_path)
        date_str = filename.replace('.dat', '')
        
        # 格式化日期显示
        try:
            if len(date_str) == 8:
                formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
            else:
                formatted_date = date_str
        except:
            formatted_date = date_str
        
        # 显示加载对话框
        loading_message = f"正在加载 {stock_code} - {stock_name} ({formatted_date} tick数据)..."
        print(f"Showing loading dialog: {loading_message}")  # 调试信息
        self.loading_dialog.show_loading(loading_message)
        
        self.info_label.setText(loading_message)
        
        # 在后台线程中加载数据
        if self.data_thread and self.data_thread.isRunning():
            self.data_thread.quit()
            self.data_thread.wait()
        
        print(f"Starting data load thread for file: {file_path}")  # 调试信息
        self.data_thread = DataLoadThread(file_path, "tick", data_dir=self.datadir_path)
        self.data_thread.data_loaded.connect(self.on_data_loaded)
        self.data_thread.progress_updated.connect(self.on_progress_updated)
        self.data_thread.error_occurred.connect(self.on_error_occurred)
        
        self.progress_bar.setVisible(True)
        self.data_thread.start()
        
        # 保存当前数据状态
        self.current_data_state = {
            'type': 'tick_file',
            'file_path': file_path,
            'stock_code': stock_code,
            'full_code': full_code
        }
            
    def on_file_clicked(self, item):
        """文件单击事件"""
        if item.column() != 0:  # 只响应第一列的单击
            return
            
        data = item.data(Qt.UserRole)
        if not data:
            return
            
        file_path = data.get('file_path')
        period_type = data.get('period_type')
        stock_code = data.get('stock_code', '')
        full_code = data.get('full_code', '')
        exchange = data.get('exchange', '')
        
        if file_path and period_type:
            if period_type == 'tick':
                # 判断当前是在股票列表还是日期文件列表
                if data.get('is_date_file'):
                    # 这是日期文件，直接加载数据
                    date_str = data.get('date', '')
                    # 更新面包屑，添加日期层级
                    exchange_name = self.get_exchange_name(exchange)
                    period_name = self.get_period_name('0')
                    stock_name = self.get_stock_name(full_code)
                    stock_display_name = f"{stock_code} - {stock_name}"
                    date_display_name = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}" if len(date_str) == 8 else date_str
                    breadcrumb_path = [
                        {'name': exchange_name, 'data': {'type': 'exchange', 'name': exchange_name}},
                        {'name': period_name, 'data': {'type': 'period', 'exchange': exchange, 'period': '0', 'path': os.path.dirname(os.path.dirname(file_path))}},
                        {'name': stock_display_name, 'data': {
                            'type': 'stock', 
                            'stock_code': stock_code,
                            'full_code': full_code,
                            'exchange': exchange,
                            'period': '0',
                            'file_path': os.path.dirname(file_path),
                            'period_path': os.path.dirname(os.path.dirname(file_path))
                        }},
                        {'name': date_display_name, 'data': data}
                    ]
                    self.update_breadcrumb(breadcrumb_path)
                    self.load_tick_data_file(file_path, stock_code, full_code)
                else:
                    # 这是股票文件夹，显示日期文件列表
                    self.show_tick_date_files(data)
            else:
                # K线数据：file_path是单个文件路径
                # 更新面包屑，添加股票层级
                exchange_name = self.get_exchange_name(exchange)
                
                # 获取正确的周期代码
                period_code = data.get('period')
                if not period_code:
                    # 如果没有period字段，根据period_type推断
                    period_type_to_code = {'1m': '60', '5m': '300', '1d': '86400'}
                    period_code = period_type_to_code.get(period_type, '86400')
                
                period_name = self.get_period_name(period_code)
                stock_name = self.get_stock_name(full_code)
                stock_display_name = f"{stock_code} - {stock_name}"
                breadcrumb_path = [
                    {'name': exchange_name, 'data': {'type': 'exchange', 'name': exchange_name}},
                    {'name': period_name, 'data': {'type': 'period', 'exchange': exchange, 'period': period_code, 'path': os.path.dirname(file_path)}},
                    {'name': stock_display_name, 'data': {
                        'type': 'stock', 
                        'stock_code': stock_code,
                        'full_code': full_code,
                        'exchange': exchange,
                        'period': period_code,
                        'file_path': file_path,
                        'period_path': os.path.dirname(file_path)
                    }}
                ]
                self.update_breadcrumb(breadcrumb_path)
                self.load_kline_data_file(file_path, period_type, stock_code)
    
    def load_kline_data_file(self, file_path, period_type, stock_code=''):
        """加载K线数据文件"""
        if stock_code:
            # 获取股票名称
            full_code = stock_code
            if '.' not in stock_code:
                # 根据文件路径判断交易所
                if '/SH/' in file_path or '\\SH\\' in file_path:
                    full_code = stock_code + '.SH'
                elif '/SZ/' in file_path or '\\SZ\\' in file_path:
                    full_code = stock_code + '.SZ'
                elif '/BJ/' in file_path or '\\BJ\\' in file_path:
                    full_code = stock_code + '.BJ'
            
            stock_name = self.get_stock_name(full_code)
            loading_message = f"正在加载 {stock_code} - {stock_name} ({period_type} 数据)..."
            self.info_label.setText(loading_message)
            # 显示加载对话框
            print(f"Showing loading dialog: {loading_message}")  # 调试信息
            self.loading_dialog.show_loading(loading_message)
        else:
            filename = os.path.basename(file_path)
            loading_message = f"正在加载 {filename} ({period_type} 数据)..."
            self.info_label.setText(loading_message)
            # 显示加载对话框
            print(f"Showing loading dialog: {loading_message}")  # 调试信息
            self.loading_dialog.show_loading(loading_message)
        
        # 在后台线程中加载数据
        if self.data_thread and self.data_thread.isRunning():
            self.data_thread.quit()
            self.data_thread.wait()
        
        print(f"Starting data load thread for file: {file_path}")  # 调试信息
        self.data_thread = DataLoadThread(file_path, period_type, data_dir=self.datadir_path)
        self.data_thread.data_loaded.connect(self.on_data_loaded)
        self.data_thread.progress_updated.connect(self.on_progress_updated)
        self.data_thread.error_occurred.connect(self.on_error_occurred)
        
        self.progress_bar.setVisible(True)
        self.data_thread.start()
        
        # 保存当前数据状态
        self.current_data_state = {
            'type': 'kline_file',
            'file_path': file_path,
            'period_type': period_type,
            'stock_code': stock_code
        }
            
    # load_stock_data方法已移除，tick数据现在通过show_tick_date_files显示日期列表
            
    def on_data_loaded(self, data):
        """数据加载完成"""
        print(f"on_data_loaded called with data length: {len(data) if data else 'None'}")  # 调试信息
        self.progress_bar.setVisible(False)
        # 隐藏加载对话框
        self.loading_dialog.hide_loading()
        
        if not data:
            self.info_label.setText("数据为空")
            self.stats_label.setText("")
            self.data_type_label.setVisible(False)  # 隐藏说明标签
            self.refresh_button.setVisible(False)  # 隐藏刷新按钮
            self.current_data_state = None  # 清除当前数据状态
            self.scroll_to_top()  # 滚动条回到顶部
            return
            
        try:
            # 将数据显示在表格中
            df = pd.DataFrame(data)
            
            # 重新排序列，确保涨跌相关字段在最后
            reordered_columns = self.reorder_columns_for_display(df.columns.tolist())
            df = df[reordered_columns]
            
            # 获取中文字段名显示
            chinese_headers = self.get_chinese_field_names(df.columns.tolist())
            
            self.table_widget.setRowCount(len(df))
            self.table_widget.setColumnCount(len(df.columns))
            self.table_widget.setHorizontalHeaderLabels(chinese_headers)
            
            # 定义原始数据字段列表（这些字段不应该被标记为计算字段）
            original_fields = {
                # 通用时间字段
                'time', 'datetime', 'timestamp',
                
                # tick数据字段（英文）
                'lastPrice', 'last', 'price', 'open', 'high', 'low', 'close',
                'lastClose', 'preClose', 'prevClose', 'amount', 'turnover', 
                'volume', 'vol', 'pvolume', 'stockStatus', 'status', 'openInt', 
                'openInterest', 'oi', 'lastSettlementPrice', 'preSettlement', 
                'settlement', 'settelementPrice', 'settlementPrice', 'askPrice', 
                'ask', 'bidPrice', 'bid', 'askVol', 'askVolume', 'bidVol', 
                'bidVolume', 'transactionNum', 'tradeNum', 'numTrades', 
                'suspendFlag', 'suspend',
                
                # K线数据额外字段
                'avgPrice', 'vwap', 'upperLimit', 'lowerLimit', 'limitUp', 
                'limitDown', 'totalValue', 'marketValue', 'floatValue', 
                'totalShares', 'floatShares', 'freeShares',
                
                # 中文字段名
                '时间', '时间戳', '最新价', '现价', '价格', '开盘价', '最高价', '最低价', 
                '收盘价', '前收盘价', '前收价', '成交总额', '成交额', '成交总量', 
                '成交量', '原始成交总量', '证券状态', '状态', '持仓量', '前结算', 
                '结算价', '今结算', '委卖价', '委买价', '委卖量', '委买量', 
                '成交笔数', '停牌标记', '停牌', '均价', '成交均价', '涨停价', 
                '跌停价', '总市值', '流通市值', '总股本', '流通股本', '自由流通股本',
                # tick数据特有的中文字段
                '总手数', '外盘', '内盘', '委比%', '委差', '成交方向',
                '买一价', '买一量', '买二价', '买二量', '买三价', '买三量', 
                '买四价', '买四量', '买五价', '买五量',
                '卖一价', '卖一量', '卖二价', '卖二量', '卖三价', '卖三量', 
                '卖四价', '卖四量', '卖五价', '卖五量'
            }
            
            # 定义计算字段列表（用于特殊样式标注）
            calculated_fields = {
                'change': '涨跌额',
                'pct_change': '涨跌幅',
                'change_rate': '涨跌幅',
                'turnover_rate': '换手率',
                'amplitude': '振幅',
                'pe_ratio': '市盈率',
                'pb_ratio': '市净率',
                'ma5': '5日均线',
                'ma10': '10日均线',
                'ma20': '20日均线',
                'ma30': '30日均线',
                'ma60': '60日均线',
                'ema': '指数均线',
                'rsi': 'RSI',
                'macd': 'MACD',
                'bias': 'BIAS',
                'cci': 'CCI',
                'kdj_k': 'KDJ_K',
                'kdj_d': 'KDJ_D',
                'kdj_j': 'KDJ_J'
            }
            
            # 增加中文计算字段名识别
            chinese_calculated_fields = [
                '涨跌额', '涨跌值', '涨跌量', '涨跌幅', '涨跌幅%', '涨跌率', '换手率', '振幅',
                '市盈率', '市净率', '均线', 'MA', 'RSI', 'MACD', 'KDJ',
                'BIAS', 'CCI', 'EMA', '流通市值', '总市值'
            ]
            
            # 检查哪些列是计算字段（使用原始英文列名）
            calculated_columns = []
            calculated_chinese_names = []  # 对应的中文名称
            for i, col in enumerate(df.columns):
                col_lower = col.lower()
                col_str = str(col)
                chinese_name = chinese_headers[i]
                
                # 首先检查是否为原始字段（优先级最高）
                is_original_field = False
                
                # 检查精确匹配
                if col in original_fields:
                    is_original_field = True
                # 检查小写匹配
                elif col_lower in original_fields:
                    is_original_field = True
                # 检查字符串匹配
                elif col_str in original_fields:
                    is_original_field = True
                # 检查中文名称匹配
                elif chinese_name in original_fields:
                    is_original_field = True
                
                if is_original_field:
                    continue  # 跳过原始字段
                
                # 检查是否为计算字段
                # 检查英文字段名
                is_calculated_en = any(field in col_lower for field in calculated_fields.keys())
                # 检查中文字段名 - 使用精确匹配而不是包含匹配
                is_calculated_cn = any(cn_field == col_str for cn_field in chinese_calculated_fields)
                
                # 特殊处理：明确的计算字段（包含特定关键词）
                is_calculated_special = (
                    '涨跌' in col_str or '换手率' in col_str or 
                    '委比' in col_str or '振幅' in col_str or
                    '市盈率' in col_str or '市净率' in col_str or
                    col_str.endswith('%')
                )
                
                if is_calculated_en or is_calculated_cn or is_calculated_special:
                    calculated_columns.append(col)
                    calculated_chinese_names.append(chinese_name)
            
            # 设置表头样式 - 为计算字段设置蓝色
            header = self.table_widget.horizontalHeader()
            
            # 设置基础表头样式
            header.setStyleSheet("""
                QHeaderView::section {
                    background-color: #404040;
                    color: #e8e8e8;
                    padding: 12px;
                    border: none;
                    border-right: 1px solid #4d4d4d;
                    border-bottom: 1px solid #4d4d4d;
                    font-weight: bold;
                    font-size: 14px;
                }
                QHeaderView::section:hover {
                    background-color: #454545;
                }
            """)
            
            # 输出调试信息
            print(f"计算字段: {calculated_columns}")
            print(f"所有字段: {list(df.columns)}")
            print(f"对应中文名: {chinese_headers}")
            print(f"计算字段: {calculated_columns}")
            print(f"计算字段中文名: {calculated_chinese_names}")
            
            # 输出原始字段识别结果
            original_columns = []
            original_chinese_names = []
            for i, col in enumerate(df.columns):
                col_lower = col.lower()
                col_str = str(col)
                chinese_name = chinese_headers[i]
                
                # 检查是否为原始字段
                is_original_field = False
                if col in original_fields:
                    is_original_field = True
                elif col_lower in original_fields:
                    is_original_field = True
                elif col_str in original_fields:
                    is_original_field = True
                elif chinese_name in original_fields:
                    is_original_field = True
                
                if is_original_field:
                    original_columns.append(col)
                    original_chinese_names.append(chinese_name)
            
            print(f"原始字段: {original_columns}")
            print(f"原始字段中文名: {original_chinese_names}")
            
            # 检查用户提到的特定字段
            missing_fields = []
            for field in ['open', 'high', 'low', 'lastClose']:
                if field not in df.columns:
                    missing_fields.append(field)
            
            if missing_fields:
                print(f"数据中缺失的字段: {missing_fields}")
                print("注意：tick数据通常不包含开高低收字段，只有K线数据才有这些字段")
            else:
                print("用户提到的字段都存在于数据中")
            
            # 详细显示每个字段的分类情况
            print("\n=== 字段分类详情 ===")
            for i, col in enumerate(df.columns):
                chinese_name = chinese_headers[i]
                if col in original_columns:
                    print(f"✓ 原始字段: {col} -> {chinese_name}")
                elif col in calculated_columns:
                    print(f"⚡ 计算字段: {col} -> {chinese_name}")
                else:
                    print(f"❓ 未分类字段: {col} -> {chinese_name}")
            print("===================")
            
            # 重新设置表头项，确保蓝色能正确显示
            for i, (col, chinese_name) in enumerate(zip(df.columns, chinese_headers)):
                header_item = QTableWidgetItem(chinese_name)
                
                if chinese_name in calculated_chinese_names:
                    # 为计算字段的表头设置蓝色样式
                    header_item.setForeground(QColor('#2E6DA4'))  # 深蓝色
                    header_item.setBackground(QColor('#404040'))  # 保持背景色一致
                    header_item.setToolTip(f"计算字段: {chinese_name} (二次计算数据)")
                    print(f"设置蓝色表头: {chinese_name}")
                else:
                    header_item.setForeground(QColor('#e8e8e8'))  # 默认白色
                    header_item.setBackground(QColor('#404040'))  # 保持背景色一致
                    header_item.setToolTip(f"原始字段: {chinese_name} (市场原始数据)")
                
                self.table_widget.setHorizontalHeaderItem(i, header_item)
            
            # 强制刷新表头显示
            header.update()
            
            # 如果有计算字段，显示说明标签
            if calculated_columns:
                self.data_type_label.setVisible(True)
            else:
                self.data_type_label.setVisible(False)
            
            # 填充数据
            for i in range(len(df)):
                for j, col in enumerate(df.columns):
                    value = str(df.iloc[i, j])
                    item = QTableWidgetItem(value)
                    chinese_name = chinese_headers[j]
                    
                    # 检查是否为计算字段
                    is_calculated = col in calculated_columns
                    
                    if is_calculated:
                        # 计算字段使用特殊样式
                        item.setBackground(QColor('#3a4a5a'))  # 深蓝灰色背景
                        item.setForeground(QColor('#a8d8ff'))  # 浅蓝色字体
                        
                        # 设置工具提示
                        field_desc = None
                        for field, desc in calculated_fields.items():
                            if field in col.lower():
                                field_desc = desc
                                break
                        
                        if field_desc:
                            item.setToolTip(f"计算字段: {field_desc}")
                        else:
                            item.setToolTip("计算字段: 此数据为程序计算所得")
                    else:
                        # 原始数据使用默认样式
                        item.setBackground(QColor('#333333'))  # 默认背景
                        item.setForeground(QColor('#e8e8e8'))  # 默认字体
                        item.setToolTip("原始数据: 此数据为市场原始数据")
                    
                    # 设置数值类型的对齐方式
                    try:
                        float_val = float(value)
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                        
                        # 如果是涨跌相关字段，根据正负值设置颜色
                        if is_calculated and (any(field in col.lower() for field in ['change', 'pct_change', 'change_rate']) or 
                                            any(cn_field in chinese_name for cn_field in ['涨跌额', '涨跌值', '涨跌量', '涨跌幅', '涨跌率'])):
                            if float_val > 0:
                                item.setForeground(QColor('#ff6b6b'))  # 上涨红色
                            elif float_val < 0:
                                item.setForeground(QColor('#4ecdc4'))  # 下跌绿色
                            else:
                                item.setForeground(QColor('#2E6DA4'))  # 平盘深蓝色
                    except:
                        item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                    
                    self.table_widget.setItem(i, j, item)
            
            # 调整列宽
            self.table_widget.resizeColumnsToContents()
            
            # 断开之前的点击事件连接，避免冲突
            try:
                self.table_widget.itemClicked.disconnect()
            except:
                pass
            
            # 更新信息标签，显示计算字段数量和说明
            calculated_count = len(calculated_chinese_names)
            total_columns = len(df.columns)
            original_count = total_columns - calculated_count
            
            info_text = f"已加载 {len(df)} 条数据记录"
            if calculated_count > 0:
                info_text += f" (原始字段: {original_count}, 计算字段: {calculated_count}) - 蓝色列名表示二次计算数据"
            
            self.info_label.setText(info_text)
            
            # 更新统计信息
            self.update_stats_info(data)
            
            # 显示刷新按钮
            self.refresh_button.setVisible(True)
            
            # 滚动条回到顶部
            self.scroll_to_top()
            
        except Exception as e:
            self.info_label.setText(f"显示数据失败: {e}")
            self.stats_label.setText("")
            self.data_type_label.setVisible(False)  # 隐藏说明标签
            self.refresh_button.setVisible(False)  # 隐藏刷新按钮
    
    def update_stats_info(self, data):
        """更新统计信息"""
        if not data:
            self.stats_label.setText("")
            return
            
        row_count = len(data)
        col_count = len(data[0].keys()) if data else 0
        
        # 计算数据大小（估算）
        data_size = sum(len(str(v)) for record in data for v in record.values())
        size_kb = data_size / 1024
        
        if size_kb < 1024:
            size_str = f"{size_kb:.1f} KB"
        else:
            size_str = f"{size_kb/1024:.1f} MB"
        
        stats_text = f"行数: {row_count} | 列数: {col_count} | 大小: {size_str}"
        self.stats_label.setText(stats_text)
            
    def on_progress_updated(self, message):
        """进度更新"""
        print(f"Progress update: {message}")  # 调试信息
        self.status_bar.showMessage(message)
        # 同时更新加载对话框的信息
        if self.loading_dialog.isVisible():
            self.loading_dialog.set_message(message)
        
    def on_error_occurred(self, error_message):
        """错误处理"""
        print(f"on_error_occurred called with error: {error_message}")  # 调试信息
        self.progress_bar.setVisible(False)
        # 隐藏加载对话框
        self.loading_dialog.hide_loading()
        self.info_label.setText(f"加载失败: {error_message}")
        self.status_bar.showMessage("加载失败")
        self.scroll_to_top()  # 滚动条回到顶部
        
    def create_supplement_widget(self):
        """创建数据补充工具"""
        supplement_widget = QWidget()
        supplement_widget.setStyleSheet("background-color: #2b2b2b;")
        
        # 主布局
        main_layout = QVBoxLayout(supplement_widget)
        main_layout.setSpacing(15)  # 增加组件间距
        main_layout.setContentsMargins(20, 20, 20, 20)  # 增加内边距
        
        # 标题
        title_label = QLabel("数据补充工具")
        title_font = QFont()
        title_font.setPointSize(14)  # 增大字体，与右侧保持一致
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setStyleSheet("color: #e8e8e8; margin-bottom: 10px;")
        main_layout.addWidget(title_label)
        
        # 添加各个组件
        self.add_stock_group(main_layout)
        self.add_stock_list_module(main_layout)  # 添加股票列表模块
        self.add_period_group(main_layout)
        self.add_date_group(main_layout)
        self.add_supplement_section(main_layout)
        
        # 状态标签
        self.supplement_status_label = QLabel("准备就绪")
        self.supplement_status_label.setStyleSheet("color: #b0b0b0; font-size: 14px;")
        main_layout.addWidget(self.supplement_status_label)
        
        # 添加弹性空间
        main_layout.addStretch()
        
        return supplement_widget
    
    def add_stock_group(self, layout):
        """添加股票代码列表组"""
        stocks_group = QGroupBox("股票代码列表文件")
        stocks_group.setStyleSheet("""
            QGroupBox {
                color: #e8e8e8;
                font-weight: bold;
                border: 1px solid #4d4d4d;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 5px;
            }
        """)
        
        stock_layout = QVBoxLayout()
        
        # 股票类型复选框
        self.stock_checkboxes = {}
        stock_types = {
            'hs_a': '沪深A股',
            'gem': '创业板',
            'sci': '科创板',
            'zz500': '中证500成分股',
            'hs300': '沪深300成分股',
            'sz50': '上证50成分股',
            'indices': '常用指数',
            'custom': '自选清单'
        }
        
        # 创建4行2列的网格布局，给复选框更多空间
        grid_layout = QGridLayout()
        grid_layout.setSpacing(8)  # 增加网格间距
        row = 0
        col = 0
        
        for stock_type, display_name in stock_types.items():
            if stock_type == 'custom':
                # 为自选清单创建特殊的标签和复选框布局
                custom_widget = QWidget()
                custom_layout = QHBoxLayout(custom_widget)
                custom_layout.setContentsMargins(0, 0, 0, 0)
                custom_layout.setSpacing(8)
                
                # 复选框
                checkbox = QCheckBox()
                checkbox.setStyleSheet("color: #e8e8e8; font-size: 14px; padding: 3px;")
                checkbox.stateChanged.connect(self.on_stock_selection_changed)
                self.stock_checkboxes[stock_type] = checkbox
                custom_layout.addWidget(checkbox)
                
                # 创建可点击的标签
                custom_label = QLabel(display_name)
                custom_label.setStyleSheet("""
                    QLabel {
                        color: #e8e8e8;
                        text-decoration: underline;
                        font-size: 14px;
                        padding: 3px;
                    }
                """)
                custom_label.setCursor(Qt.PointingHandCursor)
                custom_label.mousePressEvent = self.open_custom_list
                custom_layout.addWidget(custom_label)
                
                custom_layout.addStretch()
                
                # 将自选清单放在新的一行
                if col != 0:
                    row += 1
                    col = 0
                grid_layout.addWidget(custom_widget, row, col, 1, 2)  # 跨越2列
                row += 1
                col = 0
            else:
                checkbox = QCheckBox(display_name)
                checkbox.setStyleSheet("color: #e8e8e8; font-size: 14px; padding: 3px;")
                checkbox.stateChanged.connect(self.on_stock_selection_changed)
                self.stock_checkboxes[stock_type] = checkbox
                grid_layout.addWidget(checkbox, row, col)
                
                col += 1
                if col >= 2:  # 改为2列布局
                    col = 0
                    row += 1
        
        stock_layout.addLayout(grid_layout)
        
        # 添加自定义文件按钮和清空按钮
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)  # 增加按钮间距
        
        add_custom_button = QPushButton("添加自定义列表")
        add_custom_button.setMinimumHeight(35)  # 设置最小高度
        add_custom_button.setStyleSheet("""
            QPushButton {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 3px;
                padding: 8px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #454545;
            }
            QPushButton:pressed {
                background-color: #505050;
            }
        """)
        add_custom_button.clicked.connect(self.add_custom_stock_file)
        button_layout.addWidget(add_custom_button)
        
        clear_button = QPushButton("清空")
        clear_button.setMinimumHeight(35)  # 设置最小高度
        clear_button.setStyleSheet("""
            QPushButton {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 3px;
                padding: 8px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #454545;
            }
            QPushButton:pressed {
                background-color: #505050;
            }
        """)
        clear_button.clicked.connect(self.clear_stock_files)
        button_layout.addWidget(clear_button)
        
        stock_layout.addLayout(button_layout)
        
        # 预览区域
        self.stock_files_preview = QTextEdit()
        self.stock_files_preview.setMaximumHeight(100)  # 增加高度
        self.stock_files_preview.setMinimumHeight(80)   # 设置最小高度
        self.stock_files_preview.setReadOnly(True)
        self.stock_files_preview.setText("未选择任何股票列表文件")
        self.stock_files_preview.setStyleSheet("""
            QTextEdit {
                background-color: #2b2b2b;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 3px;
                padding: 8px;
                font-size: 14px;
            }
        """)
        stock_layout.addWidget(self.stock_files_preview)
        
        stocks_group.setLayout(stock_layout)
        layout.addWidget(stocks_group)

    def add_stock_list_module(self, layout):
        """添加股票列表模块"""
        # 创建股票列表组
        stock_list_group = QGroupBox("当前股票列表")
        stock_list_group.setStyleSheet("""
            QGroupBox {
                color: #e8e8e8;
                font-weight: bold;
                border: 1px solid #4d4d4d;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 5px;
            }
        """)
        
        stock_list_layout = QVBoxLayout()
        
        # 创建股票列表表格
        self.stock_list_table = QTableWidget(0, 2)
        self.stock_list_table.setHorizontalHeaderLabels(["股票代码", "股票名称"])
        self.stock_list_table.horizontalHeader().setStretchLastSection(True)
        self.stock_list_table.setMinimumHeight(150)
        self.stock_list_table.setMaximumHeight(200)
        self.stock_list_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.stock_list_table.setStyleSheet("""
            QTableWidget {
                background-color: #333333;
                alternate-background-color: #383838;
                color: #e8e8e8;
                gridline-color: #404040;
                border: 1px solid #404040;
                selection-background-color: #505050;
                font-size: 13px;
            }
            QTableWidget::item {
                padding: 8px;
                background-color: transparent;
                border: none;
            }
            QTableWidget::item:selected {
                background-color: #505050;
                color: #ffffff;
            }
            QHeaderView::section {
                background-color: #404040;
                color: #e8e8e8;
                padding: 8px;
                border: none;
                border-right: 1px solid #4d4d4d;
                border-bottom: 1px solid #4d4d4d;
                font-weight: bold;
                font-size: 13px;
            }
        """)
        stock_list_layout.addWidget(self.stock_list_table)
        
        # 创建操作按钮
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)
        
        # 添加股票按钮
        add_stock_button = QPushButton("添加股票")
        add_stock_button.setMinimumHeight(35)
        add_stock_button.setStyleSheet("""
            QPushButton {
                background-color: #007acc;
                color: #ffffff;
                border: none;
                border-radius: 3px;
                padding: 8px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #0088dd;
            }
            QPushButton:pressed {
                background-color: #0066aa;
            }
        """)
        add_stock_button.clicked.connect(self.add_single_stock)
        button_layout.addWidget(add_stock_button)
        
        # 导入股票按钮
        import_stock_button = QPushButton("导入文件")
        import_stock_button.setMinimumHeight(35)
        import_stock_button.setStyleSheet("""
            QPushButton {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 3px;
                padding: 8px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #454545;
            }
            QPushButton:pressed {
                background-color: #505050;
            }
        """)
        import_stock_button.clicked.connect(self.import_stocks)
        button_layout.addWidget(import_stock_button)
        
        # 第二行按钮
        button_layout2 = QHBoxLayout()
        button_layout2.setSpacing(8)
        
        # 删除选中按钮
        delete_stock_button = QPushButton("删除选中")
        delete_stock_button.setMinimumHeight(35)
        delete_stock_button.setStyleSheet("""
            QPushButton {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 3px;
                padding: 8px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #454545;
            }
            QPushButton:pressed {
                background-color: #505050;
            }
        """)
        delete_stock_button.clicked.connect(self.delete_selected_stocks)
        button_layout2.addWidget(delete_stock_button)
        
        # 清空列表按钮
        clear_stock_button = QPushButton("清空列表")
        clear_stock_button.setMinimumHeight(35)
        clear_stock_button.setStyleSheet("""
            QPushButton {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 3px;
                padding: 8px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #454545;
            }
            QPushButton:pressed {
                background-color: #505050;
            }
        """)
        clear_stock_button.clicked.connect(self.clear_stock_list)
        button_layout2.addWidget(clear_stock_button)
        
        stock_list_layout.addLayout(button_layout)
        stock_list_layout.addLayout(button_layout2)
        
        # 股票数量统计标签
        self.stock_count_label = QLabel("当前股票数量: 0")
        self.stock_count_label.setStyleSheet("color: #b0b0b0; font-size: 14px;")
        stock_list_layout.addWidget(self.stock_count_label)
        
        stock_list_group.setLayout(stock_list_layout)
        layout.addWidget(stock_list_group)

    def add_period_group(self, layout):
        """添加周期类型组"""
        period_group = QGroupBox("周期类型")
        period_group.setStyleSheet("""
            QGroupBox {
                color: #e8e8e8;
                font-weight: bold;
                border: 1px solid #4d4d4d;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 5px;
            }
        """)
        period_layout = QHBoxLayout()

        self.period_type_combo = NoWheelComboBox()
        self.period_type_combo.addItems(['tick', '1m', '5m', '1d'])
        self.period_type_combo.setMinimumHeight(35)  # 设置最小高度
        self.period_type_combo.setStyleSheet("""
            QComboBox {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 3px;
                padding: 6px;
                font-size: 14px;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 4px solid #e8e8e8;
            }
        """)
        period_layout.addWidget(self.period_type_combo)
        period_group.setLayout(period_layout)
        layout.addWidget(period_group)

    def add_date_group(self, layout):
        """添加日期范围组"""
        date_group = QGroupBox("日期范围")
        date_group.setStyleSheet("""
            QGroupBox {
                color: #e8e8e8;
                font-weight: bold;
                border: 1px solid #4d4d4d;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 5px;
            }
        """)
        date_layout = QVBoxLayout()
        
        # 起始日期
        start_date_layout = QHBoxLayout()
        start_label = QLabel("起始日期:")
        start_label.setStyleSheet("color: #e8e8e8; font-size: 14px;")
        start_date_layout.addWidget(start_label)
        
        self.start_date_edit = NoWheelDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDate(QDate(2024, 1, 1))
        self.start_date_edit.setMinimumHeight(35)  # 设置最小高度
        self.start_date_edit.setStyleSheet("""
            QDateEdit {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 3px;
                padding: 6px;
                font-size: 14px;
            }
        """)
        start_date_layout.addWidget(self.start_date_edit)
        date_layout.addLayout(start_date_layout)
        
        # 结束日期
        end_date_layout = QHBoxLayout()
        end_label = QLabel("结束日期:")
        end_label.setStyleSheet("color: #e8e8e8; font-size: 14px;")
        end_date_layout.addWidget(end_label)
        
        self.end_date_edit = NoWheelDateEdit()
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setDate(QDate.currentDate())
        self.end_date_edit.setMinimumHeight(35)  # 设置最小高度
        self.end_date_edit.setStyleSheet("""
            QDateEdit {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 3px;
                padding: 6px;
                font-size: 14px;
            }
        """)
        end_date_layout.addWidget(self.end_date_edit)
        date_layout.addLayout(end_date_layout)
        
        date_group.setLayout(date_layout)
        layout.addWidget(date_group)

    def add_supplement_section(self, layout):
        """添加补充数据按钮和进度条"""
        supplement_layout = QVBoxLayout()
        
        # 补充数据按钮
        self.supplement_button = QPushButton("补充数据")
        self.supplement_button.setMinimumHeight(35)
        self.supplement_button.setStyleSheet("""
            QPushButton {
                background-color: #007acc;
                color: #ffffff;
                border: none;
                border-radius: 5px;
                padding: 8px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #0088dd;
            }
            QPushButton:pressed {
                background-color: #0066aa;
            }
        """)
        self.supplement_button.clicked.connect(self.supplement_data)
        supplement_layout.addWidget(self.supplement_button)

        # 进度条
        self.supplement_progress_bar = QProgressBar()
        self.supplement_progress_bar.setTextVisible(False)
        self.supplement_progress_bar.setMinimumHeight(8)
        self.supplement_progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #404040;
                border: 1px solid #4d4d4d;
                border-radius: 3px;
            }
            QProgressBar::chunk {
                background-color: #007acc;
                border-radius: 2px;
            }
        """)
        supplement_layout.addWidget(self.supplement_progress_bar)

        layout.addLayout(supplement_layout)

    def open_custom_list(self, event):
        """打开自选清单文件"""
        try:
            # 获取自选清单文件路径
            custom_file = self.get_custom_list_path()
            
            if os.path.exists(custom_file):
                # 使用系统默认程序打开文件
                import subprocess
                import platform
                
                if platform.system() == 'Windows':
                    os.startfile(custom_file)
                elif platform.system() == 'Darwin':  # macOS
                    subprocess.run(['open', custom_file])
                else:  # Linux
                    subprocess.run(['xdg-open', custom_file])
                
                self.supplement_status_label.setText(f"已打开自选清单文件: {custom_file}")
                logging.info(f"已打开自选清单文件: {custom_file}")
            else:
                # 如果文件不存在，创建一个示例文件
                try:
                    os.makedirs(os.path.dirname(custom_file), exist_ok=True)
                    
                    # 创建示例自选清单文件
                    sample_content = """股票代码,股票名称
000001.SZ,平安银行
000002.SZ,万科A
600000.SH,浦发银行
600036.SH,招商银行
000858.SZ,五粮液"""
                    
                    with open(custom_file, 'w', encoding='utf-8') as f:
                        f.write(sample_content)
                    
                    # 打开新创建的文件
                    if platform.system() == 'Windows':
                        os.startfile(custom_file)
                    elif platform.system() == 'Darwin':  # macOS
                        subprocess.run(['open', custom_file])
                    else:  # Linux
                        subprocess.run(['xdg-open', custom_file])
                    
                    self.supplement_status_label.setText(f"已创建并打开新的自选清单文件: {custom_file}")
                    logging.info(f"已创建并打开新的自选清单文件: {custom_file}")
                except Exception as create_error:
                    QMessageBox.warning(self, "错误", f"创建自选清单文件失败: {str(create_error)}")
                    logging.error(f"创建自选清单文件失败: {str(create_error)}")
        except Exception as e:
            QMessageBox.warning(self, "错误", f"打开自选清单文件失败: {str(e)}")
            logging.error(f"打开自选清单文件失败: {str(e)}")
    
    def get_custom_list_path(self):
        """获取自选清单文件的路径"""
        data_dir = os.path.join(os.path.dirname(__file__), 'data')
        return os.path.join(data_dir, "otheridx.csv")
    
    def add_custom_stock_file(self):
        """添加自定义股票列表文件"""
        try:
            data_dir = os.path.join(os.path.dirname(__file__), 'data')
            files, _ = QFileDialog.getOpenFileNames(
                self,
                "选择股票代码列表文件",
                data_dir,
                "CSV Files (*.csv);;All Files (*)"
            )
            
            if files:
                current_files = []
                current_text = self.stock_files_preview.toPlainText().strip()
                if current_text and current_text != "未选择任何股票列表文件":
                    current_files = current_text.split('\n')
                
                for file in files:
                    if file not in current_files:
                        current_files.append(file)
                
                preview_text = "\n".join(current_files)
                self.stock_files_preview.setText(preview_text)
                
        except Exception as e:
            QMessageBox.warning(self, "错误", f"添加文件时出错: {str(e)}")

    def clear_stock_files(self):
        """清空股票列表预览和股票列表表格"""
        try:
            for checkbox in self.stock_checkboxes.values():
                checkbox.setChecked(False)
            self.stock_files_preview.setText("未选择任何股票列表文件")
            # 同时清空股票列表表格
            self.stock_list_table.setRowCount(0)
            self.update_stock_count()
        except Exception as e:
            QMessageBox.warning(self, "错误", f"清空列表时出错: {str(e)}")

    def on_stock_selection_changed(self):
        """股票选择变化时的处理 - 同时更新预览和股票列表"""
        self.update_stock_files_preview()
        self.load_stocks_from_selected_files()

    def update_stock_files_preview(self):
        """更新选中的股票文件预览"""
        try:
            data_dir = os.path.join(os.path.dirname(__file__), 'data')
            selected_files = []
            
            # 添加预定义列表文件
            for stock_type, checkbox in self.stock_checkboxes.items():
                if checkbox.isChecked():
                    filename = None
                    if stock_type == 'indices':
                        filename = os.path.join(data_dir, "指数_股票列表.csv")
                    elif stock_type == 'zz500':
                        filename = os.path.join(data_dir, "中证500成分股_股票列表.csv")
                    elif stock_type == 'hs300':
                        filename = os.path.join(data_dir, "沪深300成分股_股票列表.csv")
                    elif stock_type == 'sz50':
                        filename = os.path.join(data_dir, "上证50成分股_股票列表.csv")
                    elif stock_type == 'hs_a':
                        filename = os.path.join(data_dir, "沪深A股_股票列表.csv")
                    elif stock_type == 'gem':
                        filename = os.path.join(data_dir, "创业板_股票列表.csv")
                    elif stock_type == 'sci':
                        filename = os.path.join(data_dir, "科创板_股票列表.csv")
                    elif stock_type == 'custom':
                        filename = self.get_custom_list_path()
                    
                    if filename and os.path.exists(filename):
                        selected_files.append(filename)
            
            if selected_files:
                self.stock_files_preview.setText('\n'.join(selected_files))
            else:
                self.stock_files_preview.setText("未选择任何股票列表文件")
                
        except Exception as e:
            logging.error(f"更新股票文件预览时出错: {str(e)}")

    def validate_date_range(self):
        """验证日期范围"""
        start_date = self.start_date_edit.date()
        end_date = self.end_date_edit.date()
        
        if end_date < start_date:
            QMessageBox.warning(self, "警告", "结束日期不能早于开始日期")
            return False
            
        return True

    def supplement_data(self):
        """补充数据按钮点击事件处理"""
        try:
            # 如果补充数据线程正在运行，点击按钮就停止补充
            if hasattr(self, 'supplement_thread') and self.supplement_thread and self.supplement_thread.isRunning():
                self.supplement_thread.stop()
                self.supplement_status_label.setText("补充数据已停止")
                self.reset_supplement_button()
                return
                
            # 验证日期范围
            if not self.validate_date_range():
                return
            
            # 检查是否有股票数据
            stock_count = self.stock_list_table.rowCount()
            if stock_count == 0:
                QMessageBox.warning(self, "警告", "请先添加股票到股票列表中！\n可以通过勾选股票池或导入股票文件来添加股票。")
                return
            
            # 生成临时股票文件用于数据补充
            import tempfile
            temp_dir = tempfile.mkdtemp()
            temp_stock_file = os.path.join(temp_dir, "temp_stock_list.csv")
            
            try:
                with open(temp_stock_file, 'w', encoding='utf-8') as f:
                    f.write("股票代码,股票名称\n")  # 写入表头
                    for row in range(stock_count):
                        code = self.stock_list_table.item(row, 0).text()
                        name = self.stock_list_table.item(row, 1).text()
                        f.write(f"{code},{name}\n")
                
                stock_files = [temp_stock_file]
                self.temp_stock_file = temp_stock_file  # 保存临时文件路径用于后续清理
                
            except Exception as e:
                QMessageBox.warning(self, "错误", f"生成临时股票文件失败: {str(e)}")
                return

            # 获取周期类型
            period_type = self.period_type_combo.currentText()

            # 获取日期范围
            start_date = self.start_date_edit.date().toString("yyyyMMdd")
            end_date = self.end_date_edit.date().toString("yyyyMMdd")

            # 更新按钮状态
            self.supplement_button.setText("停止补充")
            self.supplement_button.setEnabled(True)

            # 设置进度条
            self.supplement_progress_bar.setVisible(True)
            self.supplement_progress_bar.setValue(0)

            # 创建参数字典
            params = {
                'stock_files': stock_files,
                'period_type': period_type,
                'start_date': start_date,
                'end_date': end_date
            }

            # 启动补充数据线程
            self.supplement_thread = SupplementThread(params, self)
            self.supplement_thread.progress.connect(self.update_supplement_progress)
            self.supplement_thread.finished.connect(self.supplement_finished)
            self.supplement_thread.error.connect(self.handle_supplement_error)
            self.supplement_thread.status_update.connect(self.update_supplement_status)
            self.supplement_thread.start()

        except Exception as e:
            QMessageBox.critical(self, "错误", f"启动补充数据时出错: {str(e)}")

    def update_supplement_progress(self, value):
        """更新补充数据进度"""
        self.supplement_progress_bar.setValue(value)

    def update_supplement_status(self, message):
        """更新补充数据状态"""
        self.supplement_status_label.setText(message)

    def supplement_finished(self, success, message):
        """补充数据完成"""
        self.supplement_progress_bar.setVisible(False)
        self.reset_supplement_button()
        
        # 清理临时文件
        if hasattr(self, 'temp_stock_file') and os.path.exists(self.temp_stock_file):
            try:
                import shutil
                shutil.rmtree(os.path.dirname(self.temp_stock_file), ignore_errors=True)
            except:
                pass
        
        if success:
            self.supplement_status_label.setText("补充数据完成！")
            QMessageBox.information(self, "成功", message)
        else:
            self.supplement_status_label.setText("补充数据失败")
            QMessageBox.warning(self, "失败", message)

    def handle_supplement_error(self, error_msg):
        """处理补充数据错误"""
        self.supplement_progress_bar.setVisible(False)
        self.reset_supplement_button()
        
        # 清理临时文件
        if hasattr(self, 'temp_stock_file') and os.path.exists(self.temp_stock_file):
            try:
                import shutil
                shutil.rmtree(os.path.dirname(self.temp_stock_file), ignore_errors=True)
            except:
                pass
        
        self.supplement_status_label.setText("补充数据失败")
        QMessageBox.critical(self, "错误", error_msg)

    def reset_supplement_button(self):
        """重置补充数据按钮"""
        self.supplement_button.setText("补充数据")
        self.supplement_button.setEnabled(True)

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
                for row in range(self.stock_list_table.rowCount()):
                    if self.stock_list_table.item(row, 0).text() == code:
                        QMessageBox.information(self, "提示", f"股票 {code} 已存在于列表中")
                        return
                
                # 获取股票名称
                name = ""
                if code in self.stock_names_cache:
                    name = self.stock_names_cache[code]
                else:
                    # 如果没有找到名称，可以让用户输入
                    input_name, ok_name = QInputDialog.getText(
                        self,
                        "股票名称",
                        f"未找到股票 {code} 的名称，请输入股票名称（可选）:",
                        text=""
                    )
                    if ok_name:
                        name = input_name.strip()
                
                # 添加到表格
                row = self.stock_list_table.rowCount()
                self.stock_list_table.insertRow(row)
                self.stock_list_table.setItem(row, 0, QTableWidgetItem(code))
                self.stock_list_table.setItem(row, 1, QTableWidgetItem(name))
                
                # 选中新添加的行
                self.stock_list_table.selectRow(row)
                
                self.update_stock_count()
                self.supplement_status_label.setText(f"已添加股票: {code}")
                
        except Exception as e:
            error_msg = f"添加股票时出错: {str(e)}"
            self.supplement_status_label.setText(error_msg)
            logging.error(error_msg)
            QMessageBox.critical(self, "错误", error_msg)

    def validate_stock_code(self, code):
        """验证股票代码格式"""
        import re
        # 股票代码格式：6位数字.交易所代码
        pattern = r'^\d{6}\.(SH|SZ|BJ)$'
        return re.match(pattern, code) is not None

    def import_stocks(self):
        """导入股票列表"""
        try:
            # 设置默认目录为data
            data_dir = os.path.join(os.path.dirname(__file__), 'data')
            
            file_name, _ = QFileDialog.getOpenFileName(
                self,
                "选择股票列表文件",
                data_dir,
                "CSV Files (*.csv);;Text Files (*.txt)"
            )
            
            if file_name:
                # 读取文件
                with open(file_name, 'r', encoding='utf-8-sig') as f:
                    lines = f.readlines()
                
                # 解析并添加股票
                added_count = 0
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith('#'):  # 跳过空行和注释行
                        parts = line.split(',')
                        if len(parts) >= 1:
                            code = parts[0].strip().replace('\ufeff', '')  # 移除BOM字符
                            name = parts[1].strip() if len(parts) > 1 else ""
                            
                            # 如果没有名称，尝试从股票名称缓存获取
                            if not name and code in self.stock_names_cache:
                                name = self.stock_names_cache[code]
                            
                            # 检查是否已存在
                            exists = False
                            for row in range(self.stock_list_table.rowCount()):
                                if self.stock_list_table.item(row, 0).text() == code:
                                    exists = True
                                    break
                            
                            # 如果不存在则添加
                            if not exists:
                                row = self.stock_list_table.rowCount()
                                self.stock_list_table.insertRow(row)
                                self.stock_list_table.setItem(row, 0, QTableWidgetItem(code))
                                self.stock_list_table.setItem(row, 1, QTableWidgetItem(name))
                                added_count += 1
                
                self.update_stock_count()
                self.supplement_status_label.setText(f"已导入 {added_count} 只股票")
                
        except Exception as e:
            error_msg = f"导入股票列表时出错: {str(e)}"
            self.supplement_status_label.setText(error_msg)
            logging.error(error_msg)
            QMessageBox.critical(self, "错误", error_msg)

    def delete_selected_stocks(self):
        """删除选中的股票"""
        try:
            selected_rows = set(item.row() for item in self.stock_list_table.selectedItems())
            if not selected_rows:
                QMessageBox.information(self, "提示", "请先选择要删除的股票")
                return
            
            # 确认删除
            reply = QMessageBox.question(
                self, 
                "确认删除", 
                f"确定要删除选中的 {len(selected_rows)} 只股票吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                # 按行号倒序删除，避免索引变化
                for row in sorted(selected_rows, reverse=True):
                    self.stock_list_table.removeRow(row)
                
                self.update_stock_count()
                self.supplement_status_label.setText(f"已删除 {len(selected_rows)} 只股票")
                
        except Exception as e:
            error_msg = f"删除股票时出错: {str(e)}"
            self.supplement_status_label.setText(error_msg)
            logging.error(error_msg)
            QMessageBox.critical(self, "错误", error_msg)

    def clear_stock_list(self):
        """清空股票列表"""
        try:
            if self.stock_list_table.rowCount() == 0:
                QMessageBox.information(self, "提示", "股票列表已经是空的")
                return
            
            # 确认清空
            reply = QMessageBox.question(
                self, 
                "确认清空", 
                "确定要清空所有股票列表吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                self.stock_list_table.setRowCount(0)
                self.update_stock_count()
                self.supplement_status_label.setText("已清空股票列表")
                
        except Exception as e:
            error_msg = f"清空股票列表时出错: {str(e)}"
            self.supplement_status_label.setText(error_msg)
            logging.error(error_msg)
            QMessageBox.critical(self, "错误", error_msg)

    def update_stock_count(self):
        """更新股票数量显示"""
        count = self.stock_list_table.rowCount()
        self.stock_count_label.setText(f"当前股票数量: {count}")

    def get_current_stock_list(self):
        """获取当前股票列表"""
        stock_list = []
        for row in range(self.stock_list_table.rowCount()):
            code = self.stock_list_table.item(row, 0).text()
            name = self.stock_list_table.item(row, 1).text()
            stock_list.append((code, name))
        return stock_list
    
    def refresh_current_data(self):
        """刷新当前数据"""
        if not self.current_data_state:
            return
        
        try:
            state = self.current_data_state
            data_type = state.get('type')
            
            if data_type == 'tick_file':
                # 重新加载tick文件数据
                self.load_tick_data_file(
                    state.get('file_path'),
                    state.get('stock_code'),
                    state.get('full_code')
                )
            elif data_type == 'kline_file':
                # 重新加载K线数据
                self.load_kline_data_file(
                    state.get('file_path'),
                    state.get('period_type'),
                    state.get('stock_code', '')
                )
            elif data_type == 'period_files':
                # 重新显示周期文件列表
                self.show_period_files(state.get('data'))
            elif data_type == 'tick_stock_list':
                # 重新显示tick股票列表
                self.show_tick_stock_list(state.get('data'))
            elif data_type == 'tick_date_files':
                # 重新显示tick日期文件列表
                self.show_tick_date_files(state.get('data'))
            
            # 滚动条回到顶部
            self.scroll_to_top()
            
        except Exception as e:
            self.info_label.setText(f"刷新数据失败: {e}")
            logging.error(f"刷新数据失败: {e}")
    
    def scroll_to_top(self):
        """滚动条回到顶部"""
        if self.table_widget.rowCount() > 0:
            self.table_widget.scrollToTop()
            self.table_widget.setCurrentCell(0, 0)  # 选中第一行第一列
    
    def reorder_columns_for_display(self, columns):
        """重新排序列，tick数据按照标准顺序，其他数据确保涨跌相关字段在最后"""
        
        # 检查是否为tick数据（包含最新价字段）
        if '最新价' in columns:
            # tick数据的标准字段顺序
            tick_order = [
                '时间',           # time
                '最新价',         # lastPrice
                '开盘价',         # open
                '最高价',         # high
                '最低价',         # low
                '前收盘价',       # lastClose
                '成交总额',       # amount
                '成交总量',       # volume
                '原始成交总量',   # pvolume
                '证券状态',       # stockStatus
                '持仓量',         # openInt
                '前结算',         # lastSettlementPrice
                '委卖价',         # askPrice
                '委买价',         # bidPrice
                '委卖量',         # askVol
                '委买量',         # bidVol
                '成交笔数',       # transactionNum
                '涨跌额',         # change (计算字段，放在最后)
                '涨跌幅%'         # change_pct (计算字段，放在最后)
            ]
            
            # 按照标准顺序重排tick数据字段
            ordered_columns = []
            remaining_columns = list(columns)
            
            # 先按照标准顺序添加字段
            for field in tick_order:
                if field in remaining_columns:
                    ordered_columns.append(field)
                    remaining_columns.remove(field)
            
            # 添加剩余的未匹配字段
            ordered_columns.extend(remaining_columns)
            
            return ordered_columns
        
        else:
            # 非tick数据：确保涨跌相关字段在最后
            # 定义涨跌相关字段的优先级（数字越大越靠后）
            change_fields_priority = {
                'change': 2,        # 涨跌额
                'change_amount': 2, # 涨跌量
                'pct_change': 3,    # 涨跌幅
                'change_rate': 3,   # 涨跌率
                '涨跌额': 2,
                '涨跌值': 2,
                '涨跌量': 2,
                '涨跌幅': 3,
                '涨跌率': 3
            }
            
            # 分离涨跌字段和其他字段
            change_fields = []
            other_fields = []
            
            for col in columns:
                col_lower = col.lower()
                col_str = str(col)
                
                # 检查是否为涨跌相关字段
                is_change_field = False
                priority = 0
                
                # 检查精确匹配
                if col in change_fields_priority:
                    is_change_field = True
                    priority = change_fields_priority[col]
                elif col_lower in change_fields_priority:
                    is_change_field = True
                    priority = change_fields_priority[col_lower]
                else:
                    # 检查包含关系
                    for field, p in change_fields_priority.items():
                        if field.lower() in col_lower or field in col_str:
                            is_change_field = True
                            priority = p
                            break
                
                if is_change_field:
                    change_fields.append((col, priority))
                else:
                    other_fields.append(col)
            
            # 按优先级排序涨跌字段
            change_fields.sort(key=lambda x: x[1])
            sorted_change_fields = [field[0] for field in change_fields]
            
            # 返回重新排序的列：其他字段 + 涨跌字段
            return other_fields + sorted_change_fields
    
    def get_chinese_field_names(self, columns):
        """将英文字段名转换为中文显示名称"""
        # 完整的字段映射表
        field_name_mapping = {
            # 通用时间字段
            'time': '时间',
            'datetime': '时间',
            'timestamp': '时间戳',
            
            # tick数据字段
            'lastPrice': '最新价',
            'last': '最新价',
            'price': '价格',
            'open': '开盘价',
            'high': '最高价',
            'low': '最低价',
            'close': '收盘价',
            'lastClose': '前收盘价',
            'preClose': '前收价',
            'prevClose': '前收盘价',
            'amount': '成交总额',
            'turnover': '成交额',
            'volume': '成交总量',
            'vol': '成交量',
            'pvolume': '原始成交总量',
            'stockStatus': '证券状态',
            'status': '状态',
            'openInt': '持仓量',
            'openInterest': '持仓量',
            'oi': '持仓量',
            'lastSettlementPrice': '前结算',
            'preSettlement': '前结算',
            'settlement': '结算价',
            'settelementPrice': '今结算',
            'settlementPrice': '今结算',
            'askPrice': '委卖价',
            'ask': '委卖价',
            'bidPrice': '委买价',
            'bid': '委买价',
            'askVol': '委卖量',
            'askVolume': '委卖量',
            'bidVol': '委买量',
            'bidVolume': '委买量',
            'transactionNum': '成交笔数',
            'tradeNum': '成交笔数',
            'numTrades': '成交笔数',
            'suspendFlag': '停牌标记',
            'suspend': '停牌',
            
            # 中文字段映射（保持一致性）
            '最新价': '最新价',
            '开盘价': '开盘价',
            '最高价': '最高价',
            '最低价': '最低价',
            '前收盘价': '前收盘价',
            '成交总额': '成交总额',
            '成交总量': '成交总量',
            '原始成交总量': '原始成交总量',
            '证券状态': '证券状态',
            '持仓量': '持仓量',
            '前结算': '前结算',
            '委卖价': '委卖价',
            '委买价': '委买价',
            '委卖量': '委卖量',
            '委买量': '委买量',
            '成交笔数': '成交笔数',
            
            # K线数据额外字段
            'avgPrice': '均价',
            'vwap': '成交均价',
            'upperLimit': '涨停价',
            'lowerLimit': '跌停价',
            'limitUp': '涨停价',
            'limitDown': '跌停价',
            
            # 可能的计算字段（但有些可能是原始数据）
            'change': '涨跌额',
            'change_amount': '涨跌量',
            'pct_change': '涨跌幅',
            'change_rate': '涨跌幅',
            'pct_chg': '涨跌幅',
            'change_pct': '涨跌幅',
            'turnover_rate': '换手率',
            'turnoverRate': '换手率',
            'amplitude': '振幅',
            'amp': '振幅',
            
            # 技术指标字段
            'pe_ratio': '市盈率',
            'pe': '市盈率',
            'pb_ratio': '市净率',
            'pb': '市净率',
            'ma5': '5日均线',
            'ma10': '10日均线',
            'ma20': '20日均线',
            'ma30': '30日均线',
            'ma60': '60日均线',
            'ema': '指数均线',
            'rsi': 'RSI',
            'macd': 'MACD',
            'bias': 'BIAS',
            'cci': 'CCI',
            'kdj_k': 'KDJ_K',
            'kdj_d': 'KDJ_D',
            'kdj_j': 'KDJ_J',
            
            # 其他可能的字段
            'totalValue': '总市值',
            'marketValue': '流通市值',
            'floatValue': '流通市值',
            'totalShares': '总股本',
            'floatShares': '流通股本',
            'freeShares': '自由流通股本',
            
            # tick数据特有字段
            '总手数': '总手数',
            '外盘': '外盘',
            '内盘': '内盘',
            '委比%': '委比%',
            '委差': '委差',
            '成交方向': '成交方向',
            '现价': '现价',
            '买一价': '买一价', '买一量': '买一量',
            '买二价': '买二价', '买二量': '买二量',
            '买三价': '买三价', '买三量': '买三量',
            '买四价': '买四价', '买四量': '买四量',
            '买五价': '买五价', '买五量': '买五量',
            '卖一价': '卖一价', '卖一量': '卖一量',
            '卖二价': '卖二价', '卖二量': '卖二量',
            '卖三价': '卖三价', '卖三量': '卖三量',
            '卖四价': '卖四价', '卖四量': '卖四量',
            '卖五价': '卖五价', '卖五量': '卖五量'
        }
        
        chinese_names = []
        for col in columns:
            # 如果有映射，使用中文名称，否则保持原名
            if col in field_name_mapping:
                chinese_names.append(field_name_mapping[col])
            else:
                # 尝试小写匹配
                col_lower = col.lower()
                if col_lower in field_name_mapping:
                    chinese_names.append(field_name_mapping[col_lower])
                else:
                    chinese_names.append(col)
        
        return chinese_names
 
    def load_stocks_from_selected_files(self):
        """从选中的股票文件加载股票到列表中"""
        try:
            # 获取选中的股票文件列表
            stock_files = []
            current_preview = self.stock_files_preview.toPlainText().strip()
            if current_preview and current_preview != "未选择任何股票列表文件":
                stock_files = current_preview.split('\n')
            
            if not stock_files:
                return
            
            # 清空当前列表
            self.stock_list_table.setRowCount(0)
            
            # 从所有选中的文件中加载股票
            total_added = 0
            for file_path in stock_files:
                if os.path.exists(file_path):
                    try:
                        with open(file_path, 'r', encoding='utf-8-sig') as f:
                            lines = f.readlines()
                        
                        for line in lines:
                            line = line.strip()
                            if line and not line.startswith('#'):
                                parts = line.split(',')
                                if len(parts) >= 1:
                                    code = parts[0].strip().replace('\ufeff', '')
                                    name = parts[1].strip() if len(parts) > 1 else ""
                                    
                                    # 如果没有名称，尝试从股票名称缓存获取
                                    if not name and code in self.stock_names_cache:
                                        name = self.stock_names_cache[code]
                                    
                                    # 检查是否已存在
                                    exists = False
                                    for row in range(self.stock_list_table.rowCount()):
                                        if self.stock_list_table.item(row, 0).text() == code:
                                            exists = True
                                            break
                                    
                                    # 如果不存在则添加
                                    if not exists:
                                        row = self.stock_list_table.rowCount()
                                        self.stock_list_table.insertRow(row)
                                        self.stock_list_table.setItem(row, 0, QTableWidgetItem(code))
                                        self.stock_list_table.setItem(row, 1, QTableWidgetItem(name))
                                        total_added += 1
                    except Exception as e:
                        logging.warning(f"读取股票文件 {file_path} 失败: {str(e)}")
            
            self.update_stock_count()
            if total_added > 0:
                self.supplement_status_label.setText(f"已从文件加载 {total_added} 只股票")
                
        except Exception as e:
            error_msg = f"加载股票文件时出错: {str(e)}"
            self.supplement_status_label.setText(error_msg)
            logging.error(error_msg)


def main():
    app = QApplication(sys.argv)
    
    # 设置应用图标
    try:
        app.setWindowIcon(QIcon('icons/stock_icon.ico'))
    except:
        pass
    
    viewer = MiniQMTDataViewer()
    viewer.show()
    
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()