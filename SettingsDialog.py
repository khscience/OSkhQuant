import os
import sys
import logging
import webbrowser
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
    QGroupBox, QPushButton, QLineEdit, QFileDialog,
    QMessageBox, QProgressDialog, QCheckBox, QComboBox
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSettings
from PyQt5.QtGui import QFont, QIcon, QDoubleValidator

from khQTTools import get_and_save_stock_list

# 导入正确的版本信息获取函数
try:
    from version import get_version_info
except ImportError:
    # 如果无法导入，定义一个备用函数
    def get_version_info():
        """获取版本信息（备用）"""
        return {
            "version": "1.0.0",
            "build_date": "2023-01-01",
            "channel": "stable",
            "app_name": "看海量化交易平台"
        }

class SettingsDialog(QDialog):
    """设置对话框类"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = QSettings('KHQuant', 'StockAnalyzer')
        self.confirmed_exit = False
        
        # 设置窗口标志
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setWindowModality(Qt.ApplicationModal)
        
        self.initUI()
    
    def initUI(self):
        """设置对话框UI初始化"""
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
        
        self.setWindowTitle('软件设置')
        self.setMinimumWidth(500)
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        
        # 主布局
        layout = QVBoxLayout(self)
        layout.setSpacing(15)  # 增加组件之间的间距
        
        # 添加基本参数设置组
        basic_params_group = QGroupBox("基本参数设置")
        basic_params_group.setStyleSheet("""
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
        basic_params_layout = QVBoxLayout()
        
        # 添加无风险收益率设置
        risk_free_rate_layout = QHBoxLayout()
        risk_free_rate_label = QLabel("无风险收益率:")
        risk_free_rate_label.setStyleSheet("color: #E0E0E0;")
        self.risk_free_rate_edit = QLineEdit()
        self.risk_free_rate_edit.setStyleSheet("""
            QLineEdit {
                border: 1px solid #3D3D3D;
                border-radius: 2px;
                padding: 5px;
                background-color: #2D2D2D;
                color: #E0E0E0;
            }
            QLineEdit:focus {
                border: 1px solid #5D5D5D;
            }
        """)
        # 从设置中读取无风险收益率，如果不存在则使用默认值0.03
        risk_free_rate_value = self.settings.value('risk_free_rate', '0.03')
        self.risk_free_rate_edit.setText(str(risk_free_rate_value))
        
        # 设置验证器，只允许输入0-1之间的浮点数
        validator = QDoubleValidator(0.0, 1.0, 6)  # 增加精度到小数点后6位
        self.risk_free_rate_edit.setValidator(validator)
        
        risk_free_rate_layout.addWidget(risk_free_rate_label)
        risk_free_rate_layout.addWidget(self.risk_free_rate_edit)
        
        # 添加说明标签
        risk_free_rate_desc = QLabel("用于计算夏普比率、索提诺比率等指标（如0.03表示3%，支持小数点后6位精度）")
        risk_free_rate_desc.setStyleSheet("color: #A0A0A0; font-size: 12px;")
        
        # 添加延迟显示日志设置
        delay_log_layout = QHBoxLayout()
        delay_log_label = QLabel("延迟显示日志:")
        delay_log_label.setStyleSheet("color: #E0E0E0;")
        
        self.delay_log_checkbox = QCheckBox()
        self.delay_log_checkbox.setStyleSheet("""
            QCheckBox {
                color: #E0E0E0;
                spacing: 5px;
                background-color: transparent;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 2px solid #3D3D3D;
                border-radius: 3px;
                background-color: #2D2D2D;
            }
            QCheckBox::indicator:checked {
                background-color: #0078D7;
                border: 2px solid #0078D7;
            }
            QCheckBox::indicator:hover {
                border: 2px solid #5D5D5D;
            }
        """)
        # 从设置中读取延迟显示状态，如果不存在则使用默认值True
        delay_log_enabled = self.settings.value('delay_log_display', True, type=bool)
        self.delay_log_checkbox.setChecked(delay_log_enabled)
        
        delay_log_layout.addWidget(delay_log_label)
        delay_log_layout.addWidget(self.delay_log_checkbox)
        delay_log_layout.addStretch()
        
        # 添加说明标签
        delay_log_desc = QLabel("启用后，策略运行期间的日志将在策略完成后统一显示，提升性能并避免干扰")
        delay_log_desc.setStyleSheet("color: #A0A0A0; font-size: 12px;")

        # 添加最大日志显示行数设置
        max_log_lines_layout = QHBoxLayout()
        max_log_lines_label = QLabel("最大日志显示行数:")
        max_log_lines_label.setStyleSheet("color: #E0E0E0;")
        self.max_log_lines_edit = QLineEdit()
        self.max_log_lines_edit.setStyleSheet("""
            QLineEdit {
                border: 1px solid #3D3D3D;
                border-radius: 2px;
                padding: 5px;
                background-color: #2D2D2D;
                color: #E0E0E0;
            }
            QLineEdit:focus {
                border: 1px solid #5D5D5D;
            }
        """)
        self.max_log_lines_edit.setFixedWidth(100)
        # 从设置中读取最大日志行数，默认1000
        max_log_lines = self.settings.value('max_log_lines', 1000, type=int)
        self.max_log_lines_edit.setText(str(max_log_lines))

        # 设置验证器，只允许输入正整数
        from PyQt5.QtGui import QIntValidator
        self.max_log_lines_edit.setValidator(QIntValidator(100, 100000))

        max_log_lines_layout.addWidget(max_log_lines_label)
        max_log_lines_layout.addWidget(self.max_log_lines_edit)
        max_log_lines_layout.addStretch()

        # 添加说明标签
        max_log_lines_desc = QLabel("限制系统日志显示的最大行数，减少UI负担（范围: 100-100000）")
        max_log_lines_desc.setStyleSheet("color: #A0A0A0; font-size: 12px;")

        # 添加初始化行情数据设置
        init_data_layout = QHBoxLayout()
        init_data_label = QLabel("初始化行情数据:")
        init_data_label.setStyleSheet("color: #E0E0E0;")
        
        self.init_data_checkbox = QCheckBox()
        self.init_data_checkbox.setStyleSheet("""
            QCheckBox {
                color: #E0E0E0;
                spacing: 5px;
                background-color: transparent;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 2px solid #3D3D3D;
                border-radius: 3px;
                background-color: #2D2D2D;
            }
            QCheckBox::indicator:checked {
                background-color: #0078D7;
                border: 2px solid #0078D7;
            }
            QCheckBox::indicator:hover {
                border: 2px solid #5D5D5D;
            }
        """)
        # 从设置中读取初始化行情数据状态，默认关闭
        init_data_enabled = self.settings.value('init_data_enabled', False, type=bool)
        self.init_data_checkbox.setChecked(init_data_enabled)
        
        init_data_layout.addWidget(init_data_label)
        init_data_layout.addWidget(self.init_data_checkbox)
        init_data_layout.addStretch()
        
        # 添加说明标签
        init_data_desc = QLabel("启用后在启动时初始化行情数据连接")
        init_data_desc.setStyleSheet("color: #A0A0A0; font-size: 12px;")
        
        # 添加账户设置
        account_label = QLabel("账户设置:")
        account_label.setStyleSheet("color: #E0E0E0; font-weight: bold; margin-top: 10px;")
        
        # 账户名称设置
        account_id_layout = QHBoxLayout()
        account_id_label = QLabel("账户名称:")
        account_id_label.setStyleSheet("color: #E0E0E0;")
        self.account_id_input = QLineEdit()
        self.account_id_input.setStyleSheet("""
            QLineEdit {
                border: 1px solid #3D3D3D;
                border-radius: 2px;
                padding: 5px;
                background-color: #2D2D2D;
                color: #E0E0E0;
            }
            QLineEdit:focus {
                border: 1px solid #5D5D5D;
            }
        """)
        self.account_id_input.setText(self.settings.value('account_id', '8888888888'))
        self.account_id_input.setPlaceholderText("请输入账户名称")
        
        account_id_layout.addWidget(account_id_label)
        account_id_layout.addWidget(self.account_id_input)
        
        # 账户类型设置
        account_type_layout = QHBoxLayout()
        account_type_label = QLabel("账户类型:")
        account_type_label.setStyleSheet("color: #E0E0E0;")
        self.account_type_selector = QComboBox()
        self.account_type_selector.addItems(["STOCK", "CREDIT", "FUTURES"])
        self.account_type_selector.setStyleSheet("""
            QComboBox {
                border: 1px solid #3D3D3D;
                border-radius: 2px;
                padding: 5px;
                background-color: #2D2D2D;
                color: #E0E0E0;
            }
            QComboBox:focus {
                border: 1px solid #5D5D5D;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox::down-arrow {
                width: 12px;
                height: 12px;
            }
        """)
        self.account_type_selector.setCurrentText(self.settings.value('account_type', 'STOCK'))
        
        account_type_layout.addWidget(account_type_label)
        account_type_layout.addWidget(self.account_type_selector)
        
        basic_params_layout.addLayout(risk_free_rate_layout)
        basic_params_layout.addWidget(risk_free_rate_desc)
        basic_params_layout.addLayout(delay_log_layout)
        basic_params_layout.addWidget(delay_log_desc)
        basic_params_layout.addLayout(max_log_lines_layout)
        basic_params_layout.addWidget(max_log_lines_desc)
        basic_params_layout.addLayout(init_data_layout)
        basic_params_layout.addWidget(init_data_desc)
        basic_params_layout.addWidget(account_label)
        basic_params_layout.addLayout(account_id_layout)
        basic_params_layout.addLayout(account_type_layout)
        
        basic_params_group.setLayout(basic_params_layout)
        layout.addWidget(basic_params_group)
        
        # 股票列表管理组
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
        update_stock_list_btn.setObjectName("update_stock_list_btn")
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
        
        # 添加客户端路径设置
        client_label = QLabel("miniQMT客户端路径:")
        client_label.setStyleSheet("color: #E0E0E0;")
        path_layout.addWidget(client_label)
        
        input_layout = QHBoxLayout()
        self.client_path_edit = QLineEdit()
        self.client_path_edit.setStyleSheet("""
            QLineEdit {
                border: 1px solid #3D3D3D;
                border-radius: 2px;
                padding: 5px;
                background-color: #2D2D2D;
                color: #E0E0E0;
            }
            QLineEdit:focus {
                border: 1px solid #5D5D5D;
            }
        """)
        self.client_path_edit.setText(self.settings.value('client_path', ''))
        
        browse_button = QPushButton("浏览...")
        browse_button.setFixedWidth(80)
        browse_button.setStyleSheet("""
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
        browse_button.clicked.connect(self.browse_client)
        
        input_layout.addWidget(self.client_path_edit)
        input_layout.addWidget(browse_button)
        path_layout.addLayout(input_layout)
        
        # 添加QMT路径设置
        qmt_path_label = QLabel("miniQMT数据路径:")
        qmt_path_label.setStyleSheet("color: #E0E0E0; margin-top: 10px;")
        path_layout.addWidget(qmt_path_label)
        
        qmt_input_layout = QHBoxLayout()
        self.qmt_path_edit = QLineEdit()
        self.qmt_path_edit.setStyleSheet("""
            QLineEdit {
                border: 1px solid #3D3D3D;
                border-radius: 2px;
                padding: 5px;
                background-color: #2D2D2D;
                color: #E0E0E0;
            }
            QLineEdit:focus {
                border: 1px solid #5D5D5D;
            }
        """)
        self.qmt_path_edit.setText(self.settings.value('qmt_path', 'D:\\国金证券QMT交易端\\userdata_mini'))
        self.qmt_path_edit.setPlaceholderText("请选择QMT数据路径")
        
        qmt_browse_button = QPushButton("浏览...")
        qmt_browse_button.setFixedWidth(80)
        qmt_browse_button.setStyleSheet("""
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
        qmt_browse_button.clicked.connect(self.browse_qmt_path)
        
        qmt_input_layout.addWidget(self.qmt_path_edit)
        qmt_input_layout.addWidget(qmt_browse_button)
        path_layout.addLayout(qmt_input_layout)
        
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
        # 获取版本信息
        version_info = get_version_info()
        version_label = QLabel(f"当前版本：v{version_info['version']}")
        version_label.setStyleSheet("color: #E0E0E0;")
        version_layout.addWidget(version_label)
        # 添加构建日期信息
        if 'build_date' in version_info:
            build_date_label = QLabel(f"构建日期：{version_info['build_date']}")
            build_date_label.setStyleSheet("color: #E0E0E0;")
            version_layout.addWidget(build_date_label)
        # 添加更新通道信息
        if 'channel' in version_info:
            channel_label = QLabel(f"更新通道：{version_info['channel']}")
            channel_label.setStyleSheet("color: #E0E0E0;")
            version_layout.addWidget(channel_label)
        
        version_group.setLayout(version_layout)
        layout.addWidget(version_group)
        
        # 底部按钮布局
        button_layout = QHBoxLayout()
        
        # 添加反馈问题按钮（靠左）
        feedback_button = QPushButton("反馈问题")
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
            # 保存客户端路径
            client_path = self.client_path_edit.text().strip()
            if client_path and not os.path.exists(client_path):
                QMessageBox.warning(self, "警告", "指定的客户端路径不存在")
                return
                
            self.settings.setValue('client_path', client_path)
            
            # 保存无风险收益率
            risk_free_rate = self.risk_free_rate_edit.text().strip()
            try:
                risk_free_rate_value = float(risk_free_rate)
                if risk_free_rate_value < 0 or risk_free_rate_value > 1:
                    QMessageBox.warning(self, "警告", "无风险收益率应在0到1之间")
                    return
                self.settings.setValue('risk_free_rate', risk_free_rate)
            except ValueError:
                QMessageBox.warning(self, "警告", "无风险收益率必须是有效的数字")
                return
                
            # 保存延迟显示日志状态
            delay_log_enabled = self.delay_log_checkbox.isChecked()
            self.settings.setValue('delay_log_display', delay_log_enabled)

            # 保存最大日志显示行数
            max_log_lines_text = self.max_log_lines_edit.text().strip()
            try:
                max_log_lines = int(max_log_lines_text)
                if max_log_lines < 100 or max_log_lines > 100000:
                    QMessageBox.warning(self, "警告", "最大日志显示行数应在100到100000之间")
                    return
                self.settings.setValue('max_log_lines', max_log_lines)
            except ValueError:
                QMessageBox.warning(self, "警告", "最大日志显示行数必须是有效的整数")
                return

            # 保存初始化行情数据状态
            init_data_enabled = self.init_data_checkbox.isChecked()
            self.settings.setValue('init_data_enabled', init_data_enabled)
            
            # 保存账户设置
            account_id = self.account_id_input.text().strip()
            self.settings.setValue('account_id', account_id)
            
            account_type = self.account_type_selector.currentText()
            self.settings.setValue('account_type', account_type)
            
            # 保存QMT路径
            qmt_path = self.qmt_path_edit.text().strip()
            self.settings.setValue('qmt_path', qmt_path)
            
            QMessageBox.information(self, "成功", "设置已保存")
            # 保存成功后关闭对话框
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存设置时出错: {str(e)}")

    def open_feedback_page(self):
        """打开反馈问题页面"""
        url = "https://khsci.com/khQuant/forum/"
        webbrowser.open(url)
        
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
            
            # 获取更新管理器（多进程版本）
            update_manager = get_and_save_stock_list(data_dir)
            
            # 连接信号
            update_manager.progress.connect(self.show_update_progress)
            update_manager.finished.connect(self.handle_update_finished)
            
            # 启动多进程更新
            update_manager.start()
            
            # 保存管理器引用
            self.update_manager = update_manager
            
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
        
        # 清理管理器
        if hasattr(self, 'update_manager'):
            self.update_manager.stop()
            self.update_manager = None
            
        # 恢复按钮
        update_stock_list_btn = self.findChild(QPushButton, "update_stock_list_btn")
        if update_stock_list_btn:
            update_stock_list_btn.setEnabled(True)

    def browse_qmt_path(self):
        """浏览选择QMT路径"""
        qmt_path = QFileDialog.getExistingDirectory(
            self,
            "选择QMT数据路径",
            self.qmt_path_edit.text()
        )
        if qmt_path:
            self.qmt_path_edit.setText(qmt_path) 