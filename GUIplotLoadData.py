import sys
import os
import pandas as pd
import matplotlib.pyplot as plt
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QPushButton, QFileDialog, QComboBox, QSizePolicy, QMessageBox, QDialog)
from PyQt5.QtCore import Qt, QPoint
from PyQt5.QtGui import QFont, QIcon, QMouseEvent
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib
import matplotlib.dates as mdates
from matplotlib.widgets import RectangleSelector
import mplcursors
from matplotlib.widgets import SpanSelector
import matplotlib.dates as mdates
import logging

ICON_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'icons')
# 添加数据文件夹路径定义
DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')

matplotlib.rcParams['font.sans-serif'] = ['SimHei']
matplotlib.rcParams['axes.unicode_minus'] = False

# 在文件开头添加日志配置
logging.basicConfig(
    filename='app.log',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("帮助")
        self.setFixedSize(400, 300)

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
            except Exception:
                pass

        layout = QVBoxLayout()
        help_text = QLabel("""
        使用说明：
        1. 点击"浏览..."选择数据文件夹
        2. 从下拉菜单选择股票
        3. 图表将自动更新
        4. 点击图例可以显示/隐藏数据系列
        """)
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.setLayout(layout)

class StockDataAnalyzerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.stock_names = {}
        self.file_stock_map = {}
        self.dragging = False
        self.resizing = False
        self.drag_position = QPoint()
        self.resize_edge = None
        self.border_width = 10
        self.max_button = None
        self.current_file_info = None
        self.date_combo = None
        self.df = None
        self.stats_label = None  # Initialize as None first

        self.load_stock_names()
        self.initUI()
        self.lines = {}

    def initUI(self):
        self.setWindowTitle('股票数据可视化')
        self.setGeometry(100, 100, 1200, 800)
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowIcon(QIcon('stock_icon.png'))

        main_widget = QWidget(self)
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(self.border_width, self.border_width, self.border_width, self.border_width)

# 在initUI方法中修改标题栏相关代码
        # 标题栏部分
        title_bar = QWidget()
        title_bar.setObjectName("titleBar")
        title_bar.setFixedHeight(60)  # 保持高度不变
        title_bar_layout = QHBoxLayout(title_bar)
        title_bar_layout.setContentsMargins(15, 0, 15, 10)  # 修改底部边距，给边框留出空间
        
        # Logo部分
        logo_path = os.path.join(ICON_PATH, 'visualize.png')
        logo_label = QLabel()
        logo_pixmap = QIcon(logo_path).pixmap(24, 24)
        logo_label.setPixmap(logo_pixmap)
        logo_label.setStyleSheet("""
            padding: 0px;
            margin-bottom: 10px;  /* 添加底部边距 */
        """)
        title_bar_layout.addWidget(logo_label)

        # 标题文字
        title_label = QLabel("股票数据可视化")
        title_label.setStyleSheet("""
            color: #E0E0E0;
            font-weight: bold;
            font-size: 24px;
            padding-left: 10px;
            margin-bottom: 10px;  /* 添加底部边距 */
        """)
        title_bar_layout.addWidget(title_label)
        title_bar_layout.addStretch()

        # 控制按钮样式也需要调整
        help_button = QPushButton("?")
        help_button.setObjectName("helpButton")
        help_button.setFixedSize(40, 40)
        help_button.clicked.connect(self.show_help)
        help_button.setStyleSheet("""
            QPushButton {
                margin-bottom: 10px;
            }
        """)
        title_bar_layout.addWidget(help_button)

        min_button = QPushButton("—")
        min_button.setObjectName("minButton")
        min_button.setFixedSize(40, 40)
        min_button.clicked.connect(self.showMinimized)
        min_button.setStyleSheet("""
            QPushButton {
                margin-bottom: 10px;
            }
        """)
        title_bar_layout.addWidget(min_button)

        self.max_button = QPushButton("□")
        self.max_button.setObjectName("maxButton")
        self.max_button.setFixedSize(40, 40)
        self.max_button.clicked.connect(self.toggle_maximize)
        self.max_button.setStyleSheet("""
            QPushButton {
                margin-bottom: 10px;
            }
        """)
        title_bar_layout.addWidget(self.max_button)

        close_button = QPushButton("×")
        close_button.setObjectName("closeButton")
        close_button.setFixedSize(40, 40)
        close_button.clicked.connect(self.close)
        close_button.setStyleSheet("""
            QPushButton {
                margin-bottom: 10px;
            }
        """)
        title_bar_layout.addWidget(close_button)

        main_layout.addWidget(title_bar)

# 内容区域使用垂直布局
        # 内容区域使用垂直布局
        content_widget = QWidget()
        main_layout.addWidget(content_widget)
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(5)  # 减少垂直间距
        
        # 上部控制和信息区域（水平布局）
        top_container = QWidget()
        top_layout = QHBoxLayout(top_container)
        top_layout.setContentsMargins(0, 0, 0, 5)  # 减少底部边距
        
        # 左侧控制面板
        control_panel = QWidget()
        control_layout = QVBoxLayout(control_panel)
        control_layout.setContentsMargins(0, 0, 10, 0)
        control_layout.setSpacing(5)  # 减少控件之间的间距
        
        # 文件夹选择部分的修改
        folder_layout = QHBoxLayout()
        folder_layout.setSpacing(5)  # 减少水平间距
        self.folder_path_label = QLabel('选择数据文件夹：')
        self.folder_path_button = QPushButton('浏览...')
        self.folder_path_button.clicked.connect(self.select_folder)

        # 修改重载按钮
        self.reload_button = QPushButton()
        reload_icon_path = os.path.join(ICON_PATH, 'reload.png')
        if os.path.exists(reload_icon_path):
            self.reload_button.setIcon(QIcon(reload_icon_path))
        else:
            self.reload_button.setText('↻')  # 使用Unicode字符作为备选
            self.reload_button.setFont(QFont("", 16))  # 设置更大的字体

        self.reload_button.setToolTip('重新加载文件夹')
        self.reload_button.setFixedSize(40, 40)  # 增加按钮大小
        self.reload_button.setObjectName("reloadButton")
        self.reload_button.clicked.connect(self.reload_folder)
        self.reload_button.setEnabled(True)  # 初始状态下启用按钮

        folder_layout.addWidget(self.folder_path_label)
        folder_layout.addWidget(self.folder_path_button)
        folder_layout.addWidget(self.reload_button)
        folder_layout.addStretch()
        control_layout.addLayout(folder_layout)

        # 选择器区域
        selector_layout = QHBoxLayout()
        selector_layout.setSpacing(5)  # 减少水平间距
        
        # 股票选择器
        stock_label = QLabel('股票：')
        selector_layout.addWidget(stock_label)
        self.stock_combo = QComboBox()
        self.stock_combo.setFixedWidth(300)
        self.stock_combo.currentIndexChanged.connect(lambda _: self.on_stock_changed())
        selector_layout.addWidget(self.stock_combo)
        
        # 日期选择器
        self.date_label = QLabel('日期：')
        self.date_label.setVisible(False)
        selector_layout.addWidget(self.date_label)
        self.date_combo = QComboBox()
        self.date_combo.setFixedWidth(200)
        self.date_combo.setVisible(False)
        self.date_combo.currentIndexChanged.connect(self.update_chart)
        selector_layout.addWidget(self.date_combo)
        selector_layout.addStretch()
        control_layout.addLayout(selector_layout)
        
        # 将控制面板添加到顶部布局
        top_layout.addWidget(control_panel)
        
        # 右侧息面板
        info_panel = QWidget()
        info_panel.setFixedWidth(300)
        info_layout = QVBoxLayout(info_panel)
        info_layout.setContentsMargins(10, 0, 0, 0)
        info_layout.setSpacing(2)  # 减少垂直间距
        
        # 统计信息标签
        stats_title = QLabel('文件信息')
        stats_title.setStyleSheet("""
            color: #E0E0E0;
            font-size: 14px;
            font-weight: bold;
            padding: 2px 0;
        """)
        info_layout.addWidget(stats_title)
        
        self.stats_label = QLabel('统计信息将在此显示')
        self.stats_label.setObjectName("statsLabel")
        self.stats_label.setStyleSheet("""
            background-color: #2D2D2D;
            padding: 8px;
            border-radius: 5px;
            font-size: 12px;
            line-height: 1.2;
        """)
        self.stats_label.setWordWrap(True)
        self.stats_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        info_layout.addWidget(self.stats_label)
        
        # 将信息面板添加到顶部布局
        top_layout.addWidget(info_panel)
        
        # 将顶部容器添加到主布局
        content_layout.addWidget(top_container)
        
        # 表区域
        self.figure = Figure(figsize=(5, 4), dpi=100)
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        content_layout.addWidget(self.canvas)
        
        # 设置内容区域的拉伸因子，使图表区域获得更多空间
        content_layout.setStretch(0, 0)  # 顶部控制区域不拉伸
        content_layout.setStretch(1, 1)  # 图表区域可以拉伸

        self.apply_styles()

        # 修改 reload_folder 方法
    def reload_folder(self, *args):
        """重新加载当前文件夹的数据"""
        # 修改获取当前文件夹路径的方式
        current_folder = self.folder_path_label.text()
        if current_folder.startswith('选择的文件夹：'):
            current_folder = current_folder[7:]  # 移除"选择的文件夹："前缀
        
        if not current_folder or not os.path.exists(current_folder):
            QMessageBox.warning(self, "警告", "请先选择有效的数据文件夹")
            return
        
        try:
            # 保存当前选择的股票和日期
            current_stock = self.stock_combo.currentText()
            current_date = self.date_combo.currentText() if self.date_combo.isVisible() else None
            
            # 重新分析文件夹
            self.analyze_folder(current_folder)
            
            # 尝试恢复之前的选择
            if current_stock:
                index = self.stock_combo.findText(current_stock)
                if index >= 0:
                    self.stock_combo.setCurrentIndex(index)
            
            if current_date and self.date_combo.isVisible():
                index = self.date_combo.findText(current_date)
                if index >= 0:
                    self.date_combo.setCurrentIndex(index)
            
        except Exception as e:
            QMessageBox.critical(self, "错误", f"重新加载文件夹时出错: {str(e)}")

    def apply_styles(self):
        style = """
            QMainWindow {
                background-color: #1A1A1A;
                border: 1px solid #333333;
                border-radius: 10px;
            }
            QWidget {
                background-color: #1A1A1A;
                color: #E0E0E0;
            }
            #titleBar {
                background-color: #1A1A1A;
                border-bottom: 1px solid #333333;
                padding-bottom: 0px;
                margin-bottom: 0px;
            }
            QPushButton {
                background-color: #2D2D2D;
                color: #E0E0E0;  
                border: none;
                border-radius: 5px;
                padding: 8px 15px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #3D3D3D;
            }
            QPushButton:pressed {
                background-color: #444444;
            }
            QPushButton#helpButton, QPushButton#minButton, QPushButton#maxButton, QPushButton#closeButton {
                background-color: transparent;
                border-radius: 16px;
                font-size: 16px;
                padding: 0px;
            }
            QPushButton#helpButton:hover, QPushButton#minButton:hover, QPushButton#maxButton:hover {
                background-color: #3D3D3D;
            }
            QPushButton#closeButton:hover {
                background-color: #E81123;
            }
            QComboBox {
                background-color: #2D2D2D;
                border: none;
                border-radius: 5px;
                padding: 5px 10px;
                color: #E0E0E0;
                min-height: 30px;
            }
            QComboBox:hover {
                background-color: #3D3D3D;
            }
            QComboBox::drop-down {
                border: none;
                width: 30px;
            }
            QComboBox::down-arrow {
                width: 12px;
                height: 12px;
            }
            QLabel {
                color: #E0E0E0;
                font-size: 14px;
            }
            #statsLabel {
                background-color: #2D2D2D;
                padding: 10px;
                border-radius: 5px;
                font-size: 14px;
            }
            QPushButton#reloadButton {
                background-color: transparent;
                border: 2px solid #3D3D3D;  /* 加粗边框 */
                border-radius: 20px;        /* 增加圆角 */
                padding: 8px;               /* 增加内边距 */
                margin: 0px 5px;
            }
            QPushButton#reloadButton:hover {
                background-color: #3D3D3D;
                border-color: #4D4D4D;
            }
            QPushButton#reloadButton:pressed {
                background-color: #2D2D2D;
                border-color: #5D5D5D;
            }
            QPushButton#reloadButton[enabled="false"] {
                border-color: #2D2D2D;
                color: #666666;
            }
        """
        self.setStyleSheet(style)
        plt.style.use('dark_background')
        self.reload_button.setObjectName("reload_button")  # 设置对象名以应用特定样式
        self.figure.patch.set_facecolor('#1E1E1E')

    def show_help(self, *args):
        help_dialog = HelpDialog(self)
        help_dialog.exec_()

    def toggle_maximize(self):
        if self.max_button is None:
            print("Warning: max_button is None")
            return

        if self.isMaximized():
            self.showNormal()
            self.max_button.setText("□")
        else:
            self.showMaximized()
            self.max_button.setText("❐")

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            title_bar = self.findChild(QWidget, "titleBar")
            if title_bar and title_bar.underMouse():
                self.dragging = True
            else:
                self.resize_edge = self.get_resize_edge(event.pos())
                if self.resize_edge:
                    self.resizing = True
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() == Qt.LeftButton:
            if self.dragging:
                self.move(event.globalPos() - self.drag_position)
                event.accept()
            elif self.resizing:
                self.resize_window(event.globalPos())
                event.accept()
        else:
            self.update_cursor(event.pos())

    def mouseReleaseEvent(self, event: QMouseEvent):
        self.dragging = False
        self.resizing = False
        self.resize_edge = None
        self.unsetCursor()
        event.accept()

    def get_resize_edge(self, pos):
        rect = self.rect()
        if pos.x() <= self.border_width:
            return 'left'
        elif pos.x() >= rect.right() - self.border_width:
            return 'right'
        elif pos.y() <= self.border_width:
            return 'top'
        elif pos.y() >= rect.bottom() - self.border_width:
            return 'bottom'
        return None

    def update_cursor(self, pos):
        edge = self.get_resize_edge(pos)
        if edge in ['left', 'right']:
            self.setCursor(Qt.SizeHorCursor)
        elif edge in ['top', 'bottom']:
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.unsetCursor()

    def resize_window(self, global_pos):
        new_rect = self.geometry()
        if self.resize_edge == 'left':
            new_rect.setLeft(global_pos.x())
        elif self.resize_edge == 'right':
            new_rect.setRight(global_pos.x())
        elif self.resize_edge == 'top':
            new_rect.setTop(global_pos.y())
        elif self.resize_edge == 'bottom':
            new_rect.setBottom(global_pos.y())
        self.setGeometry(new_rect)

    def select_folder(self, *args):
        try:
            logging.info("开始选择文件夹...")
            folder_path = QFileDialog.getExistingDirectory(self, "选择数据文件夹")
            logging.info(f"用户选择的文件夹路径: {folder_path}")
            
            if folder_path:
                logging.info(f"开始处理选择的文件夹: {folder_path}")
                self.folder_path_label.setText(f'选择的文件夹：{folder_path}')
                self.reload_button.setEnabled(True)
                
                logging.info("开始分析文件夹...")
                self.analyze_folder(folder_path)
                logging.info("文件夹分析完成")
            else:
                logging.info("用户取消了文件夹选择")
                
        except Exception as e:
            logging.error(f"选择文件夹时发生错误: {str(e)}", exc_info=True)
            QMessageBox.critical(self, "错误", f"选择文件夹时出错: {str(e)}")

    def load_stock_names(self):
        """加载股票和指数名称映射"""
        data_dir = os.path.join(os.path.dirname(__file__), 'data')
        stock_list_path = os.path.join(data_dir, '全部股票_股票列表.csv')
        
        # 定义主要指数集合
        main_indices = {
            '000300.SH': '沪深300',
            '000688.SH': '科创50',
            '000852.SH': '中证1000',
            '000905.SH': '中证500',
            '399001.SZ': '深证成指',
            '399006.SZ': '创业板指',
            '000001.SH': '上证指数'
        }
        
        # 初始化股票名称字典先加入主要指数
        self.stock_names = main_indices.copy()
        
        # 加载其他股票列表
        if os.path.exists(stock_list_path):
            try:
                df = pd.read_csv(stock_list_path, encoding='utf-8-sig', header=None, names=['code', 'name'])
                df['code'] = df['code'].astype(str)
                
                # 处理个股代码
                for _, row in df.iterrows():
                    code = row['code'].split('.')[0]  # 移除可能的后缀
                    full_code = None
                    
                    # 跳过已经在主要指数中的代码
                    if f"{code}.SH" in main_indices or f"{code}.SZ" in main_indices:
                        continue
                    
                    # 根据代码规则添加正确的后缀
                    if code.startswith('6'):
                        full_code = f"{code}.SH"
                    elif code.startswith(('0', '3')):
                        full_code = f"{code}.SZ"
                    elif code.startswith('8'):
                        full_code = f"{code}.BJ"
                    
                    if full_code:
                        self.stock_names[full_code] = row['name']
                    
            except Exception as e:
                logging.error(f"加载股票列表文件时出错: {e}")
        else:
            logging.warning(f"未找到股票列表文件: {stock_list_path}")

    # 在 StockDataAnalyzerGUI 类中修改信号连接相关的方法
    def analyze_folder(self, folder_path):
        try:
            logging.info(f"开始分析文件夹: {folder_path}")
            # 断开信号连接，防止重复触发
            self.stock_combo.blockSignals(True)
            
            # 检查文件夹是否存在
            if not os.path.exists(folder_path):
                logging.error(f"文件夹不存在: {folder_path}")
                raise FileNotFoundError(f"找不到文件夹: {folder_path}")
            
            # 获取所有csv文件
            csv_files = [f for f in os.listdir(folder_path) if f.endswith('.csv')]
            logging.info(f"找到 {len(csv_files)} 个CSV文件")
            
            # 检查是否有csv文件
            if not csv_files:
                logging.warning(f"文件夹 {folder_path} 中没有找到CSV文件")
                QMessageBox.warning(self, "警告", "所选文件夹中没有找到CSV文件")
                return
            
            total_size = sum(os.path.getsize(os.path.join(folder_path, f)) for f in csv_files)
            
            self.file_stock_map = {}
            period_types = set()
            date_ranges = []
            
            # 处理文件信息
            valid_files = []
            for file in csv_files:
                file_path = os.path.join(folder_path, file)
                if not os.path.exists(file_path):
                    logging.warning(f"文件不存在: {file_path}")
                    continue
                
                try:
                    # 验证文件是否可读
                    with open(file_path, 'r', encoding='utf-8') as f:
                        f.readline()
                    
                    parts = file.split('_')
                    if len(parts) > 0:
                        stock_code = parts[0]
                        self.file_stock_map[file] = stock_code
                        
                        file_info = self.parse_filename(file)
                        period_types.add(file_info['period_type'])
                        if file_info['start_date'] != '未知' and file_info['end_date'] != '未知':
                            try:
                                start = pd.to_datetime(file_info['start_date'])
                                end = pd.to_datetime(file_info['end_date'])
                                date_ranges.append((start, end))
                            except:
                                pass
                            
                    valid_files.append(file)
                    
                except Exception as e:
                    logging.error(f"处理文件 {file} 时出错: {str(e)}")
                    continue

            if not valid_files:
                QMessageBox.warning(self, "警告", "没有找到有效的数据文件")
                return

            # 更新界面信息显示
            stats_text = self.generate_stats_text(valid_files, total_size, period_types, date_ranges)
            self.stats_label.setText(stats_text)

            # 更新股票选择器
            self.update_stock_combo(self.file_stock_map)
            
            # 恢复信号连接
            self.stock_combo.blockSignals(False)
            
            # 选择第一个股票（如果有）
            if self.stock_combo.count() > 0:
                self.stock_combo.setCurrentIndex(0)
                # 手动调用一次更新
                self.on_stock_changed()

        except Exception as e:
            logging.error(f"分析文件夹时发生错误: {str(e)}", exc_info=True)
            QMessageBox.critical(self, "错误", f"分析文件夹时出错: {str(e)}")
        finally:
            # 确保信号恢复连接
            self.stock_combo.blockSignals(False)
            logging.info("文件夹分析完成")

    def open_visualization(self):
        """打开数据可视化窗口"""
        try:
            # 创建新的可视化窗口实例
            visualization_window = StockDataAnalyzerGUI()
            
            # 如果有当前的数据文件夹路径，则传递给可视化窗口
            if hasattr(self, 'local_data_path_edit') and self.local_data_path_edit.text().strip():
                current_path = self.local_data_path_edit.text().strip()
                if os.path.exists(current_path):
                    visualization_window.folder_path_label.setText(f'选择的文件夹：{current_path}')
                    visualization_window.analyze_folder(current_path)
            
            # 显示窗口
            visualization_window.show()
            
        except Exception as e:
            QMessageBox.critical(self, "错误", f"打开可视化窗口时出错: {str(e)}")

    def generate_stats_text(self, csv_files, total_size, period_types, date_ranges):
        """生成统计信息文本"""
        if date_ranges:
            earliest_date = min(start for start, _ in date_ranges)
            latest_date = max(end for _, end in date_ranges)
            date_range_str = f"{earliest_date.strftime('%Y-%m-%d')} 到 {latest_date.strftime('%Y-%m-%d')}"
        else:
            date_range_str = "未知"

        markets = {'SZ': 0, 'SH': 0, 'BJ': 0}
        for code in self.file_stock_map.values():
            market = code.split('.')[-1] if '.' in code else ''
            if market in markets:
                markets[market] += 1

        base_stats = f"股票总数：{len(self.file_stock_map)}  |  文件总大小：{total_size / (1024*1024):.2f} MB"
        market_stats = f"市场分布：深市 {markets['SZ']}，沪市 {markets['SH']}北所 {markets['BJ']}"
        period_stats = f"周期类型：{', '.join(sorted(period_types))}"
        date_stats = f"日期范围：{date_range_str}"
        
        return f"{base_stats}\n{market_stats}\n{period_stats}\n{date_stats}"
    def update_stock_combo(self, file_stock_map):
        """更新股票选择下拉框"""
        self.stock_combo.clear()
        for file, stock_code in file_stock_map.items():
            # 获取完整的股票代码（包含后缀）
            name = self.stock_names.get(stock_code, '未知')
            display_text = f"{stock_code} - {name}"
            self.stock_combo.addItem(display_text, file)

    def on_stock_changed(self, *args):
        """当股票选择改变时的处理函数"""
        try:
            selected_file = self.stock_combo.currentData()
            if not selected_file:
                return

            folder_path = self.folder_path_label.text()
            if folder_path.startswith('选择的文件夹：'):
                folder_path = folder_path[7:]  # 移除"选择的文件夹："前缀
            
            file_path = os.path.join(folder_path, selected_file)
            
            # 检查文件是否存在
            if not os.path.exists(file_path):
                logging.error(f"文件不存在: {file_path}")
                QMessageBox.warning(self, "警告", f"未找到文件: {selected_file}\n请检查文件是否存在或重新加载文件夹。")
                return

            # 尝试读取文件
            try:
                self.df = pd.read_csv(file_path)
            except Exception as e:
                logging.error(f"读取文件 {file_path} 时出错: {str(e)}")
                QMessageBox.critical(self, "错误", f"读取文件时出错: {str(e)}")
                return

            self.current_file_info = self.parse_filename(selected_file)
            
            # 处理日期选择器的显示/隐藏
            if self.current_file_info['period_type'] in ['tick', '1m', '5m']:
                self.prepare_date_selector()
            else:
                self.date_label.setVisible(False)
                self.date_combo.setVisible(False)
            
            # 更新图表
            self.update_chart()

        except Exception as e:
            logging.error(f"股票选择改变时出错: {str(e)}")
            QMessageBox.critical(self, "错误", f"处理股票数据时出错: {str(e)}")

    def prepare_date_selector(self):
        date_col = next((col for col in self.df.columns if 'date' in col.lower()), None)
        if date_col:
            self.df[date_col] = pd.to_datetime(self.df[date_col])
            unique_dates = self.df[date_col].dt.date.unique()
            self.date_combo.clear()
            self.date_combo.addItems([str(date) for date in sorted(unique_dates)])
            self.date_label.setVisible(True)  # 同时显示标签
            self.date_combo.setVisible(True)
            # 断开之前的连接，以防止触发不必要的更新
            self.date_combo.currentIndexChanged.disconnect()
            # 重新连接信号
            self.date_combo.currentIndexChanged.connect(self.update_chart)
        else:
            self.date_label.setVisible(False)  # 同时隐藏标签
            self.date_combo.setVisible(False)
            QMessageBox.warning(self, "警告", "无法在数据中找到日期列")

    def update_chart(self, *args):
        logging.debug(f"update_chart called with args: {args}")
        if self.df is None or self.current_file_info is None:
            return

        try:
            # 防止重复绘图，先清除之前的图形
            self.figure.clear()
            
            stock_code = self.current_file_info['stock_code']
            stock_name = self.stock_names.get(stock_code, '未知')

            df = self.df.copy()

            # 设置日期时间列
            date_col = next((col for col in df.columns if 'date' in col.lower()), None)
            time_col = next((col for col in df.columns if 'time' in col.lower()), None)

            if self.current_file_info['period_type'] == '1d':
                # 日线数据的处理
                if date_col:
                    df[date_col] = pd.to_datetime(df[date_col])
                    x_axis = date_col
                else:
                    raise ValueError("无法找到日期列")
            else:
                # 分钟级数据的处理
                if self.current_file_info['period_type'] in ['tick', '1m', '5m'] and self.date_combo.isVisible():
                    selected_date = pd.to_datetime(self.date_combo.currentText()).date()
                    df = df[df[date_col].dt.date == selected_date]

                if date_col and time_col:
                    df['datetime'] = pd.to_datetime(df[date_col].astype(str) + ' ' + df[time_col].astype(str))
                    x_axis = 'datetime'
                    
                    # 只处理交易时间内的数据
                    current_time = df[x_axis].dt.time
                    morning_mask = current_time.between(pd.to_datetime('09:30').time(), 
                                                    pd.to_datetime('11:30').time())
                    afternoon_mask = current_time.between(pd.to_datetime('13:00').time(), 
                                                        pd.to_datetime('15:00').time())
                    df = df[morning_mask | afternoon_mask]
                else:
                    raise ValueError("无法找到日期或时间列")

            ax = self.figure.add_subplot(111)
            self.plot_data(df, x_axis, stock_code, stock_name)

            # 添加交互功能
            self.toggle_selector = RectangleSelector(
                ax, 
                self.line_select_callback,
                useblit=True,
                button=[1],
                minspanx=5, 
                minspany=5,
                spancoords='pixels',
                interactive=True
            )
            
            self.toggle_selector.set_active(True)
            self.toggle_selector.set_props(facecolor='red', edgecolor='red', alpha=0.2, fill=True)
            
            self.figure.canvas.mpl_connect('button_press_event', self.on_right_click)
            self.cursor = mplcursors.cursor(self.figure, hover=True)
            self.cursor.connect("add", self.on_hover)
            self.figure.canvas.mpl_connect('pick_event', self.on_pick)
            self.canvas.draw()

        except Exception as e:
            logging.error(f"更新图表时出错: {str(e)}")
            QMessageBox.critical(self, "错误", f"更新图表时出错: {str(e)}")

    def on_hover(self, sel):
        try:
            line = sel.artist
            x, y = sel.target
            label = line.get_label()
            x_date = mdates.num2date(x)
            sel.annotation.set_text(f"{label}: {y:.2f}\n时间: {x_date:%Y-%m-%d %H:%M:%S}")
            
            # 设置标注样式
            sel.annotation.get_bbox_patch().set(
                fc="#2D2D2D",     # 深色背景
                ec="#666666",     # 灰色边框
                alpha=0.9,        # 高不透明度
                boxstyle="round,pad=0.5"  # 圆角矩形，增加内边距
            )
            
            # 设置文字样式
            sel.annotation.set_color("#E0E0E0")  # 浅色文字
            sel.annotation.set_fontsize(10)      # 设置字体大小
            sel.annotation.set_fontweight('bold') # 加粗文字
            
        except Exception as e:
            print(f"悬停显示出错: {str(e)}")

    def plot_data(self, df, x_axis, stock_code, stock_name):
            ax = self.figure.axes[0]

            # 添加中英文映射字典
            column_names = {
                'time': '时间点',
                'lastPrice': '最新价',
                'open': '开盘价',
                'high': '最高价',
                'low': '最低价',
                'close': '收盘价',
                'volume': '成交量',
                'amount': '成交额',
                'settlementPrice': '今结算',
                'openInterest': '持仓量',
                'preClose': '前收价',
                'suspendFlag': '停牌标记',
                'lastClose': '前收盘价',
                'pvolume': '原始成交量',
                'stockStatus': '证券状况',
                'openInt': '持仓量',
                'lastSettlementPrice': '前结算',
                'askPrice': '卖买价',
                'askVol': '卖委量',
                'bidPrice': '委买价',
                'bidVol': '委买量'
            }   

            self.lines = {}
            colors = ['#00A8E8', '#FF6B6B', '#4CAF50', '#FFC107', '#9C27B0']
            
            # 数据绘制逻辑，使用中文标签
            if len(df) > 1000:
                step = len(df) // 1000
                for i, column in enumerate(df.select_dtypes(include=['float64', 'int64']).columns):
                    if column != x_axis:
                        color = colors[i % len(colors)]
                        # 使用映射字典取中文名称，如果没有对应的中文名称则使用原名称
                        label = column_names.get(column, column)
                        line, = ax.plot(df[x_axis][::step], df[column][::step], 
                                    label=label, 
                                    color=color,
                                    linewidth=2,
                                    alpha=0.8)
                        self.lines[label] = line
            else:
                for i, column in enumerate(df.select_dtypes(include=['float64', 'int64']).columns):
                    if column != x_axis:
                        color = colors[i % len(colors)]
                        # 使用映射字典获取中文名称，如果没有对应的中文名称则使用原名称
                        label = column_names.get(column, column)
                        line, = ax.plot(df[x_axis], df[column], 
                                    label=label,
                                    color=color,
                                    linewidth=2,
                                    alpha=0.8)
                        self.lines[label] = line

            # 根据周期类型设置不同的x轴格式
            if self.current_file_info['period_type'] == '1d':
                # 日线数据的x轴设置
                ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
                if len(df) > 0:
                    # 为日数据添加适当的边距
                    date_range = df[x_axis].max() - df[x_axis].min()
                    buffer = date_range * 0.05
                    ax.set_xlim(df[x_axis].min() - buffer, df[x_axis].max() + buffer)
            else:
                # 分钟级别数据的x轴设置
                ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
                ax.xaxis.set_minor_locator(mdates.MinuteLocator(byminute=[0, 30]))
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
                
                if len(df) > 0:
                    trading_date = df[x_axis].iloc[0].date()
                    morning_start = pd.Timestamp(trading_date).replace(hour=9, minute=30)
                    morning_end = pd.Timestamp(trading_date).replace(hour=11, minute=30)
                    afternoon_start = pd.Timestamp(trading_date).replace(hour=13, minute=0)
                    afternoon_end = pd.Timestamp(trading_date).replace(hour=15, minute=0)
                    
                    ax.set_xlim(morning_start, afternoon_end)
                    ax.axvspan(morning_end, afternoon_start, 
                            color='gray', alpha=0.2, label='休市时间')

            # 其余的设置保持不变
            title = f"{stock_code} - {stock_name}\n"
            title += f"周期: {self.current_file_info['period_type']}, "
            if self.current_file_info['period_type'] in ['tick', '1m', '5m'] and self.date_combo.isVisible():
                title += f"日期: {self.date_combo.currentText()}\n"
            else:
                title += f"日期: {self.current_file_info['start_date']} 到 {self.current_file_info['end_date']}\n"
            title += f"时间段: {self.current_file_info['time_range']}"

            # 样式设置保持不变
            ax.set_facecolor('#1E1E1E')
            ax.grid(True, color='#333333', linestyle='--', alpha=0.3)
            
            for spine in ax.spines.values():
                spine.set_color('#333333')

            ax.set_title(title, color='#E0E0E0', fontsize=12)
            
            ax.set_xlabel('日期' if self.current_file_info['period_type'] == '1d' else '时间', 
                        color='#E0E0E0', fontsize=14)
            ax.set_ylabel('数值', color='#E0E0E0', fontsize=14)
            ax.tick_params(axis='x', colors='#E0E0E0', labelsize=12, rotation=45)
            ax.tick_params(axis='y', colors='#E0E0E0', labelsize=12)

            # 创建图例并固定位置
            leg = ax.legend(
                bbox_to_anchor=(1.02, 1),  # 将图例固定在图表右侧
                loc='upper left',           # 图例的对齐方式
                facecolor='#1E1E1E',
                edgecolor='#333333',
                labelcolor='#E0E0E0',
                fontsize=12,
                borderaxespad=0.           # 减少图例和图表之间的间距
            )
            
            # 设置图例可点击
            for legline, origline in zip(leg.get_lines(), self.lines.values()):
                legline.set_picker(5)
                legline.set_pickradius(5)
                legline.set_linewidth(2.0)

            ax.grid(True, color='#333333', linestyle='--', linewidth=0.5)
            
            # 调整布局以适应图例
            self.figure.tight_layout()
            # 为图例留出空间
            self.figure.subplots_adjust(right=0.85)
        
    def line_select_callback(self, eclick, erelease):
        x1, y1 = eclick.xdata, eclick.ydata
        x2, y2 = erelease.xdata, erelease.ydata
        
        # 只有当选择的区域足够大时才进行缩放
        if abs(x2 - x1) > 1e-5 and abs(y2 - y1) > 1e-5:  # 使用一个小的阈值
            self.figure.axes[0].set_xlim(min(x1, x2), max(x1, x2))
            self.figure.axes[0].set_ylim(min(y1, y2), max(y1, y2))
            self.canvas.draw()

    def on_mouse_click(self, event):
        if event.button == 3:  # 右键点击
            self.reset_view()

    def reset_view(self):
        ax = self.figure.axes[0]
        
        # 获取所有可见线条的数据
        visible_lines = [line for line in self.lines.values() if line.get_visible()]
        
        if not visible_lines:
            return  # 如果没有可见的线条，不行任何操作
        
        # 计算可见数据的范围
        x_min = min(line.get_xdata().min() for line in visible_lines)
        x_max = max(line.get_xdata().max() for line in visible_lines)
        y_min = min(line.get_ydata().min() for line in visible_lines)
        y_max = max(line.get_ydata().max() for line in visible_lines)
        
        # 设置轴的范围，添加一些边距
        x_margin = (x_max - x_min) * 0.05
        y_margin = (y_max - y_min) * 0.05
        ax.set_xlim(x_min - x_margin, x_max + x_margin)
        ax.set_ylim(y_min - y_margin, y_max + y_margin)
        
        self.canvas.draw()
    def line_select_callback(self, eclick, erelease):
        x1, y1 = eclick.xdata, eclick.ydata
        x2, y2 = erelease.xdata, erelease.ydata
        
        # 只有当选择的区域足够大时才进行缩放
        if abs(x2 - x1) > 1e-5 and abs(y2 - y1) > 1e-5:  # 使用一个小的阈值
            self.figure.axes[0].set_xlim(min(x1, x2), max(x1, x2))
            self.figure.axes[0].set_ylim(min(y1, y2), max(y1, y2))
            self.canvas.draw()
    def on_right_click(self, event):
        if event.button == 3:  # 右键点击
            self.reset_view()

    def parse_filename(self, filename):
        try:
            logging.info(f"开始解析文件名: {filename}")
            parts = filename.split('_')
            if len(parts) < 5:
                logging.warning(f"文件名格式不完整: {filename}")
                return {
                    'stock_code': parts[0] if len(parts) > 0 else '未知',
                    'period_type': parts[1] if len(parts) > 1 else '未知',
                    'start_date': parts[2] if len(parts) > 2 else '未知',
                    'end_date': parts[3] if len(parts) > 3 else '未知',
                    'time_range': '全天'
                }
            
            stock_code = parts[0]
            period_type = parts[1]
            start_date = parts[2]
            end_date = parts[3]
            
            # Combine all remaining parts for the time range
            time_range_parts = '_'.join(parts[4:]).split('.')[0]  # Join remaining parts and remove file extension
            
            if time_range_parts == 'all':
                time_range = '全天'
            else:
                try:
                    # Try to parse the time range
                    start_time, end_time = time_range_parts.split('-')
                    
                    # Function to format time string
                    def format_time(time_str):
                        if '_' in time_str:
                            hour, minute = time_str.split('_')
                            return f"{hour.zfill(2)}:{minute.zfill(2)}"
                        return time_str  # Return as is if it doesn't contain underscore
                    
                    start_time = format_time(start_time)
                    end_time = format_time(end_time)
                    
                    time_range = f"{start_time}-{end_time}"
                except ValueError:
                    # If parsing fails, keep the original time range string
                    time_range = time_range_parts
            
            result = {
                'stock_code': stock_code,
                'period_type': period_type,
                'start_date': start_date,
                'end_date': end_date,
                'time_range': time_range
            }
            logging.info(f"文件名解析结果: {result}")
            return result
            
        except Exception as e:
            logging.error(f"解析文件名时发生错误: {str(e)}", exc_info=True)
            return {
                'stock_code': '未知',
                'period_type': '未知',
                'start_date': '未知',
                'end_date': '未知',
                'time_range': '全天'
            }

    def on_pick(self, event):
        # 获取被点击的图例
        legline = event.artist
        origline = self.lines[legline.get_label()]
        visible = not origline.get_visible()
        origline.set_visible(visible)
        
        # 更改图例透明度
        legline.set_alpha(1.0 if visible else 0.2)
        
        self.figure.canvas.draw()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    ex = StockDataAnalyzerGUI()
    ex.show()
    sys.exit(app.exec_())