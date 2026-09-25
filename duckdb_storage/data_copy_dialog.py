# -*- coding: utf-8 -*-
"""「复制 CS 数据」对话框：把 CS 版的 DuckDB 数据复制一份给开源版用。

复制逻辑在 kh_data_dir_policy.copy_data_dir_snapshot 里：对源目录只读，
每个库复制完立即断开。这里只负责选目录、估算空间、后台执行和显示进度。
"""
import os
import shutil

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout,
)

from kh_app_identity import default_duckdb_dir
from kh_data_dir_policy import (
    CS_DEFAULT_DATA_DIRS, copy_data_dir_snapshot, has_market_data, is_os_owned,
    list_database_files, validate_copy_dirs,
)


def _format_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} GB"


def _source_size(src) -> int:
    total = 0
    for rel in list_database_files(src):
        try:
            total += os.path.getsize(os.path.join(src, *rel.split("/")))
        except OSError:
            continue
    return total


class DataCopyThread(QThread):
    progress = pyqtSignal(int, int, str)
    log = pyqtSignal(str)
    finished_with_result = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, src, dst, overwrite=False, parent=None):
        super().__init__(parent)
        self.src = src
        self.dst = dst
        self.overwrite = overwrite
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            result = copy_data_dir_snapshot(
                self.src, self.dst,
                progress=lambda done, total, rel: self.progress.emit(done, total, rel),
                should_stop=lambda: self._stop,
                log=self.log.emit,
                overwrite=self.overwrite,
            )
        except Exception as exc:  # noqa: BLE001 - 在界面上显示原因
            self.failed.emit(str(exc))
            return
        self.finished_with_result.emit(result)


class DataCopyDialog(QDialog):
    """复制完成且没有被中途停止时发出 copied(目标目录)。"""

    copied = pyqtSignal(str)

    def __init__(self, parent=None, source_dir="", target_dir=""):
        super().__init__(parent)
        self.setWindowTitle("复制 CS 数据为开源版副本")
        self.setMinimumWidth(640)
        self.copy_thread = None
        self._build_ui(source_dir or self._guess_source(), target_dir or self._guess_target())

    @staticmethod
    def _guess_source():
        for candidate in CS_DEFAULT_DATA_DIRS:
            if os.path.isdir(candidate) and has_market_data(candidate) and not is_os_owned(candidate):
                return candidate
        return ""

    @staticmethod
    def _guess_target():
        target = default_duckdb_dir()
        if not has_market_data(target):
            return target
        return ""

    def _build_ui(self, source_dir, target_dir):
        layout = QVBoxLayout(self)
        intro = QLabel(
            "把看海量化 CS 版的 DuckDB 数据复制一份给开源版用，之后两边各用各的目录，互不锁库。\n"
            "复制时每个库只读地短暂打开，复制完立即断开，不会改动 CS 目录里的任何文件。\n"
            "CS 正在补数的库可能打不开，会记为失败；稍后再运行一次即可补齐，已复制的库会跳过。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        grid = QGridLayout()
        grid.addWidget(QLabel("源目录（CS 的数据）："), 0, 0)
        self.source_edit = QLineEdit(source_dir)
        grid.addWidget(self.source_edit, 0, 1)
        source_btn = QPushButton("浏览…")
        source_btn.clicked.connect(lambda: self._browse(self.source_edit, "选择 CS 的 DuckDB 数据目录"))
        grid.addWidget(source_btn, 0, 2)
        grid.addWidget(QLabel("目标目录（开源版用）："), 1, 0)
        self.target_edit = QLineEdit(target_dir)
        grid.addWidget(self.target_edit, 1, 1)
        target_btn = QPushButton("浏览…")
        target_btn.clicked.connect(lambda: self._browse(self.target_edit, "选择开源版的数据目录（空目录）"))
        grid.addWidget(target_btn, 1, 2)
        layout.addLayout(grid)

        self.overwrite_check = QCheckBox("重新复制已存在的库（会覆盖副本里后来写入的内容）")
        layout.addWidget(self.overwrite_check)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        self.status_label = QLabel("")
        layout.addWidget(self.status_label)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(160)
        layout.addWidget(self.log_view, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.start_btn = QPushButton("开始复制")
        self.start_btn.clicked.connect(self.start_copy)
        buttons.addWidget(self.start_btn)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_copy)
        buttons.addWidget(self.stop_btn)
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.close)
        buttons.addWidget(self.close_btn)
        layout.addLayout(buttons)

    def _browse(self, edit, title):
        path = QFileDialog.getExistingDirectory(self, title, edit.text() or os.path.expanduser("~"))
        if path:
            edit.setText(os.path.normpath(path))

    def _append_log(self, text):
        self.log_view.appendPlainText(text)

    def is_copy_running(self):
        try:
            return self.copy_thread is not None and self.copy_thread.isRunning()
        except RuntimeError:
            return False

    def start_copy(self):
        if self.is_copy_running():
            return
        src = self.source_edit.text().strip()
        dst = self.target_edit.text().strip()
        try:
            validate_copy_dirs(src, dst)
        except ValueError as exc:
            QMessageBox.warning(self, "无法开始复制", str(exc))
            return
        need = _source_size(src)
        probe = dst
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            free = None
        if free is not None and free < need * 1.1:
            QMessageBox.warning(
                self, "磁盘空间不足",
                f"源数据约 {_format_bytes(need)}，目标磁盘只剩 {_format_bytes(free)}。请换一个磁盘。",
            )
            return
        self.log_view.clear()
        self._append_log(f"源目录：{src}")
        self._append_log(f"目标目录：{dst}")
        self._append_log(f"共 {len(list_database_files(src))} 个库，约 {_format_bytes(need)}")
        self.progress_bar.setValue(0)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.copy_thread = DataCopyThread(src, dst, overwrite=self.overwrite_check.isChecked(), parent=self)
        self.copy_thread.progress.connect(self._on_progress)
        self.copy_thread.log.connect(self._append_log)
        self.copy_thread.finished_with_result.connect(self._on_finished)
        self.copy_thread.failed.connect(self._on_failed)
        self.copy_thread.start()

    def stop_copy(self):
        if self.is_copy_running():
            self.copy_thread.stop()
            self.stop_btn.setEnabled(False)
            self.status_label.setText("正在停止，当前库复制完就停…")

    def _on_progress(self, done, total, rel):
        self.progress_bar.setValue(int(done * 100 / total) if total else 100)
        self.status_label.setText(f"{done}/{total}  {rel}")

    def _on_finished(self, result):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        failed = result.get("failed") or []
        summary = (
            f"复制 {result.get('copied', 0)} 个，跳过已存在 {result.get('skipped', 0)} 个，"
            f"失败 {len(failed)} 个，共写入 {_format_bytes(result.get('bytes', 0))}"
        )
        self._append_log(("已停止。" if result.get("stopped") else "完成。") + summary)
        self.status_label.setText(summary)
        if result.get("stopped"):
            return
        if failed:
            QMessageBox.warning(
                self, "部分库没有复制",
                f"{len(failed)} 个库没能打开（多半是 CS 正在写入）。等 CS 补数结束后再点一次「开始复制」，"
                "已复制的库会跳过。\n\n" + "\n".join(rel for rel, _ in failed[:10])
                + ("\n…" if len(failed) > 10 else ""),
            )
        self.copied.emit(self.target_edit.text().strip())

    def _on_failed(self, message):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._append_log(f"复制失败：{message}")
        QMessageBox.warning(self, "复制失败", message)

    def closeEvent(self, event):
        if self.is_copy_running():
            reply = QMessageBox.question(
                self, "正在复制", "复制还没完成，要停止并关闭吗？已复制的库会保留，下次可以续做。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self.copy_thread.stop()
            self.copy_thread.wait(60000)
        super().closeEvent(event)
