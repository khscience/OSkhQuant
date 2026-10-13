# coding: utf-8
"""「了解 V3」介绍窗口：先用两块卡片突出 V3 最重要的两项（三端通用、CLI 命令行），
再用对比表列出其他区别。

卡片和对比表里的每一项都要和 V3 实际提供的功能一致；链接取自官网首页「版本对比」。
数据来源一行写同花顺和 miniQMT，不写 HTTP 桥接、tx；策略案例下方必须附免责说明（不指定标的、含随机数、
仅作算法演示）。
"""
import html

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout,
)

from kh_app_identity import SHORT_VERSION

V3_TITLE = "看海量化回测平台 V3"
# 官网首页的「版本对比」区块
V3_PAGE_URL = "https://khsci.com/khQuant/#version-comparison"
LINUX_URL = "https://mp.weixin.qq.com/s/KfV0-hjfBs3swoMvm7N7bg"
MACOS_URL = "https://mp.weixin.qq.com/s/oDq0vMCIRuA59-9SPavzGQ"
CLI_URL = "https://mp.weixin.qq.com/s/aXfT2TwlnpwvQxlFv4UOZw"

# 重点卡片：（图标，标题，说明，本软件的情况，[(链接文字, 地址)]）
HIGHLIGHTS = (
    ("🖥", "三端通用：Windows · macOS · Linux",
     "Windows 与 Apple Silicon macOS 都有桌面版，Linux 提供命令行和完整的 Web 回测工作台；"
     "同一套策略代码和 DuckDB 数据在三个系统上通用。",
     "只支持 Windows",
     (("Linux 详情", LINUX_URL), ("macOS 详情", MACOS_URL))),
    ("⌨", "CLI 命令行模式",
     "不打开界面也能完成数据下载、回测执行、结果查看等全流程，适合写脚本批量跑、"
     "部署到远程服务器，也方便交给 AI 编码助手自动操作。",
     "只有图形界面",
     (("查看详情", CLI_URL),)),
)

# 其他区别：（项目，本软件，V3），V3 更有吸引力的放前面
STRATEGY_CASES_ITEM = "策略案例"
COMPARISON_ROWS = (
    ("数据来源", "BaoStock（免费）、Tushare",
     "另加同花顺（扶摇开放平台）、miniQMT，并会持续集成更多数据源"),
    ("Web 回测", "—", "Web 回测工作台：在浏览器里跑回测、看结果，手机也能远程访问"),
    ("策略编写", "用自己的编辑器写 .py 文件", "Windows 集成 VS Code 启动与断点调试，需自行安装"),
    ("数据维护", "手动下载、增量补充", "另有定时自动补充数据"),
    ("包管理", "—", "Python 包管理页：在软件里安装策略要用的第三方库"),
    (STRATEGY_CASES_ITEM, "4 个基础案例：双均线 3 个、RSI 1 个",
     "共 13 个：另有 T+0 ETF 网格 4 个、数据库指标读写 4 个、日内多次触发 1 个；另有持续更新的专栏策略案例"),
    ("回测内核", "成交、费用、滑点、T+0 / T+1、复权", "本版对齐 V3.4.1.10；相同条件下的六组冻结案例已逐笔核对"),
)
# 策略案例的免责说明，显示在对比表下方
STRATEGY_CASES_NOTE = (
    "※ 策略案例说明：案例不指定交易标的，参数中加入了随机数，仅作为算法写法演示，"
    "不构成任何策略或投资建议。"
)

# 与主窗口「了解 V3」按钮同一套配色
V3_BUTTON_STYLE = """
    QPushButton, QToolButton {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ff8a00, stop:1 #e52e71);
        color: #ffffff;
        border: none;
        border-radius: {radius}px;
        padding: {pad_y}px {pad_x}px;
        font-family: "{font}";
        font-weight: bold;
        font-size: {size}px;{height_rule}
    }}
    QPushButton:hover, QToolButton:hover {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ffa033, stop:1 #f0508a);
    }}
    QPushButton:pressed, QToolButton:pressed {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #d97400, stop:1 #c2255e);
    }}
"""


def v3_button_style(font="Microsoft YaHei UI", size=14, pad_y=4, pad_x=12, radius=6, height=None) -> str:
    # 高度写进样式表：主窗口会按界面缩放倍数统一放大样式表里的像素值，和「?」按钮一致
    height_rule = f" min-height: {height}px; max-height: {height}px;" if height else ""
    return V3_BUTTON_STYLE.format(font=font, size=size, pad_y=pad_y, pad_x=pad_x, radius=radius,
                                  height_rule=height_rule)


def intro_text() -> str:
    v = SHORT_VERSION
    return (
        f"V3 是看海量化的完整版本。V{v} 与 V3 使用同一套回测内核：本次对齐 V3.4.1.10，"
        "相同数据、固定策略参数和相同配置的六组冻结案例已逐笔核对。不同版本或配置需重新验证。V3 的主要扩展是下面两项："
    )


def comparison_html() -> str:
    head = (
        "<tr>"
        "<th align='left' bgcolor='#3a3a3a'>项目</th>"
        f"<th align='left' bgcolor='#3a3a3a'>V{SHORT_VERSION}（本软件）</th>"
        "<th align='left' bgcolor='#5a3320'><font color='#ffb36b'>V3</font></th>"
        "</tr>"
    )
    def label(item):
        text = html.escape(item)
        return f"{text} ※" if item == STRATEGY_CASES_ITEM else text

    rows = "".join(
        "<tr>"
        f"<td bgcolor='#2f2f2f'><b>{label(item)}</b></td>"
        f"<td>{html.escape(ours)}</td>"
        f"<td bgcolor='#3a2a22'>{html.escape(v3)}</td>"
        "</tr>"
        for item, ours, v3 in COMPARISON_ROWS
    )
    return (
        "<table width='100%' border='1' cellspacing='0' cellpadding='8' "
        "style='border-color:#555555; border-collapse:collapse;'>"
        f"{head}{rows}</table>"
        f"<p style='color:#d9b38c; margin-top:10px;'>{html.escape(STRATEGY_CASES_NOTE)}</p>"
    )


def dialog_font_px(font_scale: float = 1.0) -> int:
    """介绍窗口的正文字号：比主界面略大（基准 17px），跟主界面一起按屏幕缩放。"""
    return max(13, int(17 * (font_scale or 1.0)))


def _preferred_font_family() -> str:
    try:
        from khUiScale import get_preferred_ui_font_family

        return get_preferred_ui_font_family() or "Microsoft YaHei UI"
    except Exception:
        return "Microsoft YaHei UI"


def _open_url(url):
    QDesktopServices.openUrl(QUrl(url))


class V3IntroDialog(QDialog):
    """V3 介绍：两块重点卡片 + 其他区别对比表；「前往官网了解 V3」打开官网版本对比。

    字体和字号跟主窗口一致（主窗口按屏幕放大字号时这里一起放大），
    窗口高度按内容撑开，不用滚动就能看全。
    """

    def __init__(self, parent=None, font_family=None, font_px=None):
        super().__init__(parent)
        scale = getattr(parent, "font_scale", 1.0) or 1.0
        self.font_px = int(font_px or dialog_font_px(scale))
        self.font_family = font_family or _preferred_font_family()
        fp = self.font_px
        self.setWindowTitle(V3_TITLE)
        self.setStyleSheet(
            f"QDialog, QLabel, QPushButton, QTextBrowser {{ font-family: \"{self.font_family}\"; "
            f"font-size: {fp}px; }}"
            "QFrame#v3Card { border: 1px solid #b8662e; border-radius: 10px; "
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #4a2a1a, stop:1 #3a1f2c); }"
            "QFrame#v3Card QLabel { background: transparent; }"
        )
        # 默认开得大一些，字看着不费劲；不超过屏幕可用宽度的九成二
        width = max(980, fp * 64)
        try:
            width = min(width, int(self.screen().availableGeometry().width() * 0.92))
        except Exception:
            pass
        self.resize(width, max(600, fp * 42))

        layout = QVBoxLayout(self)
        margin = max(16, fp)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(max(10, fp * 3 // 4))

        title = QLabel(V3_TITLE)
        title.setStyleSheet(f"font-size: {int(fp * 1.5)}px; font-weight: bold; color: #ffb36b;")
        layout.addWidget(title)

        intro = QLabel(intro_text())
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #e0e0e0;")
        layout.addWidget(intro)

        self.link_labels = []
        cards = QHBoxLayout()
        cards.setSpacing(max(10, fp * 3 // 4))
        for icon, head, desc, ours, links in HIGHLIGHTS:
            cards.addWidget(self._card(icon, head, desc, ours, links), 1)
        layout.addLayout(cards)

        others = QLabel("其他区别")
        others.setStyleSheet(f"font-size: {int(fp * 1.1)}px; font-weight: bold; color: #e8e8e8;")
        layout.addWidget(others)

        self.table = QTextBrowser()
        self.table.setOpenExternalLinks(False)
        self.table.setStyleSheet("QTextBrowser { background: #262626; color: #e8e8e8; border: none; }")
        doc_font = self.table.font()
        doc_font.setFamily(self.font_family)
        doc_font.setPixelSize(fp)
        self.table.document().setDefaultFont(doc_font)
        self.table.setHtml(comparison_html())
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.visit_btn = QPushButton("前往官网了解 V3")
        pad_y = max(4, fp // 3)
        self.visit_btn.setStyleSheet(v3_button_style(font=self.font_family, size=fp, pad_y=pad_y, pad_x=fp))
        self.visit_btn.setCursor(Qt.PointingHandCursor)
        self.visit_btn.clicked.connect(self.open_v3_page)
        buttons.addWidget(self.visit_btn)
        close_btn = QPushButton("关闭")
        close_btn.setMinimumHeight(self.visit_btn.sizeHint().height())
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

    def _card(self, icon, head, desc, ours, links):
        fp = self.font_px
        card = QFrame()
        card.setObjectName("v3Card")
        box = QVBoxLayout(card)
        box.setContentsMargins(fp, fp * 3 // 4, fp, fp * 3 // 4)
        box.setSpacing(max(6, fp // 2))

        head_label = QLabel(f"{icon}  {html.escape(head)}")
        head_label.setWordWrap(True)
        head_label.setStyleSheet(f"font-size: {int(fp * 1.25)}px; font-weight: bold; color: #ffcf8a;")
        box.addWidget(head_label)

        desc_label = QLabel(desc)
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("color: #f0e6e0;")
        box.addWidget(desc_label)

        ours_label = QLabel(f"V{SHORT_VERSION}：{ours}")
        ours_label.setStyleSheet(f"color: #a8a8a8; font-size: {max(10, int(fp * 0.9))}px;")
        box.addWidget(ours_label)
        box.addStretch(1)

        link_row = QHBoxLayout()
        link_row.setSpacing(fp)
        for text, url in links:
            link = QLabel(f"<a href='{html.escape(url)}' style='color:#ffb36b; text-decoration:none;'>"
                          f"{html.escape(text)} →</a>")
            link.setTextFormat(Qt.RichText)
            link.setCursor(Qt.PointingHandCursor)
            link.linkActivated.connect(_open_url)
            link.setProperty("v3_url", url)
            link_row.addWidget(link)
            self.link_labels.append(link)
        link_row.addStretch(1)
        box.addLayout(link_row)
        return card

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_table_height()

    def _fit_table_height(self):
        """按当前宽度排版对比表，把窗口高度调到正好放下整张表（不超过屏幕可用高度的九成）。"""
        doc = self.table.document()
        doc.setTextWidth(self.table.viewport().width())
        extra = int(doc.size().height()) + 6 - self.table.viewport().height()
        if extra == 0:
            return
        limit = None
        try:
            limit = int(self.screen().availableGeometry().height() * 0.9)
        except Exception:
            pass
        height = max(420, self.height() + extra)
        self.resize(self.width(), min(height, limit) if limit else height)

    def open_v3_page(self):
        _open_url(V3_PAGE_URL)


__all__ = ["V3_TITLE", "V3_PAGE_URL", "LINUX_URL", "MACOS_URL", "CLI_URL", "HIGHLIGHTS",
           "COMPARISON_ROWS", "STRATEGY_CASES_ITEM", "STRATEGY_CASES_NOTE", "dialog_font_px",
           "v3_button_style", "intro_text", "comparison_html", "V3IntroDialog"]
