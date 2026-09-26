import base64
import os
import sys
import logging
import webbrowser
from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QGroupBox, QPushButton, QLineEdit, QFileDialog,
    QMessageBox, QProgressDialog, QCheckBox, QComboBox,
    QTabWidget, QWidget, QDoubleSpinBox, QSpacerItem, QSizePolicy,
    QScrollArea, QGridLayout
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSettings
from PyQt5.QtGui import QFont, QIcon, QDoubleValidator, QIntValidator

from khQTTools import get_and_save_stock_list
from khPathUtils import get_stock_pool_write_dir
from khUiScale import get_ui_font_scale, get_platform_ui_metrics
from kh_app_identity import QT_APP, QT_ORG, default_duckdb_dir
from kh_data_dir_policy import SHARED_WRITE_WARNING, claim_if_new, may_be_shared_with_cs
from data_integrity_policy import normalize_integrity_mode
from performance_config import DEFAULT_PERFORMANCE_CONFIG
from performance_config import PERFORMANCE_PRESETS, normalize_performance_preset, performance_preset_settings
from qt_settings_bridge import KhQtSettings
from tushare_config import normalize_tushare_api_url

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
            "app_name": "看海量化回测平台（开源版）"
        }


def _format_bytes(value):
    try:
        size = float(value or 0)
    except Exception:
        size = 0.0
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.2f} {units[idx]}"


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    """忽略鼠标滚轮，避免滚动设置页时误改数值。"""

    def wheelEvent(self, event):
        event.ignore()


class NoWheelComboBox(QComboBox):
    """忽略鼠标滚轮，避免滚动设置页时误切选项。"""

    def wheelEvent(self, event):
        event.ignore()


class ParquetCacheBuildThread(QThread):
    finished_signal = pyqtSignal(dict)
    error_signal = pyqtSignal(str)

    def __init__(
        self,
        config_path,
        data_root,
        pack_root=None,
        batch_size=None,
        workers=None,
        compression=None,
        performance_overrides=None,
    ):
        super().__init__()
        self.config_path = config_path
        self.data_root = data_root
        self.pack_root = pack_root
        self.batch_size = batch_size
        self.workers = workers
        self.compression = compression
        self.performance_overrides = performance_overrides or {}

    def run(self):
        try:
            from duckdb_storage.parquet_cache_pack import build_pack_for_config

            result = build_pack_for_config(
                self.config_path,
                data_root=self.data_root,
                pack_root=self.pack_root,
                batch_size=self.batch_size,
                workers=self.workers,
                compression=self.compression,
                performance_overrides=self.performance_overrides,
            )
            self.finished_signal.emit(result)
        except Exception as exc:
            self.error_signal.emit(str(exc))


class SettingsDialog(QDialog):
    """设置对话框类"""

    @staticmethod
    def _normalize_and_create_duckdb_path(path_text):
        """规范化 DuckDB 数据目录；不存在时自动创建。"""
        duckdb_path = (path_text or "").strip()
        if not duckdb_path:
            return "", False
        duckdb_path = os.path.abspath(os.path.expanduser(duckdb_path))
        created = False
        if not os.path.exists(duckdb_path):
            os.makedirs(duckdb_path, exist_ok=True)
            created = True
        if not os.path.isdir(duckdb_path):
            raise NotADirectoryError(f"路径不是目录: {duckdb_path}")
        return duckdb_path, created
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = KhQtSettings(QT_ORG, QT_APP)
        self.font_scale = get_ui_font_scale(self.settings)
        self.ui_metrics = get_platform_ui_metrics(self.font_scale)
        self.small_font_size = max(11, int(12 * self.font_scale))
        self._initial_ui_font_scale = self.settings.value('ui_font_scale', 0.0, type=float)
        self.confirmed_exit = False
        self.shared_settings_cache = self.settings.load()
        
        # 设置窗口标志
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setWindowModality(Qt.ApplicationModal)
        
        self.initUI()
    
    def initUI(self):
        """设置对话框UI初始化"""
        self.setWindowTitle('软件设置')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        screen = QApplication.primaryScreen()
        default_width, default_height = self.ui_metrics["settings_default_size"]
        minimum_width = int(self.ui_metrics["settings_min_width"])
        minimum_height = 560
        if screen is not None:
            available = screen.availableGeometry()
            horizontal_margin = 40
            vertical_margin = 60
            max_width = max(500, available.width() - horizontal_margin)
            max_height = max(minimum_height, available.height() - vertical_margin)
            self.setMinimumWidth(min(minimum_width, max_width))
            self.setMinimumHeight(min(minimum_height, max_height))
            self.resize(
                min(max(default_width, self.minimumWidth()), max_width),
                min(max(default_height, self.minimumHeight()), max_height),
            )
        else:
            self.setMinimumWidth(minimum_width)
            self.setMinimumHeight(minimum_height)
            self.resize(default_width, default_height)
        self._apply_native_titlebar_theme()
        
        # 主布局
        layout = QVBoxLayout(self)
        layout.setSpacing(self.ui_metrics["settings_layout_spacing"])

        tab_padding_v = self.ui_metrics["settings_tab_padding_v"]
        tab_padding_h = self.ui_metrics["settings_tab_padding_h"]

        # 创建标签页控件
        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 1px solid #505050;
                background-color: #333333;
                border-radius: 3px;
            }}
            QTabBar::tab {{
                background-color: #404040;
                color: #E0E0E0;
                padding: {tab_padding_v}px {tab_padding_h}px;
                border: 1px solid #505050;
                border-bottom: none;
                border-top-left-radius: 3px;
                border-top-right-radius: 3px;
                margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background-color: #333333;
                color: #FFFFFF;
                border-bottom: 1px solid #333333;
            }}
            QTabBar::tab:hover {{
                background-color: #505050;
            }}
        """)

        # 创建第一个标签页（基本设置）
        basic_tab = QWidget()
        basic_tab_layout = QVBoxLayout(basic_tab)
        basic_tab_layout.setContentsMargins(12, 12, 12, 12)
        basic_tab_layout.setSpacing(12)

        # 创建第二个标签页（数据设置）
        data_tab = QWidget()
        data_tab_layout = QVBoxLayout(data_tab)
        data_tab_layout.setContentsMargins(12, 12, 12, 12)
        data_tab_layout.setSpacing(12)

        performance_tab = QWidget()
        performance_tab_layout = QVBoxLayout(performance_tab)
        performance_tab_layout.setContentsMargins(12, 12, 12, 12)
        performance_tab_layout.setSpacing(12)

        # 添加标签页到TabWidget
        self.tab_widget.addTab(self._wrap_tab_content(basic_tab), "基本设置")
        self.tab_widget.addTab(self._wrap_tab_content(data_tab), "数据设置")
        self.tab_widget.addTab(self._wrap_tab_content(performance_tab), "回测性能")

        # 将TabWidget添加到主布局
        layout.addWidget(self.tab_widget, 1)

        # ============ 第一个标签页：基本设置 ============

        # 添加基本参数设置组
        basic_params_group = QGroupBox("基本参数设置")
        basic_params_group.setStyleSheet("""
            QGroupBox {
                border: 1px solid #505050;
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
                border: 1px solid #505050;
                border-radius: 2px;
                padding: 5px;
                background-color: #404040;
                color: #E0E0E0;
            }
            QLineEdit:focus {
                border: 1px solid #606060;
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
        risk_free_rate_desc.setStyleSheet(f"color: #A0A0A0; font-size: {self.small_font_size}px;")
        
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
                border: 2px solid #505050;
                border-radius: 3px;
                background-color: #404040;
            }
            QCheckBox::indicator:checked {
                background-color: #0078D7;
                border: 2px solid #0078D7;
            }
            QCheckBox::indicator:hover {
                border: 2px solid #606060;
            }
        """)
        # 从设置中读取延迟显示状态，如果不存在则默认启用
        delay_log_enabled = self.settings.value('delay_log_display', True, type=bool)
        self.delay_log_checkbox.setChecked(delay_log_enabled)
        
        delay_log_layout.addWidget(delay_log_label)
        delay_log_layout.addWidget(self.delay_log_checkbox)
        delay_log_layout.addStretch()
        
        # 添加说明标签
        delay_log_desc = QLabel("启用后，策略运行期间的日志将在策略完成后统一显示，提升性能并避免干扰")
        delay_log_desc.setStyleSheet(f"color: #A0A0A0; font-size: {self.small_font_size}px;")

        # 添加最大日志显示行数设置
        max_log_lines_layout = QHBoxLayout()
        max_log_lines_label = QLabel("最大日志显示行数:")
        max_log_lines_label.setStyleSheet("color: #E0E0E0;")
        self.max_log_lines_edit = QLineEdit()
        self.max_log_lines_edit.setStyleSheet("""
            QLineEdit {
                border: 1px solid #505050;
                border-radius: 2px;
                padding: 5px;
                background-color: #404040;
                color: #E0E0E0;
            }
            QLineEdit:focus {
                border: 1px solid #606060;
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
        max_log_lines_desc.setStyleSheet(f"color: #A0A0A0; font-size: {self.small_font_size}px;")

        # 添加界面字号倍率设置
        font_scale_layout = QHBoxLayout()
        font_scale_label = QLabel("界面字号倍率:")
        font_scale_label.setStyleSheet("color: #E0E0E0;")
        self.font_scale_spin = NoWheelDoubleSpinBox()
        self.font_scale_spin.setRange(0.0, 2.0)
        self.font_scale_spin.setSingleStep(0.1)
        self.font_scale_spin.setDecimals(2)
        self.font_scale_spin.setSpecialValueText("自动")
        self.font_scale_spin.setFixedWidth(120)
        self.font_scale_spin.setStyleSheet("""
            QDoubleSpinBox {
                border: 1px solid #505050;
                border-radius: 2px;
                padding: 5px;
                background-color: #404040;
                color: #E0E0E0;
            }
            QDoubleSpinBox:focus {
                border: 1px solid #606060;
            }
        """)
        font_scale_value = self.settings.value('ui_font_scale', 0.0, type=float)
        self.font_scale_spin.setValue(font_scale_value if font_scale_value is not None else 0.0)
        self.font_scale_spin.setToolTip("0表示自动（按分辨率），1.0=100%")
        self.font_scale_spin.valueChanged.connect(self._persist_ui_font_scale)

        font_scale_layout.addWidget(font_scale_label)
        font_scale_layout.addWidget(self.font_scale_spin)
        font_scale_layout.addStretch()

        font_scale_desc = QLabel("0为自动，1.0=100%，建议范围 0.8~1.6")
        font_scale_desc.setStyleSheet(f"color: #A0A0A0; font-size: {self.small_font_size}px;")

        # 添加停止后直接退出设置
        stop_exit_layout = QHBoxLayout()
        stop_exit_label = QLabel("停止后直接退出:")
        stop_exit_label.setStyleSheet("color: #E0E0E0;")

        self.stop_exit_checkbox = QCheckBox()
        self.stop_exit_checkbox.setStyleSheet("""
            QCheckBox {
                color: #E0E0E0;
                spacing: 5px;
                background-color: transparent;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 2px solid #505050;
                border-radius: 3px;
                background-color: #404040;
            }
            QCheckBox::indicator:checked {
                background-color: #0078D7;
                border: 2px solid #0078D7;
            }
            QCheckBox::indicator:hover {
                border: 2px solid #606060;
            }
        """)
        # 从设置中读取停止后直接退出状态，默认True
        stop_exit_enabled = self.settings.value('stop_exit_immediately', True, type=bool)
        self.stop_exit_checkbox.setChecked(stop_exit_enabled)

        stop_exit_layout.addWidget(stop_exit_label)
        stop_exit_layout.addWidget(self.stop_exit_checkbox)
        stop_exit_layout.addStretch()

        # 添加说明标签
        stop_exit_desc = QLabel("启用后点击停止按钮将直接停止，不显示运行报告，不保存回测记录")
        stop_exit_desc.setStyleSheet(f"color: #A0A0A0; font-size: {self.small_font_size}px;")

        # 添加数据完整性检查设置
        integrity_check_layout = QHBoxLayout()
        integrity_check_label = QLabel("回测前数据检查:")
        integrity_check_label.setStyleSheet("color: #E0E0E0;")

        self.integrity_check_combo = QComboBox()
        self.integrity_check_combo.setStyleSheet("""
            QComboBox {
                border: 1px solid #505050;
                border-radius: 2px;
                padding: 5px;
                background-color: #404040;
                color: #E0E0E0;
                min-width: 180px;
            }
            QComboBox QAbstractItemView {
                background-color: #404040;
                color: #E0E0E0;
                selection-background-color: #505050;
            }
        """)
        self.integrity_check_combo.addItem("自动（推荐）", "auto")
        self.integrity_check_combo.addItem("每次全量检查", "full")
        self.integrity_check_combo.addItem("关闭检查", "off")

        # 兼容旧 check_data_integrity=True/False，同时读取新的 auto/full/off 模式。
        integrity_check_enabled = self.settings.value('check_data_integrity', True, type=bool)
        integrity_check_mode = normalize_integrity_mode(
            self.settings.value('check_data_integrity_mode', 'auto'),
            legacy_enabled=integrity_check_enabled,
        )
        integrity_mode_idx = self.integrity_check_combo.findData(integrity_check_mode)
        self.integrity_check_combo.setCurrentIndex(max(0, integrity_mode_idx))

        integrity_check_layout.addWidget(integrity_check_label)
        integrity_check_layout.addWidget(self.integrity_check_combo)
        integrity_check_layout.addStretch()

        # 添加说明标签
        integrity_check_desc = QLabel(
            "自动模式会保留小策略的跑前数据检查；遇到全市场分钟线等大规模回测时自动跳过耗时扫描，"
            "由回测过程中的空数据/缺历史数据汇总提示接管。"
        )
        integrity_check_desc.setStyleSheet(f"color: #A0A0A0; font-size: {self.small_font_size}px;")

        # ===== 成交量限制设置 =====
        volume_limit_layout = QHBoxLayout()
        volume_limit_label = QLabel("启用成交量限制:")
        volume_limit_label.setStyleSheet("color: #E0E0E0;")

        self.volume_limit_checkbox = QCheckBox()
        self.volume_limit_checkbox.setStyleSheet("""
            QCheckBox {
                color: #E0E0E0;
                spacing: 5px;
                background-color: transparent;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 2px solid #505050;
                border-radius: 3px;
                background-color: #404040;
            }
            QCheckBox::indicator:checked {
                background-color: #0078D7;
                border: 2px solid #0078D7;
            }
            QCheckBox::indicator:hover {
                border: 2px solid #606060;
            }
        """)
        # 从设置中读取，默认False
        volume_limit_enabled = self.settings.value('volume_limit_enabled', False, type=bool)
        self.volume_limit_checkbox.setChecked(volume_limit_enabled)
        self.volume_limit_checkbox.stateChanged.connect(self.on_volume_limit_toggled)

        volume_limit_layout.addWidget(volume_limit_label)
        volume_limit_layout.addWidget(self.volume_limit_checkbox)
        volume_limit_layout.addStretch()

        # 说明标签
        volume_limit_desc = QLabel("限制单笔订单不超过当前Bar成交量的一定比例，适用于大资金策略模拟市场冲击")
        volume_limit_desc.setStyleSheet(f"color: #A0A0A0; font-size: {self.small_font_size}px;")

        for description_label in (
            risk_free_rate_desc,
            delay_log_desc,
            max_log_lines_desc,
            font_scale_desc,
            stop_exit_desc,
            integrity_check_desc,
            volume_limit_desc,
        ):
            description_label.setWordWrap(True)
            description_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        # 参与率设置（缩进，只有启用时才可编辑）
        participation_rate_layout = QHBoxLayout()
        participation_rate_layout.addSpacing(20)  # 缩进
        self.participation_rate_label = QLabel("市场参与率:")
        self.participation_rate_label.setStyleSheet("color: #E0E0E0;")
        self.participation_rate_edit = QLineEdit()
        self.participation_rate_edit.setStyleSheet("""
            QLineEdit {
                border: 1px solid #505050;
                border-radius: 2px;
                padding: 5px;
                background-color: #404040;
                color: #E0E0E0;
            }
            QLineEdit:focus {
                border: 1px solid #606060;
            }
        """)
        self.participation_rate_edit.setFixedWidth(100)
        # 从设置中读取，默认0.1
        participation_rate = self.settings.value('participation_rate', 0.1, type=float)
        self.participation_rate_edit.setText(str(participation_rate * 100))  # 转换为百分比显示
        self.participation_rate_edit.setValidator(QDoubleValidator(0.1, 100.0, 2))

        participation_rate_layout.addWidget(self.participation_rate_label)
        participation_rate_layout.addWidget(self.participation_rate_edit)
        self.participation_rate_percent_label = QLabel("%")
        self.participation_rate_percent_label.setStyleSheet("color: #E0E0E0;")
        participation_rate_layout.addWidget(self.participation_rate_percent_label)
        participation_rate_layout.addStretch()

        # 允许部分成交设置（缩进）
        allow_partial_layout = QHBoxLayout()
        allow_partial_layout.addSpacing(20)
        self.allow_partial_label = QLabel("允许部分成交:")
        self.allow_partial_label.setStyleSheet("color: #E0E0E0;")

        self.allow_partial_checkbox = QCheckBox()
        self.allow_partial_checkbox.setStyleSheet("""
            QCheckBox {
                color: #E0E0E0;
                spacing: 5px;
                background-color: transparent;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 2px solid #505050;
                border-radius: 3px;
                background-color: #404040;
            }
            QCheckBox::indicator:checked {
                background-color: #0078D7;
                border: 2px solid #0078D7;
            }
            QCheckBox::indicator:hover {
                border: 2px solid #606060;
            }
        """)
        allow_partial_enabled = self.settings.value('allow_partial_fill', True, type=bool)
        self.allow_partial_checkbox.setChecked(allow_partial_enabled)

        allow_partial_layout.addWidget(self.allow_partial_label)
        allow_partial_layout.addWidget(self.allow_partial_checkbox)
        allow_partial_layout.addStretch()

        # ===== 动态数据加载设置 (已禁用) =====
        # 注释说明：动态加载功能已设为内部功能，不再提供UI配置选项
        # 如需启用，请在配置文件中手动添加 "dynamic_load" 部分
        # # 动态数据加载开关
        # dynamic_load_layout = QHBoxLayout()
        # dynamic_load_label = QLabel("动态数据加载:")
        # dynamic_load_label.setStyleSheet("color: #E0E0E0;")
        #
        # self.dynamic_load_checkbox = QCheckBox()
        # self.dynamic_load_checkbox.setStyleSheet("""
        #     QCheckBox {
        #         color: #E0E0E0;
        #         spacing: 5px;
        #         background-color: transparent;
        #     }
        #     QCheckBox::indicator {
        #         width: 16px;
        #         height: 16px;
        #         border: 2px solid #3D3D3D;
        #         border-radius: 3px;
        #         background-color: #2D2D2D;
        #     }
        #     QCheckBox::indicator:checked {
        #         background-color: #0078D7;
        #         border: 2px solid #0078D7;
        #     }
        #     QCheckBox::indicator:hover {
        #         border: 2px solid #5D5D5D;
        #     }
        # """)
        # # 从设置中读取动态加载状态，默认False
        # dynamic_load_enabled = self.settings.value('dynamic_load_enabled', False, type=bool)
        # self.dynamic_load_checkbox.setChecked(dynamic_load_enabled)
        # # 连接信号，控制分段大小输入框的启用状态
        # self.dynamic_load_checkbox.stateChanged.connect(self._on_dynamic_load_changed)
        #
        # dynamic_load_layout.addWidget(dynamic_load_label)
        # dynamic_load_layout.addWidget(self.dynamic_load_checkbox)
        # dynamic_load_layout.addStretch()
        #
        # # 动态加载说明
        # dynamic_load_desc = QLabel("启用后，回测时按时段分批加载数据，降低内存占用（适用于大数据量回测）")
        # dynamic_load_desc.setStyleSheet("color: #A0A0A0; font-size: 12px;")
        #
        # # 分段大小设置
        # chunk_size_layout = QHBoxLayout()
        # chunk_size_label = QLabel("  分段大小(天):")
        # chunk_size_label.setStyleSheet("color: #E0E0E0;")
        #
        # self.chunk_size_edit = QLineEdit()
        # self.chunk_size_edit.setStyleSheet("""
        #     QLineEdit {
        #         border: 1px solid #3D3D3D;
        #         border-radius: 2px;
        #         padding: 5px;
        #         background-color: #2D2D2D;
        #         color: #E0E0E0;
        #     }
        #     QLineEdit:focus {
        #         border: 1px solid #5D5D5D;
        #     }
        #     QLineEdit:disabled {
        #         background-color: #1A1A1A;
        #         color: #666666;
        #     }
        # """)
        # self.chunk_size_edit.setFixedWidth(80)
        # # 从设置中读取分段大小，默认5天
        # chunk_size = self.settings.value('dynamic_load_chunk_size', 5, type=int)
        # self.chunk_size_edit.setText(str(chunk_size))
        # # 设置验证器，只允许输入1-365的整数
        # from PyQt5.QtGui import QIntValidator
        # self.chunk_size_edit.setValidator(QIntValidator(1, 365))
        # # 根据动态加载开关状态设置启用/禁用
        # self.chunk_size_edit.setEnabled(dynamic_load_enabled)
        #
        # chunk_size_layout.addWidget(chunk_size_label)
        # chunk_size_layout.addWidget(self.chunk_size_edit)
        # chunk_size_layout.addStretch()
        #
        # chunk_size_desc = QLabel("  每次加载的交易日数量（建议: tick数据1天，分钟数据5天，日线数据60天）")
        # chunk_size_desc.setStyleSheet("color: #A0A0A0; font-size: 12px;")
        # ===== 动态数据加载设置结束 =====

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
                border: 1px solid #505050;
                border-radius: 2px;
                padding: 5px;
                background-color: #404040;
                color: #E0E0E0;
            }
            QLineEdit:focus {
                border: 1px solid #606060;
            }
        """)
        self.account_id_input.setText(self.settings.value('account_id', ''))
        self.account_id_input.setPlaceholderText("请输入账户名称")
        self.account_id_input.setMaximumWidth(360)
        self.account_id_input.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        
        account_id_layout.addWidget(account_id_label)
        account_id_layout.addWidget(self.account_id_input)
        account_id_layout.addStretch()
        
        # 账户类型设置
        account_type_layout = QHBoxLayout()
        account_type_label = QLabel("账户类型:")
        account_type_label.setStyleSheet("color: #E0E0E0;")
        self.account_type_selector = NoWheelComboBox()
        self.account_type_selector.addItems(["STOCK"])
        self.account_type_selector.setFixedWidth(180)
        self.account_type_selector.setStyleSheet("""
            QComboBox {
                border: 1px solid #505050;
                border-radius: 2px;
                padding: 5px;
                background-color: #404040;
                color: #E0E0E0;
            }
            QComboBox:focus {
                border: 1px solid #606060;
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
        account_type_layout.addStretch()
        
        basic_params_layout.addLayout(risk_free_rate_layout)
        basic_params_layout.addWidget(risk_free_rate_desc)
        basic_params_layout.addLayout(delay_log_layout)
        basic_params_layout.addWidget(delay_log_desc)
        basic_params_layout.addLayout(max_log_lines_layout)
        basic_params_layout.addWidget(max_log_lines_desc)
        basic_params_layout.addLayout(font_scale_layout)
        basic_params_layout.addWidget(font_scale_desc)
        basic_params_layout.addLayout(stop_exit_layout)
        basic_params_layout.addWidget(stop_exit_desc)
        basic_params_layout.addLayout(integrity_check_layout)
        basic_params_layout.addWidget(integrity_check_desc)
        # 成交量限制设置
        basic_params_layout.addLayout(volume_limit_layout)
        basic_params_layout.addWidget(volume_limit_desc)
        basic_params_layout.addLayout(participation_rate_layout)
        basic_params_layout.addLayout(allow_partial_layout)
        # 动态数据加载设置已禁用，不再添加到界面
        # basic_params_layout.addLayout(dynamic_load_layout)
        # basic_params_layout.addWidget(dynamic_load_desc)
        # basic_params_layout.addLayout(chunk_size_layout)
        # basic_params_layout.addWidget(chunk_size_desc)
        # 账户设置
        basic_params_layout.addWidget(account_label)
        basic_params_layout.addLayout(account_id_layout)
        basic_params_layout.addLayout(account_type_layout)
        
        basic_params_group.setLayout(basic_params_layout)
        basic_tab_layout.addWidget(basic_params_group)

        # 股票列表管理组
        stock_list_group = QGroupBox("股票列表管理")
        stock_list_group.setStyleSheet("""
            QGroupBox {
                border: 1px solid #505050;
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

        update_stock_list_btn = QPushButton("更新成分股列表")
        update_stock_list_btn.setObjectName("update_stock_list_btn")
        update_stock_list_btn.setMinimumHeight(34)
        update_stock_list_btn.setMaximumWidth(260)
        update_stock_list_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        update_stock_list_btn.setToolTip(
            "通过 BaoStock 更新本地股票池和主要指数成分股（随包的基金、转债列表保持不变）。\n"
            "运行时需耐心等待，无需频繁更新。"
        )
        update_stock_list_btn.clicked.connect(self.update_stock_list)
        stock_list_layout.addWidget(update_stock_list_btn, 0, Qt.AlignLeft)

        stock_list_hint = QLabel(
            "通过 BaoStock 更新 A 股与主要指数成分股，免费、不用账号；"
            "随包的场内基金和转债列表保持不变。"
        )
        stock_list_hint.setWordWrap(True)
        stock_list_hint.setStyleSheet(f"color: #A0A0A0; font-size: {self.small_font_size}px;")
        stock_list_layout.addWidget(stock_list_hint)

        stock_list_group.setLayout(stock_list_layout)
        basic_tab_layout.addWidget(stock_list_group)

        # 版本信息组
        version_group = QGroupBox("版本信息")
        version_group.setStyleSheet("""
            QGroupBox {
                border: 1px solid #505050;
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

        version_group.setLayout(version_layout)
        basic_tab_layout.addWidget(version_group)

        # 在第一个标签页添加弹性空间
        basic_tab_layout.addStretch()

        # ============ 第二个标签页：数据设置 ============

        # 客户端路径设置组
        client_group = QGroupBox("数据设置")
        client_group.setStyleSheet("""
            QGroupBox {
                border: 1px solid #505050;
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

        # 添加DuckDB数据路径设置
        duckdb_path_label = QLabel("DuckDB数据路径:")
        duckdb_path_label.setStyleSheet("color: #E0E0E0; margin-top: 5px;")
        path_layout.addWidget(duckdb_path_label)

        duckdb_input_layout = QHBoxLayout()
        self.duckdb_path_edit = QLineEdit()
        self.duckdb_path_edit.setStyleSheet("""
            QLineEdit {
                border: 1px solid #505050;
                border-radius: 2px;
                padding: 5px;
                background-color: #404040;
                color: #E0E0E0;
            }
            QLineEdit:focus {
                border: 1px solid #606060;
            }
        """)
        # 从设置中读取DuckDB数据路径；没设置过时显示开源版默认目录
        self._initial_duckdb_path = self.settings.value('duckdb_data_path', '') or ''
        self.duckdb_path_edit.setText(self._initial_duckdb_path or default_duckdb_dir())
        self.duckdb_path_edit.setPlaceholderText("请选择DuckDB数据存储路径")

        duckdb_browse_button = QPushButton("浏览...")
        duckdb_browse_button.setFixedWidth(80)
        duckdb_browse_button.setStyleSheet("""
            QPushButton {
                background-color: #505050;
                color: #E0E0E0;
                border: none;
                padding: 5px 15px;
                border-radius: 2px;
            }
            QPushButton:hover {
                background-color: #606060;
            }
        """)
        duckdb_browse_button.clicked.connect(self.browse_duckdb_path)

        duckdb_input_layout.addWidget(self.duckdb_path_edit)
        duckdb_input_layout.addWidget(duckdb_browse_button)
        path_layout.addLayout(duckdb_input_layout)

        # 添加DuckDB路径说明
        duckdb_desc = QLabel(
            "开源版只读这个目录里的 DuckDB 数据回测。默认 %LOCALAPPDATA%\\KhQuantOS\\khData，"
            "建议换到非系统盘。不要和 CS 版共用同一个目录：两边会互相锁库，可以用下面的按钮复制一份 CS 数据。"
        )
        duckdb_desc.setWordWrap(True)
        duckdb_desc.setStyleSheet(f"color: #A0A0A0; font-size: {self.small_font_size}px;")
        path_layout.addWidget(duckdb_desc)

        copy_cs_button = QPushButton("复制 CS 数据…")
        copy_cs_button.setStyleSheet(duckdb_browse_button.styleSheet())
        copy_cs_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        copy_cs_button.setToolTip("把看海量化 CS 版的数据复制一份到开源版的目录，之后两边互不锁库")
        copy_cs_button.clicked.connect(self._open_data_copy_dialog)
        path_layout.addWidget(copy_cs_button, 0, Qt.AlignLeft)

        client_group.setLayout(path_layout)
        data_tab_layout.addWidget(client_group)

        # ============ 回测性能设置 ============
        perf_group = QGroupBox("回测性能设置")
        perf_group.setStyleSheet("""
            QGroupBox {
                border: 1px solid #505050;
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
        perf_layout = QVBoxLayout(perf_group)
        perf_layout.setSpacing(10)

        combo_style = """
            QComboBox {
                border: 1px solid #505050;
                border-radius: 2px;
                padding: 5px;
                background-color: #404040;
                color: #E0E0E0;
                min-width: 180px;
            }
            QComboBox QAbstractItemView {
                background-color: #404040;
                color: #E0E0E0;
                selection-background-color: #505050;
            }
        """
        perf_lineedit_style = """
            QLineEdit {
                border: 1px solid #505050;
                border-radius: 2px;
                padding: 5px;
                background-color: #404040;
                color: #E0E0E0;
            }
            QLineEdit:focus {
                border: 1px solid #606060;
            }
        """
        help_style = f"color: #A0A0A0; font-size: {self.small_font_size}px;"

        def _set_combo_value(combo, value):
            idx = combo.findData(value)
            if idx < 0:
                idx = 0
            combo.setCurrentIndex(idx)

        def _add_desc(parent_layout, text):
            desc = QLabel(text)
            desc.setStyleSheet(help_style)
            desc.setWordWrap(True)
            parent_layout.addWidget(desc)
            return desc

        def _add_combo_row(parent_layout, label_text, attr_name, items, current_value, desc_text):
            row = QHBoxLayout()
            label = QLabel(label_text)
            label.setStyleSheet("color: #E0E0E0;")
            combo = QComboBox()
            combo.setStyleSheet(combo_style)
            for text, value in items:
                combo.addItem(text, value)
            _set_combo_value(combo, current_value)
            setattr(self, attr_name, combo)
            row.addWidget(label)
            row.addWidget(combo)
            row.addStretch()
            parent_layout.addLayout(row)
            _add_desc(parent_layout, desc_text)
            return combo

        preset_row = QHBoxLayout()
        preset_label = QLabel("数据加载模式:")
        preset_label.setStyleSheet("color: #E0E0E0; font-weight: bold;")
        self.perf_preset_combo = QComboBox()
        self.perf_preset_combo.setStyleSheet(combo_style)
        # 文案统一取自 performance_config(单一来源, 防止与数据层漂移); 顺序: 智能(推荐)/全量/省内存
        self.perf_preset_combo.addItem(PERFORMANCE_PRESETS["balanced"]["label"], "balanced")
        self.perf_preset_combo.addItem(PERFORMANCE_PRESETS["performance"]["label"], "performance")
        self.perf_preset_combo.addItem(PERFORMANCE_PRESETS["low_memory"]["label"], "low_memory")
        current_preset = normalize_performance_preset(
            self.shared_settings_cache.get("performance_preset", DEFAULT_PERFORMANCE_CONFIG.get("preset", "balanced"))
        )
        _set_combo_value(self.perf_preset_combo, current_preset)
        preset_row.addWidget(preset_label)
        preset_row.addWidget(self.perf_preset_combo)
        preset_row.addStretch()
        perf_layout.addLayout(preset_row)

        self.perf_preset_desc = QLabel("")
        self.perf_preset_desc.setStyleSheet(help_style)
        self.perf_preset_desc.setWordWrap(True)
        perf_layout.addWidget(self.perf_preset_desc)

        preset_hint = QLabel(
            "普通用户只需要选择上面的三档模式。下方详细设置用于进阶微调；选择模式时会自动填充详细参数，Parquet 缓存保持独立设置。"
        )
        preset_hint.setStyleSheet(help_style)
        preset_hint.setWordWrap(True)
        perf_layout.addWidget(preset_hint)

        details_btn_style = """
            QPushButton { background-color: #454545; color: #E0E0E0; border: 1px solid #606060; padding: 7px 12px; border-radius: 2px; text-align: left; }
            QPushButton:hover { background-color: #505050; }
        """
        self.perf_details_toggle = QPushButton("显示详细设置")
        self.perf_details_toggle.setCheckable(True)
        self.perf_details_toggle.setChecked(False)
        self.perf_details_toggle.setStyleSheet(details_btn_style)
        perf_layout.addWidget(self.perf_details_toggle)

        self.perf_details_widget = QWidget()
        details_layout = QVBoxLayout(self.perf_details_widget)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(10)
        self.perf_details_widget.setVisible(False)

        self.perf_auto_dynamic_checkbox = QCheckBox("内存不足时自动启用分段加载并重跑")
        self.perf_auto_dynamic_checkbox.setStyleSheet("color: #E0E0E0;")
        self.perf_auto_dynamic_checkbox.setChecked(
            self.settings.value("performance_memory_auto_dynamic_load", True, type=bool)
        )
        details_layout.addWidget(self.perf_auto_dynamic_checkbox)
        _add_desc(details_layout, "运行中遇到内存不足时，自动降低内存档位重试，建议保持开启（三档默认均开启）。")

        self.perf_khhistory_fastpath_checkbox = QCheckBox("启用 khHistory 内存快路")
        self.perf_khhistory_fastpath_checkbox.setStyleSheet("color: #E0E0E0;")
        self.perf_khhistory_fastpath_checkbox.setChecked(
            self.settings.value("performance_khhistory_memory_fastpath", True, type=bool)
        )
        details_layout.addWidget(self.perf_khhistory_fastpath_checkbox)
        _add_desc(details_layout, "策略频繁调用 khHistory 时，优先从框架内存切片，减少 DuckDB 读取。复权场景已有源码保护，会自动走保守路径。")

        self.perf_khhistory_missing_prompt_checkbox = QCheckBox("khHistory 缺历史数据时弹窗确认")
        self.perf_khhistory_missing_prompt_checkbox.setStyleSheet("color: #E0E0E0;")
        self.perf_khhistory_missing_prompt_checkbox.setChecked(
            self.settings.value("performance_khhistory_missing_data_prompt", True, type=bool)
        )
        details_layout.addWidget(self.perf_khhistory_missing_prompt_checkbox)
        _add_desc(details_layout, "打开后，GUI 回测遇到 khHistory 历史数据不足会提示是否继续。大批量压测建议关闭，避免反复弹窗。")

        self.perf_framework_raw_load_checkbox = QCheckBox("框架原始 DuckDB 加载")
        self.perf_framework_raw_load_checkbox.setStyleSheet("color: #E0E0E0;")
        self.perf_framework_raw_load_checkbox.setChecked(
            self.settings.value("performance_framework_raw_duckdb_load", True, type=bool)
        )
        details_layout.addWidget(self.perf_framework_raw_load_checkbox)
        _add_desc(details_layout, "框架批量读行情时减少不必要的时间字段转换。通常更快，结果应保持一致；排查读取问题时可关闭。")

        preload_row = QHBoxLayout()
        preload_label = QLabel("历史预热天数:")
        preload_label.setStyleSheet("color: #E0E0E0;")
        self.perf_history_preload_edit = QLineEdit()
        self.perf_history_preload_edit.setFixedWidth(90)
        self.perf_history_preload_edit.setText(str(
            self.settings.value(
                "performance_framework_history_preload_days",
                DEFAULT_PERFORMANCE_CONFIG.get("framework_history_preload_days", 300),
            )
        ))
        self.perf_history_preload_edit.setValidator(QIntValidator(0, 2000))
        self.perf_history_preload_edit.setStyleSheet(perf_lineedit_style)
        preload_row.addWidget(preload_label)
        preload_row.addWidget(self.perf_history_preload_edit)
        preload_row.addStretch()
        details_layout.addLayout(preload_row)
        _add_desc(
            details_layout,
            "提前加载回测开始日前的历史 K 线，供 khHistory 内存快路使用。默认 300；日线策略通常无需更大，过大只会增加加载量和内存压力。",
        )

        _add_combo_row(
            details_layout,
            "khHistory 缓存:",
            "perf_khhistory_cache_mode_combo",
            [("按回测窗口缓存", "backtest_window"), ("关闭优化缓存", "off")],
            self.settings.value("performance_khhistory_cache_mode", "backtest_window"),
            "按回测窗口缓存能减少重复读取，是 khHistory 策略提速最明显的开关之一。关闭后更接近旧行为但会慢。",
        )

        _add_combo_row(
            details_layout,
            "khHistory 预取:",
            "perf_khhistory_prefetch_combo",
            [("只到当前交易日", "current_day"), ("整段回测窗口", "backtest_end")],
            self.settings.value("performance_khhistory_prefetch_end", "current_day"),
            "只到当前交易日最稳；整段预取对不复权分钟线压测更快。前复权/后复权请求会被源码保护，不会使用可能改变复权锚点的激进路径。",
        )

        _add_combo_row(
            details_layout,
            "DuckDB 排序:",
            "perf_duckdb_order_combo",
            [("取回后校验", "verify_after_fetch"), ("SQL 强制排序", "sql_order_by")],
            self.settings.value("performance_duckdb_order_mode", "verify_after_fetch"),
            "取回后校验通常更快；如果发现数据时间顺序异常，可切到 SQL 强制排序做保守排查。",
        )

        _add_combo_row(
            details_layout,
            "空数据日志:",
            "perf_empty_log_combo",
            [("汇总提示", "summary"), ("逐 bar 详细", "verbose")],
            self.settings.value("performance_empty_data_log_mode", "summary"),
            "汇总提示能避免全市场回测时日志被空数据 warning 刷屏。详细模式只建议排查小股票池时使用。",
        )

        advanced_row = QHBoxLayout()
        self.perf_batch_size_edit = QLineEdit()
        self.perf_batch_size_edit.setFixedWidth(90)
        self.perf_batch_size_edit.setText(str(self.settings.value("performance_duckdb_load_batch_size", "auto")))
        self.perf_batch_size_edit.setPlaceholderText("auto")
        self.perf_batch_size_edit.setStyleSheet(perf_lineedit_style)
        self.perf_workers_edit = QLineEdit()
        self.perf_workers_edit.setFixedWidth(90)
        self.perf_workers_edit.setText(str(self.settings.value("performance_duckdb_parallel_read_workers", 1)))
        self.perf_workers_edit.setValidator(QIntValidator(1, 64))
        self.perf_workers_edit.setStyleSheet(perf_lineedit_style)
        advanced_row.addWidget(QLabel("DuckDB批大小:"))
        advanced_row.addWidget(self.perf_batch_size_edit)
        advanced_row.addWidget(QLabel("并行读取:"))
        advanced_row.addWidget(self.perf_workers_edit)
        advanced_row.addStretch()
        for i in range(advanced_row.count()):
            item = advanced_row.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), QLabel):
                item.widget().setStyleSheet("color: #E0E0E0;")
        details_layout.addLayout(advanced_row)
        _add_desc(details_layout, "批大小控制一次读多少股票；并行读取过高可能增加磁盘争用和文件句柄压力。")

        perf_layout.addWidget(self.perf_details_widget)

        parquet_label = QLabel("Parquet 缓存（独立可选）")
        parquet_label.setStyleSheet("color: #E0E0E0; font-weight: bold; margin-top: 8px;")
        perf_layout.addWidget(parquet_label)
        _add_desc(perf_layout, "Parquet 适合反复跑同一批全A分钟线压测。它会占用额外磁盘空间，所以不跟随三档模式自动开启。")

        _add_combo_row(
            perf_layout,
            "Parquet 缓存:",
            "perf_parquet_mode_combo",
            [("关闭", "off"), ("只读使用", "read_only"), ("缺失时构建后读取", "build_then_read")],
            self.settings.value("performance_parquet_cache_pack", "off"),
            "关闭最安全；只读使用不会自动生成文件；构建后读取会在缺失时创建缓存包。",
        )

        parquet_root_row = QHBoxLayout()
        parquet_root_label = QLabel("Parquet缓存目录:")
        parquet_root_label.setStyleSheet("color: #E0E0E0;")
        self.perf_parquet_root_edit = QLineEdit()
        self.perf_parquet_root_edit.setStyleSheet(perf_lineedit_style)
        self.perf_parquet_root_edit.setText(self.settings.value("performance_parquet_cache_pack_root", ""))
        self.perf_parquet_root_edit.setPlaceholderText("留空则使用软件默认临时缓存目录")
        parquet_root_btn = QPushButton("浏览...")
        parquet_root_btn.setFixedWidth(80)
        parquet_root_btn.clicked.connect(self.browse_parquet_cache_root)
        parquet_root_btn.setStyleSheet("""
            QPushButton { background-color: #505050; color: #E0E0E0; border: none; padding: 5px 15px; border-radius: 2px; }
            QPushButton:hover { background-color: #606060; }
        """)
        parquet_root_row.addWidget(parquet_root_label)
        parquet_root_row.addWidget(self.perf_parquet_root_edit)
        parquet_root_row.addWidget(parquet_root_btn)
        perf_layout.addLayout(parquet_root_row)

        parquet_build_row = QGridLayout()
        self.perf_parquet_batch_edit = QLineEdit()
        self.perf_parquet_batch_edit.setFixedWidth(90)
        self.perf_parquet_batch_edit.setText(str(self.settings.value("performance_parquet_cache_pack_batch_size", "auto")))
        self.perf_parquet_batch_edit.setPlaceholderText("auto")
        self.perf_parquet_batch_edit.setStyleSheet(perf_lineedit_style)
        self.perf_parquet_workers_edit = QLineEdit()
        self.perf_parquet_workers_edit.setFixedWidth(90)
        self.perf_parquet_workers_edit.setText(str(self.settings.value("performance_parquet_cache_pack_workers", "auto")))
        self.perf_parquet_workers_edit.setPlaceholderText("auto")
        self.perf_parquet_workers_edit.setStyleSheet(perf_lineedit_style)
        self.perf_parquet_compression_combo = QComboBox()
        self.perf_parquet_compression_combo.setStyleSheet(combo_style)
        for compression in ("SNAPPY", "ZSTD", "GZIP", "LZ4", "NONE"):
            self.perf_parquet_compression_combo.addItem(compression, compression)
        compression_idx = self.perf_parquet_compression_combo.findData(
            str(self.settings.value("performance_parquet_cache_pack_compression", "SNAPPY")).upper()
        )
        self.perf_parquet_compression_combo.setCurrentIndex(max(0, compression_idx))
        parquet_build_row.addWidget(QLabel("Parquet批大小:"), 0, 0)
        parquet_build_row.addWidget(self.perf_parquet_batch_edit, 0, 1)
        parquet_build_row.addWidget(QLabel("线程:"), 0, 2)
        parquet_build_row.addWidget(self.perf_parquet_workers_edit, 0, 3)
        parquet_build_row.addWidget(QLabel("压缩:"), 1, 0)
        parquet_build_row.addWidget(self.perf_parquet_compression_combo, 1, 1, 1, 2)
        parquet_build_row.setColumnStretch(4, 1)
        for i in range(parquet_build_row.count()):
            item = parquet_build_row.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), QLabel):
                item.widget().setStyleSheet("color: #E0E0E0;")
        perf_layout.addLayout(parquet_build_row)
        _add_desc(perf_layout, "Parquet 批大小和线程数可填 auto。线程过高会提高构建压力；压缩率越高通常越省空间但构建更慢。")

        parquet_tools_row = QHBoxLayout()
        parquet_tool_style = """
            QPushButton { background-color: #505050; color: #E0E0E0; border: none; padding: 6px 12px; border-radius: 2px; }
            QPushButton:hover { background-color: #606060; }
        """
        self.parquet_stats_btn = QPushButton("查看缓存")
        self.parquet_clean_btn = QPushButton("清理缓存")
        self.parquet_check_btn = QPushButton("检查当前配置")
        self.parquet_build_btn = QPushButton("显式构建")
        for btn in (self.parquet_stats_btn, self.parquet_clean_btn, self.parquet_check_btn, self.parquet_build_btn):
            btn.setStyleSheet(parquet_tool_style)
            parquet_tools_row.addWidget(btn)
        parquet_tools_row.addStretch()
        self.parquet_stats_btn.clicked.connect(self.show_parquet_cache_stats)
        self.parquet_clean_btn.clicked.connect(self.clean_parquet_cache)
        self.parquet_check_btn.clicked.connect(self.check_parquet_cache_for_current_config)
        self.parquet_build_btn.clicked.connect(self.build_parquet_cache_for_current_config)
        perf_layout.addLayout(parquet_tools_row)

        self.perf_preset_combo.currentIndexChanged.connect(self.apply_performance_preset_to_controls)
        self.perf_details_toggle.toggled.connect(self._toggle_performance_details)
        self._update_performance_preset_desc()

        performance_tab_layout.addWidget(perf_group)
        performance_tab_layout.addStretch()

        # ============ Tushare设置 ============

        groupbox_style = """
            QGroupBox {
                border: 1px solid #505050;
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
        """
        lineedit_style = """
            QLineEdit {
                border: 1px solid #505050;
                border-radius: 2px;
                padding: 5px;
                background-color: #404040;
                color: #E0E0E0;
            }
            QLineEdit:focus { border: 1px solid #606060; }
        """
        btn_style = """
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                border-radius: 3px;
                padding: 6px 14px;
                font-size: 13px;
            }
            QPushButton:hover { background-color: #106ebe; }
            QPushButton:pressed { background-color: #005a9e; }
        """

        _measure_label = QLabel("代理地址:")
        _fm = _measure_label.fontMetrics()
        label_width = max(115, _fm.horizontalAdvance("代理地址:") + 15)

        # --- Tushare 接口配置（含代理） ---
        tushare_group = QGroupBox("Tushare 接口配置")
        tushare_group.setStyleSheet(groupbox_style)
        tushare_layout = QVBoxLayout(tushare_group)
        tushare_layout.setSpacing(10)

        # API 地址行
        api_url_row = QHBoxLayout()
        api_url_label = QLabel("API 地址:")
        api_url_label.setStyleSheet("color: #E0E0E0;")
        api_url_label.setFixedWidth(label_width)
        self.tushare_api_url_edit = QLineEdit()
        self.tushare_api_url_edit.setPlaceholderText("留空使用 Tushare SDK 默认地址")
        self.tushare_api_url_edit.setStyleSheet(lineedit_style)
        self.tushare_api_url_edit.setText(
            normalize_tushare_api_url(self.settings.value('tushare_api_url', ''))
        )
        api_url_row.addWidget(api_url_label)
        api_url_row.addWidget(self.tushare_api_url_edit)
        tushare_layout.addLayout(api_url_row)

        # Token 输入行
        token_row = QHBoxLayout()
        token_label = QLabel("Token:")
        token_label.setStyleSheet("color: #E0E0E0;")
        token_label.setFixedWidth(label_width)
        self.tushare_token_edit = QLineEdit()
        self.tushare_token_edit.setEchoMode(QLineEdit.Password)
        self.tushare_token_edit.setPlaceholderText("在 https://tushare.pro 注册后获取")
        self.tushare_token_edit.setStyleSheet(lineedit_style)
        # 读取已存储的 token（base64 解码）
        _token_b64 = self.settings.value('tushare_token', '')
        if _token_b64:
            try:
                self.tushare_token_edit.setText(base64.b64decode(_token_b64.encode()).decode())
            except Exception:
                self.tushare_token_edit.setText(_token_b64)

        # 显示/隐藏切换按钮
        self.tushare_token_toggle_btn = QPushButton("显示")
        self.tushare_token_toggle_btn.setCheckable(True)
        self.tushare_token_toggle_btn.setFixedWidth(55)
        self.tushare_token_toggle_btn.setStyleSheet(btn_style)
        self.tushare_token_toggle_btn.toggled.connect(self._toggle_token_visibility)

        # 测试连接按钮
        self.tushare_test_btn = QPushButton("测试连接")
        self.tushare_test_btn.setFixedWidth(80)
        self.tushare_test_btn.setStyleSheet(btn_style)
        self.tushare_test_btn.clicked.connect(self._test_tushare_connection)

        token_row.addWidget(token_label)
        token_row.addWidget(self.tushare_token_edit)
        token_row.addWidget(self.tushare_token_toggle_btn)
        token_row.addWidget(self.tushare_test_btn)
        tushare_layout.addLayout(token_row)

        # 网络代理设置（整合在 Tushare 卡片内）
        self.tushare_proxy_check = QCheckBox("使用网络代理访问 Tushare")
        self.tushare_proxy_check.setStyleSheet("color: #E0E0E0; margin-top: 4px;")
        self.tushare_proxy_check.setChecked(
            self.settings.value('tushare_use_proxy', False, type=bool)
        )
        tushare_layout.addWidget(self.tushare_proxy_check)

        proxy_url_row = QHBoxLayout()
        proxy_url_label = QLabel("代理地址:")
        proxy_url_label.setStyleSheet("color: #E0E0E0;")
        proxy_url_label.setFixedWidth(label_width)
        self.tushare_proxy_url_edit = QLineEdit()
        self.tushare_proxy_url_edit.setPlaceholderText("如：http://127.0.0.1:7890")
        self.tushare_proxy_url_edit.setStyleSheet(lineedit_style)
        self.tushare_proxy_url_edit.setText(
            self.settings.value('tushare_proxy_url', '')
        )
        proxy_url_row.addWidget(proxy_url_label)
        proxy_url_row.addWidget(self.tushare_proxy_url_edit)
        tushare_layout.addLayout(proxy_url_row)

        hint_label = QLabel(
            "Token 本地编码保存。分钟线需在 tushare.pro 开通 stk_mins 权限；复权因子需约 2000 积分。"
            " <a href='https://tushare.pro' style='color: #4da6ff; text-decoration: underline;'>前往官网 ↗</a>"
            "<br><span style='color: #888888;'>（网络代理为可选项，不勾选时直连；仅当网络环境无法直连时勾选并配置）</span>"
        )
        hint_label.setStyleSheet("color: #888888; font-size: 12px;")
        hint_label.setWordWrap(True)
        hint_label.setOpenExternalLinks(True)
        tushare_layout.addWidget(hint_label)

        data_tab_layout.addWidget(tushare_group)

        data_tab_layout.addStretch()

        # ============ 底部按钮布局(所有标签页共用) ============

        # 底部按钮布局
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        button_layout.setContentsMargins(0, 4, 0, 0)
        
        # 添加反馈问题按钮（靠左）
        feedback_button = QPushButton("反馈问题")
        feedback_button.setStyleSheet("""
            QPushButton {
                background-color: #404040;
                color: #E0E0E0;
                border: none;
                padding: 5px 15px;
                border-radius: 2px;
            }
            QPushButton:hover {
                background-color: #505050;
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
                background-color: #505050;
                color: #E0E0E0;
                border: none;
                padding: 5px 15px;
                border-radius: 2px;
            }
            QPushButton:hover {
                background-color: #606060;
            }
        """)
        close_button.clicked.connect(self.close)
        
        button_layout.addWidget(save_button)
        button_layout.addWidget(close_button)
        
        layout.addLayout(button_layout)

        # 初始化成交量限制控件的启用状态
        self.on_volume_limit_toggled()

        # 设置整体背景色
        self.setStyleSheet("""
            QDialog, QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget {
                background-color: #2b2b2b;
                color: #e8e8e8;
            }
        """)

    def _apply_native_titlebar_theme(self):
        """尽量让系统标题栏与深色主体保持一致。"""
        if sys.platform != 'win32':
            return

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

            caption_color = DWORD(0x333333)
            windll.dwmapi.DwmSetWindowAttribute(
                int(self.winId()),
                DWMWA_CAPTION_COLOR,
                byref(caption_color),
                sizeof(caption_color)
            )
        except Exception as e:
            logging.warning(f"设置标题栏深色模式失败: {str(e)}")

    def _wrap_tab_content(self, content_widget):
        """将标签页内容放入滚动区域，保证底部按钮始终可见。"""
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        # 正常窗口宽度下内容会自动铺满；在小屏或高字号倍率下保留横向滚动
        # 作为兜底，避免右侧控件被静默裁掉且无法操作。
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setStyleSheet("""
            QScrollArea {
                border: none;
                background-color: #2b2b2b;
            }
            QScrollArea > QWidget > QWidget {
                background-color: #2b2b2b;
            }
        """)
        scroll_area.setWidget(content_widget)
        return scroll_area


    def _toggle_token_visibility(self, checked: bool):
        """切换 token 明文/密文显示。"""
        self.tushare_token_edit.setEchoMode(
            QLineEdit.Normal if checked else QLineEdit.Password
        )
        self.tushare_token_toggle_btn.setText("隐藏" if checked else "显示")


    @staticmethod
    def _normalize_url_text(url: str) -> str:
        """将 URL 中误填的中文冒号规范化为英文冒号。"""
        if not isinstance(url, str):
            return ""
        return url.replace("：", ":").strip()

    def _test_tushare_connection(self):
        """测试 Tushare token 连接（在后台线程执行，不阻塞 GUI）。"""
        token = self.tushare_token_edit.text().strip()
        if not token:
            QMessageBox.warning(self, "提示", "请先填写 Token")
            return
        use_proxy = self.tushare_proxy_check.isChecked()
        proxy_url = self._normalize_url_text(self.tushare_proxy_url_edit.text())
        api_url   = normalize_tushare_api_url(
            self._normalize_url_text(self.tushare_api_url_edit.text())
        )
        self.tushare_proxy_url_edit.setText(proxy_url)
        self.tushare_api_url_edit.setText(api_url)
        self.tushare_test_btn.setEnabled(False)
        self.tushare_test_btn.setText("测试中...")

        # 定义局部 QThread 类
        class TestThread(QThread):
            finished_signal = pyqtSignal(bool, str)

            def __init__(self, t, up, pu, au):
                super().__init__()
                self.t = t
                self.up = up
                self.pu = pu
                self.au = au

            def run(self):
                try:
                    from duckdb_storage.tushare_importer import TushareImporter
                    importer = TushareImporter(
                        token=self.t, use_proxy=self.up,
                        proxy_url=self.pu, api_url=self.au,
                    )
                    ok, msg = importer.test_connection()
                except Exception as e:
                    ok, msg = False, str(e)
                self.finished_signal.emit(ok, msg)

        def _on_done(ok: bool, msg: str):
            try:
                self.tushare_test_btn.setEnabled(True)
                self.tushare_test_btn.setText("测试连接")
                if ok:
                    QMessageBox.information(self, "连接成功", f"Token 有效！{msg}")
                else:
                    QMessageBox.warning(self, "连接失败", msg)
            except RuntimeError:
                pass  # 对话框已在结果返回前关闭

        self._tushare_test_thread = TestThread(token, use_proxy, proxy_url, api_url)
        self._tushare_test_thread.finished_signal.connect(_on_done)
        self._tushare_test_thread.start()


    def _persist_ui_font_scale(self):
        """即时保存界面字号倍率，避免未点击保存时丢失"""
        try:
            ui_font_scale = float(self.font_scale_spin.value())
            if ui_font_scale < 0 or ui_font_scale > 2.0:
                return
            self.settings.setValue('ui_font_scale', ui_font_scale)
            self.settings.sync()
            self._initial_ui_font_scale = ui_font_scale
        except Exception:
            pass

    def closeEvent(self, event):
        """关闭时至少保存字号倍率"""
        self._persist_ui_font_scale()
        super().closeEvent(event)
            
    def save_settings(self):
        """保存设置"""
        try:
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

            # 保存界面字号倍率（0表示自动）
            ui_font_scale = float(self.font_scale_spin.value())
            if ui_font_scale < 0 or ui_font_scale > 2.0:
                QMessageBox.warning(self, "警告", "界面字号倍率应在0到2.0之间")
                return
            self.settings.setValue('ui_font_scale', ui_font_scale)
            ui_scale_changed = abs(ui_font_scale - float(self._initial_ui_font_scale or 0.0)) > 1e-6

            # 保存停止后直接退出状态
            stop_exit_enabled = self.stop_exit_checkbox.isChecked()
            self.settings.setValue('stop_exit_immediately', stop_exit_enabled)

            # 保存数据完整性检查模式（保留旧布尔键以兼容历史代码/配置）
            integrity_check_mode = self.integrity_check_combo.currentData() or "auto"
            self.settings.setValue('check_data_integrity_mode', integrity_check_mode)
            self.settings.setValue('check_data_integrity', integrity_check_mode != "off")

            # 保存成交量限制设置
            volume_limit_enabled = self.volume_limit_checkbox.isChecked()
            self.settings.setValue('volume_limit_enabled', volume_limit_enabled)

            # 保存参与率（将百分比转换为小数）
            participation_rate_text = self.participation_rate_edit.text().strip()
            try:
                participation_rate = float(participation_rate_text) / 100.0  # 转换为小数
                if participation_rate < 0.001 or participation_rate > 1.0:
                    QMessageBox.warning(self, "警告", "市场参与率应在0.1%到100%之间")
                    return
                self.settings.setValue('participation_rate', participation_rate)
            except ValueError:
                QMessageBox.warning(self, "警告", "市场参与率必须是有效的数字")
                return

            # 保存允许部分成交状态
            allow_partial_enabled = self.allow_partial_checkbox.isChecked()
            self.settings.setValue('allow_partial_fill', allow_partial_enabled)

            # 保存动态数据加载设置（已禁用UI，但保留代码以防万一）
            # dynamic_load_enabled = self.dynamic_load_checkbox.isChecked()
            # self.settings.setValue('dynamic_load_enabled', dynamic_load_enabled)

            # chunk_size_text = self.chunk_size_edit.text().strip()
            # try:
            #     chunk_size = int(chunk_size_text) if chunk_size_text else 5
            #     if chunk_size < 1 or chunk_size > 365:
            #         QMessageBox.warning(self, "警告", "分段大小应在1到365之间")
            #         return
            #     self.settings.setValue('dynamic_load_chunk_size', chunk_size)
            # except ValueError:
            #     QMessageBox.warning(self, "警告", "分段大小必须是有效的整数")
            #     return

            # 保存账户设置
            account_id = self.account_id_input.text().strip()
            self.settings.setValue('account_id', account_id)
            
            account_type = self.account_type_selector.currentText()
            self.settings.setValue('account_type', account_type)
            
            # DuckDB 数据目录（开源版唯一的回测数据源）
            duckdb_path = self.duckdb_path_edit.text().strip()
            if not duckdb_path:
                QMessageBox.warning(self, "警告", "请先设置DuckDB数据路径。")
                return
            if (
                os.path.normcase(os.path.abspath(duckdb_path))
                != os.path.normcase(os.path.abspath(self._initial_duckdb_path or default_duckdb_dir()))
                and may_be_shared_with_cs(duckdb_path)
            ):
                reply = QMessageBox.warning(
                    self, "这个目录可能和 CS 共用",
                    f"{duckdb_path}\n\n可能也在被看海量化 CS 版使用。{SHARED_WRITE_WARNING}\n"
                    "在这个目录里导入、核验索引、WAL 修复都要逐次确认，策略里的 khDuckWrite 会拒绝写入。\n\n"
                    "建议改用开源版自己的目录，并用「复制 CS 数据…」复制一份。仍然使用这个目录吗？",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
            if duckdb_path:
                try:
                    duckdb_path, created_duckdb_dir = self._normalize_and_create_duckdb_path(duckdb_path)
                    self.duckdb_path_edit.setText(duckdb_path)
                    if created_duckdb_dir:
                        QMessageBox.information(
                            self,
                            "已创建DuckDB数据目录",
                            f"您输入的DuckDB数据路径不存在，软件已自动创建：\n{duckdb_path}"
                        )
                except Exception as exc:
                    QMessageBox.critical(
                        self,
                        "创建DuckDB数据目录失败",
                        f"无法创建DuckDB数据路径：\n{duckdb_path}\n\n错误信息：{exc}"
                    )
                    return

            self.settings.setValue('duckdb_data_path', duckdb_path)
            self._initial_duckdb_path = duckdb_path
            try:
                claim_if_new(duckdb_path, "设置里选择的新目录")
            except OSError:
                pass

            # 保存回测性能设置（系统级，不写入 .kh）
            self.settings.setValue(
                'performance_preset',
                self.perf_preset_combo.currentData(),
            )
            self.settings.setValue(
                'performance_memory_auto_dynamic_load',
                self.perf_auto_dynamic_checkbox.isChecked(),
            )
            self.settings.setValue(
                'performance_khhistory_memory_fastpath',
                self.perf_khhistory_fastpath_checkbox.isChecked(),
            )
            self.settings.setValue(
                'performance_khhistory_missing_data_prompt',
                self.perf_khhistory_missing_prompt_checkbox.isChecked(),
            )
            self.settings.setValue(
                'performance_framework_raw_duckdb_load',
                self.perf_framework_raw_load_checkbox.isChecked(),
            )
            try:
                history_preload_days = int(self.perf_history_preload_edit.text().strip() or "300")
                if history_preload_days < 0 or history_preload_days > 2000:
                    QMessageBox.warning(self, "警告", "历史预热天数应在0到2000之间")
                    return
                self.settings.setValue('performance_framework_history_preload_days', history_preload_days)
            except ValueError:
                QMessageBox.warning(self, "警告", "历史预热天数必须是有效整数")
                return
            self.settings.setValue(
                'performance_khhistory_cache_mode',
                self.perf_khhistory_cache_mode_combo.currentData(),
            )
            self.settings.setValue(
                'performance_khhistory_prefetch_end',
                self.perf_khhistory_prefetch_combo.currentData(),
            )
            self.settings.setValue(
                'performance_duckdb_order_mode',
                self.perf_duckdb_order_combo.currentData(),
            )
            self.settings.setValue(
                'performance_empty_data_log_mode',
                self.perf_empty_log_combo.currentData(),
            )
            self.settings.setValue(
                'performance_parquet_cache_pack',
                self.perf_parquet_mode_combo.currentData(),
            )
            self.settings.setValue(
                'performance_parquet_cache_pack_root',
                self.perf_parquet_root_edit.text().strip(),
            )

            parquet_batch_text = self.perf_parquet_batch_edit.text().strip() or "auto"
            if parquet_batch_text.lower() != "auto":
                try:
                    parquet_batch = int(parquet_batch_text)
                    if parquet_batch < 1 or parquet_batch > 100000:
                        QMessageBox.warning(self, "警告", "Parquet批大小应为 auto 或 1 到 100000 之间的整数")
                        return
                    parquet_batch_text = str(parquet_batch)
                except ValueError:
                    QMessageBox.warning(self, "警告", "Parquet批大小应为 auto 或有效整数")
                    return
            self.settings.setValue('performance_parquet_cache_pack_batch_size', parquet_batch_text)

            parquet_workers_text = self.perf_parquet_workers_edit.text().strip() or "auto"
            if parquet_workers_text.lower() != "auto":
                try:
                    parquet_workers = int(parquet_workers_text)
                    if parquet_workers < 1 or parquet_workers > 64:
                        QMessageBox.warning(self, "警告", "Parquet线程数应为 auto 或 1 到 64 之间的整数")
                        return
                    parquet_workers_text = str(parquet_workers)
                except ValueError:
                    QMessageBox.warning(self, "警告", "Parquet线程数应为 auto 或有效整数")
                    return
            self.settings.setValue('performance_parquet_cache_pack_workers', parquet_workers_text)
            self.settings.setValue(
                'performance_parquet_cache_pack_compression',
                self.perf_parquet_compression_combo.currentData(),
            )

            batch_size_text = self.perf_batch_size_edit.text().strip() or "auto"
            if batch_size_text.lower() != "auto":
                try:
                    batch_size = int(batch_size_text)
                    if batch_size < 1 or batch_size > 100000:
                        QMessageBox.warning(self, "警告", "DuckDB批大小应为 auto 或 1 到 100000 之间的整数")
                        return
                    batch_size_text = str(batch_size)
                except ValueError:
                    QMessageBox.warning(self, "警告", "DuckDB批大小应为 auto 或有效整数")
                    return
            self.settings.setValue('performance_duckdb_load_batch_size', batch_size_text)

            workers_text = self.perf_workers_edit.text().strip() or "1"
            try:
                workers = int(workers_text)
                if workers < 1 or workers > 64:
                    QMessageBox.warning(self, "警告", "并行读取线程数应在1到64之间")
                    return
                self.settings.setValue('performance_duckdb_parallel_read_workers', workers)
            except ValueError:
                QMessageBox.warning(self, "警告", "并行读取线程数必须是有效整数")
                return

            # 自定义标记: 删预热天数后, 这条从原 try 块里拎出来单独执行(否则会被一起删掉)
            self.settings.setValue(
                'performance_detail_customized',
                not self._performance_controls_equal_selected_preset(),
            )

            # 保存 Tushare Token（base64 混淆，防止明文存储）
            token_plain = self.tushare_token_edit.text().strip()
            if token_plain:
                from tushare_config import validate_tushare_token
                token_ok, token_message = validate_tushare_token(token_plain)
                if not token_ok:
                    QMessageBox.warning(self, "Tushare Token 无效", token_message)
                    self.tushare_token_edit.setFocus()
                    return
                token_b64 = base64.b64encode(token_plain.encode()).decode()
            else:
                token_b64 = ''
            self.settings.setValue('tushare_token', token_b64)

            # 保存 API 地址（留空表示使用 Tushare SDK 默认数据接口）
            api_url = normalize_tushare_api_url(
                self._normalize_url_text(self.tushare_api_url_edit.text())
            )
            self.tushare_api_url_edit.setText(api_url)
            self.settings.setValue('tushare_api_url', api_url)

            # 保存代理设置
            self.settings.setValue('tushare_use_proxy', self.tushare_proxy_check.isChecked())
            proxy_url = self._normalize_url_text(self.tushare_proxy_url_edit.text())
            self.tushare_proxy_url_edit.setText(proxy_url)
            self.settings.setValue('tushare_proxy_url', proxy_url)

            success_message = "设置已保存"
            if ui_scale_changed:
                success_message += "（界面字号已更新，部分窗口需重新打开）"
            QMessageBox.information(self, "成功", success_message)
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存设置时出错: {str(e)}")

    def _toggle_performance_details(self, checked):
        """Show or hide advanced backtest performance controls."""
        if hasattr(self, "perf_details_widget"):
            self.perf_details_widget.setVisible(bool(checked))
        if hasattr(self, "perf_details_toggle"):
            self.perf_details_toggle.setText("隐藏详细设置" if checked else "显示详细设置")

    def _update_performance_preset_desc(self):
        if not hasattr(self, "perf_preset_combo") or not hasattr(self, "perf_preset_desc"):
            return
        preset = normalize_performance_preset(self.perf_preset_combo.currentData())
        info = PERFORMANCE_PRESETS.get(preset, PERFORMANCE_PRESETS["balanced"])
        details = {
            "performance": "全量一次性载入(standard)、khHistory 可整段预取。数据小、内存大时最快；超大数据会自动降级分段。",
            "balanced": "自动判断内存(auto)：小数据全量、大数据自动分段、分段大小按内存自动放大。日常推荐。",
            "low_memory": "主动分段载入(low)、最省内存。超大股票池长周期回测用它；分段大小按内存自动调整。",
        }.get(preset, "")
        self.perf_preset_desc.setText(f"{info.get('description', '')}\n{details}")

    def apply_performance_preset_to_controls(self):
        """Fill detailed controls from the selected preset.

        Parquet settings are intentionally left untouched because cache packs
        can consume significant disk space and should remain explicit.
        """
        if not hasattr(self, "perf_preset_combo"):
            return
        preset = normalize_performance_preset(self.perf_preset_combo.currentData())
        values = performance_preset_settings(preset)

        def set_combo(attr, value):
            combo = getattr(self, attr, None)
            if combo is None:
                return
            idx = combo.findData(value)
            combo.setCurrentIndex(max(0, idx))

        set_combo("perf_khhistory_cache_mode_combo", values.get("khhistory_cache_mode"))
        set_combo("perf_khhistory_prefetch_combo", values.get("khhistory_prefetch_end"))
        set_combo("perf_duckdb_order_combo", values.get("duckdb_order_mode"))
        set_combo("perf_empty_log_combo", values.get("empty_data_log_mode"))

        if hasattr(self, "perf_batch_size_edit"):
            self.perf_batch_size_edit.setText(str(values.get("duckdb_load_batch_size", "auto")))
        if hasattr(self, "perf_workers_edit"):
            self.perf_workers_edit.setText(str(values.get("duckdb_parallel_read_workers", 1)))
        if hasattr(self, "perf_history_preload_edit"):
            self.perf_history_preload_edit.setText(str(values.get("framework_history_preload_days", 300)))

        if hasattr(self, "perf_auto_dynamic_checkbox"):
            self.perf_auto_dynamic_checkbox.setChecked(bool(values.get("memory_auto_dynamic_load", True)))
        if hasattr(self, "perf_khhistory_fastpath_checkbox"):
            self.perf_khhistory_fastpath_checkbox.setChecked(bool(values.get("khhistory_memory_fastpath", True)))
        if hasattr(self, "perf_framework_raw_load_checkbox"):
            self.perf_framework_raw_load_checkbox.setChecked(bool(values.get("framework_raw_duckdb_load", True)))

        self._update_performance_preset_desc()

    def _performance_controls_equal_selected_preset(self):
        if not hasattr(self, "perf_preset_combo"):
            return True
        preset = normalize_performance_preset(self.perf_preset_combo.currentData())
        values = performance_preset_settings(preset)

        def combo_value(attr):
            combo = getattr(self, attr, None)
            return combo.currentData() if combo is not None else None

        def text_value(attr, default=""):
            widget = getattr(self, attr, None)
            return widget.text().strip() if widget is not None else str(default)

        def int_text(attr, default=0):
            try:
                return int(text_value(attr, default) or default)
            except Exception:
                return None

        def string_int_text(attr, default="auto"):
            text = text_value(attr, default) or default
            if str(text).strip().lower() in ("", "auto", "default"):
                return "auto"
            try:
                return str(int(text))
            except Exception:
                return str(text)

        def checked_value(attr):
            widget = getattr(self, attr, None)
            return bool(widget.isChecked()) if widget is not None else None

        comparisons = [
            combo_value("perf_khhistory_cache_mode_combo") == values.get("khhistory_cache_mode"),
            combo_value("perf_khhistory_prefetch_combo") == values.get("khhistory_prefetch_end"),
            combo_value("perf_duckdb_order_combo") == values.get("duckdb_order_mode"),
            combo_value("perf_empty_log_combo") == values.get("empty_data_log_mode"),
            string_int_text("perf_batch_size_edit", "auto") == str(values.get("duckdb_load_batch_size", "auto")),
            int_text("perf_workers_edit", 1) == int(values.get("duckdb_parallel_read_workers", 1)),
            int_text("perf_history_preload_edit", 300) == int(values.get("framework_history_preload_days", 300)),
            checked_value("perf_auto_dynamic_checkbox") == bool(values.get("memory_auto_dynamic_load", True)),
            checked_value("perf_khhistory_fastpath_checkbox") == bool(values.get("khhistory_memory_fastpath", True)),
            checked_value("perf_framework_raw_load_checkbox") == bool(values.get("framework_raw_duckdb_load", True)),
        ]
        return all(comparisons)

    def on_volume_limit_toggled(self):
        """成交量限制开关切换"""
        enabled = self.volume_limit_checkbox.isChecked()

        # 控制输入框的启用状态
        self.participation_rate_edit.setEnabled(enabled)
        self.allow_partial_checkbox.setEnabled(enabled)

        # 控制标签的颜色（启用时正常色，禁用时灰色）
        label_color = "#E0E0E0" if enabled else "#808080"
        self.participation_rate_label.setStyleSheet(f"color: {label_color};")
        self.participation_rate_percent_label.setStyleSheet(f"color: {label_color};")
        self.allow_partial_label.setStyleSheet(f"color: {label_color};")

    def open_feedback_page(self):
        """打开反馈问题页面（GitHub Issues）"""
        url = "https://github.com/khscience/OSkhQuant/issues"
        webbrowser.open(url)

    def _open_data_copy_dialog(self):
        """复制 CS 数据到开源版目录；复制完成后把目录填进输入框（点保存才生效）。"""
        from duckdb_storage.data_copy_dialog import DataCopyDialog

        target = self.duckdb_path_edit.text().strip() or default_duckdb_dir()
        dialog = DataCopyDialog(self, target_dir=target)
        dialog.copied.connect(self.duckdb_path_edit.setText)
        dialog.exec_()
        
    def update_stock_list(self):
        """更新股票列表"""
        update_stock_list_btn = self.findChild(QPushButton, "update_stock_list_btn")
        try:
            # 禁用按钮
            if update_stock_list_btn:
                update_stock_list_btn.setEnabled(False)

            # 创建进度对话框
            self.progress_dialog = QProgressDialog("正在通过 BaoStock 更新股票列表...", None, 0, 0, self)
            self.progress_dialog.setWindowModality(Qt.WindowModal)
            self.progress_dialog.setCancelButton(None)
            self.progress_dialog.show()

            # 安装版写入用户目录，避免升级覆盖或不同入口看到不同列表；
            # 源码模式仍沿用项目 data 目录。
            data_dir = get_stock_pool_write_dir(create=True)

            # 获取更新管理器（多进程版本）
            update_manager = get_and_save_stock_list(data_dir, preferred_source="baostock")

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
            if hasattr(self, 'progress_dialog') and self.progress_dialog:
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
            detail = str(message or "股票列表更新成功！")
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("成分股列表更新完成")
            msg_box.setIcon(QMessageBox.Information)
            msg_box.setTextFormat(Qt.RichText)
            msg_box.setText(detail)
            layout = msg_box.layout()
            if layout:
                layout.addItem(QSpacerItem(720, 0, QSizePolicy.Minimum, QSizePolicy.Expanding), layout.rowCount(), 0, 1, layout.columnCount())
            msg_box.exec_()
        else:
            QMessageBox.warning(self, "更新失败", f"更新股票列表失败：{message}")
        
        # 清理管理器
        if hasattr(self, 'update_manager'):
            self.update_manager.stop()
            self.update_manager = None
            
        # 恢复按钮
        update_stock_list_btn = self.findChild(QPushButton, "update_stock_list_btn")
        if update_stock_list_btn:
            update_stock_list_btn.setEnabled(True)


    def browse_duckdb_path(self):
        """浏览选择DuckDB数据路径"""
        duckdb_path = QFileDialog.getExistingDirectory(
            self,
            "选择DuckDB数据存储路径",
            self.duckdb_path_edit.text() or os.path.expanduser("~")
        )
        if duckdb_path:
            self.duckdb_path_edit.setText(duckdb_path)

    def browse_parquet_cache_root(self):
        """浏览选择 Parquet 缓存目录"""
        current = self.perf_parquet_root_edit.text().strip() or os.path.expanduser("~")
        cache_root = QFileDialog.getExistingDirectory(
            self,
            "选择Parquet缓存目录",
            current
        )
        if cache_root:
            self.perf_parquet_root_edit.setText(cache_root)

    def _get_parquet_cache_root(self):
        root = self.perf_parquet_root_edit.text().strip()
        if root:
            return root
        try:
            from duckdb_storage.parquet_cache_pack import default_pack_root
            return str(default_pack_root())
        except Exception:
            return ""

    def _get_current_config_path_for_parquet(self):
        parent = self.parent()
        for attr in ("current_config_file", "config_path"):
            value = getattr(parent, attr, None) if parent is not None else None
            if value:
                return value
        return ""

    def _get_performance_overrides(self):
        try:
            from backtest_runtime_config import performance_overrides_from_settings
            return performance_overrides_from_settings(self.settings.load())
        except Exception:
            return {}

    def show_parquet_cache_stats(self):
        try:
            from duckdb_storage.parquet_cache_pack import inspect_cache_pack_root
            root = self._get_parquet_cache_root()
            stats = inspect_cache_pack_root(root)
            rows = []
            for pack in stats.get("packs", []):
                rows.append(
                    [
                        pack.get("status", ""),
                        _format_bytes(pack.get("bytes", 0)),
                        pack.get("period", ""),
                        f"{pack.get('start_time', '')}~{pack.get('end_time', '')}",
                        pack.get("stock_count", 0),
                        pack.get("rows", 0),
                        pack.get("name", ""),
                    ]
                )
            message = (
                f"根目录: {stats.get('root')}\n"
                f"缓存包数: {stats.get('pack_count', 0)}\n"
                f"有效包: {stats.get('valid_count', 0)}\n"
                f"无效包: {stats.get('invalid_count', 0)}\n"
                f"总大小: {_format_bytes(stats.get('total_bytes', 0))}"
            )
            QMessageBox.information(self, "Parquet 缓存", message)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"查看缓存失败: {str(e)}")

    def clean_parquet_cache(self):
        try:
            from duckdb_storage.parquet_cache_pack import clear_cache_pack_root, inspect_cache_pack_root
            root = self._get_parquet_cache_root()
            stats = inspect_cache_pack_root(root)
            if not stats.get("packs"):
                QMessageBox.information(self, "Parquet 缓存", "当前没有可清理的缓存包")
                return
            preview = clear_cache_pack_root(root, dry_run=True)
            reply = QMessageBox.question(
                self,
                "确认清理",
                f"将清理 {preview.get('deleted_count', 0)} 个缓存包，合计 {_format_bytes(preview.get('deleted_bytes', 0))}。\n确定继续吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            result = clear_cache_pack_root(root, dry_run=False)
            QMessageBox.information(
                self,
                "清理完成",
                f"已清理 {result.get('deleted_count', 0)} 个缓存包，释放 {_format_bytes(result.get('deleted_bytes', 0))}。",
            )
        except Exception as e:
            QMessageBox.critical(self, "错误", f"清理缓存失败: {str(e)}")

    def check_parquet_cache_for_current_config(self):
        config_path = self._get_current_config_path_for_parquet()
        if not config_path:
            QMessageBox.warning(self, "提示", "请先加载或保存一个 .kh 配置后再检查缓存命中")
            return
        try:
            from duckdb_storage.parquet_cache_pack import check_pack_for_config
            import kh_settings as global_settings
            data_root = global_settings.load().get("duckdb_data_path", "")
            if not data_root:
                QMessageBox.warning(self, "提示", "DuckDB 数据路径未配置")
                return
            result = check_pack_for_config(
                config_path,
                data_root=data_root,
                pack_root=self._get_parquet_cache_root(),
                performance_overrides=self._get_performance_overrides(),
            )
            QMessageBox.information(
                self,
                "Parquet 缓存检查",
                f"状态: {result.get('status')}\n"
                f"目录: {result.get('pack_dir')}\n"
                f"股票数: {result.get('stock_count', 0)}\n"
                f"范围: {result.get('start_time')}~{result.get('end_time')}\n"
                f"大小: {_format_bytes(result.get('bytes', 0))}",
            )
        except Exception as e:
            QMessageBox.critical(self, "错误", f"检查缓存失败: {str(e)}")

    def build_parquet_cache_for_current_config(self):
        config_path = self._get_current_config_path_for_parquet()
        if not config_path:
            QMessageBox.warning(self, "提示", "请先加载或保存一个 .kh 配置后再构建缓存")
            return
        try:
            import kh_settings as global_settings
            data_root = global_settings.load().get("duckdb_data_path", "")
            if not data_root:
                QMessageBox.warning(self, "提示", "DuckDB 数据路径未配置")
                return
            reply = QMessageBox.question(
                self,
                "确认构建",
                "显式构建 Parquet 缓存可能耗时较长，确定继续吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

            batch_text = self.perf_parquet_batch_edit.text().strip() or "auto"
            workers_text = self.perf_parquet_workers_edit.text().strip() or "auto"
            batch_size = None if batch_text.lower() == "auto" else int(batch_text)
            workers = None if workers_text.lower() == "auto" else int(workers_text)
            compression = self.perf_parquet_compression_combo.currentData()

            self._parquet_build_dialog = QProgressDialog("正在构建 Parquet 缓存...", None, 0, 0, self)
            self._parquet_build_dialog.setWindowModality(Qt.WindowModal)
            self._parquet_build_dialog.setCancelButton(None)
            self._parquet_build_dialog.show()

            self._parquet_build_thread = ParquetCacheBuildThread(
                config_path,
                data_root,
                pack_root=self._get_parquet_cache_root(),
                batch_size=batch_size,
                workers=workers,
                compression=compression,
                performance_overrides=self._get_performance_overrides(),
            )

            def _done(result):
                if hasattr(self, "_parquet_build_dialog") and self._parquet_build_dialog:
                    self._parquet_build_dialog.close()
                status = result.get("status")
                QMessageBox.information(
                    self,
                    "构建完成",
                    f"状态: {status}\n"
                    f"目录: {result.get('pack_dir')}\n"
                    f"股票数: {result.get('stock_count', 0)}\n"
                    f"行数: {result.get('rows', 0)}\n"
                    f"大小: {_format_bytes(result.get('bytes', 0))}",
                )

            def _err(message):
                if hasattr(self, "_parquet_build_dialog") and self._parquet_build_dialog:
                    self._parquet_build_dialog.close()
                QMessageBox.critical(self, "错误", f"构建缓存失败: {message}")

            self._parquet_build_thread.finished_signal.connect(_done)
            self._parquet_build_thread.error_signal.connect(_err)
            self._parquet_build_thread.start()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"构建缓存失败: {str(e)}")
