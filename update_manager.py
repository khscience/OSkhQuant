# update_manager.py
"""开源版的更新检查：走 V2.x 系列的 s1 通道，发现新版本只提示、给出下载链接。

- 不走 CS 的 series2-update.php；
- 从不强制更新：服务器记录里即使 force_update=true 也只提示；
- 不自动下载、不运行安装包、不结束任何进程。
"""
import logging
import threading

import requests
from PyQt5.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QMessageBox

UPDATE_CHECK_URL = "https://khsci.com/khQuant/wp-admin/admin-ajax.php"
UPDATE_CHECK_ACTION = "kh_check_update"
UPDATE_FILES_BASE = "https://khsci.com/khQuant/update"
RELEASES_URL = "https://github.com/khscience/OSkhQuant/releases/latest"


class UpdateManager(QObject):
    check_finished = pyqtSignal(bool, str)  # (成功/失败, 消息)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent = parent
        self.auto_check = True
        try:
            from version import get_channel, get_version, get_version_info
            self.current_version = get_version()
            self.update_channel = get_channel()
            self.version_info = get_version_info()
        except ImportError:
            logging.warning("无法导入version模块，使用默认版本信息")
            self.current_version = "0.0.0"
            self.update_channel = "stable"
            self.version_info = {"version": "0.0.0", "channel": "stable", "app_name": "看海量化回测平台（开源版）"}

    def show_current_version(self):
        """显示当前版本信息"""
        info = (
            f"{self.version_info.get('app_name', '看海量化回测平台（开源版）')}\n"
            f"版本: {self.current_version}\n"
            f"构建日期: {self.version_info.get('build_date', '')}\n"
            f"\n开源地址: https://github.com/khscience/OSkhQuant"
        )
        if self.parent:
            QMessageBox.about(self.parent, "版本信息", info)
        return info

    @staticmethod
    def compare_versions(new_version, current_version):
        """new_version 比 current_version 新时返回 True（支持 V 前缀）。"""
        try:
            new_parts = [int(x) for x in str(new_version).lower().strip().lstrip('v').split('.')]
            current_parts = [int(x) for x in str(current_version).lower().strip().lstrip('v').split('.')]
        except (TypeError, ValueError):
            logging.warning(f"版本号无法比较: {new_version!r} / {current_version!r}")
            return False
        width = max(len(new_parts), len(current_parts))
        new_parts += [0] * (width - len(new_parts))
        current_parts += [0] * (width - len(current_parts))
        return new_parts > current_parts

    def check_for_updates(self, current_version):
        """在后台线程里查询 s1 通道，避免阻塞界面。"""

        def _do_check():
            try:
                logging.info("开始检查软件更新")
                response = requests.post(
                    UPDATE_CHECK_URL,
                    data={
                        'action': UPDATE_CHECK_ACTION,
                        'version': current_version,
                        'channel': self.update_channel,
                    },
                    timeout=5,
                )
                if response.status_code != 200:
                    self.check_finished.emit(False, f"服务器响应错误: {response.status_code}")
                    return
                data = response.json()
                if not data.get('success'):
                    self.check_finished.emit(False, str(data.get('data', '检查更新失败')))
                    return
                version_info = data.get('data', {}) or {}
                if self.compare_versions(version_info.get('version', ''), current_version):
                    logging.info(f"发现新版本: {version_info.get('version')}")
                    QTimer.singleShot(0, lambda vi=version_info: self._show_update_dialog(vi))
                    self.check_finished.emit(True, f"发现新版本：{version_info.get('version')}")
                else:
                    self.check_finished.emit(True, "当前已是最新版本")
            except Exception as exc:  # noqa: BLE001 - 网络问题只记日志，不打扰用户
                logging.warning(f"检查更新时出错: {exc}")
                self.check_finished.emit(False, f"检查更新时出错: {exc}")

        threading.Thread(target=_do_check, daemon=True).start()

    def _show_update_dialog(self, version_info):
        """提示有新版本（从不强制）；用户选择后在浏览器里打开下载页。"""
        try:
            download_url = version_info.get('download_url') or (
                f"{UPDATE_FILES_BASE}/files/{version_info.get('filename')}"
                if version_info.get('filename') else RELEASES_URL
            )
            msg = QMessageBox(self.parent)
            msg.setIcon(QMessageBox.Information)
            msg.setWindowTitle("发现新版本")
            msg.setTextFormat(Qt.RichText)
            msg.setText(
                f"发现新版本 {version_info.get('version', '')}"
                f"<br>下载地址：<a href=\"{download_url}\">{download_url}</a>"
                f"<br>也可以在 GitHub / Gitee 的 Releases 页面下载。"
            )
            msg.setInformativeText(f"更新说明：\n{version_info.get('description', '无更新说明')}")
            later_btn = msg.addButton("稍后", QMessageBox.NoRole)
            open_btn = msg.addButton("打开下载页", QMessageBox.ActionRole)
            msg.setDefaultButton(later_btn)
            msg.exec_()
            if msg.clickedButton() is open_btn:
                QDesktopServices.openUrl(QUrl(download_url))
        except Exception as exc:  # noqa: BLE001
            logging.error(f"显示更新对话框时出错: {exc}", exc_info=True)
