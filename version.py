# version.py

VERSION_INFO = {
    "version": "2.1.4",          # 当前版本号
    "build_date": "2025-12-04",  # 构建日期
    "channel": "stable",         # 更新通道
    "app_name": "看海量化回测平台" # 应用名称
}

def get_version():
    """获取版本号"""
    return VERSION_INFO["version"]

def get_version_info():
    """获取完整版本信息"""
    return VERSION_INFO.copy()

def get_channel():
    """获取更新通道"""
    return VERSION_INFO["channel"]