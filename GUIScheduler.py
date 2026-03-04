import sys
import os
import logging
import multiprocessing
from datetime import datetime, time
from queue import Empty
import time as time_module
from PyQt5.QtWidgets import (QApplication, QMainWindow, QHBoxLayout, QVBoxLayout, 
                             QWidget, QGroupBox, QCheckBox, QGridLayout, QTextEdit, 
                             QPushButton, QStatusBar, QProgressBar, QLabel, QTimeEdit,
                             QMessageBox, QDesktopWidget, QSplitter, QComboBox, QFileDialog,
                             QSizePolicy)
from PyQt5.QtCore import Qt, QSettings, QThread, pyqtSignal, QTime, QTimer, QMutex
from PyQt5.QtGui import QIcon, QFont, QColor
import schedule
from khQTTools import KhQuTools


def supplement_data_worker(params, progress_queue, result_queue, stop_event):
    """
    数据补充工作进程函数
    在独立进程中运行，避免GIL限制
    """
    # 多进程保护 - 防止在子进程中启动GUI
    if __name__ != '__main__':
        # 在子进程中，确保不会执行主程序代码
        import multiprocessing
        multiprocessing.current_process().name = 'SchedulerSupplementWorker'
    
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
        
        # 配置子进程的日志
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        
        # 进度和状态更新的时间控制
        last_progress_time = 0
        last_status_time = 0
        update_interval = 0.5  # 500毫秒
        
        def progress_callback(percent):
            nonlocal last_progress_time
            current_time = time.time()
            # 降低进度更新频率限制，确保重要进度更新不被过滤
            if current_time - last_progress_time >= 0.3 or percent >= 100 or percent % 10 == 0:  # 每10%或最终完成时一定发送
                try:
                    progress_queue.put(('progress', percent), timeout=1)
                    last_progress_time = current_time
                    print(f"[定时补充进程] 发送进度: {percent}%")
                except Exception as e:
                    print(f"[定时补充进程] 发送进度失败: {e}")
        
        def log_callback(message):
            nonlocal last_status_time
            current_time = time.time()
            
            # 检查是否是补充数据的重要消息（成功、失败、错误等）
            is_important = any(keyword in str(message) for keyword in [
                '开始', '完成', '失败', '错误', '中断', 
                '补充', '数据成功', '数据时出错', '数据为空'
            ])
            
            if is_important or current_time - last_status_time >= update_interval:
                try:
                    progress_queue.put(('status', str(message)), timeout=1)
                    last_status_time = current_time
                    print(f"[定时补充进程] 发送状态: {message}")
                except Exception as e:
                    print(f"[定时补充进程] 发送状态失败: {e}")
        
        def check_interrupt():
            # 检查停止事件
            return stop_event.is_set()
        
        # 执行数据补充
        supplement_history_data(
            stock_files=params['stock_files'],
            field_list=["open", "high", "low", "close", "volume", "amount"],
            period_type=params['period_type'],
            start_date=params['start_date'],
            end_date=params['end_date'],
            time_range='all',
            dividend_type='none',
            progress_callback=progress_callback,
            log_callback=log_callback,
            check_interrupt=check_interrupt
        )
        
        print(f"[定时补充进程] 数据补充函数执行完成")
        
        # 发送完成信号
        result_queue.put(('success', '定时数据补充完成！'))
        
    except Exception as e:
        error_msg = f"定时补充数据过程中发生错误: {str(e)}"
        result_queue.put(('error', error_msg))
        logging.error(error_msg, exc_info=True)


class ScheduledSupplementThread(QThread):
    """定时数据补充线程（使用多进程后端）"""
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
                                self.progress.emit(data)
                            elif msg_type == 'status':
                                self.status_update.emit(data)
                        except Empty:
                            break
                    
                    # 检查结果
                    try:
                        result_type, message = self.result_queue.get_nowait()
                        # 任务完成，设置运行状态为 False
                        self.mutex.lock()
                        self.running = False
                        self.mutex.unlock()
                        
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
                    # 进程异常退出，设置运行状态为 False
                    self.mutex.lock()
                    self.running = False
                    self.mutex.unlock()
                    self.error.emit(f"定时补充进程异常退出，退出码: {exit_code}")
                
        except Exception as e:
            error_msg = f"启动定时补充进程时发生错误: {str(e)}"
            logging.error(error_msg, exc_info=True)
            if self.isRunning():
                # 发生异常，设置运行状态为 False
                self.mutex.lock()
                self.running = False
                self.mutex.unlock()
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
            logging.error(f"停止定时补充进程时出错: {str(e)}")
            
        self.mutex.unlock()
        
    def isRunning(self):
        self.mutex.lock()
        result = self.running
        self.mutex.unlock()
        return result


class GUIScheduler(QMainWindow):
    """定时数据调度器"""
    
    def __init__(self):
        super().__init__()
        
        # 检测屏幕分辨率并设置字体缩放
        self.font_scale = self.detect_screen_resolution()
        
        # 初始化状态变量
        self.is_scheduled_running = False
        self.supplement_thread = None
        self.schedule_timer = QTimer()
        self.schedule_timer.timeout.connect(self.check_schedule)
        
        # 初始化xtdata工具和股票名称缓存
        from khQTTools import KhQuTools
        self.tools = KhQuTools()
        self.stock_names_cache = {}
        
        # 初始化自定义文件列表
        self.custom_files = []
        
        self.initUI()
        self.load_stock_names()
        # 在界面完全构建后，清理局部字体并统一应用缩放样式
        QTimer.singleShot(0, self.apply_scaled_styles)
    
    def detect_screen_resolution(self):
        """检测屏幕分辨率并返回字体缩放比例"""
        screen = QDesktopWidget().screenGeometry()
        width = screen.width()
        height = screen.height()
        
        # 根据屏幕宽度确定字体缩放比例（与主界面保持一致）
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
                background-color: #2b2b2b;
                color: #e8e8e8;
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
            
            /* 复选框样式 */
            QCheckBox {{
                color: #e8e8e8;
                spacing: 5px;
                font-size: {scaled_sizes['normal']}px;
                background-color: transparent;
            }}
            
            /* 时间编辑器样式 */
            QTimeEdit {{
                background-color: #404040;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                padding: 5px;
                color: #e8e8e8;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            /* 文本编辑器样式 */
            QTextEdit {{
                background-color: #2b2b2b;
                color: #e8e8e8;
                border: 1px solid #404040;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            /* 进度条样式 */
            QProgressBar {{
                background-color: #404040;
                border: 1px solid #4d4d4d;
                border-radius: 5px;
                text-align: center;
                font-size: {scaled_sizes['normal']}px;
            }}
            
            /* 状态栏样式 */
            QStatusBar {{
                background-color: #333333;
                color: #e8e8e8;
                border-top: 1px solid #4d4d4d;
                font-size: {scaled_sizes['normal']}px;
            }}
        """

    def get_complete_scaled_stylesheet(self):
        """获取包含强制覆盖的完整缩放样式表"""
        # 计算缩放后的字体大小和控件尺寸
        font_14 = int(14 * self.font_scale)
        font_16 = int(16 * self.font_scale)
        font_12 = int(12 * self.font_scale)
        
        # 计算控件高度（基于字体大小）
        button_height = max(30, int(30 * self.font_scale))
        input_height = max(25, int(25 * self.font_scale))
        combo_height = max(25, int(25 * self.font_scale))
        
        # 基础样式表
        base_style = self.get_scaled_stylesheet()
        
        # 强制覆盖样式，包含高度调整
        override_style = f"""
            /* 强制字体大小覆盖 */
            * {{ font-size: {font_14}px !important; }}
            QLabel {{ font-size: {font_14}px !important; min-height: {input_height}px; }}
            QPushButton {{ 
                font-size: {font_14}px !important; 
                min-height: {button_height}px;
                padding: {max(4, int(4 * self.font_scale))}px {max(8, int(8 * self.font_scale))}px;
            }}
            /* 复选框：同步主界面的完整样式，确保间距和尺寸正确缩放 */
            QCheckBox {{
                spacing: {max(5, int(5 * self.font_scale))}px;
                padding: {max(5, int(5 * self.font_scale))}px 0;
            }}
            QCheckBox::indicator {{
                width: {max(20, int(20 * self.font_scale))}px;
                height: {max(20, int(20 * self.font_scale))}px;
            }}
            QComboBox {{ 
                font-size: {font_14}px !important; 
                min-height: {combo_height}px;
                padding: {max(3, int(3 * self.font_scale))}px;
            }}
            QLineEdit {{ 
                font-size: {font_14}px !important; 
                min-height: {input_height}px;
                padding: {max(3, int(3 * self.font_scale))}px;
            }}
            QGroupBox {{ 
                font-size: {font_14}px !important; 
                padding-top: {max(15, int(15 * self.font_scale))}px;
            }}
            QGroupBox::title {{ 
                font-size: {font_14}px !important; 
                padding: {max(3, int(3 * self.font_scale))}px {max(5, int(5 * self.font_scale))}px;
                background-color: transparent; /* 与主背景一致，消除标题底色块 */
                subcontrol-origin: margin;
                subcontrol-position: top left;
            }}
            QTimeEdit {{ 
                font-size: {font_14}px !important; 
                min-height: {combo_height}px;
                padding: {max(3, int(3 * self.font_scale))}px;
            }}
            QTextEdit {{ font-size: {font_14}px !important; }}
            QProgressBar {{ 
                font-size: {font_14}px !important; 
                min-height: {max(16, int(16 * self.font_scale))}px;
            }}
            QStatusBar {{ font-size: {font_14}px !important; }}
            QCheckBox {{ 
                font-size: {font_14}px !important; 
                min-height: {max(16, int(16 * self.font_scale))}px;
            }}
            QSpinBox, QDoubleSpinBox {{ 
                font-size: {font_14}px !important; 
                min-height: {input_height}px;
                padding: {max(3, int(3 * self.font_scale))}px;
            }}
        """
        
        return base_style + override_style

    def apply_scaled_styles(self):
        """在界面构建完成后，统一清理并应用缩放样式，避免被局部样式覆盖"""
        # 1) 递归移除所有子控件样式中的 font-size
        try:
            from PyQt5.QtWidgets import QWidget
            import re
            for child in self.findChildren(QWidget):
                if hasattr(child, 'styleSheet'):
                    ss = child.styleSheet() or ''
                    if 'font-size' in ss:
                        ss = re.sub(r"font-size\s*:\s*\d+px;?", "", ss)
                        child.setStyleSheet(ss)
        except Exception:
            pass
        # 2) 重新应用完整缩放样式表
        self.setStyleSheet(self.get_complete_scaled_stylesheet())

    def get_icon_path(self, icon_name):
        """获取图标文件的正确路径"""
        if getattr(sys, 'frozen', False):
            # 打包环境 - 使用sys._MEIPASS
            if hasattr(sys, '_MEIPASS'):
                return os.path.join(sys._MEIPASS, 'icons', icon_name)
            else:
                # 备用路径
                return os.path.join(os.path.dirname(sys.executable), 'icons', icon_name)
        else:
            # 开发环境
            return os.path.join(os.path.dirname(__file__), 'icons', icon_name)
    
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
        
        self.setWindowTitle("定时数据补充工具 - 看海量化交易系统")
        self.setGeometry(100, 100, 1200, 700)
        
        # 将窗口居中显示
        self.center_main_window()
        
        # 设置窗口图标
        icon_file = self.get_icon_path('stock_icon.ico')
        if os.path.exists(icon_file):
            self.setWindowIcon(QIcon(icon_file))
        else:
            # 尝试png格式
            icon_file_png = self.get_icon_path('stock_icon.png')
            if os.path.exists(icon_file_png):
                self.setWindowIcon(QIcon(icon_file_png))
            else:
                logging.warning(f"图标文件不存在: {icon_file} 和 {icon_file_png}")
        
        # 在界面创建前就设置完整的样式表，避免闪烁
        self.setStyleSheet(self.get_complete_scaled_stylesheet())
        
        # 创建中心部件
        central_widget = QWidget()
        central_widget.setObjectName("centralWidget")
        self.setCentralWidget(central_widget)
        
        # 创建主布局
        main_layout = QHBoxLayout(central_widget)
        
        # 创建分割器
        splitter = QSplitter(Qt.Horizontal)
        splitter.setStyleSheet("background-color: #2b2b2b;")
        
        # 创建左侧配置区域
        config_widget = self.create_config_widget()
        config_widget.setFixedWidth(480)
        
        # 创建右侧日志和控制区域
        log_widget = self.create_log_widget()
        
        # 添加到分割器
        splitter.addWidget(config_widget)
        splitter.addWidget(log_widget)
        splitter.setStretchFactor(0, 0)  # 左侧配置区固定宽度
        splitter.setStretchFactor(1, 1)  # 右侧日志区可伸缩
        
        main_layout.addWidget(splitter)
        
        # 创建状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        
        # 创建进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)
        
        # 重新应用完整缩放样式表（确保不被覆盖）
        self.setStyleSheet(self.get_complete_scaled_stylesheet())
        
        # 原有样式注释掉，已整合到完整样式表中
        """
        # 注释掉原有的硬编码样式，避免覆盖缩放样式表
        self.setStyleSheet('''
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
        ''')
        """
    
    def create_config_widget(self):
        """创建配置区域"""
        config_widget = QWidget()
        config_widget.setStyleSheet("background-color: #2b2b2b;")
        
        # 主布局
        main_layout = QVBoxLayout(config_widget)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)
        
        # 标题
        title_label = QLabel("定时数据补充配置")
        title_font = QFont()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setStyleSheet("color: #e8e8e8; margin-bottom: 10px;")
        main_layout.addWidget(title_label)
        
        # 添加各个配置组
        self.add_stock_pool_group(main_layout)
        self.add_period_group(main_layout)
        self.add_schedule_group(main_layout)
        self.add_control_group(main_layout)
        
        # 添加弹性空间
        main_layout.addStretch()
        
        return config_widget
    
    def add_stock_pool_group(self, layout):
        """添加股票池选择组"""
        stock_group = QGroupBox("股票池选择")
        stock_group.setStyleSheet("""
            QGroupBox {
                color: #e8e8e8;
                font-weight: bold;
                font-size: 14px;
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
        
        # 股票池复选框
        self.stock_pool_checkboxes = {}
        stock_pools = {
            'hs_a': '沪深A股',
            'gem': '创业板',
            'sci': '科创板',
            'zz500': '中证500成分股',
            'hs300': '沪深300成分股',
            'sz50': '上证50成分股',
            'indices': '常用指数',
            'convertible_bonds': '沪深转债',
            'custom': '自定义股票池'
        }
        
        # 创建网格布局
        grid_layout = QGridLayout()
        grid_layout.setHorizontalSpacing(24)
        grid_layout.setVerticalSpacing(10)
        row = 0
        col = 0
        
        for pool_type, display_name in stock_pools.items():
            if pool_type == 'custom':
                # 为自定义股票池创建特殊的标签和复选框布局
                custom_widget = QWidget()
                custom_layout = QHBoxLayout(custom_widget)
                custom_layout.setContentsMargins(0, 0, 0, 0)
                custom_layout.setSpacing(8)
                
                # 复选框
                checkbox = QCheckBox()
                checkbox.setStyleSheet("color: #e8e8e8; font-size: 14px; padding: 5px;")
                checkbox.stateChanged.connect(self.on_stock_pool_changed)
                self.stock_pool_checkboxes[pool_type] = checkbox
                custom_layout.addWidget(checkbox)
                
                # 创建可点击的标签
                custom_label = QLabel(display_name)
                custom_label.setStyleSheet("""
                    QLabel {
                        color: #e8e8e8;
                        text-decoration: underline;
                        font-size: 14px;
                        padding: 5px;
                    }
                """)
                custom_label.setCursor(Qt.PointingHandCursor)
                custom_label.mousePressEvent = self.open_custom_pool
                custom_layout.addWidget(custom_label)
                
                custom_layout.addStretch()
                
                # 将自定义股票池放在新的一行
                if col != 0:
                    row += 1
                    col = 0
                grid_layout.addWidget(custom_widget, row, col, 1, 2)  # 跨越2列
                row += 1
                col = 0
            else:
                checkbox = QCheckBox(display_name)
                checkbox.setStyleSheet("color: #e8e8e8; padding: 5px;")
                # 给复选框一些最小宽度，避免长文本换行或挤压
                checkbox.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
                checkbox.stateChanged.connect(self.on_stock_pool_changed)
                self.stock_pool_checkboxes[pool_type] = checkbox
                grid_layout.addWidget(checkbox, row, col)
                
                col += 1
                if col >= 1:  # 改为1列布局，彻底解决挤压问题
                    col = 0
                    row += 1
        
        stock_layout.addLayout(grid_layout)
        
        # 添加自定义文件管理按钮
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)
        
        add_custom_button = QPushButton("添加自定义列表")
        add_custom_button.setMinimumHeight(35)
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
        
        clear_button = QPushButton("清空选择")
        clear_button.setMinimumHeight(35)
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
        clear_button.clicked.connect(self.clear_stock_pools)
        button_layout.addWidget(clear_button)
        
        stock_layout.addLayout(button_layout)
        
        # 添加预览区域
        self.stock_files_preview = QTextEdit()
        self.stock_files_preview.setMaximumHeight(80)
        self.stock_files_preview.setMinimumHeight(60)
        self.stock_files_preview.setReadOnly(True)
        self.stock_files_preview.setText("未选择任何股票池")
        self.stock_files_preview.setStyleSheet("""
            QTextEdit {
                background-color: #2b2b2b;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 3px;
                padding: 8px;
                font-size: 12px;
            }
        """)
        stock_layout.addWidget(self.stock_files_preview)
        
        # 股票池统计标签
        self.stock_pool_label = QLabel("未选择任何股票池")
        self.stock_pool_label.setStyleSheet("color: #b0b0b0; font-size: 13px; padding: 5px;")
        stock_layout.addWidget(self.stock_pool_label)
        
        stock_group.setLayout(stock_layout)
        layout.addWidget(stock_group)
    
    def add_period_group(self, layout):
        """添加周期类型组"""
        period_group = QGroupBox("周期类型选择")
        period_group.setStyleSheet("""
            QGroupBox {
                color: #e8e8e8;
                font-weight: bold;
                font-size: 14px;
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
        
        period_layout = QVBoxLayout()
        
        # 周期类型复选框
        self.period_checkboxes = {}
        periods = {
            'tick': 'Tick数据',
            '1m': '1分钟线',
            '5m': '5分钟线',
            '1d': '日线'
        }
        
        # 将水平布局改为2x2的网格布局
        grid_layout = QGridLayout()
        grid_layout.setHorizontalSpacing(int(24 * self.font_scale))
        grid_layout.setVerticalSpacing(int(10 * self.font_scale))
        
        checkboxes = []
        for period_type, display_name in periods.items():
            checkbox = QCheckBox(display_name)
            checkbox.stateChanged.connect(self.on_period_changed)
            self.period_checkboxes[period_type] = checkbox
            checkboxes.append(checkbox)
            
        # 手动将复选框添加到网格中
        grid_layout.addWidget(checkboxes[0], 0, 0) # Tick
        grid_layout.addWidget(checkboxes[1], 0, 1) # 1分钟
        grid_layout.addWidget(checkboxes[2], 1, 0) # 5分钟
        grid_layout.addWidget(checkboxes[3], 1, 1) # 日线

        period_layout.addLayout(grid_layout)
        period_group.setLayout(period_layout)
        layout.addWidget(period_group)
    
    def add_schedule_group(self, layout):
        """添加定时设置组"""
        schedule_group = QGroupBox("定时设置")
        schedule_group.setStyleSheet("""
            QGroupBox {
                color: #e8e8e8;
                font-weight: bold;
                font-size: 14px;
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
        
        schedule_layout = QVBoxLayout()
        
        # 补充时间设置
        time_layout = QHBoxLayout()
        time_label = QLabel("每日补充时间:")
        time_label.setStyleSheet("color: #e8e8e8; font-size: 14px;")
        time_layout.addWidget(time_label)
        
        self.supplement_time_edit = QTimeEdit()
        self.supplement_time_edit.setTime(QTime(15, 30))  # 默认下午3点30分
        self.supplement_time_edit.setDisplayFormat("HH:mm")
        self.supplement_time_edit.setMinimumHeight(35)
        self.supplement_time_edit.setStyleSheet("""
            QTimeEdit {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 3px;
                padding: 6px;
                font-size: 14px;
            }
        """)
        time_layout.addWidget(self.supplement_time_edit)
        time_layout.addStretch()
        
        schedule_layout.addLayout(time_layout)
        
        schedule_group.setLayout(schedule_layout)
        layout.addWidget(schedule_group)
    
    def add_control_group(self, layout):
        """添加控制按钮组"""
        control_layout = QVBoxLayout()
        
        # 开始/停止按钮
        self.control_button = QPushButton("开始定时补充")
        self.control_button.setMinimumHeight(40)
        self.control_button.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: #ffffff;
                border: none;
                border-radius: 5px;
                padding: 10px;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #218838;
            }
            QPushButton:pressed {
                background-color: #1e7e34;
            }
        """)
        self.control_button.clicked.connect(self.toggle_scheduled_supplement)
        control_layout.addWidget(self.control_button)
        
        # 立即执行按钮
        self.immediate_button = QPushButton("立即执行一次")
        self.immediate_button.setMinimumHeight(35)
        self.immediate_button.setStyleSheet("""
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
        self.immediate_button.clicked.connect(self.execute_immediate)
        control_layout.addWidget(self.immediate_button)
        

        
        layout.addLayout(control_layout)
    
    def create_log_widget(self):
        """创建日志和状态区域"""
        log_widget = QWidget()
        log_widget.setStyleSheet("background-color: #2b2b2b;")
        
        layout = QVBoxLayout(log_widget)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # 标题
        title_label = QLabel("运行日志")
        title_font = QFont()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setStyleSheet("color: #e8e8e8; margin-bottom: 10px;")
        layout.addWidget(title_label)
        
        # 状态信息
        status_layout = QHBoxLayout()
        
        self.status_label = QLabel("状态: 未启动")
        self.status_label.setStyleSheet("color: #e8e8e8; font-size: 14px; font-weight: bold;")
        status_layout.addWidget(self.status_label)
        
        self.next_run_label = QLabel("下次运行: --")
        self.next_run_label.setStyleSheet("color: #b0b0b0; font-size: 14px;")
        status_layout.addWidget(self.next_run_label)
        
        status_layout.addStretch()
        layout.addLayout(status_layout)
        
        # 日志显示区
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #e8e8e8;
                border: 1px solid #404040;
                border-radius: 5px;
                padding: 10px;
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 13px;
                line-height: 1.4;
            }
        """)
        layout.addWidget(self.log_text)
        
        # 清空日志按钮
        clear_button = QPushButton("清空日志")
        clear_button.setMinimumHeight(35)
        clear_button.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: #ffffff;
                border: none;
                border-radius: 3px;
                padding: 8px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #5a6268;
            }
            QPushButton:pressed {
                background-color: #545b62;
            }
        """)
        clear_button.clicked.connect(self.clear_log)
        layout.addWidget(clear_button)
        
        return log_widget
    
    def load_stock_names(self):
        """加载股票名称映射"""
        try:
            # 加载全部股票列表
            stock_list_file = os.path.join(os.path.dirname(__file__), 'data', '全部股票_股票列表.csv')
            if os.path.exists(stock_list_file):
                with open(stock_list_file, 'r', encoding='utf-8-sig') as f:
                    is_first_line = True
                    for line in f:
                        if line.strip():
                            # 跳过第一行表头（如果包含"股票代码"或"代码"等关键词）
                            if is_first_line:
                                is_first_line = False
                                if any(keyword in line for keyword in ['股票代码', '代码', 'code', 'Code', '股票名称', '名称']):
                                    continue  # 跳过表头行
                            
                            parts = line.strip().split(',')
                            if len(parts) >= 2:
                                code = parts[0].strip()
                                name = parts[1].strip()
                                self.stock_names_cache[code] = name
        except Exception as e:
            logging.warning(f"加载股票名称失败: {e}")
    
    def on_stock_pool_changed(self):
        """股票池选择变化处理"""
        selected_pools = []
        for pool_type, checkbox in self.stock_pool_checkboxes.items():
            if checkbox.isChecked():
                if pool_type == 'custom':
                    selected_pools.append('自定义股票池')
                else:
                    selected_pools.append(checkbox.text())
        
        if selected_pools:
            self.stock_pool_label.setText(f"已选择: {', '.join(selected_pools)}")
        else:
            self.stock_pool_label.setText("未选择任何股票池")
        
        # 更新文件预览
        self.update_stock_files_preview()
    
    def update_stock_files_preview(self):
        """更新选中股票池的文件预览"""
        try:
            data_dir = os.path.join(os.path.dirname(__file__), 'data')
            selected_files = []
            
            # 添加预定义列表文件
            for pool_type, checkbox in self.stock_pool_checkboxes.items():
                if checkbox.isChecked():
                    filename = None
                    if pool_type == 'indices':
                        filename = os.path.join(data_dir, "指数_股票列表.csv")
                    elif pool_type == 'zz500':
                        filename = os.path.join(data_dir, "中证500成分股_股票列表.csv")
                    elif pool_type == 'hs300':
                        filename = os.path.join(data_dir, "沪深300成分股_股票列表.csv")
                    elif pool_type == 'sz50':
                        filename = os.path.join(data_dir, "上证50成分股_股票列表.csv")
                    elif pool_type == 'hs_a':
                        filename = os.path.join(data_dir, "沪深A股_股票列表.csv")
                    elif pool_type == 'gem':
                        filename = os.path.join(data_dir, "创业板_股票列表.csv")
                    elif pool_type == 'sci':
                        filename = os.path.join(data_dir, "科创板_股票列表.csv")
                    elif pool_type == 'convertible_bonds':
                        filename = os.path.join(data_dir, "沪深转债_列表.csv")
                    elif pool_type == 'custom':
                        # 处理自定义股票池
                        # 1. 添加默认自定义文件
                        custom_file = self.get_custom_pool_path()
                        if os.path.exists(custom_file):
                            selected_files.append(custom_file)
                        
                        # 2. 添加额外的自定义文件
                        if hasattr(self, 'custom_files') and self.custom_files:
                            for custom_file in self.custom_files:
                                if os.path.exists(custom_file) and custom_file not in selected_files:
                                    selected_files.append(custom_file)
                        continue  # 跳过下面的filename处理
                    
                    if filename and os.path.exists(filename):
                        selected_files.append(filename)
            
            if selected_files:
                # 显示文件名而不是完整路径
                file_names = []
                for file_path in selected_files:
                    if "沪深A股" in file_path:
                        file_names.append("沪深A股_股票列表.csv")
                    elif "创业板" in file_path:
                        file_names.append("创业板_股票列表.csv")
                    elif "科创板" in file_path:
                        file_names.append("科创板_股票列表.csv")
                    elif "中证500" in file_path:
                        file_names.append("中证500成分股_股票列表.csv")
                    elif "沪深300" in file_path:
                        file_names.append("沪深300成分股_股票列表.csv")
                    elif "上证50" in file_path:
                        file_names.append("上证50成分股_股票列表.csv")
                    elif "指数" in file_path:
                        file_names.append("指数_股票列表.csv")
                    elif "otheridx.csv" in file_path:
                        file_names.append("自定义股票池.csv")
                    else:
                        file_names.append(os.path.basename(file_path))
                
                self.stock_files_preview.setText('\n'.join(file_names))
            else:
                self.stock_files_preview.setText("未选择任何股票池")
                
        except Exception as e:
            logging.error(f"更新股票池文件预览时出错: {str(e)}")
    
    def get_custom_pool_path(self):
        """获取自定义股票池文件的路径"""
        data_dir = os.path.join(os.path.dirname(__file__), 'data')
        return os.path.join(data_dir, "otheridx.csv")
    
    def open_custom_pool(self, event):
        """打开自定义股票池文件"""
        try:
            # 获取自定义股票池文件路径
            custom_file = self.get_custom_pool_path()
            
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
                
                self.add_log(f"已打开自定义股票池文件: {custom_file}")
                logging.info(f"已打开自定义股票池文件: {custom_file}")
            else:
                # 如果文件不存在，创建一个示例文件
                try:
                    os.makedirs(os.path.dirname(custom_file), exist_ok=True)
                    
                    # 创建示例自定义股票池文件
                    sample_content = """股票代码,股票名称
000001.SZ,平安银行
000002.SZ,万科A
600000.SH,浦发银行
600036.SH,招商银行
000858.SZ,五粮液
600519.SH,贵州茅台
000671.SZ,阳光城
002415.SZ,海康威视
300014.SZ,亿纬锂能
688111.SH,金山办公"""
                    
                    with open(custom_file, 'w', encoding='utf-8') as f:
                        f.write(sample_content)
                    
                    # 打开新创建的文件
                    if platform.system() == 'Windows':
                        os.startfile(custom_file)
                    elif platform.system() == 'Darwin':  # macOS
                        subprocess.run(['open', custom_file])
                    else:  # Linux
                        subprocess.run(['xdg-open', custom_file])
                    
                    self.add_log(f"已创建并打开新的自定义股票池文件: {custom_file}")
                    logging.info(f"已创建并打开新的自定义股票池文件: {custom_file}")
                except Exception as create_error:
                    QMessageBox.warning(self, "错误", f"创建自定义股票池文件失败: {str(create_error)}")
                    logging.error(f"创建自定义股票池文件失败: {str(create_error)}")
        except Exception as e:
            QMessageBox.warning(self, "错误", f"打开自定义股票池文件失败: {str(e)}")
            logging.error(f"打开自定义股票池文件失败: {str(e)}")
    
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
                # 初始化custom_files列表
                if not hasattr(self, 'custom_files'):
                    self.custom_files = []
                
                for file in files:
                    if file not in self.custom_files:
                        self.custom_files.append(file)
                        self.add_log(f"已添加自定义股票文件: {os.path.basename(file)}")
                
                # 自动勾选自定义股票池选项
                if self.custom_files:
                    self.stock_pool_checkboxes['custom'].setChecked(True)
                
                # 更新预览
                self.update_stock_files_preview()
                
        except Exception as e:
            QMessageBox.warning(self, "错误", f"添加文件时出错: {str(e)}")
            logging.error(f"添加自定义股票文件时出错: {str(e)}")
    
    def clear_stock_pools(self):
        """清空股票池选择"""
        try:
            # 取消所有复选框
            for checkbox in self.stock_pool_checkboxes.values():
                checkbox.setChecked(False)
            
            # 清空自定义文件列表
            self.custom_files = []
            
            # 更新显示
            self.stock_pool_label.setText("未选择任何股票池")
            self.stock_files_preview.setText("未选择任何股票池")
            
            self.add_log("已清空所有股票池选择")
            
        except Exception as e:
            QMessageBox.warning(self, "错误", f"清空股票池时出错: {str(e)}")
            logging.error(f"清空股票池时出错: {str(e)}")
    
    def on_period_changed(self):
        """周期类型选择变化处理"""
        # 这里可以添加周期选择的相关逻辑
        pass
    
    def toggle_scheduled_supplement(self):
        """切换定时补充状态"""
        if not self.is_scheduled_running:
            self.start_scheduled_supplement()
        else:
            self.stop_scheduled_supplement()
    
    def start_scheduled_supplement(self):
        """开始定时补充"""
        # 验证配置
        if not self.validate_config():
            return
        
        self.is_scheduled_running = True
        self.control_button.setText("停止定时补充")
        self.control_button.setStyleSheet("""
            QPushButton {
                background-color: #dc3545;
                color: #ffffff;
                border: none;
                border-radius: 5px;
                padding: 10px;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #c82333;
            }
            QPushButton:pressed {
                background-color: #bd2130;
            }
        """)
        
        # 设置定时任务
        self.setup_schedule()
        
        # 启动定时器
        self.schedule_timer.start(1000)  # 每秒检查一次
        
        self.status_label.setText("状态: 运行中")
        self.add_log("定时补充任务已启动")
        self.update_next_run_time()
    
    def stop_scheduled_supplement(self):
        """停止定时补充"""
        self.is_scheduled_running = False
        self.control_button.setText("开始定时补充")
        self.control_button.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: #ffffff;
                border: none;
                border-radius: 5px;
                padding: 10px;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #218838;
            }
            QPushButton:pressed {
                background-color: #1e7e34;
            }
        """)
        
        # 停止定时器
        self.schedule_timer.stop()
        
        # 清空定时任务
        schedule.clear()
        
        # 停止正在运行的补充任务
        if self.supplement_thread and self.supplement_thread.isRunning():
            self.supplement_thread.stop()
        
        self.status_label.setText("状态: 已停止")
        self.next_run_label.setText("下次运行: --")
        self.add_log("定时补充任务已停止")
    
    def validate_config(self):
        """验证配置"""
        # 检查股票池选择
        selected_pools = [checkbox for checkbox in self.stock_pool_checkboxes.values() if checkbox.isChecked()]
        if not selected_pools:
            QMessageBox.warning(self, "配置错误", "请至少选择一个股票池！")
            return False
        
        # 检查周期类型选择
        selected_periods = [checkbox for checkbox in self.period_checkboxes.values() if checkbox.isChecked()]
        if not selected_periods:
            QMessageBox.warning(self, "配置错误", "请至少选择一个周期类型！")
            return False
        
        return True
    
    def setup_schedule(self):
        """设置定时任务"""
        schedule.clear()
        
        supplement_time = self.supplement_time_edit.time()
        time_str = supplement_time.toString("HH:mm")
        
        # 只在工作日运行，并在执行时检查是否为交易日
        for day in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']:
            getattr(schedule.every(), day).at(time_str).do(self.check_and_execute_if_trading_day)
        
        self.add_log(f"已设置定时任务: 仅交易日 {time_str}")
    
    def check_and_execute_if_trading_day(self):
        """检查是否为交易日，如果是则执行补充"""
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        if self.tools.is_trade_day(today_str):
            self.add_log(f"今日 {today_str} 为交易日，开始执行数据补充")
            self.execute_supplement()
        else:
            self.add_log(f"今日 {today_str} 非交易日，跳过数据补充")
    
    def check_schedule(self):
        """检查定时任务"""
        if self.is_scheduled_running:
            schedule.run_pending()
            self.update_next_run_time()
    
    def update_next_run_time(self):
        """更新下次运行时间显示"""
        if schedule.jobs:
            next_run = schedule.next_run()
            if next_run:
                self.next_run_label.setText(f"下次运行: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
    
    def execute_immediate(self):
        """立即执行一次补充"""
        if not self.validate_config():
            return
        
        # 对于立即执行，检查是否为交易日
        from PyQt5.QtWidgets import QMessageBox
        from datetime import timedelta
        
        today = datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        
        if not self.tools.is_trade_day(today_str):
            # 今天不是交易日，寻找最近的交易日
            recent_trading_day = today
            for i in range(1, 10):  # 向前查找最多10天
                check_date = today - timedelta(days=i)
                check_date_str = check_date.strftime("%Y-%m-%d")
                if self.tools.is_trade_day(check_date_str):
                    recent_trading_day = check_date
                    break
            
            reply = QMessageBox.question(
                self, 
                "选择补充日期", 
                f"今天 {today_str} 不是交易日。\n\n"
                f"是否补充最近交易日的数据？\n"
                f"• 是：补充 {recent_trading_day.strftime('%Y-%m-%d')} 的数据\n"
                f"• 否：补充 {today_str} (今天) 的数据",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )
            
            if reply == QMessageBox.Yes:
                # 临时修改日期
                original_date = self.current_date if hasattr(self, 'current_date') else None
                self.current_date = recent_trading_day.strftime("%Y%m%d")
                self.add_log(f"使用最近交易日进行补充: {self.current_date}")
                self.execute_supplement()
                # 恢复原始日期
                if original_date:
                    self.current_date = original_date
                return
        
        self.execute_supplement()
    

    
    def execute_supplement(self):
        """执行数据补充"""
        if self.supplement_thread and self.supplement_thread.isRunning():
            self.add_log("上一次补充任务仍在运行中，跳过本次执行")
            return
        
        try:
            # 生成临时股票文件
            import tempfile
            temp_dir = tempfile.mkdtemp()
            temp_stock_files = []
            
            data_dir = os.path.join(os.path.dirname(__file__), 'data')
            
            # 根据选择的股票池生成股票文件
            for pool_type, checkbox in self.stock_pool_checkboxes.items():
                if checkbox.isChecked():
                    filename = None
                    if pool_type == 'indices':
                        filename = os.path.join(data_dir, "指数_股票列表.csv")
                    elif pool_type == 'zz500':
                        filename = os.path.join(data_dir, "中证500成分股_股票列表.csv")
                    elif pool_type == 'hs300':
                        filename = os.path.join(data_dir, "沪深300成分股_股票列表.csv")
                    elif pool_type == 'sz50':
                        filename = os.path.join(data_dir, "上证50成分股_股票列表.csv")
                    elif pool_type == 'hs_a':
                        filename = os.path.join(data_dir, "沪深A股_股票列表.csv")
                    elif pool_type == 'gem':
                        filename = os.path.join(data_dir, "创业板_股票列表.csv")
                    elif pool_type == 'sci':
                        filename = os.path.join(data_dir, "科创板_股票列表.csv")
                    elif pool_type == 'convertible_bonds':
                        filename = os.path.join(data_dir, "沪深转债_列表.csv")
                    elif pool_type == 'custom':
                        # 处理自定义股票池
                        # 1. 添加默认自定义文件
                        custom_file = self.get_custom_pool_path()
                        if os.path.exists(custom_file):
                            temp_stock_files.append(custom_file)
                        
                        # 2. 添加额外的自定义文件
                        if hasattr(self, 'custom_files') and self.custom_files:
                            for custom_file in self.custom_files:
                                if os.path.exists(custom_file) and custom_file not in temp_stock_files:
                                    temp_stock_files.append(custom_file)
                        continue  # 跳过下面的filename处理
                    
                    if filename and os.path.exists(filename):
                        temp_stock_files.append(filename)
            
            if not temp_stock_files:
                self.add_log("错误: 未找到选中的股票池文件")
                return
            
            # 获取选中的周期类型
            selected_periods = []
            for period_type, checkbox in self.period_checkboxes.items():
                if checkbox.isChecked():
                    selected_periods.append(period_type)
            
            if not selected_periods:
                self.add_log("错误: 未选择任何周期类型")
                return
            
            # 使用已设置的日期，如果没有设置则使用当前日期
            if not hasattr(self, 'current_date') or not self.current_date:
                self.current_date = datetime.now().strftime("%Y%m%d")
            
            # 显示选中的股票池文件信息
            self.add_log(f"选中的股票池文件数量: {len(temp_stock_files)}")
            for i, file_path in enumerate(temp_stock_files, 1):
                filename = os.path.basename(file_path)
                self.add_log(f"  {i}. {filename}")
            
            # 验证日期（检查是否为交易日）
            # 将current_date转换为YYYY-MM-DD格式进行交易日检查
            if len(self.current_date) == 8:
                check_date_str = f"{self.current_date[:4]}-{self.current_date[4:6]}-{self.current_date[6:8]}"
            else:
                check_date_str = datetime.now().strftime("%Y-%m-%d")
                
            if not self.tools.is_trade_day(check_date_str):
                self.add_log(f"⚠️ 注意: {check_date_str} 不是交易日，可能没有交易数据")
            
            self.add_log(f"开始补充数据: {', '.join(selected_periods)} 周期，日期: {self.current_date}")
            
            # 保存任务队列，用于依次处理多个周期
            self.pending_periods = selected_periods.copy()
            self.current_stock_files = temp_stock_files
            
            # 开始处理第一个周期
            self.process_next_period()
                
        except Exception as e:
            self.add_log(f"执行补充时出错: {str(e)}")
    
    def process_next_period(self):
        """处理下一个周期的数据补充"""
        if not hasattr(self, 'pending_periods') or not self.pending_periods:
            self.add_log("✓ 所有周期数据补充完成")
            return
        
        # 取出下一个要处理的周期
        period_type = self.pending_periods.pop(0)
        
        # 统计股票数量
        total_stocks = 0
        try:
            for stock_file in self.current_stock_files:
                if os.path.exists(stock_file):
                    with open(stock_file, 'r', encoding='utf-8-sig') as f:
                        lines = f.readlines()
                    # 减去表头行
                    stock_count = len([line for line in lines if line.strip() and not line.startswith('#')]) - 1
                    total_stocks += max(0, stock_count)
        except Exception as e:
            self.add_log(f"统计股票数量时出错: {e}")
        
        self.add_log(f"补充 {period_type} 数据... 预计处理 {total_stocks} 只股票")
        
        # 创建参数字典
        params = {
            'stock_files': self.current_stock_files,
            'period_type': period_type,
            'start_date': self.current_date,
            'end_date': self.current_date
        }
        
        # 启动补充线程
        self.supplement_thread = ScheduledSupplementThread(params, self)
        self.supplement_thread.progress.connect(self.update_supplement_progress)
        self.supplement_thread.finished.connect(self.supplement_finished)
        self.supplement_thread.error.connect(self.handle_supplement_error)
        self.supplement_thread.status_update.connect(self.update_supplement_status)
        
        # 设置进度条
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        
        self.supplement_thread.start()
    
    def update_supplement_progress(self, value):
        """更新补充进度"""
        self.progress_bar.setValue(value)
    
    def update_supplement_status(self, message):
        """更新补充状态"""
        self.add_log(message)
    
    def supplement_finished(self, success, message):
        """补充完成"""
        self.progress_bar.setVisible(False)
        self.add_log(message)
        
        # 清理线程对象
        if self.supplement_thread:
            try:
                # 确保线程已停止
                if self.supplement_thread.isRunning():
                    self.supplement_thread.stop()
                    self.supplement_thread.wait(1000)  # 等待最多1秒
                # 清理线程对象
                self.supplement_thread = None
            except Exception as e:
                logging.warning(f"清理线程对象时出错: {str(e)}")
        

        
        if success:
            self.add_log("✓ 当前周期数据补充完成")
            # 继续处理下一个周期
            self.process_next_period()
        else:
            self.add_log("✗ 数据补充失败")
            # 失败时也继续处理下一个周期
            self.process_next_period()
    
    def handle_supplement_error(self, error_msg):
        """处理补充错误"""
        self.progress_bar.setVisible(False)
        self.add_log(f"✗ 补充出错: {error_msg}")
        
        # 清理线程对象
        if self.supplement_thread:
            try:
                # 确保线程已停止
                if self.supplement_thread.isRunning():
                    self.supplement_thread.stop()
                    self.supplement_thread.wait(1000)  # 等待最多1秒
                # 清理线程对象
                self.supplement_thread = None
            except Exception as e:
                logging.warning(f"清理线程对象时出错: {str(e)}")
        
        # 错误时也继续处理下一个周期
        self.process_next_period()
    
    def add_log(self, message):
        """添加日志"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] {message}"
        
        # 添加到文本框
        self.log_text.append(log_entry)
        
        # 滚动到底部
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        
        # 同时输出到控制台
        print(log_entry)
    
    def clear_log(self):
        """清空日志"""
        self.log_text.clear()
        self.add_log("日志已清空")
    
    def closeEvent(self, event):
        """窗口关闭事件"""
        if self.is_scheduled_running:
            reply = QMessageBox.question(
                self, 
                "确认退出", 
                "定时补充任务正在运行，确定要退出吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                self.stop_scheduled_supplement()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


def main():
    # 多进程支持
    multiprocessing.set_start_method('spawn', force=True)
    
    app = QApplication(sys.argv)
    
    # 设置应用图标 - 使用相同的图标路径获取逻辑
    def get_icon_path(icon_name):
        if getattr(sys, 'frozen', False):
            if hasattr(sys, '_MEIPASS'):
                return os.path.join(sys._MEIPASS, 'icons', icon_name)
            else:
                return os.path.join(os.path.dirname(sys.executable), 'icons', icon_name)
        else:
            return os.path.join(os.path.dirname(__file__), 'icons', icon_name)
    
    icon_file = get_icon_path('stock_icon.ico')
    if os.path.exists(icon_file):
        app.setWindowIcon(QIcon(icon_file))
    else:
        icon_file_png = get_icon_path('stock_icon.png')
        if os.path.exists(icon_file_png):
            app.setWindowIcon(QIcon(icon_file_png))
    
    scheduler = GUIScheduler()
    scheduler.show()
    
    sys.exit(app.exec_())


if __name__ == '__main__':
    # Windows多进程保护
    multiprocessing.freeze_support()
    main()