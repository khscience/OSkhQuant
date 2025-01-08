# 该文件为GUI.py
import sys
import os
import ctypes
import subprocess
import requests
import hashlib,threading
import psutil
import pandas as pd
import numpy as np
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QLineEdit, QTextEdit, QPushButton, QFileDialog, QMessageBox, QProgressBar, 
                             QComboBox, QDateEdit, QTimeEdit, QGroupBox, QScrollArea, QCheckBox, QGridLayout,
                             QDialog,QTabWidget,QSplashScreen,QProgressDialog,QMenu, QStyle)  # 添加QStyle
from PyQt5.QtCore import  Qt, QThread, pyqtSignal, QDate, QTime, QRect, QTimer,QSettings,QPoint
from PyQt5.QtGui import QPen,QPixmap,QFont, QIcon, QPalette, QColor, QLinearGradient, QCursor, QPixmap, QPainter
from khQTTools import download_and_store_data,get_and_save_stock_list, supplement_history_data
from PyQt5 import QtCore
import logging
from GUIplotLoadData import StockDataAnalyzerGUI  # 添加这一行导入
#from activation_manager import ActivationCodeGenerator, MachineCode, ActivationManager
#from activation_thread import ActivationCheckThread  # 添加这一行
from update_manager import UpdateManager  # 将之前的UpdateManager类保存在单独的update_manager.py文件中
from version import get_version_info  # 导入版本信息


# 获取当前文件的上级目录
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.append(PARENT_DIR)

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')

# 在类的开头（__init__之前）添加图标路径的定义
#ICON_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'icons')
ICON_PATH = os.path.join(os.path.dirname(__file__), 'icons')
os.makedirs(LOGS_DIR, exist_ok=True)


# 配置日志记录
# filename: 指定日志文件的路径，将日志保存到LOGS_DIR目录下的app.log文件中
# level: 设置日志级别为DEBUG,记录所有级别的日志信息
# format: 设置日志格式,包含时间戳、日志级别和具体消息
# filemode: 设置文件模式为'w',即每次运行时覆盖之前的日志文件
logging.basicConfig(
    filename=os.path.join(LOGS_DIR, 'app.log'),
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s', 
    filemode='w'
)

# 同时将日志输出到控制台
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logging.getLogger('').addHandler(console_handler)

# 保持原有的HelpDialog类不变


# 保持原有的DownloadThread类不变
class DownloadThread(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str)
    error = pyqtSignal(str)  # 添加错误信号

    def __init__(self, params, parent=None):  # 添加parent参数
        super().__init__(parent)
        self.params = params
        self.running = True
        logging.info(f"初始化下载线程，参数: {params}")

    def run(self):
        try:
            if not self.running:
                return
                
            logging.info("下载线程开始运行")
            
            # 参数验证
            if not self.params.get('local_data_path'):
                raise ValueError("数据存储路径未设置")
                
            if not self.params.get('stock_files'):
                raise ValueError("股票代码列表为空")
                
            if not self.params.get('field_list'):
                raise ValueError("字段列表为空")

            def progress_callback(percent):
                if not self.running:
                    raise InterruptedError("下载被中断")
                try:
                    self.progress.emit(percent)
                except Exception as e:
                    logging.error(f"进度错误: {str(e)}")

            # 分离指数文件和普通股票文件
            index_files = []
            stock_files = []
            for file_path in self.params['stock_files']:
                if '指数_股票列表' in file_path:
                    index_files.append(file_path)
                else:
                    stock_files.append(file_path)

            # 分别处理指数和普通股票
            total_files = len(index_files) + len(stock_files)
            current_progress = 0

            # 下载指数数据
            if index_files:
                try:
                    params_index = {
                        'local_data_path': self.params['local_data_path'],
                        'stock_files': index_files,
                        'field_list': self.params['field_list'],
                        'period_type': self.params['period_type'],
                        'start_date': self.params['start_date'],
                        'end_date': self.params['end_date'],
                        'time_range': self.params.get('time_range', 'all')
                    }
                    # 计算进度的回调函数
                    progress_cb = lambda p: self.progress.emit(
                        int(current_progress * 100 / total_files + p * len(index_files) / total_files)
                    )
                    download_and_store_data(**params_index, progress_callback=progress_cb)
                    current_progress += len(index_files)
                except Exception as e:
                    logging.error(f"下载指数数据时出错: {str(e)}")
                    raise

            # 下载普通股票数据
            if stock_files:
                try:
                    params_stock = {
                        'local_data_path': self.params['local_data_path'],
                        'stock_files': stock_files,
                        'field_list': self.params['field_list'],
                        'period_type': self.params['period_type'],
                        'start_date': self.params['start_date'],
                        'end_date': self.params['end_date'],
                        'time_range': self.params.get('time_range', 'all')
                    }
                    # 计算进度的回调函数
                    progress_cb = lambda p: self.progress.emit(
                        int(current_progress * 100 / total_files + p * len(stock_files) / total_files)
                    )
                    download_and_store_data(**params_stock, progress_callback=progress_cb)
                except Exception as e:
                    logging.error(f"下载股票数据时出错: {str(e)}")
                    raise

            if self.running:
                self.finished.emit(True, "数据下载完成！")
                
        except Exception as e:
            error_msg = f"下载过程中发生错误: {str(e)}"
            logging.error(error_msg, exc_info=True)
            import traceback
            logging.error(traceback.format_exc())
            
            if self.running:
                self.error.emit(error_msg)
                self.finished.emit(False, error_msg)

    def stop(self):
        self.running = False

def closeEvent(self, event):
        """窗口关闭时的处理"""
        try:
            # 停止所有定时器
            if hasattr(self, 'status_timer'):
                self.status_timer.stop()
            if hasattr(self, 'refresh_timer'):
                self.refresh_timer.stop()
            
            # 停止下载线程
            if hasattr(self, 'download_thread') and self.download_thread:
                logging.info("正在停止下载线程...")
                self.download_thread.stop()
                self.download_thread.wait()
                self.download_thread = None
                logging.info("下载线程已停止")

            # 停止补充数据线程
            if hasattr(self, 'supplement_thread') and self.supplement_thread:
                logging.info("正在停止补充数据线程...")
                self.supplement_thread.stop()
                self.supplement_thread.wait()
                self.supplement_thread = None
                logging.info("补充数据线程已停止")

            # 停止清洗线程
            if hasattr(self, 'cleaner_thread') and self.cleaner_thread:
                logging.info("正在停止清洗线程...")
                self.cleaner_thread.terminate()
                self.cleaner_thread.wait()
                self.cleaner_thread = None
                logging.info("清洗线程已停止")

            # 停止更新线程
            if hasattr(self, 'update_thread') and self.update_thread:
                logging.info("正在停止更新线程...")
                self.update_thread.stop()
                self.update_thread.wait()
                self.update_thread = None
                logging.info("更新线程已停止")

            # 关闭可视化窗口
            if hasattr(self, 'visualization_window') and self.visualization_window:
                logging.info("正在关闭可视化窗口...")
                self.visualization_window.close()
                self.visualization_window = None
                logging.info("可视化窗口已关闭")

            logging.info("程序正常退出")
            event.accept()
            
        except Exception as e:
            logging.error(f"程序退出时出错: {str(e)}", exc_info=True)
            event.accept()  # 确保程序能够退出

class StockDataCleaner:
    def __init__(self):
        self.df = None
        self.columns = []
        self.row_changes = {}
        self.deleted_rows = {}
        self.reset()
        
    def reset(self):
        self.df = None
        self.columns = []
        self.row_changes = {}
        self.deleted_rows = {}
        
    def load_data(self, file_path):
        self.reset()
        self.df = pd.read_csv(file_path)
        self.columns = self.df.columns.tolist()
        return self

    def clean_data(self):
        if self.df is None:
            raise ValueError("未加载数据。请先调用 load_data() 方法。")
        
        self.remove_duplicates()
        self.handle_missing_values()
        self.correct_data_types()
        self.remove_outliers()
        self.handle_non_trading_hours()
        self.sort_data()
        return self

    def remove_duplicates(self):
        initial_rows = len(self.df)
        
        # 识别时间戳列
        time_columns = [col for col in self.df.columns if 'time' in col.lower() or 'date' in col.lower()]
        
        if time_columns:
            # 首先按时间戳检查重复
            time_duplicates = self.df[self.df.duplicated(subset=time_columns, keep='first')]
            
            # 对于时间戳重复的行，进一步检查其他数据是否也重复
            full_duplicates = self.df[self.df.duplicated(keep='first')]
            
            # 保存两种重复情况的统计
            self.duplicate_stats = {
                'time_duplicate_count': len(time_duplicates),
                'full_duplicate_count': len(full_duplicates),
                'time_only_duplicates': len(time_duplicates) - len(full_duplicates)
            }
            
            # 可以选择保留时间戳相同但数据不同的记录
            # 这种情况可能是同一时刻的多笔交易
            self.df = self.df.drop_duplicates(keep='first')
            
            # 记录细节信息
            self.deleted_rows['remove_duplicates'] = {
                'time_duplicates': time_duplicates,
                'full_duplicates': full_duplicates
            }
            
            # 添加警告日志
            if len(time_duplicates) > len(full_duplicates):
                logging.warning(
                    f"发现{len(time_duplicates) - len(full_duplicates)}行时间戳重复但数据不完全相同的记录，"
                    "这可能表示同一时刻的多笔交易"
                )
                
        else:
            # 如果没有识别到时间列，按所有列查重
            duplicates = self.df[self.df.duplicated()]
            self.df = self.df.drop_duplicates(inplace=True)
            self.deleted_rows['remove_duplicates'] = duplicates
        
        final_rows = len(self.df)
        self.row_changes['remove_duplicates'] = initial_rows - final_rows

    def handle_missing_values(self):
        initial_rows = len(self.df)
        rows_with_missing = self.df[self.df.isnull().any(axis=1)]
        
        price_columns = [col for col in ['open', 'high', 'low', 'close'] if col in self.columns]
        if price_columns:
            self.df[price_columns] = self.df[price_columns].ffill()
        
        volume_columns = [col for col in ['volume'] if col in self.columns]
        if volume_columns:
            self.df[volume_columns] = self.df[volume_columns].fillna(0)
        
        self.df.dropna(inplace=True)
        final_rows = len(self.df)
        self.row_changes['handle_missing_values'] = initial_rows - final_rows
        self.deleted_rows['handle_missing_values'] = rows_with_missing

    def correct_data_types(self):
        date_columns = [col for col in self.columns if 'date' in col.lower()]
        for col in date_columns:
            self.df[col] = pd.to_datetime(self.df[col], errors='coerce')
        
        numeric_columns = [col for col in ['open', 'high', 'low', 'close', 'volume'] if col in self.columns]
        for col in numeric_columns:
            self.df[col] = pd.to_numeric(self.df[col], errors='coerce')

    def remove_outliers(self):
        initial_rows = len(self.df)
        outliers = pd.DataFrame()
        price_columns = [col for col in ['open', 'high', 'low', 'close'] if col in self.columns]
        for col in price_columns:
            Q1 = self.df[col].quantile(0.25)
            Q3 = self.df[col].quantile(0.75)
            IQR = Q3 - Q1
            lower_bound = Q1 - 5 * IQR
            upper_bound = Q3 + 5 * IQR
            col_outliers = self.df[(self.df[col] < lower_bound) | (self.df[col] > upper_bound)]
            outliers = pd.concat([outliers, col_outliers])
            self.df = self.df[(self.df[col] >= lower_bound) & (self.df[col] <= upper_bound)]
        final_rows = len(self.df)
        self.row_changes['remove_outliers'] = initial_rows - final_rows
        self.deleted_rows['remove_outliers'] = outliers

    def handle_non_trading_hours(self):
        initial_rows = len(self.df)
        non_trading_hours = pd.DataFrame()  # 初始化变量
        if 'time' in self.columns:
            morning_start = pd.to_datetime('09:30:00').time()
            morning_end = pd.to_datetime('11:30:00').time()
            afternoon_start = pd.to_datetime('13:00:00').time()
            afternoon_end = pd.to_datetime('15:00:00').time()

            self.df['time'] = pd.to_datetime(self.df['time'], errors='coerce').dt.time

            # 修复括号匹配问题
            non_trading_hours = self.df[
                ~(((self.df['time'] >= morning_start) & (self.df['time'] <= morning_end)) |
                  ((self.df['time'] >= afternoon_start) & (self.df['time'] <= afternoon_end)))
            ]

            self.df = self.df[
                ((self.df['time'] >= morning_start) & (self.df['time'] <= morning_end)) |
                ((self.df['time'] >= afternoon_start) & (self.df['time'] <= afternoon_end))
            ]
        
        final_rows = len(self.df)
        self.row_changes['handle_non_trading_hours'] = initial_rows - final_rows
        self.deleted_rows['handle_non_trading_hours'] = non_trading_hours

    def sort_data(self):
        sort_columns = [col for col in self.columns if 'date' in col.lower() or 'time' in col.lower()]
        if sort_columns:
            self.df.sort_values(by=sort_columns, inplace=True)

    def get_cleaned_data(self):
        return self.df

    def save_cleaned_data(self, file_path):
        self.df.to_csv(file_path, index=False)

    def get_column_info(self):
        return {
            'all_columns': self.columns,
            'date_columns': [col for col in self.columns if 'date' in col.lower()],
            'time_columns': [col for col in self.columns if 'time' in col.lower()],
            'price_columns': [col for col in ['open', 'high', 'low', 'close'] if col in self.columns],
            'volume_columns': [col for col in ['volume'] if col in self.columns]
        }

    def get_data_info(self):
        return {
            'shape': self.df.shape,
            'dtypes': self.df.dtypes.to_dict(),
            'missing_values': self.df.isnull().sum().to_dict(),
            'duplicates': self.df.duplicated().sum(),
            'numeric_stats': self.df.describe().to_dict(),
            'row_changes': self.row_changes,
            'deleted_rows': self.deleted_rows
        }

class SettingsDialog(QDialog):
    """设置对话框类"""
    #activation_completed = pyqtSignal(bool)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        #self.activation_manager = ActivationManager()
        self.settings = QSettings('KHQuant', 'StockAnalyzer')
        self.confirmed_exit = False
        
        # 设置窗口标志
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setWindowModality(Qt.ApplicationModal)
        
        # 如果未激活，显示激活界面，否则显示设置界面
        # if not self.activation_manager.is_activated():
        #     self.initActivationUI()
        # else:
        #     self.initUI()
        self.initUI()
        # 移除这行重复的日志
        # logging.info("激活对话框初始化完成")

    def initActivationUI(self):
        """初始化激活界面"""
        try:
            # 只在初始化时记录一次日志
            logging.debug("初始化激活界面")
            layout = QVBoxLayout(self)
            
            # 添加标题
            title_label = QLabel("软件激活")
            title_label.setFont(QFont("Roboto", 14, QFont.Bold))
            title_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(title_label)
            
            # 添加激活码输入框
            activation_group = QGroupBox("激活信息")
            activation_layout = QVBoxLayout()
            
            # 获取机器码
            try:
                machine_code = self.activation_manager.machine_code
            except Exception as e:
                logging.error(f"获取机器码失败: {str(e)}")
                machine_code = "获取失败"
            
            machine_code_label = QLabel(f"机器码: {machine_code}")
            activation_layout.addWidget(machine_code_label)
            
            # 激活码输入
            self.activation_code_input = QLineEdit()
            self.activation_code_input.setPlaceholderText("请输入激活码")
            activation_layout.addWidget(self.activation_code_input)
            
            # 激活按钮
            activate_button = QPushButton("激活软件")
            activate_button.clicked.connect(self.activate_software)
            activation_layout.addWidget(activate_button)
            
            activation_group.setLayout(activation_layout)
            layout.addWidget(activation_group)
            
            # 添加取消按钮
            cancel_button = QPushButton("取消")
            cancel_button.clicked.connect(self.reject)
            layout.addWidget(cancel_button)
            
            self.setLayout(layout)
            self.setMinimumWidth(400)
            self.setWindowTitle("软件激活")
            
        except Exception as e:
            logging.error(f"初始化软件激活界面错误: {str(e)}", exc_info=True)
            QMessageBox.critical(self, "错误", f"初始化激活界面时出错: {str(e)}")

    def initUI(self):
        """设置对话框UI初始化"""
        self.setWindowTitle('软件设置')
        self.setMinimumWidth(500)
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        
        # 主布局
        layout = QVBoxLayout(self)
        layout.setSpacing(15)  # 增加组件之间的间距
        
        # 首先添加股票列表管理组
        stock_list_group = QGroupBox("股票列表管理")
        stock_list_group.setStyleSheet("""
            QGroupBox {
                border: 1px solid #3D3D3D;
                border-radius: 5px;
                margin-top: 12px;
                padding-top: 15px;
                color: #E0E0E0;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 7px;
                padding: 0 3px;
            }
        """)
        stock_list_layout = QVBoxLayout()
        
        # 添加更新按钮
        update_stock_list_btn = QPushButton("更新成分股列表（运行时需耐心等待，无需频繁更新")
        update_stock_list_btn.clicked.connect(self.update_stock_list)
        stock_list_layout.addWidget(update_stock_list_btn)
        
        stock_list_group.setLayout(stock_list_layout)
        layout.addWidget(stock_list_group)

        # 客户端路径设置组
        client_group = QGroupBox("客户端设置")
        client_group.setStyleSheet("""
            QGroupBox {
                border: 1px solid #3D3D3D;
                border-radius: 5px;
                margin-top: 12px;
                padding-top: 15px;
                color: #E0E0E0;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 7px;
                padding: 0 3px;
            }
        """)
        path_layout = QVBoxLayout()
        path_label = QLabel("miniQMT客户端路径:")
        path_label.setStyleSheet("color: #E0E0E0;")
        path_layout.addWidget(path_label)
        
        # 路径输入和浏览按钮放在单独的水平布局中
        input_layout = QHBoxLayout()
        self.client_path_edit = QLineEdit()
        self.client_path_edit.setMinimumWidth(400)  # 增加宽度
        default_path = r"C:\国金证券QMT交易端\bin.x64\XtItClient.exe"
        saved_path = self.settings.value('client_path', default_path)
        self.client_path_edit.setText(saved_path)
        self.client_path_edit.setToolTip("请选择miniQMT客户端启动程序XtItClient.exe")
        self.client_path_edit.setStyleSheet("""
            QLineEdit {
                background-color: #2D2D2D;
                border: 1px solid #3D3D3D;
                border-radius: 3px;
                padding: 5px;
                color: #E0E0E0;
            }
        """)
        
        browse_button = QPushButton("浏览")
        browse_button.setFixedWidth(60)
        browse_button.clicked.connect(self.browse_client)
        browse_button.setStyleSheet("""
            QPushButton {
                background-color: #3D3D3D;
                color: #E0E0E0;
                border: none;
                padding: 5px;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #4D4D4D;
            }
        """)
        
        input_layout.addWidget(self.client_path_edit)
        input_layout.addWidget(browse_button)
        path_layout.addLayout(input_layout)
        client_group.setLayout(path_layout)
        layout.addWidget(client_group)
        
        # 版本信息组
        version_group = QGroupBox("版本信息")
        version_group.setStyleSheet("""
            QGroupBox {
                border: 1px solid #3D3D3D;
                border-radius: 5px;
                margin-top: 12px;
                padding-top: 15px;
                color: #E0E0E0;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 7px;
                padding: 0 3px;
            }
        """)
        version_layout = QVBoxLayout()
        version_info = get_version_info()
        version_label = QLabel(f"当前版本：v{version_info['version']}")
        version_label.setStyleSheet("color: #E0E0E0;")
        version_layout.addWidget(version_label)
        version_group.setLayout(version_layout)
        layout.addWidget(version_group)
        
        # 底部按钮布局
        button_layout = QHBoxLayout()
        
        # 添加反馈问题按钮（靠左）
        feedback_button = QPushButton("反馈问题")
        feedback_button.setFixedWidth(100)
        feedback_button.setStyleSheet("""
            QPushButton {
                background-color: #2D2D2D;
                color: #E0E0E0;
                border: none;
                padding: 5px 15px;
                border-radius: 2px;
            }
            QPushButton:hover {
                background-color: #3D3D3D;
            }
        """)
        feedback_button.clicked.connect(self.open_feedback_page)
        button_layout.addWidget(feedback_button)
        
        # 添加弹性空间，使保存和关闭按钮靠右
        button_layout.addStretch()
        
        # 保存和关闭按钮（靠右）
        save_button = QPushButton("保存设置")
        save_button.setFixedWidth(100)
        save_button.setStyleSheet("""
            QPushButton {
                background-color: #0078D7;
                color: white;
                border: none;
                padding: 5px 15px;
                border-radius: 2px;
            }
            QPushButton:hover {
                background-color: #1984D8;
            }
        """)
        save_button.clicked.connect(self.save_settings)
        
        close_button = QPushButton("关闭")
        close_button.setFixedWidth(100)
        close_button.setStyleSheet("""
            QPushButton {
                background-color: #3D3D3D;
                color: #E0E0E0;
                border: none;
                padding: 5px 15px;
                border-radius: 2px;
            }
            QPushButton:hover {
                background-color: #4D4D4D;
            }
        """)
        close_button.clicked.connect(self.close)
        
        button_layout.addWidget(save_button)
        button_layout.addWidget(close_button)
        
        layout.addLayout(button_layout)
        
        # 设置整体背景色
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
            }
        """)

    def browse_client(self):
        """浏览选择客户端路径"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择miniQMT客户端程序",  # 更新这里的提示文字
            self.client_path_edit.text(),
            "可执行文件 (*.exe)"
        )
        if file_path:
            self.client_path_edit.setText(file_path)
            
    def save_settings(self):
        """保存设置"""
        try:
            client_path = self.client_path_edit.text().strip()
            if not os.path.exists(client_path):
                QMessageBox.warning(self, "警告", "指定的客户端路径不存在")
                return
                
            self.settings.setValue('client_path', client_path)
            QMessageBox.information(self, "成功", "设置已保存")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存设置时出错: {str(e)}")

    # 添加打开反馈页面的方法
    def open_feedback_page(self):
        """打开反馈问题页面"""
        url = "https://khsci.com/khQuant/feedback"
        import webbrowser
        webbrowser.open(url)

    # 在 SettingsDialog 类中添加更新股票列表的方法
    def update_stock_list(self):
        """更新股票列表"""
        try:
            # 禁用按钮
            update_stock_list_btn = self.findChild(QPushButton, "update_stock_list_btn")
            if update_stock_list_btn:
                update_stock_list_btn.setEnabled(False)
            # 创建进度对话框
            self.progress_dialog = QProgressDialog("正在更新股票列表...", None, 0, 0, self)
            self.progress_dialog.setWindowModality(Qt.WindowModal)
            self.progress_dialog.setCancelButton(None)
            self.progress_dialog.show()
            
            # 修改这里：使用code目录下的data文件夹
            data_dir = os.path.join(os.path.dirname(__file__), 'data')
            os.makedirs(data_dir, exist_ok=True)
            
            # 获取更新线程
            update_thread = get_and_save_stock_list(data_dir)
            
            # 连接信号
            update_thread.progress.connect(self.show_update_progress)
            update_thread.finished.connect(self.handle_update_finished)
            
            # 保存线程引用
            self.update_thread = update_thread
            
        except Exception as e:
            # 恢复按钮
            if update_stock_list_btn:
                update_stock_list_btn.setEnabled(True)
            self.progress_dialog.close()
            logging.error(f"更新股票列表时出错: {str(e)}", exc_info=True)
            QMessageBox.critical(self, "错误", f"更新股票列表时出错: {str(e)}")

    def show_update_progress(self, message):
        """显示更新进度"""
        if hasattr(self, 'progress_dialog'):
            self.progress_dialog.setLabelText(message)

    def handle_update_finished(self, success, message):
        """处理更新完成"""
        if hasattr(self, 'progress_dialog'):
            self.progress_dialog.close()
        
        if success:
            QMessageBox.information(self, "成功", "股票列表更新成功！")
        else:
            QMessageBox.warning(self, "失败", f"更新股票列表失败：{message}")
        
        # 清理线程
        if hasattr(self, 'update_thread'):
            self.update_thread = None

class CleanerThread(QThread):
    progress_updated = pyqtSignal(int, int)
    cleaning_completed = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)

    def __init__(self, cleaner, folder_path, operations):
        super().__init__()
        self.cleaner = cleaner
        self.folder_path = folder_path
        self.operations = operations

    def run(self):
        try:
            csv_files = [f for f in os.listdir(self.folder_path) if f.endswith('.csv')]
            total_files = len(csv_files)
            cleaning_info = {}

            for file_index, file in enumerate(csv_files):
                file_path = os.path.join(self.folder_path, file)
                
                # 创建临时备份
                backup_path = file_path + '.bak'
                with open(file_path, 'r', encoding='utf-8') as source:
                    with open(backup_path, 'w', encoding='utf-8') as target:
                        target.write(source.read())
                
                try:
                    self.cleaner.load_data(file_path)
                    before_info = self.cleaner.get_data_info()
                    
                    total_steps = len(self.operations)
                    for i, operation in enumerate(self.operations):
                        if hasattr(self.cleaner, operation):
                            getattr(self.cleaner, operation)()
                        
                        file_progress = int((i + 1) / total_steps * 100)
                        total_progress = int((file_index * 100 + file_progress) / total_files)
                        self.progress_updated.emit(file_progress, total_progress)
                    
                    self.cleaner.save_cleaned_data(file_path)
                    after_info = self.cleaner.get_data_info()
                    
                    cleaning_info[file] = {
                        'before': before_info,
                        'after': after_info
                    }
                    
                    os.remove(backup_path)
                    
                except Exception as e:
                    if os.path.exists(backup_path):
                        os.replace(backup_path, file_path)
                    raise e

            self.cleaning_completed.emit(cleaning_info)
            
        except Exception as e:
            self.error_occurred.emit(str(e))
class StockDataProcessorGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        
        # 检查QSettings存储位置
        settings = QSettings('KHQuant', 'StockAnalyzer')
        logging.info(f"QSettings存储位置: {settings.fileName()}")
        logging.info(f"QSettings格式: {settings.format()}")
        logging.info(f"QSettings范围: {settings.scope()}")
        
        # 移除激活相关的初始化代码
        self._activation_warning_shown = False

        # 修改图标路径的获取方式
        if getattr(sys, 'frozen', False):
            # 打包后的环境，图标文件在 _internal/icons 目录下
            self.ICON_PATH = os.path.join(os.path.dirname(sys.executable), '_internal', 'icons')
        else:
            # 开发环境
            self.ICON_PATH = os.path.join(os.path.dirname(__file__), 'icons')
        
        # 确保图标目录存在
        os.makedirs(self.ICON_PATH, exist_ok=True)

        # 添加调试日志
        logging.info(f"初始化图标路径: {self.ICON_PATH}")
        if os.path.exists(self.ICON_PATH):
            logging.info(f"图标目录内容: {os.listdir(self.ICON_PATH)}")
        else:
            logging.warning(f"图标目录不存在: {self.ICON_PATH}")

        self.cleaner = StockDataCleaner()
        self.visualization_window = None

        # 初始化更新管理器（在其他初始化之前） 
        self.initialize_update_manager()

        # 修改窗口属性设置
        self.setAttribute(Qt.WA_TranslucentBackground, False)  # 禁用透明背景
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowSystemMenuHint)  # 添加系统菜单支持
        
        # 设置窗口背景色
        palette = self.palette()
        palette.setColor(QPalette.Window, QColor("#1E1E1E"))
        self.setPalette(palette)

        # 获取版本信息
        self.version_info = get_version_info()
        
        # 在启动画面显示版本信息
        if hasattr(self, 'splash'):
            self.version_label.setText(f"V{self.version_info['version']}")

        self.initUI()
        self.can_drag = False
        self.resizing = False
        self.resize_edge = None
        self.border_thickness = 20
        self.setMouseTracking(True)
        
        # 添加定时器来检查软件状态
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.check_software_status)
        self.status_timer.start(5000)
        
        # 初始软件检查
        self.check_and_open_software()
        
        # 添加一个计时器用于延迟刷新
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.timeout.connect(self.refresh_folder)

        # 创建状态栏
        self.statusBar().showMessage('就绪')

    def load_icon(self, icon_name):
        """统一的图标加载方法"""
        icon_path = os.path.join(self.ICON_PATH, icon_name)
        if os.path.exists(icon_path):
            return QIcon(icon_path)
        else:
            logging.warning(f"图标文件不存在: {icon_path}")
            fallback_icon = self.create_fallback_icon(icon_name)
            # 如果创建备用图标失败，返回一个空的QIcon
            return fallback_icon if fallback_icon else QIcon()

    def show_current_version(self):
        """显示当前版本信息"""
        # 直接使用UpdateManager的方法
        self.update_manager.show_current_version()
        
    def start_activation_check(self):
        """启动激活检查线程"""
        if self.activation_thread is None:
            #self.activation_thread = ActivationCheckThread(self.activation_manager)
            self.activation_thread.status_changed.connect(self.handle_activation_status)
            self.activation_thread.activation_invalidated.connect(self.handle_activation_invalidated)
            self.activation_thread.start()

    def handle_activation_invalidated(self):
        """处理激活失效的情况"""
        logging.info("激活已失效，准备重新激活")
        # 停止当前激活检查线程
        if self.activation_thread is not None:
            self.activation_thread.stop()
            self.activation_thread.wait()
            self.activation_thread = None

        # 禁用所有控件
        self.disable_all_controls()
        
        # 显示激活对话框
        QMessageBox.warning(self, "激活失效", "软件激活已失效，请重新激活。")
        self.show_activation_dialog()
    
    def handle_activation_status(self, success, message, connection_status):
        """处理激活状态变化"""
        if success:
            if connection_status == 'pending':
                # 避免重复记录相同状态
                if not hasattr(self, '_last_status') or self._last_status != 'pending':
                    self.update_status_indicator("green", "等待在线验证", connection_status='pending')
                    self._last_status = 'pending'
            else:
                if not hasattr(self, '_last_status') or self._last_status != 'online':
                    self.update_status_indicator("green", "在线运行", connection_status='online')
                    self._last_status = 'online'
        else:
            if connection_status == 'offline':
                if not hasattr(self, '_last_status') or self._last_status != 'offline':
                    self.update_status_indicator("red", message, connection_status='offline')
                    self._last_status = 'offline'
            else:
                if not hasattr(self, '_last_status') or self._last_status != 'pending':
                    self.update_status_indicator("yellow", message, connection_status='pending')
                    self._last_status = 'pending'

    def update_status_indicator(self, color, tooltip, connection_status=None):
        """
        更新状态指示器的颜色和提示信息
        """
        try:
            if hasattr(self, 'status_indicator'):
                # 获取当前MiniQMT状态
                miniQMT_status = "MiniQMT已启动" if self.is_software_running("XtMiniQmt.exe") else "MiniQMT未启动"
                
                # 确定显示颜色的逻辑
                if "未启动" in miniQMT_status:
                    display_color = "red"
                else:
                    # 不管是首次激活还是后续验证，只要是待在线验证状态就显示绿色
                    display_color = "green"
                
                # 确定激活状态提示信息
                if connection_status == 'pending':
                    activation_status = "等待在线验证"
                elif connection_status == 'online':
                    activation_status = "在线运行"
                else:
                    # 如果没有提供 connection_status，使用传入的 tooltip
                    activation_status = tooltip

                combined_tooltip = f"{miniQMT_status}\n{activation_status}"
                
                # 获取当前状态用于比较
                current_tooltip = self.status_indicator.toolTip()
                
                # 只在状态发生变化时更新并记录日志
                if current_tooltip != combined_tooltip:
                    self.status_indicator.setPixmap(self.create_colored_pixmap(display_color))
                    self.status_indicator.setToolTip(combined_tooltip)
                    logging.debug(f"状态指示器状态更新: {display_color} - {combined_tooltip}")
                    
        except Exception as e:
            logging.error(f"更新状态指示器时出错: {str(e)}", exc_info=True)

    def handle_activation_violation(self, message):
        """处理激活违规情况"""
        # 停止激活检查线程
        if self.activation_thread is not None:
            self.activation_thread.stop()
            self.activation_thread.wait()
            self.activation_thread = None
        
        # 显示警告消息
        QMessageBox.critical(
            self,
            "激活异常",
            f"软件激活状态异常: {message}\n软件将退出运行。",
            QMessageBox.Ok
        )
        
        # 删除本地激活文件并退出
        try:
            self.activation_manager.deactivate()
        except:
            pass
            
        QApplication.quit()



    def check_activation_status(self):
        """检查软件激活状态"""
        return self.activation_manager.is_activated()
    
    def show_activation_dialog(self):
        """显示激活对话框"""
        dialog = SettingsDialog(self)
        #dialog.activation_completed.connect(self.handle_activation_result)
        dialog.exec_()

    def show_settings(self):
        """显示设置对话框"""
        dialog = SettingsDialog(self)
        #dialog.activation_completed.connect(self.handle_activation_result)
        dialog.exec_()

    def handle_activation_result(self, success):
        """处理激活结果"""
        if success:
            self.enable_all_controls()
            self.start_activation_check()  # 启动检查线程
            super().show()
        else:
            self.disable_all_controls()
            QApplication.quit()

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
        # if success and not message.startswith("当前已是最新版本"):
        #     QMessageBox.warning(self, "更新检查", message)



    def show_version_menu(self):
        """显示版本相关的菜单"""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #2D2D2D;
                border: 1px solid #3D3D3D;
                padding: 5px;
            }
            QMenu::item {
                padding: 5px 20px;
                color: #E0E0E0;
            }
            QMenu::item:selected {
                background-color: #3D3D3D;
            }
        """)
        
        # 添加菜单项
        check_update_action = menu.addAction("检查更新")
        about_action = menu.addAction("关于软件")
        
        # 获取按钮位置
        button = self.sender()
        if button:
            # 显示菜单
            action = menu.exec_(button.mapToGlobal(QPoint(0, button.height())))
            
            # 处理菜单选择
            if action == check_update_action:
                self.check_for_updates()
            elif action == about_action:
                self.show_current_version()

    # 在 initUI 方法中的工具栏部分添加版本按钮
    def add_version_menu(self):
        """添加版本相关菜单"""
        # 根据屏幕分辨率动态设置图标大小
        screen = QApplication.primaryScreen().geometry()
        screen_width = screen.width()
                
        # 根据屏幕宽度设置不同的图标大小
        if screen_width >= 2560:  # 2K及以上分辨率
            self.icon_size = 60
        elif screen_width >= 1920:  # 1080P
            self.icon_size = 40
        else:  # 较低分辨率
            self.icon_size = 32

        # 创建版本信息按钮
        version_btn = QPushButton()
        version_btn.setIcon(self.load_icon('version.png'))
        version_btn.setIconSize(QtCore.QSize(self.icon_size, self.icon_size))
        version_btn.setFixedSize(self.icon_size + 8, self.icon_size + 8)
        version_btn.setToolTip('版本信息')
        version_btn.clicked.connect(self.show_version_menu)
        version_btn.setObjectName("toolbarButton")
        
        # 在工具栏添加版本按钮
        toolbar_layout = self.findChild(QHBoxLayout, "toolbarLayout")
        if toolbar_layout:
            # 在设置按钮后面添加版本按钮
            toolbar_layout.insertWidget(2, version_btn)

    def disable_all_controls(self):
        """禁用所有控件"""
        for widget in self.findChildren(QPushButton):
            if widget.objectName() not in ["minButton", "maxButton", "closeButton", "helpButton"]:
                widget.setEnabled(False)
        
        for widget in self.findChildren(QLineEdit):
            widget.setEnabled(False)
        
        for widget in self.findChildren(QTextEdit):
            widget.setEnabled(False)
            
        for widget in self.findChildren(QComboBox):
            widget.setEnabled(False)
            
        for widget in self.findChildren(QDateEdit):
            widget.setEnabled(False)
            
        for widget in self.findChildren(QTimeEdit):
            widget.setEnabled(False)
            
        for widget in self.findChildren(QCheckBox):
            widget.setEnabled(False)

    def enable_all_controls(self):
        """启用所有控件"""
        for widget in self.findChildren(QPushButton):
            widget.setEnabled(True)
            
        for widget in self.findChildren(QLineEdit):
            widget.setEnabled(True)
            
        for widget in self.findChildren(QTextEdit):
            widget.setEnabled(True)
            
        for widget in self.findChildren(QComboBox):
            widget.setEnabled(True)
            
        for widget in self.findChildren(QDateEdit):
            widget.setEnabled(True)
            
        for widget in self.findChildren(QTimeEdit):
            widget.setEnabled(True)
            
        for widget in self.findChildren(QCheckBox):
            widget.setEnabled(True)

    def refresh_folder(self):
        """刷新当前加载的文件夹内容"""
        current_path = self.local_data_path_edit.text().strip()
        if current_path and os.path.exists(current_path):
            self.load_folder_info(current_path)
            logging.info(f"已刷新文件夹内容: {current_path}")
    def browse_folder(self):
        """浏览文件夹并更新显示"""
        folder_path = QFileDialog.getExistingDirectory(self, "选择数据文件夹")
        if folder_path:
            self.folder_path_label.setText(folder_path)
            self.load_folder_info(folder_path)
            # 同时更新下载界面的路径
            if hasattr(self, 'local_data_path_edit'):
                self.local_data_path_edit.setText(folder_path)


    def initUI(self):
        self.setWindowTitle('看海量化交易系统')
        
        # 设置最小尺寸
        MIN_SIZE = 1000
        self.setMinimumSize(MIN_SIZE, MIN_SIZE)
        
        # 获取屏幕分辨率
        screen = QApplication.primaryScreen().geometry()
        screen_width = screen.width()
        screen_height = screen.height()
        
        # 计算窗口大小：
        # 1. 首先尝试使用屏幕大小的75%
        # 2. 如果计算出的大小小于最小尺寸，则使用最小尺寸
        # 3. 如果计算出的大小大于屏幕大小的90%，则使用90%
        window_width = int(screen_width * 0.75)
        window_height = int(screen_height * 0.75)
        
        # 确保窗口大小不小于最小尺寸
        window_width = max(window_width, MIN_SIZE)
        window_height = max(window_height, MIN_SIZE)
        
        # 确保窗口大小不超过屏幕90%
        max_width = int(screen_width * 0.9)
        max_height = int(screen_height * 0.9)
        window_width = min(window_width, max_width)
        window_height = min(window_height, max_height)
        
        # 计算居中位置
        x_position = (screen_width - window_width) // 2
        y_position = (screen_height - window_height) // 2
        
        # 设置窗口几何尺寸（位置和大小）
        self.setGeometry(x_position, y_position, window_width, window_height)
        
        # 设置窗口图标
        icon_path = os.path.join(ICON_PATH, 'stock_icon.ico')

    # 打印调试信息
        print("\n=== 图标加载调试信息 ===")
        print(f"图标路径: {icon_path}")
        print(f"ICON_PATH: {ICON_PATH}")
        print(f"当前工作目录: {os.getcwd()}")
        print(f"ICON_PATH 是否存在: {os.path.exists(ICON_PATH)}")
        print(f"图标文件是否存在: {os.path.exists(icon_path)}")
        
        if os.path.exists(icon_path):
            try:
                # 检查文件大小
                file_size = os.path.getsize(icon_path)
                print(f"图标文件大小: {file_size} 字节")
                
                # 检查文件权限
                print(f"文件权限: {oct(os.stat(icon_path).st_mode)[-3:]}")
                
                # 尝试读取文件
                with open(icon_path, 'rb') as f:
                    content = f.read(16)  # 读取前16字节
                print(f"文件头16字节: {content.hex()}")
                
                # 尝试加载图标
                icon = QIcon(icon_path)
                if icon.isNull():
                    print("警告：QIcon返回了空图标")
                else:
                    print("QIcon成功加载图标")
                    
                self.setWindowIcon(icon)
                print("成功设置窗口图标")
                 # 设置应用程序级别的异常处理
                def qt_message_handler(mode, context, message):
                    if mode == QtCore.QtInfoMsg:
                        logging.info(message)
                    elif mode == QtCore.QtWarningMsg:
                        logging.warning(message)
                    elif mode == QtCore.QtCriticalMsg:
                        logging.critical(message)
                    elif mode == QtCore.QtFatalMsg:
                        logging.fatal(message)
                    else:
                        logging.debug(message)
                
                QtCore.qInstallMessageHandler(qt_message_handler)
            except Exception as e:
                print(f"加载图标时出错: {str(e)}")
                logging.error(f"加载图标时出错: {str(e)}", exc_info=True)
        else:
            print(f"图标文件不存在: {icon_path}")
            print(f"icons目录内容: {os.listdir(self.ICON_PATH) if os.path.exists(self.ICON_PATH) else '目录不存在'}")
            
        print("=== 调试信息结束 ===\n")


        

        # 确保窗口没有额外的边框
        self.setWindowFlag(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # 添加自定义标题栏
        title_bar = QWidget()
        title_bar.setObjectName("titleBar")
        title_bar.setFixedHeight(60)
        title_bar_layout = QHBoxLayout(title_bar)
        title_bar_layout.setContentsMargins(10, 0, 10, 0)
        
        title_bar_layout.addStretch()
        title_label = QLabel("看海量化交易系统")
        title_label.setStyleSheet("color: #E0E0E0; font-weight: bold; font-size: 30px; ")
        title_bar_layout.addWidget(title_label, alignment=Qt.AlignCenter)
        title_bar_layout.addStretch()

        # 添加帮助按钮
        help_button = QPushButton("?")
        help_button.setObjectName("helpButton")
        help_button.setFixedSize(40, 40)
        help_button.clicked.connect(self.show_help)
        title_bar_layout.addWidget(help_button)

        # 添加最小化、最大化和关闭按钮
        for button_text, func, obj_name in [("—", self.showMinimized, "minButton"), 
                                            ("□", self.toggle_maximize, "maxButton"), 
                                            ("×", self.close, "closeButton")]:
            button = QPushButton(button_text)
            button.setObjectName(obj_name)
            button.setFixedSize(40, 40)
            button.clicked.connect(func)
            title_bar_layout.addWidget(button)

        main_layout.addWidget(title_bar)

        # 在标题栏中添加状态指示器
        self.status_indicator = QLabel()
        self.status_indicator.setFixedSize(20, 20)
        self.status_indicator.setToolTip("交易平台状态")
        title_bar_layout.insertWidget(title_bar_layout.count() - 3, self.status_indicator)
        



        # 添加数据可视化按钮到工具栏
        visualize_btn = QPushButton()
        visualize_btn.setIcon(self.load_icon('visualize.png'))


        # 根据屏幕分辨率动态设置图标大小
        screen = QApplication.primaryScreen().geometry()
        screen_width = screen.width()
                
        # 根据屏幕宽度设置不同的图标大小
        if screen_width >= 2560:  # 2K及以上分辨率
            icon_size = 60
        elif screen_width >= 1920:  # 1080P
            icon_size = 40
        else:  # 较低分辨率
            icon_size = 32
        
        '''
        # 添加图标加载错误处理
        if os.path.exists(icon_path):
            visualize_btn.setIcon(QIcon(icon_path))
        else:
            logging.warning(f"图标文件未找到: {icon_path}")
            # 创建一个临时的替代图标
            self.create_fallback_icon()
            visualize_btn.setIcon(QIcon(os.path.join(self.ICON_PATH, 'visualize.png')))
        '''
        
                
        # 根据屏幕分辨率动态设置工具栏高度
        if screen_width >= 2560 or screen_height >= 1440:  # 2K及以上分辨率
            toolbar_height = 70
            toolbar_margins = (20, 10, 20, 10)
            toolbar_spacing = 20
        elif screen_width >= 1920 or screen_height >= 1080:  # 1080P
            toolbar_height = 60
            toolbar_margins = (15, 8, 15, 8)
            toolbar_spacing = 15
        else:  # 较低分辨率
            toolbar_height = 45
            toolbar_margins = (10, 5, 10, 5)
            toolbar_spacing = 10
        toolbar_widget = QWidget()
        toolbar_widget.setObjectName("toolbarWidget")
        toolbar_widget.setFixedHeight(toolbar_height)
        toolbar_layout = QHBoxLayout(toolbar_widget)
        # 修改上下边距为0，保持左右边距不变
        toolbar_layout.setContentsMargins(toolbar_margins[0], 0, toolbar_margins[2], 0)
        toolbar_layout.setSpacing(toolbar_spacing)
        # 设置布局的对齐方式为垂直居中
        toolbar_layout.setAlignment(Qt.AlignVCenter)
        
        visualize_btn.setIconSize(QtCore.QSize(icon_size, icon_size))
        visualize_btn.setFixedSize(icon_size + 6, icon_size + 6)
        visualize_btn.setToolTip('数据可视化')
        visualize_btn.clicked.connect(self.open_visualization)
        visualize_btn.setObjectName("toolbarButton")
        toolbar_layout.addWidget(visualize_btn)
        
        # 在toolbar_layout中,在visualize_btn之后添加设置按钮
        settings_btn = QPushButton()
        settings_btn.setIcon(self.load_icon('settings.png'))
        settings_btn.setIconSize(QtCore.QSize(icon_size, icon_size))
        settings_btn.setFixedSize(icon_size + 6, icon_size + 6)
        settings_btn.setToolTip('设置')
        settings_btn.clicked.connect(self.show_settings)
        settings_btn.setObjectName("toolbarButton") 
        toolbar_layout.insertWidget(1, settings_btn)  # 插入到可视化按钮后面

        # 添加版本按钮
        self.add_version_menu()

        # 添加主页按钮
        self.add_home_button()

        # 添加一个弹性空间使按钮靠左对齐
        toolbar_layout.addStretch()
        
        main_layout.addWidget(toolbar_widget)
        # 创建水平布局来容纳两个界面
        h_layout = QHBoxLayout()

        # 添加左侧下载界面
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_content = QWidget()
        left_scroll.setWidget(left_content)
        left_layout = QVBoxLayout(left_content)
        
        # 添加下载界面的组件
        self.add_downloader_interface(left_layout)
        h_layout.addWidget(left_scroll)

        # 添加右侧清洗界面
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_content = QWidget()
        right_scroll.setWidget(right_content)
        right_layout = QVBoxLayout(right_content)
        
        # 添加清洗界面的组件
        self.add_cleaner_interface(right_layout)
        h_layout.addWidget(right_scroll)

        # 将水平布局添加到主布局
        main_layout.addLayout(h_layout)

        if not os.path.exists(icon_path):
            # 如果图标不存在，创建一个简单的默认图标
            pass
        self.setWindowIcon(QIcon(icon_path))

        # 设置样式
        self.apply_styles()

    def create_fallback_icon(self, icon_name):
        """创建图标不存在时的备用图标"""
        try:
            # 确保图标目录存在
            if not os.path.exists(self.ICON_PATH):
                os.makedirs(self.ICON_PATH)
            
            icon_path = os.path.join(self.ICON_PATH, icon_name)
            
            # 创建一个简单的默认图标
            pixmap = QPixmap(32, 32)
            pixmap.fill(QColor('#0078d7'))  # 使用蓝色背景
            
            # 添加一个简单的图案
            painter = QPainter(pixmap)
            painter.setPen(QPen(QColor('white'), 2))
            painter.drawRect(8, 8, 16, 16)
            painter.drawLine(8, 16, 24, 16)
            painter.drawLine(16, 8, 16, 24)
            painter.end()
            
            # 保存图标
            pixmap.save(icon_path)
            
            return QIcon(icon_path)
        except Exception as e:
            logging.error(f"创建备用图标时出错: {str(e)}", exc_info=True)
            return QIcon()  # 返回空图标作为最后的回退方案

    def toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
            # 改变按钮文本为最大化图标
            for button in self.findChildren(QPushButton):
                if button.objectName() == "maxButton":
                    button.setText("□")
        else:
            self.showMaximized()
            # 改变按钮文本为恢复图标
            for button in self.findChildren(QPushButton):
                if button.objectName() == "maxButton":
                    button.setText("❐")  # 使用不同的Unicode字符表示恢复图标

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # 检查鼠标是否在标题栏内
            title_bar = self.findChild(QWidget, "titleBar")
            if title_bar and event.pos().y() <= title_bar.height():
                self.can_drag = True
                self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            else:
                # 检查是否在窗口边缘
                edge = self.get_resize_edge(event.pos())
                if edge:
                    self.resizing = True
                    self.resize_edge = edge
            event.accept()
            
    def paintEvent(self, event):
        # 添加自定义绘制以确保窗口边框正确显示
        painter = QPainter(self)
        painter.setPen(QColor("#3D3D3D"))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        super().paintEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton:
            if self.can_drag:
                self.move(event.globalPos() - self.drag_position)
            elif self.resizing:
                self.resize_window(event.globalPos())
        else:
            cursor_changed = False
            if event.pos().x() <= self.border_thickness:
                QApplication.setOverrideCursor(Qt.SizeHorCursor)
                cursor_changed = True
            elif event.pos().x() >= self.width() - self.border_thickness:
                QApplication.setOverrideCursor(Qt.SizeHorCursor)
                cursor_changed = True
            elif event.pos().y() <= self.border_thickness:
                QApplication.setOverrideCursor(Qt.SizeVerCursor)
                cursor_changed = True
            elif event.pos().y() >= self.height() - self.border_thickness:
                QApplication.setOverrideCursor(Qt.SizeVerCursor)
                cursor_changed = True
            
            if not cursor_changed:
                QApplication.restoreOverrideCursor()
        
        event.accept()

    def leaveEvent(self, event):
        QApplication.restoreOverrideCursor()
        event.accept()

    def mouseReleaseEvent(self, event):
        self.can_drag = False
        self.resizing = False
        self.resize_edge = None
        self.setCursor(Qt.ArrowCursor)
        event.accept()

    def get_resize_edge(self, pos):
        rect = self.rect()
        if pos.x() <= self.border_thickness:
            if pos.y() <= self.border_thickness:
                return 'topleft'
            elif pos.y() >= rect.height() - self.border_thickness:
                return 'bottomleft'
            else:
                return 'left'
        elif pos.x() >= rect.width() - self.border_thickness:
            if pos.y() <= self.border_thickness:
                return 'topright'
            elif pos.y() >= rect.height() - self.border_thickness:
                return 'bottomright'
            else:
                return 'right'
        elif pos.y() <= self.border_thickness:
            return 'top'
        elif pos.y() >= rect.height() - self.border_thickness:
            return 'bottom'
        return None

    def resize_window(self, global_pos):
        new_geo = self.geometry()
        if self.resize_edge in ['left', 'topleft', 'bottomleft']:
            new_geo.setLeft(global_pos.x())
        if self.resize_edge in ['right', 'topright', 'bottomright']:
            new_geo.setRight(global_pos.x())
        if self.resize_edge in ['top', 'topleft', 'topright']:
            new_geo.setTop(global_pos.y())
        if self.resize_edge in ['bottom', 'bottomleft', 'bottomright']:
            new_geo.setBottom(global_pos.y())
        self.setGeometry(new_geo)

    def create_colored_pixmap(self, color, size=20):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setBrush(QColor(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(0, 0, size, size)
        painter.end()
        return pixmap

    def check_and_open_software(self):
        """检查并启动miniQMT软件"""
        try:
            settings = QSettings('KHQuant', 'StockAnalyzer')
            default_path = r"C:\国金证券QMT交易端\bin.x64\XtItClient.exe"
            software_path = settings.value('client_path', default_path)
            
            # 如果路径无效，只更新状态指示器，不显示警告
            if not os.path.exists(software_path):
                logging.warning(f"QMT客户端路径无效: {software_path}")
                # 只更新状态指示器
                self.update_status_indicator("red", "MiniQMT未启动")
                return False
                
            # 只有在路径有效且软件未运行时才尝试启动
            if not self.is_software_running("XtMiniQmt.exe"):
                try:
                    if ctypes.windll.shell32.IsUserAnAdmin() == 0:
                        ctypes.windll.shell32.ShellExecuteW(None, "runas", software_path, None, None, 1)
                    else:
                        subprocess.Popen(software_path)
                    return True
                except Exception as e:
                    logging.error(f"启动QMT客户端失败: {str(e)}")
                    self.update_status_indicator("red", "MiniQMT启动失败")
                    return False
                    
            return True
            
        except Exception as e:
            logging.error(f"检查并启动软件时出错: {str(e)}")
            self.update_status_indicator("red", "MiniQMT状态检查失败")
            return False

    def is_software_running(self, process_name):
        """检查指定的进程是否正在运行"""
        for proc in psutil.process_iter(['name']):
            if proc.info['name'].lower() == process_name.lower():
                return True
        return False

    def add_downloader_interface(self, layout):
        title_font = QFont("Roboto", 10, QFont.Bold)
        
        # 添加标题
        title_label = QLabel("数据下载")
        title_label.setFont(QFont("Roboto", 14, QFont.Bold))
        title_label.setStyleSheet("color: #E0E0E0;")
        layout.addWidget(title_label)

        self.add_path_group(layout, title_font)
        self.add_stock_group(layout, title_font)
        self.add_period_group(layout, title_font)
        self.add_field_group(layout, title_font)
        self.add_date_group(layout, title_font)
        self.add_time_group(layout, title_font)
        self.add_download_section(layout)

        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: #E0E0E0; font-size: 24px;")
        layout.addWidget(self.status_label)
    def add_cleaner_interface(self, layout):
        # 添加标题
        title_label = QLabel("数据清洗")
        title_label.setFont(QFont("Roboto", 14, QFont.Bold))
        title_label.setStyleSheet("color: #E0E0E0;")
        layout.addWidget(title_label)

        # 文件夹选择组
        folder_group = QGroupBox("文件夹选择")
        folder_layout = QHBoxLayout()
        # 设置初始值为数据存储路径的当前值
        self.folder_path_label = QLabel(self.local_data_path_edit.text())
        self.browse_button = QPushButton("浏览...")
        self.browse_button.clicked.connect(self.browse_folder)
        folder_layout.addWidget(self.folder_path_label)
        folder_layout.addWidget(self.browse_button)
        folder_group.setLayout(folder_layout)
        layout.addWidget(folder_group)

        # 清洗操作组
        operations_group = QGroupBox("清洗操作")
        operations_layout = QGridLayout()
        self.operation_checkboxes = {}
        operations = [
            ('remove_duplicates', '删除重复行'),
            ('handle_missing_values', '处理缺失值'),
            ('correct_data_types', '修正数据类型'),
            ('remove_outliers', '移除异常值'),  # 这个选项将默认不勾选
            ('handle_non_trading_hours', '处理非交易时间'),
            ('sort_data', '排序数据')
        ]
        for i, (op, label) in enumerate(operations):
            checkbox = QCheckBox(label)
            # 除了 remove_outliers 之外的选项默认勾选
            checkbox.setChecked(op != 'remove_outliers')
            self.operation_checkboxes[op] = checkbox
            operations_layout.addWidget(checkbox, i // 2, i % 2)
        operations_group.setLayout(operations_layout)
        layout.addWidget(operations_group)

        # 清洗按钮和进度条
        clean_layout = QVBoxLayout()
        self.clean_button = QPushButton("开始清洗")
        self.clean_button.clicked.connect(self.start_cleaning)
        self.file_progress_bar = QProgressBar()
        self.total_progress_bar = QProgressBar()
        clean_layout.addWidget(self.clean_button)
        clean_layout.addWidget(QLabel("当前文件进度:"))
        clean_layout.addWidget(self.file_progress_bar)
        clean_layout.addWidget(QLabel("总体进度:"))
        clean_layout.addWidget(self.total_progress_bar)
        layout.addLayout(clean_layout)

        # 预览区
        preview_group = QGroupBox("清洗结果预览")
        preview_group.setObjectName("预览组")
        preview_layout = QVBoxLayout()
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        preview_layout.addWidget(self.preview_text)

        # 添加保存日志按钮
        save_layout = QHBoxLayout()
        save_layout.addStretch()
        self.save_log_button = QPushButton("保存清洗日志")
        self.save_log_button.clicked.connect(self.save_cleaning_log)
        save_layout.addWidget(self.save_log_button)
        preview_layout.addLayout(save_layout)

        preview_group.setLayout(preview_layout)
        layout.addWidget(preview_group)

    def save_cleaning_log(self):
        try:
            # 生成默认文件名
            default_filename = f"cleaning_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            default_path = os.path.join(LOGS_DIR, default_filename)
            
            # 获取保存路径，设置默认目录为logs目录
            file_path, _ = QFileDialog.getSaveFileName(
                self,
                "保存清洗日志",
                default_path,
                "Text Files (*.txt);;All Files (*)"
            )
            
            if file_path:
                # 确保目标目录存在
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(self.preview_text.toPlainText())
                QMessageBox.information(self, "成功", "清洗日志已保存！")
                
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存日志时发生错误: {str(e)}")

    def add_path_group(self, layout, title_font):
        path_group = QGroupBox("数据存储路径")
        path_group.setFont(title_font)
        path_layout = QHBoxLayout()
        
        # 从QSettings读取上次保存的路径，如果没有则使用默认路径
        settings = QSettings('KHQuant', 'StockAnalyzer')
        saved_path = settings.value('data_path', "D:/stock_data")
        logging.info(f"从QSettings读取保存的路径: {saved_path}")
        self.local_data_path_edit = QLineEdit(saved_path)
        
        # 添加信号接，当文本改变时更新清洗界面的路径和保存设置
        self.local_data_path_edit.textChanged.connect(self.update_folder_path_label)
        self.local_data_path_edit.editingFinished.connect(self.save_path_setting)
        path_layout.addWidget(self.local_data_path_edit)
        browse_button = QPushButton("浏览...")
        browse_button.clicked.connect(self.browse_path)
        path_layout.addWidget(browse_button)
        path_group.setLayout(path_layout)
        layout.addWidget(path_group)

    def save_path_setting(self):
        """保存当前路径到设置"""
        try:
            path = self.local_data_path_edit.text().strip()
            if path:
                settings = QSettings('KHQuant', 'StockAnalyzer')
                settings.setValue('data_path', path)
                settings.sync()  # 强制同步设置到磁盘
                logging.info(f"已保存路径到设置: {path}")
                
                # 如果清洗界面的文件夹路径标签存在，也更新它
                if hasattr(self, 'folder_path_label'):
                    self.folder_path_label.setText(path)
                self.refresh_folder()  # 立即刷新文件夹内容
        except Exception as e:
            logging.error(f"保存路径设置时出错: {str(e)}")
            QMessageBox.warning(self, "警告", f"保存路径设置时出错: {str(e)}")

    def browse_path(self):
        """更新浏览路径方法"""
        path = QFileDialog.getExistingDirectory(self, "选择数据存储路径")
        if path:
            logging.info(f"用户选择了新路径: {path}")
            self.local_data_path_edit.setText(path)
            
            # 保存路径到QSettings
            try:
                settings = QSettings('KHQuant', 'StockAnalyzer')
                settings.setValue('data_path', path)
                settings.sync()  # 强制同步设置到磁盘
                
                # 验证设置是否保存成功
                saved_value = settings.value('data_path')
                logging.info(f"验证保存的路径: {saved_value}")
                
                if saved_value != path:
                    logging.error("路径保存验证失败！")
                else:
                    logging.info("路径已成功保存到QSettings")
                    
            except Exception as e:
                logging.error(f"保存路径到QSettings时出错: {str(e)}")
                QMessageBox.warning(self, "警告", f"保存路径设置时出错: {str(e)}")
            
            # 如果清洗界面的文件夹路径标签存在，也更新它
            if hasattr(self, 'folder_path_label'):
                self.folder_path_label.setText(path)
                logging.info("已更新清洗界面的路径标签")
            
            self.refresh_folder()  # 立即刷新文件夹内容
            logging.info("已刷新文件夹内容")

    def add_stock_group(self, layout, title_font):
        stock_group = QGroupBox("股票代码列表文件")
        stock_group.setFont(title_font)
        stock_layout = QVBoxLayout()
        
        # 添加复选框组
        self.stock_checkboxes = {}
        self.stock_files = {}
        stock_types = {
            'hs_a': '沪深A股',
            'gem': '创业板',
            'sci': '科创板',
            'sh_a': '上证A股',
            'zz500': '中证500成分股',
            'hs300': '沪深300成分股',
            'sz50': '上证50成分股',
            'indices': '常用指数',
            'custom': '自选清单'  # 添加自选清单选项
        }
        
        # 创建复选框网格布局
        checkbox_layout = QGridLayout()
        row = 0
        col = 0
        for stock_type, display_name in stock_types.items():
            if stock_type == 'custom':
                # 为自选清单创建特殊的标签和复选框布局
                custom_layout = QHBoxLayout()
                checkbox = QCheckBox()
                self.stock_checkboxes[stock_type] = checkbox
                custom_layout.addWidget(checkbox)
                
                # 创建可点击的标签
                custom_label = QLabel(display_name)
                custom_label.setStyleSheet("""
                    QLabel {
                        color: #3DAEE9;
                        text-decoration: underline;
                        cursor: pointer;
                    }
                    QLabel:hover {
                        color: #2980B9;
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
                checkbox_layout.addLayout(custom_layout, row, col, 1, 3)
            else:
                checkbox = QCheckBox(display_name)
                self.stock_checkboxes[stock_type] = checkbox
                checkbox_layout.addWidget(checkbox, row, col)
                col += 1
                if col > 2:
                    col = 0
                    row += 1
        
        stock_layout.addLayout(checkbox_layout)
        
        # 自定义列表部分
        custom_group = QGroupBox("自定义列表")
        custom_layout = QHBoxLayout()
        
        browse_button = QPushButton("添加自定义列表")
        browse_button.clicked.connect(self.add_custom_stock_file)
        custom_layout.addWidget(browse_button)
        
        clear_button = QPushButton("清空列表")
        clear_button.clicked.connect(self.clear_stock_files)
        custom_layout.addWidget(clear_button)
        
        custom_layout.addStretch()
        
        custom_group.setLayout(custom_layout)
        stock_layout.addWidget(custom_group)
        
        # 添加已选文件预览
        preview_group = QGroupBox("已选列表预览")
        preview_layout = QVBoxLayout()
        self.stock_files_preview = QTextEdit()
        self.stock_files_preview.setReadOnly(True)
        self.stock_files_preview.setMaximumHeight(100)
        preview_layout.addWidget(self.stock_files_preview)
        preview_group.setLayout(preview_layout)
        stock_layout.addWidget(preview_group)
        
        # 连接复选框信号
        for checkbox in self.stock_checkboxes.values():
            checkbox.stateChanged.connect(self.update_stock_files_preview)
        
        stock_group.setLayout(stock_layout)
        layout.addWidget(stock_group)

    def clear_stock_files(self):
        """清空股票列表预览"""
        try:
            # 取消所有复选框的选中状态
            for checkbox in self.stock_checkboxes.values():
                checkbox.setChecked(False)
            
            # 清空预览文本
            self.stock_files_preview.setText("未选择任何股票列表文件")
            
            logging.info("已清空股票列表选择")
            
        except Exception as e:
            logging.error(f"清空股票列表时出错: {str(e)}", exc_info=True)
            QMessageBox.warning(self, "错误", f"清空列表时出错: {str(e)}")

    def add_custom_stock_file(self):
        """添加自定义股票列表文件"""
        try:
            # 使用用户数据目录
            data_dir = get_data_directory()
            
            # 打开文件选择对话框，设置默认路径为data目录
            files, _ = QFileDialog.getOpenFileNames(
                self,
                "选择股票代码列表文件",
                data_dir,  # 设置默认路径
                "CSV Files (*.csv);;All Files (*)"
            )
            
            if files:
                # 获取当前预览中的文件列表
                current_files = []
                current_text = self.stock_files_preview.toPlainText().strip()
                if current_text and current_text != "未选择任何股票列表文件":
                    current_files = current_text.split('\n')
                
                # 添加新选择的文件，避免重复
                for file in files:
                    if file not in current_files:
                        current_files.append(file)
                
                # 更新预览文本
                preview_text = "\n".join(current_files)
                self.stock_files_preview.setText(preview_text)
                
                logging.info(f"已添加自定义股票列表文件: {files}")
                
        except Exception as e:
            logging.error(f"添加自定义股票列表文件时出错: {str(e)}", exc_info=True)
            QMessageBox.warning(self, "错误", f"添加文件时出错: {str(e)}")

    def update_stock_files_preview(self):
        """更新选中的股票文件预览"""
        try:
            # 使用code目录下的data文件夹
            data_dir = os.path.join(os.path.dirname(__file__), 'data')
            selected_files = []
            
            # 获取当前预览中的自定义文件列表
            custom_files = []
            current_preview = self.stock_files_preview.toPlainText().strip()
            if current_preview and current_preview != "未选择任何股票列表文件":
                preview_lines = current_preview.split('\n')
                for line in preview_lines:
                    # 修改这里：排除所有预定义的文件名，包括自选清单
                    if line.strip() and not any(board_name in line for board_name in [
                        '上证A股', '深证A股', '创业板', '科创板', '沪深A股', '指数',
                        '中证500成分股', '沪深300成分股', '上证50成分股', 'otheridx.csv'  # 添加 otheridx.csv
                    ]):
                        custom_files.append(line.strip())
            
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
                    elif stock_type == 'custom':
                        custom_file = os.path.join(data_dir, "otheridx.csv")
                        if os.path.exists(custom_file):
                            filename = custom_file
                        else:
                            # 如果自选清单文件不存在，创建一个空文件
                            try:
                                with open(custom_file, 'w', encoding='utf-8') as f:
                                    f.write("code,name\n")  # 写入表头
                                filename = custom_file
                                logging.info(f"已创建新的自选清单文件: {custom_file}")
                            except Exception as e:
                                logging.error(f"创建自选清单文件失败: {str(e)}")
                    else:
                        board_names = {
                            'sh_a': '上证A股',
                            'sz_a': '深证A股',
                            'gem': '创业板',
                            'sci': '科创板',
                            'hs_a': '沪深A股'
                        }
                        filename = os.path.join(data_dir, f"{board_names[stock_type]}_股票列表.csv")
                    
                    if filename and os.path.exists(filename):
                        selected_files.append(filename)
                    else:
                        logging.warning(f"股票列表文件不存在: {filename}")
            
            # 合并预定义列表和自定义列表
            selected_files.extend(custom_files)
            
            # 只有当没有任何选中的文件时才显示"未选择任何股票列表文件"
            if not selected_files:
                self.stock_files_preview.setText("未选择任何股票列表文件")
            else:
                self.stock_files_preview.setText("\n".join(selected_files))
            
        except Exception as e:
            logging.error(f"更新股票文件预览时出错: {str(e)}", exc_info=True)
            self.stock_files_preview.setText("更新预览时出错")

    def download_data(self):
        try:
            logging.info("开始准备下载数据")
            
            # 验证日期和时间范围
            if not self.validate_date_range():
                return
            if not self.validate_time_range():
                return
            
            # 清理之前的线程（如果存在）
            if hasattr(self, 'download_thread') and self.download_thread:
                try:
                    self.download_thread.stop()
                    self.download_thread.wait()
                except:
                    pass

            selected_fields = [field for field, checkbox in self.field_checkboxes.items() if checkbox.isChecked()]
            if not selected_fields:
                QMessageBox.warning(self, "警告", "请至少选择一个字段")
                return

            # 获取周期类型和复权方式
            period_type = self.period_type_combo.currentText()
            dividend_type = self.dividend_type_combo.currentData()
            if dividend_type is None:  # 如果没有设置currentData，则使用currentText
                dividend_type = self.dividend_type_combo.currentText()

            # 参数验证
            local_data_path = self.local_data_path_edit.text().strip()
            if not local_data_path:
                QMessageBox.warning(self, "警告", "请设置数据存储路径")
                return

            # 获取选中的股票列表文件
            current_preview = self.stock_files_preview.toPlainText().strip()
            if not current_preview or current_preview == "未选择任何股票列表文件":
                QMessageBox.warning(self, "警告", "请选择至少一个股票列表")
                return

            # 获取所有选中的文件路径
            selected_files = [f.strip() for f in current_preview.split('\n') if f.strip()]

            # 检查文件是否存在
            for file in selected_files:
                if not os.path.exists(file):
                    QMessageBox.warning(self, "警告", f"文件不存在: {file}")
                    return

            # 准备下载参数
            try:
                from khQTTools import download_and_store_data
                download_and_store_data(
                    local_data_path=local_data_path,
                    stock_files=selected_files,
                    field_list=selected_fields,
                    period_type=period_type,
                    start_date=self.start_date_edit.date().toString('yyyyMMdd'),
                    end_date=self.end_date_edit.date().toString('yyyyMMdd'),
                    dividend_type=dividend_type,
                    progress_callback=self.update_progress,
                    log_callback=self.update_status
                )
                QMessageBox.information(self, "完成", "数据下载完成！")
            except Exception as e:
                logging.error(f"下载数据时出错: {str(e)}")
                QMessageBox.critical(self, "错误", f"下载数据时出错: {str(e)}")

        except Exception as e:
            logging.error(f"准备下载数据时出错: {str(e)}")
            QMessageBox.critical(self, "错误", f"准备下载数据时出错: {str(e)}")

    def handle_download_error(self, error_msg):
        """处理下载错误"""
        logging.error(f"下载错误: {error_msg}")
        QMessageBox.critical(self, "下载错误", error_msg)
        self.download_button.setEnabled(True)
        self.status_label.setText("下载失败")

    def update_progress(self, value):
        self.progress_bar.setValue(value)

    def download_finished(self, success, message):
        self.reset_download_button()
        if success:
            custom_msg_box = QMessageBox(self)
            custom_msg_box.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
            custom_msg_box.setText(message)
            custom_msg_box.setStyleSheet("""
                QMessageBox {
                    background-color: #F0F0F0;
                    border: 1px solid #D0D0D0;
                    border-radius: 10px;
                }
                QMessageBox QLabel {
                    background-color: #F0F0F0;
                    color: #2C3E50;
                    font-size: 24px;
                    padding: 20px;
                }
            """)
            ok_button = custom_msg_box.addButton(QMessageBox.Ok)
            ok_button.setMinimumSize(120, 50)
            ok_button.setStyleSheet("""
                QPushButton {
                    font-size: 18px;
                    background-color: #808080;
                    color: white;
                    border: none;
                    padding: 8px;
                    border-radius: 6px;
                }
                QPushButton:hover {
                    background-color: #909090;
                }
                QPushButton:pressed {
                    background-color: #707070;
                }
            """)
            custom_msg_box.exec_()
        else:
            error_msg_box = QMessageBox(self)
            error_msg_box.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
            error_msg_box.setIcon(QMessageBox.Critical)
            error_msg_box.setText(f"下载过程中发生错误: {message}")
            error_msg_box.setStyleSheet("""
                QMessageBox {
                    background-color: #F0F0F0;
                    border: 1px solid #D0D0D0;
                    border-radius: 10px;
                }
                QMessageBox QLabel {
                    background-color: #F0F0F0;
                    color: #2C3E50;
                    font-size: 24px;
                    padding: 20px;
                }
            """)
            ok_button = error_msg_box.addButton(QMessageBox.Ok)
            ok_button.setMinimumSize(120, 50)
            ok_button.setStyleSheet("""
                QPushButton {
                    font-size: 24px;
                    background-color: #808080;
                    color: white;
                    border: none;
                    padding: 8px;
                    border-radius: 6px;
                }
                QPushButton:hover {
                    background-color: #909090;
                }
                QPushButton:pressed {
                    background-color: #707070;
                }
            """)
            error_msg_box.exec_()
        
        self.status_label.setText(message)
        self.progress_bar.setValue(0)

    def update_field_checkboxes(self):
        # 保存当前选中状态
        current_selections = {}
        if hasattr(self, 'field_checkboxes'):
            for field, checkbox in self.field_checkboxes.items():
                current_selections[field] = checkbox.isChecked()

        # 清除现有的复选框
        for i in reversed(range(self.field_layout.count())): 
            self.field_layout.itemAt(i).widget().setParent(None)
        
        self.field_checkboxes.clear()
        
        period_type = self.period_type_combo.currentText()
        fields = self.tick_fields if period_type == 'tick' else self.kline_fields
        
        for i, (field, description) in enumerate(fields.items()):
            checkbox = QCheckBox(f"{description}")
            # 如果该字段之前被选中，保持选中状态
            if field in current_selections:
                checkbox.setChecked(current_selections[field])
            else:
                checkbox.setChecked(True)  # 新字段默认选中
            self.field_checkboxes[field] = checkbox
            self.field_layout.addWidget(checkbox, i // 4, i % 4)
    def add_field_group(self, layout, title_font):
        self.field_group = QGroupBox("要存储的字段列表")
        self.field_group.setFont(title_font)
        self.field_layout = QGridLayout()
        self.field_checkboxes = {}
        self.field_group.setLayout(self.field_layout)
        layout.addWidget(self.field_group)
        
        # 初始化字段
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
        
        self.update_field_checkboxes()
    def add_period_group(self, layout, title_font):
        # 创建水平布局来容纳两个组
        h_layout = QHBoxLayout()

        # 周期类型组
        period_group = QGroupBox("周期类型")
        period_group.setFont(title_font)
        period_layout = QHBoxLayout()

        self.period_type_combo = QComboBox()
        self.period_type_combo.addItems(['tick', '1m', '5m', '1d'])
        self.period_type_combo.currentTextChanged.connect(self.update_field_checkboxes)
        period_layout.addWidget(self.period_type_combo)
        period_group.setLayout(period_layout)
        h_layout.addWidget(period_group)

        # 复权方式组
        dividend_group = QGroupBox("复权方式（仅针对下载数据）")
        dividend_group.setFont(title_font)
        dividend_layout = QVBoxLayout()

        # 添加说明文字
        note_label = QLabel("注：补充数据模式不涉及复权")
        note_label.setStyleSheet("color: gray; font-size: 12px;")
        dividend_layout.addWidget(note_label)

        # 复权选择下拉框
        self.dividend_type_combo = QComboBox()
        self.dividend_type_combo.addItem("不复权", "none")
        self.dividend_type_combo.addItem("前复权", "front")
        self.dividend_type_combo.addItem("后复权", "back")
        self.dividend_type_combo.addItem("等比前复权", "front_ratio")
        self.dividend_type_combo.addItem("等比后复权", "back_ratio")
        # 设置默认选项为前复权
        self.dividend_type_combo.setCurrentIndex(1)  # 设置为"前复权"
        dividend_layout.addWidget(self.dividend_type_combo)

        dividend_group.setLayout(dividend_layout)
        h_layout.addWidget(dividend_group)

        layout.addLayout(h_layout)

    def add_date_group(self, layout, title_font):
        date_group = QGroupBox("日期范围")
        date_group.setFont(title_font)
        date_layout = QHBoxLayout()
        
        start_date_layout = QHBoxLayout()
        start_date_layout.addWidget(QLabel("起始日期:"))
        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDate(QDate(2024, 1, 1))
        start_date_layout.addWidget(self.start_date_edit)
        
        end_date_layout = QHBoxLayout()
        end_date_layout.addWidget(QLabel("结束日期:"))
        self.end_date_edit = QDateEdit()
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setDate(QDate(2024, 2, 1))
        end_date_layout.addWidget(self.end_date_edit)
        
        date_layout.addLayout(start_date_layout)
        date_layout.addLayout(end_date_layout)
        
        date_group.setLayout(date_layout)
        layout.addWidget(date_group)

    def validate_date_range(self):
        """验证日期范围"""
        start_date = self.start_date_edit.date()
        end_date = self.end_date_edit.date()
        
        if end_date < start_date:
            QMessageBox.warning(self, "警告", "结束日期不能早于开始日期")
            return False
            
        return True

    def add_time_group(self, layout, title_font):
        """添加时间段选择组"""
        time_group = QGroupBox("时间段")
        time_group.setFont(title_font)
        time_layout = QHBoxLayout()
        
        # 添加时间范围选择下拉框
        self.use_all_time_checkbox = QComboBox()
        self.use_all_time_checkbox.addItems(['指定时间段', '全天'])
        self.use_all_time_checkbox.setCurrentIndex(1)  # 默认选择"全天"
        self.use_all_time_checkbox.currentIndexChanged.connect(self.toggle_time_range)
        time_layout.addWidget(self.use_all_time_checkbox)
        
        # 添加时间选择控件
        time_layout.addWidget(QLabel("开始时间:"))
        self.start_time_edit = QTimeEdit()
        self.start_time_edit.setTime(QTime(9, 30))
        self.start_time_edit.timeChanged.connect(self.validate_time_range)
        time_layout.addWidget(self.start_time_edit)
        
        time_layout.addWidget(QLabel("结束时间:"))
        self.end_time_edit = QTimeEdit()
        self.end_time_edit.setTime(QTime(15, 0))
        self.end_time_edit.timeChanged.connect(self.validate_time_range)
        time_layout.addWidget(self.end_time_edit)
        
        # 根据当前选择设置时间编辑器的启用状态
        self.start_time_edit.setEnabled(False)
        self.end_time_edit.setEnabled(False)
        
        time_group.setLayout(time_layout)
        layout.addWidget(time_group)

    def validate_time_range(self):
        """验证时间范围"""
        # 只在选择"指定时间段"时进行验证
        if self.use_all_time_checkbox.currentIndex() == 0:
            start_time = self.start_time_edit.time()
            end_time = self.end_time_edit.time()
            
            if start_time >= end_time:
                QMessageBox.warning(self, "警告", "开始时间必须早于结束时间")
                return False
        
        return True

    def toggle_time_range(self, index):
        """切换时间范围选择"""
        use_all_time = (index == 1)  # 1 表示选择了"全天"
        self.start_time_edit.setEnabled(not use_all_time)
        self.end_time_edit.setEnabled(not use_all_time)
        
        # 在切换到"指定时间段"时验证时间范围
        if not use_all_time:
            self.validate_time_range()

    def add_download_section(self, layout):
        download_layout = QVBoxLayout()
        
        # 创建按钮布局
        button_layout = QHBoxLayout()
        
        # 下载数据按钮
        self.download_button = QPushButton("下载数据（数据下载时UI界面会进入未响应状态，如要终止下载直接关闭软件）")
        self.download_button.setMinimumHeight(40)
        self.download_button.clicked.connect(self.download_data)
        button_layout.addWidget(self.download_button)
        
        # 补充数据按钮
        self.supplement_button = QPushButton("补充数据")
        self.supplement_button.setMinimumHeight(40)
        self.supplement_button.clicked.connect(self.supplement_data)
        button_layout.addWidget(self.supplement_button)
        
        download_layout.addLayout(button_layout)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMinimumHeight(10)
        download_layout.addWidget(self.progress_bar)

        layout.addLayout(download_layout)

    def supplement_data(self):
        """补充数据按钮点击事件处理"""
        try:
            # 验证日期和时间范围
            if not self.validate_date_range():
                return
            if not self.validate_time_range():
                return
            
            # 获取选中的股票文件列表
            stock_files = []
            current_preview = self.stock_files_preview.toPlainText().strip()
            if current_preview and current_preview != "未选择任何股票列表文件":
                stock_files = current_preview.split('\n')
            
            if not stock_files:
                QMessageBox.warning(self, "警告", "请先选择股票列表文件！")
                return

            # 获取选中的字段列表
            field_list = []
            for field, checkbox in self.field_checkboxes.items():
                if checkbox.isChecked():
                    field_list.append(field)
            
            if not field_list:
                QMessageBox.warning(self, "警告", "请至少选择一个数据字段！")
                return

            # 获取周期类型
            period_type = self.period_type_combo.currentText()

            # 获取日期范围
            start_date = self.start_date_edit.date().toString("yyyyMMdd")
            end_date = self.end_date_edit.date().toString("yyyyMMdd")

            # 获取时间范围
            time_range = 'all'
            if self.use_all_time_checkbox.currentIndex() == 0:  # 指定时间段
                start_time = self.start_time_edit.time().toString("HH:mm")
                end_time = self.end_time_edit.time().toString("HH:mm")
                time_range = f"{start_time}-{end_time}"

            # 更新按钮状态
            self.supplement_button.setEnabled(False)
            self.download_button.setEnabled(False)
            self.progress_bar.setValue(0)

            try:
                # 调用补充数据函数，不传递复权参数
                supplement_history_data(
                    stock_files=stock_files,
                    field_list=field_list,
                    period_type=period_type,
                    start_date=start_date,
                    end_date=end_date,
                    time_range=time_range,
                    progress_callback=self.update_progress,
                    log_callback=self.update_status
                )
                
                QMessageBox.information(self, "完成", "数据补充完成！")
                
            except Exception as e:
                QMessageBox.critical(self, "错误", f"补充数据时出错：{str(e)}")
                logging.error(f"补充数据时出错: {str(e)}", exc_info=True)
            
            finally:
                # 恢复按钮状态
                self.supplement_button.setEnabled(True)
                self.download_button.setEnabled(True)
                self.progress_bar.setValue(0)

        except Exception as e:
            QMessageBox.critical(self, "错误", f"准备补充数据时出错：{str(e)}")
            logging.error(f"准备补充数据时出错: {str(e)}", exc_info=True)

    # 数据清洗相关方法
    def load_folder_info(self, folder_path):
        """更新文件夹信息加载方法"""
        try:
            if not os.path.exists(folder_path):
                self.preview_text.setText("文件夹不存在")
                return

            csv_files = [f for f in os.listdir(folder_path) if f.endswith('.csv')]
            
            if not csv_files:
                self.preview_text.setText("文件夹中没有CSV文件")
                return

            total_size = sum(os.path.getsize(os.path.join(folder_path, f)) for f in csv_files)
            info_text = f"文件夹信息更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            info_text += f"文件夹路径: {folder_path}\n"
            info_text += f"CSV文件数量: {len(csv_files)}\n"
            info_text += f"总文件大小: {total_size / (1024*1024):.2f} MB\n\n"
            info_text += "文件列表:\n"
            
            for file in sorted(csv_files):
                file_path = os.path.join(folder_path, file)
                file_size = os.path.getsize(file_path)
                file_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                info_text += f"- {file}\n"
                info_text += f"  大小: {file_size / 1024:.1f} KB\n"
                info_text += f"  修改时间: {file_time.strftime('%Y-%m-%d %H:%M:%S')}\n"

            self.preview_text.setText(info_text)
            logging.info(f"成功加载文件夹信息: {folder_path}")
        except Exception as e:
            error_msg = f"加载文件夹信息时出错: {str(e)}"
            logging.error(error_msg)
            QMessageBox.warning(self, "加载错误", error_msg)


    def load_folder_info(self, folder_path):
        try:
            csv_files = [f for f in os.listdir(folder_path) if f.endswith('.csv')]
            info_text = f"文件夹内CSV文件数量: {len(csv_files)}\n\n"
            info_text += "文件列表:\n"
            for file in csv_files:
                info_text += f"- {file}\n"
            self.preview_text.setText(info_text)
        except Exception as e:
            QMessageBox.warning(self, "加载错误", f"加载文件夹信息时出错: {str(e)}")

    def start_cleaning(self):
        folder_path = self.folder_path_label.text()
        if folder_path == "未选择文件夹":
            return

        # 显示警告对话框
        reply = QMessageBox.warning(
            self,
            "警告",
            "清洗后数据将覆盖原始数据，是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No  # 默认选择No按钮
        )
        
        if reply == QMessageBox.No:
            return

        operations = [op for op, checkbox in self.operation_checkboxes.items() if checkbox.isChecked()]
        self.cleaner_thread = CleanerThread(self.cleaner, folder_path, operations)
        self.cleaner_thread.progress_updated.connect(self.update_cleaner_progress)
        self.cleaner_thread.cleaning_completed.connect(self.show_cleaning_preview)
        self.cleaner_thread.error_occurred.connect(self.show_cleaner_error)
        self.cleaner_thread.start()

        self.clean_button.setEnabled(False)
        self.browse_button.setEnabled(False)

    def update_cleaner_progress(self, file_progress, total_progress):
        self.file_progress_bar.setValue(file_progress)
        self.total_progress_bar.setValue(total_progress)

    def show_cleaning_preview(self, cleaning_info):
        """Generate and display the cleaning preview with proper handling of deleted rows"""
        preview_text = "清洗完成总览报告\n" + "="*50 + "\n\n"
        
        # 总概况
        total_rows_before = sum(info['before']['shape'][0] for info in cleaning_info.values())
        total_rows_after = sum(info['after']['shape'][0] for info in cleaning_info.values())
        preview_text += f"处理文件总数: {len(cleaning_info)}\n"
        preview_text += f"总行数变化: {total_rows_before} -> {total_rows_after} (清理: {total_rows_before - total_rows_after} 行)\n\n"
        
        # 按操作类统计总删除行数
        operation_totals = {}
        for file_info in cleaning_info.values():
            for op, count in file_info['after']['row_changes'].items():
                operation_totals[op] = operation_totals.get(op, 0) + count
        
        preview_text += "各类型数据清理统计:\n" + "-"*30 + "\n"
        for op, total in operation_totals.items():
            op_name = {
                'remove_duplicates': '重复数据',
                'handle_missing_values': '缺失值',
                'remove_outliers': '异常值',
                'handle_non_trading_hours': '非交易时间数据'
            }.get(op, op)
            preview_text += f"{op_name}: 共清理 {total} 行\n"
        preview_text += "\n"

        # 单个文件详细信息
        preview_text += "各文件详细清理报告\n" + "="*50 + "\n\n"
        
        for file, info in cleaning_info.items():
            preview_text += f"文件: {file}\n" + "-"*50 + "\n"
            preview_text += f"初始行数: {info['before']['shape'][0]}\n"
            preview_text += f"最终行数: {info['after']['shape'][0]}\n"
            preview_text += f"总清理行数: {info['before']['shape'][0] - info['after']['shape'][0]}\n\n"
            
            # 各操作的详细信息
            preview_text += "清理详情:\n"
            for op, count in info['after']['row_changes'].items():
                if count > 0:
                    op_name = {
                        'remove_duplicates': '重复数据',
                        'handle_missing_values': '缺失值',
                        'remove_outliers': '异常值',
                        'handle_non_trading_hours': '非交易时间数据'
                    }.get(op, op)
                    preview_text += f"\n>> {op_name}清理详情:\n"
                    preview_text += f"清理行数: {count}\n"
                    
                    # 检查被删除的行的详细信息
                    if op in info['after']['deleted_rows']:
                        deleted_data = info['after']['deleted_rows'][op]
                        
                        # 检查deleted_data是否为DataFrame
                        if isinstance(deleted_data, pd.DataFrame) and not deleted_data.empty:
                            preview_text += "删除的数据详情:\n"
                            
                            if op == 'remove_outliers':
                                # 对于异常值，显示所有数据并按照数值大小排序
                                preview_text += "异常值数据:\n"
                                numeric_cols = deleted_data.select_dtypes(include=['float64', 'int64']).columns
                                for col in numeric_cols:
                                    if col in ['open', 'high', 'low', 'close', 'volume']:
                                        preview_text += f"\n{col} 列的异常值分析:\n"
                                        preview_text += f"最小值: {deleted_data[col].min()}\n"
                                        preview_text += f"最大值: {deleted_data[col].max()}\n"
                                        preview_text += "所有异常数据(按照列排序):\n"
                                        sorted_data = deleted_data.sort_values(by=col)
                                        preview_text += sorted_data.to_string() + "\n"
                                        preview_text += "-"*30 + "\n"
                            else:
                                # 其他类型的删除显示所有行
                                preview_text += "所有删除的数据:\n"
                                preview_text += deleted_data.to_string() + "\n"
                                preview_text += "-"*30 + "\n"
                        elif isinstance(deleted_data, dict):
                            # 处理特殊情况，如时间重复数据
                            for key, data in deleted_data.items():
                                if isinstance(data, pd.DataFrame) and not data.empty:
                                    preview_text += f"\n{key}:\n"
                                    preview_text += data.to_string() + "\n"
                                    preview_text += "-"*30 + "\n"
            
            preview_text += "\n" + "="*50 + "\n\n"

        self.preview_text.setText(preview_text)
        self.clean_button.setEnabled(True)
        self.browse_button.setEnabled(True)

    def show_cleaner_error(self, error_message):
        QMessageBox.critical(self, "清洗错误", f"数据清洗过程中出错: {error_message}")
        self.clean_button.setEnabled(True)
        self.browse_button.setEnabled(True)

    # 其他通用方法实现
    def apply_styles(self):
        # 保持原有的样式设置
        self.setStyleSheet("""
        QWidget#toolbarWidget {
            background-color: #2D2D2D;
            border-top: 1px solid #3D3D3D;
            border-bottom: 1px solid #3D3D3D;
        }
        QPushButton#toolbarButton {
            background-color: transparent;
            border: none;
            border-radius: 5px;
            padding: 5px;
        }
        QPushButton#toolbarButton:hover {
            background-color: #3D3D3D;
        }
        QPushButton#toolbarButton:pressed {
            background-color: #1E1E1E;
        }
        QMainWindow {
            background-color: #1E1E1E;
            border: 1px solid #3D3D3D;
        }
        QWidget {
            background-color: #1E1E1E;
            color: #E0E0E0;
        }
        QPushButton#minButton, QPushButton#maxButton, QPushButton#closeButton {
            background-color: transparent;
            color: #E0E0E0;
            border: none;
            font-size: 24px;
        }
        QPushButton#minButton:hover, QPushButton#maxButton:hover {
            background-color: #3D3D3D;
        }
        QPushButton#closeButton:hover {
            background-color: #E81123;
        }
        QScrollArea {
            border: none;
        }
        QGroupBox {
            border: 2px solid #3D3D3D;
            border-radius: 5px;
            margin-top: 20px;
            padding-top: 10px;
            font-weight: bold;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            top: 8px;
            padding: 0 3px 0 3px;
            font-size: 18px;
        }
        QLabel {
            color: #E0E0E0;
        }
        QLineEdit, QTextEdit, QDateEdit, QTimeEdit, QComboBox {
            background-color: #2D2D2D;
            border: 1px solid #3D3D3D;
            border-radius: 3px;
            padding: 5px;
            color: #E0E0E0;
        }
        QPushButton {
            background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #4A4A4A, stop:1 #2E2E2E);
            border: none;
            color: #E0E0E0;
            padding: 8px;
            border-radius: 3px;
        }
        QPushButton:hover {
            background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #5A5A5A, stop:1 #3E3E3E);
        }
        QPushButton:pressed {
            background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3A3A3A, stop:1 #1E1E1E);
        }
        QProgressBar {
            border: 2px solid #3D3D3D;
            border-radius: 5px;
            text-align: center;
        }
        QProgressBar::chunk {
            background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3DAEE9, stop:1 #2980B9);
        }
        QCheckBox {
            spacing: 5px;
        }
        QCheckBox::indicator {
            width: 18px;
            height: 18px;
        }
        QCheckBox::indicator:unchecked {
            border: 2px solid #3D3D3D;
            background-color: #2D2D2D;
        }
        QCheckBox::indicator:checked {
            border: 2px solid #3DAEE9;
            background-color: #3DAEE9;
        }
        """)
    def open_visualization(self):
        """打开数据可视化窗口"""
        if not hasattr(self, 'visualization_window') or not self.visualization_window:
            self.visualization_window = StockDataAnalyzerGUI()
            # 如果已经选择了数据文件夹，自动传递给可视化窗口
            if hasattr(self, 'local_data_path_edit') and self.local_data_path_edit.text():
                self.visualization_window.folder_path_label.setText(self.local_data_path_edit.text())
                self.visualization_window.analyze_folder(self.local_data_path_edit.text())
        
        self.visualization_window.show()
        self.visualization_window.raise_()
        self.visualization_window.activateWindow()
    # 添加其他必要的方法


    def check_software_status(self):
        """检查MiniQMT软件状态并更新指示器"""
        try:
            # 获取当前运行状态
            current_running = self.is_software_running("XtMiniQmt.exe")
            
            # 只在状态发生变化时更新
            if not hasattr(self, '_last_miniQMT_status') or self._last_miniQMT_status != current_running:
                self._last_miniQMT_status = current_running
                
                # 获取客户端路径
                settings = QSettings('KHQuant', 'StockAnalyzer')
                default_path = r"C:\国金证券QMT交易端\bin.x64\XtItClient.exe"
                software_path = settings.value('client_path', default_path)
                
                if not os.path.exists(software_path):
                    self.update_status_indicator("red", "MiniQMT未启动(路径无效)")
                else:
                    if current_running:
                        self.update_status_indicator("green", "MiniQMT已启动")
                    else:
                        self.update_status_indicator("red", "MiniQMT未启动")
                        
        except Exception as e:
            logging.error(f"检查软件状态时出错: {str(e)}")
            self.update_status_indicator("red", "状态检查失败")

    def show_help(self):
        help_dialog = HelpDialog(self)
        help_dialog.exec_()

    # 保持原有的其他方法实现...

    def toggle_time_range(self, index):
        """切换时间范围选择"""
        use_all_time = (index == 1)  # 1 表示选择了"全天"
        self.start_time_edit.setEnabled(not use_all_time)
        self.end_time_edit.setEnabled(not use_all_time)

    def show(self):
        """重写show方法，移除激活检查"""
        try:
            super().show()
            
            # 延迟检查更新
            QTimer.singleShot(2000, self.delayed_update_check)
            
        except Exception as e:
            logging.error(f"显示主窗口时出错: {str(e)}", exc_info=True)
            QMessageBox.critical(self, "错误", f"启动程序时出错: {str(e)}")
            QApplication.quit()

    def delayed_update_check(self):
        """延迟执行更新检查"""
        try:
            self.check_for_updates()
        except Exception as e:
            logging.error(f"延迟更新检查时出错: {str(e)}", exc_info=True)

    def add_home_button(self):
        """添加主页按钮"""
        home_btn = QPushButton()
        home_btn.setIcon(self.load_icon('home.png'))
        home_btn.setIconSize(QtCore.QSize(self.icon_size, self.icon_size))
        home_btn.setFixedSize(self.icon_size + 8, self.icon_size + 8)
        home_btn.setToolTip('访问主页')
        home_btn.clicked.connect(lambda: self.open_url("https://khsci.com/khQuant"))
        home_btn.setObjectName("toolbarButton")
        
        # 在工具栏中插入主页按钮（在设置按钮之后）
        toolbar_layout = self.findChild(QHBoxLayout, "toolbarLayout")
        if toolbar_layout:
            toolbar_layout.insertWidget(2, home_btn)  # 插入到设置按钮后面

    def open_url(self, url):
        """打开指定URL"""
        import webbrowser
        webbrowser.open(url)

    # 在StockDataProcessorGUI类中添加新方法
    def update_folder_path_label(self, text):
        """更新文件夹路径标签的文本"""
        self.folder_path_label.setText(text)

    def toggle_download(self):
        """切换下载状态"""
        if hasattr(self, 'download_thread') and self.download_thread:
            # 停止下载
            if self.download_thread:
                self.download_thread.stop()
                self.status_label.setText("下载已停止")
            self.reset_download_button()
        else:
            # 开始下载
            self.download_data()

    def reset_download_button(self):
        """重置下载按钮状态"""
        self.download_button.setEnabled(True)

    def open_custom_list(self, event):
        """打开自选清单文件"""
        try:
            # 获取自选清单文件路径
            data_dir = os.path.join(os.path.dirname(__file__), 'data')
            custom_file = os.path.join(data_dir, 'otheridx.csv')
            
            if os.path.exists(custom_file):
                if sys.platform == 'win32':
                    os.startfile(custom_file)
                else:
                    import subprocess
                    subprocess.call(['xdg-open', custom_file])
                logging.info(f"已打开自选清单文件: {custom_file}")
            else:
                # 如果文件不存在，创建一个空的自选清单文件
                try:
                    with open(custom_file, 'w', encoding='utf-8') as f:
                        f.write("code,name\n")  # 写入表头
                    if sys.platform == 'win32':
                        os.startfile(custom_file)
                    else:
                        import subprocess
                        subprocess.call(['xdg-open', custom_file])
                    logging.info(f"已创建并打开新的自选清单文件: {custom_file}")
                except Exception as e:
                    QMessageBox.warning(self, "错误", f"创建自选清单文件失败: {str(e)}")
                    logging.error(f"创建自选清单文件失败: {str(e)}")
        except Exception as e:
            QMessageBox.warning(self, "错误", f"打开自选清单文件失败: {str(e)}")
            logging.error(f"打开自选清单文件失败: {str(e)}")

    def update_status(self, message):
        """更新状态信息"""
        try:
            # 在状态栏显示消息
            self.statusBar().showMessage(message)
            # 确保消息立即显示
            QApplication.processEvents()
            # 记录日志
            logging.info(message)
        except Exception as e:
            logging.error(f"更新状态信息时出错: {str(e)}")

class CustomSplashScreen(QSplashScreen):
    def __init__(self):
        # 创建启动画面图像
        splash_img = QPixmap(os.path.join(ICON_PATH, 'splash.png'))  # 确保有这个图片
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
        
        # 添加加提示文本
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
        """将启动画面居中显示"""
        frame_geo = self.frameGeometry()
        screen = QApplication.desktop().screenNumber(
            QApplication.desktop().cursor().pos())
        center_point = QApplication.desktop().screenGeometry(screen).center()
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

# 定义日志配置函数
def setup_logging():
    """配置日志系统"""
    try:
        # 在这里设置内部标志
        ENABLE_LOGGING = True  # 将标志设置为 True 开启完整日志，False 则只记录错误
        
        # 确定日志目录路径
        if getattr(sys, 'frozen', False):
            # 打包环境下，使用可执行文件所在目录
            base_path = os.path.dirname(sys.executable)
        else:
            # 开发环境下，使用当前文件所在目录
            base_path = os.path.dirname(os.path.abspath(__file__))
        
        logs_dir = os.path.join(base_path, 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        
        # 生成日志文件名（包含时间戳）
        log_filename = os.path.join(logs_dir, f"app_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
        
        # 配置日志格式
        log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        
        # 配置根日志记录器
        root_logger = logging.getLogger()
        
        # 清除已有的处理器（避免重复）
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        if ENABLE_LOGGING:
            # 启用完整日志时的配置
            root_logger.setLevel(logging.DEBUG)
            
            # 添加文件处理器
            file_handler = logging.FileHandler(log_filename, encoding='utf-8')
            file_handler.setFormatter(log_format)
            file_handler.setLevel(logging.DEBUG)
            root_logger.addHandler(file_handler)
            
            # 添加控制台处理器
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(log_format)
            console_handler.setLevel(logging.DEBUG)
            root_logger.addHandler(console_handler)
            
            # 记录启动信息
            logging.info("="*50)
            logging.info("应用程序启动")
            logging.info(f"日志文件路径: {log_filename}")
            logging.info(f"运行模式: {'打包环境' if getattr(sys, 'frozen', False) else '开发环境'}")
            logging.info(f"Python版本: {sys.version}")
            logging.info(f"操作系统: {sys.platform}")
            logging.info("="*50)
        else:
            # 仅记录错误日志时的配置
            root_logger.setLevel(logging.ERROR)
            
            # 只添加文件处理器，用于记录错误
            file_handler = logging.FileHandler(log_filename, encoding='utf-8')
            file_handler.setFormatter(log_format)
            file_handler.setLevel(logging.ERROR)
            root_logger.addHandler(file_handler)
        
        return log_filename
        
    except Exception as e:
        print(f"配置日志系统时出错: {str(e)}")
        return None

def get_data_directory():
    """获取数据存储目录"""
    try:
        # 在用户文档目录下创建应用数据文件夹
        if sys.platform == 'win32':
            documents_path = os.path.join(os.path.expanduser('~'), 'Documents')
            data_dir = os.path.join(documents_path, 'KHQuant', 'data')
        else:
            # 其他操作系统的处理
            data_dir = os.path.join(os.path.expanduser('~'), '.khquant', 'data')
            
        # 确保目录存在
        os.makedirs(data_dir, exist_ok=True)
        logging.info(f"数据目录: {data_dir}")
        return data_dir
        
    except Exception as e:
        logging.error(f"创建数据目录时出错: {str(e)}", exc_info=True)
        # 返回一个默认路径
        return os.path.join(os.path.expanduser('~'), 'KHQuant_Data')

if __name__ == '__main__':
    import sys
    import logging
    from datetime import datetime
    import time
    
    # 日志配置部分
    log_filename = setup_logging()
    
    try:
        logging.info("程序启动")
        app = QApplication(sys.argv)
        
        # 修改图标路径获取方式
        if getattr(sys, 'frozen', False):
            ICON_PATH = os.path.join(os.path.dirname(sys.executable), '_internal', 'icons')
        else:
            ICON_PATH = os.path.join(os.path.dirname(__file__), 'icons')
            
        logging.info(f"图标路径: {ICON_PATH}")
        
        # 设置应用程序图标
        icon_file = os.path.join(ICON_PATH, 'stock_icon.ico')
        if os.path.exists(icon_file):
            app_icon = QIcon(icon_file)
            app.setWindowIcon(app_icon)
            logging.info("成功加载应用图标")
        else:
            logging.warning(f"图标文件不存在: {icon_file}")
        
        # 创建主窗口但不显示
        main_window = None
        try:
            # 创建并显示启动画面
            splash = None
            try:
                splash_img = os.path.join(ICON_PATH, 'splash.png')
                if os.path.exists(splash_img):
                    splash = CustomSplashScreen()
                    splash.show()
                    app.processEvents()
                    logging.info("启动画面显示成功")
                else:
                    logging.warning("未找到启动画面图片，跳过启动画面显示")
            except Exception as splash_error:
                logging.error(f"显示启动画面时出错: {str(splash_error)}")
                splash = None

            # 创建主窗口
            main_window = StockDataProcessorGUI()
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
                    time.sleep(0.05)
                
                # 关闭启动画面
                splash.close()
                logging.info("启动画面已关闭")
                app.processEvents()

            def show_main_window():
                """显示主窗口的辅助函数"""
                try:
                                main_window.show()
                                main_window.raise_()
                                main_window.activateWindow()
                except Exception as e:
                    logging.error(f"显示主窗口时出错: {str(e)}", exc_info=True)
                    QMessageBox.critical(None, "错误", f"显示主窗口时出错: {str(e)}")
                    QApplication.quit()

            # 使用短延时确保启动画面完全关闭后再显示主窗口
            QTimer.singleShot(100, show_main_window)
            
            # 设置定时检查
            status_timer = QTimer()
            status_timer.timeout.connect(main_window.check_software_status)
            status_timer.start(5000)
            
            # 延迟执行其他初始化操作
            QTimer.singleShot(200, main_window.check_software_status)
            QTimer.singleShot(2000, main_window.delayed_update_check)
            
            logging.info("开始事件循环")
            return_code = app.exec_()
            logging.info(f"程序退出，返回码: {return_code}")
            sys.exit(return_code)
            
        except Exception as e:
            logging.error(f"初始化过程中出错: {str(e)}", exc_info=True)
            if main_window:
                main_window.close()
            if splash:
                splash.close()
            QMessageBox.critical(None, "初始化错误", 
                               f"程序初始化过程中出错:\n{str(e)}\n\n详细信息已写入日志文件")
            sys.exit(1)
        
    except Exception as e:
        logging.critical(f"程序异常退出: {str(e)}", exc_info=True)
        print(f"程序发生严重错误，详细信息已写入日志文件: {log_filename}")
        sys.exit(1)

