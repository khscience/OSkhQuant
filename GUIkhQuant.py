import sys
import os
import math
from kh_stock_pools import DESKTOP_CODE_INDEX, desktop_pool_definitions
from khPathUtils import (
    get_custom_stock_pool_path,
    get_stock_pool_path,
    get_stock_pool_write_dir,
    is_frozen_runtime,
)
from kh_app_identity import (
    APP_NAME, QT_APP, QT_ORG, default_duckdb_dir, documents_dir, local_appdata_dir,
)

WINDOW_TITLE = "看海量化回测系统（开源版）"

if __name__ == "__main__" and sys.platform != "win32":
    print("看海量化回测平台（开源版）只支持 Windows 10/11 64 位。")
    sys.exit(2)

_IS_FROZEN_RUNTIME = is_frozen_runtime()
if _IS_FROZEN_RUNTIME:
    os.environ.setdefault("KHQUANT_PACKAGED", "1")

# ── 可复现性：固定哈希种子(与 kh.py 一致)──────────────────────────────
# GUI 在同进程的 QThread 内直接运行 KhQuantFramework(非子进程)，故 GUI
# 进程本身需固定 PYTHONHASHSEED，否则策略中 set 迭代顺序随机 → 回测结果
# 每次不可复现。须早于 multiprocessing 导入；multiprocessing 子进程会继承
# 本环境变量(=0)从而跳过重启，不影响 freeze_support 的打包子进程逻辑。
# 使用子进程并等待，而不是 os.execv，保证父进程能拿到子进程的退出码。
if os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    try:
        import subprocess
        _argv = sys.argv[1:] if _IS_FROZEN_RUNTIME else sys.argv
        sys.exit(subprocess.call([sys.executable] + _argv))
    except SystemExit:
        raise
    except Exception:
        # 极少数环境无法重启时继续运行，退化为本进程哈希种子未固定，
        # 但不能阻断 GUI 启动。
        pass

import multiprocessing

# 打包(PyInstaller)后，multiprocessing 以 spawn 方式启动的子进程（含资源跟踪进程）会重新执行本入口文件。
# 必须在任何重型导入和 multiprocessing 资源分配之前调用 freeze_support()，否则打包版的子进程会重新跑到
# main() 反复弹出主界面（从 baostock 补充数据时会按工作进程数额外弹出多个主界面）。
# 源码直接运行时该调用为 no-op。
multiprocessing.freeze_support()

# 桌面主窗口单实例。固定哈希中继父进程尚未走到这里，multiprocessing 子进程
# 的 __name__ 也不是 __main__，不会抢占锁。必须早于日志初始化。
_desktop_instance_lock = None
if __name__ == "__main__":
    from kh_single_instance import DesktopSingleInstanceLock, notify_already_running

    _desktop_instance_lock = DesktopSingleInstanceLock()
    if not _desktop_instance_lock.acquire():
        notify_already_running()
        raise SystemExit(0)

import logging
from logging.handlers import RotatingFileHandler
import psutil
import time
import traceback
import json
import csv
import subprocess
import shutil
import copy
import threading
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
    QObject,
    QSize,
)
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
                           QTableWidget, QTableWidgetItem, QMenu, QAction, QFileDialog, QMessageBox, QSplitter,
                           QTabWidget, QTextEdit, QComboBox, QGroupBox, QLineEdit, QDateEdit, QCheckBox, QProgressDialog,
                           QSizePolicy, QScrollArea, QTreeWidget, QTreeWidgetItem, QStackedWidget, QDialog, QListWidget,
                           QListWidgetItem, QSlider, QFrame, QToolBar, QButtonGroup, QRadioButton, QSpinBox, QDoubleSpinBox,
                           QCalendarWidget, QTimeEdit, QFormLayout, QSpacerItem, QGridLayout, QStatusBar, QInputDialog,
                           QHeaderView, QStyleFactory, QGraphicsDropShadowEffect, QProgressBar, QSplashScreen, QToolButton,
                           QDesktopWidget, QDialogButtonBox)
from PyQt5.QtGui import QIcon, QCursor, QFont, QColor, QPainter, QPen, QBrush, QPixmap, QTextCursor, QPalette, QDoubleValidator, QIntValidator, QDesktopServices


def _dispatch_duckdb_viewer_destroyed(owner, target):
    """主窗口也可能已进入 Qt 销毁阶段，先检查再获取其绑定方法。"""
    try:
        from PyQt5 import sip
        if sip.isdeleted(owner):
            return
    except (TypeError, RuntimeError):
        pass
    KhQuantGUI._on_duckdb_viewer_destroyed(owner, target)


try:
    from BacktestHistoryManager import BacktestHistoryManager  # 回测历史管理模块
except ImportError:
    logging.error("无法导入回测历史管理模块")
    BacktestHistoryManager = None

# 导入其他必要的模块
try:
    from khFrame import KhQuantFramework, MyTraderCallback
    from khQTTools import get_stock_names
except ImportError as e:
    logging.error(f"导入必要模块失败: {str(e)}")

from SettingsDialog import SettingsDialog
from qt_settings_bridge import KhQtSettings
from backtest_runtime_config import (
    apply_system_runtime_settings,
    build_headless_settings,
    is_memory_error,
    preserve_strategy_runtime_blocks,
    stamp_memory_decision,
    strip_runtime_config,
)
from data_integrity_policy import should_run_integrity_check
from update_manager import UpdateManager  # 导入UpdateManager类
from version import get_version_info  # 导入版本信息
from khPathUtils import resolve_strategy_file, strategy_file_for_config
from khUiScale import (
    get_ui_font_scale,
    apply_app_font,
    install_wheel_guard,
    force_primary_screen_dpi,
    get_platform_ui_metrics,
    get_adaptive_window_size,
    get_preferred_ui_font_family,
    get_preferred_mono_font_family,
)


def get_logs_dir():
    """获取日志目录：%LOCALAPPDATA%\\KhQuantOS\\logs，不可写时退到临时目录。"""
    possible_dirs = [
        local_appdata_dir('logs'),
        os.path.join(os.environ.get('TEMP', '/tmp'), 'KhQuantOS', 'logs'),
    ]

    for logs_dir in possible_dirs:
        try:
            os.makedirs(logs_dir, exist_ok=True)
            test_file = os.path.join(logs_dir, 'test_write.tmp')
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            return logs_dir
        except (OSError, PermissionError):
            continue
    
    # 如果所有目录都失败，使用临时目录
    import tempfile
    logs_dir = os.path.join(tempfile.gettempdir(), 'KhQuantOS_logs')
    try:
        os.makedirs(logs_dir, exist_ok=True)
        print(f"使用临时日志目录: {logs_dir}")
        return logs_dir
    except Exception as e:
        print(f"创建临时日志目录失败: {e}")
        return tempfile.gettempdir()


LOGS_DIR = get_logs_dir()

# 配置日志，添加异常处理
try:
    _startup_process_name = multiprocessing.current_process().name
    from kh_startup_logging import select_startup_log_name
    _startup_log_name = select_startup_log_name(
        _startup_process_name,
        sys.argv[1:],
    )
    if _startup_log_name:
        _startup_file_handler = RotatingFileHandler(
            os.path.join(LOGS_DIR, _startup_log_name),
            mode='a',
            maxBytes=20 * 1024 * 1024,
            backupCount=5,
            encoding='utf-8',
        )
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[_startup_file_handler],
            force=True
        )
        print(f"日志文件配置成功: {os.path.join(LOGS_DIR, _startup_log_name)}")
    else:
        # multiprocessing 子进程不与桌面主进程共同打开/轮转固定日志。
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            force=True,
        )
except Exception as e:
    # 如果文件日志配置失败，只使用控制台日志
    print(f"配置文件日志失败: {e}")
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        force=True
    )

# 添加控制台日志处理器（仅在标准错误可用时）
_stderr_stream = getattr(sys, 'stderr', None)
if _stderr_stream and hasattr(_stderr_stream, 'write'):
    console_handler = logging.StreamHandler(_stderr_stream)
    console_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(formatter)
    logging.getLogger('').addHandler(console_handler)
logging.getLogger('').setLevel(logging.INFO)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('requests').setLevel(logging.WARNING)
logging.info("=" * 72)
logging.info(
    "[SESSION] 看海量化进程启动 pid=%s parent_pid=%s runtime=%s",
    os.getpid(),
    os.getppid(),
    "packaged" if _IS_FROZEN_RUNTIME else "source",
)
if _desktop_instance_lock is not None and _desktop_instance_lock.error:
    logging.warning("跨进程单实例锁启用失败，已安全放行: %s", _desktop_instance_lock.error)


_SLIPPAGE_LABEL_TO_TYPE = {
    "按最小变动价跳数": "tick",
    "按成交金额比例": "ratio",
}
_SLIPPAGE_TYPE_TO_LABEL = {value: key for key, value in _SLIPPAGE_LABEL_TO_TYPE.items()}
_SLIPPAGE_UI_DEFAULTS = {"tick": "2", "ratio": "0.1"}
_TRADE_COST_ENGINE_DEFAULTS = {
    "min_commission": 5.0,
    "commission_rate": 0.0003,
    "stamp_tax_rate": 0.001,
    "flow_fee": 0.1,
}


def _normalize_slippage_ui_value(slippage_type, value):
    """把滑点输入规范为界面文本；两种单位之间不做隐式换算。"""
    normalized_type = "tick" if slippage_type == "tick" else "ratio"
    default_value = _SLIPPAGE_UI_DEFAULTS[normalized_type]
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return default_value
    if not math.isfinite(numeric_value):
        return default_value
    if normalized_type == "tick":
        if numeric_value < 0 or numeric_value > 100 or not numeric_value.is_integer():
            return default_value
        return str(int(numeric_value))
    if numeric_value < 0 or numeric_value > 10:
        return default_value
    return format(numeric_value, ".12g")


def _normalize_slippage_config(slippage):
    """把任意 .kh 滑点配置收敛为撮合引擎可安全读取的规范结构。"""
    raw = slippage if isinstance(slippage, dict) else {}
    slippage_type = raw.get("type", "ratio")
    if slippage_type not in {"tick", "ratio"}:
        slippage_type = "ratio"
    tick_text = _normalize_slippage_ui_value(
        "tick",
        raw.get("tick_count", _SLIPPAGE_UI_DEFAULTS["tick"]),
    )
    try:
        ratio_percent = float(raw.get("ratio", 0.001)) * 100
    except (TypeError, ValueError):
        ratio_percent = _SLIPPAGE_UI_DEFAULTS["ratio"]
    ratio_text = _normalize_slippage_ui_value("ratio", ratio_percent)
    try:
        tick_size = float(raw.get("tick_size", 0.01))
    except (TypeError, ValueError):
        tick_size = 0.01
    if not math.isfinite(tick_size) or tick_size <= 0 or tick_size > 100:
        tick_size = 0.01
    return {
        "type": slippage_type,
        "tick_size": tick_size,
        "tick_count": int(tick_text),
        # 配置保存小数；界面输入的是百分比。
        "ratio": float(ratio_text) / 100,
    }


def _normalize_trade_cost_config(trade_cost):
    """保留扩展费用字段，并把原始数值收敛到 GUI/撮合都可安全读取的范围。"""
    raw = trade_cost if isinstance(trade_cost, dict) else {}
    normalized = copy.deepcopy(raw)
    for key, default, lower, upper in (
        ("min_commission", _TRADE_COST_ENGINE_DEFAULTS["min_commission"], 0.0, None),
        ("commission_rate", _TRADE_COST_ENGINE_DEFAULTS["commission_rate"], 0.0, 1.0),
        ("stamp_tax_rate", _TRADE_COST_ENGINE_DEFAULTS["stamp_tax_rate"], 0.0, 1.0),
        ("flow_fee", _TRADE_COST_ENGINE_DEFAULTS["flow_fee"], 0.0, 100.0),
    ):
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        if (
            not math.isfinite(value)
            or value < lower
            or (upper is not None and value > upper)
        ):
            value = default
        normalized[key] = value
    normalized["slippage"] = _normalize_slippage_config(raw.get("slippage", {}))
    return normalized


def _merge_trade_cost_config(base_trade_cost, ui_trade_cost):
    """用界面可编辑字段覆盖费用配置，同时保留界面未知的扩展语义。"""
    merged = copy.deepcopy(base_trade_cost) if isinstance(base_trade_cost, dict) else {}
    if isinstance(ui_trade_cost, dict):
        merged.update(copy.deepcopy(ui_trade_cost))
    return _normalize_trade_cost_config(merged)

# 定义StockAccount类


class IntegrityCheckThread(QThread):
    """数据完整性检查线程"""
    # 定义信号
    progress_signal = pyqtSignal(int, int, str, int)  # 进度信号(current, total, message, task_count)
    finished_signal = pyqtSignal(dict)  # 完成信号，传递检查结果
    error_signal = pyqtSignal(str)  # 错误信号

    def __init__(self, stock_list, periods, start_date, end_date, duckdb_data_path, stock_periods=None, dividend_type='none'):
        super().__init__()
        self.stock_list = stock_list
        self.periods = periods
        self.start_date = start_date
        self.end_date = end_date
        self.duckdb_data_path = duckdb_data_path
        self.stock_periods = stock_periods
        self.dividend_type = dividend_type
        self._is_running = True

    def stop(self):
        """停止检查"""
        self._is_running = False

    def _stop_flag(self):
        """停止标志回调函数"""
        return not self._is_running

    def _progress_callback(self, current, total, message, task_count):
        """进度回调函数"""
        self.progress_signal.emit(current, total, message, task_count)

    def run(self):
        """线程运行函数"""
        try:
            import khQTTools

            # 调用检查函数
            result = khQTTools.check_duckdb_data_integrity(
                stock_list=self.stock_list,
                periods=self.periods,
                start_date=self.start_date,
                end_date=self.end_date,
                duckdb_data_path=self.duckdb_data_path,
                progress_callback=self._progress_callback,
                stop_flag=self._stop_flag,
                stock_periods=self.stock_periods,
                dividend_type=self.dividend_type
            )

            self.finished_signal.emit(result)

        except Exception as e:
            import traceback
            error_msg = f"数据完整性检查时发生异常:\n{str(e)}\n{traceback.format_exc()}"
            self.error_signal.emit(error_msg)


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
        self.temp_config_paths = [config_path] if config_path else []
        self._is_running = True

    def run(self):
        """线程运行函数"""
        try:
            # 读取UI设置，传递给回测框架
            from PyQt5.QtCore import QMetaObject, Qt, Q_ARG
            from PyQt5.QtWidgets import QMessageBox
            settings = KhQtSettings(QT_ORG, QT_APP)
            gui = self.trader_callback.gui if self.trader_callback and hasattr(self.trader_callback, 'gui') else None
            duckdb_data_path = gui._ensure_duckdb_data_path() if gui else settings.value('duckdb_data_path', '')
            
            # 定义弹窗回调
            def ui_confirm_callback(title: str, message: str) -> bool:
                if self.trader_callback and hasattr(self.trader_callback, 'gui'):
                    user_choice = [None]
                    def show_dialog():
                        msg_box = QMessageBox(self.trader_callback.gui)
                        msg_box.setIcon(QMessageBox.Warning)
                        msg_box.setWindowTitle(title)
                        msg_box.setText(message)
                        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
                        msg_box.setDefaultButton(QMessageBox.No)
                        
                        yes_button = msg_box.button(QMessageBox.Yes)
                        no_button = msg_box.button(QMessageBox.No)
                        yes_button.setText("继续运行")
                        no_button.setText("停止运行")
                        
                        user_choice[0] = msg_box.exec_()
                        
                    QMetaObject.invokeMethod(
                        self.trader_callback.gui,
                        "invoke",
                        Qt.BlockingQueuedConnection,
                        Q_ARG("PyQt_PyObject", show_dialog)
                    )
                    return user_choice[0] == QMessageBox.Yes
                return False

            def duckdb_lock_decision_callback(info: dict) -> str:
                """回测读取被写任务占用时，在GUI主线程询问用户。"""
                if not (self.trader_callback and hasattr(self.trader_callback, 'gui')):
                    return "skip"
                decision = ["skip"]

                def show_dialog():
                    stock = info.get("stock") or "未知证券"
                    period = info.get("period") or "未知周期"
                    pid = info.get("pid")
                    process = info.get("process") or "其他进程"
                    path = info.get("db_path") or ""
                    box = QMessageBox(self.trader_callback.gui)
                    box.setIcon(QMessageBox.Warning)
                    box.setWindowTitle("回测数据文件正在使用")
                    box.setText(f"{stock}（{period}）暂时无法读取")
                    details = [
                        "系统已自动重试 5 次，文件仍可能正在写入。",
                        f"占用进程：{process}" + (f"（PID {pid}）" if pid else ""),
                    ]
                    if path:
                        details.append(f"文件：{path}")
                    details.append("若选择跳过，回测结果会明确记录该证券未参与计算。")
                    box.setInformativeText("\n".join(details))
                    retry_button = box.addButton("继续重试", QMessageBox.AcceptRole)
                    skip_button = box.addButton("先跳过", QMessageBox.ActionRole)
                    abort_button = box.addButton("停止回测", QMessageBox.RejectRole)
                    box.setDefaultButton(skip_button)
                    box.exec_()
                    clicked = box.clickedButton()
                    if clicked is retry_button:
                        decision[0] = "retry"
                    elif clicked is abort_button:
                        decision[0] = "abort"
                    else:
                        decision[0] = "skip"

                QMetaObject.invokeMethod(
                    self.trader_callback.gui,
                    "invoke",
                    Qt.BlockingQueuedConnection,
                    Q_ARG("PyQt_PyObject", show_dialog),
                )
                return decision[0]

            # 定义显示结果回调
            def show_result_callback(backtest_dir: str):
                if self.trader_callback and hasattr(self.trader_callback, 'gui'):
                    if hasattr(self.trader_callback.gui, 'show_backtest_result_signal'):
                        self.trader_callback.gui.show_backtest_result_signal.emit(backtest_dir)
                    else:
                        # 兼容直接调用
                        QMetaObject.invokeMethod(
                            self.trader_callback.gui,
                            "show_backtest_result",
                            Qt.QueuedConnection,
                            Q_ARG(str, backtest_dir)
                        )

            def set_t0_mode_display_callback(enabled: bool):
                if self.trader_callback and hasattr(self.trader_callback, 'gui'):
                    self.trader_callback.gui.set_t0_mode_display(enabled)

            def show_t0_warning_callback(msg: str):
                if self.trader_callback and hasattr(self.trader_callback, 'gui'):
                    self.trader_callback.gui.show_t0_warning(msg, self.strategy_file)

            def set_progress_label_callback(label: str):
                if self.trader_callback and hasattr(self.trader_callback, 'gui'):
                    self.trader_callback.gui.set_progress_label(label)

            last_progress_emit = {'value': None, 'ts': 0.0}

            def update_progress_callback(value: int):
                if self.trader_callback and hasattr(self.trader_callback, 'gui'):
                    try:
                        progress_value = max(0, min(int(value), 100))
                    except Exception:
                        return

                    now_ts = time.time()
                    last_value = last_progress_emit['value']
                    last_ts = last_progress_emit['ts']
                    should_emit = (
                        last_value is None
                        or progress_value != last_value
                        or now_ts - last_ts >= 1.0
                    )
                    if not should_emit:
                        return

                    last_progress_emit['value'] = progress_value
                    last_progress_emit['ts'] = now_ts
                    self.trader_callback.gui.progress_signal.emit(progress_value)
            
            settings_cfg = settings.load()
            ui_settings = build_headless_settings(
                settings_cfg=settings_cfg,
                duckdb_data_path=duckdb_data_path,
                include_callbacks={
                    'confirm_callback': ui_confirm_callback,
                    'duckdb_lock_decision_callback': duckdb_lock_decision_callback,
                    'show_backtest_result_callback': show_result_callback,
                    'set_t0_mode_display_callback': set_t0_mode_display_callback,
                    'show_t0_warning_callback': show_t0_warning_callback,
                    'set_progress_label_callback': set_progress_label_callback,
                    'update_progress_callback': update_progress_callback,
                },
                include_runtime_settings={
                    'init_data_enabled': False,
                    'khhistory_missing_data_prompt': settings_cfg.get('performance_khhistory_missing_data_prompt', True),
                },
            )

            # 创建框架实例
            self.framework = KhQuantFramework(
                self.config_path,
                self.strategy_file,
                trader_callback=self.trader_callback,
                ui_settings=ui_settings
            )

            # 发送状态信号
            self.status_signal.emit("框架实例创建成功")

            # 运行策略；若遇到内存异常则自动降档后重跑，和 CLI 保持一致。
            current_config_path = self.config_path
            while True:
                try:
                    self.framework.run()
                    break
                except Exception as run_exc:
                    from performance_config import next_lower_memory_profile
                    current_perf = getattr(self.framework.config, "config_dict", {}).get("performance", {}) if self.framework else {}
                    current_profile = current_perf.get("memory_profile_effective", current_perf.get("memory_profile", "standard"))
                    next_profile = next_lower_memory_profile(current_profile) if is_memory_error(run_exc) else None
                    if not next_profile:
                        raise
                    retry_config = apply_system_runtime_settings(
                        getattr(self.framework.config, "config_dict", {}) or {},
                        settings.load(),
                        force_performance_overrides={
                            "memory_profile": next_profile,
                            "memory_profile_retry_from": current_profile,
                            "memory_profile_retry_reason": f"{run_exc.__class__.__name__}: {str(run_exc)[:300]}",
                        },
                    )
                    retry_config, _, _ = stamp_memory_decision(retry_config, config_path=current_config_path)
                    current_config_path = os.path.join(
                        os.path.dirname(current_config_path),
                        f"_tmp_gui_retry_{next_profile}_{os.path.basename(current_config_path)}",
                    )
                    with open(current_config_path, "w", encoding="utf-8") as f:
                        json.dump(retry_config, f, ensure_ascii=False, indent=2)
                    self.temp_config_paths.append(current_config_path)
                    self.framework = KhQuantFramework(
                        current_config_path,
                        self.strategy_file,
                        trader_callback=self.trader_callback,
                        ui_settings=ui_settings,
                    )

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
        self.setLevel(logging.INFO)

    # 数据管理模块（duckdb_storage 包，含各数据源导入器）拥有独立的日志窗口，
    # 其日志不应混入主界面的运行日志面板，这里按来源文件过滤掉
    _SUPPRESSED_LOG_DIRS = ("duckdb_storage",)

    def emit(self, record):
        try:
            if record.levelno < logging.INFO:
                return

            # 过滤数据管理模块的日志，使其只显示在 DuckDBViewer 自己的日志窗口
            pathname = (getattr(record, "pathname", "") or "").replace("\\", "/")
            path_parts = pathname.split("/")
            if any(d in path_parts for d in self._SUPPRESSED_LOG_DIRS):
                return

            msg = self.format(record)
            level = record.levelname

            # 拦截 [TRADE] 标签，将其转换为 TRADE 级别的日志
            if "[TRADE]" in msg:
                level = "TRADE"
                msg = msg.replace("[TRADE]", "").strip()
                
            # 使用Qt的信号槽机制来更新GUI
            self.gui.log_signal.emit(msg, level)
        except Exception:
            self.handleError(record)

class KhQuantGUI(QMainWindow):
    # 添加Qt信号
    log_signal = pyqtSignal(str, str)
    update_status_signal = pyqtSignal(str, str)
    
    # 类级别的实例计数器，用于追踪是否有多个实例被创建
    _instance_count = 0
    _init_lock = False
    show_backtest_result_signal = pyqtSignal(str)  # 添加新信号
    progress_signal = pyqtSignal(int)  # 添加进度条信号
    CSV_HEADER_CODE = "股票代码"
    CSV_HEADER_NAME = "股票名称"
    CSV_HEADER_CODE_EN = "code"
    CSV_HEADER_NAME_EN = "name"
    CSV_HEADER_KEYWORDS = [
        CSV_HEADER_CODE,
        CSV_HEADER_NAME,
        CSV_HEADER_CODE_EN,
        CSV_HEADER_NAME_EN,
        "",
    ]
    
    def __init__(self):
        # 更新实例计数器
        import threading
        KhQuantGUI._instance_count += 1
        logging.info(f"[INSTANCE] KhQuantGUI.__init__ 被调用，当前实例数: {KhQuantGUI._instance_count}")
        
        # 初始化锁，防止重复初始化
        if KhQuantGUI._init_lock:
            logging.warning("[INSTANCE] 检测到重复初始化尝试！")
            raise RuntimeError("KhQuantGUI 已经初始化，不允许重复创建")
        KhQuantGUI._init_lock = True
        
        super().__init__()
        
        # 记录程序启动时间
        self.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info(f"[INSTANCE] KhQuantGUI 实例创建中，当前实例数: {KhQuantGUI._instance_count}")
        
        # 初始化设置
        self.settings = KhQtSettings(QT_ORG, QT_APP)
        self._ensure_duckdb_data_path()
        
        # 初始化延迟日志显示相关属性（需要在早期初始化，避免AttributeError）
        self.delay_log_display = self.settings.value('delay_log_display', True, type=bool)
        self.delayed_logs = []
        self.strategy_is_running = False
        self._strategy_stop_requested = False
        self._strategy_stop_exit_immediately = True
        self._strategy_stop_warning_shown = False
        self._close_after_strategy_stop = False
        self._force_close_after_strategy_stop = False
        self.max_log_lines = self.settings.value('max_log_lines', 1000, type=int)  # 最大日志显示行数
        self._pending_log_html = []
        self._log_flush_scheduled = False
        self._log_display_batch_size = 200
        self._log_display_interval_ms = 50
        self._last_progress_ui_update_ts = 0.0
        self._last_progress_ui_value = None
        self._last_progress_ui_label = None
        self._progress_update_min_interval = 0.2
        self._last_status_table_resize_ts = 0.0

        # 检测屏幕分辨率并设置字体缩放
        self.font_scale = self.detect_screen_resolution()
        self.ui_metrics = get_platform_ui_metrics(self.font_scale)
        
        # 设置应用样式表
        self.setStyleSheet(self.get_scaled_stylesheet())
        
        # 初始化属性
        self.config = {}
        self._loaded_config_snapshot = {}
        self._ui_loaded_state_snapshot = None
        self.trader = None
        self.trader_callback = None
        self.stock_data_manager = None
        self.log_handler = None
        
        # 日志过滤器设置
        self.filter_log_levels = {"INFO": True, "DEBUG": True, "WARNING": True, "ERROR": True}
        self.current_config_file = None  # 当前加载的配置文件路径
        
        # 记录用户明确删除的股票（用于防止从股票池中重新添加）
        self.deleted_stocks = set()
        
        # 记录用户选择"不再提醒"T+0混合池警告的策略文件路径集合
        self._t0_warning_suppressed = set()
        
        # 设置窗口属性
        self.setWindowTitle(WINDOW_TITLE)
        # 设置窗口图标
        self.setWindowIcon(QIcon(self.get_icon_path("stock_icon.ico")))
        
        # 边框样式已在init_ui中设置，这里不再重复设置
        
        # 初始化更新管理器
        self.initialize_update_manager()
        
        # 初始化UI组件
        self.init_ui()
        
        # 初始化配置
        self.init_config()

        # 自动加载上次的配置文件
        self.auto_load_last_config()

        # 记录日志
        logging.info("GUI初始化完成")
        
        # 初始化属性
        self.strategy_thread = None
        
        # 日志存储
        self.log_entries = []

        # 连接信号到槽（使用QueuedConnection确保跨线程调用不阻塞）
        self.log_signal.connect(self._log_message, Qt.QueuedConnection)
        self.update_status_signal.connect(self._update_status_table, Qt.QueuedConnection)
        self.show_backtest_result_signal.connect(self.show_backtest_result, Qt.QueuedConnection)
        self.progress_signal.connect(self.update_progress_bar, Qt.QueuedConnection)
        
        # 设置定时器定期刷新日志缓冲区
        self.log_flush_timer = QTimer()
        self.log_flush_timer.timeout.connect(self.flush_logs)
        self.log_flush_timer.start(5000)  # 每5秒刷新一次日志
        
        # 记录启动信息到日志
        logging.info(f"软件启动时间: {self.start_time}")
        logging.info(f"当前版本: {get_version_info()['version']}")
        logging.info(f"日志文件路径: {os.path.join(LOGS_DIR, 'app.log')}")
        runtime_mode = "打包模式" if is_frozen_runtime() else "源码模式"
        logging.info(f"程序运行环境: {runtime_mode}")
        
        # 最后确保窗口在主屏幕居中显示（放在初始化的最末尾）
        self.center_window()
        self.show()

        # 初始化数据管理窗口实例变量
        self.duckdb_viewer_window = None
        self._close_after_duckdb_viewer = False
        
        logging.info(f"[INSTANCE] KhQuantGUI 实例初始化完成，当前实例数: {KhQuantGUI._instance_count}")

    def get_icon_path(self, icon_name):
        """获取图标文件的正确路径"""
        return os.path.join(os.path.dirname(__file__), 'icons', icon_name)
    
    def get_data_path(self, filename):
        """获取数据文件的正确路径"""
        return get_stock_pool_path(filename)

    @classmethod
    def _is_csv_header_row(cls, stock_code, stock_name=""):
        header_keywords = {keyword.lower() for keyword in cls.CSV_HEADER_KEYWORDS}
        clean_code = stock_code.strip().replace('\ufeff', '').lower()
        clean_name = stock_name.strip().lower()
        return clean_code in header_keywords or clean_name in header_keywords

    def _read_stock_rows_from_file(self, file_path, require_name=False):
        """读取股票清单文件，返回去重后的 (code, name) 列表。"""
        stock_rows = []
        seen_codes = set()

        with open(file_path, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.reader(f)
            for parts in reader:
                if not parts:
                    continue

                code = parts[0].strip().replace('\ufeff', '')
                name = parts[1].strip() if len(parts) > 1 else ""
                if not code or (require_name and not name):
                    continue
                if self._is_csv_header_row(code, name):
                    continue
                if code in seen_codes:
                    continue

                seen_codes.add(code)
                stock_rows.append((code, name))

        return stock_rows

    def _get_stock_table_codes(self):
        """获取当前股票表格中的代码集合，用于大股票池快速去重。"""
        codes = set()
        for row in range(self.stock_list.rowCount()):
            item = self.stock_list.item(row, 0)
            if item:
                code = item.text().strip()
                if code:
                    codes.add(code)
        return codes

    def _set_stock_list_rows(self, stock_rows):
        """批量刷新股票表格，避免大股票池逐行 insertRow 造成界面卡顿。"""
        table = self.stock_list
        sorting_enabled = table.isSortingEnabled()

        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        try:
            if sorting_enabled:
                table.setSortingEnabled(False)

            table.clearContents()
            table.setRowCount(len(stock_rows))
            for row, (code, name) in enumerate(stock_rows):
                table.setItem(row, 0, QTableWidgetItem(code))
                table.setItem(row, 1, QTableWidgetItem(name))
        finally:
            if sorting_enabled:
                table.setSortingEnabled(True)
            table.blockSignals(False)
            table.setUpdatesEnabled(True)
            table.viewport().update()

    def _append_stock_list_rows(self, stock_rows):
        """批量追加股票行，返回实际新增数量。"""
        if not stock_rows:
            return 0

        table = self.stock_list
        existing_codes = self._get_stock_table_codes()
        rows_to_add = []
        for code, name in stock_rows:
            if code in existing_codes:
                continue
            existing_codes.add(code)
            rows_to_add.append((code, name))

        if not rows_to_add:
            return 0

        sorting_enabled = table.isSortingEnabled()
        start_row = table.rowCount()

        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        try:
            if sorting_enabled:
                table.setSortingEnabled(False)

            table.setRowCount(start_row + len(rows_to_add))
            for offset, (code, name) in enumerate(rows_to_add):
                row = start_row + offset
                table.setItem(row, 0, QTableWidgetItem(code))
                table.setItem(row, 1, QTableWidgetItem(name))
        finally:
            if sorting_enabled:
                table.setSortingEnabled(True)
            table.blockSignals(False)
            table.setUpdatesEnabled(True)
            table.viewport().update()

        return len(rows_to_add)

    def detect_screen_resolution(self):
        """检测屏幕分辨率并返回字体缩放比例"""
        return get_ui_font_scale(self.settings)

    def apply_ui_scale(self, scale=None):
        """应用界面字号倍率到当前窗口"""
        if scale is None:
            scale = get_ui_font_scale(self.settings)
        self.font_scale = scale
        self.ui_metrics = get_platform_ui_metrics(scale)
        self.setUpdatesEnabled(False)
        self.setStyleSheet(self.get_scaled_stylesheet())
        if hasattr(self, 'log_text'):
            self.apply_log_text_style()
        toolbar = self.findChild(QToolBar, "mainToolBar")
        if toolbar:
            self._apply_toolbar_style(toolbar)
            self.set_button_colors()
            self._apply_extra_btn_styles()
        layout = self.layout()
        if layout:
            layout.invalidate()
            layout.activate()
        central = self.centralWidget()
        if central:
            central.updateGeometry()
            central.update()
        try:
            self.style().unpolish(self)
            self.style().polish(self)
        except Exception:
            pass
        self.updateGeometry()
        self.repaint()
        self.setUpdatesEnabled(True)
        self.update()

    def _get_log_font_size(self):
        """获取系统日志字号"""
        return max(10, int(14 * self.font_scale))

    def apply_log_text_style(self):
        """应用系统日志样式"""
        if not hasattr(self, 'log_text'):
            return
        font_size = self._get_log_font_size()
        mono_font_family = get_preferred_mono_font_family() or "Consolas"
        self.log_text.setStyleSheet(f"""
            QTextEdit {{
                background-color: #2b2b2b;
                color: #e8e8e8;
                border: 1px solid #404040;
                border-radius: 4px;
                padding: 5px;
                font-family: "{mono_font_family}", "Microsoft YaHei", monospace;
                font-size: {font_size}px;
            }}
            QTextEdit:focus {{
                border: 1px solid #666666;
            }}
        """)

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
        
        mono_font_family = get_preferred_mono_font_family() or "Consolas"
        ui_font_family = get_preferred_ui_font_family() or "Microsoft YaHei UI"
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
                padding: {self.ui_metrics.get('toolbar_button_padding_y', 8)}px {self.ui_metrics.get('toolbar_button_padding_x', 16)}px;
                color: #ffffff;
                min-width: 80px;
                font-family: "{ui_font_family}";
                font-weight: normal;
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
                font-family: "{mono_font_family}", "Microsoft YaHei", monospace;
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
        tb = getattr(error, "__traceback__", None)
        if tb:
            detail = "".join(traceback.format_exception(type(error), error, tb))
        else:
            detail = traceback.format_exc()
        self.log_message(f"错误详情:\n{detail}", "ERROR")

    def _install_gui_log_handler(self):
        """安装主界面日志处理器，避免重复注册到root logger。"""
        try:
            root_logger = logging.getLogger('')
            for handler in list(root_logger.handlers):
                if isinstance(handler, GUILogHandler) and getattr(handler, 'gui', None) is self:
                    root_logger.removeHandler(handler)
                    try:
                        handler.close()
                    except Exception:
                        pass

            self.log_handler = GUILogHandler(self)
            self.log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
            root_logger.addHandler(self.log_handler)
        except Exception as e:
            logging.warning(f"安装GUI日志处理器失败: {e}")

    def _remove_gui_log_handler(self):
        """从root logger移除主界面日志处理器，避免关闭后残留窗口引用。"""
        try:
            handler = getattr(self, 'log_handler', None)
            if handler is None:
                return

            root_logger = logging.getLogger('')
            if handler in root_logger.handlers:
                root_logger.removeHandler(handler)
            try:
                handler.close()
            finally:
                self.log_handler = None
        except Exception as e:
            logging.warning(f"移除GUI日志处理器失败: {e}")
    
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
        desktop = QDesktopWidget()
        primary_screen = desktop.primaryScreen()
        screen = desktop.availableGeometry(primary_screen)
        width, height = get_adaptive_window_size(
            0,
            0,
            ratio=self.ui_metrics.get("main_window_ratio", (0.70, 0.70)),
            minimum=self.ui_metrics.get("main_window_min_size"),
        )
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
                caption_color = DWORD(0x333333)  # 使用与主界面相同的颜色
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
        self._install_gui_log_handler()
        
        # 设置窗口标题
        self.setWindowTitle(WINDOW_TITLE)
        
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
        
        # DuckDB 数据路径（低调显示，放在状态标签左侧）
        self.duckdb_path_label = QLabel("")
        self.duckdb_path_label.setStyleSheet("""
            QLabel {
                color: #5cb3ff;
                background: #3a3a3a;
                font-size: 15px;
                font-weight: bold;
                border: 1px solid #4d4d4d;
                border-radius: 4px;
                padding: 3px 12px;
                margin: 0 6px;
            }
        """)
        self.status_bar.addPermanentWidget(self.duckdb_path_label)

        # 添加状态标签（放在右侧）
        self.status_label = QLabel("就绪")
        self.status_label.setMinimumWidth(self.ui_metrics["status_label_width"])
        self.status_bar.addPermanentWidget(self.status_label)

        progress_height = self.ui_metrics["progress_height"]

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
                self.bar.setFixedHeight(progress_height)
                self.bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                layout.addWidget(self.bar, 1)
                # 右侧文本
                self.label = QLabel("进度: 0%", self)
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

        # 初始化 DuckDB 路径显示
        self._update_duckdb_path_label()
        
        main_layout = QHBoxLayout()  # 水平布局，包含三列
        margins = self.ui_metrics["main_layout_margins"]
        main_layout.setContentsMargins(*margins)
        main_layout.setSpacing(self.ui_metrics["main_layout_spacing"])
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
        panel_min_width = self.ui_metrics["main_panel_min_width"]
        left_panel.setMinimumWidth(panel_min_width)
        middle_panel.setMinimumWidth(panel_min_width)
        right_panel.setMinimumWidth(panel_min_width)

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
        
        # 初始化用户策略目录（splash 仍在显示，跳过迁移弹窗，避免被启动画面遮挡）
        self.init_user_strategies(check_legacy=False)
        
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

        # 数据管理：浏览本地 DuckDB，用 BaoStock / Tushare 下载数据
        self.duckdb_viewer_action = toolbar.addAction("数据管理")
        self.duckdb_viewer_action.setToolTip("打开 DuckDB 本地数据管理，用 BaoStock / Tushare 下载数据")
        self.duckdb_viewer_action.triggered.connect(self.open_duckdb_viewer)

        # 添加分隔符
        toolbar.addSeparator()

        # 添加设置按钮
        settings_action = toolbar.addAction("设置")
        settings_action.triggered.connect(self.show_settings)
        
        # 添加弹性空间
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        toolbar.addWidget(spacer)

        self.memory_usage_label = QLabel("--G/--G")
        self.memory_usage_label.setMinimumWidth(96)
        self.memory_usage_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.memory_usage_label.setToolTip("系统内存占用，低频刷新")
        self.memory_usage_label.setStyleSheet("""
            QLabel {
                color: #e8e8e8;
                background: transparent;
                padding: 0 6px;
                font-weight: normal;
            }
        """)
        toolbar.addWidget(self.memory_usage_label)
        self.memory_usage_timer = QTimer(self)
        self.memory_usage_timer.timeout.connect(self.update_memory_usage_label)
        self.memory_usage_timer.start(5000)
        self.update_memory_usage_label()
        
        # 创建状态指示灯
        self.status_indicator = QLabel()
        indicator_size = self.ui_metrics["toolbar_indicator_size"]
        self.status_indicator.setFixedSize(indicator_size, indicator_size)
        self.status_indicator.setToolTip("DuckDB数据状态")
        
        # 创建一个容器来包装状态指示灯，并添加边距
        indicator_container = QWidget()
        indicator_layout = QHBoxLayout(indicator_container)
        indicator_layout.setContentsMargins(0, 0, 10, 0)  # 右边距为10像素
        indicator_layout.addWidget(self.status_indicator)
        toolbar.addWidget(indicator_container)
        
        # 添加帮助按钮
        self._help_btn = QToolButton()
        self._help_btn.setText("?")
        self._help_btn.setToolTip("打开使用教程")
        self._help_btn.clicked.connect(self.open_help_tutorial)
        toolbar.addWidget(self._help_btn)

        # 应用帮助按钮样式
        self._apply_extra_btn_styles()
        
        # 添加定时器来检查软件状态
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.check_software_status)
        self.status_timer.start(5000)  # 每5秒检查一次
        
        # 初始检查
        self.check_software_status()
        
        # 设置工具栏样式
        self._apply_toolbar_style(toolbar)


    def _apply_toolbar_style(self, toolbar):
        """应用工具栏样式（避免缩放后出现浅色边线）"""
        if toolbar is None:
            return
        ui_font_family = get_preferred_ui_font_family() or "Microsoft YaHei UI"
        font_size = max(10, int(14 * getattr(self, 'font_scale', 1.0)))
        spacing = self.ui_metrics["toolbar_spacing"]
        padding = self.ui_metrics["toolbar_padding"]
        separator_width = self.ui_metrics["toolbar_separator_width"]
        button_padding_y = self.ui_metrics["toolbar_button_padding_y"]
        button_padding_x = self.ui_metrics["toolbar_button_padding_x"]
        button_min_width = self.ui_metrics["toolbar_button_min_width"]
        button_min_height = self.ui_metrics.get("toolbar_button_min_height", 0)
        toolbar.setStyleSheet(f"""
            QToolBar {{
                background-color: #333333;
                border: none;
                spacing: {spacing}px;
                padding: {padding}px;
            }}
            QToolBar::separator {{
                background-color: #333333;
                border: none;
                width: {separator_width}px;
                margin: 0px;
            }}
            QToolBar::item {{
                border: 0px;
                margin: 0px;
                padding: 0px;
                background: transparent;
            }}
            QWidget {{
                background-color: #333333;
                border: none;
            }}
            QToolButton {{
                background-color: #505050;
                border: 0px solid transparent;
                border-radius: 4px;
                padding: {button_padding_y}px {button_padding_x}px;
                color: #ffffff;
                min-width: {button_min_width}px;
                min-height: {button_min_height}px;
                font-family: "{ui_font_family}";
                font-weight: normal;
                font-size: {font_size}px;
                outline: none;
            }}
            QToolButton:focus {{
                border: 0px solid transparent;
                outline: none;
            }}
            QToolButton:hover {{
                background-color: #606060;
                border: 0px solid transparent;
                outline: none;
            }}
            QToolButton:pressed {{
                background-color: #454545;
                border: 0px solid transparent;
                outline: none;
            }}
            QToolButton:disabled {{
                background-color: #404040;
                color: #808080;
                border: 0px solid transparent;
            }}
            QToolButton:checked {{
                border: 0px solid transparent;
                outline: none;
            }}
        """)

    def set_button_colors(self):
        """为运行和停止按钮设置特定颜色样式"""
        ui_font_family = get_preferred_ui_font_family() or "Microsoft YaHei UI"
        font_size = max(10, int(14 * getattr(self, 'font_scale', 1.0)))
        button_padding_y = self.ui_metrics["toolbar_button_padding_y"]
        button_padding_x = self.ui_metrics["toolbar_button_padding_x"]
        button_min_width = self.ui_metrics["toolbar_button_min_width"]
        # 获取工具栏中的按钮widget
        toolbar = self.findChild(QToolBar, "mainToolBar")
        if toolbar:
            # 为开始运行按钮设置绿色样式
            for action in toolbar.actions():
                widget = toolbar.widgetForAction(action)
                if widget and action == self.start_action:
                    widget.setStyleSheet(f"""
                        QToolButton {{
                            background-color: #2d7a2d;
                            border: none;
                            border-radius: 4px;
                            padding: {button_padding_y}px {button_padding_x}px;
                            color: #ffffff;
                            min-width: {button_min_width}px;
                            font-family: "{ui_font_family}";
                            font-weight: normal;
                            font-size: {font_size}px;
                        }}
                        QToolButton:hover {{
                            background-color: #3d8a3d;
                        }}
                        QToolButton:pressed {{
                            background-color: #1d6a1d;
                        }}
                        QToolButton:disabled {{
                            background-color: #404040;
                            color: #808080;
                        }}
                    """)
                # 为停止运行按钮设置红色样式
                elif widget and action == self.stop_action:
                    widget.setStyleSheet(f"""
                        QToolButton {{
                            background-color: #8b2635;
                            border: none;
                            border-radius: 4px;
                            padding: {button_padding_y}px {button_padding_x}px;
                            color: #ffffff;
                            min-width: {button_min_width}px;
                            font-family: "{ui_font_family}";
                            font-weight: normal;
                            font-size: {font_size}px;
                        }}
                        QToolButton:hover {{
                            background-color: #9b3645;
                        }}
                        QToolButton:pressed {{
                            background-color: #7b1625;
                        }}
                        QToolButton:disabled {{
                            background-color: #404040;
                            color: #808080;
                        }}
                    """)

    def _apply_extra_btn_styles(self):
        """应用帮助按钮的样式（含 font-size，确保缩放后一致）"""
        ui_font_family = get_preferred_ui_font_family() or "Microsoft YaHei UI"
        font_size = max(10, int(14 * getattr(self, 'font_scale', 1.0)))
        extra_button_height = self.ui_metrics["extra_button_height"]
        help_button_size = self.ui_metrics["help_button_size"]
        if hasattr(self, '_help_btn') and self._help_btn:
            self._help_btn.setStyleSheet(f"""
                QToolButton {{
                    background-color: #505050;
                    color: #ffffff;
                    border: none;
                    border-radius: 10px;
                    font-family: "{ui_font_family}";
                    font-weight: normal;
                    font-size: {font_size}px;
                    min-width: {help_button_size}px;
                    max-width: {help_button_size}px;
                    min-height: {help_button_size}px;
                    max-height: {help_button_size}px;
                }}
                QToolButton:hover {{
                    background-color: #606060;
                }}
            """)

    def _ensure_duckdb_data_path(self):
        """返回 DuckDB 数据目录。

        用户设置过的目录优先（不可写时返回空）；没设置过时用开源版自己的默认
        目录 %LOCALAPPDATA%\\KhQuantOS\\khData，只创建目录，不写进设置。
        """
        try:
            saved_path = self.settings.value('duckdb_data_path', '') or ''

            if saved_path:
                expanded = os.path.expanduser(os.path.expandvars(saved_path))
                abs_path = os.path.abspath(expanded)
                if os.path.isdir(abs_path) and os.access(abs_path, os.W_OK):
                    return abs_path
                logging.warning(f"保存的 DuckDB 路径不可写: {saved_path}")
                return ''

            default_path = default_duckdb_dir()
            os.makedirs(default_path, exist_ok=True)
            return default_path
        except Exception as e:
            logging.warning(f"获取 DuckDB 数据路径失败: {e}")
            return ''

    def _update_duckdb_path_label(self):
        """刷新状态栏中低调显示的 DuckDB 数据路径（长路径中间省略，完整路径见悬停）。"""
        try:
            label = getattr(self, 'duckdb_path_label', None)
            if label is None:
                return
            path = self._ensure_duckdb_data_path()
            if not path:
                label.setText("DuckDB：未设置")
                label.setToolTip("尚未设置 DuckDB 数据路径，可在设置中配置")
                return
            from PyQt5.QtGui import QFontMetrics
            prefix = "DuckDB："
            metrics = QFontMetrics(label.font())
            elided = metrics.elidedText(path, Qt.ElideMiddle, 520)
            label.setText(f"{prefix}{elided}")
            label.setToolTip(path)
        except Exception:
            pass

    def check_software_status(self):
        """检查 DuckDB 数据目录状态，并刷新状态栏里的路径显示。"""
        try:
            self._update_duckdb_path_label()
            self.check_duckdb_status()

        except Exception as e:
            logging.error(f"检查软件状态时出错: {str(e)}")
            self.update_status_indicator("red", "状态检查失败")

    def check_duckdb_status(self):
        """检查DuckDB数据库状态"""
        try:
            # 从设置中读取DuckDB数据路径，并在受限环境下自动回退到可写目录
            duckdb_data_path = self._ensure_duckdb_data_path()

            # 检查路径是否设置
            if not duckdb_data_path:
                self.update_status_indicator("red", "DuckDB数据路径未设置")
                return

            # 检查路径是否存在
            if not os.path.exists(duckdb_data_path):
                self.update_status_indicator("red", "DuckDB数据路径不存在")
                return

            # 检查是否有数据库文件
            has_data = False
            for market in ['SH', 'SZ', 'BJ']:
                market_path = os.path.join(duckdb_data_path, market)
                if os.path.exists(market_path):
                    # 检查目录下是否有.db文件
                    db_files = [f for f in os.listdir(market_path) if f.endswith('.db')]
                    if db_files:
                        has_data = True
                        break

            if has_data:
                self.update_status_indicator("green", "DuckDB数据库正常")
            else:
                self.update_status_indicator("red", "DuckDB数据库为空")

        except Exception as e:
            logging.error(f"检查DuckDB状态时出错: {str(e)}")
            self.update_status_indicator("red", "DuckDB状态检查失败")


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

    def update_memory_usage_label(self):
        """Refresh the lightweight system memory label in the status bar."""
        label = getattr(self, "memory_usage_label", None)
        if label is None:
            return

        try:
            memory = psutil.virtual_memory()
            gib = 1024 ** 3
            used_gb = (memory.total - memory.available) / gib
            total_gb = int(round(memory.total / gib))
            label.setText(f"{used_gb:.1f}G/{total_gb}G")
        except Exception as exc:
            label.setText("--G/--G")
            logging.debug("Failed to refresh memory usage label: %s", exc)

    def init_config(self):
        """初始化配置"""
        self.config = {
            "run_mode": "backtest",  # 固定为回测模式
            "account": {"account_id": "", "account_type": "STOCK"},
            "system": {
                "session_id": int(datetime.now().timestamp()),
                "check_interval": 3
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
        self.benchmark_input.setText("000300.SH")  # 默认沪深300（标准格式）
        self.benchmark_input.setPlaceholderText("支持两种格式: 000300.SH 或 sh.000300")
        self.benchmark_input.setToolTip(
            "基准合约代码，用于计算策略相对基准的收益率\n"
            "支持两种格式:\n"
            "  - 标准格式: 000300.SH (沪深300)\n"
            "  - BaoStock 格式: sh.000300\n"
            "常用指数:\n"
            "  000300.SH - 沪深300\n"
            "  000905.SH - 中证500\n"
            "  000852.SH - 中证1000\n"
            "  000001.SH - 上证指数"
        )
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

        # 注：过户费率是中国结算法定固定值、且按成交日期分段（2015-07-09 / 2022-04-29 两次调整），
        # 不做前台输入，统一在后台 khTrade._transfer_fee_by_date 按成交日期硬编码处理。

        # 滑点设置
        slippage_type_label = QLabel("滑点类型:")
        self.slippage_type = NoWheelComboBox()
        self.slippage_type.addItems(["按最小变动价跳数", "按成交金额比例"])
        self.slippage_type.setCurrentText(_SLIPPAGE_TYPE_TO_LABEL["ratio"])
        cost_layout.addWidget(slippage_type_label, 4, 0)
        cost_layout.addWidget(self.slippage_type, 4, 1)
        
        self.slippage_value = QLineEdit()
        self._slippage_value_cache = dict(_SLIPPAGE_UI_DEFAULTS)
        self._slippage_tick_size = 0.01
        self._active_slippage_type = None
        self._apply_slippage_input_mode("ratio", save_current=False, log_change=False)
        self.slippage_type.currentTextChanged.connect(self.slippage_type_changed)
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
        # 开源版：BaoStock 只有日线和 5 分钟线，1m / tick 需要自备数据
        for _period, _tip in (
            ("tick", "需自备数据：BaoStock 和 Tushare 都不提供 Tick，需自行导入 DuckDB"),
            ("1m", "需自备数据：BaoStock 没有 1 分钟线；Tushare 需开通 stk_mins 权限才能下载"),
        ):
            _index = self.period_selector.findText(_period)
            if _index >= 0:
                self.period_selector.setItemData(_index, _tip, Qt.ToolTipRole)
        self.period_selector.setCurrentText("1d")  # 设置默认值
        self.period_selector.currentTextChanged.connect(self.on_period_changed)
        period_widget = QWidget()
        period_widget.setStyleSheet("background-color: transparent;")
        period_row = QHBoxLayout(period_widget)
        period_row.setContentsMargins(0, 0, 0, 0)
        period_row.addWidget(self.period_selector, 1)
        period_hint = QLabel("1m / tick 需自备数据")
        period_hint.setStyleSheet("color: #d7a64a;")
        period_hint.setToolTip(
            "BaoStock 只有日线和 5 分钟线。1 分钟线可用 Tushare 下载（需 stk_mins 权限），"
            "Tick 需自行导入 DuckDB。"
        )
        period_row.addWidget(period_hint)
        data_layout.addWidget(QLabel("周期类型:"), 1, 0)
        data_layout.addWidget(period_widget, 1, 1)
        
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
        # 注: 复权(前/后/等比)由上方"复权类型"下拉(adjust_selector→dividend_type)统一控制,
        # 数据加载时按所选复权类型对 OHLC 自动调整, 故此处不再单列 *_front/_back/_*_ratio 复权字段以免重复勾选。
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
            item.label: item.desktop_codes[0]
            for item in desktop_pool_definitions()
            if item.id != "custom"
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
            # 三列布局留给每个股票池的横向空间有限。场内基金的完整注册名
            # 在窄窗口下会被裁掉，主界面使用短名称，悬浮提示保留完整含义。
            display_name = "场内基金/LOF" if code == "hs_fund" else name
            label = QLabel(display_name)
            label.setToolTip(name)
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
        self.trigger_type_combo.addItems(["Tick触发（需自备数据）", "1分钟K线触发（需自备数据）", "5分钟K线触发", "日K线触发", "自定义定时触发"])
        self.trigger_type_combo.currentIndexChanged.connect(self.trigger_type_changed)
        trigger_type_layout.addWidget(self.trigger_type_combo)
        trigger_layout.addLayout(trigger_type_layout)
        
        self.daily_trigger_cap_widget = QWidget()
        self.daily_trigger_cap_widget.setStyleSheet("background-color: transparent;")
        daily_trigger_cap_layout = QHBoxLayout(self.daily_trigger_cap_widget)
        daily_trigger_cap_layout.setContentsMargins(0, 0, 0, 0)
        daily_trigger_cap_layout.addWidget(QLabel("日内触发上限:"))
        self.daily_trigger_cap_spin = QSpinBox()
        self.daily_trigger_cap_spin.setMinimum(1)
        self.daily_trigger_cap_spin.setMaximum(100)
        self.daily_trigger_cap_spin.setValue(1)
        daily_trigger_cap_layout.addWidget(self.daily_trigger_cap_spin)
        daily_trigger_cap_layout.addStretch()
        trigger_layout.addWidget(self.daily_trigger_cap_widget)
        
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

        # 初始化触发类型相关UI状态（含日内触发上限的显示/隐藏）
        self.trigger_type_changed(self.trigger_type_combo.currentIndex())
        
    def setup_right_panel(self):
        """设置右侧面板，只包含系统日志"""
        # 创建系统日志组
        log_group = QGroupBox("系统日志")
        log_layout = QVBoxLayout()
        
        # 创建日志文本框
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)  # 设置为只读
        self.log_text.setLineWrapMode(QTextEdit.WidgetWidth)  # 自动换行
        self.apply_log_text_style()
        
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
        
        # 添加回测历史管理窗口的按钮
        self.open_backtest_btn = QPushButton("回测历史管理")
        self.open_backtest_btn.clicked.connect(self.open_history_manager)
        self.open_backtest_btn.setEnabled(True)  # 初始启用按钮
        button_layout.addWidget(self.open_backtest_btn)
        
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

    def set_strategy_file(self, file_path):
        if not file_path:
            return
        self.strategy_path.setText(file_path)
        if not hasattr(self, "config") or not self.config:
            self.init_config()
        self.config["strategy_file"] = file_path
        self.settings.setValue('last_strategy_path', file_path)
        logging.info(f"已设置策略文件: {file_path}")

    def _get_strategy_config_path(self):
        """获取当前策略配置文件路径，用作 strategy_file 相对路径基准。"""
        current = getattr(self, "current_config_file", None)
        if current:
            return current
        last_config_path = self.settings.value('last_config_path', '')
        if last_config_path:
            return last_config_path
        return None

    def _resolve_strategy_file_path(self, raw_path=None, config_path=None):
        """解析策略文件路径，优先相对于当前 .kh 文件所在目录。"""
        if raw_path is None:
            raw_path = self.strategy_path.text().strip()
            if not raw_path and hasattr(self, "config") and self.config:
                raw_path = self.config.get("strategy_file", "")

        strategy_dir = ""
        try:
            strategy_dir = self.get_user_strategies_dir()
        except Exception:
            strategy_dir = ""

        return resolve_strategy_file(
            raw_path,
            config_path=config_path or self._get_strategy_config_path(),
            strategy_dir=strategy_dir,
            extra_base_dirs=[os.path.dirname(os.path.abspath(__file__))],
        )

    def _strategy_file_value_for_config(self, target_config_path):
        """保存 .kh 时使用的 strategy_file 值。"""
        raw_path = self.strategy_path.text().strip()
        return strategy_file_for_config(raw_path, target_config_path)

    def _load_strategy_config_for_runtime(self):
        """读取当前 .kh 作为运行配置基准，避免 GUI 默认控件污染策略参数。"""
        config_path = getattr(self, "current_config_file", None)
        if config_path and os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return copy.deepcopy(getattr(self, "config", {}) or {})

    def _remember_loaded_config_state(self):
        """记录加载配置后的 UI 状态，用于运行时判断用户是否真的改过参数。"""
        try:
            self._loaded_config_snapshot = strip_runtime_config(copy.deepcopy(getattr(self, "config", {}) or {}))
            self._ui_loaded_state_snapshot = self._collect_ui_runtime_state()
        except Exception as e:
            logging.warning(f"记录GUI配置快照失败: {e}")
            self._loaded_config_snapshot = strip_runtime_config(copy.deepcopy(getattr(self, "config", {}) or {}))
            self._ui_loaded_state_snapshot = None

    @staticmethod
    def _normalize_for_compare(value):
        if isinstance(value, float):
            return round(value, 10)
        if isinstance(value, dict):
            return {k: KhQuantGUI._normalize_for_compare(v) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [KhQuantGUI._normalize_for_compare(v) for v in value]
        return value

    def _ui_state_changed(self, ui_state, section):
        baseline = getattr(self, "_ui_loaded_state_snapshot", None)
        if not isinstance(baseline, dict):
            return True
        return self._normalize_for_compare(ui_state.get(section)) != self._normalize_for_compare(baseline.get(section))

    def _ui_field_changed(self, section, key, value):
        baseline = getattr(self, "_ui_loaded_state_snapshot", None)
        if not isinstance(baseline, dict):
            return True
        old_section = baseline.get(section, {}) or {}
        if key not in old_section:
            return True
        return self._normalize_for_compare(value) != self._normalize_for_compare(old_section.get(key))

    def _collect_ui_runtime_state(self):
        """采集 GUI 当前显示的回测相关状态，不直接写入 self.config。"""
        trigger_type = self.get_trigger_type()
        trigger_config = {
            "type": trigger_type,
            "start_time": self.start_time_edit.time().toString("HH:mm:ss"),
            "end_time": self.end_time_edit.time().toString("HH:mm:ss"),
            "interval": self.interval_spin.value(),
            "daily_trigger_cap": self.daily_trigger_cap_spin.value(),
        }
        if trigger_type == "custom":
            trigger_config["custom_times"] = self.get_custom_time_points()

        backtest = {
            "start_time": self.start_date.date().toString("yyyyMMdd"),
            "end_time": self.end_date.date().toString("yyyyMMdd"),
            "init_capital": float(self.initial_cash.text()),
            "benchmark": self.benchmark_input.text().strip() or "000300.SH",
            "min_volume": int(self.min_volume.text()),
            "trade_cost": {
                "min_commission": float(self.min_commission.text()),
                "commission_rate": float(self.commission_rate.text()),
                "stamp_tax_rate": float(self.stamp_tax.text()),
                "flow_fee": float(self.flow_fee.text()),
                "slippage": self.get_slippage_settings(),
            },
            "trigger": trigger_config,
            "pre_trigger": {
                "enabled": self.pre_trigger_checkbox.isChecked(),
                "time": self.pre_trigger_time.time().toString("HH:mm:ss"),
            },
            "post_trigger": {
                "enabled": self.post_trigger_checkbox.isChecked(),
                "time": self.post_trigger_time.time().toString("HH:mm:ss"),
            },
        }

        data = {
            "kline_period": self.period_selector.currentText(),
            "dividend_type": self.get_dividend_type(),
            "fields": self.get_selected_fields(),
            "stock_list": self.get_stock_list(),
        }

        return {
            "strategy_file": self.strategy_path.text().strip(),
            "run_mode": "backtest",
            "account": {
                "account_id": self.settings.value('account_id', ''),
                "account_type": self.settings.value('account_type', 'STOCK'),
            },
            "backtest": backtest,
            "data": data,
            "market_callback": {
                "pre_market_enabled": self.pre_trigger_checkbox.isChecked(),
                "pre_market_time": self.pre_trigger_time.time().toString("HH:mm:ss"),
                "post_market_enabled": self.post_trigger_checkbox.isChecked(),
                "post_market_time": self.post_trigger_time.time().toString("HH:mm:ss"),
            },
        }

    def _apply_ui_changes_to_runtime_config(self, runtime_config, ui_state):
        """只把用户在 GUI 中实际改过的分组覆盖到运行配置。"""
        cfg = copy.deepcopy(runtime_config or {})
        cfg["run_mode"] = "backtest"
        has_baseline = isinstance(getattr(self, "_ui_loaded_state_snapshot", None), dict)

        if self._ui_state_changed(ui_state, "strategy_file"):
            cfg["strategy_file"] = ui_state["strategy_file"]

        if "account" not in cfg and (self._ui_state_changed(ui_state, "account") or not has_baseline):
            cfg.setdefault("account", {}).update(ui_state["account"])

        raw_base_bt = cfg.get("backtest", {})
        base_bt = (
            copy.deepcopy(raw_base_bt)
            if isinstance(raw_base_bt, dict)
            else {}
        )
        ui_bt = ui_state["backtest"]
        bt_changed = False
        for key in ("start_time", "end_time", "init_capital", "benchmark"):
            if self._ui_field_changed("backtest", key, ui_bt.get(key)):
                base_bt[key] = ui_bt.get(key)
                bt_changed = True
        for key in ("min_volume", "trade_cost", "trigger", "pre_trigger", "post_trigger"):
            changed = self._ui_field_changed("backtest", key, ui_bt.get(key))
            if changed or not has_baseline:
                if key == "trade_cost":
                    base_bt[key] = _merge_trade_cost_config(
                        base_bt.get(key),
                        ui_bt.get(key),
                    )
                else:
                    base_bt[key] = ui_bt.get(key)
                bt_changed = True
        if bt_changed or "backtest" in cfg:
            cfg["backtest"] = base_bt

        raw_base_data = cfg.get("data", {})
        base_data = (
            copy.deepcopy(raw_base_data)
            if isinstance(raw_base_data, dict)
            else {}
        )
        ui_data = ui_state["data"]
        data_changed = False
        for key in ("kline_period", "dividend_type", "fields", "stock_list"):
            baseline = getattr(self, "_ui_loaded_state_snapshot", None) or {}
            old_value = (baseline.get("data", {}) or {}).get(key)
            new_value = ui_data.get(key)
            changed = self._normalize_for_compare(old_value) != self._normalize_for_compare(new_value)
            if changed or not has_baseline:
                base_data[key] = new_value
                data_changed = True
        if data_changed or "data" in cfg:
            if "stock_list" in base_data and "stock_list_file" in base_data:
                base_data.pop("stock_list_file", None)
            cfg["data"] = base_data

        baseline = getattr(self, "_ui_loaded_state_snapshot", None) or {}
        market_changed = (
            self._normalize_for_compare(baseline.get("market_callback"))
            != self._normalize_for_compare(ui_state.get("market_callback"))
        )
        if market_changed or "market_callback" in cfg:
            if market_changed or not has_baseline:
                cfg["market_callback"] = copy.deepcopy(ui_state["market_callback"])

        return cfg

    @staticmethod
    def _sanitize_runtime_slippage_config(runtime_config):
        """运行前规范化原始费用配置，不能只依赖“UI 是否改过”的判断。"""
        cfg = copy.deepcopy(runtime_config or {})
        backtest = cfg.get("backtest")
        if not isinstance(backtest, dict):
            if "backtest" in cfg:
                cfg["backtest"] = {}
            return cfg
        if "trade_cost" in backtest:
            backtest["trade_cost"] = _normalize_trade_cost_config(
                backtest.get("trade_cost")
            )
        return cfg

    def _build_runtime_config_for_run(self):
        """构建 GUI 回测临时配置，与 CLI 运行语义保持一致。"""
        base_config = self._load_strategy_config_for_runtime()
        ui_state = self._collect_ui_runtime_state()
        runtime_config = self._apply_ui_changes_to_runtime_config(base_config, ui_state)
        runtime_config = preserve_strategy_runtime_blocks(runtime_config, base_config)
        runtime_config = self._sanitize_runtime_slippage_config(runtime_config)
        runtime_config = apply_system_runtime_settings(runtime_config, self.settings.load())
        runtime_config, _, _ = stamp_memory_decision(
            runtime_config,
            config_path=getattr(self, "current_config_file", None),
        )
        return runtime_config

    def update_config(self):
        """更新配置信息"""
        try:
            old_config = strip_runtime_config(getattr(self, "config", {}) or {})
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
            self.config["account"]["account_id"] = self.settings.value('account_id', '')
            self.config["account"]["account_type"] = self.settings.value('account_type', 'STOCK')
            
            # 更新初始资金和最小交易量 - 从虚拟账户设置中获取
            initial_capital = float(self.initial_cash.text())
            min_volume = int(self.min_volume.text())
            self.config["backtest"]["init_capital"] = initial_capital
            self.config["backtest"]["min_volume"] = min_volume

            # 更新基准合约
            benchmark_raw = self.benchmark_input.text().strip()
            if benchmark_raw:
                self.config["backtest"]["benchmark"] = benchmark_raw
            else:
                self.config["backtest"]["benchmark"] = "000300.SH"  # 默认沪深300

            # 更新交易成本设置
            self.config["backtest"]["trade_cost"] = _merge_trade_cost_config(
                self.config["backtest"].get("trade_cost"),
                {
                    "min_commission": float(self.min_commission.text()),
                    "commission_rate": float(self.commission_rate.text()),
                    "stamp_tax_rate": float(self.stamp_tax.text()),
                    "flow_fee": float(self.flow_fee.text()),
                    "slippage": self.get_slippage_settings(),
                },
            )
            
            # 更新触发方式配置
            trigger_type_map = {
                0: "tick",  # Tick触发
                1: "1m",    # 1分钟K线触发
                2: "5m",    # 5分钟K线触发
                3: "1d",    # 日K线触发
                4: "custom" # 自定义定时触发
            }
            
            trigger_type = trigger_type_map[self.trigger_type_combo.currentIndex()]
            trigger_config = {
                "type": trigger_type,
                "start_time": self.start_time_edit.time().toString("HH:mm:ss"),
                "end_time": self.end_time_edit.time().toString("HH:mm:ss"),
                "interval": self.interval_spin.value(),
                "daily_trigger_cap": self.daily_trigger_cap_spin.value()
            }
            if trigger_type == "custom":
                trigger_config["custom_times"] = self.get_custom_time_points()
            self.config["backtest"]["trigger"] = trigger_config
            
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
            
            # 更新股票池配置
            stock_codes = []
            seen_stock_codes = set()
            
            # 先添加自定义股票列表中的股票代码（优先级最高）
            for row in range(self.stock_list.rowCount()):
                item = self.stock_list.item(row, 0)
                code = item.text().strip() if item else ""
                if code and code not in seen_stock_codes:
                    seen_stock_codes.add(code)
                    stock_codes.append(code)
            
            # 然后添加选中的常用股票池中的股票代码（但排除已在自定义列表中的，以及用户明确删除的）
            for code, cb in self.pool_checkboxes.items():
                if cb.isChecked():
                    pool_file = self._get_pool_file(code)
                    if pool_file:
                        file_path = self.get_data_path(pool_file)
                        if os.path.exists(file_path):
                            for stock_code, _ in self._read_stock_rows_from_file(file_path):
                                # 排除已在自定义列表中的股票，以及用户明确删除的股票
                                if stock_code not in seen_stock_codes and stock_code not in self.deleted_stocks:
                                    seen_stock_codes.add(stock_code)
                                    stock_codes.append(stock_code)

            # 将股票列表直接保存到配置文件中，不再生成单独的csv文件
            self.config["data"]["stock_list"] = stock_codes
            
            # 移除旧的stock_list_file字段（如果存在）
            if "stock_list_file" in self.config["data"]:
                del self.config["data"]["stock_list_file"]

            self.log_message(f"股票列表已更新到配置文件，共 {len(stock_codes)} 支股票", "INFO")

            # 更新盘前盘后触发设置
            self.config["backtest"]["pre_trigger"] = {
                "enabled": self.pre_trigger_checkbox.isChecked(),
                "time": self.pre_trigger_time.time().toString("HH:mm:ss")
            }
            self.config["backtest"]["post_trigger"] = {
                "enabled": self.post_trigger_checkbox.isChecked(),
                "time": self.post_trigger_time.time().toString("HH:mm:ss")
            }

            self.config = preserve_strategy_runtime_blocks(self.config, old_config)
            
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
    
    def show_t0_warning(self, message: str, strategy_file: str = ""):
        """显示T+0模式混合池警告弹窗
        
        Args:
            message: 警告信息
            strategy_file: 当前策略文件路径，用于判断是否抑制提醒
        """
        # 若该策略已选择不再提醒，直接跳过
        if strategy_file and strategy_file in self._t0_warning_suppressed:
            return
        if QThread.currentThread() != self.thread():
            QMetaObject.invokeMethod(
                self,
                "_show_t0_warning",
                Qt.QueuedConnection,
                Q_ARG(str, message),
                Q_ARG(str, strategy_file)
            )
            return
        self._show_t0_warning(message, strategy_file)

    @pyqtSlot(str, str)
    def _show_t0_warning(self, message: str, strategy_file: str = ""):
        """在GUI线程中显示T+0提示（含"不再提醒"复选框）"""
        try:
            dialog = QDialog(self)
            dialog.setWindowTitle("T+0模式提醒")
            dialog.setMinimumWidth(420)
            layout = QVBoxLayout(dialog)
            layout.setSpacing(12)
            layout.setContentsMargins(18, 18, 18, 14)

            # 警告图标 + 文字
            msg_label = QLabel(message)
            msg_label.setWordWrap(True)
            layout.addWidget(msg_label)

            # 分隔线
            line = QFrame()
            line.setFrameShape(QFrame.HLine)
            line.setFrameShadow(QFrame.Sunken)
            layout.addWidget(line)

            # "不再提醒"复选框
            suppress_cb = QCheckBox("这个策略不再提醒")
            layout.addWidget(suppress_cb)

            # 确定按钮
            btn_box = QDialogButtonBox(QDialogButtonBox.Ok)
            btn_box.accepted.connect(dialog.accept)
            layout.addWidget(btn_box)

            dialog.exec_()

            if suppress_cb.isChecked() and strategy_file:
                self._t0_warning_suppressed.add(strategy_file)
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
                # 确保配置目录存在：%LOCALAPPDATA%\\KhQuantOS\\configs（安装目录对普通用户不可写）
                config_dir = local_appdata_dir("configs")
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
                
                runtime_config = self._build_runtime_config_for_run()
                self._current_runtime_config = runtime_config
                with open(self.temp_config_path, "w", encoding="utf-8") as f:
                    json.dump(runtime_config, f, indent=4, ensure_ascii=False)

            except Exception as e:
                self.log_error("保存配置文件失败", e)
                return

            # ========== 数据完整性检查 ==========
            # 只在回测模式下检查数据完整性
            if self.get_run_mode() == "backtest":
                if not self._check_data_integrity_before_backtest(runtime_config):
                    self.log_message("数据完整性检查未通过，回测已取消", "WARNING")
                    return
            # =====================================

            strategy_file_raw = runtime_config.get("strategy_file", "")
            strategy_file_to_run = self._resolve_strategy_file_path(strategy_file_raw)
            if strategy_file_to_run != strategy_file_raw:
                self.log_message(f"策略文件已解析为: {strategy_file_to_run}", "INFO")

            # 创建并启动策略线程
            self.strategy_thread = StrategyThread(
                self.temp_config_path,
                strategy_file_to_run,
                self.trader_callback,
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
                # 初始化为回测进度（后续会根据阶段改变）
                self.progress_label_text = "回测进度"
                self.progress_text.setText("回测进度: 0%")
                self.progress_container.show()
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
            # 检查是否启用了"停止后直接退出"模式
            stop_exit_immediately = getattr(
                self,
                '_strategy_stop_exit_immediately',
                self.settings.value('stop_exit_immediately', True, type=bool)
            )
            was_stop_requested = getattr(self, '_strategy_stop_requested', False)

            self.log_message("策略已停止" if was_stop_requested else "策略运行完成", "INFO")
            # 恢复界面状态
            self.start_action.setEnabled(True)
            self.stop_action.setEnabled(False)

            # 处理进度条 - 确保设置为100%并更新状态标签
            if self.get_run_mode() == "backtest":
                if was_stop_requested:
                    self.status_label.setText("策略已停止")
                    self.hide_progress()
                else:
                    self.progress_bar.setValue(100)
                    self.status_label.setText("回测完成")
                    # 延迟隐藏进度条，让用户看到100%完成状态
                    QTimer.singleShot(2000, lambda: self.hide_progress())
            else:
                # 非回测模式直接隐藏
                self.hide_progress()
                self.status_label.setText("策略已停止" if was_stop_requested else "策略运行完成")

            # 如果启用了延迟显示，提示用户正在收集日志
            # 但如果是"停止后直接退出"模式且策略已停止（不是自然完成），则跳过
            if self.delay_log_display and not was_stop_requested:
                # 检查是否是用户主动停止（通过检查framework的save_results_on_stop标志）
                is_user_stopped = False
                if (hasattr(self, 'strategy_thread') and
                    hasattr(self.strategy_thread, 'framework') and
                    self.strategy_thread.framework and
                    not self.strategy_thread.framework.save_results_on_stop):
                    is_user_stopped = True

                if not is_user_stopped:
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
                # 但如果是"停止后直接退出"模式且策略已停止，则跳过
                if self.delay_log_display and self.delayed_logs:
                    # 检查是否是用户主动停止
                    is_user_stopped = False
                    if (hasattr(self, 'strategy_thread') and
                        hasattr(self.strategy_thread, 'framework') and
                        self.strategy_thread.framework and
                        not self.strategy_thread.framework.save_results_on_stop):
                        is_user_stopped = True

                    if not is_user_stopped:
                        # 再次延迟一点时间确保所有日志都已收集
                        QTimer.singleShot(200, self.display_delayed_logs)
                    else:
                        # 直接清空延迟日志，不显示
                        self.delayed_logs.clear()

                # 清理临时配置文件
                temp_paths = [getattr(self, 'temp_config_path', None)]
                if hasattr(self, 'strategy_thread') and self.strategy_thread:
                    temp_paths.extend(getattr(self.strategy_thread, 'temp_config_paths', []) or [])
                for temp_path in dict.fromkeys([p for p in temp_paths if p]):
                    if not os.path.exists(temp_path):
                        continue
                    try:
                        os.remove(temp_path)
                    except Exception as e:
                        self.log_message(f"清理临时配置文件失败: {str(e)}", "WARNING")

                self._strategy_stop_requested = False
                self._strategy_stop_warning_shown = False
                self._strategy_stop_exit_immediately = True
                self.update_status("策略已停止运行" if was_stop_requested else "策略运行完成")
                self.set_t0_mode_display(False)

                if was_stop_requested:
                    if stop_exit_immediately:
                        self.log_message("策略已停止（直接退出模式，不保存回测记录）", "INFO")
                    else:
                        self.log_message("策略已停止", "INFO")

                if getattr(self, '_close_after_strategy_stop', False):
                    self._close_after_strategy_stop = False
                    self._close_when_strategy_thread_exited()

            # 延迟2秒执行最终处理，给策略后续日志留出时间
            QTimer.singleShot(0 if was_stop_requested else 2000, finalize_strategy)

        except Exception as e:
            self.log_error("处理策略完成回调时出错", e)


    def _close_when_strategy_thread_exited(self):
        """策略线程真正退出后再继续关闭主窗口。"""
        try:
            if getattr(self, 'strategy_thread', None) is not None and self.strategy_thread.isRunning():
                QTimer.singleShot(50, self._close_when_strategy_thread_exited)
                return

            self._force_close_after_strategy_stop = True
            self.close()
        except Exception as e:
            self.log_message(f"等待策略线程退出后关闭窗口时出错: {str(e)}", "WARNING")

    def _warn_strategy_stop_is_slow(self):
        """停止请求发出后仍未结束时给出提示，不阻塞界面。"""
        try:
            if not getattr(self, '_strategy_stop_requested', False):
                return
            if not getattr(self, 'strategy_thread', None) or not self.strategy_thread.isRunning():
                return
            if getattr(self, '_strategy_stop_warning_shown', False):
                return

            self._strategy_stop_warning_shown = True
            self.update_status("策略仍在停止中，请稍候...")
            self.log_message(
                "策略仍在停止中：当前可能正在等待数据源请求、DuckDB I/O 或结果保存完成，界面会保持可响应。",
                "WARNING"
            )
        except Exception as e:
            logging.warning(f"显示策略停止慢提示时出错: {e}")

    def stop_strategy(self):
        """停止策略运行"""
        try:
            # 清除回测开始时间记录
            if hasattr(self, 'backtest_start_time'):
                delattr(self, 'backtest_start_time')

            if getattr(self, 'strategy_thread', None) is not None and self.strategy_thread.isRunning():
                if getattr(self, '_strategy_stop_requested', False):
                    self.update_status("策略正在停止中，请稍候...")
                    return

                # 检查是否启用了"停止后直接退出"选项
                stop_exit_immediately = self.settings.value('stop_exit_immediately', True, type=bool)
                self._strategy_stop_requested = True
                self._strategy_stop_exit_immediately = stop_exit_immediately
                self._strategy_stop_warning_shown = False

                # 设置框架的标志，控制是否保存结果
                if hasattr(self.strategy_thread, 'framework') and self.strategy_thread.framework:
                    self.strategy_thread.framework.is_running = False
                    # 设置停止后是否保存结果的标志
                    self.strategy_thread.framework.save_results_on_stop = not stop_exit_immediately

                # 请求线程停止，不在GUI线程等待，避免界面未响应
                self.strategy_thread.stop()
                self.update_status("正在停止策略...")
                self.stop_action.setEnabled(False)
                self.start_action.setEnabled(False)

                if hasattr(self, 'status_label'):
                    self.status_label.setText("正在停止策略...")

                if stop_exit_immediately:
                    self.log_message("已请求停止策略（直接退出模式，不保存回测记录）", "INFO")
                else:
                    self.log_message("已请求停止策略，等待当前步骤安全结束...", "INFO")

                QTimer.singleShot(5000, self._warn_strategy_stop_is_slow)
            else:
                self.update_status("当前没有正在运行的策略")

        except Exception as e:
            error_msg = f"停止策略时出错: {str(e)}"
            self.update_status(error_msg)
            logging.error(error_msg, exc_info=True)

    def closeEvent(self, event):
        """窗口关闭事件处理"""
        try:
            if getattr(self, '_force_close_after_strategy_stop', False):
                self._force_close_after_strategy_stop = False
            elif self.strategy_thread and self.strategy_thread.isRunning():
                reply = QMessageBox.question(
                    self, '关闭确认',
                    "策略正在运行中，确定要关闭吗?\n\n"
                    "程序会先请求策略安全停止，停止完成后自动关闭。",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No
                )

                if reply == QMessageBox.Yes:
                    self._close_after_strategy_stop = True
                    self.stop_strategy()
                    event.ignore()
                    return

                event.ignore()
                return

            # 数据管理窗几乎与主窗口重合，两者的关闭按钮相距不到 50 像素，
            # 误点主窗口的 × 会连带退出整个软件。子窗口开着时先让用户确认。
            if self._duckdb_viewer_is_open():
                reply = QMessageBox.question(
                    self, '关闭确认',
                    "数据管理窗口还开着。\n\n"
                    "继续将关闭数据管理并退出整个软件；\n"
                    "只想关掉数据管理的话，请点它自己的关闭按钮。\n\n"
                    "确定退出软件吗?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No
                )
                if reply != QMessageBox.Yes:
                    event.ignore()
                    return

            benchmark_dialog = getattr(self, '_benchmark_dialog', None)
            if benchmark_dialog is not None and benchmark_dialog.is_running():
                benchmark_dialog.thread.stop()
                benchmark_dialog.thread.wait(15000)

            duckdb_close_state = self._request_duckdb_viewer_close_for_shutdown()
            if duckdb_close_state != "ready":
                # 数据管理窗口可能正在等待导入/索引线程安全退出，或者用户在其
                # 二次确认中取消关闭。两种情况都不能继续 app.quit() 强拆窗口。
                event.ignore()
                return

            # 恢复窗口标题
            self.setWindowTitle(WINDOW_TITLE)
            
            # 保存窗口状态和位置
            self.settings.setValue("windowState", self.saveState())
            self.settings.setValue("geometry", self.saveGeometry())
            
            # 记录关闭时间
            end_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.log_message(f"软件关闭时间: {end_time}", "INFO")
            
            # 关闭回测历史窗口
            if hasattr(self, 'history_manager_window') and self.history_manager_window:
                self.history_manager_window.close()
                self.history_manager_window = None

            # 关闭 DuckDB 数据库连接
            try:
                from duckdb_storage import DuckDBManager
                DuckDBManager.reset_instance()
                self.log_message("DuckDB 数据库连接已关闭", "INFO")
            except Exception as e:
                logging.warning(f"关闭 DuckDB 连接时出错: {e}")

            # 停止日志刷新定时器
            if hasattr(self, 'log_flush_timer'):
                self.log_flush_timer.stop()

            # 最后一次刷新日志，确保所有日志都写入文件
            self.flush_logs()

            # 移除GUI日志处理器，避免root logger残留已关闭窗口引用
            self._remove_gui_log_handler()
            
            # 接受关闭事件
            event.accept()
            app = QApplication.instance()
            if app is not None:
                QTimer.singleShot(0, app.quit)
            
        except Exception as e:
            logging.error(f"程序退出时出错: {str(e)}", exc_info=True)
            # 确保日志写入
            self.flush_logs()
            self._remove_gui_log_handler()
            # 即使出错也接受事件，确保程序能够退出
            event.accept()
            app = QApplication.instance()
            if app is not None:
                QTimer.singleShot(0, app.quit)

    def _duckdb_viewer_is_open(self):
        """数据管理窗口是否仍在显示（已被 Qt 销毁的窗口不算）。"""
        window = getattr(self, "duckdb_viewer_window", None)
        if window is None:
            return False
        try:
            from PyQt5 import sip
            if sip.isdeleted(window):
                return False
        except (ImportError, TypeError, RuntimeError):
            pass
        try:
            return bool(window.isVisible())
        except RuntimeError:
            return False

    def _request_duckdb_viewer_close_for_shutdown(self):
        """关闭 DuckDB 数据管理窗口；活动任务存在时让主窗口等待其收尾。"""
        window = getattr(self, "duckdb_viewer_window", None)
        if window is None:
            self._close_after_duckdb_viewer = False
            return "ready"

        try:
            from PyQt5 import sip
            try:
                window_deleted = sip.isdeleted(window)
            except TypeError:
                window_deleted = False
            if window_deleted:
                self.duckdb_viewer_window = None
                self._close_after_duckdb_viewer = False
                return "ready"
            accepted = bool(window.close())
        except RuntimeError:
            self.duckdb_viewer_window = None
            self._close_after_duckdb_viewer = False
            return "ready"

        if accepted:
            self._close_after_duckdb_viewer = False
            return "ready"

        if getattr(window, "_viewer_closing", False):
            self._close_after_duckdb_viewer = True
            self.log_message("正在等待数据管理任务安全停止，完成后自动退出软件", "INFO")
            return "waiting"

        self._close_after_duckdb_viewer = False
        self.log_message("已取消关闭数据管理窗口，软件保持运行", "INFO")
        return "cancelled"

    def _on_duckdb_viewer_destroyed(self, target):
        """清理窗口引用，并在主窗口等待退出时继续完成关闭。"""
        if getattr(self, "duckdb_viewer_window", None) is target:
            self.duckdb_viewer_window = None
        if not getattr(self, "_close_after_duckdb_viewer", False):
            return
        self._close_after_duckdb_viewer = False
        QTimer.singleShot(0, self.close)

    def mode_changed(self):
        """运行模式改变时的处理（固定为回测模式）"""
        # 固定为回测模式，启用所有相关设置
        self.initial_cash.setEnabled(True)
        self.commission_rate.setEnabled(True)
        self.stamp_tax.setEnabled(True)
        self.min_volume.setEnabled(True)
        self.start_date.setEnabled(True)
        self.end_date.setEnabled(True)

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
            self.setWindowTitle(f"{WINDOW_TITLE} - {file_name}")
            
            # 在日志中记录成功加载
            self.log_message(f"配置已从以下位置加载: {file_path}", "INFO")
            self._remember_loaded_config_state()
            
            # 检查加载的配置中是否有文件在危险位置
            strategy_file_path = config.get("strategy_file", "")
            if strategy_file_path:
                resolved_strategy_file = self._resolve_strategy_file_path(strategy_file_path, config_path=file_path)
                self.show_internal_dir_warning(file_path, resolved_strategy_file)
            
        except Exception as e:
            QMessageBox.critical(self, "加载失败", f"加载配置文件时出错: {str(e)}")

    def auto_load_last_config(self):
        """自动加载上次使用的配置文件

        在软件启动时调用，静默加载上次的配置文件
        """
        try:
            # 获取上次的配置文件路径
            last_config_path = self.settings.value('last_config_path', '')

            # 检查文件是否存在
            if not last_config_path or not os.path.exists(last_config_path):
                self.log_message("未找到上次的配置文件，使用默认配置", "INFO")
                return

            # 静默加载配置文件
            with open(last_config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

            # 保存到实例变量
            self.config = config
            self.current_config_file = last_config_path

            # 更新UI
            self.update_ui_from_config()

            # 更新窗口标题
            file_name = os.path.basename(last_config_path)
            self.setWindowTitle(f"{WINDOW_TITLE} - {file_name}")

            # 记录日志
            self.log_message(f"已自动加载配置: {file_name}", "INFO")
            self._remember_loaded_config_state()

        except Exception as e:
            # 静默失败，不弹出错误对话框
            self.log_message(f"自动加载配置文件失败: {str(e)}", "WARNING")
            self.log_message("使用默认配置", "INFO")

    def restore_config_from_history(self, config_dict):
        """从回测历史还原配置到主界面"""
        try:
            # 还原策略文件路径
            if "strategy_file" in config_dict:
                strategy_file = config_dict["strategy_file"]
                resolved_strategy_file = self._resolve_strategy_file_path(strategy_file)
                if strategy_file and os.path.exists(resolved_strategy_file):
                    self.strategy_path.setText(strategy_file)
                    self.log_message(f"策略文件路径已还原: {strategy_file}", "INFO")
                    if resolved_strategy_file != strategy_file:
                        self.log_message(f"策略文件已解析为: {resolved_strategy_file}", "INFO")
                else:
                    self.log_message(f"策略文件不存在，跳过还原: {strategy_file}", "WARNING")
            
            # 还原回测参数 - 处理嵌套的backtest配置
            backtest_config = config_dict.get("backtest", {})
            
            # 还原开始时间
            start_time = backtest_config.get("start_time") or config_dict.get("start_time")
            if start_time:
                try:
                    start_date = QDate.fromString(str(start_time), "yyyyMMdd")
                    if start_date.isValid():
                        self.start_date.setDate(start_date)
                        self.log_message(f"开始时间已还原: {start_time}", "INFO")
                except Exception as e:
                    self.log_message(f"还原开始时间失败: {str(e)}", "WARNING")
                    
            # 还原结束时间
            end_time = backtest_config.get("end_time") or config_dict.get("end_time")
            if end_time:
                try:
                    end_date = QDate.fromString(str(end_time), "yyyyMMdd")
                    if end_date.isValid():
                        self.end_date.setDate(end_date)
                        self.log_message(f"结束时间已还原: {end_time}", "INFO")
                except Exception as e:
                    self.log_message(f"还原结束时间失败: {str(e)}", "WARNING")
                    
            # 还原初始资金
            init_capital = backtest_config.get("init_capital") or config_dict.get("init_capital")
            if init_capital:
                try:
                    self.initial_cash.setText(str(init_capital))
                    self.log_message(f"初始资金已还原: {init_capital}", "INFO")
                except Exception as e:
                    self.log_message(f"还原初始资金失败: {str(e)}", "WARNING")
                    
            # 还原基准合约
            benchmark = backtest_config.get("benchmark") or config_dict.get("benchmark")
            if benchmark:
                try:
                    self.benchmark_input.setText(str(benchmark))
                    self.log_message(f"基准合约已还原: {benchmark}", "INFO")
                except Exception as e:
                    self.log_message(f"还原基准合约失败: {str(e)}", "WARNING")
            
            # 还原最小交易量
            min_volume = backtest_config.get("min_volume") or config_dict.get("min_volume")
            if min_volume:
                try:
                    self.min_volume.setText(str(min_volume))
                    self.log_message(f"最小交易量已还原: {min_volume}", "INFO")
                except Exception as e:
                    self.log_message(f"还原最小交易量失败: {str(e)}", "WARNING")
            
            # 获取触发模式配置
            trigger_config = backtest_config.get("trigger", {})
            
            # 构造临时配置对象，复用主界面的配置加载逻辑
            temp_config = {
                "strategy_file": config_dict.get("strategy_file", ""),
                "data": {
                    "kline_period": config_dict.get("kline_period", "1m"),
                    "dividend_type": config_dict.get("dividend_type", "front"),
                    "stock_list": []
                },
                "backtest": {
                    "start_time": start_time or "",
                    "end_time": end_time or "",
                    "init_capital": init_capital or 1000000,
                    "benchmark": benchmark or "",
                    "min_volume": min_volume or 100,
                    "trigger": trigger_config,
                    "trade_cost": copy.deepcopy(
                        backtest_config.get(
                            "trade_cost",
                            config_dict.get("trade_cost", {}),
                        )
                    ),
                },
                "market_callback": config_dict.get("market_callback", {})
            }
            
            # 处理股票池数据
            stock_list_str = config_dict.get("stock_list")
            if stock_list_str and isinstance(stock_list_str, str) and stock_list_str.strip():
                stock_codes = [stock.strip() for stock in stock_list_str.split(',') if stock.strip()]
                temp_config["data"]["stock_list"] = stock_codes
            
            # 保存当前配置
            original_config = getattr(self, 'config', {})
            
            # 临时设置配置并调用主界面的更新方法
            self.config = temp_config
            
            try:
                # 使用统一的配置更新方法
                self.update_ui_from_config()
                self.log_message("配置已通过统一方法还原到主界面", "INFO")
                    
            finally:
                # 恢复原始配置
                self.config = original_config
            
            # 更新内部配置对象
            self.config.update(config_dict)
            self.config = self._sanitize_runtime_slippage_config(self.config)
            
            self.log_message("配置已从回测历史成功还原到主界面", "INFO")
            self._remember_loaded_config_state()
            
        except Exception as e:
            self.log_message(f"还原配置时出错: {str(e)}", "ERROR")
            import traceback
            self.log_message(f"详细错误信息: {traceback.format_exc()}", "ERROR")
            raise
    
    def update_ui_from_config(self):
        """根据已加载的配置更新UI"""
        if not hasattr(self, 'config') or not self.config:
            return
            
        # 更新策略文件路径
        if "strategy_file" in self.config:
            self.strategy_path.setText(self.config["strategy_file"])
            
        # 运行模式固定为回测，无需更新选择器
        self.config["run_mode"] = "backtest"
            
        # 更新回测参数
        backtest_config = self.config.get("backtest", {})
        if not isinstance(backtest_config, dict):
            backtest_config = {}
        if backtest_config:
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
                daily_trigger_cap = int(trigger_config.get("daily_trigger_cap", 1) or 1)
                self.daily_trigger_cap_spin.setValue(daily_trigger_cap)
        
        # 更新交易成本设置。即使旧配置没有滑点字段，也必须恢复默认值，
        # 不能残留上一个策略文件的 tick/ratio 状态。
        trade_cost = backtest_config.get("trade_cost")
        normalized_trade_cost = _normalize_trade_cost_config(trade_cost)
        self.min_commission.setText(
            format(normalized_trade_cost["min_commission"], ".12g")
        )
        self.commission_rate.setText(
            format(normalized_trade_cost["commission_rate"], ".12g")
        )
        self.stamp_tax.setText(
            format(normalized_trade_cost["stamp_tax_rate"], ".12g")
        )
        self.flow_fee.setText(
            format(normalized_trade_cost["flow_fee"], ".12g")
        )
        self._load_slippage_settings(normalized_trade_cost["slippage"])
        if isinstance(trade_cost, dict):
            # 把内存中的旧/异常值同步收敛，保存和立即运行使用同一语义；
            # 未配置 trade_cost 时只重置界面，不擅自向原配置注入字段。
            backtest_config["trade_cost"] = normalized_trade_cost
        
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
                

    def on_start_date_changed(self, date):
        """开始日期变化时更新结束日期的最小值"""
        self.end_date.setMinimumDate(date)

    def _load_all_stock_entries(self):
        """加载本地股票和场内基金列表，返回去重后的 [(code, name), ...]。"""
        entries = []
        seen_codes = set()
        for filename in (
            "全部股票_股票列表.csv",
            "沪深ETF_成分股列表.csv",
            "沪深基金_列表.csv",
        ):
            try:
                path = self.get_data_path(filename)
                if not os.path.exists(path):
                    continue
                with open(path, 'r', encoding='utf-8-sig') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        parts = line.split(',')
                        code = parts[0].strip().replace('﻿', '')
                        name = parts[1].strip() if len(parts) >= 2 else ""
                        if not code or self._is_csv_header_row(code, name):
                            continue
                        normalized_code = code.upper()
                        if normalized_code in seen_codes:
                            continue
                        seen_codes.add(normalized_code)
                        entries.append((normalized_code, name))
            except Exception:
                continue
        return entries

    @staticmethod
    def _normalize_name(text):
        """名称归一化：去除所有空白并转小写，便于容错匹配。"""
        return ''.join(str(text).split()).casefold()

    def _resolve_stock_input(self, raw):
        """将用户输入解析为 (code, name)。

        支持三种输入：
        - 完整代码（000001.SZ / 600000.SH）
        - 6 位纯数字代码（自动按本地列表补全交易所后缀）
        - 股票名称（精确优先，其次模糊包含；多结果弹窗选择）

        返回 (code, name)；无法解析时返回 (None, None)。
        """
        import re
        raw = raw.strip()
        if not raw:
            return None, None

        upper = raw.upper()
        entries = self._load_all_stock_entries()
        code_to_name = {c: n for c, n in entries}

        # 1) 完整代码
        if re.match(r'^\d{6}\.(SH|SZ)$', upper):
            return upper, code_to_name.get(upper, "")

        # 2) 6 位纯数字（缺交易所后缀）—— 用本地列表补全
        if re.match(r'^\d{6}$', upper):
            matched = [(c, n) for c, n in entries if c.split('.')[0] == upper]
            if len(matched) == 1:
                return matched[0]
            if len(matched) > 1:
                return self._pick_from_matches(matched)
            QMessageBox.warning(
                self, "格式错误",
                f"未在本地股票列表中找到代码 {upper}，\n"
                "请补全交易所后缀，例如 {0}.SZ 或 {0}.SH".format(upper))
            return None, None

        # 3) 按名称解析
        if not entries:
            QMessageBox.warning(self, "无法解析",
                "本地股票列表文件缺失，无法按名称查找，请输入完整股票代码。")
            return None, None

        norm = self._normalize_name(raw)
        exact = [(c, n) for c, n in entries if self._normalize_name(n) == norm]
        if exact:
            return exact[0] if len(exact) == 1 else self._pick_from_matches(exact)

        partial = [(c, n) for c, n in entries if norm in self._normalize_name(n)]
        if not partial:
            QMessageBox.warning(self, "未找到",
                f"未找到与“{raw}”匹配的股票代码或名称。")
            return None, None
        if len(partial) == 1:
            return partial[0]
        # 结果过多时截断，避免选择框过长
        return self._pick_from_matches(partial[:50])

    def _ask_text(self, title, label, default=""):
        """文本输入弹窗（深色标题栏），返回 (text, ok)。"""
        dlg = QInputDialog(self)
        dlg.setWindowTitle(title)
        dlg.setLabelText(label)
        dlg.setInputMode(QInputDialog.TextInput)
        dlg.setTextValue(default)
        self.apply_dark_titlebar(dlg)
        ok = bool(dlg.exec_())
        return dlg.textValue(), ok

    def _ask_item(self, title, label, items):
        """下拉选择弹窗（深色标题栏），返回 (text, ok)。"""
        dlg = QInputDialog(self)
        dlg.setWindowTitle(title)
        dlg.setLabelText(label)
        dlg.setComboBoxEditable(False)
        dlg.setComboBoxItems(items)
        self.apply_dark_titlebar(dlg)
        ok = bool(dlg.exec_())
        return dlg.textValue(), ok

    def _pick_from_matches(self, matches):
        """多结果时弹出选择框，返回选中的 (code, name)。"""
        display = [f"{c}  {n}" for c, n in matches]
        choice, ok = self._ask_item(
            "选择股票", "匹配到多只股票，请选择：", display)
        if not ok or not choice:
            return None, None
        idx = display.index(choice)
        return matches[idx]

    def add_single_stock(self):
        """手动添加单只股票（支持输入股票代码或股票名称）"""
        try:
            # 弹出输入对话框
            user_input, ok = self._ask_text(
                "添加股票",
                "请输入股票代码或名称\n"
                "（例如：600000.SH、000001.SZ，或 平安银行）:"
            )

            if ok and user_input.strip():
                code, name = self._resolve_stock_input(user_input)
                if not code:
                    return

                code = code.upper()

                # 解析后再做一次代码格式校验（兜底）
                if not self.validate_stock_code(code):
                    QMessageBox.warning(self, "格式错误",
                        f"解析得到的股票代码 {code} 格式不正确。")
                    return

                # 检查是否已存在
                for row in range(self.stock_list.rowCount()):
                    if self.stock_list.item(row, 0).text() == code:
                        QMessageBox.information(self, "提示", f"股票 {code} 已存在于列表中")
                        return

                # 名称未知时，让用户补充输入（可选）
                if not name:
                    input_name, ok_name = self._ask_text(
                        "股票名称",
                        f"未找到股票 {code} 的名称，请输入股票名称（可选）:"
                    )
                    if ok_name:
                        name = input_name.strip()

                # 添加到表格
                row = self.stock_list.rowCount()
                self.stock_list.insertRow(row)
                self.stock_list.setItem(row, 0, QTableWidgetItem(code))
                self.stock_list.setItem(row, 1, QTableWidgetItem(name))

                # 如果该股票在删除集合中，从删除集合中移除（用户手动添加回来了）
                if code in self.deleted_stocks:
                    self.deleted_stocks.discard(code)

                # 选中新添加的行
                self.stock_list.selectRow(row)

                self.update_status(f"已添加股票: {code} {name}".strip())

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
            default_dir = get_stock_pool_write_dir(create=True)
            
            file_name, _ = QFileDialog.getOpenFileName(
                self,
                "选择股票列表文件",
                default_dir,  # 设置默认目录
                "CSV Files (*.csv);;Text Files (*.txt)"
            )
            
            if file_name:
                rows = self._read_stock_rows_from_file(file_name)
                existing_codes = self._get_stock_table_codes()
                rows_to_add = [
                    (code, name)
                    for code, name in rows
                    if code not in existing_codes
                ]
                added_count = self._append_stock_list_rows(rows_to_add)

                # 用户显式导入相当于手动添加回来，移除删除标记。
                for code, _ in rows_to_add:
                    self.deleted_stocks.discard(code)
                
                if added_count > 0:
                    self.update_status(f"已导入 {added_count} 只股票: {os.path.basename(file_name)}")
                else:
                    self.update_status(f"导入完成，但所有股票已存在于列表中: {os.path.basename(file_name)}")
                
        except Exception as e:
            error_msg = f"导入股票列表时出错: {str(e)}"
            self.update_status(error_msg)
            logging.error(error_msg)
            QMessageBox.critical(self, "错误", error_msg)

    def delete_selected_stocks(self):
        """删除选中的股票"""
        selected_rows = set(item.row() for item in self.stock_list.selectedItems())
        
        # 收集要删除的股票代码
        deleted_codes = []
        for row in sorted(selected_rows):
            code = self.stock_list.item(row, 0).text()
            if code:
                deleted_codes.append(code)
        
        for row in sorted(selected_rows, reverse=True):
            self.stock_list.removeRow(row)
        
        stock_codes = []
        seen_codes = set()
        for row in range(self.stock_list.rowCount()):
            item = self.stock_list.item(row, 0)
            code = item.text().strip() if item else ""
            if code and code not in seen_codes:
                seen_codes.add(code)
                stock_codes.append(code)
        
        if "data" not in self.config:
            self.config["data"] = {}
        self.config["data"]["stock_list"] = stock_codes
        if "stock_list_file" in self.config["data"]:
            del self.config["data"]["stock_list_file"]
        
        # 记录被删除的股票到删除集合中（用于防止从股票池中重新添加）
        for code in deleted_codes:
            self.deleted_stocks.add(code)
        
        if stock_codes:
            self.update_status(f"已删除选中股票，当前股票池共{len(stock_codes)}只股票")
        else:
            self.update_status("已删除所有股票")

    def clear_stock_list(self):
        """清空股票列表和取消所有股票池的勾选"""
        # 清空股票列表
        self._set_stock_list_rows([])
        
        # 取消所有股票池的勾选
        for checkbox in self.pool_checkboxes.values():
            was_blocked = checkbox.blockSignals(True)
            try:
                checkbox.setChecked(False)
            finally:
                checkbox.blockSignals(was_blocked)
        
        self.update_status("已清空股票列表和股票池选择")

    def on_pool_changed(self, code, state):
        """股票池选择变化时的处理"""
        try:
            if state == Qt.Checked:
                # 检查是否选中了T0型ETF，如果是则弹出警告
                if code in ['t0_etf', 't0_stock_etf']:
                    warning_msg = "提示：T0型ETF和T0股票型ETF清单可能存在滞后，请仔细甄别后使用。\n\n这些清单基于历史数据整理，实际交易规则可能已发生变化，请以交易所最新公告为准。"
                    QMessageBox.warning(self, "数据滞后提示", warning_msg)
                
                # 获取对应的股票列表文件
                pool_file = self._get_pool_file(code)
                if pool_file:
                    file_path = self.get_data_path(pool_file)
                    if os.path.exists(file_path):
                        rows = [
                            (stock_code, stock_name)
                            for stock_code, stock_name in self._read_stock_rows_from_file(file_path, require_name=True)
                            if stock_code not in self.deleted_stocks
                        ]
                        added_count = self._append_stock_list_rows(rows)
                        
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
                                for stock_code, stock_name in self._read_stock_rows_from_file(file_path, require_name=True):
                                    if stock_code not in self.deleted_stocks:
                                        stocks_to_keep.add((stock_code, stock_name))
                
                # 清空并重新填充表格
                self._set_stock_list_rows(sorted(stocks_to_keep))
                
                self.update_status(f"更新后保留{len(stocks_to_keep)}只股票")
                
        except Exception as e:
            error_msg = f"更新股票池时出错: {str(e)}"
            self.update_status(error_msg)
            logging.error(error_msg)

    def _get_pool_file(self, code):
        """获取股票池对应的文件名"""
        definition = DESKTOP_CODE_INDEX.get(code)
        return definition.aliases[0] if definition else None

    def open_custom_list(self):
        """打开自选清单文件"""
        try:
            file_path = get_custom_stock_pool_path(for_write=True)
            
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
                try:
                    os.startfile(file_path)
                    self.update_status(f"已打开自选清单文件: {file_path}")
                except OSError as open_error:
                    self.log_message(f"打开自选清单文件失败: {open_error}", "WARNING")
                    QMessageBox.warning(self, "错误", f"打开自选清单文件失败：{open_error}\n文件位置：{file_path}")
                return

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
        selected_before = set()
        try:
            selected_before = {
                field for field, cb in self.fields_checkboxes.items() if cb.isChecked()
            }
        except Exception:
            selected_before = set()

        configured_fields = None
        try:
            data_fields = (getattr(self, "config", {}) or {}).get("data", {}).get("fields")
            if isinstance(data_fields, list) and data_fields:
                configured_fields = set(data_fields)
        except Exception:
            configured_fields = None

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
            if configured_fields is not None:
                cb.setChecked(field_code in configured_fields)
            elif selected_before:
                cb.setChecked(field_code in selected_before)
            else:
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
            
            # 列宽自适应较重，运行中高频状态更新时做节流
            now_ts = time.time()
            last_resize_ts = getattr(self, '_last_status_table_resize_ts', 0.0)
            if self.status_table.rowCount() <= 3 or now_ts - last_resize_ts >= 1.0:
                self.status_table.resizeColumnsToContents()
                self._last_status_table_resize_ts = now_ts
            
        except Exception as e:
            print(f"更新状态表格时出错: {str(e)}")


    def _apply_slippage_input_mode(self, slippage_type, *, save_current=True, log_change=True):
        """切换滑点输入单位，同时保留离开单位的独立值。"""
        normalized_type = "tick" if slippage_type == "tick" else "ratio"
        cache = getattr(self, "_slippage_value_cache", None)
        if not isinstance(cache, dict):
            cache = dict(_SLIPPAGE_UI_DEFAULTS)
            self._slippage_value_cache = cache

        previous_type = getattr(self, "_active_slippage_type", None)
        if save_current and previous_type in {"tick", "ratio"}:
            cache[previous_type] = _normalize_slippage_ui_value(
                previous_type,
                self.slippage_value.text(),
            )

        if normalized_type == "tick":
            self.slippage_value.setValidator(QIntValidator(0, 100))
            self.slippage_value.setPlaceholderText("请输入跳数(0-100)")
            self.slippage_value.setToolTip(
                "0 表示不加跳；每跳按配置中的 tick_size 计算（默认0.01）"
            )
            log_text = "已切换到按最小变动价跳数模式，请输入整数跳数"
        else:
            self.slippage_value.setValidator(QDoubleValidator(0.0, 10.0, 4))
            self.slippage_value.setPlaceholderText("请输入双边总滑点比例(0-10)%")
            self.slippage_value.setToolTip(
                "输入双边总滑点百分比；买入和卖出分别按该比例的一半调整成交价"
            )
            log_text = "已切换到按成交金额比例模式，请输入双边总滑点百分比"

        cache[normalized_type] = _normalize_slippage_ui_value(
            normalized_type,
            cache.get(normalized_type),
        )
        self.slippage_value.setText(cache[normalized_type])
        self._active_slippage_type = normalized_type
        if log_change:
            self.log_message(log_text, "INFO")

    def _load_slippage_settings(self, slippage):
        """从配置加载两套滑点值，并显示配置指定的单位。"""
        normalized = _normalize_slippage_config(slippage)
        slippage_type = normalized["type"]
        self._slippage_tick_size = normalized["tick_size"]

        # 两种单位分别加载、分别缓存。切换类型时只恢复该类型自己的值，
        # 绝不能把“2 跳”的数字直接解释成“2%”。
        cache = dict(_SLIPPAGE_UI_DEFAULTS)
        cache["tick"] = _normalize_slippage_ui_value(
            "tick", normalized["tick_count"]
        )
        ratio_percent = normalized["ratio"] * 100
        cache["ratio"] = _normalize_slippage_ui_value("ratio", ratio_percent)
        self._slippage_value_cache = cache

        self.slippage_type.blockSignals(True)
        self.slippage_type.setCurrentText(_SLIPPAGE_TYPE_TO_LABEL[slippage_type])
        self.slippage_type.blockSignals(False)
        self._apply_slippage_input_mode(
            slippage_type,
            save_current=False,
            log_change=False,
        )
        return normalized

    def slippage_type_changed(self, text):
        """滑点类型变更时恢复该单位上次使用的值。"""
        try:
            slippage_type = _SLIPPAGE_LABEL_TO_TYPE.get(text, "ratio")
            self._apply_slippage_input_mode(slippage_type)
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
                            self.update_progress_bar(progress_value)
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
                    self._queue_log_display(formatted_message)
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

    def _queue_log_display(self, formatted_message):
        """将日志HTML加入UI批量刷新队列，避免高频日志逐条重绘。"""
        try:
            if not hasattr(self, '_pending_log_html'):
                self._pending_log_html = []
            self._pending_log_html.append(formatted_message)

            if not getattr(self, '_log_flush_scheduled', False):
                self._log_flush_scheduled = True
                interval = getattr(self, '_log_display_interval_ms', 50)
                QTimer.singleShot(interval, self._flush_pending_log_display)
        except Exception as e:
            print(f"加入日志刷新队列时出错: {str(e)}")

    def _flush_pending_log_display(self):
        """批量刷新待显示日志，降低QTextEdit重排次数。"""
        try:
            self._log_flush_scheduled = False
            if not hasattr(self, 'log_text') or not getattr(self, '_pending_log_html', None):
                return

            batch_size = getattr(self, '_log_display_batch_size', 200)
            pending = self._pending_log_html
            batch = pending[:batch_size]
            del pending[:batch_size]

            if batch:
                self.log_text.setUpdatesEnabled(False)
                cursor = self.log_text.textCursor()
                cursor.movePosition(QTextCursor.End)
                cursor.insertHtml(''.join(batch))
                self.log_text.setTextCursor(cursor)
                self._trim_log_lines()
                self.log_text.verticalScrollBar().setValue(
                    self.log_text.verticalScrollBar().maximum()
                )
                self.log_text.setUpdatesEnabled(True)

            if pending:
                self._log_flush_scheduled = True
                interval = getattr(self, '_log_display_interval_ms', 50)
                QTimer.singleShot(interval, self._flush_pending_log_display)
        except Exception as e:
            try:
                if hasattr(self, 'log_text'):
                    self.log_text.setUpdatesEnabled(True)
            except Exception:
                pass
            print(f"批量刷新日志时出错: {str(e)}")

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
        if hasattr(self, '_pending_log_html'):
            self._pending_log_html.clear()
        self._log_flush_scheduled = False
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
                os.path.join(LOGS_DIR, f"log_{int(time.time())}.txt"),
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

    def apply_dark_titlebar(self, widget):
        if sys.platform == 'win32':
            try:
                from ctypes import windll, c_int, byref, sizeof
                from ctypes.wintypes import DWORD
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                DWMWA_CAPTION_COLOR = 35
                windll.dwmapi.DwmSetWindowAttribute(
                    int(widget.winId()),
                    DWMWA_USE_IMMERSIVE_DARK_MODE,
                    byref(c_int(2)),
                    sizeof(c_int)
                )
                caption_color = DWORD(0x333333)
                windll.dwmapi.DwmSetWindowAttribute(
                    int(widget.winId()),
                    DWMWA_CAPTION_COLOR,
                    byref(caption_color),
                    sizeof(caption_color)
                )
            except Exception:
                pass

    def show_error_dialog(self, title, message, details=None):
        """显示错误弹窗"""
        msg_box = QMessageBox(self)
        self.apply_dark_titlebar(msg_box)
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

    def _check_data_integrity_before_backtest(self, runtime_config=None) -> bool:
        """
        在回测开始前检查股票池数据的完整性（使用后台线程和进度对话框）

        Returns:
            bool: True表示数据完整或用户选择继续，False表示用户取消回测
        """
        try:
            cfg = runtime_config or getattr(self, "_current_runtime_config", None) or self.config

            # 检查是否启用数据完整性检查
            check_enabled = self.settings.value('check_data_integrity', True, type=bool)
            if not check_enabled:
                self.log_message("数据完整性检查已禁用（可在设置中启用）", "INFO")
                return True

            import khQTTools

            # 获取股票池
            stock_list = cfg.get("data", {}).get("stock_list", [])
            if not stock_list:
                self.log_message("股票池为空，跳过数据完整性检查", "WARNING")
                return True

            # 获取数据周期
            period = cfg.get("data", {}).get("kline_period", "1d")
            periods = [period]  # 只检查当前选择的周期

            # 准备检查列表和各股票对应的周期
            check_list = stock_list.copy()
            stock_periods = {stock: periods for stock in stock_list}

            # 获取基准合约
            benchmark = cfg.get("backtest", {}).get("benchmark", "")
            if benchmark:
                # 标准化基准合约代码
                benchmark = khQTTools.normalize_stock_code(benchmark)
                # 将基准合约加入检查列表（如果不在股票池中）
                if benchmark not in check_list:
                    check_list.append(benchmark)
                    self.log_message(f"基准合约 {benchmark} 将被加入数据完整性检查", "INFO")
                    stock_periods[benchmark] = ['1d']
                else:
                    # 如果基准合约同时也是交易标的，它既需要检查交易周期，也需要检查日线
                    if '1d' not in stock_periods[benchmark]:
                        stock_periods[benchmark] = stock_periods[benchmark] + ['1d']
            else:
                self.log_message("未设置基准合约", "WARNING")

            # 获取回测时间范围
            start_date = cfg.get("backtest", {}).get("start_time", "")
            end_date = cfg.get("backtest", {}).get("end_time", "")
            if not start_date or not end_date:
                self.log_message("回测时间范围未设置，跳过数据完整性检查", "WARNING")
                return True

            check_mode = self.settings.value('check_data_integrity_mode', 'auto')
            decision = should_run_integrity_check(
                mode=check_mode,
                legacy_enabled=check_enabled,
                stock_count=len(check_list),
                period=period,
                start_date=start_date,
                end_date=end_date,
            )
            if not decision.should_run:
                if decision.reason == "disabled":
                    self.log_message("数据完整性检查已禁用（可在设置中启用）", "INFO")
                else:
                    days_text = (
                        f"{decision.estimated_trading_days} 个交易日"
                        if decision.estimated_trading_days is not None else "未知交易日数"
                    )
                    bars_text = (
                        f"，估算约 {decision.estimated_bars:,} 条待扫描K线"
                        if decision.estimated_bars is not None else ""
                    )
                    self.log_message(
                        "数据完整性检查采用自动模式：检测到大规模回测 "
                        f"({decision.stock_count} 只股票，周期 {decision.period or period}，{days_text}{bars_text})，"
                        "已跳过 GUI 前置全量扫描；回测过程中仍会汇总提示空数据/缺历史数据。",
                        "INFO",
                    )
                return True

            # 获取DuckDB数据路径
            duckdb_data_path = self.settings.value('duckdb_data_path', '')
            if not duckdb_data_path or not os.path.exists(duckdb_data_path):
                self.log_message("DuckDB数据路径未设置或不存在，跳过数据完整性检查", "WARNING")
                return True

            self.log_message(f"正在检查 {len(check_list)} 只股票的数据完整性（含基准合约）...", "INFO")

            # 创建进度对话框
            progress_dialog = QProgressDialog(
                "正在检查数据完整性...\n"
                "注意：仅检查股票池在回测时间段和周期的数据完整性\n"
                "策略若需更早历史数据（如均线计算），请自行确认",
                "取消",
                0,
                len(check_list),
                self
            )
            progress_dialog.setWindowTitle("数据完整性检查")
            progress_dialog.setWindowModality(Qt.ApplicationModal)
            progress_dialog.setMinimumDuration(0)  # 立即显示
            progress_dialog.setAutoClose(False)
            progress_dialog.setAutoReset(False)

            # 创建检查线程
            self.integrity_check_thread = IntegrityCheckThread(
                stock_list=check_list,
                periods=periods,
                start_date=start_date,
                end_date=end_date,
                duckdb_data_path=duckdb_data_path,
                stock_periods=stock_periods,
                dividend_type=cfg.get("data", {}).get("dividend_type", "none")
            )

            # 用于存储检查结果
            check_result = {'completed': False, 'result': None, 'error': None}
            last_integrity_progress = {'ts': 0.0, 'current': -1}

            # 连接进度信号
            def update_progress(current, total, message, task_count):
                now_ts = time.time()
                should_update = (
                    current >= total
                    or current != last_integrity_progress['current']
                    and now_ts - last_integrity_progress['ts'] >= 0.1
                )
                if not should_update:
                    return

                last_integrity_progress['ts'] = now_ts
                last_integrity_progress['current'] = current
                progress_dialog.setMaximum(total)
                progress_dialog.setValue(current)
                progress_dialog.setLabelText(
                    f"{message}\n"
                    f"已发现 {task_count} 处缺失数据\n"
                    f"（提示：可在设置中关闭数据完整性检查）"
                )

            self.integrity_check_thread.progress_signal.connect(update_progress)

            # 连接完成信号
            def on_check_finished(result):
                check_result['completed'] = True
                check_result['result'] = result
                progress_dialog.close()

            self.integrity_check_thread.finished_signal.connect(on_check_finished)

            # 连接错误信号
            def on_check_error(error_msg):
                check_result['completed'] = True
                check_result['error'] = error_msg
                progress_dialog.close()

            self.integrity_check_thread.error_signal.connect(on_check_error)

            # 连接取消按钮
            def on_cancel():
                self.integrity_check_thread.stop()
                progress_dialog.setLabelText("正在取消检查...")

            progress_dialog.canceled.connect(on_cancel)

            # 如果数据管理窗口(DuckDBViewer)已打开，要求其释放连接池以免扫描时抛出锁冲突异常
            if hasattr(self, 'duckdb_viewer_window') and self.duckdb_viewer_window:
                if hasattr(self.duckdb_viewer_window, 'manager') and self.duckdb_viewer_window.manager:
                    mgr = self.duckdb_viewer_window.manager
                    if hasattr(mgr, 'close_all_no_checkpoint'):
                        mgr.close_all_no_checkpoint()
                    elif hasattr(mgr, 'close_all'):
                        mgr.close_all()

            # 启动线程
            self.integrity_check_thread.start()

            # 显示进度对话框（阻塞等待）
            progress_dialog.exec_()

            # 处理错误
            if check_result.get('error'):
                self.log_message(f"数据完整性检查时发生异常: {check_result['error']}", "WARNING")
                return True  # 发生异常时仍允许继续

            # 处理取消
            if not check_result['completed']:
                if getattr(self, 'integrity_check_thread', None) is not None and self.integrity_check_thread.isRunning():
                    self.integrity_check_thread.stop()
                self.log_message("数据完整性检查被用户取消", "INFO")
                return False

            result = check_result.get('result')
            if not result:
                return False

            # 分析结果
            missing_tasks = result.get('missing_tasks', [])

            if not missing_tasks:
                self.log_message("数据完整性检查通过，所有股票数据完整", "INFO")
                return True

            # 统计缺失情况
            missing_stocks = set()
            benchmark_missing = False
            total_missing_days = 0
            for task in missing_tasks:
                stock_code = task['stock']
                missing_stocks.add(stock_code)
                total_missing_days += task.get('missing_days', 1)
                # 检查是否为基准合约
                if benchmark and stock_code == benchmark:
                    benchmark_missing = True

            # 构建提示信息
            msg_lines = []

            # 特别提示基准合约缺失
            if benchmark_missing:
                msg_lines.extend([
                    "⚠️ 警告：基准合约数据缺失！",
                    f"基准合约 {benchmark} 的 {period} 周期数据不完整",
                    "这将严重影响回测收益率计算和基准对比",
                    ""
                ])

            msg_lines.extend([
                f"检测到 {len(missing_stocks)} 只股票的 {period} 周期数据不完整",
                f"共缺失约 {total_missing_days} 个交易日的数据",
                "",
                f"时间范围: {start_date[:4]}-{start_date[4:6]}-{start_date[6:]} ~ {end_date[:4]}-{end_date[4:6]}-{end_date[6:]}",
                "",
                "缺失数据明细已在下方列表展示",
                "",
                "建议：",
                "1. 打开「数据管理模块」补充缺失数据",
                "2. 或者缩小回测时间范围",
                "",
                "是否仍要继续回测？（可能导致回测结果不准确）"
            ])

            self.log_message(f"数据完整性检查发现 {len(missing_stocks)} 只股票数据不完整", "WARNING")

            detail_text = ""
            try:
                from collections import defaultdict

                stock_tasks_map = defaultdict(list)
                for task in missing_tasks:
                    stock_tasks_map[task.get('stock', '')].append(task)

                detail_lines = []
                for idx, stock in enumerate(sorted(stock_tasks_map.keys())):
                    tasks_for_stock = stock_tasks_map[stock]
                    # 股票名称
                    try:
                        stock_name = khQTTools.get_stock_name(stock)
                        header = f"{idx + 1}. {stock} ({stock_name})"
                    except Exception:
                        header = f"{idx + 1}. {stock}"
                    detail_lines.append(header)

                    # 该股票各周期、各缺失区间
                    def _fmt_date(d):
                        if isinstance(d, str) and len(d) == 8 and d.isdigit():
                            return f"{d[:4]}-{d[4:6]}-{d[6:]}"
                        return str(d)

                    for t in sorted(
                        tasks_for_stock,
                        key=lambda x: (x.get('period', ''), x.get('start', '')),
                    ):
                        p = t.get('period', period)
                        s = t.get('start')
                        e = t.get('end')
                        cnt = int(t.get('missing_days', 1))
                        s_txt = _fmt_date(s)
                        e_txt = _fmt_date(e)

                        if s and e and s != e:
                            line = f"    - 周期 {p}: {s_txt} ~ {e_txt}，缺失 {cnt} 个交易日"
                        else:
                            line = f"    - 周期 {p}: {s_txt}，缺失 {cnt} 个交易日"
                        detail_lines.append(line)

                    detail_lines.append("")  # 股票之间空一行

                detail_text = "\n".join(detail_lines).strip()
            except Exception:
                pass
            dialog = QDialog(self)
            dialog.setWindowTitle("数据完整性检查")
            dialog.setWindowModality(Qt.ApplicationModal)
            dialog.setMinimumWidth(680)
            if sys.platform == 'win32':
                try:
                    from ctypes import windll, c_int, byref, sizeof
                    from ctypes.wintypes import DWORD
                    DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                    DWMWA_CAPTION_COLOR = 35
                    windll.dwmapi.DwmSetWindowAttribute(
                        int(dialog.winId()),
                        DWMWA_USE_IMMERSIVE_DARK_MODE,
                        byref(c_int(2)),
                        sizeof(c_int)
                    )
                    caption_color = DWORD(0x333333)
                    windll.dwmapi.DwmSetWindowAttribute(
                        int(dialog.winId()),
                        DWMWA_CAPTION_COLOR,
                        byref(caption_color),
                        sizeof(caption_color)
                    )
                except Exception:
                    pass

            dialog_layout = QVBoxLayout(dialog)
            summary_label = QLabel("\n".join(msg_lines))
            summary_label.setWordWrap(True)
            dialog_layout.addWidget(summary_label)

            detail_label = QLabel("缺失数据明细列表")
            dialog_layout.addWidget(detail_label)

            detail_box = QTextEdit()
            detail_box.setReadOnly(True)
            detail_box.setText(detail_text if detail_text else "无")
            detail_box.setFixedHeight(260)
            dialog_layout.addWidget(detail_box)

            button_layout = QHBoxLayout()
            open_data_button = QPushButton("打开数据管理")
            cancel_button = QPushButton("取消")
            continue_button = QPushButton("继续回测")
            button_layout.addWidget(open_data_button)
            button_layout.addStretch()
            button_layout.addWidget(cancel_button)
            button_layout.addWidget(continue_button)
            dialog_layout.addLayout(button_layout)

            result = {"action": None}

            def on_open_data():
                result["action"] = "open_data"
                dialog.reject()

            def on_cancel():
                result["action"] = "cancel"
                dialog.reject()

            def on_continue():
                result["action"] = "continue"
                dialog.accept()

            open_data_button.clicked.connect(on_open_data)
            cancel_button.clicked.connect(on_cancel)
            continue_button.clicked.connect(on_continue)
            cancel_button.setDefault(True)

            dialog.exec_()

            if result["action"] == "open_data":
                self._open_data_manager_for_supplement()
                return False
            if result["action"] == "continue":
                self.log_message("用户选择继续回测（数据可能不完整）", "WARNING")
                return True
            return False

        except Exception as e:
            self.log_message(f"数据完整性检查时发生异常: {str(e)}", "WARNING")
            import traceback
            traceback.print_exc()
            return True  # 出错时默认允许继续

    def _open_data_manager_for_supplement(self):
        """打开数据管理模块用于数据补充"""
        try:
            duckdb_data_path = self._ensure_duckdb_data_path()
            if not duckdb_data_path:
                QMessageBox.warning(self, "错误", "未设置DuckDB数据路径，请先在设置中配置")
                return

            from duckdb_storage.viewer import DuckDBViewer
            history_source = self.settings.value('history_import_source', None)
            self.data_viewer = DuckDBViewer(
                data_root=duckdb_data_path,
                read_only=False,
                history_import_source=history_source,
            )
            self.data_viewer.show()
            self.log_message("已打开数据管理模块", "INFO")
        except Exception as e:
            self.log_message(f"打开数据管理模块失败: {str(e)}", "ERROR")

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
                temp_paths = [getattr(self, 'temp_config_path', None)]
                if hasattr(self, 'strategy_thread') and self.strategy_thread:
                    temp_paths.extend(getattr(self.strategy_thread, 'temp_config_paths', []) or [])
                for temp_path in dict.fromkeys([p for p in temp_paths if p]):
                    if not os.path.exists(temp_path):
                        continue
                    try:
                        os.remove(temp_path)
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
            
            # 列宽自适应会扫描表格内容，高频状态更新时做节流
            now_ts = time.time()
            last_resize_ts = getattr(self, '_last_status_table_resize_ts', 0.0)
            if self.status_table.rowCount() <= 3 or now_ts - last_resize_ts >= 1.0:
                self.status_table.resizeColumnsToContents()
                self._last_status_table_resize_ts = now_ts
            
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
            old_window = getattr(self, 'result_window', None)
            if old_window is not None:
                try:
                    old_window.close()
                except RuntimeError:
                    pass
            # 确保窗口在主线程创建
            self.result_window = BacktestResultWindow(backtest_dir)
            result_window = self.result_window
            result_window.destroyed.connect(
                lambda _obj=None, target=result_window: self._forget_result_window(target)
            )
            
            # 先显示窗口，让Qt完成窗口的初始化
            self.result_window.show()
            QTimer.singleShot(0, lambda window=self.result_window: self._center_backtest_result_window(window))
            
            self.log_message("回测结果窗口已打开", "INFO")
        except Exception as e:
            self.log_message(f"显示回测结果窗口时出错: {str(e)}", "ERROR")
            import traceback
            self.log_message(traceback.format_exc(), "ERROR")

    def _forget_result_window(self, window):
        """只清除与销毁信号对应的结果窗口，避免旧窗口误清新引用。"""
        if getattr(self, 'result_window', None) is window:
            self.result_window = None

    def _center_backtest_result_window(self, window):
        """在下一轮事件里居中结果窗口，避免手动processEvents造成重入。"""
        try:
            if window is None or not window.isVisible():
                return
            if window.isMaximized() or window.windowState() & Qt.WindowMaximized:
                return

            screen = QDesktopWidget().availableGeometry()
            window_geometry = window.frameGeometry()
            x = screen.x() + (screen.width() - window_geometry.width()) // 2
            y = screen.y() + (screen.height() - window_geometry.height()) // 2
            window.move(x, y)
        except Exception as e:
            self.log_message(f"居中回测结果窗口时出错: {str(e)}", "WARNING")

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
        show_daily_trigger_cap = (index == 3)
        self.daily_trigger_cap_widget.setVisible(show_daily_trigger_cap)
        self.daily_trigger_cap_spin.setEnabled(show_daily_trigger_cap)

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
        slippage_type = _SLIPPAGE_LABEL_TO_TYPE.get(
            self.slippage_type.currentText(),
            "ratio",
        )
        cache = getattr(self, "_slippage_value_cache", dict(_SLIPPAGE_UI_DEFAULTS))
        cache[slippage_type] = _normalize_slippage_ui_value(
            slippage_type,
            self.slippage_value.text(),
        )
        cache["tick"] = _normalize_slippage_ui_value("tick", cache.get("tick"))
        cache["ratio"] = _normalize_slippage_ui_value("ratio", cache.get("ratio"))
        self._slippage_value_cache = cache
        self.slippage_value.setText(cache[slippage_type])
        normalized = _normalize_slippage_config({
            "type": slippage_type,
            "tick_size": getattr(self, "_slippage_tick_size", 0.01),
            "tick_count": int(cache["tick"]),
            # 配置保存小数；界面显示百分比。该值是双边总滑点，成交时单边取一半。
            "ratio": float(cache["ratio"]) / 100,
        })
        self._slippage_tick_size = normalized["tick_size"]
        return normalized
    
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
        seen_codes = set()
        
        # 添加选中的常用股票池中的股票代码
        for code, cb in self.pool_checkboxes.items():
            if cb.isChecked():
                pool_file = self._get_pool_file(code)
                if pool_file:
                    file_path = self.get_data_path(pool_file)
                    if os.path.exists(file_path):
                        for stock_code, _ in self._read_stock_rows_from_file(file_path):
                            if stock_code not in seen_codes:
                                seen_codes.add(stock_code)
                                stock_codes.append(stock_code)
        
        # 添加自定义股票列表中的股票代码
        for row in range(self.stock_list.rowCount()):
            item = self.stock_list.item(row, 0)
            code = item.text().strip() if item else ""
            if code and code not in seen_codes:
                seen_codes.add(code)
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
        for cb in self.pool_checkboxes.values():
            was_blocked = cb.blockSignals(True)
            try:
                cb.setChecked(False)
            finally:
                cb.blockSignals(was_blocked)
            
        # 从股票、ETF、LOF/场内基金的统一本地清单获取名称。
        stock_names = dict(self._load_all_stock_entries())
        
        # 添加到表格中
        rows = []
        seen_codes = set()
        for code in stock_list:
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            rows.append((code, stock_names.get(code, "--")))
        self._set_stock_list_rows(rows)


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
        if hasattr(self, '_pending_log_html'):
            self._pending_log_html.clear()
        self._log_flush_scheduled = False

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
            # 保存修改前的DuckDB路径
            old_duckdb_path = self.settings.value('duckdb_data_path', '')

            # 创建并显示设置对话框
            settings_dialog = SettingsDialog(self)
            result = settings_dialog.exec_()

            # 如果用户点击了保存按钮，更新延迟显示设置并强制更新配置
            if result == QDialog.Accepted:
                self.update_delay_log_setting()

                # 更新最大日志行数设置
                self.max_log_lines = self.settings.value('max_log_lines', 1000, type=int)
                self.log_message(f"设置已更新 - 最大日志行数: {self.max_log_lines}", "INFO")

                # 应用界面字号倍率（仅在倍率发生变化时才重新应用，避免不必要的样式重刷导致视觉跳变）
                scale = get_ui_font_scale(self.settings)
                old_scale = getattr(self, 'font_scale', None)
                if old_scale != scale:
                    apply_app_font(scale)
                    self.apply_ui_scale(scale)
                    try:
                        app = QApplication.instance()
                        if app:
                            for widget in app.topLevelWidgets():
                                if widget is self:
                                    continue
                                if hasattr(widget, "apply_ui_scale"):
                                    widget.apply_ui_scale(scale)
                    except Exception as e:
                        logging.warning(f"应用界面字号倍率时出错: {e}")

                # 检查DuckDB路径是否改变
                new_duckdb_path = self.settings.value('duckdb_data_path', '')
                if old_duckdb_path != new_duckdb_path:
                    self.log_message(f"DuckDB数据路径已更新: {new_duckdb_path}", "INFO")

                    # 如果DuckDB Viewer窗口已打开，关闭它
                    if hasattr(self, 'duckdb_viewer_window') and self.duckdb_viewer_window:
                        try:
                            self.log_message("检测到DuckDB数据路径改变，关闭当前DuckDB Viewer窗口", "INFO")
                            if self.duckdb_viewer_window.close():
                                self.log_message("请重新点击【DuckDB数据管理】按钮以使用新路径", "INFO")
                            else:
                                self.log_message(
                                    "数据管理窗口仍有任务或用户取消关闭；窗口安全关闭后再按新路径重新打开",
                                    "WARNING",
                                )
                        except Exception as e:
                            logging.warning(f"关闭DuckDB Viewer窗口时出错: {e}")

            # 如果需要，可以在这里处理设置对话框关闭后的操作
            self.check_software_status()


        except Exception as e:
            logging.error(f"显示设置对话框时出错: {str(e)}")
            self.show_error_dialog("设置错误", f"显示设置对话框时出错: {str(e)}")
            
    def update_delay_log_setting(self):
        """更新延迟显示日志设置"""
        try:
            # 从设置中重新读取延迟显示状态
            old_setting = getattr(self, 'delay_log_display', True)
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
        """初始化更新管理器（发现新版本时提示去下载，从不强制更新）"""
        self.update_manager = UpdateManager(self)
        self.update_manager.check_finished.connect(self.handle_update_check_finished)
        
        # 加载更新设置
        self.set_update_config()
        
    def set_update_config(self):
        """设置更新配置"""
        if not getattr(self, 'update_manager', None):
            return
        settings = QSettings(QT_ORG, QT_APP)
        self.update_manager.auto_check = settings.value('auto_check_update', True, type=bool)
        self.update_manager.update_channel = 'stable'

    
    def check_for_updates(self):
        """检查软件更新"""
        if not getattr(self, 'update_manager', None):
            return
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
        if not getattr(self, 'update_manager', None):
            return
        try:
            self.check_for_updates()
        except Exception as e:
            logging.error(f"延迟更新检查时出错: {str(e)}", exc_info=True)
    
    def show_current_version(self):
        """显示当前版本信息"""
        if not getattr(self, 'update_manager', None):
            try:
                from version import get_version_info as _vi
                v = _vi().get('version', 'unknown')
            except Exception:
                v = 'unknown'
            QMessageBox.information(self, "版本信息", f"{APP_NAME} v{v}")
            return
        # 直接使用UpdateManager的方法
        self.update_manager.show_current_version()


    def open_duckdb_viewer(self):
        """打开DuckDB数据管理界面"""
        try:
            benchmark_dialog = getattr(self, '_benchmark_dialog', None)
            if benchmark_dialog is not None and benchmark_dialog.is_running():
                QMessageBox.information(
                    self, "基准指数正在下载",
                    "首次启动的沪深300基准还在下载，完成后再打开数据管理。",
                )
                benchmark_dialog.show()
                benchmark_dialog.raise_()
                return

            # 记录日志
            self.log_message("正在打开DuckDB数据管理界面...", "INFO")

            # 从设置中读取DuckDB数据路径，并在受限环境下自动回退到可写目录
            duckdb_data_path = self._ensure_duckdb_data_path()

            if not duckdb_data_path:
                # 如果没有设置路径，提示用户先设置
                self.log_message("未设置DuckDB数据路径，请先在设置中配置", "WARNING")
                QMessageBox.warning(
                    self,
                    "未设置DuckDB数据路径",
                    "请先在【设置】中配置DuckDB数据路径。\n\n"
                    "路径设置后，点击【DuckDB数据管理】按钮即可打开数据管理界面。"
                )
                # 打开设置对话框
                self.show_settings()
                return

            # 检查路径是否存在
            if not os.path.exists(duckdb_data_path):
                self.log_message(f"DuckDB数据路径不存在: {duckdb_data_path}", "WARNING")
                reply = QMessageBox.question(
                    self,
                    "路径不存在",
                    f"DuckDB数据路径不存在:\n{duckdb_data_path}\n\n是否创建该目录？",
                    QMessageBox.Yes | QMessageBox.No
                )
                if reply == QMessageBox.Yes:
                    try:
                        os.makedirs(duckdb_data_path, exist_ok=True)
                        self.log_message(f"已创建DuckDB数据目录: {duckdb_data_path}", "INFO")
                    except Exception as e:
                        self.log_message(f"创建目录失败: {str(e)}", "ERROR")
                        QMessageBox.critical(self, "错误", f"创建目录失败:\n{str(e)}")
                        return
                else:
                    return

            # 检查是否已经创建了DuckDB Viewer窗口
            if hasattr(self, 'duckdb_viewer_window') and self.duckdb_viewer_window:
                # 如果窗口已存在，显示并激活它
                if hasattr(self.duckdb_viewer_window, "apply_ui_scale"):
                    self.duckdb_viewer_window.apply_ui_scale(get_ui_font_scale(self.settings))
                self.duckdb_viewer_window.show()
                self.duckdb_viewer_window.raise_()
                self.duckdb_viewer_window.activateWindow()
                self.log_message("DuckDB数据管理窗口已激活", "INFO")
                return

            # 导入DuckDB Viewer
            try:
                from duckdb_storage.viewer import DuckDBViewer
                # 创建新的DuckDB Viewer窗口，并传入数据路径
                self.duckdb_viewer_window = DuckDBViewer(data_root=duckdb_data_path)
                if hasattr(self.duckdb_viewer_window, "apply_ui_scale"):
                    self.duckdb_viewer_window.apply_ui_scale(get_ui_font_scale(self.settings))

                # 连接窗口关闭信号，当窗口被关闭时清除引用
                viewer_window = self.duckdb_viewer_window
                self.duckdb_viewer_window.destroyed.connect(
                    lambda _obj=None, target=viewer_window:
                    _dispatch_duckdb_viewer_destroyed(self, target)
                )

                self.duckdb_viewer_window.show()  # 显示窗口
                self.log_message(f"DuckDB数据管理界面已打开，数据路径: {duckdb_data_path}", "INFO")

            except ImportError as e:
                self.log_message(f"无法导入DuckDB Viewer模块: {str(e)}", "ERROR")
                QMessageBox.critical(
                    self,
                    "模块导入错误",
                    f"无法导入DuckDB Viewer模块:\n{str(e)}\n\n"
                    "请确保 duckdb_storage 模块已正确安装。"
                )
            except Exception as e:
                self.log_message(f"创建DuckDB Viewer失败: {str(e)}", "ERROR")
                logging.error(f"创建DuckDB Viewer失败", exc_info=True)
                QMessageBox.critical(
                    self,
                    "错误",
                    f"打开DuckDB数据管理界面时出错:\n{str(e)}"
                )

        except Exception as e:
            error_message = f"打开DuckDB数据管理界面时出错: {str(e)}"
            self.log_message(error_message, "ERROR")
            logging.error(error_message, exc_info=True)
            QMessageBox.critical(self, "错误", f"打开DuckDB数据管理界面时出错:\n{str(e)}")

    def open_history_manager(self):
        """打开回测历史管理器"""
        try:
            # 记录日志
            self.log_message("正在打开回测历史管理器...", "INFO")
            
            # 检查是否已经创建了回测历史管理器窗口
            if hasattr(self, 'history_manager_window') and self.history_manager_window:
                # 如果窗口已存在，最大化显示并激活它
                self.history_manager_window.showMaximized()
                self.history_manager_window.raise_()
                self.history_manager_window.activateWindow()
                # 自动刷新数据
                self.history_manager_window.load_results()
                self.log_message("回测历史管理器窗口已激活并刷新", "INFO")
                return
            
            # 创建新的回测历史管理器窗口
            if BacktestHistoryManager is not None:
                # 构造函数内部已经调用load_results()，这里不再重复调用
                # （之前重复调用导致所有结果目录被完整扫描两遍）
                self.history_manager_window = BacktestHistoryManager(self)
                # 连接窗口关闭信号，当窗口被关闭时清除引用
                self.history_manager_window.destroyed.connect(lambda: setattr(self, 'history_manager_window', None))
                self.history_manager_window.showMaximized()  # 最大化显示
                self.log_message("回测历史管理器已成功打开并刷新", "INFO")
            else:
                error_message = "回测历史管理器模块未正确导入"
                self.log_message(error_message, "ERROR")
                QMessageBox.critical(self, "错误", error_message)
                
        except Exception as e:
            error_message = f"打开回测历史管理器时出错: {str(e)}"
            self.log_message(error_message, "ERROR")
            logging.error(error_message, exc_info=True)
            QMessageBox.critical(self, "错误", f"打开回测历史管理器时出错:\n{str(e)}")

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

    def set_progress_label(self, label_text):
        """设置进度条的标签文字
        
        Args:
            label_text: 标签文字，如 "加载数据进度" 或 "回测进度"
        """
        self.progress_label_text = label_text
        self._last_progress_ui_label = None
    
    @pyqtSlot(int)
    def update_progress_bar(self, value):
        """更新进度条的值"""
        if self.progress_bar:
            # 确保值在0-100之间
            value = max(0, min(value, 100))

            # 初始化/重置最近进度历史（用于滑动窗口平均速度）
            # 当进度从0开始或发生回退时，认为是新阶段，重置历史
            if not hasattr(self, "_recent_progress_history"):
                from collections import deque
                self._recent_progress_history = deque(maxlen=200)

            now_ts = time.time()
            label_text = getattr(self, 'progress_label_text', '回测进度')
            last_ui_value = getattr(self, '_last_progress_ui_value', None)
            last_ui_label = getattr(self, '_last_progress_ui_label', None)
            last_ui_ts = getattr(self, '_last_progress_ui_update_ts', 0.0)
            min_interval = getattr(self, '_progress_update_min_interval', 0.2)

            force_update = (
                last_ui_value is None
                or value != last_ui_value
                or label_text != last_ui_label
            )
            if not force_update and value == last_ui_value and now_ts - last_ui_ts < min_interval:
                return

            self.progress_bar.setValue(value)
            self._last_progress_ui_value = value
            self._last_progress_ui_label = label_text
            self._last_progress_ui_update_ts = now_ts

            try:
                last_value = self._recent_progress_history[-1][1] if self._recent_progress_history else None
            except Exception:
                # 极端情况下，如果历史损坏则重建
                from collections import deque
                self._recent_progress_history = deque(maxlen=200)
                last_value = None

            if last_value is None or value <= last_value:
                # 新一轮进度（例如新任务或阶段），清空历史
                self._recent_progress_history.clear()

            # 记录当前进度点
            self._recent_progress_history.append((now_ts, value))

            if hasattr(self, 'progress_text') and self.progress_text:
                # 计算预计剩余时间（优先使用最近200条进度的平均速度）
                remaining_time_text = ""
                if value > 0:
                    remaining_time = None
                    # 用"全局平均速度"估剩余(已耗时 / 已完成比例 - 已耗时)。
                    # 不用瞬时/近窗速度: 全A分钟线分段加载忽快忽慢(换段重载几分钟、段内飞快),
                    # 瞬时速度会让"预计剩余"在 0 和很大之间乱跳、且段内偏低; 全局平均稳, 随进度单调收敛到真实值。
                    if hasattr(self, 'backtest_start_time'):
                        elapsed_time = now_ts - self.backtest_start_time
                        if elapsed_time > 0:
                            estimated_total_time = elapsed_time / (value / 100.0)
                            remaining_time = max(0.0, estimated_total_time - elapsed_time)

                    # 格式化剩余时间
                    if remaining_time is not None and remaining_time > 0:
                        hours = int(remaining_time // 3600)
                        minutes = int((remaining_time % 3600) // 60)
                        seconds = int(remaining_time % 60)
                        remaining_time_text = f" | 预计剩余: {hours:02d}:{minutes:02d}:{seconds:02d}"

                # 更新文本
                self.progress_text.setText(f"{label_text}: {value}%{remaining_time_text}")
            
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
            strategy_file_for_save = self._strategy_file_value_for_config(file_path)

            # 构建配置字典
            config = {
                "run_mode": self.get_run_mode(),
                "account": {
                    "account_id": self.settings.value('account_id', ''),
                    "account_type": self.settings.value('account_type', 'STOCK')
                },
                "strategy_file": strategy_file_for_save,
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
                        "interval": self.interval_spin.value(),
                        "daily_trigger_cap": self.daily_trigger_cap_spin.value()
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

            config = self._prepare_config_for_save(config)
            
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
            self.setWindowTitle(f"{WINDOW_TITLE} - {file_name}")
            
            # 记录日志
            self.log_message(f"配置已保存到: {file_path}", "INFO")
            
            # 检测文件是否在危险位置
            strategy_file_path = self._resolve_strategy_file_path(strategy_file_for_save, config_path=file_path)
            self.show_internal_dir_warning(file_path, strategy_file_path)
            
            # 显示成功消息
            QMessageBox.information(self, "保存成功", f"配置已保存到: {file_path}")
            
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"保存配置文件时出错: {str(e)}")

    def _prepare_config_for_save(self, config):
        """Preserve explicit strategy overrides and drop runtime-only fields."""
        old_config = getattr(self, "config", {}) or {}
        prepared = preserve_strategy_runtime_blocks(config, old_config)
        old_backtest = old_config.get("backtest", {}) if isinstance(old_config, dict) else {}
        new_backtest = prepared.get("backtest", {})
        if isinstance(old_backtest, dict) and isinstance(new_backtest, dict):
            new_backtest["trade_cost"] = _merge_trade_cost_config(
                old_backtest.get("trade_cost"),
                new_backtest.get("trade_cost"),
            )
        return prepared

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
                strategy_file_for_save = self._strategy_file_value_for_config(self.current_config_file)

                # 构建配置字典
                config = {
                    "run_mode": self.get_run_mode(),
                    "account": {
                        "account_id": self.settings.value('account_id', ''),
                        "account_type": self.settings.value('account_type', 'STOCK')
                    },
                    "strategy_file": strategy_file_for_save,
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
                            "interval": self.interval_spin.value(),
                            "daily_trigger_cap": self.daily_trigger_cap_spin.value()
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

                config = self._prepare_config_for_save(config)
                
                # 保存到当前配置文件
                with open(self.current_config_file, 'w', encoding='utf-8') as f:
                    json.dump(config, f, indent=4, ensure_ascii=False)
                    
                # 保存到实例变量
                self.config = config
                
                # 记录日志
                self.log_message(f"配置已保存到: {self.current_config_file}", "INFO")
                
                # 检测文件是否在危险位置
                strategy_file_path = self._resolve_strategy_file_path(
                    strategy_file_for_save,
                    config_path=self.current_config_file,
                )
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
            self.apply_dark_titlebar(msg_box)
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

    def run_first_run_guide(self):
        """首次启动引导：选数据目录、导入 V2.1 设置、补沪深300基准，数据为空时引导去下载。"""
        from kh_first_run import BenchmarkDownloadDialog, FirstRunDialog, needs_first_run

        if not needs_first_run(self.settings):
            return
        dialog = FirstRunDialog(self, self.settings)
        self.apply_dark_titlebar(dialog)
        if dialog.exec_() != QDialog.Accepted:
            self.log_message("已跳过首次启动引导，下次启动时会再次显示", "INFO")
            return
        self.log_message(f"数据目录: {dialog.data_dir}", "INFO")
        if dialog.imported_keys:
            self.log_message(f"已从 V2.1 导入设置: {', '.join(dialog.imported_keys)}", "INFO")
            if 'last_config_path' in dialog.imported_keys:
                self.auto_load_last_config()
        self.check_software_status()
        if dialog.benchmark_requested:
            self._benchmark_dialog = BenchmarkDownloadDialog(dialog.data_dir, self)
            if self._benchmark_dialog.start():
                self._benchmark_dialog.thread.finished.connect(
                    lambda _result=None, data_dir=dialog.data_dir: QTimer.singleShot(
                        300, lambda: self._suggest_download_if_empty(data_dir)
                    )
                )
                self._benchmark_dialog.show()
                return
        self._suggest_download_if_empty(dialog.data_dir)

    def _suggest_download_if_empty(self, data_dir):
        """数据目录里除了基准以外没有行情库时，引导用户去数据管理下载。"""
        # 基准在后台下载，下完时「复制 V2.1 的策略」等对话框可能还开着。新提示框会
        # 叠在同一位置，用户正要点下面那个框时容易误点，所以等它们关掉再问。
        if QApplication.activeModalWidget() is not None:
            QTimer.singleShot(1000, lambda: self._suggest_download_if_empty(data_dir))
            return
        stock_dbs = 0
        for market in ('SH', 'SZ', 'BJ'):
            market_dir = os.path.join(data_dir, market)
            if not os.path.isdir(market_dir):
                continue
            stock_dbs += sum(
                1 for name in os.listdir(market_dir)
                if name.lower().endswith('.db') and name.upper() != '000300.DB'
            )
        self.check_software_status()
        if stock_dbs:
            return
        msg_box = QMessageBox(self)
        self.apply_dark_titlebar(msg_box)
        msg_box.setWindowTitle("还没有行情数据")
        msg_box.setIcon(QMessageBox.Information)
        msg_box.setText(
            "数据目录里还没有股票行情，回测前需要先下载。\n\n"
            "• 日线、5 分钟线：「数据管理 → BaoStock导入」，免费，不用注册账号\n"
            "• 1 分钟线：「数据管理 → Tushare导入」，需要在设置里填 Token 并开通 stk_mins 权限\n\n"
            "下载时开始日期要早于回测开始日期，给均线等指标留出预热期。"
        )
        open_btn = msg_box.addButton("打开数据管理", QMessageBox.AcceptRole)
        msg_box.addButton("稍后", QMessageBox.RejectRole)
        msg_box.exec_()
        if msg_box.clickedButton() is open_btn:
            self.open_duckdb_viewer()

    def get_user_strategies_dir(self):
        r"""获取用户策略文件目录路径：文档\KhQuant_OS\strategies

        和 CS 版（文档\KhQuant）分开，两边的示例策略互不覆盖。
        """
        candidates = [
            documents_dir('strategies'),
            # 受限环境下回退到项目内目录，避免因为无权写用户目录而启动失败
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'user_data', 'strategies'),
        ]

        for strategies_dir in candidates:
            try:
                os.makedirs(strategies_dir, exist_ok=True)
                return strategies_dir
            except OSError:
                continue

        return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategies')

    def get_legacy_strategies_dir(self):
        """V2.1 的策略目录 %LOCALAPPDATA%\\KhQuant\\strategies（只读，仅用于复制旧策略）"""
        return os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'KhQuant', 'strategies')

    def init_user_strategies(self, check_legacy=True):
        """初始化用户策略目录，复制默认策略文件，并按需检测旧目录提示迁移

        Args:
            check_legacy: 是否同步触发旧目录迁移弹窗。启动期间（splash 仍在显示时）
                应传 False，避免弹窗被启动画面遮挡造成"卡死"假象；启动完成后由
                主入口通过 QTimer 单独调用 ``check_and_migrate_legacy_strategies``。
        """
        user_strategies_dir = self.get_user_strategies_dir()

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

        # 每次会话只检查一次旧目录迁移
        if check_legacy and not getattr(self, '_legacy_migration_checked', False):
            self._legacy_migration_checked = True
            try:
                self.check_and_migrate_legacy_strategies()
            except Exception as e:
                logging.warning(f"检测旧策略目录失败: {e}")

        return user_strategies_dir

    def check_and_migrate_legacy_strategies(self):
        """检测 V2.1 的策略目录，提示把旧策略复制一份过来（原文件不动）"""
        legacy_dir = self.get_legacy_strategies_dir()
        if not os.path.isdir(legacy_dir):
            return

        try:
            legacy_files = [
                f for f in os.listdir(legacy_dir)
                if f.endswith(('.py', '.kh')) and os.path.isfile(os.path.join(legacy_dir, f))
            ]
        except Exception:
            return

        if not legacy_files:
            return

        # 用户之前已选择"不再提醒"
        if self.settings.value('strategies_migration_dismissed', False, type=bool):
            return

        new_dir = self.get_user_strategies_dir()

        msg_box = QMessageBox(self)
        self.apply_dark_titlebar(msg_box)
        msg_box.setWindowTitle("复制 V2.1 的策略")
        msg_box.setIcon(QMessageBox.Question)
        msg_box.setText(
            f"检测到 V2.1 的策略目录中有 {len(legacy_files)} 个文件：\n{legacy_dir}\n\n"
            f"要把它们复制到开源版的策略目录吗？\n{new_dir}\n\n"
            f"只复制，不改动也不删除 V2.1 目录里的文件，V2.1 仍可照常使用。"
        )
        migrate_btn = msg_box.addButton("复制过来", QMessageBox.AcceptRole)
        later_btn = msg_box.addButton("稍后提醒", QMessageBox.RejectRole)
        never_btn = msg_box.addButton("不再提醒", QMessageBox.DestructiveRole)
        msg_box.setDefaultButton(migrate_btn)
        msg_box.exec_()

        clicked = msg_box.clickedButton()
        if clicked is migrate_btn:
            self._migrate_legacy_strategies(legacy_dir, new_dir, legacy_files)
        elif clicked is never_btn:
            self.settings.setValue('strategies_migration_dismissed', True)

    def _migrate_legacy_strategies(self, legacy_dir, new_dir, files):
        """将旧目录下的策略文件复制到新目录"""
        import shutil
        os.makedirs(new_dir, exist_ok=True)
        migrated, renamed, failed = [], [], []

        for name in files:
            src = os.path.join(legacy_dir, name)
            dst = os.path.join(new_dir, name)
            try:
                if os.path.exists(dst):
                    # 同名文件已存在，自动加后缀避免覆盖
                    base, ext = os.path.splitext(name)
                    idx = 1
                    while True:
                        candidate = os.path.join(new_dir, f"{base}_legacy{idx}{ext}")
                        if not os.path.exists(candidate):
                            dst = candidate
                            break
                        idx += 1
                    shutil.copy2(src, dst)
                    renamed.append((name, os.path.basename(dst)))
                else:
                    shutil.copy2(src, dst)
                    migrated.append(name)
            except Exception as e:
                self.log_message(f"迁移失败 {name}: {e}", "WARNING")
                failed.append(name)

        summary_lines = [f"已复制到：\n{new_dir}", ""]
        summary_lines.append(f"成功: {len(migrated)}")
        if renamed:
            summary_lines.append(f"重命名: {len(renamed)}（目标目录存在同名文件）")
        if failed:
            summary_lines.append(f"失败: {len(failed)}")
        summary_lines.append("")
        summary_lines.append("V2.1 目录里的文件原样保留：")
        summary_lines.append(legacy_dir)

        msg = QMessageBox(self)
        self.apply_dark_titlebar(msg)
        msg.setWindowTitle("复制完成")
        msg.setIcon(QMessageBox.Information)
        msg.setText("\n".join(summary_lines))
        msg.exec_()

        # 迁移过后不再提醒
        self.settings.setValue('strategies_migration_dismissed', True)
        self.log_message(
            f"V2.1 策略复制完成: 成功{len(migrated)}, 重命名{len(renamed)}, 失败{len(failed)}",
            "INFO"
        )

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

# 系统可用字体集合的模块级缓存：避免每次创建日期/时间控件都重新扫描
# 同时使用 families() 而非 hasFamily()，兼容老版本 PyQt5（hasFamily 是 Qt 5.13+ 才加的）
_AVAILABLE_FONT_FAMILIES = None

def _get_available_font_families():
    global _AVAILABLE_FONT_FAMILIES
    if _AVAILABLE_FONT_FAMILIES is None:
        try:
            from PyQt5.QtGui import QFontDatabase
            _AVAILABLE_FONT_FAMILIES = set(QFontDatabase().families())
        except Exception:
            _AVAILABLE_FONT_FAMILIES = set()
    return _AVAILABLE_FONT_FAMILIES

def _apply_chinese_font(widget):
    """为 widget 设置一个可用的中文字体；找不到则保持系统默认"""
    try:
        from PyQt5.QtGui import QFont
        available = _get_available_font_families()
        preferred_family = get_preferred_ui_font_family()
        fallback_families = (
            [preferred_family] if preferred_family else []
        ) + ["PingFang SC", "Hiragino Sans GB", "Helvetica Neue",
             "Microsoft YaHei", "SimHei", "SimSun", "Arial Unicode MS"]
        for family in fallback_families:
            if family and family in available:
                font = QFont(family, 9)
                font.setStyleHint(QFont.SansSerif)
                widget.setFont(font)
                return
    except Exception as e:
        print(f"设置中文字体时出错: {str(e)}")

# 自定义QDateEdit类，禁用滚轮事件并修复中文显示
class NoWheelDateEdit(QDateEdit):
    """禁用滚轮事件的QDateEdit，修复中文显示问题"""
    def __init__(self, parent=None):
        super().__init__(parent)
        _apply_chinese_font(self)
    
    def wheelEvent(self, event):
        # 忽略滚轮事件，不调用父类的wheelEvent
        event.ignore()

# 自定义QTimeEdit类，禁用滚轮事件并修复中文显示
class NoWheelTimeEdit(QTimeEdit):
    """禁用滚轮事件的QTimeEdit，修复中文显示问题"""
    def __init__(self, parent=None):
        super().__init__(parent)
        _apply_chinese_font(self)
    
    def wheelEvent(self, event):
        # 忽略滚轮事件，不调用父类的wheelEvent
        event.ignore()

class DisclaimerDialog(QDialog):
    """免责声明弹窗"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{WINDOW_TITLE} - 免责声明")
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
                caption_color = DWORD(0x333333)
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
        disclaimer_text = """在使用"看海量化回测系统（开源版）"（以下简称"本系统"）之前，请务必仔细阅读并充分理解本文的全部条款。这些条款构成了您与本系统作者之间关于使用本软件的重要约定。


第一章  数据来源与免责声明

■ 数据来源
本系统只读取您本地 DuckDB 数据库里的行情数据进行回测。数据由您自己通过 BaoStock（免费）或 Tushare（需要账号与相应权限）下载，也可以自行导入；系统本身不生产任何原始数据。

■ 数据验证与检验机制
本系统在运行过程中包含了数据有效性检验功能，会对本地数据进行基础的完整性和格式校验。但需要明确的是，这些检验仅为程序正常运行的技术保障，不能等同于对数据准确性的担保。市场数据的准确性和及时性完全取决于 BaoStock、Tushare 等数据提供方。

■ 核心功能定位
请注意，当前版本的"看海量化回测系统"是一款策略回测与研究平台，其核心功能是历史数据验证，当前官方版本不包含任何直接执行实盘交易的功能。

■ 全面责任界定
本系统作者的责任仅限于提供软件工具本身。使用本软件过程中遇到的任何问题，包括但不限于系统故障、数据错误、策略失效、操作失误、电脑故障等，均由用户自行承担全部责任。对于因以下原因导致的任何直接或间接损失，作者不承担任何形式的法律或经济责任：

    • BaoStock、Tushare 等第三方数据服务的任何故障、错误、延迟、限流或数据偏差
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


第四章  问题反馈

■ 反馈渠道
使用中遇到的问题和建议，请在 GitHub 或 Gitee 提交 Issue：
    • https://github.com/khscience/OSkhQuant/issues
    • https://gitee.com/mrkanhai/oskhquant/issues
提交时请附上软件版本、操作步骤和日志（%LOCALAPPDATA%\\KhQuantOS\\logs），便于定位问题。


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

def _apply_dark_titlebar_win(widget):
    """对单个顶层窗口应用深色标题栏（仅 Windows）。"""
    if sys.platform != 'win32':
        return
    try:
        from ctypes import windll, c_int, byref, sizeof
        from ctypes.wintypes import DWORD
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        DWMWA_CAPTION_COLOR = 35
        hwnd = int(widget.winId())
        windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
            byref(c_int(2)), sizeof(c_int))
        caption_color = DWORD(0x333333)
        windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_CAPTION_COLOR,
            byref(caption_color), sizeof(caption_color))
    except Exception:
        pass


class _DarkTitleBarFilter(QObject):
    """应用级事件过滤器：所有顶层 QDialog 首次显示时自动套深色标题栏。

    一处生效，覆盖 QMessageBox / QInputDialog / QProgressDialog 等
    静态便捷弹窗，以及同进程内打开的其他 QDialog 窗口，
    避免白色标题栏与深色主题不搭。
    """
    _PROP = "_dark_titlebar_applied"

    def eventFilter(self, obj, event):
        try:
            if (event.type() == QEvent.Show
                    and isinstance(obj, QDialog)
                    and obj.isWindow()
                    and not obj.property(self._PROP)):
                obj.setProperty(self._PROP, True)
                _apply_dark_titlebar_win(obj)
        except Exception:
            pass
        return False


def use_writable_work_dir():
    r"""打包版把当前目录切到 %LOCALAPPDATA%\KhQuantOS。

    从开始菜单启动时当前目录是安装目录，普通用户不可写；内核里按当前目录写的
    临时文件（DuckDB 溢写目录 temp、默认数据目录 ./stock_data）才不会写失败。
    """
    if not _IS_FROZEN_RUNTIME:
        return
    try:
        work_dir = local_appdata_dir()
        os.makedirs(work_dir, exist_ok=True)
        os.chdir(work_dir)
    except OSError as exc:
        logging.warning(f"切换工作目录失败，继续使用当前目录: {exc}")


def main():
    try:
        use_writable_work_dir()
        force_primary_screen_dpi()
        app = QApplication(sys.argv)

        # 全局深色标题栏：所有弹窗/对话框统一风格（必须保留引用防止被回收）
        app._dark_titlebar_filter = _DarkTitleBarFilter(app)
        app.installEventFilter(app._dark_titlebar_filter)
        
        # 未获焦点的下拉框/日期框不再吃滚轮，避免滚动页面时静默改掉参数
        install_wheel_guard(app)

        # 设置字体和编码，解决时间选择器乱码问题
        try:
            scale = get_ui_font_scale()
            default_font = apply_app_font(scale)
            if default_font:
                print(f"已设置应用字体: {default_font.family()} {default_font.pointSize()}pt (scale={scale})")
            else:
                print("设置应用字体失败，使用系统默认字体")
            
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
        app.setApplicationName(QT_APP)
        app.setOrganizationName(QT_ORG)
        
        def get_app_icon_path(icon_name):
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
                    logging.info("主窗口已显示，开始执行show事件")
                    window.center_window()
                    logging.info(f"主窗口居中完成，几何信息: {window.frameGeometry().getRect()}")
                    window.show()
                    logging.info(f"window.show() 调用完成，isVisible={window.isVisible()}")
                    window.showNormal()
                    window.raise_()
                    window.activateWindow()
                    app.processEvents()
                    logging.info(f"主窗口激活完成，isVisible={window.isVisible()}, isActive={window.isActiveWindow()}")

                    def _run_legacy_migration_check():
                        # 首次启动引导（数据目录、V2.1 设置导入、自动补基准）先于旧策略迁移
                        try:
                            window.run_first_run_guide()
                        except Exception as exc:
                            logging.warning(f"首次启动引导失败: {exc}", exc_info=True)
                        try:
                            if not getattr(window, '_legacy_migration_checked', False):
                                window._legacy_migration_checked = True
                                window.check_and_migrate_legacy_strategies()
                        except Exception as exc:
                            logging.warning(f"检测旧策略目录失败: {exc}")

                    def _show_disclaimer_and_continue():
                        logging.info("主窗口已显示，准备展示免责声明弹窗")
                        disclaimer_dialog = DisclaimerDialog(window)
                        disclaimer_dialog.setWindowModality(Qt.ApplicationModal)
                        disclaimer_dialog.center_on_screen()
                        disclaimer_dialog.raise_()
                        disclaimer_dialog.activateWindow()
                        result = disclaimer_dialog.exec_()
                        
                        if result == QDialog.Rejected:
                            logging.info("用户拒绝免责声明，程序退出")
                            QApplication.quit()
                            return
                        
                        window.raise_()
                        window.activateWindow()

                        QTimer.singleShot(500, _run_legacy_migration_check)

                    # 免责声明弹窗展示规则：
                    # 1. 源码模式下直接跳过，便于开发调试与后台启动；
                    # 2. 自动化 GUI 测试（KHQUANT_GUI_TEST_SKIP_DISCLAIMER=1）始终跳过；
                    # 3. 仅在打包安装模式（is_frozen_runtime()）下才弹出免责声明供用户阅读确认。
                    if not is_frozen_runtime():
                        logging.info("源码运行模式：跳过免责声明弹窗，直接进入主界面")
                        QTimer.singleShot(500, _run_legacy_migration_check)
                    elif os.environ.get("KHQUANT_GUI_TEST_SKIP_DISCLAIMER") == "1":
                        logging.info("自动化测试模式：跳过免责声明弹窗")
                        QTimer.singleShot(500, _run_legacy_migration_check)
                    else:
                        # 稍微延后执行免责声明，确保主窗口有足够的时间完成首屏绘制和前台激活
                        QTimer.singleShot(600, _show_disclaimer_and_continue)

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
            exit_code = app.exec_()
            # 趁 QApplication 还在，先销毁窗口并回收引用环，再让 main() 返回。
            # 否则窗口、对话框及其线程和 lambda 之间的引用环要等垃圾回收，若发生
            # 在 QApplication 析构之后，会在 sip 里访问已释放的对象而崩溃（打包版
            # 关闭时偶发 0xc0000005，开源版在 Windows 沙盒实测复现）。
            try:
                import gc
                from PyQt5 import sip
                for widget in (splash, window):
                    if widget is not None and not sip.isdeleted(widget):
                        widget.deleteLater()
                QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                splash = window = None
                gc.collect()
            except Exception as exc:
                logging.warning(f"退出前释放窗口时出错: {exc}")
            # 单实例锁必须覆盖整个进程生命周期。这里不提前释放，交由操作系统
            # 在进程真正退出时关闭句柄，避免 Qt/日志仍在收尾时新实例抢先启动。
            return exit_code
            
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
    import multiprocessing
    try:
        multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        pass

    sys.exit(main())
