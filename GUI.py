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
import multiprocessing
import time
from queue import Empty
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QLineEdit, QTextEdit, QPushButton, QFileDialog, QMessageBox, QProgressBar, 
                             QComboBox, QDateEdit, QTimeEdit, QGroupBox, QScrollArea, QCheckBox, QGridLayout,
                             QDialog,QTabWidget,QSplashScreen,QProgressDialog,QMenu, QStyle, QSplitter)  # 添加QStyle和QSplitter
from PyQt5.QtCore import  Qt, QThread, pyqtSignal, QDate, QTime, QRect, QTimer,QSettings,QPoint, QMutex, QUrl
from PyQt5.QtGui import QPen,QPixmap,QFont, QIcon, QPalette, QColor, QLinearGradient, QCursor, QPixmap, QPainter, QPainterPath, QDesktopServices
from khQTTools import download_and_store_data,get_and_save_stock_list, supplement_history_data
from PyQt5 import QtCore
import logging
from GUIplotLoadData import StockDataAnalyzerGUI  # 添加这一行导入
#from activation_manager import ActivationCodeGenerator, MachineCode, ActivationManager
#from activation_thread import ActivationCheckThread  # 添加这一行
from update_manager import UpdateManager  # 将之前的UpdateManager类保存在单独的update_manager.py文件中
from version import get_version_info  # 导入版本信息
from SettingsDialog import SettingsDialog

# 自定义控件类，禁用滚轮事件
class NoWheelComboBox(QComboBox):
    """禁用滚轮事件的QComboBox"""
    def wheelEvent(self, event):
        # 忽略滚轮事件，不调用父类的wheelEvent
        event.ignore()

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


# 数据下载工作进程函数
def download_data_worker(params, progress_queue, result_queue, stop_event):
    """
    数据下载工作进程函数
    在独立进程中运行，避免GIL限制
    """
    if __name__ != '__main__':
        import multiprocessing
        multiprocessing.current_process().name = 'DownloadWorker'
    
    try:
        import sys
        import os
        import time
        
        # 延迟导入
        try:
            from khQTTools import download_and_store_data
        except Exception as import_error:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, current_dir)
            try:
                from khQTTools import download_and_store_data
            except:
                result_queue.put(('error', f"无法导入数据下载模块: {str(import_error)}"))
                return
        
        import logging
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        
        # 控制更新频率
        last_progress_time = 0
        last_status_time = 0
        update_interval = 1.0  # 1秒更新一次，减少UI压力
        
        def progress_callback(percent):
            nonlocal last_progress_time
            current_time = time.time()
            if current_time - last_progress_time >= update_interval or percent >= 100:
                if not stop_event.is_set():
                    try:
                        progress_queue.put(('progress', percent), timeout=1)
                        last_progress_time = current_time
                    except:
                        pass
                else:
                    raise InterruptedError("下载被中断")
        
        def log_callback(message):
            nonlocal last_status_time
            current_time = time.time()
            if current_time - last_status_time >= update_interval:
                if not stop_event.is_set():
                    try:
                        progress_queue.put(('status', str(message)), timeout=1)
                        last_status_time = current_time
                    except:
                        pass
                else:
                    raise InterruptedError("下载被中断")
        
        def check_interrupt():
            return stop_event.is_set()
        
        # 执行数据下载
        download_and_store_data(
            local_data_path=params['local_data_path'],
            stock_files=params['stock_files'],
            field_list=params['field_list'],
            period_type=params['period_type'],
            start_date=params['start_date'],
            end_date=params['end_date'],
            dividend_type=params.get('dividend_type', 'none'),
            time_range=params.get('time_range', 'all'),
            progress_callback=progress_callback,
            log_callback=log_callback,
            check_interrupt=check_interrupt
        )
        
        result_queue.put(('success', '数据下载完成！'))
        
    except Exception as e:
        error_msg = f"下载过程中发生错误: {str(e)}"
        result_queue.put(('error', error_msg))
        logging.error(error_msg, exc_info=True)


class DownloadThread(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str)
    error = pyqtSignal(str)  # 添加错误信号
    status_update = pyqtSignal(str)  # 添加状态更新信号

    def __init__(self, params, parent=None):  # 添加parent参数
        super().__init__(parent)
        self.params = params
        self.running = True
        self.mutex = QMutex()  # 添加互斥锁保护状态
        
        # 多进程通信队列
        self.progress_queue = multiprocessing.Queue(maxsize=50)  # 减少队列大小
        self.result_queue = multiprocessing.Queue()
        self.stop_event = multiprocessing.Event()
        self.process = None
        
        logging.info(f"初始化下载线程，参数: {params}")

    def run(self):
        try:
            if not self.isRunning():
                return
                
            # 参数验证
            if not self.params.get('stock_files'):
                raise ValueError("股票代码列表为空")

            # 创建并启动子进程
            self.process = multiprocessing.Process(
                target=download_data_worker,
                args=(self.params, self.progress_queue, self.result_queue, self.stop_event)
            )
            self.process.start()
            
            # 在线程中监控进程间通信
            while self.isRunning() and (self.process and self.process.is_alive()):
                try:
                    # 检查进度和状态消息
                    while True:
                        try:
                            msg_type, data = self.progress_queue.get_nowait()
                            if msg_type == 'progress':
                                self.progress.emit(data)
                            elif msg_type == 'status':
                                self.status_update.emit(data)
                        except:
                            break
                    
                    # 检查结果
                    try:
                        result_type, message = self.result_queue.get_nowait()
                        if result_type == 'success':
                            self.finished.emit(True, message)
                        else:
                            self.error.emit(message)
                        return
                    except:
                        pass
                    
                    # 短暂休眠
                    self.msleep(200)  # 减少检查频率
                    
                except Exception as e:
                    logging.error(f"监控下载进程时出错: {str(e)}")
                    break
            
            # 检查进程是否异常退出
            if self.process and not self.process.is_alive():
                exit_code = self.process.exitcode
                if exit_code != 0 and self.isRunning():
                    self.error.emit(f"下载进程异常退出，退出码: {exit_code}")
                
        except Exception as e:
            error_msg = f"启动下载进程时发生错误: {str(e)}"
            logging.error(error_msg, exc_info=True)
            if self.isRunning():
                self.error.emit(error_msg)
                self.finished.emit(False, error_msg)

    def stop(self):
        """停止数据下载"""
        logging.info("尝试停止下载线程")
        self.mutex.lock()
        self.running = False
        
        # 停止多进程
        try:
            if self.stop_event:
                self.stop_event.set()
                
            if self.process and self.process.is_alive():
                # 等待进程结束
                self.process.join(timeout=5)
                
                # 如果进程还没结束，强制终止
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=2)
                    
                    if self.process.is_alive():
                        self.process.kill()
        except Exception as e:
            logging.error(f"停止下载进程时出错: {str(e)}")
            
        self.mutex.unlock()
        logging.info("已设置下载中断标志")
        
    def isRunning(self):
        self.mutex.lock()
        result = self.running
        self.mutex.unlock()
        return result

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

def supplement_data_worker(params, progress_queue, result_queue, stop_event):
    """
    数据补充工作进程函数
    在独立进程中运行，避免GIL限制
    """
    # 多进程保护 - 防止在子进程中启动GUI
    if __name__ != '__main__':
        # 在子进程中，确保不会执行主程序代码
        import multiprocessing
        multiprocessing.current_process().name = 'GUISupplementWorker'
    
    try:
        # 在子进程中导入需要的模块
        import sys
        import os
        
        # 延迟导入并捕获任何GUI相关错误
        try:
            from khQTTools import supplement_history_data
        except Exception as import_error:
            # 如果导入失败，尝试直接从当前目录导入
            current_dir = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, current_dir)
            try:
                from khQTTools import supplement_history_data
            except:
                result_queue.put(('error', f"无法导入数据补充模块: {str(import_error)}"))
                return
        
        import logging
        import time
        import re
        
        # 配置子进程的日志
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        
        # 进度和状态更新的时间控制
        last_progress_time = 0
        last_status_time = 0
        update_interval = 0.5  # 500毫秒
        
        # 统计信息
        supplement_stats = {
            'total_stocks': 0,
            'success_count': 0,
            'empty_data_count': 0,
            'error_count': 0,
            'empty_stocks': []
        }
        
        def progress_callback(percent):
            nonlocal last_progress_time
            current_time = time.time()
            if current_time - last_progress_time >= update_interval or percent >= 100:
                try:
                    progress_queue.put(('progress', percent), timeout=1)
                    last_progress_time = current_time
                    print(f"[GUI进程] 发送进度: {percent}%")  # 调试信息
                except Exception as e:
                    print(f"[GUI进程] 发送进度失败: {e}")
        
        def log_callback(message):
            nonlocal last_status_time
            current_time = time.time()
            
            try:
                # 处理消息的统计和格式化（修复过滤逻辑）
                # 检查是否是补充数据的成功消息
                success_pattern = r"^补充\s+(.*?\.\S+)\s+数据成功"
                success_match = re.match(success_pattern, message)
                
                if success_match:
                    # 这是成功的补充消息，应该显示出来
                    stock_code = success_match.group(1)
                    supplement_stats['success_count'] += 1
                    
                    # 直接转发成功消息，不修改格式
                    progress_queue.put(('status', message), timeout=1)
                    print(f"[GUI进程] 发送成功状态: {message}")  # 调试信息
                    return
                
                # 检查是否是错误消息
                error_pattern = r"^补充\s+(.*?\.\S+)\s+数据时出错"
                error_match = re.match(error_pattern, message)
                
                if error_match:
                    # 这是错误消息，应该显示出来
                    stock_code = error_match.group(1)
                    supplement_stats['error_count'] += 1
                    
                    # 直接转发错误消息
                    progress_queue.put(('status', message), timeout=1)
                    print(f"[GUI进程] 发送错误状态: {message}")  # 调试信息
                    return
                
                # 检查是否是空数据消息
                empty_pattern = r"^补充\s+(.*?\.\S+)\s+数据成功，但数据为空"
                empty_match = re.match(empty_pattern, message)
                
                if empty_match:
                    # 这是空数据消息，应该显示出来
                    stock_code = empty_match.group(1)
                    supplement_stats['empty_data_count'] += 1
                    if stock_code not in supplement_stats['empty_stocks']:
                        supplement_stats['empty_stocks'].append(stock_code)
                    
                    # 直接转发空数据消息
                    progress_queue.put(('status', message), timeout=1)
                    print(f"[GUI进程] 发送空数据状态: {message}")  # 调试信息
                    return
                
                # 重要消息立即发送
                is_important = any(keyword in str(message) for keyword in ['开始', '完成', '失败', '错误', '中断'])
                
                if is_important or current_time - last_status_time >= update_interval:
                    try:
                        progress_queue.put(('status', str(message)), timeout=1)
                        last_status_time = current_time
                        print(f"[GUI进程] 发送状态: {message}")  # 调试信息
                    except Exception as e:
                        print(f"[GUI进程] 发送状态失败: {e}")
                        
            except Exception as e:
                print(f"[GUI进程] log_callback 处理错误: {e}")
        
        def check_interrupt():
            # 检查停止事件
            return stop_event.is_set()
        
        # 执行数据补充
        supplement_history_data(
            stock_files=params['stock_files'],
            field_list=params['field_list'],
            period_type=params['period_type'],
            start_date=params['start_date'],
            end_date=params['end_date'],
            time_range=params.get('time_range', 'all'),
            dividend_type=params.get('dividend_type', 'none'),
            progress_callback=progress_callback,
            log_callback=log_callback,
            check_interrupt=check_interrupt
        )
        
        # 构建详细的完成消息（单行汇总，避免换行）
        total = supplement_stats['success_count'] + supplement_stats['empty_data_count'] + supplement_stats['error_count']
        parts = ["数据补充完成！"]
        parts.append(f"总股票数: {total}")
        parts.append(f"成功补充: {supplement_stats['success_count']} 只股票")
        if supplement_stats['empty_data_count']:
            parts.append(f"空数据: {supplement_stats['empty_data_count']}")
        if supplement_stats['error_count']:
            parts.append(f"出错: {supplement_stats['error_count']}")
        result_message = "；".join(parts)
        
        # 发送完成信号
        result_queue.put(('success', result_message.strip()))
        
    except Exception as e:
        error_msg = f"补充数据过程中发生错误: {str(e)}"
        result_queue.put(('error', error_msg))
        logging.error(error_msg, exc_info=True)


# 添加数据补充线程类（现在使用多进程后端）
class SupplementThread(QThread):
    """数据补充线程（现在使用多进程后端）"""
    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str)
    error = pyqtSignal(str)
    status_update = pyqtSignal(str)

    def __init__(self, params, parent=None):
        super().__init__(parent)
        self.params = params
        self.running = True
        self.mutex = QMutex()
        
        # 在主线程中创建进程间通信队列
        self.progress_queue = multiprocessing.Queue(maxsize=100)
        self.result_queue = multiprocessing.Queue()
        self.stop_event = multiprocessing.Event()
        self.process = None

    def run(self):
        try:
            if not self.isRunning():
                return
                
            # 参数验证
            if not self.params.get('stock_files'):
                raise ValueError("股票代码列表为空")

            # 创建并启动子进程
            self.process = multiprocessing.Process(
                target=supplement_data_worker,
                args=(self.params, self.progress_queue, self.result_queue, self.stop_event)
            )
            self.process.start()
            
            # 在线程中直接监控进程间通信
            while self.isRunning() and (self.process and self.process.is_alive()):
                try:
                    # 检查进度消息
                    while True:
                        try:
                            msg_type, data = self.progress_queue.get_nowait()
                            if msg_type == 'progress':
                                print(f"[GUI线程] 接收进度: {data}%")  # 调试信息
                                self.progress.emit(data)
                            elif msg_type == 'status':
                                print(f"[GUI线程] 接收状态: {data}")  # 调试信息
                                self.status_update.emit(data)
                        except Empty:
                            break
                    
                    # 检查结果
                    try:
                        result_type, message = self.result_queue.get_nowait()
                        if result_type == 'success':
                            self.finished.emit(True, message)
                        else:
                            self.error.emit(message)
                        return  # 完成后退出
                    except Empty:
                        pass
                    
                    # 短暂休眠
                    self.msleep(100)
                    
                except Exception as e:
                    logging.error(f"监控进程时出错: {str(e)}")
                    break
            
            # 检查进程是否异常退出
            if self.process and not self.process.is_alive():
                exit_code = self.process.exitcode
                if exit_code != 0 and self.isRunning():
                    self.error.emit(f"数据补充进程异常退出，退出码: {exit_code}")
                
        except Exception as e:
            error_msg = f"启动数据补充进程时发生错误: {str(e)}"
            logging.error(error_msg, exc_info=True)
            if self.isRunning():
                self.error.emit(error_msg)
                self.finished.emit(False, error_msg)

    def stop(self):
        """停止数据补充"""
        self.mutex.lock()
        self.running = False
        
        # 停止多进程
        try:
            if self.stop_event:
                self.stop_event.set()
                
            if self.process and self.process.is_alive():
                # 等待进程结束
                self.process.join(timeout=5)
                
                # 如果进程还没结束，强制终止
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=2)
                    
                    if self.process.is_alive():
                        self.process.kill()
        except Exception as e:
            logging.error(f"停止进程时出错: {str(e)}")
            
        self.mutex.unlock()
        
    def isRunning(self):
        self.mutex.lock()
        result = self.running
        self.mutex.unlock()
        return result

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
        
        # 检测屏幕分辨率并设置字体大小比例
        self.font_scale = self.detect_screen_resolution()
        
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
        # logging.info(f"初始化图标路径: {self.ICON_PATH}")
        if os.path.exists(self.ICON_PATH):
            pass
            #logging.info(f"图标目录内容: {os.listdir(self.ICON_PATH)}")
        else:
            logging.warning(f"图标目录不存在: {self.ICON_PATH}")

        self.cleaner = StockDataCleaner()
        self.visualization_window = None

        # 初始化更新管理器（在其他初始化之前） 
        self.initialize_update_manager()

        # 修改窗口属性设置
        # self.setAttribute(Qt.WA_TranslucentBackground) # 移除
        # self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowSystemMenuHint) # 移除，使用默认窗口样式
        
        # 移除之前设置的非透明背景（冲突设置）
        # self.setAttribute(Qt.WA_TranslucentBackground, False)  # 禁用透明背景

        # 设置窗口背景色 (这个会影响 central_widget 的背景，如果central_widget有自己的背景设置，这个可能不需要)
        palette = self.palette()
        palette.setColor(QPalette.Window, QColor("#2b2b2b"))
        self.setPalette(palette)

        # 获取版本信息
        self.version_info = get_version_info()
        
        # 在启动画面显示版本信息
        if hasattr(self, 'splash'):
            self.version_label.setText(f"V{self.version_info['version']}")

        self.initUI()
        # 下面这些属性是为无边框窗口拖动和缩放服务的，现在移除
        # self.can_drag = False
        # self.resizing = False
        # self.resize_edge = None
        # self.border_thickness = 20 
        # self.setMouseTracking(True) # 如果没有其他地方用到mouseMoveEvent，则此行也应移除
        
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

        # 不再使用状态栏，改用status_label (status_label现在也没有明确位置了)
        # self.statusBar().showMessage('就绪')
        
        # 隐藏状态栏 (如果用系统标题栏，可以考虑显示状态栏)
        # self.statusBar().hide()

        # 获取屏幕分辨率
        screen = QApplication.primaryScreen().geometry()
        screen_width = screen.width()
        screen_height = screen.height()
        
        # 设置初始窗口大小（将通过showFullScreen()进入全屏）
        # 在全屏模式下不需要初始resize，因为会立即进入全屏

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
        
        return f"""
            /* 主窗口和基础样式 */
            QMainWindow {{
                font-size: {scaled_sizes['normal']}px;
            }}
            
            QWidget#mainContainer {{
                background-color: #2b2b2b;
                color: #f0f0f0;
                border: none;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            QWidget {{
                background-color: #2b2b2b;
                color: #f0f0f0;
                border: none;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            /* 分组框样式 */
            QGroupBox {{
                background-color: #333333;
                border: 1px solid #4a4a4a;
                border-radius: 6px;
                margin-top: 1em;
                padding-top: 1em;
                color: #f0f0f0;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: #f0f0f0;
                font-weight: bold;
                background-color: #333333;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            /* 标签样式 */
            QLabel {{
                color: #f0f0f0;
                background-color: transparent;
                border: none;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            /* 链接样式 */
            QLabel[linkEnabled="true"] {{
                color: #b0b0b0;
                font-size: {scaled_sizes['normal']}px;
            }}
            QLabel[linkEnabled="true"]:hover {{
                color: #ffffff;
            }}
            
            /* 输入框样式 */
            QLineEdit {{
                background-color: #3a3a3a;
                border: 1px solid #454545;
                border-radius: 4px;
                padding: 5px;
                color: #f0f0f0;
                selection-background-color: #0078d7;
                font-size: {scaled_sizes['normal']}px;
            }}
            QLineEdit:focus {{
                border: 1px solid #0078d7;
                background-color: #3c3c3c;
            }}
            
            /* 按钮样式 */
            QPushButton {{
                background-color: #444444;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                padding: 6px 14px;
                color: #f0f0f0;
                min-width: 80px;
                font-weight: bold;
                font-size: {scaled_sizes['normal']}px;
            }}
            QPushButton:hover {{
                background-color: #505050;
                border-color: #555555;
            }}
            QPushButton:pressed {{
                background-color: #353535;
                border-color: #0078d7;
            }}
            QPushButton:disabled {{
                background-color: #353535;
                color: #777777;
                border-color: #3c3c3c;
            }}

            /* 工具栏按钮特殊样式 */
            QPushButton#toolbarButton {{
                background-color: transparent;
                border: none;
                padding: 5px;
                min-width: 0px;
                font-size: {scaled_sizes['normal']}px;
            }}
            QPushButton#toolbarButton:hover {{
                background-color: #505050;
            }}
            QPushButton#toolbarButton:pressed {{
                background-color: #353535;
            }}
            
            /* 下拉框样式 */
            QComboBox {{
                background-color: #3a3a3a;
                border: 1px solid #454545;
                border-radius: 4px;
                padding: 5px;
                color: #f0f0f0;
                min-width: 100px;
                font-size: {scaled_sizes['normal']}px;
            }}
            QComboBox:hover {{
                border: 1px solid #555555;
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QComboBox::down-arrow {{
                image: none; 
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #f0f0f0;
                margin-right: 5px;
            }}
            QComboBox QAbstractItemView {{
                background-color: #3a3a3a;
                border: 1px solid #454545;
                selection-background-color: #0078d7;
                selection-color: #ffffff;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            /* 表格样式 */
            QTableWidget {{
                background-color: #333333;
                alternate-background-color: #383838;
                border: 1px solid #3c3c3c;
                color: #f0f0f0;
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
                color: #f0f0f0;
                padding: 8px;
                border: none;
                border-right: 1px solid #454545;
                border-bottom: 1px solid #454545;
                font-weight: bold;
                font-size: {scaled_sizes['normal']}px;
            }}
            QTableCornerButton::section {{
                background-color: #3a3a3a;
                border: none;
                border-right: 1px solid #454545;
                border-bottom: 1px solid #454545;
            }}
            QTableCornerButton::section:pressed {{
                background-color: #444444;
            }}
            
            /* 复选框样式 */
            QCheckBox {{
                color: #f0f0f0;
                spacing: 5px;
                background-color: transparent;
                font-size: {scaled_sizes['normal']}px;
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                border: 2px solid #555555;
                border-radius: 3px;
                background-color: #3a3a3a;
            }}
            QCheckBox::indicator:checked {{
                background-color: #0078d7;
                border: 2px solid #0078d7;
            }}
            QCheckBox::indicator:hover {{
                border: 2px solid #777777;
            }}
            
            /* 单选按钮样式 */
            QRadioButton {{
                color: #f0f0f0;
                spacing: 5px;
                font-size: {scaled_sizes['normal']}px;
            }}
            QRadioButton::indicator {{
                width: 16px;
                height: 16px;
                border: 2px solid #555555;
                border-radius: 8px;
                background-color: #3a3a3a;
            }}
            QRadioButton::indicator:checked {{
                background-color: #0078d7;
                border: 2px solid #0078d7;
            }}
            QRadioButton::indicator:hover {{
                border: 2px solid #777777;
            }}
            
            /* 旋转框样式 */
            QSpinBox, QDoubleSpinBox {{
                background-color: #3a3a3a;
                border: 1px solid #454545;
                border-radius: 4px;
                padding: 5px;
                color: #f0f0f0;
                font-size: {scaled_sizes['normal']}px;
            }}
            QSpinBox:focus, QDoubleSpinBox:focus {{
                border: 1px solid #0078d7;
                background-color: #3c3c3c;
            }}
            
            /* 日期时间编辑器样式 */
            QDateEdit, QTimeEdit, QDateTimeEdit {{
                background-color: #3a3a3a;
                border: 1px solid #454545;
                border-radius: 4px;
                padding: 5px;
                color: #f0f0f0;
                font-size: {scaled_sizes['normal']}px;
            }}
            QDateEdit:focus, QTimeEdit:focus, QDateTimeEdit:focus {{
                border: 1px solid #0078d7;
                background-color: #3c3c3c;
            }}
            
            /* 文本编辑器样式 */
            QTextEdit, QPlainTextEdit {{
                background-color: #2b2b2b;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                color: #f0f0f0;
                selection-background-color: #0078d7;
                font-family: "Consolas", "Microsoft YaHei", monospace;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            /* 进度条样式 */
            QProgressBar {{
                background-color: #3a3a3a;
                border: 1px solid #454545;
                border-radius: 5px;
                text-align: center;
                font-size: {scaled_sizes['normal']}px;
            }}
            QProgressBar::chunk {{
                background-color: #0078d7;
                border-radius: 4px;
            }}
            
            /* 状态栏样式 */
            QStatusBar {{
                background-color: #333333;
                color: #f0f0f0;
                border-top: 1px solid #454545;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            /* 菜单栏样式 */
            QMenuBar {{
                background-color: #333333;
                color: #f0f0f0;
                border-bottom: 1px solid #454545;
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
                border: 1px solid #454545;
                color: #f0f0f0;
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
                background-color: #454545;
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
                background-color: #454545;
                width: 1px;
                margin: 2px;
            }}
            
            /* 工具提示样式 */
            QToolTip {{
                background-color: #555555;
                color: #f0f0f0;
                border: 1px solid #666666;
                padding: 4px;
                border-radius: 3px;
                font-size: {scaled_sizes['small']}px;
            }}
            
            /* Tab样式 */
            QTabWidget::pane {{
                border: 1px solid #454545;
                background-color: #333333;
            }}
            QTabBar::tab {{
                background-color: #3a3a3a;
                color: #f0f0f0;
                padding: 8px 16px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                font-size: {scaled_sizes['normal']}px;
            }}
            QTabBar::tab:selected {{
                background-color: #333333;
                border-bottom: 2px solid #0078d7;
            }}
            QTabBar::tab:hover {{
                background-color: #505050;
            }}
            
            /* 分割器样式 */
            QSplitter::handle {{
                background-color: #454545;
            }}
            QSplitter::handle:horizontal {{
                width: 2px;
            }}
            QSplitter::handle:vertical {{
                height: 2px;
            }}
            
            /* 滚动条样式 */
            QScrollBar:vertical {{
                background-color: #2b2b2b; 
                width: 12px;
                margin: 0px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background-color: #4d4d4d;
                min-height: 20px;
                border-radius: 6px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical:hover {{
                background-color: #5a5a5a;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
            QScrollBar:horizontal {{
                background-color: #2b2b2b;
                height: 12px;
                margin: 0px;
                border: none;
            }}
            QScrollBar::handle:horizontal {{
                background-color: #4d4d4d;
                min-width: 20px;
                border-radius: 6px;
                margin: 2px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background-color: #5a5a5a;
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0px;
            }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
                background: none;
            }}
        """

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
        (状态指示器 QLabel 控件已移除，此方法可能需要修改或移除，除非有新的状态显示机制)
        """
        try:
            if hasattr(self, 'status_indicator'): # status_indicator QLabel 已被注释掉
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
        # 禁用更新管理器
        self.update_manager = None
        pass
    
    def set_update_config(self):
        """设置更新配置"""
        # 禁用更新配置
        pass
    
    def check_for_updates(self):
        """检查软件更新"""
        # 禁用更新检查
        pass
    
    def delayed_update_check(self):
        """延迟执行更新检查"""
        # 禁用延迟更新检查
        pass
    
    def handle_update_check_finished(self, success, message):
        """处理更新检查完成的回调"""
        # 禁用更新检查回调
        pass

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
        screen_height = screen.height()
                
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
            if widget.objectName() not in ["minButton", "maxButton", "closeButton", "helpButton"]: # 这些按钮已移除
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
        # 设置窗口标题栏颜色（仅适用于Windows） - 借鉴 GUIkhQuant.py
        if sys.platform == 'win32':
            try:
                from ctypes import windll, c_int, byref, sizeof, POINTER
                from ctypes.wintypes import DWORD, HWND, BOOL

                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                DWMWA_CAPTION_COLOR = 35
                
                hwnd = int(self.winId())
                # 先尝试启用深色模式（一些较新Windows版本需要这个才能让标题栏颜色生效）
                windll.dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    DWMWA_USE_IMMERSIVE_DARK_MODE,
                    byref(c_int(1)), # 1 for true, 0 for false, 2 for force true
                    sizeof(c_int)
                )
                
                # 设置标题栏颜色为 #2b2b2b
                # BGR format: 0x00bbggrr
                caption_color_val = 0x002B2B2B 
                caption_color = DWORD(caption_color_val)
                windll.dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    DWMWA_CAPTION_COLOR,
                    byref(caption_color),
                    sizeof(caption_color)
                )

            except Exception as e:
                logging.warning(f"设置Windows标题栏深色模式或颜色失败: {str(e)}")

        self.setWindowTitle('CSV数据管理模块')
        
        # 设置最小尺寸（减小窗口尺寸）
        MIN_SIZE = 800  # 将最小尺寸从900减小到800
        self.setMinimumSize(MIN_SIZE, MIN_SIZE)
        
        # 获取屏幕分辨率以用于居中
        screen_geometry = QApplication.primaryScreen().geometry()
        
        # 设置默认窗口大小为 1000x1000
        window_width = 1300
        window_height = 1300
        
        # 计算居中位置
        x_position = (screen_geometry.width() - window_width) // 2
        y_position = (screen_geometry.height() - window_height) // 2
        
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

        # self.setWindowFlag(Qt.FramelessWindowHint) # 移除
        # self.setAttribute(Qt.WA_TranslucentBackground) # 移除
        
        central_widget = QWidget()
        central_widget.setObjectName("mainContainer") # mainContainer 样式可能需要调整
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10) # 边距可以保留

        # --- 自定义标题栏移除开始 ---
        # title_bar = QWidget()
        # title_bar.setObjectName("titleBar")
        # title_bar.setFixedHeight(40)
        # title_bar_layout = QHBoxLayout(title_bar)
        # title_bar_layout.setContentsMargins(15, 0, 10, 0) 
        # title_bar_layout.setSpacing(0) 
        # title_label = QLabel("看海量化回测系统——数据模块")
        # title_label.setObjectName("titleLabel")
        # title_bar_layout.addWidget(title_label)
        # title_bar_layout.addStretch()
        # help_button = QPushButton("?")
        # help_button.setObjectName("helpButton")
        # help_button.setFixedSize(40, 40)
        # help_button.clicked.connect(self.show_help) 
        # title_bar_layout.addWidget(help_button)
        # for button_text, func, obj_name in [("—", self.showMinimized, "minButton"), 
        #                                     ("□", self.toggle_maximize, "maxButton"), 
        #                                     ("×", self.close, "closeButton")]:
        #     button = QPushButton(button_text)
        #     button.setObjectName(obj_name)
        #     button.setFixedSize(40, 40)
        #     button.clicked.connect(func)
        #     title_bar_layout.addWidget(button)
        # main_layout.addWidget(title_bar)
        # --- 自定义标题栏移除结束 ---

        # 状态指示器需要重新考虑位置，原自定义标题栏已移除
        # self.status_indicator = QLabel()
        # self.status_indicator.setFixedSize(20, 20)
        # self.status_indicator.setToolTip("交易平台状态")
        # title_bar_layout.insertWidget(title_bar_layout.count() - 3, self.status_indicator) # 会报错

        # 添加数据可视化按钮到工具栏
        visualize_btn = QPushButton()
        visualize_btn.setIcon(self.load_icon('visualize.png'))

        # 根据屏幕分辨率动态设置图标大小
        screen = QApplication.primaryScreen().geometry()
        screen_width = screen.width()
        screen_height = screen.height()
                
        # 根据屏幕宽度设置不同的图标大小（与 add_version_menu 和 GUIkhQuant.py 统一）
        if screen_width >= 2560:  # 2K及以上分辨率
            icon_size = 60
        elif screen_width >= 1920:  # 1080P
            icon_size = 40
        else:  # 较低分辨率
            icon_size = 32
        
        # 添加图标加载错误处理
        # if os.path.exists(icon_path): # icon_path 在这里未定义，应该用 self.ICON_PATH
        #    visualize_btn.setIcon(QIcon(icon_path))
        # else:
        #    logging.warning(f"图标文件未找到: {icon_path}")
        #    # 创建一个临时的替代图标
        #    self.create_fallback_icon()
        #    visualize_btn.setIcon(QIcon(os.path.join(self.ICON_PATH, 'visualize.png')))
        # 使用 self.load_icon 来加载，它内部处理了回退
        visualize_btn.setIcon(self.load_icon('visualize.png'))
        
                
        # 根据屏幕分辨率动态设置工具栏高度
        if screen_width >= 2560 or screen_height >= 1440:  # 2K及以上分辨率
            toolbar_height = 70
            toolbar_margins_left_right = 20
            toolbar_spacing = 20
        elif screen_width >= 1920 or screen_height >= 1080:  # 1080P
            toolbar_height = 60
            toolbar_margins_left_right = 15
            toolbar_spacing = 15
        else:  # 较低分辨率
            toolbar_height = 50 # GUIkhQuant.py中较低分辨率工具栏高度通常为50
            toolbar_margins_left_right = 10
            toolbar_spacing = 10
            
        toolbar_widget = QWidget()
        toolbar_widget.setObjectName("toolbarWidget") # 确保ID用于样式表
        toolbar_widget.setFixedHeight(toolbar_height)
        toolbar_layout = QHBoxLayout(toolbar_widget)
        toolbar_layout.setObjectName("toolbarLayout") # 添加ID
        # 修改上下边距为0，保持左右边距不变
        toolbar_layout.setContentsMargins(toolbar_margins_left_right, 0, toolbar_margins_left_right, 0)
        toolbar_layout.setSpacing(toolbar_spacing)
        # 设置布局的对齐方式为垂直居中
        toolbar_layout.setAlignment(Qt.AlignVCenter | Qt.AlignLeft) # 明确左对齐
        
        visualize_btn.setIconSize(QtCore.QSize(icon_size, icon_size))
        visualize_btn.setFixedSize(icon_size + 8, icon_size + 8) # 稍微调整按钮大小以适应图标
        visualize_btn.setToolTip('数据可视化')
        visualize_btn.clicked.connect(self.open_visualization)
        visualize_btn.setObjectName("toolbarButton")
        toolbar_layout.addWidget(visualize_btn)
        
        # 在toolbar_layout中,在visualize_btn之后添加设置按钮
        settings_btn = QPushButton()
        settings_btn.setIcon(self.load_icon('settings.png'))
        settings_btn.setIconSize(QtCore.QSize(icon_size, icon_size))
        settings_btn.setFixedSize(icon_size + 8, icon_size + 8) # 稍微调整按钮大小
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
        
        # 创建水平分割器来容纳两个界面，确保固定比例
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)  # 防止面板被完全折叠

        # 添加左侧下载界面
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_content = QWidget()
        left_scroll.setWidget(left_content)
        left_layout = QVBoxLayout(left_content)
        
        # 添加下载界面的组件
        self.add_downloader_interface(left_layout)
        splitter.addWidget(left_scroll)

        # 添加右侧清洗界面
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_content = QWidget()
        right_scroll.setWidget(right_content)
        right_layout = QVBoxLayout(right_content)
        
        # 添加清洗界面的组件
        self.add_cleaner_interface(right_layout)
        splitter.addWidget(right_scroll)

        # 设置初始分割比例（左:右 = 1:1）
        splitter.setSizes([500, 500])
        splitter.setStretchFactor(0, 1)  # 左侧面板拉伸因子
        splitter.setStretchFactor(1, 1)  # 右侧面板拉伸因子

        # 将分割器添加到主布局
        main_layout.addWidget(splitter)

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

    # paintEvent, mouseMoveEvent, mousePressEvent, mouseReleaseEvent, leaveEvent, 
    # get_resize_edge, resize_window, toggle_maximize 方法将被完全删除。

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
        # title_label.setFont(QFont("Roboto", 12, QFont.Bold))  # 由样式表或默认字体控制
        title_label.setStyleSheet("color: #E0E0E0; margin-bottom: 10px;") 
        layout.addWidget(title_label)

        self.add_path_group(layout, title_font)
        self.add_stock_group(layout, title_font)
        self.add_period_group(layout, title_font)
        self.add_field_group(layout, title_font)
        self.add_date_group(layout, title_font)
        self.add_time_group(layout, title_font)
        self.add_download_section(layout)

        # 创建状态标签容器来控制宽度
        status_container = QWidget()
        status_container.setMaximumWidth(500)  # 设置最大宽度
        status_container.setMinimumWidth(200)  # 设置最小宽度
        status_layout = QVBoxLayout(status_container)
        status_layout.setContentsMargins(0, 0, 0, 0)
        
        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: #E0E0E0; font-size: 14px; line-height: 1.2;")
        self.status_label.setWordWrap(True)  # 启用文本换行
        # 设置固定高度，大约能显示两行文字（14px字体 * 1.2行高 * 2行 + 一些padding）
        self.status_label.setFixedHeight(40)  
        self.status_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)  # 设置对齐方式
        status_layout.addWidget(self.status_label)
        
        layout.addWidget(status_container)

    def add_cleaner_interface(self, layout):
        # 添加标题
        title_label = QLabel("数据清洗")
        # title_label.setFont(QFont("Roboto", 12, QFont.Bold))  # 由样式表或默认字体控制
        title_label.setStyleSheet("color: #E0E0E0; margin-bottom: 10px;")
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
        preview_layout.setContentsMargins(8, 8, 8, 8)  # 减少内边距
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
        # 创建股票代码列表组
        stocks_group = QGroupBox("股票代码列表文件")
        stocks_group.setObjectName("stockListGroup")  # 添加特殊ID以便应用样式
        
        stock_layout = QVBoxLayout()
        stock_layout.setSpacing(8)  # 减少子区域之间的间距（从15改为8）
        
        # 直接添加复选框布局，不使用组框
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
            'convertible_bonds': '沪深转债',
            'custom': '自选清单'  # 添加自选清单选项
        }
        
        # 创建复选框网格布局
        checkbox_layout = QGridLayout()
        checkbox_layout.setVerticalSpacing(5)  # 减少垂直间距（从10改为5）
        checkbox_layout.setHorizontalSpacing(10)  # 减少水平间距（从15改为10）
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
                        color: #f0f0f0;  /* 使用与普通文本相同的颜色 */
                        text-decoration: underline;  /* 保留下划线 */
                        cursor: pointer;
                        font-weight: bold;
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
        
        # 直接添加自定义列表按钮，不使用组框
        custom_layout = QHBoxLayout()
        
        # 根据字体缩放比例动态计算按钮高度
        button_height = int(32 * self.font_scale)  # 基础高度32px，根据缩放比例调整
        
        browse_button = QPushButton("添加自定义列表")
        browse_button.setMinimumHeight(button_height)  # 使用最小高度而不是最大高度
        browse_button.clicked.connect(self.add_custom_stock_file)
        custom_layout.addWidget(browse_button)
        
        clear_button = QPushButton("清空列表")
        clear_button.setMinimumHeight(button_height)  # 使用最小高度而不是最大高度
        clear_button.clicked.connect(self.clear_stock_files)
        custom_layout.addWidget(clear_button)
        
        custom_layout.addStretch()
        
        stock_layout.addLayout(custom_layout)
        
        # 添加已选文件预览
        preview_group = QGroupBox("已选列表预览")
        preview_group.setObjectName("stockPreviewGroup")  # 添加子区域ID
        preview_layout = QVBoxLayout()
        preview_layout.setContentsMargins(8, 8, 8, 8)  # 减少内边距
        self.stock_files_preview = QTextEdit()
        self.stock_files_preview.setObjectName("stockFilesPreview")  # 添加预览文本区域ID
        self.stock_files_preview.setReadOnly(True)
        # 移除固定最大高度限制，设置最小高度和大小策略让其能够自适应
        self.stock_files_preview.setMinimumHeight(60)
        from PyQt5.QtWidgets import QSizePolicy
        self.stock_files_preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        preview_layout.addWidget(self.stock_files_preview, 1)  # 添加伸展因子
        preview_group.setLayout(preview_layout)
        # 为预览组设置大小策略，让它能够扩展
        preview_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # 添加预览组到股票布局时设置伸展因子
        stock_layout.addWidget(preview_group, 1)  # 伸展因子为1
        
        # 连接复选框信号
        for stock_type, checkbox in self.stock_checkboxes.items():
            checkbox.stateChanged.connect(lambda state, st=stock_type: self.on_stock_checkbox_changed(st, state))
        
        stocks_group.setLayout(stock_layout)
        # 为整个股票组设置大小策略，让它能够扩展
        stocks_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # 添加股票组到主布局时设置伸展因子
        layout.addWidget(stocks_group, 1)  # 伸展因子为1，让它占据更多空间

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

    def on_stock_checkbox_changed(self, stock_type, state):
        """股票池复选框状态改变处理"""
        # 更新预览
        self.update_stock_files_preview()
    
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
                        '中证500成分股', '沪深300成分股', '上证50成分股', '沪深转债',
                        'otheridx.csv'
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
                        custom_file = self.get_custom_list_path()
                        if os.path.exists(custom_file):
                            filename = custom_file
                        else:
                            # 如果自选清单文件不存在，创建一个示例文件
                            try:
                                # 确保目录存在
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
                                filename = custom_file
                                logging.info(f"已创建新的自选清单文件: {custom_file}")
                            except Exception as e:
                                logging.error(f"创建自选清单文件失败: {str(e)}")
                    elif stock_type == 'convertible_bonds':
                        filename = os.path.join(data_dir, "沪深转债_列表.csv")
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
            # 如果下载线程正在运行，点击按钮就停止下载
            if hasattr(self, 'download_thread') and self.download_thread and self.download_thread.isRunning():
                self.download_thread.stop()
                self.status_label.setText("下载已停止")
                self.reset_download_button()
                return
                
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
                except: # noqa: E722
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
                # 获取时间范围
                time_range = 'all'
                if self.use_all_time_checkbox.currentIndex() == 0:  # 指定时间段
                    start_time = self.start_time_edit.time().toString("HH:mm")
                    end_time = self.end_time_edit.time().toString("HH:mm")
                    time_range = f"{start_time}-{end_time}"
                
                # 准备参数字典
                params = {
                    'local_data_path': local_data_path,
                    'stock_files': selected_files,
                    'field_list': selected_fields,
                    'period_type': period_type,
                    'start_date': self.start_date_edit.date().toString('yyyyMMdd'),
                    'end_date': self.end_date_edit.date().toString('yyyyMMdd'),
                    'dividend_type': dividend_type,
                    'time_range': time_range
                }
                
                # 创建并启动下载线程
                if hasattr(self, 'supplement_button'):
                    self.supplement_button.setEnabled(False)
                self.progress_bar.setValue(0)
                self.status_label.setText("正在下载数据...")
                
                # 更改下载按钮为停止下载按钮
                self.download_button.setText("停止下载")
                self.download_button.setStyleSheet("background-color: #E74C3C;")
                
                self.download_thread = DownloadThread(params, self)
                self.download_thread.progress.connect(self.update_progress)
                self.download_thread.finished.connect(self.download_finished)
                self.download_thread.error.connect(self.handle_download_error)
                self.download_thread.status_update.connect(self.update_status)
                self.download_thread.start()
                
            except Exception as e:
                logging.error(f"启动下载线程时出错: {str(e)}", exc_info=True)
                QMessageBox.critical(self, "错误", f"启动下载线程时出错: {str(e)}")
                self.reset_download_button()
                if hasattr(self, 'supplement_button'):
                    self.supplement_button.setEnabled(True)

        except Exception as e:
            logging.error(f"准备下载数据时出错: {str(e)}")
            QMessageBox.critical(self, "错误", f"准备下载数据时出错: {str(e)}")
            self.reset_download_button()

    def handle_download_error(self, error_msg):
        """处理下载错误"""
        logging.error(f"下载错误: {error_msg}")
        QMessageBox.critical(self, "下载错误", error_msg)
        self.reset_download_button()
        # 清除线程引用
        self.download_thread = None
        self.status_label.setText("下载失败")

    def update_progress(self, value):
        self.progress_bar.setValue(value)

    def download_finished(self, success, message):
        self.reset_download_button()
        # 清除线程引用
        self.download_thread = None
        
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

        # 周期类型下拉框
        self.period_type_combo = NoWheelComboBox()
        self.period_type_combo.addItems(['tick', '1m', '5m', '1d'])
        self.period_type_combo.currentTextChanged.connect(self.update_field_checkboxes)
        period_layout.addWidget(self.period_type_combo)
        period_group.setLayout(period_layout)
        h_layout.addWidget(period_group)

        # 复权方式组
        dividend_group = QGroupBox("复权方式")
        dividend_group.setFont(title_font)
        dividend_layout = QVBoxLayout()

        # 添加说明文字
        note_label = QLabel("注：补充数据模式不涉及复权")
        note_label.setStyleSheet("color: gray; font-size: 12px;")
        dividend_layout.addWidget(note_label)

        # 复权选择下拉框
        self.dividend_type_combo = NoWheelComboBox()
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
        
        # 从QSettings读取保存的日期设置
        settings = QSettings('KHQuant', 'StockAnalyzer')
        
        # 设置合理的默认日期：今年年初到今天
        from datetime import datetime
        current_year = datetime.now().year
        today = datetime.now()
        default_start_date = QDate(current_year, 1, 1)
        default_end_date = QDate(today.year, today.month, today.day)
        
        # 读取保存的日期，如果没有则使用默认值
        saved_start_date = settings.value('start_date', default_start_date.toString('yyyy-MM-dd'))
        saved_end_date = settings.value('end_date', default_end_date.toString('yyyy-MM-dd'))
        
        start_date_layout = QHBoxLayout()
        start_date_layout.addWidget(QLabel("起始日期:"))
        self.start_date_edit = NoWheelDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        # 从字符串解析日期
        if isinstance(saved_start_date, str):
            self.start_date_edit.setDate(QDate.fromString(saved_start_date, 'yyyy-MM-dd'))
        else:
            self.start_date_edit.setDate(default_start_date)
        # 添加信号连接，当日期改变时保存设置
        self.start_date_edit.dateChanged.connect(self.save_date_settings)
        start_date_layout.addWidget(self.start_date_edit)
        
        end_date_layout = QHBoxLayout()
        end_date_layout.addWidget(QLabel("结束日期:"))
        self.end_date_edit = NoWheelDateEdit()
        self.end_date_edit.setCalendarPopup(True)
        # 从字符串解析日期
        if isinstance(saved_end_date, str):
            self.end_date_edit.setDate(QDate.fromString(saved_end_date, 'yyyy-MM-dd'))
        else:
            self.end_date_edit.setDate(default_end_date)
        # 添加信号连接，当日期改变时保存设置
        self.end_date_edit.dateChanged.connect(self.save_date_settings)
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
        
        # 从QSettings读取保存的时间设置
        settings = QSettings('KHQuant', 'StockAnalyzer')
        
        # 设置默认值
        default_time_range_mode = 1  # 默认选择"全天"
        default_start_time = QTime(9, 30)
        default_end_time = QTime(15, 0)
        
        # 读取保存的时间设置
        saved_time_range_mode = settings.value('time_range_mode', default_time_range_mode, type=int)
        saved_start_time = settings.value('start_time', default_start_time.toString('HH:mm'))
        saved_end_time = settings.value('end_time', default_end_time.toString('HH:mm'))
        
        # 添加时间范围选择下拉框
        self.use_all_time_checkbox = NoWheelComboBox()
        self.use_all_time_checkbox.addItems(['指定时间段', '全天'])
        self.use_all_time_checkbox.setCurrentIndex(saved_time_range_mode)  # 使用保存的选择
        self.use_all_time_checkbox.currentIndexChanged.connect(self.toggle_time_range)
        self.use_all_time_checkbox.currentIndexChanged.connect(self.save_time_settings)  # 添加保存信号
        time_layout.addWidget(self.use_all_time_checkbox)
        
        # 添加时间选择控件
        time_layout.addWidget(QLabel("开始时间:"))
        self.start_time_edit = NoWheelTimeEdit()
        # 从字符串解析时间
        if isinstance(saved_start_time, str):
            self.start_time_edit.setTime(QTime.fromString(saved_start_time, 'HH:mm'))
        else:
            self.start_time_edit.setTime(default_start_time)
        self.start_time_edit.timeChanged.connect(self.validate_time_range)
        self.start_time_edit.timeChanged.connect(self.save_time_settings)  # 添加保存信号
        time_layout.addWidget(self.start_time_edit)
        
        time_layout.addWidget(QLabel("结束时间:"))
        self.end_time_edit = NoWheelTimeEdit()
        # 从字符串解析时间
        if isinstance(saved_end_time, str):
            self.end_time_edit.setTime(QTime.fromString(saved_end_time, 'HH:mm'))
        else:
            self.end_time_edit.setTime(default_end_time)
        self.end_time_edit.timeChanged.connect(self.validate_time_range)
        self.end_time_edit.timeChanged.connect(self.save_time_settings)  # 添加保存信号
        time_layout.addWidget(self.end_time_edit)
        
        # 根据当前选择设置时间编辑器的启用状态
        use_all_time = (saved_time_range_mode == 1)  # 1 表示选择了"全天"
        self.start_time_edit.setEnabled(not use_all_time)
        self.end_time_edit.setEnabled(not use_all_time)
        
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
        self.download_button = QPushButton("下载数据")
        self.download_button.setMinimumHeight(40)
        self.download_button.clicked.connect(self.download_data)
        button_layout.addWidget(self.download_button)
        
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
            # 如果补充数据线程正在运行，点击按钮就停止补充
            if hasattr(self, 'supplement_thread') and self.supplement_thread and self.supplement_thread.isRunning():
                self.supplement_thread.stop()
                self.status_label.setText("补充数据已停止")
                self.reset_supplement_button()
                return
                
            # 验证日期和时间范围
            if not self.validate_date_range():
                return
            if not self.validate_time_range():
                return
            
            # 清理之前的线程（如果存在）
            if hasattr(self, 'supplement_thread') and self.supplement_thread:
                try:
                    self.supplement_thread.stop()
                    self.supplement_thread.wait()
                except: # noqa: E722
                    pass
            
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

            # 获取复权方式
            dividend_type = self.dividend_type_combo.currentData()
            if dividend_type is None:  # 如果没有设置currentData，则使用currentText
                dividend_type = self.dividend_type_combo.currentText()

            # 更新按钮状态
            self.download_button.setEnabled(False)
            self.progress_bar.setValue(0)
            self.status_label.setText("正在补充数据...")
            
            # 更改补充数据按钮为停止按钮
            if hasattr(self, 'supplement_button'):
                self.supplement_button.setText("停止补充")
                self.supplement_button.setStyleSheet("background-color: #E74C3C;")

            try:
                # 准备参数字典
                params = {
                    'stock_files': stock_files,
                    'field_list': field_list,
                    'period_type': period_type,
                    'start_date': start_date,
                    'end_date': end_date,
                    'time_range': time_range,
                    'dividend_type': dividend_type
                }
                
                # 创建并启动补充数据线程
                self.supplement_thread = SupplementThread(params, self)
                self.supplement_thread.progress.connect(self.update_progress)
                self.supplement_thread.finished.connect(self.supplement_finished)
                self.supplement_thread.error.connect(self.handle_supplement_error)
                self.supplement_thread.status_update.connect(self.update_status)
                self.supplement_thread.start()
                
            except Exception as e:
                QMessageBox.critical(self, "错误", f"启动补充数据线程时出错：{str(e)}")
                logging.error(f"启动补充数据线程时出错: {str(e)}", exc_info=True)
                self.reset_supplement_button()

        except Exception as e:
            QMessageBox.critical(self, "错误", f"准备补充数据时出错：{str(e)}")
            logging.error(f"准备补充数据时出错: {str(e)}", exc_info=True)
            self.reset_supplement_button()

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
        # 使用根据分辨率缩放的样式表
        self.setStyleSheet(self.get_scaled_stylesheet())

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

    def show_help(self):
        """打开在线教程页面"""
        try:
            from PyQt5.QtCore import QUrl
            from PyQt5.QtGui import QDesktopServices
            
            # 打开教程网址
            url = QUrl("https://khsci.com/khQuant/tutorial/") # 使用正确的教程链接
            QDesktopServices.openUrl(url)
            
            logging.info("已打开在线教程页面")
        except Exception as e:
            error_msg = f"打开教程页面失败: {str(e)}"
            logging.error(error_msg)
            QMessageBox.critical(self, "错误", error_msg)

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

    def show(self):
        """重写show方法，默认最大化窗口"""
        try:
            super().show()
            self.showMaximized()  # 默认最大化窗口
            
            # 延迟检查更新
            QTimer.singleShot(2000, self.delayed_update_check)
            
        except Exception as e:
            logging.error(f"显示主窗口时出错: {str(e)}", exc_info=True)
            QMessageBox.critical(self, "错误", f"启动程序时出错: {str(e)}")
            QApplication.quit()

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
        if hasattr(self, 'download_thread') and self.download_thread and self.download_thread.isRunning():
            # 停止下载
            self.download_thread.stop()
            self.status_label.setText("下载已停止")
            self.reset_download_button()
            # 清除线程引用
            self.download_thread = None
        else:
            # 开始下载
            self.download_data()

    def reset_download_button(self):
        """重置下载按钮状态"""
        self.download_button.setText("下载数据")
        self.download_button.setStyleSheet("")
        self.download_button.setEnabled(True)
        if hasattr(self, 'supplement_button'):
            self.supplement_button.setEnabled(True)

    def reset_supplement_button(self):
        """重置补充数据按钮状态"""
        if hasattr(self, 'supplement_button'):
            self.supplement_button.setText("补充数据")
            self.supplement_button.setStyleSheet("")
            self.supplement_button.setEnabled(True)
        self.download_button.setEnabled(True)

    def open_custom_list(self, event):
        """打开自选清单文件"""
        try:
            # 获取自选清单文件路径 - 使用用户可写目录
            custom_file = self.get_custom_list_path()
            
            # 如果文件不存在，创建一个示例文件
            if not os.path.exists(custom_file):
                try:
                    # 确保目录存在
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
                    logging.info(f"已创建示例自选清单文件: {custom_file}")
                except Exception as create_error:
                    QMessageBox.warning(self, "错误", f"创建自选清单文件失败: {str(create_error)}")
                    logging.error(f"创建自选清单文件失败: {str(create_error)}")
                    return
            
            # 打开文件
            if os.path.exists(custom_file):
                if sys.platform == 'win32':
                    os.startfile(custom_file)
                elif sys.platform == 'darwin':  # macOS
                    subprocess.call(['open', custom_file])
                else:  # linux
                    subprocess.call(['xdg-open', custom_file])
                logging.info(f"已打开自选清单文件: {custom_file}")
            else:
                QMessageBox.warning(self, "错误", "找不到自选清单文件")
                
        except Exception as e:
            QMessageBox.warning(self, "错误", f"打开自选清单文件失败: {str(e)}")
            logging.error(f"打开自选清单文件失败: {str(e)}")
    
    def get_custom_list_path(self):
        """获取自选清单文件的路径"""
        if getattr(sys, 'frozen', False):
            # 打包环境 - 使用用户文档目录
            user_docs = os.path.expanduser("~/Documents")
            khquant_dir = os.path.join(user_docs, "KHQuant", "data")
            return os.path.join(khquant_dir, "otheridx.csv")
        else:
            # 开发环境 - 使用原路径
            data_dir = os.path.join(os.path.dirname(__file__), 'data')
            return os.path.join(data_dir, 'otheridx.csv')

    def update_status(self, message):
        """更新状态信息"""
        try:
            # 限制状态更新频率
            import time
            current_time = time.time()
            if not hasattr(self, '_last_status_update_time'):
                self._last_status_update_time = 0
            
            # 只有距离上次更新超过0.5秒或是重要消息才更新
            is_important = any(keyword in str(message) for keyword in ['完成', '失败', '错误', '成功', '开始'])
            if current_time - self._last_status_update_time < 0.5 and not is_important:
                return
                
            self._last_status_update_time = current_time
            
            # 使用status_label显示消息，而不是statusBar
            if hasattr(self, 'status_label'):
                # 设置工具提示，显示完整消息
                self.status_label.setToolTip(message)
                
                # 计算能显示的文字长度（基于字体和宽度估算）
                # 假设每个字符约8像素宽，状态容器最大宽度500px，减去一些边距
                chars_per_line = (500 - 20) // 8  # 约60个字符一行
                max_chars_for_two_lines = chars_per_line * 2
                
                displayed_message = message
                if len(message) > max_chars_for_two_lines:
                    # 如果超过两行的字符数，截断并添加省略号
                    displayed_message = message[:max_chars_for_two_lines-3] + "..."
                
                self.status_label.setText(displayed_message)
                
                # 记录完整消息以便参考
                logging.info(f"状态更新: {message}")
        except Exception as e:
            logging.error(f"更新状态信息时出错: {str(e)}")

    def supplement_finished(self, success, message):
        """处理补充数据线程完成事件"""
        self.reset_supplement_button()
        # 清除线程引用
        self.supplement_thread = None

        if success:
            # 检查是否没有下载到新数据
            if "没有下载到新数据" in message:
                self.status_label.setText("补充数据完成，未发现新数据可供下载。")
            else:
                # 使用标准消息框，自动适配内容
                QMessageBox.information(self, "成功", message)
                self.status_label.setText(message)  # 成功时也更新底部状态栏
        else:
            # 使用标准错误消息框
            QMessageBox.critical(self, "错误", f"补充数据过程中发生错误: {message}")
            self.status_label.setText(f"补充数据错误: {message}") # 错误时也更新底部状态栏

        self.progress_bar.setValue(0)

    def handle_supplement_error(self, error_msg):
        """处理补充数据线程错误事件"""
        logging.error(f"补充数据错误: {error_msg}")
        QMessageBox.critical(self, "补充数据错误", error_msg)
        self.reset_supplement_button()
        # 清除线程引用
        self.supplement_thread = None
        self.status_label.setText("补充数据失败")

    def save_date_settings(self):
        """保存日期设置到QSettings"""
        try:
            settings = QSettings('KHQuant', 'StockAnalyzer')
            settings.setValue('start_date', self.start_date_edit.date().toString('yyyy-MM-dd'))
            settings.setValue('end_date', self.end_date_edit.date().toString('yyyy-MM-dd'))
            settings.sync()
            logging.debug("日期设置已保存到QSettings")
        except Exception as e:
            logging.error(f"保存日期设置时出错: {str(e)}")

    def save_time_settings(self):
        """保存时间设置到QSettings"""
        try:
            settings = QSettings('KHQuant', 'StockAnalyzer')
            settings.setValue('start_time', self.start_time_edit.time().toString('HH:mm'))
            settings.setValue('end_time', self.end_time_edit.time().toString('HH:mm'))
            settings.setValue('time_range_mode', self.use_all_time_checkbox.currentIndex())
            settings.sync()
            logging.debug("时间设置已保存到QSettings")
        except Exception as e:
            logging.error(f"保存时间设置时出错: {str(e)}")

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
        # 使用主屏幕（而非鼠标所在屏幕）
        screen = QApplication.desktop().primaryScreen()
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
    
    # 支持多进程
    multiprocessing.freeze_support()
    
    # 日志配置部分
    log_filename = setup_logging()
    
    try:
        logging.info("程序启动")
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
                logging.info(f"已设置应用字体: {default_font.family()}")
            else:
                # 使用系统默认字体
                default_font = QFont()
                default_font.setPointSize(9)
                app.setFont(default_font)
                logging.info("使用系统默认字体")
            
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
            
            logging.info("已设置中文本地化")
            
        except Exception as e:
            logging.error(f"设置字体和本地化时出错: {str(e)}")
        
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
            # 直接创建主窗口，不显示启动画面
            main_window = StockDataProcessorGUI()
            logging.info("主窗口创建成功")

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

            # 使用短延时确保窗口准备好后再显示
            QTimer.singleShot(100, show_main_window)
            
            # 设置定时检查
            status_timer = QTimer()
            status_timer.timeout.connect(main_window.check_software_status)
            status_timer.start(5000)
            
            # 只执行软件状态检查，不执行更新检查
            QTimer.singleShot(200, main_window.check_software_status)
            
            logging.info("开始事件循环")
            return_code = app.exec_()
            logging.info(f"程序退出，返回码: {return_code}")
            sys.exit(return_code)
            
        except Exception as e:
            logging.error(f"初始化过程中出错: {str(e)}", exc_info=True)
            if main_window:
                main_window.close()
            QMessageBox.critical(None, "初始化错误", 
                               f"程序初始化过程中出错:\n{str(e)}\n\n详细信息已写入日志文件")
            sys.exit(1)
        
    except Exception as e:
        logging.critical(f"程序异常退出: {str(e)}", exc_info=True)
        print(f"程序发生严重错误，详细信息已写入日志文件: {log_filename}")
        sys.exit(1)

