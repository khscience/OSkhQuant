# coding: utf-8
"""DuckDB 占用诊断窗口的线程生命周期回归测试。"""

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import QApplication, QDialogButtonBox, QWidget

import duckdb_storage.lock_diagnostics_dialog as dialog_module


class _ControlledScanThread(QThread):
    """故意忽略中断，模拟 Windows 句柄读取暂时无法返回。"""

    scan_finished = pyqtSignal(object)
    scan_failed = pyqtSignal(str)

    def __init__(self, data_root, parent=None):
        super().__init__(parent)
        self.data_root = data_root
        self.release = threading.Event()

    def run(self):
        self.release.wait(5)


@pytest.fixture()
def qt_app():
    return QApplication.instance() or QApplication([])


def _wait_until(app, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


@pytest.mark.parametrize(
    ("pid", "name", "command", "expected"),
    [
        (100, "explorer.exe", ["explorer.exe"], True),
        (200, "python.exe", ["python.exe", "custom.py"], True),
        (201, "KhQuant.exe", ["KhQuant.exe"], True),
        (202, "javaw.exe", ["javaw.exe", "dbeaver", "duckdb"], True),
        (203, "pythonw.exe", ["pythonw.exe", "private_service.py"], True),
        (204, "chrome.exe", ["chrome.exe", "https://example.test"], False),
        (205, "explorer.exe", ["explorer.exe"], False),
    ],
)
def test_database_related_process_filter(pid, name, command, expected):
    assert (
        dialog_module._is_database_related_process(
            pid=pid,
            name=name,
            executable=command[0],
            command_line=command,
            data_root=r"I:\khData",
            gui_pid=100,
        )
        is expected
    )


def test_real_scan_thread_returns_subprocess_result(qt_app, tmp_path):
    results = []
    thread = dialog_module.OccupancyScanThread(str(tmp_path / "missing"))
    thread.scan_finished.connect(results.append)

    try:
        thread.start()
        assert _wait_until(qt_app, lambda: bool(results), timeout=8.0)
        assert _wait_until(qt_app, lambda: not thread.isRunning())
        assert "不存在" in results[0].errors[0]
    finally:
        if thread.isRunning():
            thread.requestInterruption()
            thread.wait(5000)
        thread.deleteLater()
        qt_app.processEvents()


def test_real_scan_thread_can_be_cancelled_without_waiting_for_handle_scan(
    qt_app, tmp_path
):
    thread = dialog_module.OccupancyScanThread(str(tmp_path))

    try:
        thread.start()
        assert _wait_until(qt_app, thread.isRunning)
        thread.requestInterruption()
        assert thread.wait(5000)
    finally:
        if thread.isRunning():
            thread.requestInterruption()
            thread.wait(5000)
        thread.deleteLater()
        qt_app.processEvents()


def test_scan_thread_reports_process_start_failure_without_cleanup_error(
    qt_app, tmp_path, monkeypatch
):
    class _Connection:
        def close(self):
            pass

    class _Process:
        closed = False

        def start(self):
            raise OSError("spawn denied")

        def is_alive(self):
            return False

        def close(self):
            self.closed = True

    class _Context:
        process = _Process()

        def Pipe(self, duplex=False):
            assert duplex is True
            return _Connection(), _Connection()

        def Process(self, **_kwargs):
            return self.process

    context = _Context()
    monkeypatch.setattr(
        dialog_module.multiprocessing,
        "get_context",
        lambda _method: context,
    )
    monkeypatch.setattr(
        dialog_module,
        "_list_database_related_process_ids",
        lambda _data_root, _gui_pid: ([123], 1),
    )
    failures = []
    thread = dialog_module.OccupancyScanThread(str(tmp_path))
    thread.scan_failed.connect(failures.append)

    thread.run()
    qt_app.processEvents()

    assert failures == ["spawn denied"]
    assert context.process.closed is True


def test_scan_thread_restarts_worker_after_timeout_and_continues(
    qt_app, tmp_path, monkeypatch
):
    class _Connection:
        def __init__(self, mode):
            self.mode = mode
            self.pid = None
            self.stop_sent = False
            self.closed = False
            self.ready_sent = False

        def send(self, value):
            if value is None:
                self.stop_sent = True
            else:
                self.pid = value

        def poll(self):
            # 工作进程先握手报就绪，再回结果（与真实协议一致）
            if not self.ready_sent:
                return True
            return self.mode == "finished" and self.pid is not None

        def recv(self):
            if not self.ready_sent:
                self.ready_sent = True
                return ("ready", 0, None)
            return (
                "finished",
                self.pid,
                dialog_module.OccupancyScanResult(
                    data_root=str(tmp_path),
                    scanned_processes=1,
                ),
            )

        def close(self):
            self.closed = True

    class _Process:
        def __init__(self, connection):
            self.connection = connection
            self.alive = False
            self.terminated = False
            self.closed = False
            self.exitcode = 0

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, _timeout):
            if self.connection.stop_sent or self.terminated:
                self.alive = False

        def terminate(self):
            self.terminated = True
            self.alive = False

        def kill(self):
            self.terminated = True
            self.alive = False

        def close(self):
            self.closed = True

    class _Context:
        def __init__(self):
            self.modes = iter(("timeout", "finished"))
            self.pending_connection = None
            self.processes = []

        def Pipe(self, duplex=False):
            assert duplex is True
            parent = _Connection(next(self.modes))
            child = _Connection("child")
            self.pending_connection = parent
            return parent, child

        def Process(self, **_kwargs):
            process = _Process(self.pending_connection)
            self.processes.append(process)
            return process

    context = _Context()

    class _Clock:
        """首个工作进程的句柄读取超时，之后时间不再推进。

        调用顺序：worker1 冷启动时刻 → 分配 PID 时刻 → 超时判定(6s>5s) → 其余。
        """

        def __init__(self):
            self.values = [0.0, 0.0, 6.0]

        def __call__(self):
            return self.values.pop(0) if self.values else 6.0

    clock = _Clock()
    monkeypatch.setattr(
        dialog_module.multiprocessing,
        "get_context",
        lambda _method: context,
    )
    monkeypatch.setattr(
        dialog_module,
        "_list_database_related_process_ids",
        lambda _data_root, _gui_pid: ([111, 222], 4),
    )
    monkeypatch.setattr(dialog_module.time, "monotonic", clock)
    results = []
    thread = dialog_module.OccupancyScanThread(str(tmp_path))
    thread.scan_finished.connect(results.append)

    thread.run()
    qt_app.processEvents()

    assert len(results) == 1
    assert results[0].total_processes == 4
    assert results[0].skipped_processes == 2
    assert results[0].scanned_processes == 2
    assert results[0].inaccessible_processes == 0
    assert results[0].errors == []
    assert len(context.processes) == 2
    assert context.processes[0].terminated is True
    assert context.processes[1].connection.stop_sent is True
    assert context.processes[1].connection.pid == 111
    assert all(process.closed for process in context.processes)


def test_scan_thread_reports_pid_only_after_two_consecutive_timeouts(
    qt_app, tmp_path, monkeypatch
):
    class _Connection:
        def __init__(self):
            self.closed = False

        def send(self, _value):
            pass

        def poll(self):
            return False

        def close(self):
            self.closed = True

    class _Process:
        def __init__(self):
            self.alive = True
            self.closed = False
            self.exitcode = 0

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.alive = False

        def join(self, _timeout):
            pass

        def kill(self):
            self.alive = False

        def close(self):
            self.closed = True

    created = []

    def create_worker(_thread, _context, slot):
        worker = {
            "slot": slot,
            "connection": _Connection(),
            "child_connection": None,
            "process": _Process(),
            "pid": None,
            "started_at": 0.0,
            # 本用例只验证句柄读取超时，直接按"已就绪"起算
            "ready": True,
            "spawned_at": 0.0,
        }
        created.append(worker)
        return worker

    clock = iter((0.0, 6.0, 6.0, 12.0))
    monkeypatch.setattr(
        dialog_module,
        "_list_database_related_process_ids",
        lambda _data_root, _gui_pid: ([111], 1),
    )
    monkeypatch.setattr(
        dialog_module.multiprocessing,
        "get_context",
        lambda _method: object(),
    )
    monkeypatch.setattr(
        dialog_module.OccupancyScanThread,
        "_create_started_worker",
        create_worker,
    )
    monkeypatch.setattr(dialog_module.time, "monotonic", lambda: next(clock))
    results = []
    thread = dialog_module.OccupancyScanThread(str(tmp_path))
    thread.scan_finished.connect(results.append)

    thread.run()
    qt_app.processEvents()

    assert len(results) == 1
    assert results[0].scanned_processes == 1
    assert results[0].inaccessible_processes == 1
    assert results[0].errors == ["PID 111: 句柄读取连续两次超过 5 秒，已跳过"]
    assert len(created) == 2
    assert all(worker["process"].closed for worker in created)


def test_scan_thread_has_overall_timeout_and_reports_unfinished_processes(
    qt_app, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        dialog_module,
        "_list_database_related_process_ids",
        lambda _data_root, _gui_pid: ([111, 222], 4),
    )
    monkeypatch.setattr(
        dialog_module,
        "_OCCUPANCY_SCAN_TOTAL_TIMEOUT_SECONDS",
        -1.0,
    )
    results = []
    thread = dialog_module.OccupancyScanThread(str(tmp_path))
    thread.scan_finished.connect(results.append)

    thread.run()
    qt_app.processEvents()

    assert len(results) == 1
    assert results[0].scanned_processes == 2
    assert results[0].inaccessible_processes == 2
    assert "整体检测达到 -1 秒上限" in results[0].errors[0]
    assert "2 个相关进程未完成" in results[0].errors[0]


def test_close_button_waits_for_running_scan_before_destroying_dialog(
    qt_app, tmp_path, monkeypatch
):
    monkeypatch.setattr(dialog_module, "OccupancyScanThread", _ControlledScanThread)
    viewer = QWidget()
    viewer.data_root = str(tmp_path)
    dialog = dialog_module.DatabaseOccupancyDialog(viewer)
    dialog.show()
    thread = dialog._scan_thread

    try:
        assert _wait_until(qt_app, thread.isRunning)
        dialog._on_scan_progress(3, 10, "正在检查 PID 123")
        assert "已完成 3/10" in dialog.scan_status.text()
        assert "PID 123" in dialog.scan_status.text()
        close_box = dialog.findChild(QDialogButtonBox)
        assert close_box.button(QDialogButtonBox.Close).text() == "关闭"
        close_box.button(QDialogButtonBox.Close).click()
        qt_app.processEvents()

        assert dialog.isVisible()
        assert dialog._close_pending is True
        assert thread.isRunning()
        assert "自动关闭" in dialog.scan_status.text()

        thread.release.set()
        assert _wait_until(
            qt_app,
            lambda: dialog._scan_thread is None and not dialog.isVisible(),
        )
        assert sip.isdeleted(thread) or not thread.isRunning()
    finally:
        thread.release.set()
        if not sip.isdeleted(thread):
            thread.wait(3000)
        dialog.close()
        dialog.deleteLater()
        viewer.deleteLater()
        qt_app.processEvents()


def test_window_close_uses_the_same_deferred_shutdown(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(dialog_module, "OccupancyScanThread", _ControlledScanThread)
    viewer = QWidget()
    viewer.data_root = str(tmp_path)
    dialog = dialog_module.DatabaseOccupancyDialog(viewer)
    dialog.show()
    thread = dialog._scan_thread

    try:
        assert _wait_until(qt_app, thread.isRunning)
        assert dialog.close() is False
        qt_app.processEvents()
        assert dialog.isVisible()
        assert dialog._close_pending is True

        thread.release.set()
        assert _wait_until(qt_app, lambda: not dialog.isVisible())
        assert dialog._scan_thread is None
    finally:
        thread.release.set()
        if not sip.isdeleted(thread):
            thread.wait(3000)
        dialog.close()
        dialog.deleteLater()
        viewer.deleteLater()
        qt_app.processEvents()


def test_repeated_immediate_close_leaves_no_running_scan_threads(
    qt_app, tmp_path, monkeypatch
):
    monkeypatch.setattr(dialog_module, "OccupancyScanThread", _ControlledScanThread)
    viewer = QWidget()
    viewer.data_root = str(tmp_path)

    try:
        for _index in range(10):
            dialog = dialog_module.DatabaseOccupancyDialog(viewer)
            dialog.show()
            thread = dialog._scan_thread
            assert _wait_until(qt_app, thread.isRunning)

            close_box = dialog.findChild(QDialogButtonBox)
            close_box.button(QDialogButtonBox.Close).click()
            close_box.button(QDialogButtonBox.Close).click()
            thread.release.set()

            assert _wait_until(qt_app, lambda: not dialog.isVisible())
            assert dialog._scan_thread is None
            if not sip.isdeleted(thread):
                assert not thread.isRunning()
            dialog.deleteLater()
            qt_app.processEvents()
    finally:
        viewer.deleteLater()
        qt_app.processEvents()


def test_status_refuses_clean_conclusion_when_no_handle_was_read(
    qt_app, tmp_path, monkeypatch
):
    """一个进程都没读成句柄时不能报"未发现占用"（假阴性）。

    2026-09-20 实测：27 个相关进程全部读取失败，界面仍显示"检测到 0 个占用进程"，
    而用户正是在数据库被占用、写不进去时才会打开这个工具。
    """
    monkeypatch.setattr(dialog_module, "OccupancyScanThread", _ControlledScanThread)
    viewer = QWidget()
    viewer.data_root = str(tmp_path)
    dialog = dialog_module.DatabaseOccupancyDialog(viewer)

    try:
        dialog._on_scan_finished(
            dialog_module.OccupancyScanResult(
                data_root=str(tmp_path),
                scanned_processes=27,
                inaccessible_processes=27,   # 27 个全部读取失败 → 成功读取 0 个
            )
        )
        text = dialog.scan_status.text()

        assert "诊断未完成" in text
        assert "未发现该数据目录的打开句柄" not in text
    finally:
        dialog.deleteLater()
        viewer.deleteLater()
        qt_app.processEvents()


def test_status_reports_no_occupancy_when_scan_actually_succeeded(
    qt_app, tmp_path, monkeypatch
):
    """句柄确实读成功且没有占用时，仍然给出干净结论。"""
    monkeypatch.setattr(dialog_module, "OccupancyScanThread", _ControlledScanThread)
    viewer = QWidget()
    viewer.data_root = str(tmp_path)
    dialog = dialog_module.DatabaseOccupancyDialog(viewer)

    try:
        dialog._on_scan_finished(
            dialog_module.OccupancyScanResult(
                data_root=str(tmp_path),
                scanned_processes=29,
                inaccessible_processes=0,    # 29 个全部读取成功
            )
        )
        text = dialog.scan_status.text()

        assert "未发现该数据目录的打开句柄" in text
        assert "诊断未完成" not in text
    finally:
        dialog.deleteLater()
        viewer.deleteLater()
        qt_app.processEvents()
