# -*- coding: utf-8 -*-
"""
DuckDB数据查看器 - 图形界面

提供数据浏览、查询、统计，以及从 BaoStock / Tushare 下载导入数据功能
"""

import os
import sys
import json
import logging
import subprocess
import tempfile
import time
import uuid
from threading import Event, Lock
from datetime import date as datetime_date, datetime, timedelta
from typing import Any, Optional, List, Dict, Tuple, Mapping

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeWidget, QTreeWidgetItem, QTableView, QGroupBox,
    QLabel, QLineEdit, QPushButton, QComboBox, QDateEdit, QTextEdit,
    QFileDialog, QMessageBox, QProgressBar, QStatusBar, QTabWidget,
    QHeaderView, QAbstractItemView, QMenu, QAction, QToolBar,
    QFrame, QGridLayout, QSpinBox, QCheckBox, QDialog, QListWidget,
    QListWidgetItem, QDialogButtonBox, QRadioButton, QButtonGroup,
    QDesktopWidget, QScrollArea
)
from PyQt5.QtCore import Qt, QDate, QSize, QAbstractTableModel, QModelIndex, QThread, pyqtSignal, QTimer, QMutex, QWaitCondition, QSettings
from PyQt5.QtGui import QFont, QIcon, QColor, QPixmap, QPainter, QFontMetrics, QPalette
from PyQt5 import sip
from khPathUtils import get_stock_pool_path
from khUiScale import get_ui_font_scale, get_preferred_ui_font_family, install_wheel_guard
from security_type_utils import is_listed_fund_code, split_security_code
from tushare_config import load_tushare_settings
from duckdb_storage.lock_retry import is_duckdb_lock_error, parse_duckdb_lock_error
from duckdb_storage.lock_diagnostics import inspect_process_identity
from duckdb_storage.lock_diagnostics_dialog import DatabaseOccupancyDialog
from kh_data_dir_policy import (
    claim_if_new, mark_os_owned, may_be_shared_with_cs, shared_dir_write_message,
)
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
import matplotlib.font_manager as fm

# 设置matplotlib中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示问题

DOWNLOAD_LOG_MAX_BLOCKS = 100
DOWNLOAD_UI_UPDATE_MIN_INTERVAL = 0.2


def _qt_object_is_deleted(obj) -> bool:
    """返回 PyQt 包装对象的底层 C++ 实例是否已经销毁。"""
    if obj is None:
        return True
    try:
        return bool(sip.isdeleted(obj))
    except (TypeError, RuntimeError):
        # 单元测试会使用轻量替身；非 SIP 对象按仍可用处理。
        return False


def _safe_qt_method_call(obj, method_name: str, *args) -> bool:
    """仅在 Qt 对象仍存活时调用方法，避免销毁阶段再次进入 SIP。"""
    if _qt_object_is_deleted(obj):
        return False
    try:
        getattr(obj, method_name)(*args)
        return True
    except (AttributeError, RuntimeError):
        return False


def _dispatch_import_dialog_destroyed(viewer, attr_name: str) -> None:
    """在获取已销毁 Qt 对象的绑定方法之前先完成 SIP 存活检查。"""
    if _qt_object_is_deleted(viewer):
        return
    DuckDBViewer._on_import_dialog_destroyed(viewer, attr_name)


def _dispatch_initial_viewer_scale(viewer) -> None:
    """忽略窗口已销毁后才到达事件队列的首帧缩放回调。"""
    if _qt_object_is_deleted(viewer):
        return
    DuckDBViewer.apply_ui_scale(viewer, viewer.font_scale)


def _resolve_preset_stock_pool_file(filename: str, legacy_dirs=()) -> Optional[str]:
    """解析预设股票池文件，用户更新优先，打包资源及旧目录兜底。"""
    managed_path = get_stock_pool_path(filename)
    if os.path.isfile(managed_path):
        return managed_path
    for directory in legacy_dirs:
        candidate = os.path.join(directory, filename)
        if os.path.isfile(candidate):
            return candidate
    return None


def _normalize_market_security_code(code: str) -> str:
    """统一数据管理各导入窗口的证券代码市场后缀规则。"""
    raw = str(code or "").strip().upper()
    numeric_code, explicit_market = split_security_code(raw)
    if explicit_market and numeric_code:
        return f"{numeric_code}.{explicit_market}"
    if not (numeric_code.isdigit() and len(numeric_code) == 6):
        return raw

    # 深市 12 系转债以及 15/16/17/18 系场内基金必须优先于笼统的
    # “1 开头默认上海”规则，否则 161226 会被误写为 161226.SH。
    if numeric_code.startswith(("12", "15", "16", "17", "180", "184")):
        return f"{numeric_code}.SZ"
    if numeric_code.startswith(("0", "2", "3")):
        return f"{numeric_code}.SZ"
    if numeric_code.startswith(("4", "8")):
        return f"{numeric_code}.BJ"
    if numeric_code.startswith(("1", "5", "6", "9")):
        return f"{numeric_code}.SH"
    return raw


def _coalesce_metadata_records(records) -> List[tuple]:
    """同一证券/周期只刷新一次元数据，并合并可用的写入条数。"""
    merged = {}
    for record in records or []:
        if isinstance(record, dict):
            stock = record.get('stock_code') or record.get('stock')
            period = record.get('period')
            count = record.get('records')
        else:
            values = tuple(record)
            if len(values) < 2:
                continue
            stock, period = values[:2]
            count = values[2] if len(values) >= 3 else None
        if not stock or not period:
            continue
        key = (str(stock), str(period))
        if count is not None:
            merged[key] = int(merged.get(key) or 0) + int(count or 0)
        elif key not in merged:
            merged[key] = None
    return [
        (stock, period, count) if count is not None else (stock, period)
        for (stock, period), count in merged.items()
    ]


def _configure_download_log_widget(widget):
    """限制下载日志显示规模，避免长任务中 QTextDocument 持续膨胀。"""
    try:
        widget.document().setMaximumBlockCount(DOWNLOAD_LOG_MAX_BLOCKS)
    except Exception:
        try:
            widget.setMaximumBlockCount(DOWNLOAD_LOG_MAX_BLOCKS)
        except Exception:
            pass
    try:
        widget.setUndoRedoEnabled(False)
    except Exception:
        pass


def _append_download_log(widget, line: str):
    scrollbar = widget.verticalScrollBar()
    at_bottom = scrollbar.value() >= scrollbar.maximum() - 2
    if hasattr(widget, 'appendPlainText'):
        widget.appendPlainText(line)
    else:
        widget.append(line)
    if at_bottom:
        scrollbar.setValue(scrollbar.maximum())


def _should_update_download_ui(owner, attr_name: str, current: int = None, total: int = None) -> bool:
    force_update = current is None or current <= 1 or (total is not None and total > 0 and current >= total)
    now = time.monotonic()
    last_update = getattr(owner, attr_name, 0.0)
    if force_update or now - last_update >= DOWNLOAD_UI_UPDATE_MIN_INTERVAL:
        setattr(owner, attr_name, now)
        return True
    return False

# 尝试导入DuckDB模块
try:
    from .manager import DuckDBManager
    from .config import DuckDBConfig
    from .worker_common import dict_to_dataframe
except ImportError as e:
    from manager import DuckDBManager
    from config import DuckDBConfig
    from worker_common import dict_to_dataframe

try:
    from .wal_repair import repair_data_root
except ImportError:
    try:
        from wal_repair import repair_data_root
    except ImportError:
        def repair_data_root(*args, **kwargs):
            logging.warning("wal_repair module not found")
            return []

# 尝试导入khQTTools
try:
    import khQTTools
except ImportError:
    try:
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import khQTTools
    except ImportError:
        khQTTools = None


class PandasModel(QAbstractTableModel):
    """Pandas DataFrame的Qt模型"""
    
    def __init__(self, df: pd.DataFrame = None):
        super().__init__()
        self._df = df if df is not None else pd.DataFrame()
        self._header_map = {
            'time': '时间',
            'open': '开盘价',
            'high': '最高价',
            'low': '最低价',
            'close': '收盘价',
            'volume': '成交量',
            'amount': '成交额',
            'settelementPrice': '今结算',
            'settlementPrice': '今结算',
            'openInterest': '持仓量',
            'openInt': '持仓量',
            'preClose': '前收价',
            'suspendFlag': '停牌标记',
            'open_front': '前复权开盘',
            'high_front': '前复权最高',
            'low_front': '前复权最低',
            'close_front': '前复权收盘',
            'open_back': '后复权开盘',
            'high_back': '后复权最高',
            'low_back': '后复权最低',
            'close_back': '后复权收盘',
            'open_front_ratio': '等比前复权开盘',
            'high_front_ratio': '等比前复权最高',
            'low_front_ratio': '等比前复权最低',
            'close_front_ratio': '等比前复权收盘',
            'open_back_ratio': '等比后复权开盘',
            'high_back_ratio': '等比后复权最高',
            'low_back_ratio': '等比后复权最低',
            'close_back_ratio': '等比后复权收盘',
            'turn': '换手率',
            'pctChg': '涨跌幅',
            'peTTM': '滚动市盈率',
            'psTTM': '滚动市销率',
            'pcfNcfTTM': '滚动市现率',
            'pbMRQ': '市净率',
            'isST': '是否ST',
            'lastPrice': '最新价',
            'lastClose': '昨收',
            'pvolume': '现量',
            'stockStatus': '状态',
            'lastSettlementPrice': '昨结算',
            'askPrice1': '卖一价',
            'askPrice2': '卖二价',
            'askPrice3': '卖三价',
            'askPrice4': '卖四价',
            'askPrice5': '卖五价',
            'bidPrice1': '买一价',
            'bidPrice2': '买二价',
            'bidPrice3': '买三价',
            'bidPrice4': '买四价',
            'bidPrice5': '买五价',
            'askVol1': '卖一量',
            'askVol2': '卖二量',
            'askVol3': '卖三量',
            'askVol4': '卖四量',
            'askVol5': '卖五量',
            'bidVol1': '买一量',
            'bidVol2': '买二量',
            'bidVol3': '买三量',
            'bidVol4': '买四量',
            'bidVol5': '买五量',
            'transactionNum': '成交笔数'
        }
        self._header_map_lower = {k.lower(): v for k, v in self._header_map.items()}
    
    def rowCount(self, parent=None):
        return len(self._df)
    
    def columnCount(self, parent=None):
        return len(self._df.columns)
    
    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        
        if role == Qt.DisplayRole:
            value = self._df.iloc[index.row(), index.column()]
            # 格式化显示
            if isinstance(value, float):
                return f"{value:.4f}"
            elif isinstance(value, (datetime, pd.Timestamp)):
                return value.strftime("%Y-%m-%d %H:%M:%S")
            return str(value)
        
        elif role == Qt.TextAlignmentRole:
            return Qt.AlignCenter
        
        return None
    
    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            if orientation == Qt.Horizontal:
                field = str(self._df.columns[section])
                normalized = field.strip()
                cn_name = self._header_map.get(normalized)
                if not cn_name:
                    cn_name = self._header_map_lower.get(normalized.lower())
                if cn_name:
                    return f"{normalized}({cn_name})"
                return field
            else:
                return str(section + 1)
        return None
    
    def update_data(self, df: pd.DataFrame):
        """更新数据"""
        self.beginResetModel()
        self._df = df if df is not None else pd.DataFrame()
        self.endResetModel()


class DataLoadThread(QThread):
    """数据加载线程"""
    finished = pyqtSignal(object)  # 返回DataFrame
    error = pyqtSignal(str)
    progress = pyqtSignal(int)
    
    def __init__(self, manager: DuckDBManager, stock_code: str, period: str,
                 start_time: str = None, end_time: str = None):
        super().__init__()
        self.manager = manager
        self.stock_code = stock_code
        self.period = period
        self.start_time = start_time
        self.end_time = end_time
    
    def run(self):
        try:
            self.progress.emit(50)
            df = self.manager.get_kline_data(
                self.stock_code, self.period,
                self.start_time, self.end_time
            )
            self.progress.emit(100)
            self.finished.emit(df)
        except Exception as e:
            self.error.emit(str(e))


class ScanThread(QThread):
    """只读核验未纳入索引的数据库文件。"""
    result_ready = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(int)
    
    def __init__(self, manager: DuckDBManager):
        super().__init__()
        self.manager = manager
        self.result = None
        self.error_message = ""
    
    def run(self):
        try:
            summary = self.manager.audit_unindexed_databases(
                progress_callback=self.progress.emit,
                should_stop=self.isInterruptionRequested,
                process_isolation=True,
            )
            self.result = summary
            self.result_ready.emit(summary)
        except Exception as e:
            self.error_message = str(e)
            if not self.isInterruptionRequested():
                self.error.emit(self.error_message)


class WalRepairThread(QThread):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, data_root: str, memory_limit: str = "256MB"):
        super().__init__()
        self.data_root = data_root
        self.memory_limit = memory_limit

    def run(self):
        try:
            result = repair_data_root(self.data_root, self.memory_limit)
            if result is None:
                self.error.emit("修复失败：无效的数据目录")
                return
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class WrapHeaderView(QHeaderView):
    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setDefaultAlignment(Qt.AlignCenter)
        self.setMinimumHeight(48)

    def _wrap_text(self, text: str) -> str:
        try:
            import re
            s = str(text)
            if "(" in s:
                return s.replace("(", "\n(")
            s = s.replace("_", "\n")
            s = re.sub(r'([a-z])([A-Z])', r'\1\n\2', s)
            return s
        except Exception:
            return str(text)

    def paintSection(self, painter, rect, logicalIndex):
        if not rect.isValid():
            return
        painter.save()
        bg_color = self.palette().color(QPalette.Button)
        border_color = self.palette().color(QPalette.Dark)
        painter.fillRect(rect, bg_color)
        painter.setPen(border_color)
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        model = self.model()
        if model is not None:
            text = model.headerData(logicalIndex, self.orientation(), Qt.DisplayRole)
        else:
            text = None
        if text is not None:
            display_text = self._wrap_text(text)
            painter.setFont(self.font())
            painter.setPen(self.palette().color(QPalette.ButtonText))
            painter.drawText(
                rect.adjusted(4, 2, -4, -2),
                Qt.AlignCenter | Qt.TextWordWrap,
                display_text
            )
        painter.restore()

    def sectionSizeFromContents(self, logicalIndex):
        size = super().sectionSizeFromContents(logicalIndex)
        text = self.model().headerData(logicalIndex, self.orientation(), Qt.DisplayRole)
        if text is None:
            return size
        display_text = self._wrap_text(text)
        fm = QFontMetrics(self.font())
        lines = display_text.split("\n")
        max_line_w = max((fm.horizontalAdvance(line) for line in lines), default=size.width())
        height = max(size.height(), fm.height() * len(lines) + 8)
        width = max(size.width(), max_line_w + 8)
        return QSize(width, height)


class DuckDBViewer(QMainWindow):
    """DuckDB数据查看器主窗口"""

    def __init__(self, data_root: str = None, read_only: bool = True):
        super().__init__()

        # 设置窗口为非模态，确保主界面可以继续操作
        self.setWindowFlags(Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose)

        self.data_root = data_root
        # 默认只读打开，避免单纯浏览数据时长期占用 metadata.db 写锁。
        # 需要扫描/导入/删除时，再通过 _ensure_writable_manager() 临时切到写连接。
        self.default_read_only = bool(read_only)
        self._manager_read_only = bool(read_only)
        self.manager: Optional[DuckDBManager] = None
        self.current_stock = None
        self.current_period = '1d'
        self.baostock_import_dialog = None  # 保存导入对话框引用
        self.tushare_import_dialog = None
        self._pending_close_after_tushare_stop = False
        self._pending_import_dialogs_to_close = set()
        self._viewer_closing = False
        self.font_scale = get_ui_font_scale()
        self._base_style_raw = None
        self.wal_repair_thread = None
        self.scan_thread = None
        self._pending_close_after_scan = False
        self._last_reindex_summary = None

        self.init_ui()

        # 设置Windows暗色标题栏
        self._set_dark_titlebar()

        # 如果指定了数据目录，自动加载
        if data_root and os.path.exists(data_root):
            self.load_data_root(data_root)

    def _set_dark_titlebar(self):
        """设置Windows暗色标题栏"""
        try:
            import platform
            if platform.system() == "Windows":
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
                caption_color = DWORD(0x333333)  # 与主界面背景色一致
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    DWMWA_CAPTION_COLOR,
                    byref(caption_color),
                    sizeof(caption_color)
                )
        except Exception as e:
            import logging
            logging.debug(f"设置暗色标题栏失败: {str(e)}")

    def _scale_stylesheet(self, style: str, scale: float) -> str:
        if not style:
            return style
        import re

        def repl(match):
            value = float(match.group(1))
            unit = match.group(2)
            scaled = max(6, int(round(value * float(scale))))
            return f"font-size: {scaled}{unit}"

        return re.sub(
            r"font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(px|pt)",
            repl,
            style,
            flags=re.IGNORECASE
        )

    def _set_scaled_stylesheet(self, widget: QWidget, style: str):
        widget.setProperty("ui_base_stylesheet", style)
        widget.setStyleSheet(self._scale_stylesheet(style, self.font_scale))

    def _set_scaled_font(self, widget: QWidget, base_pt: int):
        widget.setProperty("ui_base_font_pt", base_pt)
        font = widget.font()
        font.setPointSize(max(6, int(round(base_pt * self.font_scale))))
        widget.setFont(font)

    def apply_ui_scale(self, scale=None):
        if scale is None:
            scale = get_ui_font_scale()
        self.font_scale = scale
        if self._base_style_raw:
            self.setStyleSheet(self._scale_stylesheet(self._base_style_raw, self.font_scale))
        try:
            for child in self.findChildren(QWidget):
                base_ss = child.property("ui_base_stylesheet")
                if base_ss:
                    child.setStyleSheet(self._scale_stylesheet(base_ss, self.font_scale))
                base_pt = child.property("ui_base_font_pt")
                if base_pt:
                    font = child.font()
                    font.setPointSize(max(6, int(round(float(base_pt) * self.font_scale))))
                    child.setFont(font)
        except Exception:
            pass

    def _scale_stylesheet(self, style: str, scale: float) -> str:
        """按倍率缩放样式表中的 font-size"""
        if not style:
            return style
        import re

        def repl(match):
            value = float(match.group(1))
            unit = match.group(2)
            scaled = max(6, int(round(value * float(scale))))
            return f"font-size: {scaled}{unit}"

        return re.sub(
            r"font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(px|pt)",
            repl,
            style,
            flags=re.IGNORECASE
        )

    def _set_scaled_stylesheet(self, widget: QWidget, style: str):
        """设置并记录可缩放样式表"""
        widget.setProperty("ui_base_stylesheet", style)
        widget.setStyleSheet(self._scale_stylesheet(style, self.font_scale))

    def _set_scaled_font(self, widget: QWidget, base_pt: int):
        """设置并记录可缩放字体"""
        widget.setProperty("ui_base_font_pt", base_pt)
        font = widget.font()
        font.setPointSize(max(6, int(round(base_pt * self.font_scale))))
        widget.setFont(font)

    def apply_ui_scale(self, scale=None):
        """应用界面字号倍率到当前窗口"""
        if scale is None:
            scale = get_ui_font_scale()
        self.font_scale = scale
        if self._base_style_raw:
            self.setStyleSheet(self._scale_stylesheet(self._base_style_raw, self.font_scale))
        try:
            for child in self.findChildren(QWidget):
                base_ss = child.property("ui_base_stylesheet")
                if base_ss:
                    child.setStyleSheet(self._scale_stylesheet(base_ss, self.font_scale))
                base_pt = child.property("ui_base_font_pt")
                if base_pt:
                    font = child.font()
                    font.setPointSize(max(6, int(round(float(base_pt) * self.font_scale))))
                    child.setFont(font)
        except Exception:
            pass

    def _get_icon_path(self, icon_name):
        """获取图标文件的正确路径"""
        # 源码环境
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'icons', icon_name)

    def _scale_stylesheet(self, style: str, scale: float) -> str:
        """按倍率缩放样式表中的 font-size"""
        if not style:
            return style
        import re

        def repl(match):
            value = float(match.group(1))
            unit = match.group(2)
            scaled = max(6, int(round(value * float(scale))))
            return f"font-size: {scaled}{unit}"

        return re.sub(
            r"font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(px|pt)",
            repl,
            style,
            flags=re.IGNORECASE
        )

    def apply_ui_scale(self, scale=None):
        """应用界面字号倍率到当前窗口"""
        if scale is None:
            scale = get_ui_font_scale()
        self.font_scale = scale

        # 腾讯窗口从固定像素基准自行缩放，父窗口不再采集其已缩放字体。
        def independently_scaled(child):
            current = child
            while current is not None and current is not self:
                if current.property('ui_scale_managed'):
                    return True
                current = current.parentWidget()
            return False

        # 1) 缩放子控件样式中的 font-size
        try:
            for child in self.findChildren(QWidget):
                if independently_scaled(child):
                    continue
                ss = child.styleSheet() or ''
                if ss:
                    base_ss = child.property("ui_base_stylesheet")
                    if base_ss is None:
                        base_ss = ss
                        child.setProperty("ui_base_stylesheet", base_ss)
                    scaled_ss = self._scale_stylesheet(base_ss, self.font_scale)
                    if scaled_ss != ss:
                        child.setStyleSheet(scaled_ss)
        except Exception:
            pass

        # 2) 统一缩放显式设置过字体的控件
        try:
            for child in self.findChildren(QWidget):
                if independently_scaled(child):
                    continue
                font = child.font()
                base_pt = child.property("ui_base_font_pt")
                if base_pt is None and font.pointSize() > 0:
                    base_pt = font.pointSize()
                    child.setProperty("ui_base_font_pt", base_pt)
                if base_pt:
                    new_pt = max(6, int(round(float(base_pt) * self.font_scale)))
                    if font.pointSize() != new_pt:
                        font.setPointSize(new_pt)
                        child.setFont(font)
        except Exception:
            pass

        # 3) 应用缩放后的基础样式
        base_style = self._base_style_raw or ""
        self.setStyleSheet(self._scale_stylesheet(base_style, self.font_scale))

        # 刷新工具栏及其子按钮的几何尺寸，确保缩放后文字完整不截断
        try:
            tb = self.findChild(QToolBar, "DataManagerToolbar")
            if tb:
                for act in tb.actions():
                    w = tb.widgetForAction(act)
                    if w:
                        w.updateGeometry()
                        w.adjustSize()
                tb.updateGeometry()
                tb.adjustSize()
        except Exception:
            pass

        # 4) 同步导入对话框的字体缩放
        try:
            if hasattr(self, "baostock_import_dialog") and self.baostock_import_dialog:
                if hasattr(self.baostock_import_dialog, "apply_ui_scale"):
                    self.baostock_import_dialog.apply_ui_scale(self.font_scale)
        except Exception:
            pass
    
    def init_ui(self):
        """初始化UI"""
        self.setWindowTitle("看海数据管理模块")

        # 根据屏幕分辨率设置窗口大小（屏幕的2/3）
        desktop = QDesktopWidget()
        screen_rect = desktop.availableGeometry(desktop.primaryScreen())
        window_width = int(screen_rect.width() * 2 / 3)
        window_height = int(screen_rect.height() * 2 / 3)
        # 居中显示
        x = (screen_rect.width() - window_width) // 2
        y = (screen_rect.height() - window_height) // 2
        self.setGeometry(x, y, window_width, window_height)

        # 设置窗口图标（与主界面一致）
        icon_path = self._get_icon_path("stock_icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        else:
            # 尝试png格式
            icon_path_png = self._get_icon_path("stock_icon.png")
            if os.path.exists(icon_path_png):
                self.setWindowIcon(QIcon(icon_path_png))

        # 设置主题样式（与主界面一致的暗色主题）
        self._base_style_raw = """
            QMainWindow, QWidget {
                background-color: #333333;
                color: #e8e8e8;
                font-family: "Microsoft YaHei UI";
                font-size: 14px;
            }
            QLabel {
                color: #e8e8e8;
                font-size: 14px;
            }
            QLineEdit, QTextEdit {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                padding: 5px;
                font-size: 14px;
            }
            QPushButton {
                background-color: #0078d4;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-family: "Microsoft YaHei UI";
                font-weight: normal;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:pressed {
                background-color: #005a9e;
            }
            QPushButton:disabled {
                background-color: #555555;
                color: #888888;
            }
            QComboBox {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                padding: 5px;
                font-size: 14px;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #e8e8e8;
                margin-right: 5px;
            }
            QComboBox QAbstractItemView {
                background-color: #404040;
                color: #e8e8e8;
                selection-background-color: #0078d4;
                font-size: 14px;
            }
            QTreeWidget, QTableView {
                background-color: #333333;
                alternate-background-color: #383838;
                color: #e8e8e8;
                border: 1px solid #404040;
                gridline-color: #404040;
                font-size: 14px;
            }
            QTreeWidget::item:selected, QTableView::item:selected {
                background-color: #505050;
                color: #ffffff;
            }
            QTreeWidget::item:hover, QTableView::item:hover {
                background-color: #404040;
            }
            QHeaderView::section {
                background-color: #404040;
                color: #e8e8e8;
                border: none;
                border-right: 1px solid #4d4d4d;
                border-bottom: 1px solid #4d4d4d;
                padding: 8px;
                font-weight: bold;
                font-size: 14px;
            }
            QGroupBox {
                background-color: #333333;
                border: 1px solid #404040;
                border-radius: 6px;
                margin-top: 1em;
                padding-top: 1em;
                color: #e8e8e8;
                font-size: 14px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: #e8e8e8;
                font-weight: bold;
                background-color: #333333;
            }
            QProgressBar {
                background-color: #404040;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                text-align: center;
                color: #e8e8e8;
                font-size: 14px;
            }
            QProgressBar::chunk {
                background-color: #0078d4;
            }
            QStatusBar {
                background-color: #333333;
                color: #e8e8e8;
                border-top: 1px solid #404040;
                font-size: 14px;
            }
            QStatusBar QLabel {
                color: #e8e8e8;
            }
            QToolBar {
                background-color: #333333;
                border: none;
                border-bottom: 1px solid #404040;
                spacing: 3px;
                padding: 5px;
            }
            QToolBar::separator {
                background-color: #404040;
                width: 1px;
                margin: 8px 5px;
            }
            QToolButton {
                background-color: #505050;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                margin: 2px;
                font-family: "Microsoft YaHei UI";
                font-weight: normal;
                font-size: 14px;
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
            QSpinBox, QDateEdit {
                background-color: #404040;
                color: #e8e8e8;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                padding: 5px;
                font-size: 14px;
            }
            QSpinBox::up-button, QDateEdit::up-button {
                background-color: #555555;
                border: none;
            }
            QSpinBox::down-button, QDateEdit::down-button {
                background-color: #555555;
                border: none;
            }
            QTabWidget::pane {
                border: 1px solid #404040;
                background-color: #333333;
            }
            QTabBar::tab {
                background-color: #404040;
                color: #e8e8e8;
                padding: 8px 16px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                font-size: 14px;
            }
            QTabBar::tab:selected {
                background-color: #333333;
                border-bottom: 2px solid #007acc;
            }
            QTabBar::tab:hover {
                background-color: #505050;
            }
            QScrollBar:vertical {
                background-color: #333333;
                width: 12px;
            }
            QScrollBar::handle:vertical {
                background-color: #555555;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #666666;
            }
            QScrollBar:horizontal {
                background-color: #333333;
                height: 12px;
            }
            QScrollBar::handle:horizontal {
                background-color: #555555;
                border-radius: 6px;
            }
            QScrollBar::handle:horizontal:hover {
                background-color: #666666;
            }
            QMenu {
                background-color: #333333;
                color: #e8e8e8;
                border: 1px solid #404040;
                font-size: 14px;
            }
            QMenu::item {
                padding: 6px 20px;
                background-color: transparent;
            }
            QMenu::item:selected {
                background-color: #505050;
            }
            QMenu::separator {
                height: 1px;
                background-color: #404040;
                margin: 2px 0px;
            }
            QToolTip {
                background-color: #555555;
                color: #e8e8e8;
                border: 1px solid #666666;
                padding: 4px;
                border-radius: 3px;
                font-size: 12px;
            }
        """
        _ui_font = get_preferred_ui_font_family() or "Microsoft YaHei UI"
        if _ui_font != "Microsoft YaHei UI":
            self._base_style_raw = self._base_style_raw.replace('"Microsoft YaHei UI"', f'"{_ui_font}"')
        self.setStyleSheet(self._base_style_raw)

        # 创建中央部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # 主布局
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(5, 5, 5, 5)

        # ========== 工具栏 ==========
        self._create_toolbar()

        # ========== 顶部：数据目录选择 ==========
        top_layout = QHBoxLayout()
        top_layout.addWidget(QLabel("数据目录:"))

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("选择或输入DuckDB数据存储目录...")
        self.path_edit.setReadOnly(True)  # 只读，通过浏览按钮选择
        top_layout.addWidget(self.path_edit, 1)

        self.browse_btn = QPushButton("浏览...")
        self.browse_btn.setMinimumWidth(80)
        self.browse_btn.clicked.connect(self.browse_data_root)
        top_layout.addWidget(self.browse_btn)

        main_layout.addLayout(top_layout)

        # ========== 主要内容区域 ==========
        splitter = QSplitter(Qt.Horizontal)

        # 左侧：股票列表树
        left_panel = self._create_left_panel()
        splitter.addWidget(left_panel)

        # 右侧：数据展示
        right_panel = self._create_right_panel()
        splitter.addWidget(right_panel)

        splitter.setSizes([300, 1100])
        main_layout.addWidget(splitter, 1)

        # ========== 状态栏 ==========
        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(200)
        self.progress_bar.setVisible(False)
        self.statusBar.addPermanentWidget(self.progress_bar)

        self.baostock_usage_label = QLabel("")
        self.baostock_usage_label.setStyleSheet("padding-left: 12px; border-left: 1px solid #555555;")
        self.baostock_usage_label.setToolTip(
            "BaoStock 当天已用的请求次数（软件每天最多用 3 万次，按数据目录分别计数）。\n"
            "增量下载前复权数据会把本地整段历史重新拉一遍，全市场只更新日线约需 1.5 万次。"
        )
        self.statusBar.addPermanentWidget(self.baostock_usage_label)
        self._baostock_usage_timer = QTimer(self)
        self._baostock_usage_timer.setInterval(15000)
        self._baostock_usage_timer.timeout.connect(self._refresh_baostock_usage_label)
        self._baostock_usage_timer.start()

        self.statusBar.showMessage("就绪")

        # 在界面构建完成后统一应用缩放，避免后续样式覆盖字号设置
        QTimer.singleShot(
            0,
            lambda owner=self: _dispatch_initial_viewer_scale(owner),
        )

    def _create_toolbar(self):
        """创建工具栏"""
        toolbar = QToolBar("工具栏")
        toolbar.setObjectName("DataManagerToolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        baostock_action = QAction("BaoStock导入", self)
        baostock_action.setToolTip("通过 BaoStock 免费下载日线、5分钟行情与指标，无需账号")
        baostock_action.triggered.connect(self.show_baostock_import_dialog)
        toolbar.addAction(baostock_action)

        tushare_action = QAction("Tushare导入", self)
        tushare_action.setToolTip("使用 Tushare 接口下载日线、1分钟、5分钟行情（需在软件设置中配置Token，分钟数据需要相应权限）")
        tushare_action.triggered.connect(self.show_tushare_import_dialog)
        toolbar.addAction(tushare_action)

        copy_cs_action = QAction("复制CS数据", self)
        copy_cs_action.setToolTip("把看海量化 CS 版的数据复制一份给开源版用，之后两边各用各的目录，互不锁库")
        copy_cs_action.triggered.connect(self.show_data_copy_dialog)
        toolbar.addAction(copy_cs_action)

        toolbar.addSeparator()

        # 占用诊断是数据库报错后的首要排查入口，放在维护区首位并保持醒目。
        self.occupancy_action = QAction("占用诊断", self)
        self.occupancy_action.setToolTip(
            "查看当前数据目录由哪些模块和PID占用，并按精确进程释放连接"
        )
        self.occupancy_action.triggered.connect(self.show_duckdb_occupancy_dialog)
        toolbar.addAction(self.occupancy_action)

        occupancy_button = toolbar.widgetForAction(self.occupancy_action)
        if occupancy_button:
            occupancy_button.setObjectName("DatabaseOccupancyButton")
            _ui_font = get_preferred_ui_font_family() or "Microsoft YaHei UI"
            occupancy_button.setStyleSheet(f"""
                QToolButton {{
                    background-color: #1677c8;
                    color: #ffffff;
                    border: none;
                    border-radius: 4px;
                    padding: 6px 12px;
                    margin: 2px;
                    font-family: "{_ui_font}";
                    font-weight: normal;
                    font-size: 14px;
                }}
                QToolButton:hover {{
                    background-color: #2389da;
                }}
                QToolButton:pressed {{
                    background-color: #0f65ad;
                }}
            """)

        # 统计
        stats_action = QAction("统计信息", self)
        stats_action.setToolTip("查看元数据索引、数据库文件和各周期的数据统计")
        stats_action.triggered.connect(self.show_statistics)
        toolbar.addAction(stats_action)

        self.reindex_action = QAction("核验索引", self)
        self.reindex_action.setToolTip(
            "只读核验未纳入索引的数据库；仅对确有行情的库备份后事务写入元数据"
        )
        self.reindex_action.triggered.connect(self.scan_data_directory)
        toolbar.addAction(self.reindex_action)

        wal_repair_action = QAction("WAL修复", self)
        wal_repair_action.setToolTip("检测并修复本地数据库WAL错误")
        wal_repair_action.triggered.connect(self.run_wal_repair)
        toolbar.addAction(wal_repair_action)

    def show_duckdb_occupancy_dialog(self):
        """打开进程级 DuckDB 占用诊断，不主动连接任何数据库文件。"""
        if self._reject_while_reindex_active("打开占用诊断"):
            return
        if not self.data_root or not os.path.isdir(self.data_root):
            QMessageBox.warning(self, "提示", "请先加载 DuckDB 数据目录")
            return
        dialog = DatabaseOccupancyDialog(self)
        dialog.exec_()

    def _create_left_panel(self) -> QWidget:
        """创建左侧面板：股票列表"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # 搜索框
        search_layout = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索股票代码...")
        self.search_edit.textChanged.connect(self.filter_stock_list)
        search_layout.addWidget(self.search_edit)
        layout.addLayout(search_layout)

        # 市场筛选
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("市场:"))
        self.market_combo = QComboBox()
        self.market_combo.addItems(["全部", "SH", "SZ", "BJ"])
        self.market_combo.currentTextChanged.connect(self.refresh_stock_list)
        filter_layout.addWidget(self.market_combo)

        filter_layout.addWidget(QLabel("周期:"))
        self.filter_period_combo = QComboBox()
        self.filter_period_combo.addItems(["全部", "1d", "1m", "5m", "tick"])
        self.filter_period_combo.currentTextChanged.connect(self.refresh_stock_list)
        filter_layout.addWidget(self.filter_period_combo)

        # 手动刷新。放在筛选栏右侧，保持轻量，不打断左侧面板布局。
        self.refresh_tree_btn = QPushButton("⟳")
        self.refresh_tree_btn.setToolTip("刷新数据列表")
        self.refresh_tree_btn.setFixedSize(32, 32)
        self.refresh_tree_btn.setStyleSheet("""
            QPushButton {
                background-color: #404040;
                color: #E0E0E0;
                border: 1px solid #505050;
                border-radius: 3px;
                font-size: 16px;
                padding: 0;
            }
            QPushButton:hover {
                background-color: #4A4A4A;
                border-color: #606060;
            }
            QPushButton:pressed {
                background-color: #353535;
            }
        """)
        self.refresh_tree_btn.clicked.connect(self.on_refresh_clicked)
        filter_layout.addWidget(self.refresh_tree_btn)
        filter_layout.addStretch()
        layout.addLayout(filter_layout)

        # 股票树
        self.stock_tree = QTreeWidget()
        self.stock_tree.setHeaderLabels(["股票/市场", "记录数"])
        self.stock_tree.setColumnWidth(0, 200)  # 增加宽度以显示股票名称
        self.stock_tree.itemClicked.connect(self.on_stock_selected)
        self.stock_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.stock_tree.customContextMenuRequested.connect(self.show_tree_context_menu)
        layout.addWidget(self.stock_tree)

        # 统计标签
        self.stats_label = QLabel("股票数: 0")
        layout.addWidget(self.stats_label)

        return panel

    def _create_right_panel(self) -> QWidget:
        """创建右侧面板：数据展示"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # 查询条件
        query_group = QGroupBox("查询条件")
        query_layout = QGridLayout(query_group)

        # 股票代码
        query_layout.addWidget(QLabel("股票代码:"), 0, 0)
        self.stock_code_label = QLabel("-")
        self.stock_code_label.setFont(QFont("Arial", 12, QFont.Bold))
        query_layout.addWidget(self.stock_code_label, 0, 1)

        # 周期选择
        query_layout.addWidget(QLabel("周期:"), 0, 2)
        self.period_combo = QComboBox()
        self.period_combo.addItems(["1d", "1m", "5m", "tick"])
        self.period_combo.currentTextChanged.connect(self.on_period_changed)
        query_layout.addWidget(self.period_combo, 0, 3)

        # 开始日期
        query_layout.addWidget(QLabel("开始日期:"), 1, 0)
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDate(QDate.currentDate().addYears(-1))
        query_layout.addWidget(self.start_date, 1, 1)

        # 结束日期
        query_layout.addWidget(QLabel("结束日期:"), 1, 2)
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDate(QDate.currentDate())
        query_layout.addWidget(self.end_date, 1, 3)

        # 查询按钮
        self.query_btn = QPushButton("查询")
        self.query_btn.clicked.connect(self.query_data)
        query_layout.addWidget(self.query_btn, 1, 4)

        # 数据完整性检查按钮
        self.integrity_btn = QPushButton("数据完整性检查")
        self.integrity_btn.clicked.connect(self.check_data_integrity)
        query_layout.addWidget(self.integrity_btn, 1, 5)

        # 导出CSV按钮
        self.export_btn = QPushButton("导出CSV")
        self.export_btn.clicked.connect(self.export_to_csv)
        query_layout.addWidget(self.export_btn, 1, 6)

        # 限制条数
        query_layout.addWidget(QLabel("限制:"), 0, 4)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(100, 10000000)
        self.limit_spin.setValue(10000)
        self.limit_spin.setSingleStep(1000)
        query_layout.addWidget(self.limit_spin, 0, 5, 1, 2)  # 跨2列

        layout.addWidget(query_group)

        # Tab页
        self.tab_widget = QTabWidget()

        # 数据表格Tab
        table_tab = QWidget()
        table_layout = QVBoxLayout(table_tab)

        self.data_table = QTableView()
        self.data_model = PandasModel()
        self.data_table.setModel(self.data_model)
        self.data_table.setAlternatingRowColors(True)
        self.data_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.data_table.setHorizontalHeader(WrapHeaderView(Qt.Horizontal, self.data_table))
        self.data_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table_layout.addWidget(self.data_table)

        # 数据信息
        self.data_info_label = QLabel("加载数据后显示信息")
        table_layout.addWidget(self.data_info_label)

        self.tab_widget.addTab(table_tab, "数据表格")

        # SQL查询Tab
        sql_tab = self._create_sql_tab()
        self.tab_widget.addTab(sql_tab, "SQL查询")

        layout.addWidget(self.tab_widget, 1)

        return panel

    def _create_sql_tab(self) -> QWidget:
        """创建SQL查询Tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # SQL输入框
        layout.addWidget(QLabel("SQL查询 (针对当前选中股票的数据库):"))
        self.sql_edit = QTextEdit()
        self.sql_edit.setMaximumHeight(100)
        self.sql_edit.setPlaceholderText(
            "输入SQL语句，例如:\n"
            "SELECT * FROM kline_1d WHERE close > 10 ORDER BY time DESC LIMIT 100\n"
            "SELECT AVG(close) as avg_price, MAX(high) as max_high FROM kline_1d"
        )
        layout.addWidget(self.sql_edit)

        # 执行按钮
        btn_layout = QHBoxLayout()
        self.exec_sql_btn = QPushButton("执行SQL")
        self.exec_sql_btn.clicked.connect(self.execute_sql)
        btn_layout.addWidget(self.exec_sql_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # SQL结果表格
        self.sql_table = QTableView()
        self.sql_model = PandasModel()
        self.sql_table.setModel(self.sql_model)
        self.sql_table.setAlternatingRowColors(True)
        self.sql_table.setHorizontalHeader(WrapHeaderView(Qt.Horizontal, self.sql_table))
        self.sql_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        layout.addWidget(self.sql_table, 1)

        # SQL结果信息
        self.sql_info_label = QLabel("")
        layout.addWidget(self.sql_info_label)

        return tab

    # ============ 事件处理 ============

    def browse_data_root(self):
        """浏览选择数据目录"""
        if self._reject_while_reindex_active("切换数据目录"):
            return
        path = QFileDialog.getExistingDirectory(
            self, "选择DuckDB数据目录",
            self.path_edit.text() or os.path.expanduser("~")
        )
        if path:
            self.path_edit.setText(path)
            # 自动加载数据目录
            self.load_data_root(path)

    def on_refresh_clicked(self):
        """刷新按钮点击"""
        if self._reject_while_reindex_active("刷新数据目录"):
            return
        if not self.manager:
            if self.data_root and os.path.exists(self.data_root):
                try:
                    self._open_manager(self.data_root, read_only=True)
                except Exception as e:
                    QMessageBox.warning(self, "恢复失败", f"无法重新连接数据目录:\n{e}")
                    return
            else:
                QMessageBox.warning(self, "提示", "请先加载数据目录")
                return

        try:
            # 强制重新初始化元数据库连接
            if self.manager._metadata_conn:
                self.manager._metadata_conn.close()
                self.manager._metadata_conn = None

            # 重新连接
            self.manager._init_metadata_db()

            # 刷新股票列表
            self.refresh_stock_list()

            self.statusBar.showMessage("股票列表已刷新")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"刷新失败:\n{e}")
            import traceback
            traceback.print_exc()

    def _close_current_manager(self, skip_checkpoint: bool = False):
        if not self.manager:
            return
        try:
            if skip_checkpoint and hasattr(self.manager, "close_all_no_checkpoint"):
                self.manager.close_all_no_checkpoint()
            else:
                self.manager.close_all()
        except Exception as e:
            print(f"关闭管理器时出错: {e}")
        finally:
            self.manager = None

    def _open_manager(self, path: str, read_only: bool):
        # 同进程读写模式切换前先释放相反模式的单例，避免 Windows 文件锁冲突。
        if read_only:
            DuckDBManager.close_writable_instances(path)
        else:
            DuckDBManager.close_read_only_instances(path)
        self.manager = DuckDBManager(data_root=path, read_only=read_only)
        self._manager_read_only = bool(read_only)
        return self.manager

    def _ensure_metadata_initialized(self, path: str) -> bool:
        """确保空数据目录也能进入数据管理模块。

        只读浏览模式无法创建 metadata.db。用户刚在设置里新建 DuckDB 路径时，
        目录通常是空的；此时先短暂打开可写 manager 初始化 metadata 和市场子目录，
        再关闭连接，让后续只读浏览正常打开。
        """
        metadata_path = os.path.join(path, 'metadata.db')
        if os.path.exists(metadata_path):
            return False

        try:
            claim_if_new(path, "数据管理初始化空目录")
        except OSError as exc:
            logging.warning(f"登记开源版数据目录失败: {exc}")
        DuckDBManager.close_read_only_instances(path)
        init_manager = DuckDBManager(data_root=path, read_only=False)
        try:
            init_manager.close_all()
        finally:
            DuckDBManager.close_writable_instances(path)
        return True

    def _ensure_writable_manager(self):
        """确保当前窗口持有可写连接；失败时尽力恢复只读浏览状态。

        DuckDB 在 Windows 下不允许一个进程以只读配置打开数据库、另一个进程
        同时以读写配置打开。切换写连接前必须先关闭当前只读 manager。旧实现
        一旦写连接创建失败便把 ``self.manager`` 永久留成 ``None``，导致用户
        再点其他导入入口时误报“请先加载数据目录”。这里把模式切换做成可恢复
        操作：保留原始异常，随后尽力恢复只读连接；即使外部写进程也阻止只读
        恢复，后续入口仍可依据 ``data_root`` 重新尝试，不再把连接状态误当成
        数据目录未配置。
        """
        if not self.data_root or not os.path.exists(self.data_root):
            raise ValueError("数据目录无效，请先选择正确的DuckDB数据目录")
        if self.manager and not getattr(self.manager, "read_only", False):
            return self.manager
        self._close_current_manager(skip_checkpoint=True)
        try:
            return self._open_manager(self.data_root, read_only=False)
        except Exception:
            # DuckDBManager.__new__ 可能已登记了一个初始化失败的写实例；创建
            # 只读实例时会先清理它。恢复失败不能覆盖最初、更有诊断价值的锁异常。
            try:
                self._open_manager(self.data_root, read_only=True)
            except Exception as restore_error:
                self.manager = None
                logging.warning(f"写连接失败后恢复只读连接失败: {restore_error}")
            raise

    def _is_reindex_scan_running(self) -> bool:
        thread = getattr(self, "scan_thread", None)
        try:
            return bool(thread is not None and thread.isRunning())
        except RuntimeError:
            self.scan_thread = None
            return False

    def _reject_while_reindex_active(self, operation_name: str) -> bool:
        if not DuckDBViewer._is_reindex_scan_running(self):
            return False
        message = f"元数据索引核验正在进行，暂不能{operation_name}。请等待核验完成或关闭窗口取消。"
        try:
            QMessageBox.information(self, "索引核验进行中", message)
            self.statusBar.showMessage(message)
        except Exception:
            pass
        return True

    def _confirm_shared_cs_write(self, operation_name: str) -> bool:
        """数据目录可能和 CS 共用时，每次写操作前都让用户确认。

        返回 True 表示可以继续。OS 自己的目录不询问；还没有数据的新目录会
        顺手登记为 OS 的目录。用户选择「这不是 CS 的目录」后补上标记，以后
        不再询问。
        """
        root = getattr(self, "data_root", None)
        if not root:
            return True
        try:
            if not may_be_shared_with_cs(root):
                claim_if_new(root, "数据管理首次写入")
                return True
        except OSError as exc:
            logging.warning(f"判断数据目录归属失败: {exc}")
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("数据目录可能和 CS 共用")
        box.setText(shared_dir_write_message(root, operation_name))
        box.setInformativeText(
            "建议给开源版单独用一个目录：点工具栏「复制CS数据」，把 CS 的数据复制一份过去。"
        )
        continue_btn = box.addButton("仍然继续（仅这一次）", QMessageBox.AcceptRole)
        not_cs_btn = box.addButton("这不是 CS 的目录，以后不再询问", QMessageBox.ActionRole)
        cancel_btn = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is not_cs_btn:
            try:
                mark_os_owned(root, "用户确认不是 CS 的目录")
            except OSError as exc:
                QMessageBox.warning(self, "无法记录", f"写入目录标记失败：{exc}")
                return False
            return True
        return clicked is continue_btn

    def _refresh_baostock_usage_label(self):
        label = getattr(self, "baostock_usage_label", None)
        if label is None:
            return
        root = getattr(self, "data_root", None)
        if not root or not os.path.isdir(root):
            label.setText("")
            return
        try:
            tracker = get_baostock_request_tracker(root)
            label.setText(f"BaoStock 今日已用 {tracker.get_count()}/{tracker.display_limit} 次")
        except Exception:
            label.setText("")

    def show_data_copy_dialog(self):
        """打开「复制 CS 数据」对话框；复制完成后可切换到新目录。"""
        from duckdb_storage.data_copy_dialog import DataCopyDialog

        existing = getattr(self, "data_copy_dialog", None)
        if existing is not None:
            try:
                existing.show()
                existing.raise_()
                existing.activateWindow()
                return
            except RuntimeError:
                self.data_copy_dialog = None
        source = ""
        root = getattr(self, "data_root", None)
        if root and may_be_shared_with_cs(root):
            source = root
        dialog = DataCopyDialog(self, source_dir=source)
        dialog.copied.connect(self._on_data_copied)
        dialog.finished.connect(lambda _code=0: setattr(self, "data_copy_dialog", None))
        self.data_copy_dialog = dialog
        dialog.show()

    def _on_data_copied(self, target: str):
        reply = QMessageBox.question(
            self,
            "改用复制出来的目录",
            f"数据已复制到：\n{target}\n\n要把开源版的数据目录改成它，并在这里打开吗？"
            "\n（回测和数据管理以后都用这个目录，不再碰 CS 的目录。）",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            import kh_settings
            kh_settings.set_value("duckdb_data_path", target)
        except Exception as exc:
            QMessageBox.warning(self, "保存设置失败", f"没能把数据目录写进设置：{exc}\n请到「设置 → 数据设置」里手动修改。")
        self.load_data_root(target)

    def _request_writable_manager(self, operation_name: str = "执行写操作"):
        """为 GUI 写入口统一申请写连接，并处理跨进程 DuckDB 占用。

        返回可写 manager 表示成功，返回 ``None`` 表示用户取消或恢复失败。
        只有用户在弹窗中明确确认后才会结束外部占用进程。
        """
        if DuckDBViewer._reject_while_reindex_active(self, operation_name):
            return None
        if not self._confirm_shared_cs_write(operation_name):
            return None
        try:
            return self._ensure_writable_manager()
        except Exception as error:
            lock_info = self._extract_duckdb_lock_info(error)
            pid = lock_info.get("pid")
            process_path = lock_info.get("process_path")
            file_path = lock_info.get("file_path")

            detail_lines = [f"{operation_name}需要打开 DuckDB 写连接，但当前无法取得写权限。"]
            if file_path:
                detail_lines.append(f"被占用文件: {file_path}")
            if process_path:
                detail_lines.append(f"占用程序: {process_path}")
            if pid:
                detail_lines.append(f"占用 PID: {pid}")

            if pid and pid != os.getpid():
                reply = QMessageBox.question(
                    self,
                    "数据库正在被其他程序使用",
                    "\n".join(detail_lines)
                    + "\n\n可能是另一个看海量化程序（例如 CS 版的补数或策略）仍在读取数据库。"
                    + "\n是否结束该占用进程并重试？\n\n未确认前不会结束任何程序。",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    self.statusBar.showMessage(f"{operation_name}已取消：DuckDB 正被其他程序占用")
                    return None

                ok, kill_message = self._terminate_process_by_pid(pid)
                if not ok:
                    QMessageBox.warning(
                        self,
                        "无法结束占用进程",
                        f"无法结束 PID {pid}:\n{kill_message}",
                    )
                    self.statusBar.showMessage(f"{operation_name}失败：无法释放 DuckDB 占用")
                    return None

                try:
                    time.sleep(0.3)
                    manager = self._ensure_writable_manager()
                    self.statusBar.showMessage(f"已释放占用，正在{operation_name}")
                    return manager
                except Exception as retry_error:
                    QMessageBox.warning(
                        self,
                        "重试失败",
                        f"占用进程已结束，但仍无法打开写连接:\n{retry_error}",
                    )
                    self.statusBar.showMessage(f"{operation_name}失败：DuckDB 写连接重试失败")
                    return None

            QMessageBox.warning(
                self,
                "无法打开写连接",
                "\n".join(detail_lines) + f"\n\n错误详情:\n{error}",
            )
            self.statusBar.showMessage(f"{operation_name}失败：无法打开 DuckDB 写连接")
            return None

    def _on_import_dialog_destroyed(self, attr_name: str):
        """导入窗口关闭后释放写连接，并恢复数据管理的只读浏览模式。"""
        # QApplication.quit() 或父窗口级联销毁时，导入子窗口的 destroyed
        # 信号可能晚于 QTreeWidget/QStatusBar 的 C++ 实例销毁。此时 Python
        # 包装对象仍可能存在，但任何控件访问都会抛 RuntimeError，甚至继续
        # 进入 SIP 造成原生崩溃。销毁阶段只清理引用，不再恢复连接或刷新 UI。
        if _qt_object_is_deleted(self):
            return
        setattr(self, attr_name, None)
        if getattr(self, "_viewer_closing", False):
            return

        # 如果还有其他导入窗口存活，其后台任务可能仍需要写 manager，不能
        # 被当前窗口的 destroyed 信号误切成只读。
        dialog_attrs = (
            "baostock_import_dialog",
            "tushare_import_dialog",
        )
        if any(getattr(self, name, None) is not None for name in dialog_attrs):
            return

        try:
            self._ensure_read_only_manager()
            self.refresh_stock_list()
            _safe_qt_method_call(
                getattr(self, "statusBar", None),
                "showMessage",
                "导入窗口已关闭，数据管理已恢复只读浏览",
            )
        except Exception as error:
            if _qt_object_is_deleted(self):
                return
            # 外部写任务可能恰好在此时接管数据库。数据目录仍然有效，后续点击
            # 刷新或任一导入入口都会重新尝试，不能再误报目录未加载。
            self.manager = None
            logging.warning(f"导入窗口关闭后恢复只读连接失败: {error}")
            _safe_qt_method_call(
                getattr(self, "statusBar", None),
                "showMessage",
                "导入窗口已关闭；数据库暂被其他任务占用，可稍后刷新",
            )

    def _ensure_read_only_manager(self):
        """写操作结束后可切回只读连接，减少对 Codex/CLI 回测的影响。"""
        if not self.data_root or not os.path.exists(self.data_root):
            raise ValueError("数据目录无效，请先选择正确的DuckDB数据目录")
        if self.manager and getattr(self.manager, "read_only", False):
            return self.manager
        self._close_current_manager(skip_checkpoint=True)
        return self._open_manager(self.data_root, read_only=True)

    def load_data_root(self, path: str, read_only: Optional[bool] = None):
        """加载数据目录"""
        if self._reject_while_reindex_active("切换数据目录"):
            return False
        if not os.path.exists(path):
            QMessageBox.warning(self, "错误", f"目录不存在: {path}")
            return

        self.data_root = path
        self.path_edit.setText(path)
        read_only = self.default_read_only if read_only is None else bool(read_only)

        # 先关闭旧的管理器
        self._close_current_manager(skip_checkpoint=True)

        # 等待一小段时间确保文件句柄被释放
        import time
        time.sleep(0.1)

        # 创建新的管理器
        try:
            initialized_empty_dir = False
            if read_only:
                initialized_empty_dir = self._ensure_metadata_initialized(path)

            self._open_manager(path, read_only=read_only)

            # 刷新股票列表
            self.refresh_stock_list()

            self._refresh_baostock_usage_label()
            mode_text = "只读浏览" if read_only else "读写管理"
            if initialized_empty_dir:
                self.statusBar.showMessage(f"已初始化空DuckDB目录并加载({mode_text}): {path}")
            else:
                self.statusBar.showMessage(f"已加载数据目录({mode_text}): {path}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"加载数据目录失败:\n{e}")
            self.manager = None

    def refresh_stock_list(self):
        """刷新股票列表（优化版本：延迟加载记录数）"""
        if DuckDBViewer._is_reindex_scan_running(self):
            self.statusBar.showMessage("元数据索引核验进行中，完成后会自动刷新股票列表")
            return
        if not self.manager:
            return

        self.stock_tree.clear()

        # 获取筛选条件
        market_filter = self.market_combo.currentText()
        period_filter = self.filter_period_combo.currentText()

        market = None if market_filter == "全部" else market_filter
        period = None if period_filter == "全部" else period_filter

        # 获取股票列表（从元数据库获取）
        stocks = self.manager.get_available_stocks(period=period, market=market)

        # 尝试导入 khQTTools 获取股票名称
        get_stock_name_func = None
        try:
            import sys
            import os
            parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if parent_dir not in sys.path:
                sys.path.insert(0, parent_dir)
            import khQTTools
            get_stock_name_func = khQTTools.get_stock_name
        except Exception:
            pass

        # 按市场分组
        market_groups = {'SH': [], 'SZ': [], 'BJ': []}
        for stock in stocks:
            if '.' in stock:
                _, m = stock.split('.')
                if m in market_groups:
                    market_groups[m].append(stock)

        # 填充树
        search_text = self.search_edit.text().strip().upper()
        total_count = 0

        for market_name, stock_list in market_groups.items():
            if market != None and market != market_name:
                continue

            if not stock_list:
                continue

            # 过滤搜索（搜索股票代码和名称）
            if search_text:
                filtered_list = []
                for s in stock_list:
                    if search_text in s.upper():
                        filtered_list.append(s)
                    elif get_stock_name_func:
                        name = get_stock_name_func(s)
                        if name and search_text in name.upper():
                            filtered_list.append(s)
                stock_list = filtered_list

            if not stock_list:
                continue

            market_item = QTreeWidgetItem([f"{market_name} ({len(stock_list)}只)", ""])
            market_item.setExpanded(True)

            for stock_code in sorted(stock_list):
                # 获取股票名称
                stock_name = get_stock_name_func(stock_code) if get_stock_name_func else ''
                # 显示格式: 股票代码 名称
                display_text = f"{stock_code} {stock_name}" if stock_name else stock_code
                stock_item = QTreeWidgetItem([display_text, "点击查看"])
                stock_item.setData(0, Qt.UserRole, stock_code)
                market_item.addChild(stock_item)
                total_count += 1

            self.stock_tree.addTopLevelItem(market_item)

        scope = "当前显示" if (market is not None or period is not None or search_text) else "可浏览"
        self.stats_label.setText(
            f"{scope}股票数: {total_count}（来自元数据索引；物理文件口径见“统计信息”）"
        )

    def filter_stock_list(self):
        """筛选股票列表"""
        self.refresh_stock_list()

    @staticmethod
    def _preferred_period_from_counts(counts: dict) -> str:
        """按显示优先级选择该股票当前最适合展示的周期。"""
        for period in ("1d", "1m", "5m", "tick"):
            try:
                if int(counts.get(period, 0) or 0) > 0:
                    return period
            except Exception:
                continue
        return "1d"

    def on_stock_selected(self, item: QTreeWidgetItem, column: int):
        """股票选中事件"""
        if self._reject_while_reindex_active("查询股票数据"):
            return
        stock_code = item.data(0, Qt.UserRole)
        if stock_code:
            self.current_stock = stock_code
            # 尝试显示股票名称（形如: 110074.SH (某ETF)），若失败则回退显示代码
            try:
                import khQTTools
                display_text = khQTTools.get_stock_display(stock_code)
            except Exception:
                display_text = stock_code
            self.stock_code_label.setText(display_text)

            # 延迟加载：选中时更新该股票的记录数
            try:
                stock_db = self.manager.get_stock_db(stock_code)
                counts = stock_db.get_all_counts()
                count_str = (
                    f"1d:{counts.get('1d', 0)} "
                    f"1m:{counts.get('1m', 0)} "
                    f"5m:{counts.get('5m', 0)} "
                    f"tick:{counts.get('tick', 0)}"
                )
                item.setText(1, count_str)
                preferred_period = self._preferred_period_from_counts(counts)
                if self.period_combo.currentText() != preferred_period:
                    self.period_combo.blockSignals(True)
                    self.period_combo.setCurrentText(preferred_period)
                    self.period_combo.blockSignals(False)
                self.current_period = preferred_period
            except Exception:
                item.setText(1, "加载失败")

            self.query_data()

    def on_period_changed(self, period: str):
        """周期改变"""
        self.current_period = period
        if self.current_stock:
            self.query_data()

    def query_data(self):
        """查询数据"""
        if self._reject_while_reindex_active("查询行情数据"):
            return
        if not self.manager or not self.current_stock:
            return

        start = self.start_date.date().toString("yyyyMMdd")
        end = self.end_date.date().toString("yyyyMMdd")
        period = self.period_combo.currentText()

        # 在状态栏也显示带名称的显示文本（若可用）
        try:
            import khQTTools
            display_text = khQTTools.get_stock_display(self.current_stock)
        except Exception:
            display_text = self.current_stock
        self.statusBar.showMessage(f"正在查询 {display_text} {period} 数据...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        # 使用线程加载数据
        self.load_thread = DataLoadThread(
            self.manager, self.current_stock, period, start, end
        )
        self.load_thread.finished.connect(self.on_data_loaded)
        self.load_thread.error.connect(self.on_load_error)
        self.load_thread.progress.connect(self.progress_bar.setValue)
        self.load_thread.start()

    def on_data_loaded(self, df: pd.DataFrame):
        """数据加载完成"""
        self.progress_bar.setVisible(False)

        # 限制显示条数
        limit = self.limit_spin.value()
        display_df = df.tail(limit) if len(df) > limit else df

        self.data_model.update_data(display_df)

        # 更新信息
        info = f"共 {len(df)} 条记录"
        if len(df) > limit:
            info += f" (显示最近 {limit} 条)"

        if len(df) > 0:
            info += f" | 时间范围: {df['time'].min()} ~ {df['time'].max()}"

        self.data_info_label.setText(info)
        self.statusBar.showMessage(f"查询完成: {len(df)} 条记录")

    def on_load_error(self, error: str):
        """数据加载错误"""
        self.progress_bar.setVisible(False)
        QMessageBox.warning(self, "查询错误", error)
        self.statusBar.showMessage(f"查询失败: {error}")

    def check_data_integrity(self):
        """检查数据完整性"""
        if self._reject_while_reindex_active("检查数据完整性"):
            return
        if not self.manager or not self.current_stock:
            QMessageBox.warning(self, "提示", "请先选择股票并查询数据")
            return
        
        start = self.start_date.date().toString("yyyyMMdd")
        end = self.end_date.date().toString("yyyyMMdd")
        period = self.period_combo.currentText()
        
        # 转换日期格式为YYYY-MM-DD
        start_date_str = self.start_date.date().toString("yyyy-MM-dd")
        end_date_str = self.end_date.date().toString("yyyy-MM-dd")
        
        # 打开数据完整性检查对话框
        dialog = DataIntegrityDialog(
            self.manager, self.current_stock, period,
            start_date_str, end_date_str, self
        )
        dialog.exec_()

    def execute_sql(self):
        """执行SQL查询"""
        if self._reject_while_reindex_active("执行 SQL 查询"):
            return
        if not self.manager or not self.current_stock:
            QMessageBox.warning(self, "提示", "请先选择一只股票")
            return

        sql = self.sql_edit.toPlainText().strip()
        if not sql:
            return

        try:
            stock_db = self.manager.get_stock_db(self.current_stock)
            df = stock_db.execute_sql(sql)

            self.sql_model.update_data(df)
            self.sql_info_label.setText(f"查询结果: {len(df)} 行, {len(df.columns)} 列")

        except Exception as e:
            QMessageBox.warning(self, "SQL执行错误", str(e))
            self.sql_info_label.setText(f"错误: {e}")

    def show_tree_context_menu(self, pos):
        """显示右键菜单"""
        item = self.stock_tree.itemAt(pos)
        if not item:
            return

        stock_code = item.data(0, Qt.UserRole)
        if not stock_code:
            return

        menu = QMenu(self)

        # 查看详情
        view_action = menu.addAction("查看数据")
        view_action.triggered.connect(lambda: self.on_stock_selected(item, 0))

        # 导出
        export_action = menu.addAction("导出CSV")
        export_action.triggered.connect(lambda: self.export_stock_data(stock_code))

        menu.addSeparator()

        # 删除（谨慎操作）
        delete_action = menu.addAction("删除数据")
        delete_action.triggered.connect(lambda: self.delete_stock_data(stock_code))

        menu.exec_(self.stock_tree.mapToGlobal(pos))

    def export_stock_data(self, stock_code: str):
        """导出单只股票数据"""
        if not self.manager:
            return

        period = self.period_combo.currentText()

        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出CSV",
            f"{stock_code}_{period}.csv",
            "CSV文件 (*.csv)"
        )

        if file_path:
            df = self.manager.get_kline_data(stock_code, period)
            df.to_csv(file_path, index=False, encoding='utf-8-sig')
            QMessageBox.information(self, "导出成功", f"已导出 {len(df)} 条记录到:\n{file_path}")

    def export_to_csv(self):
        """导出当前显示的数据"""
        if self.data_model._df is None or len(self.data_model._df) == 0:
            QMessageBox.warning(self, "提示", "没有数据可导出")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出CSV",
            f"{self.current_stock}_{self.current_period}.csv",
            "CSV文件 (*.csv)"
        )

        if file_path:
            self.data_model._df.to_csv(file_path, index=False, encoding='utf-8-sig')
            QMessageBox.information(self, "导出成功",
                                   f"已导出 {len(self.data_model._df)} 条记录到:\n{file_path}")

    def delete_stock_data(self, stock_code: str):
        """删除股票数据"""
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除 {stock_code} 的所有数据吗?\n此操作不可恢复!",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            try:
                manager = self._request_writable_manager(f"删除 {stock_code} 数据")
                if manager is None:
                    return
                # 获取数据库路径并删除文件
                config = manager.config
                db_path = config.get_db_path(stock_code)

                if os.path.exists(db_path):
                    # 先关闭连接
                    if stock_code in manager._stock_dbs:
                        manager._stock_dbs[stock_code].close()
                        del manager._stock_dbs[stock_code]

                    # 删除数据库文件
                    os.remove(db_path)

                # 删除元数据库中的记录
                manager.delete_stock_metadata(stock_code)

                # 从树形列表中移除该项
                self._remove_stock_from_tree(stock_code)

                QMessageBox.information(self, "删除成功", f"已删除 {stock_code} 的数据")

            except Exception as e:
                QMessageBox.warning(self, "删除失败", str(e))
                import traceback
                traceback.print_exc()
            finally:
                # 删除是短写操作，结束后立即恢复只读，避免数据管理窗口继续
                # 阻塞研究看板、CLI 或其他只读进程。
                try:
                    self._ensure_read_only_manager()
                except Exception as restore_error:
                    logging.warning(f"删除操作后恢复只读连接失败: {restore_error}")

    def _remove_stock_from_tree(self, stock_code: str):
        """从树形列表中移除股票项

        Args:
            stock_code: 股票代码
        """
        # 遍历所有市场节点
        for i in range(self.stock_tree.topLevelItemCount()):
            market_item = self.stock_tree.topLevelItem(i)

            # 遍历市场节点下的所有股票
            for j in range(market_item.childCount()):
                stock_item = market_item.child(j)
                item_code = stock_item.data(0, Qt.UserRole)

                if item_code == stock_code:
                    # 找到了，删除该项
                    market_item.removeChild(stock_item)

                    # 如果市场节点下没有股票了，更新显示
                    if market_item.childCount() == 0:
                        market_item.setText(0, f"{market_item.text(0).split('(')[0]} (0)")
                    else:
                        # 更新市场节点的计数
                        market_name = market_item.text(0).split('(')[0].strip()
                        market_item.setText(0, f"{market_name} ({market_item.childCount()})")

                    return

    def scan_data_directory(self):
        """后台只读核验候选库；确有行情时才申请写连接同步。"""
        if self.scan_thread is not None and self.scan_thread.isRunning():
            QMessageBox.information(self, "索引核验", "索引核验正在进行，请稍候。")
            return
        if not self.manager:
            QMessageBox.warning(self, "提示", "请先加载数据目录")
            return
        blockers = self._local_database_release_blockers()
        if blockers:
            QMessageBox.warning(
                self,
                "暂不能核验索引",
                "以下数据任务仍在运行：" + "、".join(blockers) + "。请先正常结束任务。",
            )
            return
        reply = QMessageBox.question(
            self,
            "核验并同步元数据索引",
            "系统将先在后台只读核验未纳入索引的数据库文件。\n\n"
            "空行情库和非证券文件会跳过；只有发现真实行情时，才会先备份 "
            "metadata.db，再以单个事务补充索引。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            manager = self._ensure_read_only_manager()
        except Exception as exc:
            QMessageBox.warning(self, "无法开始核验", str(exc))
            return

        self.statusBar.showMessage("正在只读核验未纳入索引的数据库...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.reindex_action.setEnabled(False)
        self.scan_thread = ScanThread(manager)
        self.scan_thread.progress.connect(self.progress_bar.setValue)
        self.scan_thread.finished.connect(self._on_scan_thread_done)
        self.scan_thread.start()

    @staticmethod
    def _format_reindex_summary(summary: Dict[str, Any]) -> str:
        return (
            f"核验文件: {int(summary.get('candidate_files', 0) or 0)}\n"
            f"整库未入索引候选: {int(summary.get('unindexed_candidates', 0) or 0)}\n"
            f"已有证券周期标志候选: {int(summary.get('period_flag_candidates', 0) or 0)}\n"
            f"新增索引: {int(summary.get('updated_stocks', 0) or 0)}\n"
            f"有行情待补库: {int(summary.get('valid_data_files', 0) or 0)}\n"
            f"空行情库跳过: {int(summary.get('empty_data_files', 0) or 0)}\n"
            "无缺失周期数据跳过: "
            f"{int(summary.get('no_missing_period_data_files', 0) or 0)}\n"
            f"非证券库跳过: {int(summary.get('non_stock_files', 0) or 0)}\n"
            f"读取失败: {int(summary.get('failed_files', 0) or 0)}"
        )

    def on_scan_finished(self, summary):
        """只读核验完成；仅对完整且有数据的结果执行备份和事务写入。"""
        self.progress_bar.setVisible(False)
        if self._pending_close_after_scan or summary.get("cancelled"):
            self.statusBar.showMessage("索引核验已取消，metadata.db 未修改")
            return

        final_summary = dict(summary)
        if int(summary.get("failed_files", 0) or 0) > 0:
            self._last_reindex_summary = final_summary
            details = self._format_reindex_summary(final_summary)
            failures = "\n".join(summary.get("failures", [])[:5])
            QMessageBox.warning(
                self,
                "索引核验未提交",
                details
                + "\n\n存在无法读取的候选库，为避免部分提交，metadata.db 未修改。"
                + (f"\n\n失败样例:\n{failures}" if failures else ""),
            )
            self.statusBar.showMessage("索引核验存在读取失败，未修改 metadata.db")
            return

        if int(summary.get("valid_data_files", 0) or 0) > 0:
            manager = self._request_writable_manager("同步元数据索引")
            if manager is None:
                self._last_reindex_summary = final_summary
                return
            try:
                final_summary = manager.apply_metadata_reindex(summary)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "索引同步失败",
                    f"只读核验已经完成，但写入失败：\n{exc}\n\nmetadata.db 事务未提交。",
                )
                self.statusBar.showMessage("索引写入失败，metadata.db 事务未提交")
                return
            finally:
                try:
                    self._ensure_read_only_manager()
                except Exception:
                    pass

        self._last_reindex_summary = final_summary
        try:
            self._ensure_read_only_manager()
        except Exception:
            pass
        self.refresh_stock_list()
        summary_text = self._format_reindex_summary(final_summary)
        if final_summary.get("backup_path"):
            summary_text += f"\n\n元数据备份: {final_summary['backup_path']}"
        if int(final_summary.get("updated_stocks", 0) or 0) > 0:
            title = "索引同步完成"
            self.statusBar.showMessage("索引核验与同步完成")
        else:
            title = "索引核验完成"
            summary_text += "\n\n没有发现漏索引的有效行情库，metadata.db 未修改。"
            self.statusBar.showMessage("索引核验完成：没有有效行情库需要补索引")
        QMessageBox.information(self, title, summary_text)

    def on_scan_error(self, error: str):
        """核验线程异常。"""
        self.progress_bar.setVisible(False)
        if not self._pending_close_after_scan:
            QMessageBox.warning(self, "索引核验错误", error)
            self.statusBar.showMessage("索引核验失败，metadata.db 未修改")

    def _on_scan_thread_done(self):
        thread = self.scan_thread
        summary = getattr(thread, "result", None) if thread is not None else None
        error_message = getattr(thread, "error_message", "") if thread is not None else ""
        self.scan_thread = None
        if thread is not None:
            thread.deleteLater()
        self.reindex_action.setEnabled(True)
        if self._pending_close_after_scan:
            self._pending_close_after_scan = False
            QTimer.singleShot(0, self.close)
        elif error_message:
            self.on_scan_error(error_message)
        elif summary is not None:
            self.on_scan_finished(summary)

    def show_statistics(self):
        """显示统计信息"""
        if self._reject_while_reindex_active("读取统计信息"):
            return
        if not self.manager:
            QMessageBox.warning(self, "提示", "请先加载数据目录")
            return

        stats = self.manager.get_statistics()

        msg = f"""数据目录统计信息

数据路径: {stats['data_root']}
可浏览股票数（元数据索引）: {stats['indexed_stocks']}
物理 .db 文件总数: {stats['total_database_files']}
六位证券数据库文件: {stats['total_stocks']}
未纳入索引的六位文件: {stats['unverified_unindexed_database_files']}
周期标志核验候选证券库: {stats.get('period_flag_candidate_files', 0)}
非证券数据库文件: {stats['non_stock_database_files']}
仅有元数据但文件缺失: {stats['metadata_only_stocks']}
总大小: {stats['total_size_mb']} MB

各市场:
"""
        for market, info in stats['markets'].items():
            msg += (
                f"  {market}: 可浏览 {info.get('indexed_stocks', 0)} 只 / "
                f"证券库 {info['stocks']} 个 / .db文件 {info.get('database_files', info['stocks'])} 个, "
                f"{info['size_mb']} MB\n"
            )

        msg += f"""
各周期股票数:
  日线(1d): {stats['by_period']['1d']}
  1分钟(1m): {stats['by_period']['1m']}
  5分钟(5m): {stats['by_period']['5m']}
  Tick: {stats['by_period']['tick']}

说明:
  左侧股票树与“可浏览股票数”均使用 metadata.db 索引。
  “未纳入索引的六位文件”可能是空行情库，核验后仍会保留，不等于漏数据。
  “周期标志核验候选”只表示至少一个周期标志为否，不代表该周期存在行情。
  请用工具栏“核验索引”做只读确认。
  核验只会把确有行情的证券库写入索引，空库和非证券文件不会污染股票树。
"""

        if self._last_reindex_summary:
            msg += "\n本次会话最近一次核验:\n" + self._format_reindex_summary(
                self._last_reindex_summary
            ) + "\n"

        QMessageBox.information(self, "统计信息", msg)

    def run_wal_repair(self):
        if self._reject_while_reindex_active("执行 WAL 修复"):
            return
        if not self.manager:
            QMessageBox.warning(self, "提示", "请先加载数据目录")
            return
        if not self._confirm_shared_cs_write("WAL 修复"):
            return

        confirm = QMessageBox.question(
            self,
            "确认修复",
            "将检测并修复全部数据库的WAL错误，是否继续？",
            QMessageBox.Yes | QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return

        if self.wal_repair_thread:
            try:
                if self.wal_repair_thread.isRunning():
                    QMessageBox.information(self, "提示", "修复任务正在进行")
                    return
            except Exception:
                pass

        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.statusBar.showMessage("正在执行WAL修复...")

        self.wal_repair_thread = WalRepairThread(self.manager.data_root)
        self.wal_repair_thread.finished.connect(self._on_wal_repair_finished)
        self.wal_repair_thread.error.connect(self._on_wal_repair_error)
        self.wal_repair_thread.start()

    def _on_wal_repair_finished(self, result: dict):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setVisible(False)
        self.statusBar.showMessage("WAL修复完成")

        msg = (
            f"总计: {result.get('total', 0)}\n"
            f"正常: {result.get('ok', 0)}\n"
            f"修复: {result.get('fixed', 0)}\n"
            f"重建: {result.get('rebuilt', 0)}\n"
            f"失败: {len(result.get('failed', []))}\n"
            f"删除WAL/SHM: {result.get('removed_sidecars', 0)}"
        )

        if result.get("rebuilt", 0) > 0:
            msg += f"\n隔离目录: {result.get('quarantine_root', '')}"

        QMessageBox.information(self, "WAL修复结果", msg)

    def _on_wal_repair_error(self, error: str):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setVisible(False)
        self.statusBar.showMessage("WAL修复失败")
        QMessageBox.warning(self, "WAL修复失败", error)

    def _extract_duckdb_lock_info(self, error: Exception):
        """从DuckDB文件锁错误中提取占用进程信息"""
        import re

        msg = str(error) if error is not None else ""
        pid = None
        process_path = None
        file_path = None

        file_match = re.search(
            r'(?:Cannot open file|Could not set lock on file)\s+"([^"]+)"',
            msg,
            flags=re.IGNORECASE,
        )
        if file_match:
            file_path = file_match.group(1)

        pid_match = re.search(r'\(PID\s+(\d+)\)', msg)
        if pid_match:
            try:
                pid = int(pid_match.group(1))
            except Exception:
                pid = None

        proc_match = re.search(
            r'(?:open in|held in)\s+(.+?)\s*\(PID',
            msg,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if proc_match:
            process_path = " ".join(proc_match.group(1).split())

        return {
            "message": msg,
            "pid": pid,
            "process_path": process_path,
            "file_path": file_path
        }

    def _terminate_process_by_pid(
        self,
        pid: int,
        expected_create_time: Optional[float] = None,
    ):
        """结束精确PID；提供启动时间时先防止PID复用误杀。"""
        import subprocess

        if pid <= 0:
            return False, "无效的PID"
        if pid == os.getpid():
            return False, "拒绝结束当前GUI进程"

        if expected_create_time is not None:
            identity, identity_error = inspect_process_identity(pid)
            if identity is None:
                return False, f"无法复核进程身份，进程可能已经退出: {identity_error}"
            actual_create_time = identity.create_time
            if actual_create_time is None or abs(actual_create_time - expected_create_time) > 0.01:
                return False, "PID 已被系统复用，已拒绝结束新进程"

        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                text=True,
                errors="replace",
                shell=False
            )
            if result.returncode == 0:
                msg = (result.stdout or "").strip() or f"已结束进程 PID {pid}"
                logging.info("已按用户确认结束 DuckDB 占用进程 PID %s", pid)
                return True, msg

            err_msg = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()
            return False, err_msg or f"结束进程失败 (PID {pid})"
        except Exception as e:
            return False, str(e)

    def _local_database_release_blockers(self) -> List[str]:
        """返回当前窗口中不能直接断开数据库的活动模块。"""
        blockers = []
        if DuckDBViewer._is_reindex_scan_running(self):
            blockers.append("元数据索引核验")
        try:
            load_thread = getattr(self, "load_thread", None)
            if load_thread is not None and load_thread.isRunning():
                blockers.append("行情数据查询")
        except RuntimeError:
            self.load_thread = None
        for attr_name, display_name in (
            ("baostock_import_dialog", "BaoStock 导入"),
            ("tushare_import_dialog", "Tushare 导入"),
        ):
            try:
                if getattr(self, attr_name, None) is not None:
                    blockers.append(display_name)
            except RuntimeError:
                setattr(self, attr_name, None)
        try:
            if self.wal_repair_thread is not None and self.wal_repair_thread.isRunning():
                blockers.append("WAL 修复")
        except RuntimeError:
            self.wal_repair_thread = None

        # 桌面回测在主GUI进程的QThread内运行。此时系统句柄只能看到同一个PID，
        # 无法把“数据管理连接”和“回测连接”分开；必须拒绝一键重置单例。
        try:
            for window in QApplication.topLevelWidgets():
                if window is self:
                    continue
                strategy_thread = getattr(window, "strategy_thread", None)
                if strategy_thread is not None and strategy_thread.isRunning():
                    blockers.append("桌面回测/策略运行")
                    break
        except (AttributeError, RuntimeError):
            pass
        return blockers

    def _release_local_duckdb_connections(self):
        """安全释放当前GUI进程持有的连接，不自动重新连接。"""
        blockers = self._local_database_release_blockers()
        if blockers:
            return (
                False,
                "以下数据任务或窗口仍在使用当前连接："
                + "、".join(blockers)
                + "。请先正常停止并关闭这些模块，避免中断写库。",
            )

        release_errors = []
        try:
            self._close_current_manager(skip_checkpoint=True)
        except Exception as exc:
            release_errors.append(f"关闭当前管理器失败: {exc}")

        try:
            DuckDBManager.reset_instance()
        except Exception as exc:
            release_errors.append(f"重置DuckDB单例失败: {exc}")

        try:
            try:
                from . import xtdata_adapter as _xt_adapter
            except ImportError:
                import xtdata_adapter as _xt_adapter
            if hasattr(_xt_adapter, "reset_manager"):
                _xt_adapter.reset_manager()
        except Exception as exc:
            release_errors.append(f"重置xtdata_adapter失败: {exc}")

        try:
            from khDataSource import get_data_source_manager

            data_source_manager = get_data_source_manager()
            if data_source_manager is not None:
                adapter = getattr(data_source_manager, "_duckdb_adapter", None)
                if adapter is not None and hasattr(adapter, "reset_manager"):
                    adapter.reset_manager()
        except Exception as exc:
            release_errors.append(f"重置数据源DuckDB适配器失败: {exc}")

        try:
            import gc

            gc.collect()
            time.sleep(0.2)
        except Exception as exc:
            release_errors.append(f"等待文件句柄释放失败: {exc}")

        if release_errors:
            message = (
                "未能确认当前数据管理进程的 DuckDB 连接已全部释放。"
                "为避免误判，请勿继续强制结束外部进程；可关闭数据管理窗口后重试。"
                "\n\n失败步骤：\n"
            ) + "\n".join(
                f"- {item}" for item in release_errors
            )
            logging.warning(
                "数据管理释放本进程DuckDB连接失败，errors=%s detail=%s",
                len(release_errors),
                "; ".join(release_errors),
            )
            return False, message

        message = "已释放当前数据管理进程持有的 DuckDB 连接。"
        logging.info("数据管理已释放本进程DuckDB连接")
        return True, message

    def _reopen_manager_after_release(self):
        """释放连接后重建管理器并刷新列表"""
        if not self.data_root or not os.path.exists(self.data_root):
            raise ValueError("数据目录无效，请先选择正确的DuckDB数据目录")

        self._open_manager(self.data_root, read_only=True)
        self.refresh_stock_list()

    def release_duckdb_occupancy(self):
        """释放DuckDB占用（先释放本进程连接，必要时可结束外部占用进程）"""
        self.statusBar.showMessage("正在释放DuckDB占用...")

        released, release_message = self._release_local_duckdb_connections()
        if not released:
            QMessageBox.warning(self, "无法释放连接", release_message)
            self.statusBar.showMessage("DuckDB占用释放已取消")
            return

        try:
            self._reopen_manager_after_release()
            msg = release_message + "\n\n已重新加载数据目录。"
            QMessageBox.information(self, "完成", msg)
            self.statusBar.showMessage("DuckDB占用已释放")
            return
        except Exception as reopen_error:
            lock_info = self._extract_duckdb_lock_info(reopen_error)
            pid = lock_info.get("pid")
            process_path = lock_info.get("process_path")
            file_path = lock_info.get("file_path")

            detail_lines = ["释放本进程连接后，仍无法重新打开DuckDB。"]
            if file_path:
                detail_lines.append(f"被锁文件: {file_path}")
            if process_path:
                detail_lines.append(f"占用进程: {process_path}")
            if pid:
                detail_lines.append(f"占用PID: {pid}")
            detail_lines.append(release_message)

            if pid and pid != os.getpid():
                reply = QMessageBox.question(
                    self,
                    "检测到外部占用",
                    "\n".join(detail_lines) + "\n\n是否尝试结束该占用进程并自动重试？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No
                )
                if reply == QMessageBox.Yes:
                    ok, kill_msg = self._terminate_process_by_pid(pid)
                    if not ok:
                        QMessageBox.warning(
                            self,
                            "结束进程失败",
                            f"无法结束占用进程 (PID {pid}):\n{kill_msg}"
                        )
                        self.statusBar.showMessage("DuckDB占用释放失败")
                        return

                    try:
                        import time
                        time.sleep(0.3)
                        self._reopen_manager_after_release()
                        QMessageBox.information(
                            self,
                            "完成",
                            f"已结束占用进程并重新加载数据目录。\n\n{kill_msg}"
                        )
                        self.statusBar.showMessage("DuckDB占用已释放")
                        return
                    except Exception as retry_error:
                        QMessageBox.warning(
                            self,
                            "重试失败",
                            f"结束进程后重试仍失败:\n{retry_error}"
                        )
                        self.statusBar.showMessage("DuckDB占用释放失败")
                        return

            QMessageBox.warning(
                self,
                "释放失败",
                "\n".join(detail_lines) + f"\n\n错误详情:\n{reopen_error}"
            )
            self.statusBar.showMessage("DuckDB占用释放失败")


        # 导入完成后会自动刷新列表（在对话框的finished信号中处理）


    def show_baostock_import_dialog(self):
        manager = self._request_writable_manager("打开 BaoStock 导入")
        if manager is None:
            return

        if self.baostock_import_dialog:
            try:
                if self.baostock_import_dialog.isVisible():
                    self.baostock_import_dialog.raise_()
                    self.baostock_import_dialog.activateWindow()
                    return
            except RuntimeError:
                self.baostock_import_dialog = None

        self.baostock_import_dialog = BaoStockImportDialog(manager, self)
        self.baostock_import_dialog.destroyed.connect(
            lambda _obj=None, owner=self:
            _dispatch_import_dialog_destroyed(owner, "baostock_import_dialog")
        )
        if hasattr(self.baostock_import_dialog, "apply_ui_scale"):
            self.baostock_import_dialog.apply_ui_scale(get_ui_font_scale())
        self.baostock_import_dialog.show()


    def show_tushare_import_dialog(self):
        """显示Tushare导入对话框"""
        # 检查 token 是否已配置
        if not load_tushare_settings().token:
            QMessageBox.warning(
                self, "未配置Token",
                "请先在【软件设置 → Tushare设置】中填写 Tushare Token，然后再使用此功能。"
            )
            return
        manager = self._request_writable_manager("打开 Tushare 导入")
        if manager is None:
            return

        if self.tushare_import_dialog:
            try:
                if self.tushare_import_dialog.isVisible():
                    self.tushare_import_dialog.raise_()
                    self.tushare_import_dialog.activateWindow()
                    return
            except RuntimeError:
                self.tushare_import_dialog = None

        self.tushare_import_dialog = TushareImportDialog(manager, self)
        self.tushare_import_dialog.destroyed.connect(
            lambda _obj=None, owner=self:
            _dispatch_import_dialog_destroyed(owner, "tushare_import_dialog")
        )
        if hasattr(self.tushare_import_dialog, "apply_ui_scale"):
            self.tushare_import_dialog.apply_ui_scale(get_ui_font_scale())
        self.tushare_import_dialog.show()


    def closeEvent(self, event):
        """窗口关闭事件"""
        self._viewer_closing = True
        scan_thread = getattr(self, "scan_thread", None)
        if scan_thread is not None and scan_thread.isRunning():
            self._pending_close_after_scan = True
            scan_thread.requestInterruption()
            self.reindex_action.setEnabled(False)
            self.statusBar.showMessage(
                "正在取消只读索引核验，完成后将自动关闭；metadata.db 不会写入"
            )
            event.ignore()
            return
        if self._pending_close_after_tushare_stop:
            event.ignore()
            return
        running_dialogs = []
        for attr_name, display_name in (
            ("baostock_import_dialog", "BaoStock"),
            ("tushare_import_dialog", "Tushare"),
        ):
            dialog = getattr(self, attr_name, None)
            if dialog is None:
                continue
            try:
                if dialog.is_import_running():
                    running_dialogs.append((attr_name, display_name, dialog))
            except RuntimeError:
                setattr(self, attr_name, None)

        if running_dialogs:
            names = "、".join(item[1] for item in running_dialogs)
            reply = QMessageBox.question(
                self,
                "确认关闭",
                f"{names} 数据任务正在进行中，确定要关闭数据管理模块吗？\n"
                "系统会先停止领取新任务，等待当前写入完成并收尾数据库，再关闭窗口。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self._pending_close_after_tushare_stop = True
                self._pending_import_dialogs_to_close = {
                    item[0] for item in running_dialogs
                }
                for attr_name, _display_name, dialog in running_dialogs:
                    dialog.destroyed.connect(
                        lambda _obj=None, name=attr_name:
                        self._finish_close_after_import_stop(name)
                    )
                    dialog.request_close_after_stop()
                event.ignore()
                return

            self._viewer_closing = False
            event.ignore()
            return

        if self.manager:
            self.manager.close_all()
        event.accept()

    def _finish_close_after_tushare_stop(self):
        """兼容旧调用：等待导入线程安全退出后，再关闭主窗口。"""
        self._finish_close_after_import_stop("tushare_import_dialog")

    def _finish_close_after_import_stop(self, attr_name: str):
        """等待所有活动导入窗口完成数据库收尾后，再关闭主窗口。"""
        pending = getattr(self, "_pending_import_dialogs_to_close", set())
        pending.discard(attr_name)
        self._pending_import_dialogs_to_close = pending
        if pending:
            return
        self._pending_close_after_tushare_stop = False
        QTimer.singleShot(0, self.close)


# ============================================================
# 导入对话框公共工具
# ============================================================


def fit_dialog_height_to_content(dialog, tab_widget=None, max_height=1200):
    """把对话框高度撑到当前页内容所需高度（受屏幕可用高度限制）。

    BaoStock / Tushare 导入对话框把"开始/停止/关闭"按钮放在可滚动页里，
    窗口按 800x600 的最小尺寸打开时按钮落在可视区之外，用户在默认窗口里
    找不到开始按钮。这里在窗口显示后按内容需要的高度撑一次；屏幕放不下时
    仍然可以滚动，不会把窗口顶出屏幕。
    """
    try:
        page = tab_widget.currentWidget() if tab_widget is not None else None
        scroll = page if isinstance(page, QScrollArea) else None
        if scroll is None and page is not None:
            scroll = page.findChild(QScrollArea)
        if scroll is None or scroll.widget() is None:
            return
        # 视口之外的部分（状态栏、标签栏、边距）维持原样，只补内容差额
        deficit = scroll.widget().sizeHint().height() - scroll.viewport().height()
        if deficit <= 0:
            return
        available = QApplication.desktop().availableGeometry(dialog)
        target = min(dialog.height() + deficit + 8, max_height, available.height() - 60)
        if target <= dialog.height():
            return
        dialog.resize(dialog.width(), int(target))
        frame = dialog.frameGeometry()
        if frame.bottom() > available.bottom():
            dialog.move(dialog.x(), max(available.top(), available.bottom() - frame.height()))
    except Exception:
        pass


def _baostock_unsupported_code(stock_code: str) -> bool:
    """BaoStock 只提供 A 股股票和指数，场内基金（ETF / LOF 等）和可转债都返回空。"""
    if is_listed_fund_code(stock_code):
        return True
    code, market = split_security_code(stock_code)
    return (market == "SH" and code.startswith("11")) or (market == "SZ" and code.startswith("12"))


class BaoStockRequestTracker:
    def __init__(self, data_root: str, daily_limit: int = 30000, display_limit: int = 30000):
        self.data_root = data_root
        self.daily_limit = daily_limit
        self.display_limit = display_limit
        self.file_path = os.path.join(self.data_root, "baostock_request_usage.json")
        self._lock = Lock()
        self._date = QDate.currentDate().toString("yyyyMMdd")
        self._count = 0
        self._last_saved_count = 0
        self.limit_message = f"已达到BaoStock当日请求上限{self.daily_limit}次，已停止下载"
        self._load()

    def _load(self):
        if not os.path.exists(self.file_path):
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            date_value = str(data.get("date", ""))
            count_value = int(data.get("count", 0))
            if date_value == self._date:
                self._count = max(0, count_value)
                self._last_saved_count = self._count
        except Exception:
            pass

    def _save(self):
        try:
            data = {"date": self._date, "count": self._count}
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._last_saved_count = self._count
        except Exception:
            pass

    def _reset_if_new_day(self):
        current = QDate.currentDate().toString("yyyyMMdd")
        if current != self._date:
            self._date = current
            self._count = 0
            self._last_saved_count = 0
            self._save()

    def get_count(self) -> int:
        with self._lock:
            self._reset_if_new_day()
            return self._count

    def is_limit_reached(self) -> bool:
        with self._lock:
            self._reset_if_new_day()
            return self._count >= self.daily_limit

    def consume(self, n: int = 1) -> bool:
        with self._lock:
            self._reset_if_new_day()
            if self._count + n > self.daily_limit:
                return False
            self._count += n
            if self._count - self._last_saved_count >= 20 or self._count == self.daily_limit:
                self._save()
            return True

    def flush(self):
        """立即持久化请求计数，避免少于20次的短任务丢失计数。"""
        with self._lock:
            self._reset_if_new_day()
            if self._count != self._last_saved_count:
                self._save()


_BAOSTOCK_REQUEST_TRACKERS = {}
_BAOSTOCK_REQUEST_TRACKERS_LOCK = Lock()


def get_baostock_request_tracker(data_root: str) -> BaoStockRequestTracker:
    """同一数据目录复用一个 BaoStock 请求计数器。"""
    key = os.path.normcase(os.path.abspath(data_root or os.getcwd()))
    with _BAOSTOCK_REQUEST_TRACKERS_LOCK:
        tracker = _BAOSTOCK_REQUEST_TRACKERS.get(key)
        if tracker is None:
            tracker = BaoStockRequestTracker(key)
            _BAOSTOCK_REQUEST_TRACKERS[key] = tracker
        return tracker


class BaoStockImportThread(QThread):
    progress = pyqtSignal(int, int, int)
    status = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    conflict = pyqtSignal(str, str, int, int)
    request_count = pyqtSignal(int)

    def __init__(
        self,
        manager: DuckDBManager,
        stocks: List[str],
        periods_config: Dict[str, dict],
        request_tracker=None,
        num_workers: int = 2,
        timeout_per_task: float = 120.0,
        force_overwrite: bool = False,
        include_front_adjusted: bool = True,
        include_back_adjusted: bool = True,
    ):
        super().__init__()
        self.manager = manager
        self.stocks = stocks
        self.periods_config = periods_config
        self._is_running = True
        self._mutex = QMutex()
        self._wait = QWaitCondition()
        self._pending_resolution = None
        self._pending_apply_all = False
        self._default_resolution = None
        self.request_tracker = request_tracker
        self.num_workers = max(1, int(num_workers))
        self.timeout_per_task = float(timeout_per_task)
        self.force_overwrite = bool(force_overwrite)
        self.include_front_adjusted = bool(include_front_adjusted)
        self.include_back_adjusted = bool(include_back_adjusted)
        self._mp_importer = None

    def stop(self):
        self._is_running = False
        self._mutex.lock()
        self._wait.wakeAll()
        self._mutex.unlock()
        try:
            if self._mp_importer is not None and getattr(self._mp_importer, "is_running", False):
                self._mp_importer.force_stop()
        except Exception:
            pass

    def set_conflict_resolution(self, resolution: str, apply_all: bool):
        self._mutex.lock()
        self._pending_resolution = resolution
        self._pending_apply_all = apply_all
        self._wait.wakeAll()
        self._mutex.unlock()

    def _to_baostock_code(self, stock_code: str) -> str:
        code = stock_code.strip().upper()
        if '.' in code:
            num, market = code.split('.')
            if market in ('SH', 'SZ', 'BJ'):
                return f"{market.lower()}.{num}"
        return code.lower()

    @staticmethod
    def _to_baostock_date(value: str) -> str:
        from .incremental import normalize_date8

        date8 = normalize_date8(value)
        return f"{date8[:4]}-{date8[4:6]}-{date8[6:]}"

    def _build_incremental_plan(
        self,
        stock_code: str,
        period: str,
        start_date: str,
        end_date: str,
        *,
        include_adjusted: bool = True,
        include_front_adjusted: bool = True,
        include_back_adjusted: bool = True,
    ):
        from .incremental import (
            RAW_COMPLETENESS_COLUMNS,
            build_incremental_plan,
            required_price_columns,
        )

        return build_incremental_plan(
            self.manager,
            stock_code,
            period,
            start_date,
            end_date,
            # raw、前复权、后复权可由调用方独立规划；这样既能识别旧库
            # 缺失的派生列，也不会因单一复权缺口重复写入完整 raw。
            required_columns=(
                required_price_columns(
                    front=include_front_adjusted,
                    back=include_back_adjusted,
                )
                if include_adjusted else RAW_COMPLETENESS_COLUMNS
            ),
        )

    def _adjustment_refresh_range(
        self,
        stock_code: str,
        period: str,
        requested_start: str,
        requested_end: str,
    ) -> Tuple[str, str]:
        """把前复权刷新范围扩展到库内该周期的全部 raw 历史。"""

        from .incremental import normalize_date8

        start8 = normalize_date8(requested_start)
        end8 = normalize_date8(requested_end)
        getter = getattr(self.manager, "get_kline_data_range", None)
        if not callable(getter):
            return start8, end8
        try:
            local_start, local_end = getter(stock_code, period)
            if local_start is not None:
                start8 = min(start8, normalize_date8(str(local_start)))
            if local_end is not None:
                end8 = max(end8, normalize_date8(str(local_end)))
            return start8, end8
        finally:
            try:
                self.manager.close_stock_connection(
                    stock_code, skip_checkpoint=True,
                )
            except Exception:
                pass

    def _validate_adjustment_refresh_frame(
        self,
        frame: pd.DataFrame,
        stock_code: str,
        period: str,
        start_date: str,
        end_date: str,
        columns: List[str],
    ) -> Optional[int]:
        """确认一次前复权结果完整覆盖库内 raw 后再原子更新。

        这一步阻止 BaoStock 全历史请求半途截断或部分空值时把一部分旧行
        刷成新基准、另一部分仍留在旧基准。旧测试替身没有读取接口时保持
        兼容；正式 DuckDBManager 始终执行严格比对。
        """

        getter = getattr(self.manager, "get_kline_data", None)
        if not callable(getter):
            return
        missing_columns = [column for column in columns if column not in frame.columns]
        if missing_columns:
            raise RuntimeError(
                f"前复权返回缺少字段: {', '.join(missing_columns)}"
            )
        local = getter(
            stock_code,
            period,
            start_time=start_date,
            end_time=end_date,
            fields=["time"],
        )
        if local is None or local.empty or "time" not in local.columns:
            raise RuntimeError("本地 raw 行情为空，拒绝应用前复权刷新")

        if "time" not in frame.columns:
            raise RuntimeError("前复权返回缺少 time")
        local_times = pd.to_datetime(local["time"], errors="coerce")
        source_times_all = pd.to_datetime(frame["time"], errors="coerce")
        if local_times.isna().any() or source_times_all.isna().any():
            raise RuntimeError("前复权刷新包含无效时间，已拒绝应用")
        import math

        numeric_columns = frame[columns].apply(pd.to_numeric, errors="coerce")
        invalid_columns = [
            column for column in columns
            if numeric_columns[column].isna().any()
            or not numeric_columns[column].map(math.isfinite).all()
        ]
        if invalid_columns:
            raise RuntimeError(
                "前复权刷新包含空值或无效数值: "
                + ", ".join(invalid_columns)
            )
        valid_source = frame.copy()
        source_times = pd.to_datetime(valid_source["time"], errors="coerce")
        if period == "1d":
            local_keys = set(local_times.dt.strftime("%Y%m%d"))
            source_keys = set(source_times.dt.strftime("%Y%m%d"))
            all_source_keys = source_times_all.dt.strftime("%Y%m%d")
        else:
            local_keys = set(local_times.astype("int64").tolist())
            source_keys = set(source_times.astype("int64").tolist())
            all_source_keys = source_times_all.astype("int64")
        if all_source_keys.duplicated().any():
            raise RuntimeError("前复权刷新返回重复行情时间，已拒绝应用")
        missing = local_keys - source_keys
        unexpected = source_keys - local_keys
        if missing or unexpected:
            raise RuntimeError(
                f"前复权结果未覆盖本地 {len(missing)} 个行情时间点，"
                f"并包含 {len(unexpected)} 个本地 raw 之外的时间点；"
                "已拒绝部分刷新以避免混用两套基准"
            )
        return len(local_keys)

    def _mark_adjustment_columns_pending(
        self,
        stock_code: str,
        period: str,
        columns: List[str],
    ) -> int:
        """清空无法证明一致的复权列，让下一次增量必然重新补齐。"""

        clearer = getattr(self.manager, "clear_kline_columns", None)
        if not callable(clearer) or not columns:
            return 0
        return int(clearer(stock_code, period, list(columns)) or 0)

    # 说明：_fetch_kline/_prepare_base_df/_merge_adjusted 已迁移至子进程 worker 中执行，
    # 主线程仅负责冲突处理与写入 DuckDB。

    def _get_existing_times(self, stock_db, period: str, start_time: datetime, end_time: datetime) -> List[datetime]:
        table_name = stock_db.PERIOD_TABLE_MAP.get(period)
        if not table_name:
            return []
        try:
            tables = stock_db.conn.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_name = ?
            """, [table_name]).fetchall()
            if not tables:
                return []
            rows = stock_db.conn.execute(
                f"SELECT time FROM {table_name} WHERE time >= ? AND time <= ?",
                [start_time, end_time]
            ).fetchall()
            return [row[0] for row in rows]
        except Exception:
            return []

    def _resolve_conflict(self, stock: str, period: str, existing_count: int, new_count: int) -> str:
        if self._default_resolution:
            return self._default_resolution
        self._mutex.lock()
        self._pending_resolution = None
        self._pending_apply_all = False
        self._mutex.unlock()
        self.conflict.emit(stock, period, existing_count, new_count)
        self._mutex.lock()
        while self._pending_resolution is None and self._is_running:
            self._wait.wait(self._mutex)
        resolution = self._pending_resolution or 'skip'
        apply_all = self._pending_apply_all
        self._mutex.unlock()
        if apply_all:
            self._default_resolution = resolution
        return resolution

    def _ensure_benchmark_incremental(self, imported_records: list, results: dict):
        """按缺口补充 000300.SH；指数每段只消耗一次 BaoStock 请求。"""

        import baostock as bs
        import pandas as pd
        from datetime import datetime, timedelta

        benchmark_code = '000300.SH'
        end_date = datetime.now()
        start_date = end_date - timedelta(days=365 * 20)
        start_text = start_date.strftime("%Y-%m-%d")
        end_text = end_date.strftime("%Y-%m-%d")
        self.status.emit("检查基准指数 000300.SH 增量缺口...")

        try:
            plan = self._build_incremental_plan(
                benchmark_code, '1d', start_text, end_text,
            )
        finally:
            try:
                self.manager.close_stock_connection(
                    benchmark_code, skip_checkpoint=True,
                )
            except Exception:
                pass

        from .incremental import full_range, group_missing_trade_dates

        anomalous = set(plan.anomalous_dates)
        normal_unresolved = (
            set(plan.missing_dates) | set(plan.partial_dates)
        ) - anomalous
        download_specs = [
            (*full_range('1d', group_start, group_end), False)
            for group_start, group_end in group_missing_trade_dates(
                normal_unresolved, plan.expected_dates,
            )
        ]
        download_specs.extend(
            (*full_range('1d', date8, date8), True)
            for date8 in sorted(anomalous)
        )

        if not download_specs:
            results['benchmark_up_to_date'] = True
            self.status.emit("基准指数 000300.SH 已是最新")
            return

        self.status.emit(
            f"000300.SH 发现 {len(download_specs)} 段缺口，正在增量补充..."
        )
        login_result = bs.login()
        if login_result.error_code != '0':
            message = f"BaoStock 登录失败，无法补充 000300.SH: {login_result.error_msg}"
            results['benchmark_unresolved'] = list(plan.unresolved_dates)
            self.status.emit(f"警告：{message}")
            return

        try:
            for range_start, range_end, overwrite_range in download_specs:
                if not self._is_running:
                    break
                if self.request_tracker and not self.request_tracker.consume(1):
                    results['benchmark_unresolved'] = list(plan.unresolved_dates)
                    raise RuntimeError(self.request_tracker.limit_message)
                if self.request_tracker:
                    self.request_count.emit(self.request_tracker.get_count())
                start_dash = self._to_baostock_date(range_start)
                end_dash = self._to_baostock_date(range_end)
                rs = bs.query_history_k_data_plus(
                    "sh.000300",
                    "date,code,open,high,low,close,preclose,volume,amount,pctChg",
                    start_date=start_dash,
                    end_date=end_dash,
                    frequency="d",
                )
                if rs.error_code != '0':
                    raise RuntimeError(rs.error_msg or rs.error_code)
                rows = []
                while (rs.error_code == '0') & rs.next():
                    rows.append(rs.get_row_data())
                if not rows:
                    results['benchmark_empty'] = int(
                        results.get('benchmark_empty', 0) or 0
                    ) + 1
                    self.status.emit(
                        f"警告：000300.SH {start_dash} ~ {end_dash} 返回空行情"
                    )
                    continue

                frame = pd.DataFrame(rows, columns=rs.fields)
                frame["time"] = (
                    pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="coerce")
                    + pd.Timedelta(hours=9, minutes=30)
                )
                if "preclose" in frame.columns:
                    frame["preClose"] = frame["preclose"]
                for column in (
                    "open", "high", "low", "close", "preClose", "volume", "amount"
                ):
                    if column in frame.columns:
                        frame[column] = pd.to_numeric(frame[column], errors="coerce")
                for suffix in ('front', 'back', 'front_ratio', 'back_ratio'):
                    for field in ('open', 'high', 'low', 'close'):
                        if field in frame.columns:
                            frame[f'{field}_{suffix}'] = frame[field]
                keep = [
                    "time", "open", "high", "low", "close", "preClose",
                    "volume", "amount",
                ] + [
                    f"{field}_{suffix}"
                    for suffix in ('front', 'back', 'front_ratio', 'back_ratio')
                    for field in ('open', 'high', 'low', 'close')
                ]
                frame = frame[[column for column in keep if column in frame.columns]]
                frame = frame.dropna(subset=["time"]).sort_values("time")
                if frame.empty:
                    continue
                if overwrite_range:
                    from .incremental import validate_overwrite_frame

                    validate_overwrite_frame(
                        frame,
                        '1d',
                        [
                            date8 for date8 in plan.expected_dates
                            if range_start <= date8 <= range_end
                        ],
                    )
                # BaoStock 成交量为股，与主导入路径一样在入口统一换算为手；
                # 放在所有筛选之后，保证写库的 frame 带着单位标记。
                from .units import normalize_kline_units

                frame = normalize_kline_units(frame, source_volume_unit="shares")
                saved = self.manager.save_kline_data(
                    frame,
                    benchmark_code,
                    '1d',
                    'none',
                    skip_metadata=True,
                    overwrite=overwrite_range,
                    merge_missing=not overwrite_range,
                    **(
                        {"overwrite_trade_dates": True}
                        if overwrite_range else {}
                    ),
                )
                if saved > 0:
                    imported_records.append((benchmark_code, '1d', int(saved)))
                self.manager.close_stock_connection(
                    benchmark_code, skip_checkpoint=True,
                )
        finally:
            try:
                bs.logout()
            except Exception:
                pass
            try:
                self.manager.close_stock_connection(
                    benchmark_code, skip_checkpoint=True,
                )
            except Exception:
                pass

        try:
            remaining = self._build_incremental_plan(
                benchmark_code, '1d', start_text, end_text,
            )
            results['benchmark_unresolved'] = list(remaining.unresolved_dates)
            if remaining.unresolved_dates:
                self.status.emit(
                    f"警告：000300.SH 仍有 {len(remaining.unresolved_dates)} 个待核验交易日"
                )
            else:
                self.status.emit("基准指数 000300.SH 增量补充完成")
        finally:
            try:
                self.manager.close_stock_connection(
                    benchmark_code, skip_checkpoint=True,
                )
            except Exception:
                pass

    def run(self):
        results = {
            'success': 0,
            'failed': 0,
            'empty': 0,
            'up_to_date': 0,
            'total_records': 0,
            'cancelled': False,
            'planned_ranges': 0,
            'unresolved': [],
            'benchmark_unresolved': [],
            'benchmark_error': '',
            'benchmark_empty': 0,
            'adjustment_failed': 0,
            'limit_reached': False,
        }
        limit_reached = False
        imported_records = []
        short_lock_enabled = False
        try:
            try:
                if hasattr(self.manager, 'enable_short_lock_write'):
                    self.manager.enable_short_lock_write()
                    short_lock_enabled = True
            except Exception as exc:
                self.status.emit(f"短锁写入模式启用失败，将继续使用普通模式: {exc}")

            try:
                self._ensure_benchmark_incremental(imported_records, results)
            except Exception as e:
                results['benchmark_error'] = str(e)
                self.status.emit(f"检查/补充 000300.SH 基准时出错: {e}")

            completed_tasks = 0

            # 下载前先扫描本地覆盖；默认只提交缺失/部分交易日区间。
            tasks = []
            planned_stock_periods = {}
            for period, config in self.periods_config.items():
                if not self._is_running:
                    break
                start_date = config["start"]
                end_date = config["end"]
                for stock_code in self.stocks:
                    if not self._is_running:
                        break
                    key = (stock_code, period)
                    first_task_index = len(tasks)
                    raw_plan = None
                    front_plan = None
                    back_plan = None
                    if self.force_overwrite:
                        from .incremental import expected_trade_dates, normalize_date8

                        force_expected, ignored_open = expected_trade_dates(
                            start_date, end_date,
                        )
                        if not force_expected:
                            results['up_to_date'] += 1
                            if ignored_open:
                                self.status.emit(
                                    f"{stock_code} {period} 仅包含尚未收盘/未来交易日，"
                                    "未执行强制覆写"
                                )
                            continue
                        range_specs = [(start_date, end_date, True)]
                        plan_expected_dates = force_expected
                    else:
                        self.status.emit(f"扫描 {stock_code} {period} 增量缺口...")
                        try:
                            # raw、用户选中的前/后复权分别规划。前复权缺口只需
                            # 全历史复权任务，不能为了补派生列重复写 raw。
                            raw_plan = self._build_incremental_plan(
                                stock_code,
                                period,
                                start_date,
                                end_date,
                                include_adjusted=False,
                            )
                            if self._is_running and self.include_back_adjusted:
                                back_plan = self._build_incremental_plan(
                                    stock_code,
                                    period,
                                    start_date,
                                    end_date,
                                    include_front_adjusted=False,
                                    include_back_adjusted=True,
                                )
                            if self._is_running and self.include_front_adjusted:
                                front_plan = self._build_incremental_plan(
                                    stock_code,
                                    period,
                                    start_date,
                                    end_date,
                                    include_front_adjusted=True,
                                    include_back_adjusted=False,
                                )
                            # 后复权被选择时，以 raw+back 的覆盖结果规划普通
                            # 下载区间；否则只要求 raw 完整。
                            plan = back_plan or raw_plan
                        finally:
                            try:
                                self.manager.close_stock_connection(
                                    stock_code, skip_checkpoint=True,
                                )
                            except Exception:
                                pass
                        if not self._is_running:
                            break
                        from .incremental import (
                            full_range,
                            group_missing_trade_dates,
                            normalize_date8,
                        )

                        anomalous = set(plan.anomalous_dates)
                        normal_unresolved = (
                            set(plan.missing_dates) | set(plan.partial_dates)
                        ) - anomalous
                        normal_groups = group_missing_trade_dates(
                            normal_unresolved, plan.expected_dates,
                        )
                        range_specs = [
                            (*full_range(period, group_start, group_end), False)
                            for group_start, group_end in normal_groups
                        ]
                        # 超额时间戳无法靠 INSERT/MERGE 清理，逐日覆写才能恢复到
                        # 数据源返回的规范条数；只覆写异常日，不扩大到相邻正常日。
                        range_specs.extend(
                            (*full_range(period, date8, date8), True)
                            for date8 in sorted(anomalous)
                        )
                        if plan.partial_dates:
                            self.status.emit(
                                f"{stock_code} {period} 有 {len(plan.partial_dates)} 个部分交易日，"
                                "将按整日请求并安全合并"
                            )
                        if plan.anomalous_dates:
                            self.status.emit(
                                f"{stock_code} {period} 有 {len(plan.anomalous_dates)} 个条数异常交易日，"
                                "将逐日修复覆写"
                            )
                        if plan.ignored_open_dates:
                            self.status.emit(
                                f"{stock_code} {period} 暂不核验尚未收盘/未来交易日"
                            )
                        plan_expected_dates = plan.expected_dates

                    raw_unresolved_dates = set()
                    if raw_plan is not None:
                        raw_unresolved_dates.update(raw_plan.missing_dates)
                        raw_unresolved_dates.update(raw_plan.partial_dates)
                    front_needs_download = bool(
                        self.include_front_adjusted
                        and (
                            self.force_overwrite
                            or (
                                front_plan is not None
                                and front_plan.needs_download
                            )
                        )
                    )
                    planned_stock_periods[key] = (start_date, end_date)
                    for range_start, range_end, overwrite_range in range_specs:
                        range_start8 = normalize_date8(range_start)
                        range_end8 = normalize_date8(range_end)
                        expected_for_range = [
                            date8 for date8 in plan_expected_dates
                            if range_start8 <= date8 <= range_end8
                        ]
                        raw_change_expected = bool(
                            self.force_overwrite
                            or raw_unresolved_dates.intersection(
                                expected_for_range
                            )
                        )
                        do_front = bool(
                            self.include_front_adjusted and raw_change_expected
                        )
                        do_back = bool(self.include_back_adjusted)
                        tasks.append({
                            "stock_code": stock_code,
                            "bs_code": self._to_baostock_code(stock_code),
                            "period": period,
                            "start_date": self._to_baostock_date(range_start),
                            "end_date": self._to_baostock_date(range_end),
                            "overwrite_range": overwrite_range,
                            "expected_dates": expected_for_range,
                            "task_kind": "kline",
                            # 纯后复权缺口只请求 raw+后复权用于填列；raw
                            # 已完整，不触碰前复权，也不触发任何失效操作。
                            "do_front": do_front,
                            "do_back": do_back,
                            "raw_change_expected": raw_change_expected,
                            "request_cost": 1 + int(do_front) + int(do_back),
                        })
                    has_local_raw = bool(
                        raw_plan is not None
                        and (
                            raw_plan.complete_dates
                            or raw_plan.partial_dates
                        )
                    )
                    if range_specs or (
                        front_needs_download and has_local_raw
                    ):
                        from .incremental import normalize_date8

                        refresh_start, refresh_end = self._adjustment_refresh_range(
                            stock_code, period, start_date, end_date,
                        )
                        selected_start = normalize_date8(start_date)
                        selected_end = normalize_date8(end_date)
                        refresh_expanded = (
                            refresh_start != selected_start
                            or refresh_end != selected_end
                        )
                        needs_basis_refresh = bool(
                            self.include_front_adjusted
                            and (
                                (self.force_overwrite and refresh_expanded)
                                or (
                                    not self.force_overwrite
                                    and front_needs_download
                                    and (has_local_raw or refresh_expanded)
                                )
                            )
                        )
                    else:
                        needs_basis_refresh = False
                    if needs_basis_refresh:
                        # 前复权以最新复权因子为基准。增量新增日期时，如果只
                        # 写新行，旧行会保留上一次基准并形成混合口径。因此在
                        # 已有历史数据的情况下，用一次请求刷新库内完整区间。
                        if refresh_expanded:
                            self.status.emit(
                                f"{stock_code} {period} 前复权刷新范围已扩展到本地全部历史 "
                                f"{refresh_start} ~ {refresh_end}"
                            )
                        # 新缺口先只保存 raw + 后复权。前复权必须等全历史结果
                        # 通过覆盖校验后再一次性更新；全量请求失败时新行保持
                        # NULL，而不是与旧历史混用两套基准。
                        for pending_task in tasks[first_task_index:]:
                            if (
                                pending_task.get("task_kind") == "kline"
                                and pending_task.get("raw_change_expected", True)
                            ):
                                pending_task["do_front"] = False
                                pending_task["request_cost"] = (
                                    1 + int(bool(pending_task.get("do_back", False)))
                                )
                        tasks.append({
                            "stock_code": stock_code,
                            "bs_code": self._to_baostock_code(stock_code),
                            "period": period,
                            "start_date": self._to_baostock_date(refresh_start),
                            "end_date": self._to_baostock_date(refresh_end),
                            "task_kind": "adjustment",
                            "adjustflag": "2",
                            "suffix": "front",
                            "columns": [
                                "open_front", "high_front",
                                "low_front", "close_front",
                            ],
                            "request_cost": 1,
                        })
                    if not range_specs and not needs_basis_refresh:
                        planned_stock_periods.pop(key, None)
                        results['up_to_date'] += 1

            # 所有 raw 区间必须先落库，随后才能校验并应用全历史前复权。
            # 多进程任务仅靠 append 顺序不能形成依赖，显式设置全局阶段屏障。
            tasks.sort(
                key=lambda item: 1 if item.get("task_kind") == "adjustment" else 0
            )
            raw_task_count = sum(
                1 for item in tasks if item.get("task_kind") != "adjustment"
            )
            raw_completed = 0
            total_tasks = len(tasks)
            results['planned_ranges'] = total_tasks

            if not tasks:
                compact_metadata = _coalesce_metadata_records(imported_records)
                if compact_metadata:
                    try:
                        self.manager.batch_update_metadata(compact_metadata)
                    except Exception as exc:
                        self.status.emit(f"警告：基准已写入，但元数据刷新失败: {exc}")
                try:
                    self.manager.close_metadata_connection()
                except Exception:
                    pass
                if short_lock_enabled and hasattr(self.manager, 'disable_short_lock_write'):
                    self.manager.disable_short_lock_write()
                    short_lock_enabled = False
                if not self._is_running:
                    results["cancelled"] = True
                    self.status.emit("BaoStock 增量扫描已停止，未提交行情请求")
                else:
                    self.status.emit("所选 BaoStock 行情均已是最新，无需发起行情请求")
                self.finished.emit(results)
                return

            self.status.emit(f"使用 {self.num_workers} 个进程并行下载 BaoStock 数据...")

            try:
                from .baostock_import_worker import (
                    MultiProcessBaoStockImporter,
                    sanitize_requested_adjustments,
                )
            except ImportError:
                pending_count = sum(
                    1 for task in tasks
                    if task.get("task_kind") == "adjustment"
                )
                results["adjustment_failed"] += pending_count
                results["failed"] += max(1, pending_count)
                self.error.emit("未找到 baostock_import_worker 模块，请确保文件存在。请重新安装开源版。")
                self.finished.emit(results)
                return

            importer = MultiProcessBaoStockImporter(
                num_workers=self.num_workers,
                timeout_per_task=self.timeout_per_task,
                max_task_retries=2,
                retry_backoff=(2.0, 5.0),
            )
            self._mp_importer = importer
            importer.start()

            next_submit = 0
            in_flight = 0
            id_to_task = {}

            def _consume_requests_for_task(task: dict) -> bool:
                request_cost = int(task.get("request_cost", 3) or 3)
                if not self.request_tracker:
                    return True
                if not self.request_tracker.consume(request_cost):
                    return False
                try:
                    self.request_count.emit(self.request_tracker.get_count())
                except Exception:
                    pass
                return True

            def _submit_task(task: dict) -> int:
                if task.get("task_kind") == "adjustment":
                    return importer.add_adjustment_task(
                        task["stock_code"],
                        task["bs_code"],
                        task["period"],
                        task["start_date"],
                        task["end_date"],
                        adjustflag=task.get("adjustflag", "2"),
                        suffix=task.get("suffix", "front"),
                    )
                task_options = {}
                if task.get("do_front") is False:
                    task_options["do_front"] = False
                if task.get("do_back") is False:
                    task_options["do_back"] = False
                return importer.add_task(
                    task["stock_code"],
                    task["bs_code"],
                    task["period"],
                    task["start_date"],
                    task["end_date"],
                    **task_options,
                )

            def _next_task_ready() -> bool:
                if next_submit >= len(tasks):
                    return False
                return not (
                    tasks[next_submit].get("task_kind") == "adjustment"
                    and raw_completed < raw_task_count
                )

            # 预提交填满管道
            while (
                in_flight < self.num_workers
                and _next_task_ready()
                and self._is_running
            ):
                t = tasks[next_submit]
                if not _consume_requests_for_task(t):
                    limit_reached = True
                    results['limit_reached'] = True
                    self._is_running = False
                    self.error.emit(self.request_tracker.limit_message)
                    break
                tid = _submit_task(t)
                id_to_task[tid] = t
                next_submit += 1
                in_flight += 1

            # 主循环：收结果 → 写库 → 提交下一个
            while completed_tasks < total_tasks and in_flight > 0 and self._is_running:
                result = importer.get_result(timeout=0.5)
                for event in importer.get_all_progress():
                    if event.get("type") == "retry":
                        retry_cost = int(event.get("request_cost", 3) or 3)
                        if self.request_tracker and not self.request_tracker.consume(retry_cost):
                            limit_reached = True
                            results['limit_reached'] = True
                            self._is_running = False
                            self.error.emit(self.request_tracker.limit_message)
                            break
                        if self.request_tracker:
                            self.request_count.emit(self.request_tracker.get_count())
                        self.status.emit(
                            f"↻ {event.get('stock_code')} {event.get('period')} "
                            f"连接或登录异常，{event.get('delay', 0):g}秒后进行"
                            f"第{event.get('attempt')}次重试"
                        )
                    elif event.get("type") == "fatal":
                        self.status.emit(str(event.get("msg") or "BaoStock工作进程异常"))
                if not self._is_running:
                    break
                if not result:
                    continue

                tid = result.get("task_id")
                orig = id_to_task.pop(tid, None)
                stock_code = (orig or {}).get("stock_code") or result.get("stock_code") or result.get("stock_code", "")
                period = (orig or {}).get("period") or result.get("period", "")

                try:
                    if result.get("success"):
                        df_dict = result.get("df_dict")
                        if df_dict:
                            df = dict_to_dataframe(df_dict)
                        else:
                            df = pd.DataFrame()

                        task_kind = (orig or {}).get("task_kind", "kline")
                        if df is None or df.empty:
                            results["empty"] += 1
                            if task_kind == "adjustment":
                                results["adjustment_failed"] += 1
                                self.status.emit(
                                    f"○ {stock_code} {period} 前复权返回空数据；"
                                    "raw 已保留，前复权全历史保持待补状态"
                                )
                            else:
                                self.status.emit(f"○ {stock_code} {period} 无数据")
                        else:
                            force = bool((orig or {}).get("overwrite_range", False))
                            adjustment_errors = list(
                                result.get("adjustment_errors") or []
                            )
                            if task_kind != "adjustment":
                                requested_suffixes = []
                                if bool((orig or {}).get("do_front", True)):
                                    requested_suffixes.append("front")
                                if bool((orig or {}).get("do_back", True)):
                                    requested_suffixes.append("back")
                                df, defensive_errors = sanitize_requested_adjustments(
                                    df,
                                    requested_suffixes,
                                )
                                adjustment_errors.extend(
                                    message for message in defensive_errors
                                    if message not in adjustment_errors
                                )
                            if task_kind == "adjustment":
                                columns = list((orig or {}).get("columns") or [])
                                expected_updates = self._validate_adjustment_refresh_frame(
                                    df,
                                    stock_code,
                                    period,
                                    str((orig or {}).get("start_date") or ""),
                                    str((orig or {}).get("end_date") or ""),
                                    columns,
                                )
                                records = self.manager.update_kline_columns(
                                    df,
                                    stock_code,
                                    period,
                                    columns,
                                )
                                if (
                                    expected_updates is not None
                                    and int(records or 0) != expected_updates
                                ):
                                    raise RuntimeError(
                                        f"数据库仅匹配 {int(records or 0)}/"
                                        f"{expected_updates} 行，前复权刷新未完整"
                                    )
                            else:
                                if force:
                                    from .incremental import (
                                        required_price_columns,
                                        validate_overwrite_frame,
                                    )

                                    if adjustment_errors:
                                        raise RuntimeError(
                                            "复权返回不完整，拒绝整日覆写，原数据未改动："
                                            + "；".join(adjustment_errors)
                                        )

                                    validate_overwrite_frame(
                                        df,
                                        period,
                                        list((orig or {}).get("expected_dates") or []),
                                        required_columns=required_price_columns(
                                            front=bool((orig or {}).get("do_front", True)),
                                            back=bool((orig or {}).get("do_back", True)),
                                        ),
                                    )
                                from .incremental import (
                                    FRONT_ADJUSTMENT_COLUMNS,
                                    adjustment_columns_to_invalidate,
                                )

                                records = self.manager.save_kline_data(
                                    df,
                                    stock_code,
                                    period,
                                    "none",
                                    skip_metadata=True,
                                    overwrite=force,
                                    merge_missing=not force,
                                    invalidate_adjustment_columns=(
                                        adjustment_columns_to_invalidate(
                                            front_valid=bool(
                                                (orig or {}).get("do_front", True)
                                                and all(
                                                    f"{field}_front" in df.columns
                                                    for field in ("open", "high", "low", "close")
                                                )
                                            ),
                                            back_valid=bool(
                                                (orig or {}).get("do_back", True)
                                                and all(
                                                    f"{field}_back" in df.columns
                                                    for field in ("open", "high", "low", "close")
                                                )
                                            ),
                                        )
                                        if bool(
                                            (orig or {}).get(
                                                "raw_change_expected", True
                                            )
                                        ) else ()
                                    ),
                                    invalidate_adjustment_columns_full_history=(
                                        FRONT_ADJUSTMENT_COLUMNS
                                        if (
                                            bool(
                                                (orig or {}).get(
                                                    "raw_change_expected", True
                                                )
                                            )
                                            and not bool(
                                                (orig or {}).get("do_front", True)
                                            )
                                        )
                                        else ()
                                    ),
                                    **(
                                        {"overwrite_trade_dates": True}
                                        if force else {}
                                    ),
                                )
                            if records > 0:
                                imported_records.append((stock_code, period, int(records)))
                                results["success"] += 1
                                results["total_records"] += records
                                action = (
                                    "刷新前复权口径" if task_kind == "adjustment"
                                    else "覆写" if force else "增量处理"
                                )
                                self.status.emit(
                                    f"✓ {stock_code} {period} {action} {records} 条数据"
                                )
                            else:
                                results["empty"] += 1
                                self.status.emit(f"○ {stock_code} {period} 无新数据")

                            if adjustment_errors:
                                results["adjustment_failed"] += 1
                                self.status.emit(
                                    f"警告：{stock_code} {period} raw 已保存，"
                                    "对应旧复权已随 raw 同事务失效，但复权未完整："
                                    + "；".join(adjustment_errors)
                                )

                        try:
                            del df
                        except Exception:
                            pass
                    else:
                        results["failed"] += 1
                        err = result.get("error", "未知错误")
                        if (orig or {}).get("task_kind") == "adjustment":
                            results["adjustment_failed"] += 1
                        self.status.emit(f"✗ {stock_code} {period} 失败: {err}")
                except Exception as e:
                    results["failed"] += 1
                    if (orig or {}).get("task_kind") == "adjustment":
                        results["adjustment_failed"] += 1
                    self.status.emit(f"✗ {stock_code} {period} 失败: {e}")
                finally:
                    try:
                        self.manager.close_stock_connection(
                            stock_code, skip_checkpoint=True
                        )
                    except Exception:
                        pass

                completed_tasks += 1
                in_flight -= 1
                if (orig or {}).get("task_kind", "kline") != "adjustment":
                    raw_completed += 1
                self.progress.emit(int((completed_tasks / total_tasks) * 100) if total_tasks else 100, completed_tasks, total_tasks)

                # 提交下一个保持管道满
                while (
                    in_flight < self.num_workers
                    and _next_task_ready()
                    and self._is_running
                ):
                    t = tasks[next_submit]
                    if not _consume_requests_for_task(t):
                        limit_reached = True
                        results['limit_reached'] = True
                        self._is_running = False
                        self.error.emit(self.request_tracker.limit_message)
                        break
                    tid2 = _submit_task(t)
                    id_to_task[tid2] = t
                    next_submit += 1
                    in_flight += 1

                if completed_tasks % 20 == 0:
                    self.manager.cleanup_connections_aggressive(skip_checkpoint=True)

            try:
                importer.stop()
            except Exception:
                pass

            self._mp_importer = None

            # 下载完成后重新读取数据库覆盖情况。接口返回空、停牌/未上市、进程中断
            # 等情况都不能仅凭“请求成功”宣称数据完整，因此单独保留待核验清单。
            if self._is_running:
                unresolved_items = []
                unresolved_total = 0
                post_scan_errors = []
                for (stock_code, period), (start_date, end_date) in planned_stock_periods.items():
                    try:
                        remaining = self._build_incremental_plan(
                            stock_code, period, start_date, end_date,
                            include_front_adjusted=self.include_front_adjusted,
                            include_back_adjusted=self.include_back_adjusted,
                        )
                        count = len(remaining.unresolved_dates)
                        if count:
                            unresolved_total += count
                            unresolved_items.append({
                                "stock": stock_code,
                                "period": period,
                                "count": count,
                                "preview": list(remaining.unresolved_dates[:10]),
                            })
                    except Exception as exc:
                        post_scan_errors.append(f"{stock_code} {period}: {exc}")
                    finally:
                        try:
                            self.manager.close_stock_connection(
                                stock_code, skip_checkpoint=True,
                            )
                        except Exception:
                            pass
                results["unresolved"] = unresolved_items
                results["unresolved_count"] = unresolved_total
                results["post_scan_errors"] = post_scan_errors
                if unresolved_total:
                    self.status.emit(
                        f"警告：导入后仍有 {unresolved_total} 个交易日待核验；"
                        "可能是停牌、未上市、数据源未返回或下载未完成"
                    )
                if post_scan_errors:
                    self.status.emit(
                        f"警告：{len(post_scan_errors)} 个股票周期未能完成导入后复核"
                    )

            compact_metadata = _coalesce_metadata_records(imported_records)
            if compact_metadata:
                try:
                    updated = self.manager.batch_update_metadata(compact_metadata)
                    self.status.emit(f"元数据刷新完成: {updated}/{len(compact_metadata)}")
                    imported_records.clear()
                except Exception as exc:
                    self.status.emit(f"警告：行情已写入，但元数据刷新失败: {exc}")
            try:
                self.manager.close_metadata_connection()
            except Exception:
                pass
            if short_lock_enabled and hasattr(self.manager, 'disable_short_lock_write'):
                self.manager.disable_short_lock_write()
            short_lock_enabled = False
            results['cancelled'] = not self._is_running and not limit_reached
            results['limit_reached'] = bool(limit_reached)
            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            try:
                if self._mp_importer is not None:
                    self._mp_importer.force_stop()
            except Exception:
                pass
            self._mp_importer = None
            # 异常路径也尽力让已经提交的行情在数据树中可见。
            if imported_records:
                try:
                    self.manager.batch_update_metadata(
                        _coalesce_metadata_records(imported_records)
                    )
                except Exception as exc:
                    self.status.emit(f"警告：异常收尾时元数据刷新失败: {exc}")
            try:
                self.manager.close_metadata_connection()
            except Exception:
                pass
            if short_lock_enabled and hasattr(self.manager, 'disable_short_lock_write'):
                try:
                    self.manager.disable_short_lock_write()
                except Exception:
                    pass
            if self.request_tracker:
                try:
                    self.request_tracker.flush()
                except Exception:
                    pass


class BaoStockIndicatorImportThread(QThread):
    progress = pyqtSignal(int, int, int)
    status = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    request_count = pyqtSignal(int)

    # 一次 BaoStock 请求中字段多少不增加请求次数。始终补齐整组指标并以
    # tradestatus 作为“该交易日已成功请求过指标”的稳定证据，避免合法的
    # 空估值字段（如亏损公司的 peTTM）被反复误判为缺失。
    ALL_INDICATORS = (
        'turn', 'tradestatus', 'pctChg', 'peTTM', 'pbMRQ',
        'psTTM', 'pcfNcfTTM', 'isST',
    )

    def __init__(
        self,
        manager: DuckDBManager,
        stocks: List[str],
        start_date: str,
        end_date: str,
        indicators: List[str],
        request_tracker=None,
        num_workers: int = 1,
        timeout_per_task: float = 60.0,
        force_refresh: bool = False,
    ):
        super().__init__()
        self.manager = manager
        self.stocks = stocks
        self.start_date = start_date
        self.end_date = end_date
        self.indicators = list(indicators or [])
        self.fetch_indicators = list(self.ALL_INDICATORS)
        self._is_running = True
        self.request_tracker = request_tracker
        self.num_workers = max(1, int(num_workers))
        self.timeout_per_task = max(5.0, float(timeout_per_task))
        self.force_refresh = bool(force_refresh)
        self._mp_importer = None

    def stop(self):
        self._is_running = False
        importer = self._mp_importer
        if importer is not None:
            try:
                importer.force_stop()
            except Exception:
                pass

    def _to_baostock_code(self, stock_code: str) -> str:
        code = stock_code.strip().upper()
        if '.' in code:
            num, market = code.split('.')
            if market in ('SH', 'SZ', 'BJ'):
                return f"{market.lower()}.{num}"
        return code.lower()

    @staticmethod
    def _to_baostock_date(date8: str) -> str:
        text = str(date8).replace('-', '')[:8]
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"

    def _indicator_download_ranges(self, stock_code: str):
        """只返回本地已有日线中尚无指标导入证据的日期区间。"""

        from datetime import time as datetime_time
        from .incremental import group_missing_trade_dates, normalize_date8

        coverage = self.manager.get_existing_date_completeness(
            stock_code,
            '1d',
            required_columns=['tradestatus'],
            raise_on_error=True,
            start_date=self.start_date,
            end_date=self.end_date,
        )
        now = datetime.now()
        today8 = now.strftime('%Y%m%d')
        local_dates = []
        for raw_date in sorted(coverage):
            date8 = normalize_date8(raw_date)
            if date8 > today8:
                continue
            if date8 == today8 and now.time() < datetime_time(15, 15):
                continue
            local_dates.append(date8)
        if not local_dates:
            return [], 0
        if self.force_refresh:
            groups = ((local_dates[0], local_dates[-1]),)
        else:
            missing = [
                date8 for date8 in local_dates
                if int((coverage.get(date8) or {}).get('valid_rows', 0) or 0) <= 0
            ]
            groups = group_missing_trade_dates(missing, local_dates)
        return [
            (self._to_baostock_date(start), self._to_baostock_date(end))
            for start, end in groups
        ], len(local_dates)

    def _fetch_indicators(self, bs, stock: str) -> pd.DataFrame:
        if self.request_tracker:
            if not self.request_tracker.consume(1):
                raise RuntimeError(self.request_tracker.limit_message)
            self.request_count.emit(self.request_tracker.get_count())
        fields = ['date', 'code'] + self.indicators
        rs = bs.query_history_k_data_plus(
            stock,
            ','.join(fields),
            start_date=self.start_date,
            end_date=self.end_date,
            frequency="d",
            adjustflag="3"
        )

        if rs.error_code != '0':
            raise RuntimeError(rs.error_msg or rs.error_code)

        data_list = []
        while (rs.error_code == '0') & rs.next():
            data_list.append(rs.get_row_data())

        if not data_list:
            return pd.DataFrame()

        df = pd.DataFrame(data_list, columns=rs.fields)
        df['time'] = pd.to_datetime(df['date'], format="%Y-%m-%d", errors='coerce')
        df = df.dropna(subset=['time'])
        for col in self.indicators:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        if 'isST' in df.columns:
            df['isST'] = pd.to_numeric(df['isST'], errors='coerce')
        keep_cols = ['time'] + [c for c in self.indicators if c in df.columns]
        return df[keep_cols]

    def _merge_with_existing(self, stock_code: str, indicator_df: pd.DataFrame) -> pd.DataFrame:
        """兼容旧调用：仅以已有日线为左表，不引入指标独有日期。"""
        base_df = self.manager.get_kline_data(
            stock_code,
            '1d',
            self.start_date,
            self.end_date
        )
        if base_df is None or base_df.empty:
            return pd.DataFrame()
        base_df = base_df.copy()
        if 'time' not in base_df.columns:
            return pd.DataFrame()
        base_df['time'] = pd.to_datetime(base_df['time'], errors='coerce')
        indicator_df = indicator_df.copy()
        indicator_df['time'] = pd.to_datetime(indicator_df['time'], errors='coerce')
        base_df = base_df.dropna(subset=['time']).set_index('time')
        indicator_df = indicator_df.dropna(subset=['time']).set_index('time')
        indicator_cols = [c for c in self.indicators if c in indicator_df.columns]
        for col in indicator_cols:
            if col not in base_df.columns:
                base_df[col] = pd.NA
        indicator_aligned = indicator_df[indicator_cols].reindex(base_df.index)
        for col in indicator_cols:
            base_df[col] = indicator_aligned[col].combine_first(base_df[col])
        merged = base_df.reset_index()
        merged = merged.dropna(subset=['time'])
        merged = merged.sort_values('time')
        merged = merged.reset_index(drop=True)
        return merged

    def run(self):
        results = {
            'success': 0,
            'failed': 0,
            'empty': 0,
            'up_to_date': 0,
            'no_local_daily': 0,
            'total_records': 0,
            'unprocessed': 0,
            'cancelled': False,
            'limit_reached': False,
            'unresolved_count': 0,
        }
        limit_reached = False
        importer = None
        try:
            try:
                from .baostock_import_worker import MultiProcessBaoStockImporter
            except ImportError:
                try:
                    # 兼容直接运行 viewer.py 的源码调试方式。
                    from baostock_import_worker import MultiProcessBaoStockImporter
                except ImportError:
                    self.error.emit(
                        "未找到BaoStock多进程下载模块，请更新到最新版本。"
                    )
                    return

            tasks = []
            planned_stocks = set()
            for stock_code in self.stocks:
                try:
                    ranges, local_count = self._indicator_download_ranges(stock_code)
                    if local_count <= 0:
                        results['no_local_daily'] += 1
                        self.status.emit(f"○ {stock_code} 没有可匹配的本地日线，跳过指标请求")
                    elif not ranges:
                        results['up_to_date'] += 1
                    else:
                        planned_stocks.add(stock_code)
                        for range_start, range_end in ranges:
                            tasks.append((stock_code, range_start, range_end))
                except Exception as exc:
                    results['failed'] += 1
                    self.status.emit(f"✗ {stock_code} 指标增量扫描失败: {exc}")
                finally:
                    try:
                        self.manager.close_stock_connection(
                            stock_code, skip_checkpoint=True,
                        )
                    except Exception:
                        pass

            total_tasks = len(tasks)
            completed_tasks = 0
            if total_tasks <= 0:
                if results['failed']:
                    self.status.emit(
                        f"指标增量扫描结束，但有 {results['failed']} 只股票扫描失败"
                    )
                elif results['no_local_daily'] and not results['up_to_date']:
                    self.status.emit("所选股票没有可匹配的本地日线，未发起指标请求")
                else:
                    self.status.emit("所选股票的 BaoStock 指标均已补齐，无需发起请求")
                self.finished.emit(results)
                return

            importer = MultiProcessBaoStockImporter(
                num_workers=min(self.num_workers, total_tasks),
                timeout_per_task=self.timeout_per_task,
                max_task_retries=2,
                retry_backoff=(2.0, 5.0),
            )
            self._mp_importer = importer
            importer.start()
            next_submit = 0
            in_flight = 0
            id_to_task = {}

            def consume_request() -> bool:
                if not self.request_tracker:
                    return True
                if not self.request_tracker.consume(1):
                    return False
                self.request_count.emit(self.request_tracker.get_count())
                return True

            def submit_next() -> bool:
                nonlocal next_submit, in_flight, limit_reached
                if next_submit >= total_tasks or not self._is_running:
                    return False
                if not consume_request():
                    limit_reached = True
                    self._is_running = False
                    self.status.emit(self.request_tracker.limit_message)
                    return False
                stock_code, range_start, range_end = tasks[next_submit]
                task_id = importer.add_indicator_task(
                    stock_code,
                    self._to_baostock_code(stock_code),
                    range_start,
                    range_end,
                    self.fetch_indicators,
                )
                id_to_task[task_id] = (stock_code, range_start, range_end)
                next_submit += 1
                in_flight += 1
                return True

            for _ in range(min(self.num_workers, total_tasks)):
                if not submit_next():
                    break

            while in_flight > 0 and self._is_running:
                result = importer.get_result(timeout=0.25)
                for event in importer.get_all_progress():
                    event_type = event.get('type')
                    if event_type == 'retry':
                        if not consume_request():
                            limit_reached = True
                            self._is_running = False
                            self.status.emit(self.request_tracker.limit_message)
                            break
                        self.status.emit(
                            f"↻ {event.get('stock_code')} 网络或登录异常，"
                            f"{event.get('delay', 0):g}秒后进行第{event.get('attempt')}次重试"
                        )
                    elif event_type == 'fatal':
                        self.status.emit(str(event.get('msg') or 'BaoStock工作进程异常'))
                if not self._is_running:
                    break
                if not result:
                    continue

                task_id = int(result.get('task_id', 0) or 0)
                task = id_to_task.pop(task_id, None)
                stock_code = (task or ('', '', ''))[0] or result.get('stock_code', '')
                indicator_df = None
                try:
                    if result.get('success'):
                        df_dict = result.get('df_dict')
                        indicator_df = dict_to_dataframe(df_dict) if df_dict else pd.DataFrame()
                        if indicator_df is None or indicator_df.empty:
                            results['empty'] += 1
                            self.status.emit(f"○ {stock_code} 无指标数据")
                        else:
                            records = self.manager.update_daily_indicators(
                                indicator_df,
                                stock_code,
                                columns=self.fetch_indicators,
                            )
                            if records <= 0:
                                results['empty'] += 1
                                self.status.emit(f"○ {stock_code} 无匹配的本地日线，已跳过指标")
                            else:
                                results['success'] += 1
                                results['total_records'] += records
                                self.status.emit(f"✓ {stock_code} 指标更新 {records} 条日线")
                    else:
                        results['failed'] += 1
                        self.status.emit(
                            f"✗ {stock_code} 失败（已重试）: "
                            f"{result.get('error', '未知错误')}"
                        )
                except Exception as exc:
                    results['failed'] += 1
                    self.status.emit(f"✗ {stock_code} 写入失败: {exc}")
                finally:
                    if indicator_df is not None:
                        del indicator_df
                    try:
                        self.manager.close_stock_connection(
                            stock_code, skip_checkpoint=True
                        )
                    except Exception:
                        pass

                completed_tasks += 1
                in_flight -= 1
                percent = int((completed_tasks / total_tasks) * 100)
                self.progress.emit(percent, completed_tasks, total_tasks)
                submit_next()

            if importer is not None:
                importer.stop(timeout=2.0)
                importer = None
                self._mp_importer = None
            if self._is_running and not self.force_refresh:
                unresolved_count = 0
                for stock_code in planned_stocks:
                    try:
                        ranges, _ = self._indicator_download_ranges(stock_code)
                        if ranges:
                            # 复核清单按范围展示即可；精确日期仍可由下一次扫描恢复。
                            unresolved_count += len(ranges)
                    except Exception as exc:
                        unresolved_count += 1
                        self.status.emit(f"警告：{stock_code} 指标导入后复核失败: {exc}")
                    finally:
                        try:
                            self.manager.close_stock_connection(
                                stock_code, skip_checkpoint=True,
                            )
                        except Exception:
                            pass
                results['unresolved_count'] = unresolved_count
                if unresolved_count:
                    self.status.emit(
                        f"警告：仍有 {unresolved_count} 个指标缺口区间待核验"
                    )
            processed = results['success'] + results['failed'] + results['empty']
            results['unprocessed'] = max(0, total_tasks - processed)
            results['cancelled'] = not self._is_running and not limit_reached
            results['limit_reached'] = limit_reached
            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            if importer is not None:
                try:
                    importer.force_stop()
                except Exception:
                    pass
            self._mp_importer = None
            if self.request_tracker:
                try:
                    self.request_tracker.flush()
                except Exception:
                    pass


class BaoStockImportDialog(QDialog):
    def __init__(self, manager: DuckDBManager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.import_thread = None
        self.indicator_thread = None
        self.font_scale = get_ui_font_scale()
        self._base_style_raw = None
        self._import_start_time = None
        self._indicator_start_time = None
        self._close_after_stop = False
        self._close_poll_scheduled = False

        self.setWindowTitle("从BaoStock导入数据")
        self.setMinimumSize(800, 700)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowFlags(self.windowFlags() | Qt.Window)
        self._set_dark_titlebar()

        self._base_style_raw = """
            QDialog, QWidget {
                background-color: #333333;
                color: #e8e8e8;
                font-family: "Microsoft YaHei UI";
            }
            QLabel {
                color: #e8e8e8;
            }
            QLineEdit, QTextEdit {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px;
            }
            QPushButton {
                background-color: #0078d4;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-family: "Microsoft YaHei UI";
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:pressed {
                background-color: #005a9e;
            }
            QPushButton:disabled {
                background-color: #555555;
                color: #888888;
            }
            QComboBox {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px;
            }
            QComboBox QAbstractItemView {
                background-color: #3c3c3c;
                color: #e8e8e8;
                selection-background-color: #0078d4;
            }
            QCheckBox {
                color: #e8e8e8;
                min-height: 24px;
                spacing: 6px;
                padding: 2px 0;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 1px solid #555555;
                border-radius: 3px;
                background-color: #3c3c3c;
            }
            QCheckBox::indicator:checked {
                background-color: #0078d4;
                border-color: #0078d4;
            }
            QRadioButton {
                color: #e8e8e8;
            }
            QRadioButton::indicator {
                width: 18px;
                height: 18px;
                border: 1px solid #555555;
                border-radius: 9px;
                background-color: #3c3c3c;
            }
            QRadioButton::indicator:checked {
                background-color: #0078d4;
                border-color: #0078d4;
            }
            QGroupBox {
                border: 1px solid #555555;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
                color: #e8e8e8;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QProgressBar {
                background-color: #3c3c3c;
                border: 1px solid #555555;
                border-radius: 3px;
                text-align: center;
                color: #e8e8e8;
            }
            QProgressBar::chunk {
                background-color: #0078d4;
            }
            QSpinBox, QDateEdit {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px;
            }
            QFrame {
                background-color: #333333;
                color: #e8e8e8;
            }
        """
        _ui_font = get_preferred_ui_font_family() or "Microsoft YaHei UI"
        if _ui_font != "Microsoft YaHei UI":
            self._base_style_raw = self._base_style_raw.replace('"Microsoft YaHei UI"', f'"{_ui_font}"')
        self.setStyleSheet(self._scale_stylesheet(self._base_style_raw, self.font_scale))
        data_root = getattr(self.manager, "data_root", None) or os.getcwd()
        self._request_tracker = get_baostock_request_tracker(data_root)
        self.init_ui()
        self.apply_ui_scale(self.font_scale)
        self._refresh_request_usage_label()

    def _set_dark_titlebar(self):
        try:
            import platform
            if platform.system() == "Windows":
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
        except Exception:
            pass

    def _scale_stylesheet(self, style: str, scale: float) -> str:
        if not style:
            return style
        import re

        def repl(match):
            value = float(match.group(1))
            unit = match.group(2)
            scaled = max(6, int(round(value * float(scale))))
            return f"font-size: {scaled}{unit}"

        return re.sub(
            r"font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(px|pt)",
            repl,
            style,
            flags=re.IGNORECASE
        )

    def _set_scaled_stylesheet(self, widget: QWidget, style: str):
        widget.setProperty("ui_base_stylesheet", style)
        widget.setStyleSheet(self._scale_stylesheet(style, self.font_scale))

    def _set_scaled_font(self, widget: QWidget, base_pt: int):
        widget.setProperty("ui_base_font_pt", base_pt)
        font = widget.font()
        font.setPointSize(max(6, int(round(base_pt * self.font_scale))))
        widget.setFont(font)

    def apply_ui_scale(self, scale=None):
        if scale is None:
            scale = get_ui_font_scale()
        self.font_scale = scale
        if self._base_style_raw:
            self.setStyleSheet(self._scale_stylesheet(self._base_style_raw, self.font_scale))
        try:
            for child in self.findChildren(QWidget):
                base_ss = child.property("ui_base_stylesheet")
                if base_ss:
                    child.setStyleSheet(self._scale_stylesheet(base_ss, self.font_scale))
                base_pt = child.property("ui_base_font_pt")
                if base_pt:
                    font = child.font()
                    font.setPointSize(max(6, int(round(float(base_pt) * self.font_scale))))
                    child.setFont(font)
            # 复选框/单选框高度需随字号缩放，否则放大字体后指示器与文字会被裁切
            for box in self.findChildren(QCheckBox) + self.findChildren(QRadioButton):
                row_h = max(QFontMetrics(box.font()).height() + 10, 24)
                box.setMinimumHeight(row_h)
        except Exception:
            pass

    def showEvent(self, event):
        """首次显示后把高度撑到内容所需，避免底部按钮落在可视区外。"""
        super().showEvent(event)
        if getattr(self, "_content_height_fitted", False):
            return
        self._content_height_fitted = True
        QTimer.singleShot(0, lambda: fit_dialog_height_to_content(self, getattr(self, "tabs", None)))

    def init_ui(self):
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.kline_tab = QWidget()
        self.indicator_tab = QWidget()
        self.tabs.addTab(self.kline_tab, "行情数据")
        self.tabs.addTab(self.indicator_tab, "指标下载")
        layout.addWidget(self.tabs)
        self._build_kline_tab()
        self._build_indicator_tab()

    def _build_kline_tab(self):
        # 与指标页一致，用滚动区域包裹，避免高分屏放大字号后分组被压扁
        outer_layout = QVBoxLayout(self.kline_tab)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        layout = QVBoxLayout(content)

        stock_group = QGroupBox("股票池选择")
        stock_layout = QVBoxLayout(stock_group)

        method_layout = QHBoxLayout()
        self.method_group = QButtonGroup(self)
        self.preset_radio = QRadioButton("预设板块")
        self.preset_radio.setChecked(True)
        self.method_group.addButton(self.preset_radio)
        method_layout.addWidget(self.preset_radio)

        self.file_radio = QRadioButton("从文件导入")
        self.method_group.addButton(self.file_radio)
        method_layout.addWidget(self.file_radio)

        self.manual_radio = QRadioButton("手动输入")
        self.method_group.addButton(self.manual_radio)
        method_layout.addWidget(self.manual_radio)

        method_layout.addStretch()
        stock_layout.addLayout(method_layout)

        self.preset_frame = QFrame()
        preset_layout = QGridLayout(self.preset_frame)
        preset_layout.setContentsMargins(0, 0, 0, 0)
        self.preset_checks = {}
        presets = [
            ('沪深A股', 'all_a'),
            ('上证A股', 'sh_a'),
            ('深证A股', 'sz_a'),
            ('沪深300', 'hs300'),
            ('上证50', 'sz50'),
            ('中证500', 'zz500'),
            ('创业板', 'cyb'),
            ('科创板', 'kcb'),
            # BaoStock 不提供 ETF / LOF 等场内基金和可转债行情，这几个预设放在 Tushare 导入里
            ('常用指数', 'common_index'),
        ]
        for i, (name, key) in enumerate(presets):
            cb = QCheckBox(name)
            self.preset_checks[key] = cb
            preset_layout.addWidget(cb, i // 4, i % 4)

        stock_layout.addWidget(self.preset_frame)

        self.file_frame = QFrame()
        file_layout = QHBoxLayout(self.file_frame)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_path_edit = QLineEdit()
        self.file_path_edit.setPlaceholderText("选择股票列表CSV文件...")
        file_layout.addWidget(self.file_path_edit)
        self.browse_file_btn = QPushButton("浏览...")
        self.browse_file_btn.clicked.connect(self.browse_stock_file)
        file_layout.addWidget(self.browse_file_btn)
        self.file_frame.setVisible(False)
        stock_layout.addWidget(self.file_frame)

        self.manual_frame = QFrame()
        manual_layout = QVBoxLayout(self.manual_frame)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.addWidget(QLabel("输入股票代码（每行一个，支持纯数字或带市场后缀）:"))
        self.manual_edit = QTextEdit()
        self.manual_edit.setMaximumHeight(100)
        self.manual_edit.setPlaceholderText("000001.SZ 或 000001\n600000.SH 或 600000\n300750")
        manual_layout.addWidget(self.manual_edit)
        self.manual_frame.setVisible(False)
        stock_layout.addWidget(self.manual_frame)

        self.preset_radio.toggled.connect(self.on_method_changed)
        self.file_radio.toggled.connect(self.on_method_changed)
        self.manual_radio.toggled.connect(self.on_method_changed)

        layout.addWidget(stock_group)

        period_group = QGroupBox("数据周期设置")
        period_layout = QGridLayout(period_group)
        today = QDate.currentDate()

        self.period_1d_check = QCheckBox("日线 (1d)")
        self.period_1d_check.setChecked(True)
        period_layout.addWidget(self.period_1d_check, 0, 0)
        period_layout.addWidget(QLabel("开始:"), 0, 1)
        self.period_1d_start = QDateEdit()
        self.period_1d_start.setCalendarPopup(True)
        self.period_1d_start.setDate(today.addYears(-10))
        period_layout.addWidget(self.period_1d_start, 0, 2)
        period_layout.addWidget(QLabel("结束:"), 0, 3)
        self.period_1d_end = QDateEdit()
        self.period_1d_end.setCalendarPopup(True)
        self.period_1d_end.setDate(today)
        period_layout.addWidget(self.period_1d_end, 0, 4)

        self.period_5m_check = QCheckBox("5分钟 (5m)")
        self.period_5m_check.setChecked(True)
        period_layout.addWidget(self.period_5m_check, 1, 0)
        period_layout.addWidget(QLabel("开始:"), 1, 1)
        self.period_5m_start = QDateEdit()
        self.period_5m_start.setCalendarPopup(True)
        self.period_5m_start.setDate(today.addYears(-1))
        period_layout.addWidget(self.period_5m_start, 1, 2)
        period_layout.addWidget(QLabel("结束:"), 1, 3)
        self.period_5m_end = QDateEdit()
        self.period_5m_end.setCalendarPopup(True)
        self.period_5m_end.setDate(today)
        period_layout.addWidget(self.period_5m_end, 1, 4)

        self.warmup_hint_label = QLabel(
            "提示：开始日期要早于回测开始日期，给均线、MACD 等指标留出预热期（例如用 60 日均线，至少往前多下 3 个月）。BaoStock 只有股票和指数的日线、5 分钟线，没有 1 分钟、Tick，也不提供 ETF / LOF 等场内基金和可转债（请用 Tushare）。"
        )
        self.warmup_hint_label.setWordWrap(True)
        self.warmup_hint_label.setStyleSheet("color: #8a5a00;")
        period_layout.addWidget(self.warmup_hint_label, 2, 0, 1, 5)

        layout.addWidget(period_group)

        adjustment_group = QGroupBox("价格字段（原始价必存，复权价可选）")
        adjustment_layout = QHBoxLayout(adjustment_group)
        self.adjustment_none_check = QCheckBox("原始价（必存）")
        self.adjustment_front_check = QCheckBox("前复权")
        self.adjustment_back_check = QCheckBox("后复权")
        self.adjustment_none_check.setChecked(True)
        self.adjustment_none_check.setEnabled(False)
        self.adjustment_none_check.setToolTip(
            "DuckDB 以不复权行情作为基础字段，导入时始终保存。"
        )
        # 保留原窗口默认同时下载前/后复权的行为，用户现在可以按需取消。
        self.adjustment_front_check.setChecked(True)
        self.adjustment_back_check.setChecked(True)
        self.adjustment_front_check.setToolTip(
            "保存 open_front/high_front/low_front/close_front；"
            "增量补充时可能刷新本地完整历史，以保持同一复权基准。"
        )
        self.adjustment_back_check.setToolTip(
            "保存 open_back/high_back/low_back/close_back。"
        )
        adjustment_layout.addWidget(self.adjustment_none_check)
        adjustment_layout.addWidget(self.adjustment_front_check)
        adjustment_layout.addWidget(self.adjustment_back_check)
        adjustment_layout.addStretch()
        layout.addWidget(adjustment_group)

        mode_group = QGroupBox("导入方式")
        mode_layout = QVBoxLayout(mode_group)
        mode_hint = QLabel(
            "默认增量：先核验本地每个交易日，只请求缺失或条数不完整的区间；"
            "尚未收盘的当天不会被误判为缺失。"
        )
        mode_hint.setWordWrap(True)
        mode_layout.addWidget(mode_hint)
        self.force_overwrite_check = QCheckBox(
            "强制覆写已有数据（跳过增量检测，重新下载所选完整区间）"
        )
        self.force_overwrite_check.setChecked(False)
        self.force_overwrite_check.setToolTip(
            "仅在确认本地历史数据需要整体重建时使用；默认无需勾选。"
        )
        mode_layout.addWidget(self.force_overwrite_check)
        layout.addWidget(mode_group)

        progress_group = QGroupBox("进度信息")
        progress_layout = QVBoxLayout(progress_group)
        self.progress_bar = QProgressBar()
        progress_layout.addWidget(self.progress_bar)
        usage_layout = QHBoxLayout()
        self.request_usage_label = QLabel("软件每日BaoStock请求上限3万次；当前：0/30000")
        usage_layout.addWidget(self.request_usage_label)
        usage_layout.addStretch()
        progress_layout.addLayout(usage_layout)

        status_layout = QHBoxLayout()
        self.import_status_label = QLabel("就绪")
        status_layout.addWidget(self.import_status_label)
        self.import_eta_label = QLabel("预计剩余：--")
        status_layout.addWidget(self.import_eta_label)
        status_layout.addStretch()
        progress_layout.addLayout(status_layout)

        layout.addWidget(progress_group)

        log_group = QGroupBox("执行日志")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        _configure_download_log_widget(self.log_text)
        log_layout.addWidget(self.log_text)
        layout.addWidget(log_group)

        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始导入")
        self.start_btn.clicked.connect(self.start_import)
        btn_layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_import)
        btn_layout.addWidget(self.stop_btn)

        btn_layout.addWidget(QLabel("下载进程数:"))
        self.baostock_workers_spin = QSpinBox()
        self.baostock_workers_spin.setRange(1, 8)
        self.baostock_workers_spin.setValue(2)
        self.baostock_workers_spin.setToolTip("BaoStock 并行下载进程数。建议 2~4；过多可能导致网络/请求上限消耗更快。")
        self.baostock_workers_spin.setFixedWidth(60)
        btn_layout.addWidget(self.baostock_workers_spin)

        btn_layout.addStretch()
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addWidget(self.close_btn)

        # 按钮固定在滚动区域下面：屏幕较矮（1080p 缩放 125%、笔记本）时内容会超出，
        # 放在滚动区域里的「开始导入」要往下滚才看得到
        btn_layout.setContentsMargins(layout.contentsMargins())
        outer_layout.addLayout(btn_layout)

    def _build_indicator_tab(self):
        # 指标页分组较多，整体高度可能超过窗口，用滚动区域包裹避免分组被压扁
        outer_layout = QVBoxLayout(self.indicator_tab)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        layout = QVBoxLayout(content)

        stock_group = QGroupBox("股票池选择")
        stock_layout = QVBoxLayout(stock_group)

        method_layout = QHBoxLayout()
        self.indicator_method_group = QButtonGroup(self)
        self.indicator_preset_radio = QRadioButton("预设板块")
        self.indicator_preset_radio.setChecked(True)
        self.indicator_method_group.addButton(self.indicator_preset_radio)
        method_layout.addWidget(self.indicator_preset_radio)

        self.indicator_file_radio = QRadioButton("从文件导入")
        self.indicator_method_group.addButton(self.indicator_file_radio)
        method_layout.addWidget(self.indicator_file_radio)

        self.indicator_manual_radio = QRadioButton("手动输入")
        self.indicator_method_group.addButton(self.indicator_manual_radio)
        method_layout.addWidget(self.indicator_manual_radio)

        method_layout.addStretch()
        stock_layout.addLayout(method_layout)

        self.indicator_preset_frame = QFrame()
        preset_layout = QGridLayout(self.indicator_preset_frame)
        preset_layout.setContentsMargins(0, 0, 0, 0)
        self.indicator_preset_checks = {}
        presets = [
            ('沪深A股', 'all_a'),
            ('上证A股', 'sh_a'),
            ('深证A股', 'sz_a'),
            ('沪深300', 'hs300'),
            ('上证50', 'sz50'),
            ('中证500', 'zz500'),
            ('创业板', 'cyb'),
            ('科创板', 'kcb'),
            # BaoStock 不提供 ETF / LOF 等场内基金和可转债行情，这几个预设放在 Tushare 导入里
            ('常用指数', 'common_index'),
        ]
        for i, (name, key) in enumerate(presets):
            cb = QCheckBox(name)
            self.indicator_preset_checks[key] = cb
            preset_layout.addWidget(cb, i // 4, i % 4)

        stock_layout.addWidget(self.indicator_preset_frame)

        self.indicator_file_frame = QFrame()
        file_layout = QHBoxLayout(self.indicator_file_frame)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.indicator_file_path_edit = QLineEdit()
        self.indicator_file_path_edit.setPlaceholderText("选择股票列表CSV文件...")
        file_layout.addWidget(self.indicator_file_path_edit)
        self.indicator_browse_file_btn = QPushButton("浏览...")
        self.indicator_browse_file_btn.clicked.connect(self.browse_indicator_stock_file)
        file_layout.addWidget(self.indicator_browse_file_btn)
        self.indicator_file_frame.setVisible(False)
        stock_layout.addWidget(self.indicator_file_frame)

        self.indicator_manual_frame = QFrame()
        manual_layout = QVBoxLayout(self.indicator_manual_frame)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.addWidget(QLabel("输入股票代码（每行一个，支持纯数字或带市场后缀）:"))
        self.indicator_manual_edit = QTextEdit()
        self.indicator_manual_edit.setMaximumHeight(100)
        self.indicator_manual_edit.setPlaceholderText("000001.SZ 或 000001\n600000.SH 或 600000\n300750")
        manual_layout.addWidget(self.indicator_manual_edit)
        self.indicator_manual_frame.setVisible(False)
        stock_layout.addWidget(self.indicator_manual_frame)

        self.indicator_preset_radio.toggled.connect(self.on_indicator_method_changed)
        self.indicator_file_radio.toggled.connect(self.on_indicator_method_changed)
        self.indicator_manual_radio.toggled.connect(self.on_indicator_method_changed)

        layout.addWidget(stock_group)

        indicator_group = QGroupBox("指标字段（每次请求统一补齐全部字段）")
        indicator_layout = QGridLayout(indicator_group)
        self.indicator_checks = {}
        indicators = [
            ('turn', '换手率'),
            ('tradestatus', '交易状态'),
            ('pctChg', '涨跌幅'),
            ('peTTM', '滚动市盈率'),
            ('psTTM', '滚动市销率'),
            ('pcfNcfTTM', '滚动市现率'),
            ('pbMRQ', '市净率'),
            ('isST', '是否ST'),
        ]
        for i, (field, label) in enumerate(indicators):
            cb = QCheckBox(f"{label} ({field})")
            cb.setChecked(True)
            cb.setEnabled(False)
            cb.setToolTip("BaoStock 同一次请求可返回整组指标，统一补齐能提供可靠的增量完成证据。")
            self.indicator_checks[field] = cb
            indicator_layout.addWidget(cb, i // 2, i % 2)
        layout.addWidget(indicator_group)

        date_group = QGroupBox("日期范围")
        date_layout = QHBoxLayout(date_group)
        today = QDate.currentDate()
        date_layout.addWidget(QLabel("开始:"))
        self.indicator_start = QDateEdit()
        self.indicator_start.setCalendarPopup(True)
        self.indicator_start.setDate(today.addYears(-10))
        date_layout.addWidget(self.indicator_start)
        date_layout.addWidget(QLabel("结束:"))
        self.indicator_end = QDateEdit()
        self.indicator_end.setCalendarPopup(True)
        self.indicator_end.setDate(today)
        date_layout.addWidget(self.indicator_end)
        layout.addWidget(date_group)

        self.indicator_force_refresh_check = QCheckBox(
            "强制重新请求所选日期范围（默认仅补本地日线中尚未导入的指标）"
        )
        self.indicator_force_refresh_check.setChecked(False)
        self.indicator_force_refresh_check.setToolTip(
            "默认无需勾选；仅在确认历史指标需要整体刷新时使用。"
        )
        layout.addWidget(self.indicator_force_refresh_check)

        progress_group = QGroupBox("进度信息")
        progress_layout = QVBoxLayout(progress_group)
        self.indicator_progress_bar = QProgressBar()
        progress_layout.addWidget(self.indicator_progress_bar)
        usage_layout2 = QHBoxLayout()
        self.indicator_request_usage_label = QLabel("软件每日BaoStock请求上限3万次；当前：0/30000（指标请求也计入）")
        usage_layout2.addWidget(self.indicator_request_usage_label)
        usage_layout2.addStretch()
        progress_layout.addLayout(usage_layout2)

        status_layout = QHBoxLayout()
        self.indicator_status_label = QLabel("就绪")
        status_layout.addWidget(self.indicator_status_label)
        self.indicator_eta_label = QLabel("预计剩余：--")
        status_layout.addWidget(self.indicator_eta_label)
        status_layout.addStretch()
        progress_layout.addLayout(status_layout)

        layout.addWidget(progress_group)

        log_group = QGroupBox("执行日志")
        log_layout = QVBoxLayout(log_group)
        self.indicator_log_text = QTextEdit()
        self.indicator_log_text.setReadOnly(True)
        _configure_download_log_widget(self.indicator_log_text)
        log_layout.addWidget(self.indicator_log_text)
        layout.addWidget(log_group)

        btn_layout = QHBoxLayout()
        self.indicator_start_btn = QPushButton("开始下载")
        self.indicator_start_btn.clicked.connect(self.start_indicator_import)
        btn_layout.addWidget(self.indicator_start_btn)

        self.indicator_stop_btn = QPushButton("停止")
        self.indicator_stop_btn.setEnabled(False)
        self.indicator_stop_btn.clicked.connect(self.stop_indicator_import)
        btn_layout.addWidget(self.indicator_stop_btn)

        btn_layout.addStretch()
        self.indicator_close_btn = QPushButton("关闭")
        self.indicator_close_btn.clicked.connect(self.close)
        btn_layout.addWidget(self.indicator_close_btn)

        # 与行情页一样固定在滚动区域下面
        btn_layout.setContentsMargins(layout.contentsMargins())
        outer_layout.addLayout(btn_layout)

    def on_method_changed(self):
        self.preset_frame.setVisible(self.preset_radio.isChecked())
        self.file_frame.setVisible(self.file_radio.isChecked())
        self.manual_frame.setVisible(self.manual_radio.isChecked())

    def on_indicator_method_changed(self):
        self.indicator_preset_frame.setVisible(self.indicator_preset_radio.isChecked())
        self.indicator_file_frame.setVisible(self.indicator_file_radio.isChecked())
        self.indicator_manual_frame.setVisible(self.indicator_manual_radio.isChecked())

    def browse_stock_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择股票列表文件",
            "", "CSV文件 (*.csv);;所有文件 (*.*)"
        )
        if file_path:
            self.file_path_edit.setText(file_path)

    def browse_indicator_stock_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择股票列表文件",
            "", "CSV文件 (*.csv);;所有文件 (*.*)"
        )
        if file_path:
            self.indicator_file_path_edit.setText(file_path)

    def _normalize_stock_code_with_market(self, code: str) -> str:
        return _normalize_market_security_code(code)

    def _read_stock_file(self, file_path: str) -> List[str]:
        stocks = []
        try:
            df = pd.read_csv(file_path, dtype=str, header=None, encoding='utf-8-sig')
            if len(df) > 0 and len(df.columns) > 0:
                first_cell = str(df.iloc[0, 0])
                if '.SH' in first_cell or '.SZ' in first_cell or '.BJ' in first_cell:
                    raw_stocks = df.iloc[:, 0].dropna().tolist()
                else:
                    df = pd.read_csv(file_path, dtype=str, encoding='utf-8-sig')
                    raw_stocks = []
                    for col in df.columns:
                        if '代码' in col or 'code' in col.lower():
                            raw_stocks.extend(df[col].dropna().tolist())
                            break
                    else:
                        if len(df.columns) > 0:
                            raw_stocks.extend(df.iloc[:, 0].dropna().tolist())
                for code in raw_stocks:
                    normalized_code = self._normalize_stock_code_with_market(code)
                    stocks.append(normalized_code)
        except Exception as e:
            self.log(f"读取文件失败: {e}")
        return stocks

    def get_stock_list(self) -> List[str]:
        stocks = []
        if self.preset_radio.isChecked():
            legacy_dirs = [
                os.path.join(os.path.dirname(os.path.dirname(__file__)), 'stock_lists'),
                os.path.join(os.path.dirname(__file__), 'stock_lists'),
            ]
            preset_files = {
                'all_a': '沪深A股_股票列表.csv',
                'sh_a': '上证A股_股票列表.csv',
                'sz_a': '深证A股_股票列表.csv',
                'hs300': '沪深300成分股_股票列表.csv',
                'sz50': '上证50成分股_股票列表.csv',
                'zz500': '中证500成分股_股票列表.csv',
                'cyb': '创业板_股票列表.csv',
                'kcb': '科创板_股票列表.csv',
                'hs_etf': '沪深ETF_成分股列表.csv',
                'hs_fund': '沪深基金_列表.csv',
                'hs_convertible_bonds': '沪深转债_列表.csv',
                't0_etf': 'T0型ETF.csv',
                'common_index': '指数_股票列表.csv',
            }
            for key, cb in self.preset_checks.items():
                if cb.isChecked() and key in preset_files:
                    file_path = _resolve_preset_stock_pool_file(
                        preset_files[key], legacy_dirs
                    )
                    if file_path:
                        stocks.extend(self._read_stock_file(file_path))
        elif self.file_radio.isChecked():
            file_path = self.file_path_edit.text().strip()
            if file_path and os.path.exists(file_path):
                stocks = self._read_stock_file(file_path)
        elif self.manual_radio.isChecked():
            text = self.manual_edit.toPlainText().strip()
            for line in text.split('\n'):
                code = line.strip()
                if code:
                    normalized_code = self._normalize_stock_code_with_market(code)
                    stocks.append(normalized_code)
        return list(set(stocks))

    def get_indicator_stock_list(self) -> List[str]:
        stocks = []
        if self.indicator_preset_radio.isChecked():
            legacy_dirs = [
                os.path.join(os.path.dirname(os.path.dirname(__file__)), 'stock_lists'),
                os.path.join(os.path.dirname(__file__), 'stock_lists'),
            ]
            preset_files = {
                'all_a': '沪深A股_股票列表.csv',
                'sh_a': '上证A股_股票列表.csv',
                'sz_a': '深证A股_股票列表.csv',
                'hs300': '沪深300成分股_股票列表.csv',
                'sz50': '上证50成分股_股票列表.csv',
                'zz500': '中证500成分股_股票列表.csv',
                'cyb': '创业板_股票列表.csv',
                'kcb': '科创板_股票列表.csv',
                'hs_etf': '沪深ETF_成分股列表.csv',
                'hs_fund': '沪深基金_列表.csv',
                'hs_convertible_bonds': '沪深转债_列表.csv',
                't0_etf': 'T0型ETF.csv',
                'common_index': '指数_股票列表.csv',
            }
            for key, cb in self.indicator_preset_checks.items():
                if cb.isChecked() and key in preset_files:
                    file_path = _resolve_preset_stock_pool_file(
                        preset_files[key], legacy_dirs
                    )
                    if file_path:
                        stocks.extend(self._read_stock_file(file_path))
        elif self.indicator_file_radio.isChecked():
            file_path = self.indicator_file_path_edit.text().strip()
            if file_path and os.path.exists(file_path):
                stocks = self._read_stock_file(file_path)
        elif self.indicator_manual_radio.isChecked():
            text = self.indicator_manual_edit.toPlainText().strip()
            for line in text.split('\n'):
                code = line.strip()
                if code:
                    normalized_code = self._normalize_stock_code_with_market(code)
                    stocks.append(normalized_code)
        return list(set(stocks))

    def get_periods_config(self) -> Dict[str, dict]:
        periods = {}
        if self.period_1d_check.isChecked():
            periods['1d'] = {
                'start': self.period_1d_start.date().toString("yyyy-MM-dd"),
                'end': self.period_1d_end.date().toString("yyyy-MM-dd")
            }
        if self.period_5m_check.isChecked():
            periods['5m'] = {
                'start': self.period_5m_start.date().toString("yyyy-MM-dd"),
                'end': self.period_5m_end.date().toString("yyyy-MM-dd")
            }
        return periods

    def get_adjustments_config(self) -> Tuple[bool, bool]:
        """返回用户选择的（前复权, 后复权）；原始价始终保存。"""
        return (
            bool(self.adjustment_front_check.isChecked()),
            bool(self.adjustment_back_check.isChecked()),
        )

    def log(self, message: str):
        _append_download_log(self.log_text, f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def start_import(self):
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        try:
            self._import_start_time = datetime.now()
            self.import_eta_label.setText("预计剩余：--")
            if self._request_tracker.is_limit_reached():
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                QMessageBox.warning(self, "提示", self._request_tracker.limit_message)
                return
            try:
                import baostock as bs
            except ImportError:
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                QMessageBox.warning(
                    self,
                    "导入错误",
                    "无法导入baostock模块，请先安装BaoStock。\n\n安装命令：pip install baostock"
                )
                return
            stocks = self.get_stock_list()
            if not stocks:
                QMessageBox.warning(self, "提示", "请选择要导入的股票")
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                return
            # BaoStock 只提供 A 股股票和指数：ETF / LOF 等场内基金和可转债的请求都返回空，
            # 先跳过，免得白白消耗请求次数
            skipped = [code for code in stocks if _baostock_unsupported_code(code)]
            if skipped:
                stocks = [code for code in stocks if not _baostock_unsupported_code(code)]
                preview = "、".join(skipped[:5]) + ("等" if len(skipped) > 5 else "")
                self.log(
                    f"BaoStock 不提供 ETF、LOF 等场内基金和可转债行情，已跳过 {len(skipped)} 个：{preview}。"
                    "这类标的请用「Tushare导入」下载（基金需要 fund_daily 权限）。"
                )
                if not stocks:
                    QMessageBox.information(
                        self, "提示",
                        "所选标的都是场内基金（ETF / LOF 等）或可转债，BaoStock 不提供这类行情。\n"
                        "请改用「Tushare导入」下载。",
                    )
                    self.start_btn.setEnabled(True)
                    self.stop_btn.setEnabled(False)
                    return
            periods_config = self.get_periods_config()
            if not periods_config:
                QMessageBox.warning(self, "提示", "请至少选择一个数据周期")
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                return

            include_front_adjusted, include_back_adjusted = (
                self.get_adjustments_config()
            )
            invalid_periods = [
                period for period, config in periods_config.items()
                if config["start"] > config["end"]
            ]
            if invalid_periods:
                QMessageBox.warning(
                    self,
                    "提示",
                    f"以下周期的开始日期晚于结束日期：{', '.join(invalid_periods)}",
                )
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                return

            self._refresh_request_usage_label()
            num_workers = 2
            try:
                if hasattr(self, "baostock_workers_spin") and self.baostock_workers_spin:
                    num_workers = int(self.baostock_workers_spin.value())
            except Exception:
                num_workers = 2

            self.import_thread = BaoStockImportThread(
                self.manager,
                stocks,
                periods_config,
                self._request_tracker,
                num_workers=num_workers,
                timeout_per_task=120.0,
                force_overwrite=self.force_overwrite_check.isChecked(),
                include_front_adjusted=include_front_adjusted,
                include_back_adjusted=include_back_adjusted,
            )
            self.import_thread.progress.connect(self.on_progress)
            self.import_thread.status.connect(self.on_status)
            self.import_thread.finished.connect(self.on_finished)
            self.import_thread.error.connect(self.on_error)
            self.import_thread.conflict.connect(self.on_conflict)
            self.import_thread.request_count.connect(self.on_request_count_changed)
            self.import_thread.start()
            adjustment_labels = ["原始价"]
            if include_front_adjusted:
                adjustment_labels.append("前复权")
            if include_back_adjusted:
                adjustment_labels.append("后复权")
            self.log(
                f"开始导入 {len(stocks)} 只股票；价格字段："
                + "、".join(adjustment_labels)
            )
        except Exception as e:
            self.on_error(str(e))

    def get_selected_indicators(self) -> List[str]:
        return [key for key, cb in self.indicator_checks.items() if cb.isChecked()]

    def start_indicator_import(self):
        self.indicator_start_btn.setEnabled(False)
        self.indicator_stop_btn.setEnabled(True)
        try:
            self._indicator_start_time = datetime.now()
            self.indicator_eta_label.setText("预计剩余：--")
            if self._request_tracker.is_limit_reached():
                self.indicator_start_btn.setEnabled(True)
                self.indicator_stop_btn.setEnabled(False)
                QMessageBox.warning(self, "提示", self._request_tracker.limit_message)
                return
            try:
                import baostock as bs
            except ImportError:
                self.indicator_start_btn.setEnabled(True)
                self.indicator_stop_btn.setEnabled(False)
                QMessageBox.warning(
                    self,
                    "导入错误",
                    "无法导入baostock模块，请先安装BaoStock。\n\n安装命令：pip install baostock"
                )
                return
            stocks = self.get_indicator_stock_list()
            if not stocks:
                QMessageBox.warning(self, "提示", "请选择要导入的股票")
                self.indicator_start_btn.setEnabled(True)
                self.indicator_stop_btn.setEnabled(False)
                return
            indicators = self.get_selected_indicators()
            if not indicators:
                QMessageBox.warning(self, "提示", "请至少选择一个指标")
                self.indicator_start_btn.setEnabled(True)
                self.indicator_stop_btn.setEnabled(False)
                return
            start_date = self.indicator_start.date().toString("yyyy-MM-dd")
            end_date = self.indicator_end.date().toString("yyyy-MM-dd")
            if self.indicator_start.date() > self.indicator_end.date():
                QMessageBox.warning(self, "提示", "指标开始日期不能晚于结束日期")
                self.indicator_start_btn.setEnabled(True)
                self.indicator_stop_btn.setEnabled(False)
                return
            self._refresh_request_usage_label()
            self.indicator_thread = BaoStockIndicatorImportThread(
                self.manager,
                stocks,
                start_date,
                end_date,
                indicators,
                self._request_tracker,
                force_refresh=self.indicator_force_refresh_check.isChecked(),
            )
            self.indicator_thread.progress.connect(self.on_indicator_progress)
            self.indicator_thread.status.connect(self.on_indicator_status)
            self.indicator_thread.finished.connect(self.on_indicator_finished)
            self.indicator_thread.error.connect(self.on_indicator_error)
            self.indicator_thread.request_count.connect(self.on_request_count_changed)
            self.indicator_thread.start()
            self.indicator_log(f"开始下载 {len(stocks)} 只股票指标")
        except Exception as e:
            self.on_indicator_error(str(e))

    def stop_indicator_import(self):
        if self.indicator_thread and self.indicator_thread.isRunning():
            self.indicator_thread.stop()
        self.indicator_stop_btn.setEnabled(False)
        self.indicator_eta_label.setText("预计剩余：--")
        self.indicator_log("已发送停止请求，等待当前写入和数据库收尾...")

    def _refresh_request_usage_label(self):
        count = self._request_tracker.get_count()
        total = self._request_tracker.display_limit
        self.request_usage_label.setText(f"软件每日BaoStock请求上限3万次；当前：{count}/{total}")
        self.indicator_request_usage_label.setText(f"软件每日BaoStock请求上限3万次；当前：{count}/{total}（指标请求也计入）")

    def on_request_count_changed(self, count: int):
        self._refresh_request_usage_label()

    def stop_import(self):
        if self.import_thread and self.import_thread.isRunning():
            self.import_thread.stop()
        self.stop_btn.setEnabled(False)
        self.import_eta_label.setText("预计剩余：--")
        self.log("已发送停止请求，等待当前写入和数据库收尾...")

    def on_progress(self, percent: int, completed: int, total: int):
        if not _should_update_download_ui(self, '_last_baostock_progress_ui_ts', completed, total):
            return
        self.progress_bar.setValue(percent)
        self.import_status_label.setText(f"进度: {completed}/{total}")
        self.import_eta_label.setText(self._estimate_eta_text(self._import_start_time, completed, total))

    def on_status(self, message: str):
        self.import_status_label.setText(message)
        self.log(message)

    def on_indicator_progress(self, percent: int, completed: int, total: int):
        if not _should_update_download_ui(self, '_last_baostock_indicator_progress_ui_ts', completed, total):
            return
        self.indicator_progress_bar.setValue(percent)
        self.indicator_status_label.setText(f"进度: {completed}/{total}")
        self.indicator_eta_label.setText(self._estimate_eta_text(self._indicator_start_time, completed, total))

    def on_indicator_status(self, message: str):
        self.indicator_status_label.setText(message)
        self.indicator_log(message)

    def on_conflict(self, stock: str, period: str, existing_count: int, new_count: int):
        msg = QMessageBox(self)
        msg.setWindowTitle("数据已存在")
        msg.setText(
            f"{stock} {period} 已存在 {existing_count} 条记录，准备导入 {new_count} 条。\n请选择处理方式:"
        )
        overwrite_btn = msg.addButton("覆盖", QMessageBox.AcceptRole)
        skip_btn = msg.addButton("跳过", QMessageBox.RejectRole)
        msg.setDefaultButton(skip_btn)
        checkbox = QCheckBox("后续都采用相同方式处理")
        msg.setCheckBox(checkbox)
        msg.exec_()
        chosen = msg.clickedButton()
        resolution = "overwrite" if chosen == overwrite_btn else "skip"
        apply_all = checkbox.isChecked()
        if self.import_thread:
            self.import_thread.set_conflict_resolution(resolution, apply_all)

    def on_finished(self, results: dict):
        cancelled = bool(results.get('cancelled'))
        unresolved_count = int(results.get('unresolved_count', 0) or 0)
        benchmark_unresolved = results.get('benchmark_unresolved') or []
        benchmark_error = str(results.get('benchmark_error') or '')
        benchmark_empty = int(results.get('benchmark_empty', 0) or 0)
        post_scan_errors = results.get('post_scan_errors') or []
        failed_count = int(results.get('failed', 0) or 0)
        empty_count = int(results.get('empty', 0) or 0)
        adjustment_failed = int(results.get('adjustment_failed', 0) or 0)
        limit_reached = bool(results.get('limit_reached'))
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if not cancelled:
            self.progress_bar.setValue(100)
        self.import_eta_label.setText("预计剩余：--" if cancelled else "预计剩余：00:00:00")
        self.log("导入已安全停止" if cancelled else "导入完成")
        self._refresh_parent_after_changes(results)
        if self._close_after_stop:
            self._schedule_close_when_idle()
            return
        if cancelled:
            title = "导入已停止"
        elif (
            adjustment_failed
            or limit_reached
            or failed_count
            or empty_count
            or unresolved_count
            or benchmark_unresolved
            or benchmark_error
            or benchmark_empty
            or post_scan_errors
        ):
            title = "导入完成（仍有待核验项）"
        else:
            title = "导入完成"
        summary = (
            f"已是最新: {results.get('up_to_date', 0)}\n"
            f"成功区间: {results.get('success', 0)}\n"
            f"失败区间: {results.get('failed', 0)}\n"
            f"无数据区间: {results.get('empty', 0)}\n"
            f"处理记录数: {results.get('total_records', 0)}"
        )
        if unresolved_count:
            summary += (
                f"\n待核验交易日: {unresolved_count}"
                "（可能停牌、未上市、数据源未返回或下载未完成）"
            )
        if benchmark_unresolved:
            summary += f"\n基准待核验交易日: {len(benchmark_unresolved)}"
        if benchmark_empty:
            summary += f"\n基准空返区间: {benchmark_empty}"
        if benchmark_error:
            summary += f"\n基准检查失败: {benchmark_error}"
        if post_scan_errors:
            summary += f"\n复核失败的股票周期: {len(post_scan_errors)}"
        if adjustment_failed:
            summary += f"\n复权未完整的区间: {adjustment_failed}"
        if limit_reached:
            summary += "\nBaoStock 请求保护上限已触发，未完成项保持待补"
        if (
            adjustment_failed
            or limit_reached
            or failed_count
            or empty_count
            or unresolved_count
            or benchmark_unresolved
            or benchmark_error
            or benchmark_empty
            or post_scan_errors
        ):
            QMessageBox.warning(self, title, summary)
        else:
            QMessageBox.information(self, title, summary)

    def on_indicator_finished(self, results: dict):
        cancelled = bool(results.get('cancelled'))
        unresolved_count = int(results.get('unresolved_count', 0) or 0)
        failed_count = int(results.get('failed', 0) or 0)
        self.indicator_start_btn.setEnabled(True)
        self.indicator_stop_btn.setEnabled(False)
        if not cancelled:
            self.indicator_progress_bar.setValue(100)
        self.indicator_eta_label.setText("预计剩余：--" if cancelled else "预计剩余：00:00:00")
        self.indicator_log("下载已安全停止" if cancelled else "下载完成")
        self._refresh_parent_after_changes(results)
        if self._close_after_stop:
            self._schedule_close_when_idle()
            return
        title = (
            "下载已停止" if cancelled
            else "下载完成（仍有待核验项）" if failed_count or unresolved_count
            else "下载完成"
        )
        summary = (
            f"已是最新: {results.get('up_to_date', 0)}\n"
            f"无本地日线: {results.get('no_local_daily', 0)}\n"
            f"成功区间: {results.get('success', 0)}\n"
            f"失败: {results.get('failed', 0)}\n"
            f"无数据区间: {results.get('empty', 0)}\n"
            f"更新日线数: {results.get('total_records', 0)}"
        )
        if unresolved_count:
            summary += f"\n待核验指标缺口区间: {unresolved_count}"
        if failed_count or unresolved_count:
            QMessageBox.warning(self, title, summary)
        else:
            QMessageBox.information(self, title, summary)

    def on_error(self, error: str):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.import_eta_label.setText("预计剩余：--")
        if self._close_after_stop:
            self._schedule_close_when_idle()
        else:
            QMessageBox.warning(self, "导入错误", error)

    def on_indicator_error(self, error: str):
        self.indicator_start_btn.setEnabled(True)
        self.indicator_stop_btn.setEnabled(False)
        self.indicator_eta_label.setText("预计剩余：--")
        if self._close_after_stop:
            self._schedule_close_when_idle()
        else:
            QMessageBox.warning(self, "导入错误", error)

    def _refresh_parent_after_changes(self, results: dict):
        if not results.get('total_records', 0):
            return
        parent = self.parent()
        parent_closing = bool(
            parent
            and (
                getattr(parent, '_viewer_closing', False)
                or getattr(parent, '_pending_close_after_tushare_stop', False)
            )
        )
        if parent_closing or parent is None:
            return
        try:
            if hasattr(parent, 'on_refresh_clicked'):
                parent.on_refresh_clicked()
            elif hasattr(parent, 'refresh_stock_list'):
                parent.refresh_stock_list()
        except Exception:
            pass

    def is_import_running(self) -> bool:
        try:
            return bool(
                (self.import_thread and self.import_thread.isRunning())
                or (self.indicator_thread and self.indicator_thread.isRunning())
            )
        except RuntimeError:
            return False

    def request_close_after_stop(self):
        """停止领取新任务，待线程和数据库收尾完成后自动关闭。"""
        if not self.is_import_running():
            self.close()
            return
        self._close_after_stop = True
        for button in (
            self.start_btn, self.stop_btn, self.close_btn,
            self.indicator_start_btn, self.indicator_stop_btn,
            self.indicator_close_btn,
        ):
            button.setEnabled(False)
        if self.import_thread and self.import_thread.isRunning():
            self.import_thread.stop()
            self.import_status_label.setText("正在安全停止并收尾数据库...")
            self.log("正在安全停止：等待当前写入和数据库连接收尾后关闭窗口...")
        if self.indicator_thread and self.indicator_thread.isRunning():
            self.indicator_thread.stop()
            self.indicator_status_label.setText("正在安全停止并收尾数据库...")
            self.indicator_log("正在安全停止：等待当前写入和数据库连接收尾后关闭窗口...")
        self._schedule_close_when_idle()

    def _schedule_close_when_idle(self):
        if self._close_poll_scheduled:
            return
        self._close_poll_scheduled = True
        QTimer.singleShot(100, self._close_when_idle)

    def _close_when_idle(self):
        self._close_poll_scheduled = False
        if self.is_import_running():
            self._schedule_close_when_idle()
            return
        self.close()

    def _estimate_eta_text(self, start_time: Optional[datetime], completed: int, total: int) -> str:
        if not start_time or completed <= 0 or total <= 0:
            return "预计剩余：--"
        elapsed = (datetime.now() - start_time).total_seconds()
        if elapsed <= 0:
            return "预计剩余：--"
        remaining_tasks = max(0, total - completed)
        if remaining_tasks == 0:
            return "预计剩余：00:00:00"
        seconds = int(round(elapsed * remaining_tasks / max(1, completed)))
        seconds = max(0, seconds)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"预计剩余：{hours:02d}:{minutes:02d}:{secs:02d}"

    def closeEvent(self, event):
        if self.is_import_running():
            if not self._close_after_stop:
                reply = QMessageBox.question(
                    self, "确认关闭",
                    "任务正在进行中，确定要关闭吗？\n"
                    "系统会先停止领取新任务，等待当前写入和数据库连接收尾后再关闭。",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    event.ignore()
                    return
                self.request_close_after_stop()
            event.ignore()
            self._schedule_close_when_idle()
            return
        event.accept()

    def indicator_log(self, message: str):
        _append_download_log(self.indicator_log_text, f"[{datetime.now().strftime('%H:%M:%S')}] {message}")


# ============================================================
# 扫描报告对话框
# ============================================================


# ============================================================
# 数据完整性检查对话框
# ============================================================

class DataIntegrityDialog(QDialog):
    """数据完整性检查对话框"""
    
    def __init__(self, manager: DuckDBManager, stock_code: str, period: str,
                 start_date: str, end_date: str, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.stock_code = stock_code
        self.period = period
        self.start_date = start_date
        self.end_date = end_date
        self.rect_info = {}  # 存储方块信息，用于鼠标悬停

        self.setWindowTitle(f"数据完整性检查 - {stock_code} ({period})")
        self.setMinimumSize(800, 600)

        # 设置Windows暗色标题栏
        self._set_dark_titlebar()

        # 设置暗色主题样式（与主界面一致）
        self.setStyleSheet("""
            QDialog, QWidget {
                background-color: #333333;
                color: #e8e8e8;
                font-family: "Microsoft YaHei UI";
            }
            QLabel {
                color: #e8e8e8;
            }
            QPushButton {
                background-color: #0078d4;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-family: "Microsoft YaHei UI";
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QFrame {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 5px;
            }
        """)

        self.init_ui()
        self.check_integrity()

    def _set_dark_titlebar(self):
        """设置Windows暗色标题栏"""
        try:
            import platform
            if platform.system() == "Windows":
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
            import logging
            logging.debug(f"设置暗色标题栏失败: {str(e)}")
    
    def init_ui(self):
        """初始化UI"""
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)

        # 标题区域 - 暗色主题
        title_frame = QFrame()
        title_frame.setStyleSheet("""
            QFrame {
                background-color: #3c3c3c;
                border-radius: 5px;
                padding: 10px;
            }
        """)
        title_layout = QVBoxLayout(title_frame)
        title_layout.setContentsMargins(10, 10, 10, 10)

        # 信息标签 - 使用更大的字体和更好的样式
        info_label = QLabel(f"📊 股票: <b>{self.stock_code}</b> | 周期: <b>{self.period}</b> | "
                          f"时间范围: <b>{self.start_date}</b> ~ <b>{self.end_date}</b>")
        info_label.setFont(QFont("Microsoft YaHei", 10))
        info_label.setStyleSheet("color: #e8e8e8;")
        title_layout.addWidget(info_label)
        layout.addWidget(title_frame)

        # 图例 - 使用暗色主题样式
        legend_frame = QFrame()
        legend_frame.setStyleSheet("""
            QFrame {
                background-color: #3c3c3c;
                border: 1px solid #555555;
                border-radius: 5px;
                padding: 8px;
            }
        """)
        legend_layout = QHBoxLayout(legend_frame)
        legend_layout.setContentsMargins(10, 5, 10, 5)
        legend_layout.addWidget(QLabel("<b>图例:</b>"))
        legend_layout.addSpacing(10)

        legend_items = [
            ("完整数据", "#4CAF50", "#2E7D32"),  # 绿色系
            ("部分数据", "#FF9800", "#F57C00"),  # 橙色系
            ("无数据", "#5a5a5a", "#404040")      # 暗灰色系
        ]

        for text, color, border_color in legend_items:
            # 颜色方块
            color_label = QLabel()
            color_label.setStyleSheet(f"""
                QLabel {{
                    background-color: {color};
                    border: 2px solid {border_color};
                    border-radius: 3px;
                    min-width: 24px;
                    max-width: 24px;
                    min-height: 24px;
                    max-height: 24px;
                }}
            """)
            legend_layout.addWidget(color_label)

            # 文字标签
            text_label = QLabel(text)
            text_label.setFont(QFont("Microsoft YaHei", 9))
            text_label.setStyleSheet("color: #e8e8e8;")
            legend_layout.addWidget(text_label)
            legend_layout.addSpacing(15)

        legend_layout.addStretch()
        layout.addWidget(legend_frame)

        # Matplotlib图表 - 暗色背景
        self.figure = Figure(figsize=(11, 7))
        self.figure.patch.set_facecolor('#333333')
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setStyleSheet("background-color: #333333; border: 1px solid #404040; border-radius: 5px;")
        layout.addWidget(self.canvas)
        
        # 统计信息 - 使用暗色主题样式
        stats_frame = QFrame()
        stats_frame.setStyleSheet("""
            QFrame {
                background-color: #3c3c3c;
                border: 1px solid #555555;
                border-radius: 5px;
                padding: 10px;
            }
        """)
        stats_layout = QHBoxLayout(stats_frame)
        stats_layout.setContentsMargins(15, 8, 15, 8)
        self.stats_label = QLabel("正在检查...")
        self.stats_label.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
        self.stats_label.setStyleSheet("color: #e8e8e8;")
        stats_layout.addWidget(self.stats_label)
        stats_layout.addStretch()
        layout.addWidget(stats_frame)

        # 关闭按钮 - 使用暗色主题样式
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setFont(QFont("Microsoft YaHei", 9))
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 25px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:pressed {
                background-color: #005a9e;
            }
        """)
        close_btn.clicked.connect(self.close)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)
    
    def check_integrity(self):
        """检查数据完整性"""
        try:
            # 获取交易日列表
            if khQTTools is None:
                QMessageBox.warning(self, "错误", "无法导入khQTTools模块")
                return

            # 使用 _get_trade_days_list 方法（接收datetime对象）
            # 支持两种日期格式: YYYYMMDD 或 YYYY-MM-DD
            try:
                start_dt = datetime.strptime(self.start_date, '%Y%m%d')
            except ValueError:
                start_dt = datetime.strptime(self.start_date, '%Y-%m-%d')

            try:
                end_dt = datetime.strptime(self.end_date, '%Y%m%d')
            except ValueError:
                end_dt = datetime.strptime(self.end_date, '%Y-%m-%d')

            trade_days_dt = khQTTools._get_trade_days_list(start_dt, end_dt)

            # 转换为字符串列表
            trade_days = [dt.strftime('%Y-%m-%d') for dt in trade_days_dt]
            
            if not trade_days:
                QMessageBox.warning(self, "错误", "无法获取交易日列表")
                return
            
            # 获取实际数据
            df = self.manager.get_kline_data(
                self.stock_code, self.period,
                self.start_date, self.end_date
            )
            
            if df.empty:
                QMessageBox.warning(self, "提示", "该时间段内无数据")
                return
            
            # 转换时间列为日期
            if 'time' in df.columns:
                df['date'] = pd.to_datetime(df['time']).dt.date
            else:
                QMessageBox.warning(self, "错误", "数据中缺少time列")
                return
            
            # 根据周期判断每个交易日的数据完整性
            integrity_status = {}
            
            if self.period == '1d':
                # 日线数据：每个交易日应该有1条数据
                for trade_day_str in trade_days:
                    trade_day = datetime.strptime(trade_day_str, '%Y-%m-%d').date()
                    day_data = df[df['date'] == trade_day]
                    
                    if len(day_data) == 0:
                        integrity_status[trade_day_str] = 'none'  # 无数据
                    elif len(day_data) >= 1:
                        integrity_status[trade_day_str] = 'complete'  # 完整数据
                    else:
                        integrity_status[trade_day_str] = 'partial'  # 部分数据
            
            elif self.period in ['1m', '5m']:
                # 分钟数据：需要判断是否有足够的K线
                # 正常交易日应该有240条1分钟数据（4小时 * 60分钟）
                # 或者48条5分钟数据（4小时 * 12个5分钟）
                expected_count = 240 if self.period == '1m' else 48
                
                for trade_day_str in trade_days:
                    trade_day = datetime.strptime(trade_day_str, '%Y-%m-%d').date()
                    day_data = df[df['date'] == trade_day]
                    
                    if len(day_data) == 0:
                        integrity_status[trade_day_str] = 'none'
                    elif len(day_data) >= expected_count * 0.95:  # 95%以上认为完整
                        integrity_status[trade_day_str] = 'complete'
                    else:
                        integrity_status[trade_day_str] = 'partial'
            
            elif self.period == 'tick':
                # Tick数据：需要判断是否有足够的数据
                # 正常交易日应该有4700条以上的tick数据
                expected_count = 4700
                # 以95%作为完整标准
                complete_threshold = int(expected_count * 0.95)  # 4465条
                
                for trade_day_str in trade_days:
                    trade_day = datetime.strptime(trade_day_str, '%Y-%m-%d').date()
                    day_data = df[df['date'] == trade_day]
                    
                    if len(day_data) == 0:
                        integrity_status[trade_day_str] = 'none'
                    elif len(day_data) >= complete_threshold:  # 达到4700的95%（4465条）认为完整
                        integrity_status[trade_day_str] = 'complete'
                    else:
                        integrity_status[trade_day_str] = 'partial'
            
            else:
                # 其他周期：简单判断有数据即可
                for trade_day_str in trade_days:
                    trade_day = datetime.strptime(trade_day_str, '%Y-%m-%d').date()
                    day_data = df[df['date'] == trade_day]
                    
                    if len(day_data) == 0:
                        integrity_status[trade_day_str] = 'none'
                    elif len(day_data) > 0:
                        integrity_status[trade_day_str] = 'complete'
                    else:
                        integrity_status[trade_day_str] = 'partial'
            
            # 绘制方块矩阵图
            self.plot_integrity_matrix(trade_days, integrity_status)
            
            # 更新统计信息 - 使用更美观的格式
            complete_count = sum(1 for v in integrity_status.values() if v == 'complete')
            partial_count = sum(1 for v in integrity_status.values() if v == 'partial')
            none_count = sum(1 for v in integrity_status.values() if v == 'none')
            total_count = len(trade_days)
            
            # 计算百分比
            complete_pct = (complete_count / total_count * 100) if total_count > 0 else 0
            partial_pct = (partial_count / total_count * 100) if total_count > 0 else 0
            none_pct = (none_count / total_count * 100) if total_count > 0 else 0
            
            stats_text = (f"📈 统计: "
                         f"<span style='color: #4CAF50; font-weight: bold;'>完整 {complete_count} 天 ({complete_pct:.1f}%)</span> | "
                         f"<span style='color: #FF9800; font-weight: bold;'>部分 {partial_count} 天 ({partial_pct:.1f}%)</span> | "
                         f"<span style='color: #757575; font-weight: bold;'>缺失 {none_count} 天 ({none_pct:.1f}%)</span> | "
                         f"<span style='color: #2196F3; font-weight: bold;'>总计 {total_count} 个交易日</span>")
            self.stats_label.setText(stats_text)
            
        except Exception as e:
            QMessageBox.warning(self, "错误", f"检查数据完整性时出错: {str(e)}")
            import traceback
            traceback.print_exc()
    
    def plot_integrity_matrix(self, trade_days: List[str], integrity_status: Dict[str, str]):
        """绘制数据完整性方块矩阵图"""
        self.figure.clear()
        ax = self.figure.add_subplot(111)

        # 颜色映射 - 暗色主题
        color_map = {
            'complete': '#4CAF50',  # 柔和的绿色
            'partial': '#FF9800',   # 柔和的橙色
            'none': '#5a5a5a'       # 暗灰色（适合暗色主题）
        }
        
        # 计算矩阵大小（尽量接近正方形）
        total_days = len(trade_days)
        cols = int(np.ceil(np.sqrt(total_days)))
        rows = int(np.ceil(total_days / cols))
        
        # 存储每个方块的位置和对应的日期信息
        self.rect_info = {}  # {(row, col): {'date': date_str, 'status': status}}
        
        # 绘制彩色方块
        for row in range(rows):
            for col in range(cols):
                idx = row * cols + col
                if idx < len(trade_days):
                    trade_day = trade_days[idx]
                    status = integrity_status.get(trade_day, 'none')
                    color = color_map.get(status, '#FFFFFF')
                    
                    # 根据状态设置边框颜色
                    edge_color = {
                        'complete': '#2E7D32',  # 深绿色边框
                        'partial': '#F57C00',   # 深橙色边框
                        'none': '#BDBDBD'       # 灰色边框
                    }.get(status, '#CCCCCC')
                    
                    rect = Rectangle((col - 0.5, row - 0.5), 1, 1,
                                    facecolor=color,
                                    edgecolor=edge_color, 
                                    linewidth=1.2,
                                    alpha=0.9)
                    ax.add_patch(rect)
                    
                    # 存储方块信息
                    self.rect_info[(row, col)] = {
                        'date': trade_day,
                        'status': status
                    }
        
        ax.set_xlim(-0.5, cols - 0.5)
        ax.set_ylim(-0.5, rows - 0.5)
        ax.set_xticks([])
        ax.set_yticks([])

        # 设置标题样式 - 暗色主题
        ax.set_title(f"股票数据完整性 ({self.stock_code} - {self.period})",
                    fontsize=14, fontweight='bold', pad=15, color='#e8e8e8')

        # 设置背景色 - 暗色主题
        ax.set_facecolor('#333333')

        # 反转Y轴，使第一个交易日显示在左上角（从下往上排列）
        ax.invert_yaxis()

        # 添加鼠标悬停提示 - 暗色主题样式
        self.annot = ax.annotate("", xy=(0,0), xytext=(20,20), textcoords="offset points",
                                bbox=dict(boxstyle="round,pad=0.8",
                                         facecolor='#3c3c3c',
                                         edgecolor='#0078d4',
                                         linewidth=2,
                                         alpha=0.95),
                                arrowprops=dict(arrowstyle="->",
                                               connectionstyle="arc3,rad=0.3",
                                               color='#0078d4',
                                               lw=2))
        self.annot.set_visible(False)
        
        # 连接鼠标移动事件
        self.canvas.mpl_connect("motion_notify_event", self.on_hover)
        
        self.canvas.draw()
    
    def on_hover(self, event):
        """鼠标悬停事件处理"""
        if event.inaxes is None or event.inaxes != self.figure.axes[0]:
            self.annot.set_visible(False)
            self.canvas.draw_idle()
            return
        
        if event.xdata is None or event.ydata is None:
            return
        
        # 获取鼠标位置对应的行列
        # 由于使用了invert_yaxis()，Y轴已反转
        # 绘制时row从0到rows-1，Y坐标从rows-0.5到-0.5（反转后）
        # 所以需要从反转后的坐标计算row
        col = int(round(event.xdata + 0.5))
        # 计算row：由于Y轴反转，需要从顶部计算
        # 反转后Y坐标范围：顶部是rows-0.5，底部是-0.5
        # row = rows - 1 - int(round(event.ydata + 0.5))
        # 更简单的方法：直接使用反转后的坐标
        row = int(round(event.ydata + 0.5))
        
        # 检查是否有对应的方块
        if (row, col) in self.rect_info:
            info = self.rect_info[(row, col)]
            date_str = info['date']
            status = info['status']
            
            # 状态中文描述和颜色
            status_info = {
                'complete': ('完整数据', '#4CAF50'),
                'partial': ('部分数据', '#FF9800'),
                'none': ('无数据', '#757575')
            }.get(status, ('未知', '#757575'))
            
            status_text, status_color = status_info
            
            # 格式化日期显示（添加星期）
            try:
                date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                weekday = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][date_obj.weekday()]
                date_display = f"{date_str} ({weekday})"
            except:
                date_display = date_str
            
            # 显示提示信息 - 使用更美观的格式
            self.annot.xy = (event.xdata, event.ydata)
            
            # 使用纯文本格式（matplotlib不支持HTML）
            text = f"📅 日期: {date_display}\n📊 状态: {status_text}"
            self.annot.set_text(text)
            self.annot.set_fontsize(10)
            self.annot.set_fontfamily('Microsoft YaHei')
            self.annot.set_visible(True)
        else:
            self.annot.set_visible(False)

        self.canvas.draw_idle()


# ============================================================
# 全量增量数据补充线程（扫描+下载一体化）
# ============================================================


class TushareImportDialog(QDialog):
    """
    Tushare 数据导入对话框

    单页 UI，与 BaoStock 导入对话框的"自定义补充数据"风格一致。
    使用乘法前复权（adj_factor 方式）。
    Token 和代理配置从 QSettings 读取，在【软件设置 → Tushare设置】中设置。
    """

    def __init__(self, manager: DuckDBManager, parent=None):
        super().__init__(parent)
        self.manager      = manager
        self.import_thread = None
        self._close_after_stop = False
        self.font_scale    = get_ui_font_scale()
        self._base_style_raw = None
        self._import_start_time = None

        self.setWindowTitle("从Tushare导入数据")
        self.setMinimumSize(820, 760)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowFlags(self.windowFlags() | Qt.Window)
        self._set_dark_titlebar()

        self._base_style_raw = """
            QDialog, QWidget {
                background-color: #333333;
                color: #e8e8e8;
                font-family: "Microsoft YaHei UI";
            }
            QLabel { color: #e8e8e8; }
            QLineEdit, QTextEdit {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px;
            }
            QPushButton {
                background-color: #0078d4;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-family: "Microsoft YaHei UI";
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover { background-color: #106ebe; }
            QPushButton:pressed { background-color: #005a9e; }
            QPushButton:disabled {
                background-color: #555555;
                color: #888888;
            }
            QCheckBox { color: #e8e8e8; }
            QCheckBox::indicator {
                width: 18px; height: 18px;
                border: 1px solid #555555;
                border-radius: 3px;
                background-color: #3c3c3c;
            }
            QCheckBox::indicator:checked {
                background-color: #0078d4;
                border-color: #0078d4;
            }
            QRadioButton { color: #e8e8e8; }
            QRadioButton::indicator {
                width: 18px; height: 18px;
                border: 1px solid #555555;
                border-radius: 9px;
                background-color: #3c3c3c;
            }
            QRadioButton::indicator:checked {
                background-color: #0078d4;
                border-color: #0078d4;
            }
            QGroupBox {
                border: 1px solid #555555;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
                color: #e8e8e8;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QProgressBar {
                background-color: #3c3c3c;
                border: 1px solid #555555;
                border-radius: 3px;
                text-align: center;
                color: #e8e8e8;
            }
            QProgressBar::chunk { background-color: #0078d4; }
            QSpinBox, QDateEdit {
                background-color: #3c3c3c;
                color: #e8e8e8;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px;
            }
            QFrame { background-color: #333333; color: #e8e8e8; }
        """
        _ui_font = get_preferred_ui_font_family() or "Microsoft YaHei UI"
        if _ui_font != "Microsoft YaHei UI":
            self._base_style_raw = self._base_style_raw.replace('"Microsoft YaHei UI"', f'"{_ui_font}"')
        self.setStyleSheet(self._scale_stylesheet(self._base_style_raw, self.font_scale))
        self._init_ui()
        self.apply_ui_scale(self.font_scale)

    # ------------------------------------------------------------------
    # 暗色标题栏 / 字号缩放（与其他对话框保持一致）
    # ------------------------------------------------------------------

    def _set_dark_titlebar(self):
        try:
            import platform
            if platform.system() == "Windows":
                from ctypes import windll, c_int, byref, sizeof
                from ctypes.wintypes import DWORD
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                DWMWA_CAPTION_COLOR = 35
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()), DWMWA_USE_IMMERSIVE_DARK_MODE,
                    byref(c_int(2)), sizeof(c_int)
                )
                caption_color = DWORD(0x333333)
                windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()), DWMWA_CAPTION_COLOR,
                    byref(caption_color), sizeof(caption_color)
                )
        except Exception:
            pass

    def _scale_stylesheet(self, style: str, scale: float) -> str:
        if not style:
            return style
        import re
        def repl(m):
            scaled = max(6, int(round(float(m.group(1)) * float(scale))))
            return f"font-size: {scaled}{m.group(2)}"
        return re.sub(
            r"font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(px|pt)",
            repl, style, flags=re.IGNORECASE
        )

    def apply_ui_scale(self, scale=None):
        if scale is None:
            scale = get_ui_font_scale()
        self.font_scale = scale
        if self._base_style_raw:
            self.setStyleSheet(self._scale_stylesheet(self._base_style_raw, self.font_scale))
        try:
            for child in self.findChildren(QWidget):
                base_ss = child.property("ui_base_stylesheet")
                if base_ss:
                    child.setStyleSheet(self._scale_stylesheet(base_ss, self.font_scale))
                base_pt = child.property("ui_base_font_pt")
                if base_pt:
                    font = child.font()
                    font.setPointSize(max(6, int(round(float(base_pt) * self.font_scale))))
                    child.setFont(font)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _init_ui(self):
        layout = QVBoxLayout(self)

        # ===== 股票池选择 =====
        stock_group = QGroupBox("股票池选择")
        stock_layout = QVBoxLayout(stock_group)

        method_layout = QHBoxLayout()
        self.method_group = QButtonGroup(self)
        self.preset_radio = QRadioButton("预设板块")
        self.preset_radio.setChecked(True)
        self.method_group.addButton(self.preset_radio)
        method_layout.addWidget(self.preset_radio)

        self.file_radio = QRadioButton("从文件导入")
        self.method_group.addButton(self.file_radio)
        method_layout.addWidget(self.file_radio)

        self.manual_radio = QRadioButton("手动输入")
        self.method_group.addButton(self.manual_radio)
        method_layout.addWidget(self.manual_radio)
        method_layout.addStretch()
        stock_layout.addLayout(method_layout)

        # 预设板块
        self.preset_frame = QFrame()
        preset_grid = QGridLayout(self.preset_frame)
        preset_grid.setContentsMargins(0, 0, 0, 0)
        self.preset_checks = {}
        presets = [
            ('沪深A股',   'all_a'),
            ('上证A股',   'sh_a'),
            ('深证A股',   'sz_a'),
            ('沪深300',   'hs300'),
            ('上证50',    'sz50'),
            ('中证500',   'zz500'),
            ('创业板',    'cyb'),
            ('科创板',    'kcb'),
            ('沪深ETF',   'hs_etf'),
            ('沪深场内基金（含ETF/LOF）', 'hs_fund'),
            ('沪深转债',  'hs_convertible_bonds'),
            ('T0型ETF',   't0_etf'),
            ('常用指数',  'common_index'),
        ]
        for i, (name, key) in enumerate(presets):
            cb = QCheckBox(name)
            self.preset_checks[key] = cb
            preset_grid.addWidget(cb, i // 4, i % 4)
        stock_layout.addWidget(self.preset_frame)

        # 文件导入
        self.file_frame = QFrame()
        file_layout = QHBoxLayout(self.file_frame)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_path_edit = QLineEdit()
        self.file_path_edit.setPlaceholderText("选择股票列表CSV文件...")
        file_layout.addWidget(self.file_path_edit)
        self.browse_file_btn = QPushButton("浏览...")
        self.browse_file_btn.clicked.connect(self.browse_stock_file)
        file_layout.addWidget(self.browse_file_btn)
        self.file_frame.setVisible(False)
        stock_layout.addWidget(self.file_frame)

        # 手动输入
        self.manual_frame = QFrame()
        manual_layout = QVBoxLayout(self.manual_frame)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.addWidget(QLabel("输入股票代码（每行一个，支持纯数字或带市场后缀）:"))
        self.manual_edit = QTextEdit()
        self.manual_edit.setMaximumHeight(100)
        self.manual_edit.setPlaceholderText("000001.SZ 或 000001\n600000.SH 或 600000\n300750")
        manual_layout.addWidget(self.manual_edit)
        self.manual_frame.setVisible(False)
        stock_layout.addWidget(self.manual_frame)

        self.preset_radio.toggled.connect(self._on_method_changed)
        self.file_radio.toggled.connect(self._on_method_changed)
        self.manual_radio.toggled.connect(self._on_method_changed)
        layout.addWidget(stock_group)

        # ===== 数据周期设置 =====
        period_group = QGroupBox("数据周期设置")
        period_layout = QGridLayout(period_group)
        today = QDate.currentDate()

        self.period_1d_check = QCheckBox("日线 (1d)")
        self.period_1d_check.setChecked(True)
        period_layout.addWidget(self.period_1d_check, 0, 0)
        period_layout.addWidget(QLabel("开始:"), 0, 1)
        self.period_1d_start = QDateEdit()
        self.period_1d_start.setCalendarPopup(True)
        self.period_1d_start.setDate(today.addYears(-10))
        period_layout.addWidget(self.period_1d_start, 0, 2)
        period_layout.addWidget(QLabel("结束:"), 0, 3)
        self.period_1d_end = QDateEdit()
        self.period_1d_end.setCalendarPopup(True)
        self.period_1d_end.setDate(today)
        period_layout.addWidget(self.period_1d_end, 0, 4)

        self.period_1m_check = QCheckBox("1分钟 (1m)")
        self.period_1m_check.setChecked(False)
        self.period_1m_check.setToolTip(
            "分钟行情请求量较大，且 Tushare 指数接口不支持分钟线；需要时再勾选。"
        )
        period_layout.addWidget(self.period_1m_check, 1, 0)
        period_layout.addWidget(QLabel("开始:"), 1, 1)
        self.period_1m_start = QDateEdit()
        self.period_1m_start.setCalendarPopup(True)
        self.period_1m_start.setDate(today.addYears(-1))
        period_layout.addWidget(self.period_1m_start, 1, 2)
        period_layout.addWidget(QLabel("结束:"), 1, 3)
        self.period_1m_end = QDateEdit()
        self.period_1m_end.setCalendarPopup(True)
        self.period_1m_end.setDate(today)
        period_layout.addWidget(self.period_1m_end, 1, 4)

        self.period_5m_check = QCheckBox("5分钟 (5m)")
        self.period_5m_check.setChecked(False)
        self.period_5m_check.setToolTip(
            "分钟行情请求量较大，且 Tushare 指数接口不支持分钟线；需要时再勾选。"
        )
        period_layout.addWidget(self.period_5m_check, 2, 0)
        period_layout.addWidget(QLabel("开始:"), 2, 1)
        self.period_5m_start = QDateEdit()
        self.period_5m_start.setCalendarPopup(True)
        self.period_5m_start.setDate(today.addYears(-1))
        period_layout.addWidget(self.period_5m_start, 2, 2)
        period_layout.addWidget(QLabel("结束:"), 2, 3)
        self.period_5m_end = QDateEdit()
        self.period_5m_end.setCalendarPopup(True)
        self.period_5m_end.setDate(today)
        period_layout.addWidget(self.period_5m_end, 2, 4)

        self.warmup_hint_label = QLabel(
            "提示：开始日期要早于回测开始日期，给均线、MACD 等指标留出预热期（例如用 60 日均线，至少往前多下 3 个月）。1 分钟线需要 Tushare 的 stk_mins 权限，ETF 等场内基金需要 fund_daily 权限；指数只有日线。"
        )
        self.warmup_hint_label.setWordWrap(True)
        self.warmup_hint_label.setStyleSheet("color: #8a5a00;")
        period_layout.addWidget(self.warmup_hint_label, 3, 0, 1, 5)

        layout.addWidget(period_group)

        # ===== 复权方式 =====
        adj_group = QGroupBox(
            "价格字段（原始价为基础必存；复权价需 adj_factor 接口约2000积分）"
        )
        adj_layout = QHBoxLayout(adj_group)
        self.adj_none_check  = QCheckBox("原始价（必存）")
        self.adj_front_check = QCheckBox("前复权")
        self.adj_back_check  = QCheckBox("后复权")
        self.adj_none_check.setChecked(True)
        self.adj_none_check.setEnabled(False)
        self.adj_none_check.setToolTip(
            "DuckDB 同一行以原始行情作为基础字段，前/后复权价存放在附加列中。"
        )
        self.adj_front_check.setChecked(True)
        self.adj_back_check.setChecked(True)
        adj_layout.addWidget(self.adj_none_check)
        adj_layout.addWidget(self.adj_front_check)
        adj_layout.addWidget(self.adj_back_check)
        adj_layout.addStretch()
        layout.addWidget(adj_group)

        # ===== 进度信息 =====
        progress_group = QGroupBox("进度信息")
        progress_layout = QVBoxLayout(progress_group)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("%v/%m (%p%)")
        progress_layout.addWidget(self.progress_bar)
        status_layout = QVBoxLayout()
        self.import_status_label = QLabel("就绪")
        self.import_status_label.setWordWrap(True)
        status_layout.addWidget(self.import_status_label)
        self.import_eta_label = QLabel("预计剩余：--")
        self.import_eta_label.setWordWrap(True)
        status_layout.addWidget(self.import_eta_label)
        progress_layout.addLayout(status_layout)
        layout.addWidget(progress_group)

        # ===== 执行日志 =====
        log_group = QGroupBox("执行日志")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        _configure_download_log_widget(self.log_text)
        log_layout.addWidget(self.log_text)
        layout.addWidget(log_group)

        # ===== 按钮行 =====
        # 覆写选项（默认增量，勾选才强制覆写）
        overwrite_layout = QHBoxLayout()
        self.force_overwrite_check = QCheckBox("强制覆写已有数据（跳过增量检测，重新下载上方设定的全部时间范围）")
        self.force_overwrite_check.setChecked(False)
        overwrite_layout.addWidget(self.force_overwrite_check)
        overwrite_layout.addStretch()
        layout.addLayout(overwrite_layout)

        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始下载")
        self.start_btn.clicked.connect(self.start_import)
        btn_layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_import)
        btn_layout.addWidget(self.stop_btn)

        btn_layout.addStretch()

        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def _on_method_changed(self):
        self.preset_frame.setVisible(self.preset_radio.isChecked())
        self.file_frame.setVisible(self.file_radio.isChecked())
        self.manual_frame.setVisible(self.manual_radio.isChecked())

    def browse_stock_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择股票列表文件", "", "CSV文件 (*.csv);;所有文件 (*.*)"
        )
        if file_path:
            self.file_path_edit.setText(file_path)

    def _normalize_stock_code_with_market(self, code: str) -> str:
        return _normalize_market_security_code(code)

    def _read_stock_file(self, file_path: str) -> List[str]:
        stocks = []
        try:
            # 只读一次（header=None），根据首行内容判断是否有列名行，避免二次 I/O
            df = pd.read_csv(file_path, dtype=str, header=None, encoding='utf-8-sig')
            if df.empty or df.shape[1] == 0:
                return stocks
            first_cell = str(df.iloc[0, 0]).strip()
            if any(x in first_cell for x in ('.SH', '.SZ', '.BJ')):
                # 首行已是带市场后缀的代码，直接取第一列
                raw_stocks = df.iloc[:, 0].dropna().tolist()
            else:
                # 首行是列名；将首行提升为表头，其余为数据
                df.columns = [str(c).strip() for c in df.iloc[0]]
                df = df.iloc[1:].reset_index(drop=True)
                code_col = next(
                    (c for c in df.columns if '代码' in c or 'code' in c.lower()),
                    None,
                )
                raw_stocks = (
                    df[code_col].dropna().tolist() if code_col
                    else df.iloc[:, 0].dropna().tolist()
                )
            for code in raw_stocks:
                stocks.append(self._normalize_stock_code_with_market(code))
        except Exception as e:
            self._append_log(f"[错误] 读取文件失败: {e}")
        return stocks

    def get_stock_list(self) -> List[str]:
        stocks = []
        if self.preset_radio.isChecked():
            legacy_dirs = [
                os.path.join(os.path.dirname(os.path.dirname(__file__)), 'stock_lists'),
                os.path.join(os.path.dirname(__file__), 'stock_lists'),
            ]
            preset_files = {
                'all_a':  '沪深A股_股票列表.csv',
                'sh_a':   '上证A股_股票列表.csv',
                'sz_a':   '深证A股_股票列表.csv',
                'hs300':  '沪深300成分股_股票列表.csv',
                'sz50':   '上证50成分股_股票列表.csv',
                'zz500':  '中证500成分股_股票列表.csv',
                'cyb':    '创业板_股票列表.csv',
                'kcb':    '科创板_股票列表.csv',
                'hs_etf': '沪深ETF_成分股列表.csv',
                'hs_fund': '沪深基金_列表.csv',
                'hs_convertible_bonds': '沪深转债_列表.csv',
                't0_etf': 'T0型ETF.csv',
                'common_index': '指数_股票列表.csv',
            }
            for key, cb in self.preset_checks.items():
                if not cb.isChecked() or key not in preset_files:
                    continue
                fname = preset_files[key]
                file_path = _resolve_preset_stock_pool_file(fname, legacy_dirs)
                if file_path:
                    stocks.extend(self._read_stock_file(file_path))

        elif self.file_radio.isChecked():
            fp = self.file_path_edit.text().strip()
            if fp and os.path.exists(fp):
                stocks = self._read_stock_file(fp)
            else:
                QMessageBox.warning(self, "错误", "请选择有效的股票列表文件")
                return []

        elif self.manual_radio.isChecked():
            text = self.manual_edit.toPlainText().strip()
            for line in text.split('\n'):
                code = line.strip()
                if code:
                    stocks.append(self._normalize_stock_code_with_market(code))

        return list(set(stocks))

    def get_periods_config(self) -> List[dict]:
        """构建传给 TushareImportThread 的 periods 列表。"""
        periods = []
        do_none  = self.adj_none_check.isChecked()
        do_front = self.adj_front_check.isChecked()
        do_back  = self.adj_back_check.isChecked()

        if self.period_1d_check.isChecked():
            periods.append({
                'period':     '1d',
                'start_date': self.period_1d_start.date().toString("yyyyMMdd"),
                'end_date':   self.period_1d_end.date().toString("yyyyMMdd"),
                'adj_none':   do_none,
                'adj_front':  do_front,
                'adj_back':   do_back,
            })
        if self.period_1m_check.isChecked():
            periods.append({
                'period':     '1m',
                'start_date': self.period_1m_start.date().toString("yyyy-MM-dd") + " 09:00:00",
                'end_date':   self.period_1m_end.date().toString("yyyy-MM-dd")   + " 15:00:00",
                'adj_none':   do_none,
                'adj_front':  do_front,
                'adj_back':   do_back,
            })
        if self.period_5m_check.isChecked():
            periods.append({
                'period':     '5m',
                'start_date': self.period_5m_start.date().toString("yyyy-MM-dd") + " 09:00:00",
                'end_date':   self.period_5m_end.date().toString("yyyy-MM-dd")   + " 15:00:00",
                'adj_none':   do_none,
                'adj_front':  do_front,
                'adj_back':   do_back,
            })
        return periods

    def _append_log(self, msg: str):
        _append_download_log(self.log_text, f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    # ------------------------------------------------------------------
    # 导入逻辑
    # ------------------------------------------------------------------

    def _read_token(self) -> str:
        """从 GUI/CLI 共享配置读取并解码 token。"""
        return load_tushare_settings().token

    def _read_proxy(self) -> Tuple[bool, str, str]:
        settings = load_tushare_settings()
        return settings.use_proxy, settings.proxy_url, settings.api_url

    def start_import(self):
        self._close_after_stop = False
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._import_start_time = datetime.now()
        self.import_eta_label.setText("预计剩余：--")

        # 读取 token
        token = self._read_token()
        if not token:
            QMessageBox.warning(self, "错误", "Token 未配置，请在【软件设置 → Tushare设置】中设置。")
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            return

        # 校验股票列表
        stock_list = self.get_stock_list()
        if not stock_list:
            QMessageBox.warning(self, "错误", "股票列表为空，请至少选择或输入一只股票。")
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            return

        # 校验周期
        periods = self.get_periods_config()
        if not periods:
            QMessageBox.warning(self, "错误", "请至少勾选一个数据周期。")
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            return

        date_pairs = []
        if self.period_1d_check.isChecked():
            date_pairs.append(("1d", self.period_1d_start.date(), self.period_1d_end.date()))
        if self.period_1m_check.isChecked():
            date_pairs.append(("1m", self.period_1m_start.date(), self.period_1m_end.date()))
        if self.period_5m_check.isChecked():
            date_pairs.append(("5m", self.period_5m_start.date(), self.period_5m_end.date()))
        invalid_periods = [name for name, start, end in date_pairs if start > end]
        if invalid_periods:
            QMessageBox.warning(
                self,
                "错误",
                f"以下周期的开始日期晚于结束日期：{', '.join(invalid_periods)}",
            )
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            return

        minute_periods = [
            item["period"] for item in periods if item["period"] in ("1m", "5m")
        ]
        if minute_periods:
            from .tushare_importer import is_tushare_index_code

            index_stocks = [code for code in stock_list if is_tushare_index_code(code)]
            if index_stocks:
                preview = "、".join(index_stocks[:5])
                if len(index_stocks) > 5:
                    preview += f" 等 {len(index_stocks)} 只"
                QMessageBox.warning(
                    self,
                    "指数不支持分钟线",
                    "Tushare 当前指数接口只支持日线，不能与 1m/5m 一起提交。\n"
                    f"检测到指数：{preview}\n\n"
                    "请取消分钟周期，或将指数与股票分成两次下载。",
                )
                self.start_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                return

        # 校验复权（三选一，至少要勾一种）
        if not any([
            self.adj_none_check.isChecked(),
            self.adj_front_check.isChecked(),
            self.adj_back_check.isChecked(),
        ]):
            QMessageBox.warning(self, "错误", "请至少勾选一种复权方式（不复权 / 前复权 / 后复权）。")
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            return

        use_proxy, proxy_url, api_url = self._read_proxy()
        data_root = getattr(self.manager, 'data_root', None) or ''

        self.log_text.clear()
        self.progress_bar.setValue(0)
        self._append_log(f"开始下载，股票 {len(stock_list)} 只，周期 {[p['period'] for p in periods]}")

        # 导入前先释放当前进程已持有的 DuckDB 连接，避免线程启动后写入时撞锁。
        try:
            if hasattr(self.manager, 'close_all_no_checkpoint'):
                self.manager.close_all_no_checkpoint()
            elif hasattr(self.manager, 'close_all'):
                self.manager.close_all()
        except Exception as e:
            self._append_log(f"[警告] 导入前释放数据库连接失败: {e}")

        from .tushare_import_worker import TushareImportThread
        self.import_thread = TushareImportThread(
            token=token,
            use_proxy=use_proxy,
            proxy_url=proxy_url,
            api_url=api_url,
            stock_list=stock_list,
            periods=periods,
            manager=self.manager,
            force_overwrite=self.force_overwrite_check.isChecked(),
            parent=self,
        )
        self.import_thread.progress.connect(self._on_progress)
        self.import_thread.status.connect(self.import_status_label.setText)
        self.import_thread.log.connect(self._append_log)
        self.import_thread.lock_conflict.connect(self._on_lock_conflict)
        self.import_thread.enable_lock_prompt()
        self.import_thread.finished.connect(self._on_finished)
        self.import_thread.start()

    def _on_lock_conflict(self, info: dict):
        """Tushare 自动重试耗尽后，在界面线程询问如何继续。"""
        thread = self.import_thread
        if thread is None:
            return

        stock = info.get("stock") or "元数据索引"
        period = info.get("period") or ""
        operation = info.get("operation") or "数据库操作"
        pid = info.get("pid")
        process = info.get("process") or "其他进程"
        path = info.get("db_path") or ""

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("数据库文件正在使用")
        target = f"{stock}（{period}）" if period else stock
        box.setText(f"{target}暂时无法完成{operation}")
        detail_lines = [
            "系统已自动重试 5 次，但文件仍被其他任务占用。",
            f"占用进程：{process}" + (f"（PID {pid}）" if pid else ""),
        ]
        if path:
            detail_lines.append(f"文件：{path}")
        if operation == "刷新元数据":
            detail_lines.append("跳过不会丢失已写入行情，可稍后通过扫描修复元数据。")
        else:
            detail_lines.append("跳过项会在本次任务结束总结中单独列出。")
        box.setInformativeText("\n".join(detail_lines))
        retry_button = box.addButton("继续重试", QMessageBox.AcceptRole)
        skip_button = box.addButton("先跳过", QMessageBox.ActionRole)
        abort_button = box.addButton("停止任务", QMessageBox.RejectRole)
        box.setDefaultButton(skip_button)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is retry_button:
            thread.set_lock_resolution("retry")
        elif clicked is abort_button:
            thread.set_lock_resolution("abort")
        else:
            thread.set_lock_resolution("skip")

    def is_import_running(self) -> bool:
        """返回后台下载线程是否仍在运行。"""
        try:
            return bool(self.import_thread and self.import_thread.isRunning())
        except RuntimeError:
            self.import_thread = None
            return False

    def request_close_after_stop(self):
        """请求线程停止，并在真正退出后自动关闭窗口。"""
        if not self.is_import_running():
            self.close()
            return

        self._close_after_stop = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.import_status_label.setText("正在停止下载，线程退出后自动关闭...")
        self.import_eta_label.setText("预计剩余：--")
        self._append_log("正在停止下载，等待当前请求结束后自动关闭窗口...")
        try:
            self.import_thread.stop()
        except Exception:
            pass
        self.hide()

    def stop_import(self):
        if self.import_thread and self.import_thread.isRunning():
            self.import_thread.stop()
            self._append_log("已发送停止信号，等待当前任务完成...")
            # 注意：start_btn 由 _on_finished 信号在线程真正结束时重新启用
            # 此处不立即启用，防止旧线程未退出时重复启动
        else:
            self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.import_eta_label.setText("预计剩余：--")

    def closeEvent(self, event):
        """关闭前检查线程状态，防止线程仍在运行时销毁对话框导致崩溃。"""
        if self.is_import_running():
            reply = QMessageBox.question(
                self, "确认关闭",
                "数据下载正在进行中，确定要关闭吗？\n"
                "系统会先停止领取新任务，等待当前请求、元数据刷新和数据库连接收尾后再关闭。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.request_close_after_stop()
                event.ignore()
            else:
                event.ignore()
        else:
            event.accept()

    def _on_progress(self, completed: int, total: int, requests: int = 0, recent_requests: int = 0):
        if not _should_update_download_ui(self, '_last_tushare_progress_ui_ts', completed, total):
            return
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(completed)
            # 更新 ETA 和完成比例
            if self._import_start_time:
                elapsed = (datetime.now() - self._import_start_time).total_seconds()
                
                # 如果运行时间不足1分钟，推算每分钟速率；否则直接使用最近一分钟的真实请求数
                if elapsed < 60 and elapsed > 0:
                    req_per_min = (requests / elapsed) * 60
                else:
                    req_per_min = recent_requests
                
                if completed > 0:
                    eta_sec = elapsed / completed * (total - completed)
                    mins, secs = divmod(int(eta_sec), 60)
                    if mins >= 60:
                        hours, mins = divmod(mins, 60)
                        eta_str = f"{hours}小时{mins}分{secs:02d}秒"
                    else:
                        eta_str = f"{mins}分{secs:02d}秒"
                        
                    self.import_eta_label.setText(f"进度：{completed}/{total} | 请求数：{requests} ({req_per_min:.1f}次/分) | 预计剩余：{eta_str}")
                else:
                    self.import_eta_label.setText(f"进度：0/{total} | 请求数：{requests} ({req_per_min:.1f}次/分) | 预计剩余：计算中...")

    def _on_finished(self, ok: bool, summary: str):
        thread = self.import_thread
        self.import_thread = None
        results = getattr(thread, "last_results", {}) if thread else {}

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.import_eta_label.setText("已完成")
        self._append_log(f"{'[完成]' if ok else '[部分失败]'} {summary}")
        lock_skipped = results.get("lock_skipped", [])
        if lock_skipped:
            self._append_log(f"[汇总] 数据库占用项: {len(lock_skipped)}")
            for item in lock_skipped:
                target = item.get("stock") or "metadata"
                period = item.get("period") or ""
                pid = item.get("pid") or "未知"
                scope = item.get("scope") or "task"
                self._append_log(
                    f"  - {target} {period}，PID {pid}，范围 {scope}"
                )
        errors = results.get("errors", [])
        if errors:
            self._append_log(f"[汇总] 错误/无数据项: {len(errors)}")
            for message in errors[:20]:
                self._append_log(f"  - {message}")
            if len(errors) > 20:
                self._append_log(f"  - 其余 {len(errors) - 20} 项请查看运行日志")
        adjustment_errors = results.get("adjustment_errors", [])
        if adjustment_errors:
            self._append_log(f"[汇总] 复权待补项: {len(adjustment_errors)}")
            for message in adjustment_errors[:20]:
                self._append_log(f"  - {message}")
        unresolved = results.get("unresolved", [])
        if unresolved:
            self._append_log(f"[汇总] 导入后仍待核验: {len(unresolved)} 个任务")
            for item in unresolved[:20]:
                raw_count = len(item.get("raw_dates") or [])
                adjustment_count = len(item.get("adjustment_dates") or [])
                self._append_log(
                    f"  - {item.get('stock', '')} {item.get('period', '')}: "
                    f"raw {raw_count} 日，复权 {adjustment_count} 日"
                )

        has_changes = bool(
            results.get("total_records", 0)
            or results.get("metadata_updated", 0)
        )
        if (
            has_changes
            and self.parent()
            and not (
                getattr(self.parent(), '_viewer_closing', False)
                or getattr(self.parent(), '_pending_close_after_tushare_stop', False)
            )
            and hasattr(self.parent(), "on_refresh_clicked")
        ):
            try:
                self.parent().on_refresh_clicked()
                self._append_log("股票列表已自动刷新")
            except Exception as exc:
                self._append_log(f"[警告] 自动刷新股票列表失败: {exc}")

        display_summary = summary
        if lock_skipped:
            preview = []
            for item in lock_skipped[:8]:
                target = item.get("stock") or "metadata"
                period = item.get("period") or ""
                preview.append(f"{target} {period}".strip())
            display_summary += "\n\n数据库占用跳过：\n" + "\n".join(f"• {x}" for x in preview)
            if len(lock_skipped) > 8:
                display_summary += f"\n• 其余 {len(lock_skipped) - 8} 项请查看日志"
        if self._close_after_stop:
            self._close_after_stop = False
            if thread:
                try:
                    thread.deleteLater()
                except Exception as e:
                    self._append_log(f"线程资源释放失败: {e}")
            QTimer.singleShot(0, self.close)
            return

        if ok:
            QMessageBox.information(self, "完成", display_summary)
        else:
            QMessageBox.warning(self, "部分失败", display_summary)

        if thread:
            try:
                thread.deleteLater()
            except Exception as e:
                self._append_log(f"线程资源释放失败: {e}")


def main():
    """主函数"""
    app = QApplication(sys.argv)

    # 设置样式
    app.setStyle('Fusion')

    # 未获焦点的下拉框/日期框不再吃滚轮，避免滚动对话框时静默改掉下载区间
    install_wheel_guard(app)

    # 获取命令行参数中的数据目录
    data_root = None
    if len(sys.argv) > 1:
        data_root = sys.argv[1]

    viewer = DuckDBViewer(data_root=data_root)
    viewer.show()

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
