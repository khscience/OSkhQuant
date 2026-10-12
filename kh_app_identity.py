# coding: utf-8
"""OSkhQuant（看海量化回测平台开源版）的全部身份标识。

开源版可以和看海量化的其他版本装在同一台电脑上同时运行。凡是会和它们撞车的名字、
路径、注册表键、锁名都集中在这里，其他代码一律从这里取，不在各处写死。
"""
import os

APP_NAME = "看海量化回测平台（开源版）"


def _short_version() -> str:
    """窗口标题里显示的版本号：主版本.次版本（2.2.0 → 2.2）。"""
    try:
        from version import get_version

        major, minor = str(get_version()).lstrip("vV").split(".")[:2]
        return f"{major}.{minor}"
    except Exception:
        return "2.2"


SHORT_VERSION = _short_version()
# 窗口标题用：只写版本号，不写「开源版」
APP_TITLE = f"看海量化回测平台 V{SHORT_VERSION}"
APP_SHORT_NAME = "khQuantOS"
EXE_NAME = "khQuantOS.exe"

# QSettings（HKCU\Software\KHQuant\StockAnalyzerOS）
QT_ORG = "KHQuant"
QT_APP = "StockAnalyzerOS"

# 全局配置 ~/.khquant_os/settings.json
SETTINGS_DIR = os.path.join(os.path.expanduser("~"), ".khquant_os")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")

# 单实例锁（Windows 命名互斥量 Local\...）
DESKTOP_INSTANCE_KEY = "KhQuant.OS.Desktop.Main.v1"

# %LOCALAPPDATA% 与「文档」下的目录名
LOCAL_APPDATA_DIR_NAME = "KhQuantOS"
DOCUMENTS_DIR_NAME = "KhQuant_OS"

# V2.1 老版本的标识：只读导入旧配置、检测旧版安装时用，绝不写入
LEGACY_V21_QT_ORG = "KHQuant"
LEGACY_V21_QT_APP = "StockAnalyzer"
LEGACY_APP_ID = "{B39AFBCB-2847-4B4A-B92D-6366E6677A9A}"


def local_appdata_dir(*parts):
    """%LOCALAPPDATA%\\KhQuantOS\\...；取不到环境变量时退回用户目录。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, LOCAL_APPDATA_DIR_NAME, *parts)


def documents_dir(*parts):
    """「文档」\\KhQuant_OS\\...：策略与回测结果等用户文件。"""
    return os.path.join(os.path.expanduser("~"), "Documents", DOCUMENTS_DIR_NAME, *parts)


def default_duckdb_dir():
    """默认行情数据目录。放在 LocalAppData，避开常被 OneDrive 接管的「文档」。"""
    return local_appdata_dir("khData")
