# -*- coding: utf-8 -*-
"""
回测历史结果管理模块
功能：
1. 列出所有历史回测结果
2. 查看回测结果详情
3. 删除指定回测结果
4. 批量删除回测结果
5. 回测结果对比分析
6. 导出回测结果
"""

import os
import sys
import shutil
import json
import pandas as pd
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTableWidget,
    QTableWidgetItem, QPushButton, QLabel, QMessageBox, QHeaderView,
    QCheckBox, QLineEdit, QComboBox, QDateEdit, QGroupBox, QSplitter,
    QTextEdit, QProgressBar, QFileDialog, QTabWidget, QFrame, QDialog,
    QStyledItemDelegate
)
from PyQt5.QtCore import Qt, QDate, pyqtSignal, QThread, pyqtSlot, QTimer, QRect, QEvent
from PyQt5.QtGui import QFont, QIcon, QColor, QPainter
import csv
import json
import logging
from khPathUtils import get_backtest_results_dir, get_backtest_results_dirs


_HISTORY_CACHE_VERSION = 1
_HISTORY_CACHE_FILES = ("config.csv", "summary.csv", "daily_stats.csv", "trades.csv")


def _history_cache_path() -> str:
    """返回历史索引缓存路径，确保源码版和安装版都不会写入程序目录。"""
    from kh_app_identity import local_appdata_dir

    return os.path.join(local_appdata_dir("cache"), "backtest_history_index.json")


class _ActionButtonDelegate(QStyledItemDelegate):
    """操作列的"报告/还原/删除"按钮直接由delegate绘制，不创建真实控件。

    之前用setCellWidget逐行创建按钮：QTableWidget每次几何更新都会遍历
    全部单元格控件（O(总行数)），数千行时加载、滚动、重绘全部严重卡顿。
    delegate只绘制可见行，开销与总行数无关。
    """
    action_clicked = pyqtSignal(int, str)  # (表格行号, 动作key)

    _BUTTONS = [
        ("报告", "report", "#0078d4"),
        ("还原", "restore", "#0078d4"),
        ("删除", "delete", "#d13438"),
    ]
    _BTN_W, _BTN_H, _GAP = 50, 25, 8

    def _button_rects(self, cell_rect):
        n = len(self._BUTTONS)
        total_w = n * self._BTN_W + (n - 1) * self._GAP
        x = cell_rect.x() + max((cell_rect.width() - total_w) // 2, 2)
        y = cell_rect.y() + max((cell_rect.height() - self._BTN_H) // 2, 0)
        return [QRect(x + i * (self._BTN_W + self._GAP), y, self._BTN_W, self._BTN_H)
                for i in range(n)]

    def paint(self, painter, option, index):
        super().paint(painter, option, index)  # 先画默认背景（选中高亮等）
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        for rect, (label, _key, color) in zip(self._button_rects(option.rect), self._BUTTONS):
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(rect, 3, 3)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(rect, Qt.AlignCenter, label)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            for rect, (_label, key, _color) in zip(self._button_rects(option.rect), self._BUTTONS):
                if rect.contains(event.pos()):
                    self.action_clicked.emit(index.row(), key)
                    return True
        return super().editorEvent(event, model, option, index)


class _HistoryScanThread(QThread):
    """后台扫描线程：解析所有回测结果目录。

    目录数可能上千（每个目录要读1-2个CSV），同步执行会把主界面和
    管理器窗口一起冻结十几秒，必须放后台线程。
    线程内只做文件IO和纯计算，不触碰任何Qt控件。
    """
    progress = pyqtSignal(int)        # 已处理的目录数
    finished_scan = pyqtSignal(object)  # 解析结果、缓存和统计
    error = pyqtSignal(str)

    def __init__(self, manager, result_dirs, cache_entries=None):
        super().__init__(manager)
        self._manager = manager
        self._result_dirs = result_dirs
        self._cache_entries = cache_entries or {}

    def run(self):
        try:
            data = []
            refreshed_cache = {}
            cache_hits = 0
            for i, (_base_dir, dir_name, result_path) in enumerate(self._result_dirs):
                if self.isInterruptionRequested():
                    return
                cache_key = os.path.normcase(os.path.abspath(result_path))
                fingerprint = self._manager.result_fingerprint(result_path)
                cached = self._cache_entries.get(cache_key) or {}
                if cached.get("fingerprint") == fingerprint and isinstance(cached.get("result"), dict):
                    result_info = dict(cached["result"])
                    cache_hits += 1
                else:
                    result_info = self._manager.parse_result_directory(result_path, dir_name)
                if result_info:
                    data.append(result_info)
                    refreshed_cache[cache_key] = {
                        "fingerprint": fingerprint,
                        "result": result_info,
                    }
                if (i + 1) % 100 == 0:
                    self.progress.emit(i + 1)

            # 按创建时间排序（最新的在前）
            data.sort(key=lambda x: x.get('create_time', ''), reverse=True)
            self.progress.emit(len(self._result_dirs))
            self.finished_scan.emit({
                "data": data,
                "cache": refreshed_cache,
                "cache_hits": cache_hits,
                "scanned": len(self._result_dirs),
            })
        except Exception as e:
            logging.error(f"扫描回测结果目录线程出错: {e}", exc_info=True)
            self.error.emit(str(e))


class BacktestHistoryManager(QMainWindow):
    """回测历史结果管理器"""
    
    # 信号定义已移除，因为详情面板功能已被移除
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.parent = parent
        self.backtest_results_dir = get_backtest_results_dir(create=False)
        self.backtest_results_dirs = get_backtest_results_dirs(include_legacy=True, include_missing=False)
        self.results_data = []  # 存储回测结果数据
        self._all_results_data = []
        self._scan_thread = None  # 后台扫描线程
        self._closing_after_scan = False

        self.init_ui()
        self.load_results()
        self.apply_dark_theme()

    def closeEvent(self, event):
        """关闭窗口前停止后台扫描线程，避免线程向已销毁的窗口发信号"""
        self._fill_generation = getattr(self, '_fill_generation', 0) + 1
        try:
            if self._scan_thread is not None and self._scan_thread.isRunning():
                self._scan_thread.requestInterruption()
                if not self._scan_thread.wait(3000):
                    # 文件系统偶发阻塞时不能销毁仍在运行的QThread。先隐藏窗口，
                    # 待线程自然退出后再完成关闭。
                    if not self._closing_after_scan:
                        self._closing_after_scan = True
                        self.hide()
                        self._scan_thread.finished.connect(self.close)
                    event.ignore()
                    return
        except Exception as e:
            logging.warning(f"停止扫描线程时出错: {e}")
        try:
            self.results_table.setRowCount(0)
            self.results_data.clear()
            self._all_results_data.clear()
        except Exception:
            pass
        event.accept()
        
    def init_ui(self):
        """初始化用户界面"""
        # 设置窗口标题栏颜色（仅适用于Windows）
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
            caption_color = DWORD(0x2b2b2b)  # 使用与主界面相同的颜色
            windll.dwmapi.DwmSetWindowAttribute(
                int(self.winId()),
                DWMWA_CAPTION_COLOR,
                byref(caption_color),
                sizeof(caption_color)
            )

        except Exception as e:
            logging.warning(f"设置标题栏深色模式失败: {str(e)}")
        
        self.setWindowTitle("回测历史结果管理器")
        # 根据表格列的实际宽度设置窗口宽度
        # 选择列80px + 操作列320px + 9个数据列(每列约120px) = 80+320+1080 = 1480px
        # 加上边距和滚动条等，设置为1620px
        self.setGeometry(100, 100, 1620, 800)
        
        # 设置窗口图标
        self.load_icon()
        
        # 创建中央窗口部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 创建主布局
        main_layout = QVBoxLayout(central_widget)
        
        # 创建工具栏
        self.create_toolbar(main_layout)
        
        # 创建结果列表
        self.create_results_panel(main_layout)
        
        # 创建状态栏
        self.create_status_bar()
        
    def load_icon(self):
        """加载窗口图标"""
        icon_paths = [
            os.path.join("icons", "stock_icon.ico"),
            os.path.join("icons", "stock_icon.png"),
            "stock_icon.ico",
            "stock_icon.png"
        ]
        
        for icon_path in icon_paths:
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
                break
                
    def create_toolbar(self, parent_layout):
        """创建工具栏"""
        toolbar_frame = QFrame()
        toolbar_layout = QHBoxLayout(toolbar_frame)
        
        # 刷新按钮
        self.refresh_btn = QPushButton("刷新列表")
        self.refresh_btn.clicked.connect(self.load_results)
        toolbar_layout.addWidget(self.refresh_btn)
        
        # 删除选中按钮
        self.delete_selected_btn = QPushButton("删除选中")
        self.delete_selected_btn.clicked.connect(self.delete_selected_results)
        toolbar_layout.addWidget(self.delete_selected_btn)
        
        # 导出按钮和对比分析按钮已移除
        
        # 搜索框
        toolbar_layout.addWidget(QLabel("搜索:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入策略名称或日期范围...")
        self.search_edit.textChanged.connect(self.filter_results)
        toolbar_layout.addWidget(self.search_edit)
        
        # 筛选下拉框
        toolbar_layout.addWidget(QLabel("筛选:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["全部", "最近7天", "最近30天", "最近90天", "自定义日期"])
        self.filter_combo.currentTextChanged.connect(self.filter_by_date)
        toolbar_layout.addWidget(self.filter_combo)

        self.show_invalid_checkbox = QCheckBox("显示不完整结果")
        self.show_invalid_checkbox.setToolTip(
            "默认隐藏缺少配置或回测统计的中断/损坏结果；勾选后可查看并删除"
        )
        self.show_invalid_checkbox.toggled.connect(self._apply_result_visibility)
        toolbar_layout.addWidget(self.show_invalid_checkbox)
        
        toolbar_layout.addStretch()
        parent_layout.addWidget(toolbar_frame)
        
    def create_results_panel(self, parent):
        """创建结果列表面板"""
        results_widget = QWidget()
        results_layout = QVBoxLayout(results_widget)
        
        # 标题
        title_label = QLabel("回测历史结果")
        title_label.setFont(QFont("Arial", 12, QFont.Bold))
        results_layout.addWidget(title_label)
        
        # 创建表格
        self.results_table = QTableWidget()
        self.setup_results_table()
        results_layout.addWidget(self.results_table)
        
        parent.addWidget(results_widget)
        
    def setup_results_table(self):
        """设置结果表格"""
        headers = [
            "选择", "策略名称", "开始日期", "结束日期", "初始资金", 
            "最终资产", "总收益率", "年化收益率", "最大回撤", 
            "创建时间", "操作"
        ]
        
        self.results_table.setColumnCount(len(headers))
        self.results_table.setHorizontalHeaderLabels(headers)

        # 操作列用delegate绘制按钮（见_ActionButtonDelegate注释）
        self._action_delegate = _ActionButtonDelegate(self.results_table)
        self._action_delegate.action_clicked.connect(self._on_action_clicked)
        self.results_table.setItemDelegateForColumn(10, self._action_delegate)
        
        # 设置表格属性
        self.results_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setSortingEnabled(True)
        
        # 设置列宽随窗口缩放
        header = self.results_table.horizontalHeader()
        
        # 设置选择列为固定宽度
        header.setSectionResizeMode(0, QHeaderView.Fixed)  # 选择列固定宽度
        self.results_table.setColumnWidth(0, 80)
        
        # 操作列也设置为固定宽度
        header.setSectionResizeMode(10, QHeaderView.Fixed)  # 操作列固定宽度
        self.results_table.setColumnWidth(10, 320)
        
        # 创建时间列：不能用ResizeToContents——该模式下每次setItem都会触发
        # 全表列宽扫描，数千行时填充耗时从0.1秒级恶化到20秒级（O(N²)）。
        # 改为固定初始宽度，填充完成后再一次性按内容调整（见_fill_table_batch）
        header.setSectionResizeMode(9, QHeaderView.Interactive)
        self.results_table.setColumnWidth(9, 160)
        
        # 其他数据列设置为拉伸模式，随窗口大小等比例缩放
        for col in range(1, 9):  # 列1-8设置为拉伸模式
            header.setSectionResizeMode(col, QHeaderView.Stretch)
            
        # 移除详情面板相关的信号连接
    def create_status_bar(self):
        """创建状态栏"""
        self.status_label = QLabel("就绪")
        self.statusBar().addWidget(self.status_label)
        
        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress_bar)
        
    def load_results(self):
        """加载回测结果（目录解析放后台线程，避免冻结主界面和本窗口）"""
        # 正在加载时忽略重复请求（例如打开窗口后立刻点刷新）
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return

        self.status_label.setText("正在加载回测结果...")
        self.refresh_btn.setEnabled(False)
        self.progress_bar.setVisible(True)

        try:
            self.backtest_results_dir = get_backtest_results_dir(create=False)
            self.backtest_results_dirs = get_backtest_results_dirs(include_legacy=True, include_missing=False)

            if not self.backtest_results_dirs:
                self.status_label.setText("回测结果目录不存在")
                self.progress_bar.setVisible(False)
                self.refresh_btn.setEnabled(True)
                return

            # 列出所有回测结果目录（仅listdir，开销小，留在主线程）
            result_dirs = []
            seen_paths = set()
            for base_dir in self.backtest_results_dirs:
                try:
                    for dir_name in os.listdir(base_dir):
                        result_path = os.path.join(base_dir, dir_name)
                        if not os.path.isdir(result_path):
                            continue
                        key = os.path.normcase(os.path.abspath(result_path))
                        if key in seen_paths:
                            continue
                        seen_paths.add(key)
                        result_dirs.append((base_dir, dir_name, result_path))
                except OSError as e:
                    logging.warning(f"读取回测结果目录失败 {base_dir}: {e}")

            self.progress_bar.setMaximum(max(len(result_dirs), 1))
            self.progress_bar.setValue(0)

            # 解析交给后台线程，完成后回调 _on_scan_finished 更新表格
            cache_entries = self._load_history_cache()
            self._scan_thread = _HistoryScanThread(self, result_dirs, cache_entries)
            self._scan_thread.progress.connect(self._on_scan_progress)
            self._scan_thread.finished_scan.connect(self._on_scan_finished)
            self._scan_thread.error.connect(self._on_scan_error)
            self._scan_thread.finished.connect(self._on_scan_thread_finished)
            self._scan_thread.start()

        except Exception as e:
            self.status_label.setText(f"加载失败: {str(e)}")
            QMessageBox.critical(self, "错误", f"加载回测结果时出错:\n{str(e)}")
            self.progress_bar.setVisible(False)
            self.refresh_btn.setEnabled(True)

    def _on_scan_progress(self, done):
        self.progress_bar.setValue(done)

    def _on_scan_finished(self, payload):
        """后台扫描完成，在主线程更新表格"""
        if self._closing_after_scan:
            return
        data = list((payload or {}).get("data") or [])
        self._all_results_data = data
        self._save_history_cache((payload or {}).get("cache") or {})
        self._apply_result_visibility()
        invalid_count = sum(not item.get("is_valid", True) for item in data)
        cache_hits = int((payload or {}).get("cache_hits", 0) or 0)
        dir_count = len(self.backtest_results_dirs)
        self.show_invalid_checkbox.setText(f"显示不完整结果（{invalid_count}）")
        self.status_label.setText(
            f"已加载 {len(data) - invalid_count} 个有效结果，隔离 {invalid_count} 个不完整结果"
            f"（缓存命中 {cache_hits}，扫描 {dir_count} 个目录）"
        )
        self.progress_bar.setVisible(False)
        self.refresh_btn.setEnabled(True)

    def _on_scan_thread_finished(self):
        thread = self.sender()
        if thread is self._scan_thread:
            self._scan_thread = None
        if thread is not None:
            thread.deleteLater()

    def _on_scan_error(self, msg):
        self.status_label.setText(f"加载失败: {msg}")
        QMessageBox.critical(self, "错误", f"加载回测结果时出错:\n{msg}")
        self.progress_bar.setVisible(False)
        self.refresh_btn.setEnabled(True)

    def _apply_result_visibility(self, *_args):
        """默认只展示完整结果；需要清理旧垃圾记录时可显式展开。"""
        show_invalid = bool(getattr(self, 'show_invalid_checkbox', None) and
                            self.show_invalid_checkbox.isChecked())
        self.results_data = [
            item for item in self._all_results_data
            if show_invalid or item.get("is_valid", True)
        ]
        self.update_results_table()

    @staticmethod
    def result_fingerprint(result_path: str):
        """用关键结果文件的大小和纳秒修改时间判断目录是否变化。"""
        fingerprint = []
        for filename in _HISTORY_CACHE_FILES:
            path = os.path.join(result_path, filename)
            try:
                stat = os.stat(path)
                fingerprint.append([filename, int(stat.st_size), int(stat.st_mtime_ns)])
            except OSError:
                fingerprint.append([filename, -1, -1])
        return fingerprint

    @staticmethod
    def _load_history_cache() -> Dict:
        path = _history_cache_path()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("version") == _HISTORY_CACHE_VERSION:
                entries = payload.get("entries")
                if isinstance(entries, dict):
                    return entries
        except (OSError, ValueError, TypeError):
            pass
        return {}

    @staticmethod
    def _save_history_cache(entries: Dict) -> None:
        path = _history_cache_path()
        temp_path = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {"version": _HISTORY_CACHE_VERSION, "entries": entries},
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            os.replace(temp_path, path)
        except OSError as exc:
            logging.warning(f"保存回测历史索引缓存失败: {exc}")
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            
    @staticmethod
    def _read_first_csv_row(path: str) -> Optional[Dict]:
        """读取单行CSV（config.csv/summary.csv）的首行数据为dict。

        这类文件只有一行数据，用标准库csv比pandas.read_csv快一个数量级——
        上千个结果目录场景下这是加载耗时的主要来源。
        """
        try:
            with open(path, 'r', encoding='utf-8-sig', newline='') as f:
                for row in csv.DictReader(f):
                    return row
        except Exception as e:
            logging.warning(f"读取 {path} 失败: {e}")
        return None

    @staticmethod
    def _to_float(value, default=0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_float_optional(value):
        if value is None or str(value).strip() == "":
            return None
        try:
            number = float(value)
            return number if pd.notna(number) else None
        except (TypeError, ValueError):
            return None

    def parse_result_directory(self, result_path: str, dir_name: str) -> Optional[Dict]:
        """解析回测结果目录"""
        try:
            config_file = os.path.join(result_path, "config.csv")
            daily_stats_file = os.path.join(result_path, "daily_stats.csv")
            trades_file = os.path.join(result_path, "trades.csv")

            # 从文件夹名中提取回测时间戳
            backtest_time = self.extract_backtest_time(dir_name)

            result_info = {
                'dir_name': dir_name,
                'path': result_path,
                'create_time': backtest_time if backtest_time else datetime.fromtimestamp(os.path.getctime(result_path)).strftime('%Y-%m-%d %H:%M:%S')
            }
            invalid_reasons = []

            # 解析配置信息
            config = None
            if os.path.exists(config_file):
                config = self._read_first_csv_row(config_file)
                if config:
                    result_info.update({
                        'strategy_name': self.extract_strategy_name(dir_name),
                        'start_date': config.get('start_time') or '',
                        'end_date': config.get('end_time') or '',
                        'init_capital': self._to_float(config.get('init_capital')),
                        'benchmark': config.get('benchmark') or '',
                        'runtime': config.get('total_runtime_formatted') or ''
                    })
            if not config:
                invalid_reasons.append("缺少有效 config.csv")
            elif not result_info.get('start_date') or not result_info.get('end_date'):
                invalid_reasons.append("缺少回测起止日期")

            # 尝试读取汇总数据
            summary_file = os.path.join(result_path, "summary.csv")
            summary_valid = False
            if os.path.exists(summary_file):
                summary = self._read_first_csv_row(summary_file)
                if summary:
                    final_asset = self._to_float_optional(summary.get('final_capital'))
                    summary_valid = final_asset is not None
                    if summary_valid:
                        result_info.update({
                        'final_asset': final_asset,
                        'total_return': self._to_float(summary.get('total_return')),
                        'annual_return': self._to_float(summary.get('annual_return')),
                        'max_drawdown': self._to_float(summary.get('max_drawdown'))
                        })
            
            # 如果没有从 summary.csv 中获取到数据，再尝试从 daily_stats.csv 中计算（兼容旧版本）
            daily_stats_valid = False
            if not summary_valid and os.path.exists(daily_stats_file):
                try:
                    daily_stats_df = pd.read_csv(
                        daily_stats_file,
                        encoding='utf-8-sig',
                        usecols=lambda name: name in {'date', 'total_asset'},
                    )
                    if len(daily_stats_df) > 0 and {'date', 'total_asset'}.issubset(daily_stats_df.columns):
                        daily_stats_valid = True
                        final_asset = daily_stats_df['total_asset'].iloc[-1]
                        init_capital = result_info.get('init_capital', 1000000)
                        
                        # 修正总收益率计算
                        total_return = (final_asset - init_capital) / init_capital * 100
                        
                        # 每日统计本身就是实际回测交易日序列，直接数唯一日期即可。
                        # 这里不能再逐结果调用交易日历工具，否则加载数千条历史时
                        # 会产生海量INFO日志并重复解析日历。
                        trading_days = int(daily_stats_df['date'].dropna().astype(str).nunique())
                        if trading_days > 0:
                            annual_return = ((final_asset / init_capital) ** (250 / trading_days) - 1) * 100
                        else:
                            annual_return = 0
                        
                        # 修正最大回撤计算
                        max_drawdown = self.calculate_max_drawdown(daily_stats_df['total_asset'])
                        
                        result_info.update({
                            'final_asset': final_asset,
                            'total_return': total_return,
                            'annual_return': annual_return,
                            'max_drawdown': max_drawdown
                        })
                except Exception as e:
                    logging.warning(f"从 {daily_stats_file} 计算指标失败: {str(e)}")

            if not summary_valid and not daily_stats_valid:
                invalid_reasons.append("缺少有效 summary.csv 或 daily_stats.csv")

            result_info['is_valid'] = not invalid_reasons
            result_info['invalid_reason'] = "；".join(invalid_reasons)
            
            return result_info
            
        except Exception as e:
            logging.error(f"解析回测结果目录 {result_path} 时出错: {str(e)}")
            return None
            
    def extract_strategy_name(self, dir_name: str) -> str:
        """从目录名提取策略名称"""
        # 目录名格式: 策略名_开始日期_结束日期_时间戳
        parts = dir_name.split('_')
        if len(parts) >= 5:  # 新格式: 策略名_开始日期_结束日期_日期_时间
            return '_'.join(parts[:-4])  # 去掉最后四个部分（日期和时间戳）
        elif len(parts) >= 3:  # 旧格式: 策略名_开始日期_结束日期
            return '_'.join(parts[:-2])  # 去掉最后两个部分（日期）
        return dir_name
        
    def extract_backtest_time(self, dir_name: str) -> str:
        """从目录名中提取回测时间戳"""
        # 目录名格式: 策略名_开始日期_结束日期_时间戳(YYYYMMDD_HHMMSS)
        parts = dir_name.split('_')
        if len(parts) >= 5:  # 策略名_开始日期_结束日期_日期_时间
            date_part = parts[-2]  # YYYYMMDD
            time_part = parts[-1]  # HHMMSS
            try:
                # 将时间戳转换为可读格式
                timestamp_str = f"{date_part}_{time_part}"
                timestamp = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
                return timestamp.strftime('%Y-%m-%d %H:%M:%S')
            except ValueError:
                pass
        return None
        
    def calculate_max_drawdown(self, asset_series) -> float:
        """计算最大回撤"""
        try:
            peak = asset_series.expanding().max()
            drawdown = (asset_series - peak) / peak * 100
            return abs(drawdown.min())
        except:
            return 0.0
            
    def calculate_sharpe_ratio(self, daily_stats_df) -> float:
        """计算夏普比率"""
        try:
            if len(daily_stats_df) < 2:
                return 0.0
                
            # 计算每日收益率
            total_assets = daily_stats_df['total_asset'].values
            daily_returns = []
            
            for i in range(1, len(total_assets)):
                if total_assets[i-1] != 0:
                    daily_return = (total_assets[i] - total_assets[i-1]) / total_assets[i-1]
                    daily_returns.append(daily_return)
            
            if len(daily_returns) < 2:
                return 0.0
                
            daily_returns = pd.Series(daily_returns)
            
            # 计算年化夏普比率
            # 假设无风险利率为3%年化，转换为日收益率
            risk_free_rate_daily = 0.03 / 252
            
            excess_returns = daily_returns - risk_free_rate_daily
            
            if daily_returns.std() == 0:
                return 0.0
                
            sharpe_ratio = (excess_returns.mean() / daily_returns.std()) * (252 ** 0.5)
            return sharpe_ratio
            
        except Exception as e:
            return 0.0
            
    # 每批填充的行数：分批并在批间让出事件循环，保持界面响应
    _TABLE_FILL_BATCH = 500

    def update_results_table(self):
        """更新结果表格（分批填充；全部使用item而非单元格控件，
        setCellWidget在数千行时会让表格的每次几何更新都退化为O(总行数)）"""
        table = self.results_table
        # 代数标记：新一轮填充开始后，旧一轮未完成的批次直接作废
        self._fill_generation = getattr(self, '_fill_generation', 0) + 1

        # 填充期间禁用排序：排序开启时每次setItem都会触发重排
        self._sorting_was_enabled = table.isSortingEnabled()
        table.setSortingEnabled(False)
        table.setRowCount(len(self.results_data))

        self._fill_table_batch(0, self._fill_generation)

    def _fill_table_batch(self, start_row, generation):
        """填充[start_row, start_row+批大小)的行，剩余部分排队到下一个事件循环"""
        if generation != getattr(self, '_fill_generation', 0):
            return  # 已有新一轮填充，本轮作废

        table = self.results_table
        end_row = min(start_row + self._TABLE_FILL_BATCH, len(self.results_data))

        table.setUpdatesEnabled(False)
        try:
            for row in range(start_row, end_row):
                result = self.results_data[row]
                # 选择框：用可勾选item代替QCheckBox控件
                check_item = QTableWidgetItem()
                check_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                check_item.setCheckState(Qt.Unchecked)
                table.setItem(row, 0, check_item)

                # 填充数据
                is_valid = result.get('is_valid', True)
                strategy_name = result.get('strategy_name', '')
                if not is_valid:
                    strategy_name = f"⚠ 不完整 · {strategy_name or result.get('dir_name', '')}"
                items = [
                    strategy_name,
                    result.get('start_date', ''),
                    result.get('end_date', ''),
                    f"{result.get('init_capital', 0):,.0f}",
                    f"{result.get('final_asset', 0):,.0f}" if is_valid else "--",
                    f"{result.get('total_return', 0):.2f}%" if is_valid else "--",
                    f"{result.get('annual_return', 0):.2f}%" if is_valid else "--",
                    f"{result.get('max_drawdown', 0):.2f}%" if is_valid else "--",
                    result.get('create_time', ''),
                ]

                for col, item in enumerate(items, 1):
                    cell = QTableWidgetItem(str(item))
                    if col == 1:
                        # 策略名称item携带results_data索引：排序后行号会变，
                        # 操作按钮/批量删除必须通过该索引找到正确的数据
                        cell.setData(Qt.UserRole, row)
                    if not is_valid:
                        cell.setToolTip(result.get('invalid_reason', '结果文件不完整'))
                        cell.setForeground(QColor('#ffb74d'))
                    table.setItem(row, col, cell)
        finally:
            table.setUpdatesEnabled(True)

        if end_row < len(self.results_data):
            QTimer.singleShot(0, lambda: self._fill_table_batch(end_row, generation))
        else:
            table.setSortingEnabled(self._sorting_was_enabled)
            # 全部填充完后一次性按内容调整创建时间列宽（单次O(N)扫描）
            table.resizeColumnToContents(9)

    def _data_index_for_row(self, row) -> int:
        """表格行号 -> results_data索引（排序后行号与数据顺序不再一致）"""
        item = self.results_table.item(row, 1)
        if item is not None:
            idx = item.data(Qt.UserRole)
            if idx is not None:
                return int(idx)
        return row

    def _on_action_clicked(self, row, action):
        """操作列按钮点击分发"""
        data_index = self._data_index_for_row(row)
        if action == "report":
            self.show_backtest_report(data_index)
        elif action == "restore":
            self.restore_to_main(data_index)
        elif action == "delete":
            self.delete_result(data_index)
            
    def filter_results(self):
        """根据搜索条件筛选结果"""
        search_text = self.search_edit.text().lower()
        
        for row in range(self.results_table.rowCount()):
            show_row = True
            
            if search_text:
                # 检查策略名称和日期
                strategy_name = self.results_table.item(row, 1)
                start_date = self.results_table.item(row, 2)
                end_date = self.results_table.item(row, 3)
                
                if not any(search_text in (item.text().lower() if item else '') 
                          for item in [strategy_name, start_date, end_date]):
                    show_row = False
                    
            self.results_table.setRowHidden(row, not show_row)
            
    def filter_by_date(self):
        """根据日期筛选结果"""
        filter_type = self.filter_combo.currentText()
        
        if filter_type == "全部":
            for row in range(self.results_table.rowCount()):
                self.results_table.setRowHidden(row, False)
            return
            
        # 计算日期范围
        now = datetime.now()
        if filter_type == "最近7天":
            cutoff_date = now - pd.Timedelta(days=7)
        elif filter_type == "最近30天":
            cutoff_date = now - pd.Timedelta(days=30)
        elif filter_type == "最近90天":
            cutoff_date = now - pd.Timedelta(days=90)
        else:
            return  # 自定义日期暂不实现
            
        for row in range(self.results_table.rowCount()):
            data_index = self._data_index_for_row(row)
            if data_index < len(self.results_data):
                result = self.results_data[data_index]
                try:
                    create_time = datetime.strptime(result.get('create_time', ''), '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    continue
                show_row = create_time >= cutoff_date
                self.results_table.setRowHidden(row, not show_row)
                

            
    def delete_result(self, row):
        """删除单个回测结果"""
        if row < len(self.results_data):
            result = self.results_data[row]
            
            reply = QMessageBox.question(
                self, "确认删除", 
                f"确定要删除回测结果 '{result['strategy_name']}' 吗？\n\n"
                f"回测期间: {result.get('start_date', 'N/A')} 至 {result.get('end_date', 'N/A')}\n"
                f"此操作不可撤销！",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                try:
                    shutil.rmtree(result['path'])
                    self.load_results()  # 重新加载列表
                    QMessageBox.information(self, "成功", "回测结果已删除")
                except Exception as e:
                    QMessageBox.critical(self, "错误", f"删除失败:\n{str(e)}")
                    
    def _checked_data_indices(self) -> List[int]:
        """返回所有勾选行对应的results_data索引（已处理排序后的行号错位）"""
        indices = []
        for row in range(self.results_table.rowCount()):
            item = self.results_table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                indices.append(self._data_index_for_row(row))
        return indices

    def delete_selected_results(self):
        """删除选中的回测结果"""
        selected_indices = self._checked_data_indices()

        if not selected_indices:
            QMessageBox.information(self, "提示", "请先选择要删除的回测结果")
            return

        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除选中的 {len(selected_indices)} 个回测结果吗？\n\n"
            f"此操作不可撤销！",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            try:
                deleted_count = 0
                for idx in selected_indices:
                    if idx < len(self.results_data):
                        result = self.results_data[idx]
                        shutil.rmtree(result['path'])
                        deleted_count += 1

                self.load_results()  # 重新加载列表
                QMessageBox.information(self, "成功", f"已删除 {deleted_count} 个回测结果")
            except Exception as e:
                QMessageBox.critical(self, "错误", f"删除失败:\n{str(e)}")
                

    def export_results(self):
        """导出回测结果"""
        if not self.results_data:
            QMessageBox.information(self, "提示", "没有可导出的回测结果")
            return
            
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出回测结果", "backtest_results.csv", "CSV Files (*.csv)"
        )
        
        if file_path:
            try:
                # 准备导出数据
                export_data = []
                for result in self.results_data:
                    export_data.append({
                        '策略名称': result.get('strategy_name', ''),
                        '开始日期': result.get('start_date', ''),
                        '结束日期': result.get('end_date', ''),
                        '初始资金': result.get('init_capital', 0),
                        '最终资产': result.get('final_asset', 0),
                        '总收益率(%)': result.get('total_return', 0),
                        '年化收益率(%)': result.get('annual_return', 0),
                        '最大回撤(%)': result.get('max_drawdown', 0),
                        '夏普比率': result.get('sharpe_ratio', 0),
                        '基准指数': result.get('benchmark', ''),
                        '创建时间': result.get('create_time', ''),
                        '运行时长': result.get('runtime', ''),
                        '结果路径': result.get('path', '')
                    })
                    
                df = pd.DataFrame(export_data)
                df.to_csv(file_path, index=False, encoding='utf-8-sig')
                
                QMessageBox.information(self, "成功", f"回测结果已导出到:\n{file_path}")
                
            except Exception as e:
                QMessageBox.critical(self, "错误", f"导出失败:\n{str(e)}")
                
    def compare_results(self):
        """对比分析回测结果"""
        selected_rows = self._checked_data_indices()

        if len(selected_rows) < 2:
            QMessageBox.information(self, "提示", "请至少选择2个回测结果进行对比")
            return
            
        # 这里可以实现回测结果对比功能
        QMessageBox.information(self, "提示", "回测结果对比功能开发中...")
        
    def restore_to_main(self, row):
        """还原策略文件和配置到主界面"""
        if row < len(self.results_data):
            result = self.results_data[row]
            
            reply = QMessageBox.question(
                self, "确认还原", 
                f"确定要还原回测结果 '{result['strategy_name']}' 到主界面吗？\n\n"
                f"回测期间: {result.get('start_date', 'N/A')} 至 {result.get('end_date', 'N/A')}\n\n"
                f"⚠️ 重要提醒：\n"
                f"• 这将覆盖当前主界面的策略配置\n"
                f"• 如果目标路径存在同名策略文件，将被覆盖\n"
                f"• 建议在还原前备份当前的策略文件\n\n"
                f"是否继续还原？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                try:
                    success_msg = self.restore_strategy_config(result)
                    QMessageBox.information(self, "成功", success_msg)
                except Exception as e:
                    QMessageBox.critical(self, "错误", f"还原失败:\n{str(e)}")
                    logging.error(f"还原失败: {str(e)}")
                    
    def restore_strategy_config(self, result):
        """还原策略配置"""
        result_path = result['path']
        config_file = os.path.join(result_path, "config.csv")
        
        if not os.path.exists(config_file):
            raise Exception(f"配置文件不存在: {config_file}")
            
        # 读取配置文件
        try:
            config_df = pd.read_csv(config_file, encoding='utf-8-sig')
        except Exception as e:
            raise Exception(f"读取配置文件失败: {str(e)}")
            
        if len(config_df) == 0:
            raise Exception("配置文件为空")
            
        config = config_df.iloc[0]
        config_dict = config.to_dict()
        
        restored_files = []
        warnings = []
        
        # 还原策略文件
        original_strategy_file = config_dict.get('strategy_file', '')
        if original_strategy_file:
            # 查找备份的策略文件
            strategy_filename = os.path.basename(original_strategy_file)
            backup_strategy_path = os.path.join(result_path, strategy_filename)
            
            if os.path.exists(backup_strategy_path):
                try:
                    # 确保目标目录存在
                    os.makedirs(os.path.dirname(original_strategy_file), exist_ok=True)
                    
                    # 还原.py文件
                    shutil.copy2(backup_strategy_path, original_strategy_file)
                    restored_files.append(f"策略文件: {original_strategy_file}")
                    logging.info(f"策略文件已还原: {original_strategy_file}")
                    
                    # 还原对应的.kh文件
                    kh_filename = os.path.splitext(strategy_filename)[0] + ".kh"
                    backup_kh_path = os.path.join(result_path, kh_filename)
                    original_kh_path = os.path.splitext(original_strategy_file)[0] + ".kh"
                    
                    if os.path.exists(backup_kh_path):
                        shutil.copy2(backup_kh_path, original_kh_path)
                        restored_files.append(f"配置文件: {original_kh_path}")
                        logging.info(f"策略配置文件已还原: {original_kh_path}")
                    else:
                        warnings.append(f"未找到备份的.kh文件: {backup_kh_path}")
                        
                except Exception as e:
                    warnings.append(f"还原策略文件时出错: {str(e)}")
                    logging.warning(f"还原策略文件时出错: {str(e)}")
            else:
                warnings.append(f"未找到备份的策略文件: {backup_strategy_path}")
        
        # 尝试读取完整的配置文件（.kh文件）以获取触发器等完整信息
        full_config_dict = config_dict.copy()  # 先使用CSV中的基本配置
        
        # 查找并读取.kh配置文件
        kh_files = ["full_temp_running_config.kh", f"{os.path.splitext(os.path.basename(original_strategy_file))[0]}.kh"]
        for kh_filename in kh_files:
            kh_file_path = os.path.join(result_path, kh_filename)
            if os.path.exists(kh_file_path):
                try:
                    with open(kh_file_path, 'r', encoding='utf-8') as f:
                        kh_config = json.load(f)
                    
                    # 合并配置，优先使用.kh文件中的完整配置
                    full_config_dict.update(kh_config)
                    
                    # 特别处理嵌套的配置结构
                    if "data" in kh_config:
                        full_config_dict.update({
                            "kline_period": kh_config["data"].get("kline_period", full_config_dict.get("kline_period")),
                            "dividend_type": kh_config["data"].get("dividend_type", full_config_dict.get("dividend_type"))
                        })
                    
                    logging.info(f"已读取完整配置文件: {kh_file_path}")
                    break
                except Exception as e:
                    logging.warning(f"读取.kh配置文件失败 {kh_file_path}: {str(e)}")
                    continue
        
        # 尝试还原配置到界面
        config_restored = False
        if self.parent and hasattr(self.parent, 'restore_config_from_history'):
            try:
                self.parent.restore_config_from_history(full_config_dict)
                config_restored = True
                restored_files.append("主界面配置已更新")
            except Exception as e:
                warnings.append(f"还原到主界面失败: {str(e)}")
                logging.warning(f"还原到主界面失败: {str(e)}")
        
        if not config_restored:
            # 如果没有父窗口或还原失败，保存配置到默认位置
            try:
                default_config_path = os.path.join(os.getcwd(), "restored_config.json")
                with open(default_config_path, 'w', encoding='utf-8') as f:
                    json.dump(config_dict, f, ensure_ascii=False, indent=2)
                restored_files.append(f"配置已保存到: {default_config_path}")
                logging.info(f"配置已保存到 {default_config_path}")
            except Exception as e:
                warnings.append(f"保存配置文件失败: {str(e)}")
        
        # 构建返回消息
        success_msg = "还原完成！\n\n"
        if restored_files:
            success_msg += "已还原:\n" + "\n".join([f"• {item}" for item in restored_files])
        
        if warnings:
            success_msg += "\n\n警告:\n" + "\n".join([f"• {warning}" for warning in warnings])
            
        return success_msg

    def show_backtest_report(self, row):
        """显示回测报告"""
        try:
            if row >= len(self.results_data):
                QMessageBox.warning(self, "错误", "无效的行索引")
                return
                
            result = self.results_data[row]
            result_path = result['path']
            
            # 检查回测结果文件是否存在
            daily_stats_file = os.path.join(result_path, "daily_stats.csv")
            trades_file = os.path.join(result_path, "trades.csv")
            config_file = os.path.join(result_path, "config.csv")
            
            if not os.path.exists(daily_stats_file) and not os.path.exists(trades_file):
                QMessageBox.warning(self, "错误", "未找到回测结果文件")
                return
            
            # 调用BacktestResultWindow显示回测报告
            from backtest_result_window import BacktestResultWindow
            self.result_window = BacktestResultWindow(result_path)
            
            # 将窗口居中显示在主显示器
            from PyQt5.QtWidgets import QDesktopWidget
            desktop = QDesktopWidget()
            screen_geometry = desktop.screenGeometry(desktop.primaryScreen())
            window_geometry = self.result_window.frameGeometry()
            center_point = screen_geometry.center()
            window_geometry.moveCenter(center_point)
            self.result_window.move(window_geometry.topLeft())
            
            self.result_window.show()
            
        except Exception as e:
            QMessageBox.critical(self, "错误", f"显示回测报告失败:\n{str(e)}")
            logging.error(f"显示回测报告失败: {str(e)}")


    def apply_dark_theme(self):
        """应用深色主题"""
        dark_style = """
        QMainWindow {
            background-color: #2b2b2b;
            color: #ffffff;
        }
        QWidget {
            background-color: #2b2b2b;
            color: #ffffff;
        }
        QTableWidget {
            background-color: #3c3c3c;
            alternate-background-color: #404040;
            gridline-color: #555555;
            selection-background-color: #0078d4;
        }
        QHeaderView::section {
            background-color: #404040;
            color: #ffffff;
            border: 1px solid #555555;
            padding: 4px;
        }
        QPushButton {
            background-color: #0078d4;
            color: #ffffff;
            border: none;
            padding: 6px 12px;
            border-radius: 3px;
        }
        QPushButton:hover {
            background-color: #106ebe;
        }
        QPushButton:pressed {
            background-color: #005a9e;
        }
        QLineEdit, QComboBox {
            background-color: #404040;
            color: #ffffff;
            border: 1px solid #555555;
            padding: 4px;
            border-radius: 3px;
        }
        QTextEdit {
            background-color: #2b2b2b;
            color: #ffffff;
            border: 1px solid #555555;
        }
        QTabWidget::pane {
            border: 1px solid #555555;
            background-color: #3c3c3c;
        }
        QTabBar::tab {
            background-color: #404040;
            color: #ffffff;
            padding: 8px 16px;
            margin-right: 2px;
        }
        QTabBar::tab:selected {
            background-color: #0078d4;
        }
        QLabel {
            color: #ffffff;
        }
        QStatusBar {
            background-color: #404040;
            color: #ffffff;
        }
        QProgressBar {
            border: 1px solid #555555;
            border-radius: 3px;
            text-align: center;
        }
        QProgressBar::chunk {
            background-color: #0078d4;
            border-radius: 2px;
        }
        QCheckBox {
            color: #ffffff;
            background-color: transparent;
        }
        QCheckBox::indicator {
            width: 32px;
            height: 32px;
            background-color: #404040;
            border: 1px solid #666666;
            border-radius: 2px;
        }
        QCheckBox::indicator:unchecked {
            background-color: #404040;
            border: 1px solid #666666;
        }
        QCheckBox::indicator:checked {
            background-color: #0078d4;
            border: 1px solid #0078d4;
            image: url(data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMTIiIGhlaWdodD0iMTIiIHZpZXdCb3g9IjAgMCAxMiAxMiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTEwIDNMNC41IDguNUwyIDYiIHN0cm9rZT0id2hpdGUiIHN0cm9rZS13aWR0aD0iMiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+Cjwvc3ZnPgo=);
        }
        QCheckBox::indicator:hover {
            border: 1px solid #0078d4;
        }
        """
        
        self.setStyleSheet(dark_style)


if __name__ == "__main__":
    import sys
    from PyQt5.QtWidgets import QApplication
    
    app = QApplication(sys.argv)
    
    # 创建并显示回测历史管理器
    manager = BacktestHistoryManager()
    manager.show()
    
    sys.exit(app.exec_())
