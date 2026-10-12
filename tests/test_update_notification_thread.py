"""A real worker and Qt event loop must deliver the update dialog on the GUI thread."""
from types import SimpleNamespace
import threading

from PyQt5.QtCore import QEventLoop, QThread, QTimer
from PyQt5.QtWidgets import QApplication

import update_manager


def test_update_discovered_in_background_reaches_gui_thread(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(update_manager, '__file__', str(tmp_path / 'update_manager.py'))
    requested_on, dialogs, finished = [], [], []
    def post(*args, **kwargs):
        requested_on.append(threading.get_ident())
        return SimpleNamespace(status_code=200, json=lambda: {
            'success': True, 'data': {'version': '99.0.0', 'force_update': False},
        })
    monkeypatch.setattr(update_manager.requests, 'post', post)
    manager = update_manager.UpdateManager()
    loop = QEventLoop()
    def show(info):
        dialogs.append((info['version'], QThread.currentThread() == app.thread()))
        if finished:
            loop.quit()
    def record_finished(ok, message):
        finished.append((ok, message))
        if dialogs:
            loop.quit()
    monkeypatch.setattr(manager, '_show_update_dialog', show)
    manager.check_finished.connect(record_finished)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    timeout.start(2000)
    manager.check_for_updates('0.0.1')
    loop.exec_()
    timeout.stop()
    assert requested_on and requested_on[0] != threading.get_ident()
    assert dialogs == [('99.0.0', True)]
    assert finished and finished[0][0] is True
