# coding: utf-8
"""「新手上手」窗口：第一次用时告诉用户从哪下数据、示例要先填股票池，并引导去看视频教程。

首次启动、数据目录里还没有股票行情时自动弹出；之后随时可从工具栏「?」打开。
视频链接指向官网教程 1.5 页面（不直接指向视频文件），重录视频只更新网页，软件不用重新发版。
"""
from PyQt5.QtCore import QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

QUICKSTART_VIDEO_URL = "https://khsci.com/khQuant/chapter4/"   # 教程 1.5 跑通第一个回测（含视频）
FULL_TUTORIAL_URL = "https://khsci.com/khQuant/tutorial/"

QUICKSTART_TITLE = "新手上手"

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


def quickstart_html() -> str:
    rows = "".join(
        f"<p style='margin:6px 0;'><b>{index}. {title}</b>：{text}</p>"
        for index, (title, text) in enumerate(QUICKSTART_STEPS, 1)
    )
    return (
        "<p>第一次用，按下面四步就能跑通第一个回测：</p>"
        f"{rows}"
        "<p style='margin-top:10px;'>不清楚的地方，看教程 1.5 的视频，跟着做一遍。</p>"
    )


def open_quickstart_video() -> bool:
    return QDesktopServices.openUrl(QUrl(QUICKSTART_VIDEO_URL))


class QuickStartDialog(QDialog):
    """返回 RESULT_LATER / RESULT_OPEN_DATA / RESULT_WATCH_VIDEO。"""

    def __init__(self, parent=None, show_data_button=True):
        super().__init__(parent)
        self.setWindowTitle(QUICKSTART_TITLE)
        self.setMinimumWidth(600)
        layout = QVBoxLayout(self)
        self.text = QLabel(quickstart_html())
        self.text.setWordWrap(True)
        layout.addWidget(self.text)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.video_btn = QPushButton("观看视频教程")
        self.video_btn.setDefault(True)
        self.video_btn.clicked.connect(lambda: self.done(RESULT_WATCH_VIDEO))
        buttons.addWidget(self.video_btn)
        self.data_btn = None
        if show_data_button:
            self.data_btn = QPushButton("打开数据管理")
            self.data_btn.clicked.connect(lambda: self.done(RESULT_OPEN_DATA))
            buttons.addWidget(self.data_btn)
        later_btn = QPushButton("稍后" if show_data_button else "关闭")
        later_btn.clicked.connect(lambda: self.done(RESULT_LATER))
        buttons.addWidget(later_btn)
        layout.addLayout(buttons)
