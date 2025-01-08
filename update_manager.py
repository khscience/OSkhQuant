# update_manager.py
import os
import json
import sys
import logging
import hashlib
import tempfile
import subprocess
import requests
from datetime import datetime
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QMessageBox, QProgressDialog, QApplication, QDialog,
    QVBoxLayout, QLabel, QProgressBar
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject
from version import get_version_info  # 导入版本信息
import ctypes


class UpdateCheckThread(QThread):
    """检查更新的线程类"""
    # 定义信号
    update_available = pyqtSignal(dict)  # 发现新版本时发出
    check_finished = pyqtSignal(bool, str)  # 检查完成时发出 (成功/失败, 消息)
    error_occurred = pyqtSignal(str)  # 发生错误时发出

    def __init__(self, current_version, update_url, update_channel='stable'):
        super().__init__()
        self.current_version = current_version
        self.update_url = update_url
        self.update_channel = update_channel
        self._is_running = True

    def stop(self):
        """停止线程"""
        self._is_running = False

    def run(self):
        """运行更新检查"""
        if not self._is_running:
            return

        try:
            logging.info(f"开始检查更新，当前版本：{self.current_version}")
            # 构建版本检查URL
            url = f"{self.update_url}/version.json"
            logging.debug(f"检查更新URL: {url}")

            # 发送请求，减少超时时间为3秒
            response = requests.get(url, timeout=3)
            response.raise_for_status()  # 抛出HTTP错误
            
            version_info = response.json()
            logging.debug(f"获取到版本信息: {version_info}")

            # 检查版本信息格式
            required_fields = ['version', 'force_update', 'filename', 'checksum']
            if not all(field in version_info for field in required_fields):
                raise ValueError("版本信息格式不正确")

            # 验证更新通道
            if 'channel' in version_info and version_info['channel'] != self.update_channel:
                logging.info(f"忽略不同通道的更新：{version_info['channel']}")
                self.check_finished.emit(True, "当前已是最新版本")
                return

            # 比较版本号
            if self.compare_versions(version_info['version'], self.current_version):
                logging.info(f"发现新版本：{version_info['version']}")
                self.update_available.emit(version_info)
            else:
                logging.info("当前已是最新版本")
                self.check_finished.emit(True, "当前已是最新版本")

        except requests.exceptions.ConnectionError:
            msg = "无法连接到更新服务器，已跳过更新检查"
            logging.info(msg)  # 改为info级别
            self.check_finished.emit(True, msg)  # 改为True，允许程序继续
        except requests.exceptions.Timeout:
            msg = "连接更新服务器超时，已跳过更新检查"
            logging.info(msg)  # 改为info级别
            self.check_finished.emit(True, msg)  # 改为True，允许程序继续
        except requests.exceptions.RequestException as e:
            msg = f"检查更新失败，已跳过更新检查: {str(e)}"
            logging.info(msg)  # 改为info级别，因为这是预期内的情况
            self.check_finished.emit(True, msg)  # 改为True，允许程序继续
        except ValueError as e:
            msg = f"版本信息格式无效，已跳过更新检查: {str(e)}"
            logging.warning(msg)  # 使用warning级别，因为这可能表示服务器配置问题
            self.check_finished.emit(True, msg)  # 改为True，允许程序继续
        except Exception as e:
            msg = f"检查更新时发生未知错误，已跳过更新检查: {str(e)}"
            logging.error(msg, exc_info=True)  # 保持error级别，因为这是意外错误
            self.check_finished.emit(True, msg)  # 改为True，允许程序继续

    @staticmethod
    def compare_versions(new_version, current_version):
        """比较版本号，如果new_version大于current_version返回True"""
        try:
            logging.debug(f"比较版本: 新版本={new_version}, 当前版本={current_version}")  # 添加日志
            
            # 移除版本号前的'v'或'V'并转换为小写
            new_version = new_version.lower().strip('v')
            current_version = current_version.lower().strip('v')
            
            # 将版本号分割为数字列表
            new_parts = [int(x) for x in new_version.split('.')]
            current_parts = [int(x) for x in current_version.split('.')]
            
            # 确保两个版本号列表长度相同
            while len(new_parts) < len(current_parts):
                new_parts.append(0)
            while len(current_parts) < len(new_parts):
                current_parts.append(0)
            
            # 逐位比较版本号
            for new, current in zip(new_parts, current_parts):
                if new > current:
                    logging.debug("发现新版本")  # 添加日志
                    return True
                elif new < current:
                    logging.debug("当前版本更新")  # 添加日志
                    return False
            
            # 如果所有位都相同，返回False（不需要更新）
            logging.debug("版本号相同")  # 添加日志
            return False
            
        except Exception as e:
            logging.error(f"版本号比较出错: {str(e)}", exc_info=True)
            return False


class UpdateDownloadThread(QThread):
    """下载更新的线程类"""
    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str)
    error_occurred = pyqtSignal(str)

    def __init__(self, url, save_path, version_info):
        super().__init__()
        self.url = url
        self.save_path = save_path
        self.version_info = version_info
        
    def run(self):
        try:
            # 构建完整的下载URL
            download_url = f"{self.url}/files/{self.version_info['filename']}"
            logging.info(f"开始下载更新文件: {download_url}")
            
            # 确保保存目录存在
            os.makedirs(self.save_path, exist_ok=True)
            
            # 构建保存文件路径，确保有.exe扩展名
            filename = self.version_info['filename']
            if not filename.lower().endswith('.exe'):
                filename += '.exe'
            save_file = os.path.join(self.save_path, filename)
            
            logging.info(f"更新文件将保存到: {save_file}")
            
            # 下载文件
            response = requests.get(download_url, stream=True)
            response.raise_for_status()
            
            # 获取文件大小
            total_size = int(response.headers.get('content-length', 0))
            
            # 先将文件下载到临时文件
            temp_file = save_file + '.tmp'
            try:
                with open(temp_file, 'wb') as f:
                    if total_size == 0:
                        f.write(response.content)
                    else:
                        downloaded = 0
                        for data in response.iter_content(chunk_size=8192):
                            downloaded += len(data)
                            f.write(data)
                            progress = int((downloaded / total_size) * 100)
                            self.progress.emit(progress)
                
                # 下载完成后，检查文件是否为有效的可执行文件
                if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
                    # 如果目标文件已存在，先删除
                    if os.path.exists(save_file):
                        os.remove(save_file)
                    # 将临时文件重命名为正式文件
                    os.rename(temp_file, save_file)
                    logging.info(f"更新包下载完成: {save_file}")
                    self.finished.emit(True, save_file)
                else:
                    raise Exception("下载文件无效")
                    
            except Exception as e:
                # 清理临时文件
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except:
                        pass
                raise e
                
        except Exception as e:
            error_msg = f"下载更新文件时出错: {str(e)}"
            logging.error(error_msg, exc_info=True)
            self.error_occurred.emit(error_msg)
            self.finished.emit(False, error_msg)


class UpdateProgressDialog(QDialog):
    """更新进度对话框"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("软件更新")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setFixedSize(400, 150)
        
        # 创建布局
        layout = QVBoxLayout()
        
        # 添加状态标签
        self.status_label = QLabel("正在下载更新...")
        layout.addWidget(self.status_label)
        
        # 添加进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        layout.addWidget(self.progress_bar)
        
        # 添加详细信息标签
        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)
        
        self.setLayout(layout)
        
    def update_progress(self, value):
        """更新进度条"""
        self.progress_bar.setValue(value)
        
    def update_status(self, status):
        """更新状态文本"""
        self.status_label.setText(status)
        
    def update_detail(self, detail):
        """更新详细信息"""
        self.detail_label.setText(detail)


class UpdateManager(QObject):
    """软件更新管理器"""
    # 添加类级别的信号定义
    check_finished = pyqtSignal(bool, str)  # 检查完成信号
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent = parent
        
        # 设置临时目录
        if getattr(sys, 'frozen', False):
            # 打包环境
            base_dir = os.path.dirname(sys.executable)
        else:
            # 开发环境
            base_dir = os.path.dirname(os.path.abspath(__file__))
        
        self.temp_dir = os.path.join(base_dir, 'temp')
        os.makedirs(self.temp_dir, exist_ok=True)
        
        # 尝试从version.py导入版本信息
        try:
            from version import get_version, get_version_info, get_channel
            self.current_version = get_version()
            self.update_channel = get_channel()
            self.version_info = get_version_info()
        except ImportError:
            logging.warning("无法导入version模块，使用默认版本信息")
            self.current_version = "1.0.0"  # 默认版本
            self.update_channel = "stable"  # 默认通道
            self.version_info = {
                "version": "1.0.0",
                "build_date": "2024-02-20",
                "channel": "stable",
                "app_name": "看海量化交易平台"
            }
        
        self.update_url = "https://khsci.com/khQuant/update"
        self.update_thread = None
        self.download_thread = None

    def show_current_version(self):
        """显示当前版本信息"""
        info = (
            f"{self.version_info.get('app_name', '看海量化交易平台')}\n"
            f"版本: {self.current_version}\n"
            f"构建日期: {self.version_info.get('build_date', '2024-02-20')}\n"
            f"更新通道: {self.update_channel}\n"
            f"\nCopyright © 2024 看海科技"
        )
        if self.parent:
            QMessageBox.about(self.parent, "版本信息", info)
        return info

    @staticmethod
    def compare_versions(new_version, current_version):
        """比较版本号，如果new_version大于current_version返回True"""
        try:
            logging.debug(f"比较版本: 新版本={new_version}, 当前版本={current_version}")  # 添加日志
            
            # 移除版本号前的'v'或'V'并转换为小写
            new_version = new_version.lower().strip('v')
            current_version = current_version.lower().strip('v')
            
            # 将版本号分割为数字列表
            new_parts = [int(x) for x in new_version.split('.')]
            current_parts = [int(x) for x in current_version.split('.')]
            
            # 确保两个版本号列表长度相同
            while len(new_parts) < len(current_parts):
                new_parts.append(0)
            while len(current_parts) < len(new_parts):
                current_parts.append(0)
            
            # 逐位比较版本号
            for new, current in zip(new_parts, current_parts):
                if new > current:
                    logging.debug("发现新版本")  # 添加日志
                    return True
                elif new < current:
                    logging.debug("当前版本更新")  # 添加日志
                    return False
            
            # 如果所有位都相同，返回False（不需要更新）
            logging.debug("版本号相同")  # 添加日志
            return False
            
        except Exception as e:
            logging.error(f"版本号比较出错: {str(e)}", exc_info=True)
            return False

    def check_for_updates(self, current_version):
        """
        检查更新
        :param current_version: 当前版本号
        """
        try:
            logging.info("开始检查软件更新")
            response = requests.post(
                'https://khsci.com/khQuant/wp-admin/admin-ajax.php',
                data={
                    'action': 'kh_check_update',
                    'version': current_version,
                    'channel': self.update_channel
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                logging.debug(f"服务器响应: {data}")  # 添加日志
                
                if data.get('success'):
                    version_info = data.get('data', {})
                    logging.debug(f"获取到版本信息: {version_info}")  # 添加日志
                    
                    # 检查是否有新版本
                    if self.compare_versions(version_info.get('version', ''), current_version):
                        logging.info(f"发现新版本: {version_info['version']}")
                        # 显示更新对话框
                        self.handle_update_available(version_info)
                    else:
                        logging.info("当前已是最新版本")
                        self.check_finished.emit(True, "当前已是最新版本")
                else:
                    error_msg = data.get('data', '检查更新失败')
                    logging.warning(f"检查更新失败: {error_msg}")
                    self.check_finished.emit(False, error_msg)
            else:
                error_msg = f"服务器响应错误: {response.status_code}"
                logging.error(error_msg)
                self.check_finished.emit(False, error_msg)
                
        except Exception as e:
            error_msg = f"检查更新时出错: {str(e)}"
            logging.error(error_msg, exc_info=True)
            self.check_finished.emit(False, error_msg)

    def handle_update_available(self, version_info):
        try:
            # 先发出成信号，避免界面卡住
            self.check_finished.emit(True, f"发现新版本：{version_info['version']}")
            
            # 使用 QTimer 延迟显示对话框
            QTimer.singleShot(100, lambda: self._show_update_dialog(version_info))
                
        except Exception as e:
            logging.error(f"处理更新信息时出错: {str(e)}", exc_info=True)
            self.check_finished.emit(False, f"处理更新信息时出错: {str(e)}")

    def _show_update_dialog(self, version_info):
        """显示更新对话框"""
        try:
            # 检查是否为强制更新
            is_force_update = version_info.get('force_update', False)
            
            if is_force_update:
                # 强制更新时显示不同的对话框
                msg = QMessageBox(self.parent)
                msg.setIcon(QMessageBox.Warning)
                msg.setWindowTitle("强制更新")
                msg.setText(f"发现重要更新 {version_info['version']}\n此更新为强制更新，必须安装才能继续使用。")
                msg.setInformativeText(f"更新说明：\n{version_info.get('description', '无更新说明')}")
                msg.setStandardButtons(QMessageBox.Yes)
                msg.setDefaultButton(QMessageBox.Yes)
                
                # 重写关闭事件
                msg.closeEvent = lambda event: self.handle_force_update_close(event)
                
                reply = msg.exec_()
                if reply == QMessageBox.Yes:
                    self.download_update(version_info)
                else:
                    # 强制更新时，如果用户选择不更新，直接退出程序
                    QApplication.quit()
            else:
                # 普通更新时的对话框
                reply = QMessageBox.question(
                    self.parent,
                    "发现新版本",
                    f"发现新版本 {version_info['version']}\n\n"
                    f"更新说明：\n{version_info.get('description', '无更新说明')}\n\n"
                    "是否现在更新？",
                    QMessageBox.Yes | QMessageBox.No
                )
                
                if reply == QMessageBox.Yes:
                    self.download_update(version_info)
            
        except Exception as e:
            logging.error(f"显示更新对话框时出错: {str(e)}", exc_info=True)
            QMessageBox.critical(self.parent, "错误", f"显示更新对话框时出错: {str(e)}")

    def handle_force_update_close(self, event):
        """处理强制更新对话框的关闭事件"""
        logging.info("用户尝试关闭强制更新对话框")
        QApplication.quit()
        event.accept()

    def download_update(self, version_info):
        """下载更新"""
        try:
            # 创建进度对话框
            self.progress_dialog = UpdateProgressDialog(self.parent)
            self.progress_dialog.setModal(True)
            
            # 如果是强制更新，禁用关闭按钮
            if version_info.get('force_update', False):
                self.progress_dialog.setWindowFlags(
                    self.progress_dialog.windowFlags() & ~Qt.WindowCloseButtonHint
                )
            
            # 确保下载URL正确
            download_url = self.update_url.rstrip('/')  # 移除末尾的斜杠
            
            # 创建下载线程
            self.download_thread = UpdateDownloadThread(
                download_url,
                self.temp_dir,
                version_info
            )
            
            # 连接信号
            self.download_thread.progress.connect(self.progress_dialog.update_progress)
            self.download_thread.finished.connect(lambda success, result: 
                self.handle_download_finished(success, result, version_info))
            self.download_thread.error_occurred.connect(self.handle_error)
            
            # 开始下载
            self.download_thread.start()
            self.progress_dialog.exec_()
            
        except Exception as e:
            logging.error(f"启动下载时出错: {str(e)}", exc_info=True)
            if version_info.get('force_update', False):
                QMessageBox.critical(self.parent, "错误", f"下载更新失败: {str(e)}\n程序将退出。")
                QApplication.quit()
            else:
                QMessageBox.warning(self.parent, "下载错误", f"启动下载时出错: {str(e)}")

    def handle_download_close(self, event, version_info):
        """处理下载对话框的关闭事件"""
        # 停止下载线程
        if self.download_thread and self.download_thread.isRunning():
            self.download_thread.terminate()
            self.download_thread.wait()
        
        if version_info.get('force_update', False):
            # 对于强制更新，直接退出程序
            QApplication.quit()
        
        event.accept()

    def handle_download_finished(self, success, result, version_info):
        """处理下载完成"""
        try:
            self.progress_dialog.close()
            
            if success:
                if version_info.get('force_update', False):
                    # 强制更新时显示带有强制更新说明的确认对话框
                    reply = QMessageBox.question(
                        self.parent,
                        "重要更新",
                        "重要更新包已下载完成，此更新包含关键功能更新，需要立即安装。\n"
                        "安装过程中软件将关闭，请确认是否现在安装？",
                        QMessageBox.Yes | QMessageBox.No
                    )
                    
                    if reply == QMessageBox.Yes:
                        self.install_update(result)
                    else:
                        # 如果用户选择不安装强制更新，则退出程序
                        QApplication.quit()
                else:
                    # 普通更新显示标准确认对话框
                    reply = QMessageBox.question(
                        self.parent,
                        "下载完成",
                        "更新包下载完成，是否现在安装？\n"
                        "安装过程中软件将关闭。",
                        QMessageBox.Yes | QMessageBox.No
                    )
                    
                    if reply == QMessageBox.Yes:
                        self.install_update(result)
            else:
                error_msg = f"下载更新失败: {result}"
                if version_info.get('force_update', False):
                    QMessageBox.critical(self.parent, "下载失败", f"{error_msg}\n程序将退出。")
                    QApplication.quit()
                else:
                    QMessageBox.critical(self.parent, "下载失败", error_msg)

        except Exception as e:
            logging.error(f"处理下载完成时出错: {str(e)}", exc_info=True)
            if version_info.get('force_update', False):
                QMessageBox.critical(self.parent, "错误", f"处理下载完成时出错: {str(e)}\n程序将退出。")
                QApplication.quit()
            else:
                QMessageBox.critical(self.parent, "错误", f"处理下载完成时出错: {str(e)}")

    def install_update(self, update_file):
        """安装更新"""
        try:
            logging.info(f"准备安装更新: {update_file}")
            
            # 验证更新文件是否存在且为exe
            if not os.path.exists(update_file) or not update_file.endswith('.exe'):
                raise Exception(f"无效的更新文件: {update_file}")
            
            # 创建更新批处理文件
            batch_file = os.path.join(self.temp_dir, "update.bat")
            current_exe = sys.executable
            current_pid = str(os.getpid())
            
            # 修改批处理文件内容，增加更多的安全检查和延迟
            batch_content = [
                '@echo off',
                'setlocal EnableDelayedExpansion',
                'cd /d %~dp0',
                
                # 等待主程序退出
                'echo 正在等待程序关闭...',
                ':wait_loop',
                f'tasklist | find "{os.path.basename(current_exe)}" >nul 2>&1',
                'if not errorlevel 1 (',
                '    timeout /t 2 /nobreak >nul',
                '    goto wait_loop',
                ')',
                
                # 增加额外的等待时间确保进程完全关闭
                'echo 确保程序完全关闭...',
                'timeout /t 5 /nobreak >nul',
                
                # 强制结束可能残留的进程
                f'taskkill /F /FI "IMAGENAME eq {os.path.basename(current_exe)}" /T >nul 2>&1',
                'timeout /t 2 /nobreak >nul',
                
                # 开始安装
                'echo 正在安装更新...',
                f'start /wait "" "{os.path.abspath(update_file)}" /SILENT /SUPPRESSMSGBOXES /NOCANCEL /NORESTART',
                
                # 检查安装结果
                'if errorlevel 1 (',
                '    echo 安装失败',
                '    pause',
                '    exit /b 1',
                ')',
                
                # 增加安装后的等待时间
                'echo 安装完成，正在完成最后配置...',
                'timeout /t 10 /nobreak >nul',
                
                # 清理文件
                'echo 清理临时文件...',
                f'del /F /Q "{update_file}"',
                
                # 使用 wscript 来隐藏启动窗口
                'echo 正在启动新版本...',
                'echo Set WshShell = CreateObject("WScript.Shell") > "%temp%\\start_app.vbs"',
                f'echo WshShell.Run """{current_exe}""", 1, False >> "%temp%\\start_app.vbs"',
                'wscript "%temp%\\start_app.vbs"',
                'timeout /t 2 /nobreak >nul',
                'del "%temp%\\start_app.vbs"',
                
                # 删除批处理自身
                'timeout /t 1 /nobreak >nul',
                '(goto) 2>nul & del "%~f0"'
            ]
            
            # 写入批处理文件
            with open(batch_file, 'w', encoding='gbk') as f:
                f.write('\n'.join(batch_content))
            
            logging.info("正在启动更新程序...")
            
            # 启动更新批处理
            if ctypes.windll.shell32.IsUserAnAdmin() == 0:
                ctypes.windll.shell32.ShellExecuteW(
                    None,
                    "runas",
                    "cmd.exe",
                    f'/c "{batch_file}"',
                    os.path.dirname(batch_file),
                    1
                )
            else:
                subprocess.Popen(
                    f'cmd /c "{batch_file}"',
                    shell=True,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                )
            
            # 延长退出延迟时间
            QTimer.singleShot(2000, QApplication.quit)
            
        except Exception as e:
            error_msg = f"安装更新时出错: {str(e)}"
            logging.error(error_msg, exc_info=True)
            QMessageBox.critical(self.parent, "安装失败", error_msg)

    def handle_error(self, error_msg):
        """处理错误"""
        logging.error(f"更新过程出错: {error_msg}")
        QMessageBox.critical(self.parent, "更新错误", error_msg)

    def on_check_finished(self, success, message):
        """处理更新检查完成"""
        if not success and not message.startswith("当前已是最新版本"):
            logging.warning(f"更新检查失败: {message}")

    def cleanup(self):
        """清理临时文件和线程"""
        try:
            # 停止所有线程
            if self.update_thread and self.update_thread.isRunning():
                self.update_thread.stop()
                self.update_thread.wait()

            if self.download_thread and self.download_thread.isRunning():
                self.download_thread.stop()
                self.download_thread.wait()

            # 清理临时文件
            if os.path.exists(self.temp_dir):
                for file in os.listdir(self.temp_dir):
                    try:
                        file_path = os.path.join(self.temp_dir, file)
                        if os.path.isfile(file_path):
                            os.remove(file_path)
                    except Exception as e:
                        logging.error(f"清理临时文件失败: {str(e)}")

        except Exception as e:
            logging.error(f"清理更新管理器时出错: {str(e)}")

    def __del__(self):
        """析构函数，确保资源被正确释放"""
        self.cleanup()

def check_update():
    current_version = "V1.1.1"  # 获取当前版本号
    data = {
        'action': 'check_update',
        'client_version': current_version
    }
    # 发送请求到服务器