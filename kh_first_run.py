# coding: utf-8
"""首次启动引导：选数据目录、只读导入 V2.1 设置、自动补沪深300基准。

- 数据目录默认用开源版自己的 %LOCALAPPDATA%\\KhQuantOS\\khData，建议换到非系统盘；
  目录在 OneDrive 下、或可能是 CS 版在用的目录时给出提醒。
- V2.1 的设置（HKCU\\Software\\KHQuant\\StockAnalyzer）只读导入一次，不写回。
- 基准 000300.SH 用 BaoStock 下载，走数据管理里同一个下载线程。
"""
import logging
import os

from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from kh_app_identity import (
    APP_NAME, LEGACY_V21_QT_APP, LEGACY_V21_QT_ORG, default_duckdb_dir,
)
from kh_data_dir_policy import (
    CS_DEFAULT_DATA_DIRS, SHARED_WRITE_WARNING, claim_if_new, has_market_data,
    is_os_owned, may_be_shared_with_cs, normalize_dir,
)

FIRST_RUN_DONE_KEY = "first_run_completed"
BENCHMARK_CODE = "000300.SH"

# V2.1 存在 QSettings 里、开源版仍然使用的设置项。交易成本、股票池、日期
# 这些在 V2.1 的 .kh 配置文件里，导入 last_config_path 后启动时会自动加载。
V21_IMPORT_KEYS = (
    "risk_free_rate",
    "delay_log_display",
    "max_log_lines",
    "account_id",
    "last_config_path",
    "last_strategy_path",
)


def needs_first_run(settings) -> bool:
    return not settings.value(FIRST_RUN_DONE_KEY, False, type=bool)


def _legacy_v21_settings():
    return QSettings(LEGACY_V21_QT_ORG, LEGACY_V21_QT_APP)


def v21_settings_available() -> bool:
    """本机有没有 V2.1（或更早版本）留下的界面设置。"""
    try:
        legacy = _legacy_v21_settings()
        return any(legacy.contains(key) for key in V21_IMPORT_KEYS)
    except Exception:
        return False


def import_v21_settings(settings) -> list:
    """只读地把 V2.1 的设置复制到开源版（只在首次启动时调用）；返回导入了哪些键。"""
    legacy = _legacy_v21_settings()
    imported = []
    for key in V21_IMPORT_KEYS:
        if not legacy.contains(key):
            continue
        value = legacy.value(key)
        if value in (None, ""):
            continue
        if key in ("last_config_path", "last_strategy_path") and not os.path.exists(str(value)):
            continue
        settings.setValue(key, value)
        imported.append(key)
    return imported


def is_under_onedrive(path) -> bool:
    try:
        norm = normalize_dir(path)
    except (TypeError, ValueError):
        return False
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(env)
        if root and (norm == normalize_dir(root) or norm.startswith(normalize_dir(root) + os.sep)):
            return True
    return "onedrive" in norm.lower().split(os.sep)


def is_on_system_drive(path) -> bool:
    system_drive = os.environ.get("SystemDrive", "C:")
    return os.path.splitdrive(normalize_dir(path))[0].lower() == system_drive.lower()


def detect_cs_data_dir() -> str:
    """找一个看起来是 CS 在用的数据目录（只看 CS 的默认目录，不读 CS 的设置文件）。"""
    for candidate in CS_DEFAULT_DATA_DIRS:
        if os.path.isdir(candidate) and has_market_data(candidate) and not is_os_owned(candidate):
            return candidate
    return ""


class FirstRunDialog(QDialog):
    """accept 之后读 data_dir、benchmark_requested、imported_keys。"""

    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.settings = settings
        self.data_dir = ""
        self.benchmark_requested = False
        self.imported_keys = []
        self.setWindowTitle(f"欢迎使用{APP_NAME}")
        self.setMinimumWidth(640)
        self._build_ui()
        self._refresh_dir_hint()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        intro = QLabel(
            "开源版只读本地 DuckDB 数据进行回测，不需要安装 QMT。\n"
            "日线和 5 分钟线可以用 BaoStock 免费下载（不用注册账号）；"
            "1 分钟线用 Tushare 下载，需要开通 stk_mins 权限；Tick 需自行导入。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        dir_group = QGroupBox("数据目录")
        dir_layout = QVBoxLayout(dir_group)
        row = QHBoxLayout()
        saved = self.settings.value("duckdb_data_path", "") if self.settings is not None else ""
        self.dir_edit = QLineEdit(saved or default_duckdb_dir())
        self.dir_edit.textChanged.connect(self._refresh_dir_hint)
        row.addWidget(self.dir_edit, 1)
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self._browse)
        row.addWidget(browse_btn)
        dir_layout.addLayout(row)
        self.dir_hint = QLabel("")
        self.dir_hint.setWordWrap(True)
        dir_layout.addWidget(self.dir_hint)

        self.cs_dir = detect_cs_data_dir()
        if self.cs_dir:
            cs_row = QHBoxLayout()
            cs_label = QLabel(
                f"检测到看海量化 CS 版的数据目录 {self.cs_dir}。建议不要直接共用，"
                "可以把它复制一份到上面的目录。"
            )
            cs_label.setWordWrap(True)
            cs_row.addWidget(cs_label, 1)
            copy_btn = QPushButton("复制 CS 数据…")
            copy_btn.clicked.connect(self._open_copy_dialog)
            cs_row.addWidget(copy_btn)
            dir_layout.addLayout(cs_row)
        layout.addWidget(dir_group)

        self.v21_check = None
        if v21_settings_available():
            self.v21_check = QCheckBox(
                "导入 V2.1 的设置（无风险收益率、日志设置、上次的配置文件和策略路径；只读，不改动 V2.1）"
            )
            self.v21_check.setChecked(True)
            layout.addWidget(self.v21_check)

        self.benchmark_check = QCheckBox(
            f"用 BaoStock 下载沪深300（{BENCHMARK_CODE}）日线作为回测基准（需要联网，约一分钟）"
        )
        self.benchmark_check.setChecked(True)
        layout.addWidget(self.benchmark_check)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        later_btn = QPushButton("以后再说")
        later_btn.clicked.connect(self.reject)
        buttons.addWidget(later_btn)
        done_btn = QPushButton("完成")
        done_btn.setDefault(True)
        done_btn.clicked.connect(self._finish)
        buttons.addWidget(done_btn)
        layout.addLayout(buttons)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择数据目录", self.dir_edit.text() or os.path.expanduser("~"))
        if path:
            self.dir_edit.setText(os.path.normpath(path))

    def _refresh_dir_hint(self, *_args):
        path = self.dir_edit.text().strip()
        notes = []
        if not path:
            notes.append("请填写数据目录。")
        else:
            if may_be_shared_with_cs(path):
                notes.append("⚠ 这个目录可能也在被 CS 版使用。" + SHARED_WRITE_WARNING)
            if is_under_onedrive(path):
                notes.append("⚠ 目录在 OneDrive 下，同步会占用和锁住数据库文件，建议换一个位置。")
            if is_on_system_drive(path):
                notes.append("建议放在非系统盘（例如 D:\\KhQuantOS_Data），全市场数据会占用较多空间。")
        self.dir_hint.setText("\n".join(notes))

    def _open_copy_dialog(self):
        from duckdb_storage.data_copy_dialog import DataCopyDialog

        dialog = DataCopyDialog(self, source_dir=self.cs_dir, target_dir=self.dir_edit.text().strip())
        dialog.copied.connect(lambda target: self.dir_edit.setText(target))
        dialog.exec_()

    def _finish(self):
        path = self.dir_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "提示", "请填写数据目录。")
            return
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
        if may_be_shared_with_cs(path):
            reply = QMessageBox.warning(
                self, "直接使用可能和 CS 共用的目录",
                f"{path}\n\n{SHARED_WRITE_WARNING}\n"
                "在这个目录里导入、核验索引、WAL 修复都要逐次确认，策略里的 khDuckWrite 会拒绝写入。\n\n"
                "仍然直接使用这个目录吗？（建议点「否」，改用开源版自己的目录，或先复制一份 CS 数据）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        if is_under_onedrive(path):
            reply = QMessageBox.question(
                self, "目录在 OneDrive 下",
                "OneDrive 同步会占用和锁住数据库文件，导致下载或回测失败。仍然使用这个目录吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        try:
            os.makedirs(path, exist_ok=True)
            claim_if_new(path, "首次启动引导")
        except OSError as exc:
            QMessageBox.warning(self, "无法使用这个目录", f"创建目录失败：{exc}")
            return
        self.data_dir = path
        if self.settings is not None:
            self.settings.setValue("duckdb_data_path", path)
            if self.v21_check is not None and self.v21_check.isChecked():
                try:
                    self.imported_keys = import_v21_settings(self.settings)
                except Exception as exc:
                    logging.warning(f"导入 V2.1 设置失败: {exc}")
            self.settings.setValue(FIRST_RUN_DONE_KEY, True)
        self.benchmark_requested = self.benchmark_check.isChecked()
        self.accept()


class BenchmarkDownloadDialog(QDialog):
    """用 BaoStock 补齐 000300.SH 日线，完成后关闭写连接。"""

    def __init__(self, data_dir, parent=None):
        super().__init__(parent)
        self.data_dir = data_dir
        self.manager = None
        self.thread = None
        self.result = None
        self.setWindowTitle("下载基准指数")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        self.status_label = QLabel(f"正在用 BaoStock 下载 {BENCHMARK_CODE} 日线…")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(140)
        layout.addWidget(self.log_view)
        row = QHBoxLayout()
        row.addStretch(1)
        self.close_btn = QPushButton("后台继续")
        self.close_btn.clicked.connect(self.hide)
        row.addWidget(self.close_btn)
        layout.addLayout(row)

    def start(self):
        from duckdb_storage.manager import DuckDBManager
        from duckdb_storage.viewer import BaoStockImportThread, get_baostock_request_tracker

        try:
            DuckDBManager.close_read_only_instances(self.data_dir)
            self.manager = DuckDBManager(data_root=self.data_dir, read_only=False)
        except Exception as exc:
            self._finish_with_message(f"无法打开数据目录：{exc}", ok=False)
            return False
        self.thread = BaoStockImportThread(
            self.manager, [], {},
            request_tracker=get_baostock_request_tracker(self.data_dir),
        )
        self.thread.status.connect(self.log_view.appendPlainText)
        self.thread.error.connect(lambda message: self.log_view.appendPlainText(f"错误：{message}"))
        self.thread.finished.connect(self._on_finished)
        self.thread.start()
        return True

    def is_running(self):
        try:
            return self.thread is not None and self.thread.isRunning()
        except RuntimeError:
            return False

    def _release_manager(self):
        from duckdb_storage.manager import DuckDBManager

        if self.manager is not None:
            try:
                self.manager.close_all()
            finally:
                DuckDBManager.close_writable_instances(self.data_dir)
                self.manager = None

    def _on_finished(self, result):
        self.result = result or {}
        self._release_manager()
        error = self.result.get("benchmark_error")
        if error:
            self._finish_with_message(
                f"{BENCHMARK_CODE} 没有下载成功：{error}\n可以稍后在「数据管理 → BaoStock导入」里重试。",
                ok=False,
            )
        else:
            self._finish_with_message(f"{BENCHMARK_CODE} 日线已就绪，可以作为回测基准。", ok=True)

    def _finish_with_message(self, message, ok):
        self.status_label.setText(message)
        self.log_view.appendPlainText(message)
        self.close_btn.setText("关闭")
        try:
            self.close_btn.clicked.disconnect()
        except TypeError:
            pass
        self.close_btn.clicked.connect(self.accept)
        if not self.isVisible():
            parent = self.parent()
            if parent is not None and hasattr(parent, "log_message"):
                parent.log_message(message, "INFO" if ok else "WARNING")

    def closeEvent(self, event):
        if self.is_running():
            self.hide()
            event.ignore()
            return
        super().closeEvent(event)


__all__ = [
    "FIRST_RUN_DONE_KEY", "BENCHMARK_CODE", "V21_IMPORT_KEYS", "needs_first_run",
    "v21_settings_available", "import_v21_settings", "is_under_onedrive", "is_on_system_drive",
    "detect_cs_data_dir", "FirstRunDialog", "BenchmarkDownloadDialog",
]
