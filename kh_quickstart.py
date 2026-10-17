# coding: utf-8
"""「新手上手」窗口：第一次用时告诉用户从哪下数据、示例要先填股票池，并引导去看视频教程。

首次启动、数据目录里还没有股票行情时自动弹出；之后随时可从工具栏「?」打开。
视频链接指向官网「快速上手」页面（不直接指向视频文件），重录视频只更新网页，软件不用重新发版。
"""
import html

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

QUICKSTART_VIDEO_URL = "https://khsci.com/khQuant/quickstart/"   # 教程 0 快速上手（含视频）
FULL_TUTORIAL_URL = "https://khsci.com/khQuant/tutorial/"

QUICKSTART_TITLE = "新手上手"
QUICKSTART_SUBTITLE = "第一次用，按下面四步就能跑通第一个回测"
QUICKSTART_HINT = "不清楚的地方，点「观看视频教程」，跟着视频做一遍。"

QUICKSTART_STEPS = (
    ("下载数据", "点「打开数据管理」→「BaoStock导入」，填要研究的股票代码，下载日线（免费，不用注册）。"
                 "开始日期要早于回测开始日期，给均线等指标留出预热期；1 分钟线用「Tushare导入」，需要 Token 和 stk_mins 权限。"),
    ("加载示例", "工具栏「加载配置」，选一个示例策略（在 文档\\KhQuant_OS\\strategies）。"),
    ("添加股票", "示例策略不带股票：在左侧「股票池设置」点「添加股票」，填刚才下载的股票，再「保存配置」。"),
    ("开始运行", "回测区间要在已下载的数据范围内，点「开始运行」，看回测报告。"),
)

# exec_() 的返回值：用户选了哪个按钮
RESULT_LATER = 0
RESULT_OPEN_DATA = 1
RESULT_WATCH_VIDEO = 2

ACCENT = "#F28C28"
ACCENT_HOVER = "#F5A04C"
ACCENT_PRESSED = "#D9771A"


def open_quickstart_video() -> bool:
    return QDesktopServices.openUrl(QUrl(QUICKSTART_VIDEO_URL))


def _stylesheet(scale: float) -> str:
    def px(value):
        return max(1, round(value * scale))

    return f"""
        QDialog#khQuickStart {{ background-color: #262626; }}
        QDialog#khQuickStart QLabel, QDialog#khQuickStart QPushButton {{
            font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
        }}
        QLabel {{ background: transparent; color: #e8e8e8; }}
        QLabel#qsTitle {{ font-size: {px(24)}px; font-weight: bold; color: #ffffff; }}
        QLabel#qsSubtitle {{ font-size: {px(14)}px; color: #b8b8b8; }}
        QFrame#qsStep {{
            background-color: #303030; border: 1px solid #3d3d3d; border-radius: {px(10)}px;
        }}
        QLabel#qsBadge {{
            background-color: {ACCENT}; color: #ffffff; font-size: {px(16)}px; font-weight: bold;
            border-radius: {px(16)}px;
        }}
        QLabel#qsStepTitle {{ font-size: {px(15)}px; font-weight: bold; color: #ffffff; }}
        QLabel#qsStepText {{ font-size: {px(13)}px; color: #cfcfcf; }}
        QLabel#qsHint {{ font-size: {px(13)}px; color: {ACCENT}; }}
        QPushButton {{
            font-size: {px(14)}px; min-height: {px(34)}px; padding: 0 {px(18)}px;
            border-radius: {px(6)}px; color: #e8e8e8; background-color: #3a3a3a; border: 1px solid #4a4a4a;
        }}
        QPushButton:hover {{ background-color: #444444; }}
        QPushButton:pressed {{ background-color: #333333; }}
        QPushButton#qsPrimary {{
            color: #ffffff; background-color: {ACCENT}; border: 1px solid {ACCENT}; font-weight: bold;
        }}
        QPushButton#qsPrimary:hover {{ background-color: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
        QPushButton#qsPrimary:pressed {{ background-color: {ACCENT_PRESSED}; border-color: {ACCENT_PRESSED}; }}
        QPushButton#qsLater {{ background-color: transparent; border: 1px solid transparent; color: #a8a8a8; }}
        QPushButton#qsLater:hover {{ color: #e8e8e8; background-color: #333333; }}
    """


class QuickStartDialog(QDialog):
    """返回 RESULT_LATER / RESULT_OPEN_DATA / RESULT_WATCH_VIDEO。"""

    def __init__(self, parent=None, show_data_button=True):
        super().__init__(parent)
        scale = float(getattr(parent, "font_scale", 1.0) or 1.0)
        self.setObjectName("khQuickStart")
        self.setWindowTitle(QUICKSTART_TITLE)
        self.setMinimumWidth(round(700 * scale))
        self.setStyleSheet(_stylesheet(scale))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(round(28 * scale), round(24 * scale), round(28 * scale), round(20 * scale))
        layout.setSpacing(round(10 * scale))

        title = QLabel(QUICKSTART_TITLE)
        title.setObjectName("qsTitle")
        layout.addWidget(title)
        subtitle = QLabel(QUICKSTART_SUBTITLE)
        subtitle.setObjectName("qsSubtitle")
        layout.addWidget(subtitle)
        layout.addSpacing(round(6 * scale))

        self.step_frames = []
        for index, (step_title, step_text) in enumerate(QUICKSTART_STEPS, 1):
            frame = self._step_card(index, step_title, step_text, scale)
            self.step_frames.append(frame)
            layout.addWidget(frame)

        layout.addSpacing(round(8 * scale))
        self.hint = QLabel(QUICKSTART_HINT)
        self.hint.setObjectName("qsHint")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        layout.addSpacing(round(8 * scale))

        buttons = QHBoxLayout()
        buttons.setSpacing(round(10 * scale))
        later_btn = QPushButton("稍后" if show_data_button else "关闭")
        later_btn.setObjectName("qsLater")
        later_btn.clicked.connect(lambda: self.done(RESULT_LATER))
        buttons.addWidget(later_btn)
        buttons.addStretch(1)
        self.data_btn = None
        if show_data_button:
            self.data_btn = QPushButton("打开数据管理")
            self.data_btn.clicked.connect(lambda: self.done(RESULT_OPEN_DATA))
            buttons.addWidget(self.data_btn)
        self.video_btn = QPushButton("▶  观看视频教程")
        self.video_btn.setObjectName("qsPrimary")
        self.video_btn.setDefault(True)
        self.video_btn.clicked.connect(lambda: self.done(RESULT_WATCH_VIDEO))
        buttons.addWidget(self.video_btn)
        layout.addLayout(buttons)

        width = self.minimumWidth()
        self.resize(width, layout.heightForWidth(width))

    @staticmethod
    def _step_card(index, step_title, step_text, scale):
        frame = QFrame()
        frame.setObjectName("qsStep")
        row = QHBoxLayout(frame)
        row.setContentsMargins(round(14 * scale), round(12 * scale), round(16 * scale), round(12 * scale))
        row.setSpacing(round(14 * scale))

        badge = QLabel(str(index))
        badge.setObjectName("qsBadge")
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedSize(round(32 * scale), round(32 * scale))
        row.addWidget(badge, 0, Qt.AlignTop)

        texts = QVBoxLayout()
        texts.setSpacing(round(4 * scale))
        head = QLabel(step_title)
        head.setObjectName("qsStepTitle")
        texts.addWidget(head)
        body = QLabel(f"<div style='line-height:140%;'>{html.escape(step_text)}</div>")
        body.setObjectName("qsStepText")
        body.setTextFormat(Qt.RichText)
        body.setWordWrap(True)
        texts.addWidget(body)
        row.addLayout(texts, 1)
        return frame
