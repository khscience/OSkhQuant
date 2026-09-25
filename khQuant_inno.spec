# khQuant_inno.spec —— 看海量化回测平台（开源版）PyInstaller 打包配置
#
#   pyinstaller khQuant_inno.spec
#
# 产物：dist\khQuantOS\khQuantOS.exe（ASCII 目录名，便于 CI 和 Release 附件）。
# 只打包下面白名单里的源码和数据：开源版没有 xtquant、内嵌编辑器、网页端、CLI。
import glob
import os
import sys

from PyInstaller.building.build_main import Analysis, COLLECT, EXE, PYZ
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

base_path = os.path.abspath(os.getcwd())
sys.path.insert(0, base_path)
from kh_app_identity import EXE_NAME  # noqa: E402  khQuantOS.exe

APP_BASENAME = os.path.splitext(EXE_NAME)[0]

release_root_python_sources = [
    'BacktestHistoryManager.py',
    'GUIkhQuant.py',
    'MyTT.py',
    'SettingsDialog.py',
    'backtest_result_window.py',
    'backtest_runtime_config.py',
    'baostock_proxy.py',
    'data_integrity_policy.py',
    'khChanLunTools.py',
    'khConfig.py',
    'khDataSource.py',
    'khDynamicLoader.py',
    'khFrame.py',
    'khPathUtils.py',
    'khQTTools.py',
    'khQuantImport.py',
    'khRisk.py',
    'khTrade.py',
    'khUiScale.py',
    'kh_app_identity.py',
    'kh_constants.py',
    'kh_data_dir_policy.py',
    'kh_first_run.py',
    'kh_platform.py',
    'kh_revisioned_config.py',
    'kh_settings.py',
    'kh_single_instance.py',
    'kh_startup_logging.py',
    'kh_stock_pools.py',
    'kh_xtdata_stub.py',
    'performance_config.py',
    'qt_settings_bridge.py',
    'security_type_utils.py',
    'stock_analysis_window.py',
    'tushare_config.py',
    'update_manager.py',
    'version.py',
]

missing = [name for name in release_root_python_sources if not os.path.isfile(os.path.join(base_path, name))]
if missing:
    raise RuntimeError('打包清单里的文件不存在: %s' % ', '.join(missing))

duckdb_storage_modules = sorted(
    'duckdb_storage.' + os.path.splitext(os.path.basename(path))[0]
    for path in glob.glob(os.path.join(base_path, 'duckdb_storage', '*.py'))
    if not path.endswith('__init__.py')
)
local_modules = [os.path.splitext(name)[0] for name in release_root_python_sources] + ['duckdb_storage'] + duckdb_storage_modules

datas = [
    ('icons/*', 'icons'),
    ('data/*.csv', 'data'),
    ('strategies/*.py', 'strategies'),
    ('strategies/*.kh', 'strategies'),
    ('duckdb_storage/*.py', 'duckdb_storage'),
    ('LICENSE', '.'),
    ('THIRD_PARTY_NOTICES.md', '.'),
]
if os.path.isfile(os.path.join(base_path, 'data', 'stock_list', 'current_stock_list.csv')):
    datas.append(('data/stock_list/current_stock_list.csv', 'data/stock_list'))
datas += [(os.path.join(base_path, name), '.') for name in release_root_python_sources]
datas += collect_data_files('holidays')
datas += collect_data_files('duckdb')
datas += copy_metadata('duckdb')
datas += collect_data_files('tushare')
datas += collect_data_files('baostock')
datas += collect_data_files('matplotlib')

hiddenimports = local_modules + [
    'PyQt5', 'PyQt5.sip', 'PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets',
    'matplotlib.backends.backend_qt5agg', 'mplcursors',
    'duckdb', '_duckdb',
    'psutil', 'requests',
    'multiprocessing', 'multiprocessing.spawn', 'multiprocessing.pool', 'multiprocessing.managers',
] + collect_submodules('holidays') + collect_submodules('baostock') + collect_submodules('tushare') \
  + collect_submodules('duckdb')

excludes = [
    # 开源版不包含的模块（即使误加了导入，也不能被打进安装包）
    'xtquant', 'debugpy', 'pyautogui', 'editor_debug_modules', 'cli', 'webapp',
    'GUI', 'GUIDataViewer', 'GUIScheduler', 'GUIScheduledDataSync', 'GUIPackageManager',
    'khTheme', 'miniQMT_data_viewer', 'miniQMT_data_parser',
    'kh_bigqmt_bridge', 'kh_qmt_native_bridge', 'khprivate', 'research',
    # 用不到的大库
    'tkinter', 'tcl', 'tk',
    'PyQt5.QtWebEngine', 'PyQt5.QtWebEngineCore', 'PyQt5.QtWebEngineWidgets', 'PyQt5.QtWebChannel',
    'IPython', 'jupyter', 'notebook', 'scipy',
    'pandas.tests', 'numpy.testing', 'matplotlib.tests', 'pytest',
]

analysis = Analysis(
    ['GUIkhQuant.py'],
    pathex=[base_path],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(analysis.pure, analysis.zipped_data)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=APP_BASENAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon='icons/stock_icon.ico',
    # 数据、设置、日志都在 %LOCALAPPDATA%\KhQuantOS 和「文档\KhQuant_OS」，不需要管理员权限
    uac_admin=False,
)

coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_BASENAME,
)
