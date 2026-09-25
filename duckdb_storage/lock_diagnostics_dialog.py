# -*- coding: utf-8 -*-
"""数据管理中的 DuckDB 占用诊断窗口。"""

from __future__ import annotations

from collections import deque
import logging
import multiprocessing
import os
import time
from typing import Any, Deque, Dict, List

from PyQt5.QtCore import QElapsedTimer, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from duckdb_storage.lock_diagnostics import (
    DatabaseOccupancyRecord,
    OccupancyScanResult,
    scan_database_occupancy,
)


# Windows 的句柄枚举即使跨进程并行也会争用同一底层接口：实测 2/6 路并发
# 都可能把原本 0.2 秒可完成的进程误判为 5 秒超时。只保留一个受监督工作
# 进程；单个 PID 卡死时仍会被精确终止并重建，不会冻结 Qt 主界面或阻断后续 PID。
_OCCUPANCY_SCAN_MAX_WORKERS = 1
_OCCUPANCY_PROCESS_TIMEOUT_SECONDS = 5.0
# 工作进程冷启动（spawn 重新导入主模块）单独计时，不占用单个 PID 的预算
_OCCUPANCY_WORKER_START_TIMEOUT_SECONDS = 30.0
_OCCUPANCY_SCAN_TOTAL_TIMEOUT_SECONDS = 90.0


def _is_database_related_process(
    *,
    pid: int,
    name: str,
    executable: str,
    command_line,
    data_root: str,
    gui_pid: int,
) -> bool:
    if int(pid) == int(gui_pid):
        return True
    process_name = os.path.basename(str(name or executable or "")).lower()
    if process_name.startswith(("python", "pypy", "khquant")):
        return True
    if process_name in {
        "kh.exe",
        "duckdb.exe",
        "dbeaver.exe",
        "dbeaver-ce.exe",
        "datagrip.exe",
        "datagrip64.exe",
    }:
        return True
    if isinstance(command_line, (list, tuple)):
        command_text = " ".join(str(part) for part in command_line)
    else:
        command_text = str(command_line or "")
    searchable = f"{executable or ''} {command_text}".lower()
    markers = (
        os.path.abspath(data_root).lower(),
        "khdata",
        "khquant",
        "duckdb",
        "runbacktest",
        "gui khquant",
    )
    return any(marker and marker in searchable for marker in markers)


def _list_database_related_process_ids(
    data_root: str,
    gui_pid: int,
) -> tuple[List[int], int]:
    import psutil

    process_ids = []
    total_processes = 0
    for process in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        total_processes += 1
        info = process.info
        pid = int(info.get("pid", getattr(process, "pid", 0)) or 0)
        if _is_database_related_process(
            pid=pid,
            name=str(info.get("name") or ""),
            executable=str(info.get("exe") or ""),
            command_line=info.get("cmdline") or [],
            data_root=data_root,
            gui_pid=gui_pid,
        ):
            process_ids.append(pid)
    return process_ids, total_processes


def _run_occupancy_scan_worker(data_root: str, gui_pid: int, connection):
    """逐个扫描分配到的 PID；监督线程可精确终止卡住的工作进程。"""
    try:
        import psutil

        # spawn 子进程会把主模块（源码版是 GUIkhQuant.py）整个重新导入，实测约
        # 5 秒；握手告诉调度方"我已就绪"，让单个 PID 的超时只覆盖真正的句柄读取。
        connection.send(("ready", 0, None))

        while True:
            try:
                pid = connection.recv()
            except EOFError:
                return
            if pid is None:
                return
            try:
                process = psutil.Process(int(pid))
                result = scan_database_occupancy(
                    data_root,
                    psutil_module=psutil,
                    processes=[process],
                    current_pid=gui_pid,
                )
                connection.send(("finished", int(pid), result))
            except Exception as exc:
                connection.send(("failed", int(pid), str(exc)))
    except (BrokenPipeError, EOFError, OSError):
        return
    finally:
        connection.close()


class OccupancyScanThread(QThread):
    """管理独立扫描进程，并把结果安全送回 Qt 主线程。"""

    scan_finished = pyqtSignal(object)
    scan_failed = pyqtSignal(str)
    scan_progress = pyqtSignal(int, int, str)

    def __init__(self, data_root: str, parent=None):
        super().__init__(parent)
        self.data_root = data_root
        self.gui_pid = os.getpid()

    @staticmethod
    def _start_worker(
        context,
        slot: int,
        data_root: str,
        gui_pid: int,
    ) -> Dict[str, Any]:
        parent_connection, child_connection = context.Pipe(duplex=True)
        process = context.Process(
            target=_run_occupancy_scan_worker,
            args=(data_root, gui_pid, child_connection),
            name=f"KhQuantDuckDBOccupancyScan-{slot}",
            daemon=True,
        )
        return {
            "slot": slot,
            "connection": parent_connection,
            "child_connection": child_connection,
            "process": process,
            "pid": None,
            "started_at": 0.0,
            "ready": False,
            "spawned_at": 0.0,
        }

    @staticmethod
    def _dispose_worker(worker: Dict[str, Any], *, graceful: bool = False):
        connection = worker.get("connection")
        child_connection = worker.get("child_connection")
        process = worker.get("process")
        if process is not None:
            if graceful and process.is_alive() and connection is not None:
                try:
                    connection.send(None)
                except (BrokenPipeError, EOFError, OSError):
                    pass
                process.join(0.5)
            if process.is_alive():
                process.terminate()
                process.join(0.75)
            if process.is_alive():
                process.kill()
                process.join(0.5)
            if not process.is_alive():
                process.close()
        if connection is not None:
            connection.close()
        if child_connection is not None:
            child_connection.close()

    def _create_started_worker(self, context, slot: int) -> Dict[str, Any]:
        worker = self._start_worker(
            context,
            slot,
            self.data_root,
            self.gui_pid,
        )
        process = worker["process"]
        try:
            process.start()
            worker["spawned_at"] = time.monotonic()
            worker["child_connection"].close()
            worker["child_connection"] = None
            return worker
        except Exception:
            self._dispose_worker(worker)
            raise

    @staticmethod
    def _merge_scan_result(target: OccupancyScanResult, source: OccupancyScanResult):
        target.records.extend(source.records)
        target.scanned_processes += source.scanned_processes
        target.inaccessible_processes += source.inaccessible_processes
        if len(target.errors) < 5:
            target.errors.extend(source.errors[: 5 - len(target.errors)])

    def run(self):
        workers: List[Dict[str, Any]] = []
        try:
            if not self.data_root or not os.path.isdir(self.data_root):
                self.scan_finished.emit(scan_database_occupancy(self.data_root))
                return

            process_ids, total_processes = _list_database_related_process_ids(
                self.data_root,
                self.gui_pid,
            )
            process_ids = sorted(
                set(process_ids),
                key=lambda pid: (pid != self.gui_pid, pid),
            )
            aggregate = OccupancyScanResult(
                data_root=os.path.abspath(self.data_root),
                total_processes=total_processes,
                skipped_processes=max(total_processes - len(process_ids), 0),
            )
            if not process_ids:
                self.scan_finished.emit(aggregate)
                return

            context = multiprocessing.get_context("spawn")
            pending: Deque[int] = deque(process_ids)
            timed_out_once = set()
            total_related = len(process_ids)
            total_timer = QElapsedTimer()
            total_timer.start()
            self.scan_progress.emit(0, total_related, "准备扫描")
            desired_workers = min(_OCCUPANCY_SCAN_MAX_WORKERS, len(process_ids))
            next_slot = 0
            last_spawn_error = None

            while pending or any(worker["pid"] is not None for worker in workers):
                if self.isInterruptionRequested():
                    return
                if total_timer.elapsed() > int(
                    _OCCUPANCY_SCAN_TOTAL_TIMEOUT_SECONDS * 1000
                ):
                    unresolved = set(pending)
                    unresolved.update(
                        worker["pid"]
                        for worker in workers
                        if worker["pid"] is not None
                    )
                    aggregate.scanned_processes += len(unresolved)
                    aggregate.inaccessible_processes += len(unresolved)
                    if unresolved and len(aggregate.errors) < 5:
                        aggregate.errors.append(
                            f"整体检测达到 {_OCCUPANCY_SCAN_TOTAL_TIMEOUT_SECONDS:g} 秒上限，"
                            f"仍有 {len(unresolved)} 个相关进程未完成"
                        )
                    self.scan_progress.emit(
                        aggregate.scanned_processes,
                        total_related,
                        "已达到整体时限",
                    )
                    break

                while pending and len(workers) < desired_workers:
                    try:
                        worker = self._create_started_worker(context, next_slot)
                    except Exception as exc:
                        last_spawn_error = exc
                        break
                    next_slot += 1
                    workers.append(worker)

                if pending and not workers:
                    if last_spawn_error is not None:
                        raise last_spawn_error
                    raise RuntimeError("无法启动占用检测工作进程")

                for worker in list(workers):
                    process = worker["process"]
                    connection = worker["connection"]
                    pid = worker["pid"]

                    if not process.is_alive():
                        if pid is not None:
                            aggregate.scanned_processes += 1
                            aggregate.inaccessible_processes += 1
                            if len(aggregate.errors) < 5:
                                aggregate.errors.append(
                                    f"PID {pid}: 检测进程异常退出（退出码 {process.exitcode}）"
                                )
                            self.scan_progress.emit(
                                aggregate.scanned_processes,
                                total_related,
                                f"PID {pid} 检测进程异常退出",
                            )
                        self._dispose_worker(worker)
                        workers.remove(worker)
                        continue

                    if not worker["ready"]:
                        # 工作进程还在冷启动：只等它的就绪握手，不占 PID 预算
                        try:
                            if connection.poll():
                                message = connection.recv()
                                if (isinstance(message, tuple) and message
                                        and message[0] == "ready"):
                                    worker["ready"] = True
                                    self.scan_progress.emit(
                                        aggregate.scanned_processes,
                                        total_related,
                                        "检测进程已就绪",
                                    )
                        except (BrokenPipeError, EOFError, OSError):
                            self._dispose_worker(worker)
                            workers.remove(worker)
                            continue
                        if not worker["ready"]:
                            if (time.monotonic() - worker["spawned_at"]
                                    > _OCCUPANCY_WORKER_START_TIMEOUT_SECONDS):
                                if len(aggregate.errors) < 5:
                                    aggregate.errors.append(
                                        f"检测工作进程启动超过 "
                                        f"{_OCCUPANCY_WORKER_START_TIMEOUT_SECONDS:g} 秒未就绪"
                                    )
                                self._dispose_worker(worker)
                                workers.remove(worker)
                            continue

                    if pid is None and pending:
                        next_pid = pending.popleft()
                        try:
                            connection.send(next_pid)
                        except (BrokenPipeError, EOFError, OSError):
                            pending.appendleft(next_pid)
                            self._dispose_worker(worker)
                            workers.remove(worker)
                            continue
                        worker["pid"] = next_pid
                        worker["started_at"] = time.monotonic()
                        pid = next_pid
                        self.scan_progress.emit(
                            aggregate.scanned_processes,
                            total_related,
                            f"正在检查 PID {pid}",
                        )

                    if pid is None:
                        continue

                    try:
                        has_message = connection.poll()
                    except (BrokenPipeError, EOFError, OSError):
                        has_message = False
                    if has_message:
                        try:
                            status, result_pid, payload = connection.recv()
                        except (BrokenPipeError, EOFError, OSError):
                            status, result_pid, payload = (
                                "failed",
                                pid,
                                "检测工作进程连接意外关闭",
                            )
                        if status == "ready":
                            # 重建的工作进程可能在分配 PID 后才送达握手，忽略即可
                            worker["ready"] = True
                            continue
                        if status == "finished":
                            self._merge_scan_result(aggregate, payload)
                        else:
                            aggregate.scanned_processes += 1
                            aggregate.inaccessible_processes += 1
                            if len(aggregate.errors) < 5:
                                aggregate.errors.append(f"PID {result_pid}: {payload}")
                        worker["pid"] = None
                        worker["started_at"] = 0.0
                        self.scan_progress.emit(
                            aggregate.scanned_processes,
                            total_related,
                            "",
                        )
                        continue

                    if (
                        time.monotonic() - worker["started_at"]
                        > _OCCUPANCY_PROCESS_TIMEOUT_SECONDS
                    ):
                        if pid not in timed_out_once:
                            # Windows 的句柄接口偶尔会出现一次性全局阻塞；换一个全新
                            # 子进程在队尾重试，可消除瞬时假告警，同时仍限制永久卡死。
                            timed_out_once.add(pid)
                            pending.append(pid)
                            self.scan_progress.emit(
                                aggregate.scanned_processes,
                                total_related,
                                f"PID {pid} 首次超时，已进入重试队列",
                            )
                        else:
                            aggregate.scanned_processes += 1
                            aggregate.inaccessible_processes += 1
                            if len(aggregate.errors) < 5:
                                aggregate.errors.append(
                                    f"PID {pid}: 句柄读取连续两次超过 "
                                    f"{_OCCUPANCY_PROCESS_TIMEOUT_SECONDS:g} 秒，已跳过"
                                )
                            self.scan_progress.emit(
                                aggregate.scanned_processes,
                                total_related,
                                f"PID {pid} 连续超时，已跳过",
                            )
                        self._dispose_worker(worker)
                        workers.remove(worker)

                QThread.msleep(20)

            aggregate.records.sort(
                key=lambda item: (
                    item.pid != self.gui_pid,
                    item.protected,
                    item.module,
                    item.pid,
                )
            )
            self.scan_finished.emit(aggregate)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.scan_failed.emit(str(exc))
        finally:
            for worker in workers:
                self._dispose_worker(worker, graceful=not self.isInterruptionRequested())


class DatabaseOccupancyDialog(QDialog):
    """展示当前数据目录的进程级占用，并提供受控释放操作。"""

    def __init__(self, viewer):
        super().__init__(viewer)
        self.viewer = viewer
        self.data_root = os.path.abspath(str(getattr(viewer, "data_root", "") or ""))
        self.records_by_pid: Dict[int, DatabaseOccupancyRecord] = {}
        self._scan_thread = None
        self._close_pending = False

        self.setWindowTitle("DuckDB 数据库占用诊断")
        self.setMinimumSize(980, 580)
        self.resize(1180, 680)
        self.setModal(True)
        self._build_ui()
        self.refresh_scan()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel(
            "这里显示当前数据目录中由系统检测到的 DuckDB 文件句柄。默认检查当前看海量化、"
            "所有 Python 任务及常见数据库工具；无明显数据库特征的系统进程会跳过，避免 Windows "
            "句柄接口卡死。扫描只读，不会主动打开数据库。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        root_label = QLabel(f"数据目录：{self.data_root or '未选择'}")
        root_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(root_label)

        self.scan_status = QLabel("准备扫描……")
        self.scan_status.setWordWrap(True)
        layout.addWidget(self.scan_status)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["模块", "PID", "进程", "启动时间", "占用数据库", "处理策略", "命令行"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in (0, 1, 2, 3, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        header.setSectionResizeMode(6, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._update_details)
        layout.addWidget(self.table, 1)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(118)
        self.details.setPlaceholderText("选择一行可查看完整进程路径、命令行及数据库文件。")
        layout.addWidget(self.details)

        actions = QHBoxLayout()
        self.refresh_button = QPushButton("刷新检测")
        self.refresh_button.clicked.connect(self.refresh_scan)
        actions.addWidget(self.refresh_button)

        self.release_local_button = QPushButton("释放本窗口连接")
        self.release_local_button.clicked.connect(self.release_local_connections)
        actions.addWidget(self.release_local_button)

        self.stop_selected_button = QPushButton("强制停止选中进程")
        self.stop_selected_button.clicked.connect(self.stop_selected_processes)
        actions.addWidget(self.stop_selected_button)

        self.stop_all_button = QPushButton("释放全部可结束连接")
        self.stop_all_button.setToolTip(
            "释放本窗口连接，并结束已识别的回测、数据导入和定时补充进程；"
            "不会批量结束行情、模拟盘、策略运行时、主程序或未知进程"
        )
        self.stop_all_button.clicked.connect(self.stop_all_stoppable_processes)
        actions.addWidget(self.stop_all_button)
        actions.addStretch()

        close_box = QDialogButtonBox(QDialogButtonBox.Close)
        close_box.button(QDialogButtonBox.Close).setText("关闭")
        close_box.rejected.connect(self.reject)
        actions.addWidget(close_box)
        layout.addLayout(actions)

    def _set_actions_enabled(self, enabled: bool):
        self.refresh_button.setEnabled(enabled)
        self.release_local_button.setEnabled(enabled)
        self.stop_selected_button.setEnabled(enabled)
        self.stop_all_button.setEnabled(enabled)

    def refresh_scan(self):
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return
        if not self.data_root or not os.path.isdir(self.data_root):
            self.scan_status.setText("数据目录不存在或尚未选择，无法检测。")
            return
        self.records_by_pid = {}
        self.table.setRowCount(0)
        self.details.clear()
        self._set_actions_enabled(False)
        self.scan_status.setText("正在读取系统文件句柄……")
        self._scan_thread = OccupancyScanThread(self.data_root, self)
        self._scan_thread.scan_finished.connect(self._on_scan_finished)
        self._scan_thread.scan_failed.connect(self._on_scan_failed)
        progress_signal = getattr(self._scan_thread, "scan_progress", None)
        if progress_signal is not None:
            progress_signal.connect(self._on_scan_progress)
        self._scan_thread.finished.connect(self._on_scan_thread_done)
        self._scan_thread.start()

    def _on_scan_progress(self, completed: int, total: int, detail: str):
        if self._close_pending:
            return
        message = f"正在读取系统文件句柄……已完成 {completed}/{total} 个相关进程"
        if detail:
            message += f"（{detail}）"
        self.scan_status.setText(message)

    def _on_scan_thread_done(self):
        thread = self._scan_thread
        self._scan_thread = None
        if thread is not None:
            thread.deleteLater()
        if self._close_pending:
            QTimer.singleShot(0, self._finish_pending_close)
        else:
            self._set_actions_enabled(True)

    def _on_scan_failed(self, message: str):
        if self._close_pending:
            return
        self.scan_status.setText(f"检测失败：{message}")

    def _on_scan_finished(self, result: OccupancyScanResult):
        if self._close_pending:
            return
        self.records_by_pid = {record.pid: record for record in result.records}
        self.table.setRowCount(0)
        current_pid = os.getpid()
        for record in result.records:
            row = self.table.rowCount()
            self.table.insertRow(row)
            db_names = [os.path.relpath(path, self.data_root) for path in record.database_files]
            if len(db_names) > 4:
                db_summary = "、".join(db_names[:4]) + f" 等 {len(db_names)} 个"
            else:
                db_summary = "、".join(db_names)
            if record.pid == current_pid:
                strategy = "本窗口：仅释放连接"
            elif record.can_bulk_stop:
                strategy = "已识别：可批量结束"
            else:
                strategy = "受保护：仅逐项强确认"

            values = (
                record.module,
                str(record.pid),
                record.process_name,
                record.started_at,
                db_summary,
                strategy,
                record.command_line or record.executable or "未知",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, record.pid)
                if record.protected:
                    item.setForeground(QColor("#d98c3f"))
                item.setToolTip(value)
                self.table.setItem(row, column, item)
            self.table.item(row, 4).setToolTip("\n".join(record.database_files))
            self.table.item(row, 6).setToolTip(
                f"程序：{record.executable or '未知'}\n命令行：{record.command_line or '未知'}"
            )

        pieces = [
            f"检测到 {len(result.records)} 个占用进程",
            f"已纳入检测 {result.scanned_processes} 个数据库相关进程",
            f"成功读取 {result.successfully_checked_processes} 个进程的文件句柄",
        ]
        if result.inaccessible_processes:
            pieces.append(
                f"其中 {result.inaccessible_processes} 个相关进程因权限、超时或退出无法检查"
            )
        if result.skipped_processes:
            pieces.append(
                f"已跳过 {result.skipped_processes} 个无明显数据库特征的系统进程"
            )
        if result.errors:
            pieces.append("部分诊断信息：" + "；".join(result.errors[:3]))
        if not result.records:
            if result.scanned_processes and not result.successfully_checked_processes:
                # 一个相关进程都没读成句柄时，"未发现占用"是假阴性：用户正是在
                # 库被占用、写不进去时才打开这个工具，不能给出干净的结论。
                pieces.insert(0, "诊断未完成：所有相关进程的文件句柄都没能读取，本次结果不能视为\"没有占用\"")
            else:
                pieces.insert(0, "当前在数据库相关进程范围内未发现该数据目录的打开句柄")
        self.scan_status.setText("；".join(pieces) + "。这是刷新时刻的瞬时快照。")
        self._update_details()

    def _selected_records(self) -> List[DatabaseOccupancyRecord]:
        selected = []
        seen = set()
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), 0)
            pid = item.data(Qt.UserRole) if item is not None else None
            record = self.records_by_pid.get(pid)
            if record is not None and record.pid not in seen:
                seen.add(record.pid)
                selected.append(record)
        return selected

    def _update_details(self):
        records = self._selected_records()
        if not records:
            self.details.clear()
            return
        blocks = []
        for record in records:
            files = "\n    ".join(record.database_files) or "无"
            blocks.append(
                f"{record.module} | PID {record.pid} | {record.process_name}\n"
                f"程序：{record.executable or '未知'}\n"
                f"命令行：{record.command_line or '未知'}\n"
                f"处理说明：{record.protection_reason or '已识别'}\n"
                f"数据库：\n    {files}"
            )
        self.details.setPlainText("\n\n".join(blocks))

    def release_local_connections(self):
        ok, message = self.viewer._release_local_duckdb_connections()
        if not ok:
            QMessageBox.warning(self, "无法释放本窗口连接", message)
            return False
        QMessageBox.information(
            self,
            "本窗口连接已释放",
            message + "\n\n数据管理暂时断开；关闭诊断窗口后可点击主界面的刷新按钮恢复浏览。",
        )
        self.viewer.statusBar.showMessage("本窗口 DuckDB 连接已释放")
        self.refresh_scan()
        return True

    def _confirm_records(self, records: List[DatabaseOccupancyRecord], title: str, intro: str) -> bool:
        lines = [intro, ""]
        for record in records[:12]:
            lines.append(f"- {record.module} | PID {record.pid} | {record.process_name}")
        if len(records) > 12:
            lines.append(f"- 其余 {len(records) - 12} 个进程")
        lines.extend(("", "结束进程可能中断尚未完成的回测或数据写入，未确认前不会执行。"))
        reply = QMessageBox.question(
            self,
            title,
            "\n".join(lines),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _terminate_records(self, records: List[DatabaseOccupancyRecord]):
        succeeded = []
        failed = []
        for record in records:
            if record.create_time is None:
                failed.append((record, "无法复核进程启动时间，已拒绝结束以避免PID复用误杀"))
                continue
            ok, message = self.viewer._terminate_process_by_pid(
                record.pid,
                expected_create_time=record.create_time,
            )
            logging.info(
                "DuckDB占用诊断结束进程: module=%s pid=%s ok=%s detail=%s",
                record.module,
                record.pid,
                ok,
                message,
            )
            (succeeded if ok else failed).append((record, message))

        summary = [f"成功结束：{len(succeeded)} 个", f"失败或已跳过：{len(failed)} 个"]
        if failed:
            summary.append("")
            summary.extend(
                f"- PID {record.pid}（{record.module}）：{message}"
                for record, message in failed[:8]
            )
        QMessageBox.information(self, "处理结果", "\n".join(summary))
        self.refresh_scan()

    def stop_selected_processes(self):
        records = self._selected_records()
        if not records:
            QMessageBox.information(self, "请选择进程", "请先在表格中选择要停止的进程。")
            return
        if any(record.pid == os.getpid() for record in records):
            QMessageBox.warning(
                self,
                "不能结束当前窗口",
                "当前数据管理进程不能结束自身，请使用“释放本窗口连接”。",
            )
            return
        if not self._confirm_records(
            records,
            "确认强制停止选中进程",
            "将精确结束以下数据库占用进程：",
        ):
            return

        protected = [record for record in records if record.protected]
        if protected:
            protected_lines = "\n".join(
                f"- {record.module} | PID {record.pid}：{record.protection_reason}"
                for record in protected
            )
            reply = QMessageBox.question(
                self,
                "受保护进程二次确认",
                "选中项包含受保护或用途未知的进程：\n\n"
                + protected_lines
                + "\n\n仍要强制结束这些精确 PID 吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        self._terminate_records(records)

    def stop_all_stoppable_processes(self):
        records = [
            record
            for record in self.records_by_pid.values()
            if record.pid != os.getpid() and record.can_bulk_stop and not record.protected
        ]
        protected_count = sum(
            1
            for record in self.records_by_pid.values()
            if record.pid != os.getpid() and not record.can_bulk_stop
        )
        intro = (
            "将先释放本数据管理窗口的连接，再结束以下已识别且允许批量处理的外部任务。"
            f"另有 {protected_count} 个受保护或未知进程不会被结束："
        )
        if not self._confirm_records(records, "确认释放全部可结束连接", intro):
            return

        ok, local_message = self.viewer._release_local_duckdb_connections()
        if not ok:
            QMessageBox.warning(self, "无法释放本窗口连接", local_message)
            return
        if records:
            self._terminate_records(records)
        else:
            QMessageBox.information(
                self,
                "处理完成",
                local_message + "\n\n没有发现可批量结束的外部占用进程；受保护或未知进程未处理。",
            )
            self.refresh_scan()

    def _defer_close_until_scan_finishes(self) -> bool:
        """扫描尚未结束时延迟销毁窗口，避免 Qt 直接终止整个进程。"""
        thread = self._scan_thread
        if thread is not None and thread.isRunning():
            self._close_pending = True
            thread.requestInterruption()
            self._set_actions_enabled(False)
            self.scan_status.setText("正在结束占用检测，完成后将自动关闭……")
            return True
        return False

    def _finish_pending_close(self):
        if self._scan_thread is not None:
            return
        self._close_pending = False
        QDialog.reject(self)

    def reject(self):
        """覆盖按钮和 Esc 的关闭路径，确保后台线程不会随窗口被销毁。"""
        if self._defer_close_until_scan_finishes():
            return
        QDialog.reject(self)

    def closeEvent(self, event):
        if self._defer_close_until_scan_finishes():
            event.ignore()
            return
        super().closeEvent(event)
