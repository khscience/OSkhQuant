# coding: utf-8
import os
from typing import Optional, List, Dict, Tuple

from PyQt5.QtCore import QEvent, QObject, QSettings
from PyQt5.QtWidgets import QAbstractScrollArea, QAbstractSpinBox, QApplication, QComboBox
from PyQt5.QtGui import QFont, QFontDatabase

DEFAULT_BASE_FONT_SIZE = 9

DEFAULT_MONO_FONT_FAMILIES: List[str] = [
    "Consolas",
    "Microsoft YaHei UI",
    "Courier New",
    "DejaVu Sans Mono",
]


def force_primary_screen_dpi() -> None:
    """多显示器环境下，强制整个进程以主屏幕的缩放比例渲染界面。

    解决副屏 DPI 缩放高于主屏时，从副屏启动界面/报告被异常放大或
    横跨两块屏幕的问题。必须在创建 QApplication 之前调用。
    """
    # 关闭 Qt 的逐屏自动缩放，避免界面采用副屏的缩放因子
    for _var in ("QT_ENABLE_HIGHDPI_SCALING", "QT_SCALE_FACTOR",
                 "QT_SCREEN_SCALE_FACTORS"):
        os.environ.pop(_var, None)
    os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "0"

    try:
        import ctypes
        # PROCESS_SYSTEM_DPI_AWARE = 1：整个进程统一使用主屏幕 DPI
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
DEFAULT_FONT_FAMILIES: List[str] = [
    "PingFang SC",
    "Helvetica Neue",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "Segoe UI",
    "SimHei",
    "SimSun",
    "Arial Unicode MS",
    "DejaVu Sans",
]


def get_preferred_font_families() -> List[str]:
    """返回推荐的 UI 字体候选列表。"""
    return DEFAULT_FONT_FAMILIES


def get_preferred_ui_font_family() -> str:
    """返回实际可用的首选字体名。"""
    if QApplication.instance() is None:
        return "Microsoft YaHei UI"
    db = QFontDatabase()
    try:
        available_families = set(db.families())
    except Exception:
        available_families = set()

    for family in get_preferred_font_families():
        if family in available_families:
            return family
    return ""


def get_preferred_mono_font_family() -> str:
    """返回实际可用的首选等宽字体名。"""
    if QApplication.instance() is None:
        return "Consolas"
    db = QFontDatabase()
    try:
        available_families = set(db.families())
    except Exception:
        available_families = set()

    for family in DEFAULT_MONO_FONT_FAMILIES:
        if family in available_families:
            return family
    return ""


def get_platform_ui_metrics(scale: float = 1.0) -> Dict[str, object]:
    """返回界面布局参数（开源版只支持 Windows，取值与 CS 版在 Windows 上一致）。"""
    return {
        "main_window_ratio": (0.70, 0.70),
        "main_window_min_size": (1200, 760),
        "main_layout_margins": (0, 0, 0, 0),
        "main_layout_spacing": 0,
        "main_panel_min_width": 500,
        "status_label_width": 100,
        "progress_height": 16,
        "toolbar_spacing": 10,
        "toolbar_padding": 5,
        "toolbar_separator_width": 6,
        "toolbar_button_padding_y": 8,
        "toolbar_button_padding_x": 16,
        "toolbar_button_min_width": 80,
        "toolbar_button_min_height": 0,
        "toolbar_indicator_size": max(16, int(round(16 * float(scale or 1.0)))),
        "extra_button_height": 20,
        "help_button_size": 20,
        "settings_min_width": 760,
        "settings_default_size": (900, 760),
        "settings_layout_spacing": 10,
        "settings_tab_padding_v": 8,
        "settings_tab_padding_h": 20,
    }


def get_adaptive_window_size(
    base_width: int,
    base_height: int,
    *,
    ratio: Optional[Tuple[float, float]] = None,
    minimum: Optional[Tuple[int, int]] = None,
) -> Tuple[int, int]:
    """按屏幕可用区域返回合适的窗口尺寸。"""
    app = QApplication.instance()
    if app is None:
        return base_width, base_height

    desktop = app.desktop()
    screen = desktop.availableGeometry(desktop.primaryScreen())
    use_ratio = ratio

    if use_ratio:
        width = int(screen.width() * use_ratio[0])
        height = int(screen.height() * use_ratio[1])
    else:
        width, height = base_width, base_height

    width = min(width, screen.width() - 40)
    height = min(height, screen.height() - 40)

    if minimum:
        width = max(width, minimum[0])
        height = max(height, minimum[1])

    return width, height


def _get_auto_font_scale() -> float:
    """根据屏幕分辨率自动推断字体缩放倍率"""
    app = QApplication.instance()
    if app is None:
        return 1.0

    screen = app.desktop().screenGeometry()
    width = screen.width()

    if width >= 3840:  # 4K及以上分辨率
        return 1.8
    if width >= 2560:  # 2K分辨率
        return 1.4
    if width >= 1920:  # 1080P分辨率
        return 1.0
    return 0.8


def get_ui_font_scale(settings: Optional[QSettings] = None) -> float:
    """获取UI字体缩放倍率（0表示自动）"""
    app = QApplication.instance()
    if settings is None:
        # 延迟导入，避免 qt_settings_bridge -> kh_settings 的模块初始化环。
        from kh_app_identity import QT_APP, QT_ORG
        from qt_settings_bridge import KhQtSettings
        settings = KhQtSettings(QT_ORG, QT_APP)

    try:
        scale = settings.value('ui_font_scale', 0.0, type=float)
    except Exception:
        scale = 0.0

    if scale and scale > 0:
        effective_scale = float(scale)
    else:
        effective_scale = None
        if app is not None:
            cached_auto = app.property("ui_font_scale_auto")
            try:
                if cached_auto is not None:
                    effective_scale = float(cached_auto)
            except Exception:
                effective_scale = None
        if effective_scale is None:
            effective_scale = _get_auto_font_scale()
            if app is not None:
                app.setProperty("ui_font_scale_auto", effective_scale)

    if app is not None:
        app.setProperty("ui_font_scale_effective", effective_scale)
    return effective_scale


def apply_app_font(
    scale: Optional[float] = None,
    base_size: int = DEFAULT_BASE_FONT_SIZE,
    families: Optional[List[str]] = None
) -> Optional[QFont]:
    """应用全局字体设置并返回已设置的字体"""
    app = QApplication.instance()
    if app is None:
        return None

    if scale is None:
        scale = get_ui_font_scale()

    point_size = max(8, int(round(base_size * float(scale))))
    families = families or get_preferred_font_families()

    font = None
    db = QFontDatabase()
    try:
        available_families = set(db.families())
    except Exception:
        available_families = set()

    for family in families:
        if family in available_families:
            font = QFont(family, point_size)
            break

    if font is None:
        font = QFont()
        font.setPointSize(point_size)

    try:
        font.setStyleStrategy(QFont.PreferQuality)
    except Exception:
        pass

    app.setFont(font)
    app.setProperty("ui_font_scale_effective", scale)
    return font


# ── 滚轮误触保护 ────────────────────────────────────────────────────────
# 下拉框/数字框/日期框在未获得焦点时也会响应滚轮：用户滚动对话框想看下面的
# 内容，滚轮正好经过这类控件就会静默改掉它的值（实测导入对话框的 5 分钟
# 起始年份被从 2025 滚成 2015，界面本身却没滚动）。
# 这里让未获焦点的此类控件放弃滚轮，并把事件转交给最近的滚动区域，既不会被
# 误改，滚动手感也保持正常；控件被点击获得焦点后滚轮照常可用。
class UnfocusedWheelGuard(QObject):
    """把未获焦点控件上的滚轮事件交给外层滚动区域。"""

    _GUARDED = (QComboBox, QAbstractSpinBox)

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Wheel or not isinstance(obj, self._GUARDED):
            return False
        if obj.hasFocus():
            return False

        parent = obj.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                QApplication.sendEvent(parent.viewport(), event)
                break
            parent = parent.parentWidget()
        return True


def install_wheel_guard(app: Optional[QApplication] = None) -> Optional[UnfocusedWheelGuard]:
    """给整个应用装上滚轮误触保护；重复调用只装一次。"""
    app = app or QApplication.instance()
    if app is None:
        return None
    guard = app.property("kh_wheel_guard")
    if isinstance(guard, UnfocusedWheelGuard):
        return guard
    guard = UnfocusedWheelGuard(app)
    app.installEventFilter(guard)
    app.setProperty("kh_wheel_guard", guard)
    return guard
