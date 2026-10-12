# 第三方组件说明

看海量化回测平台（开源版）的安装包里包含下列第三方组件。它们各自遵循自己的许可协议，
版权归原作者所有；本软件自身的许可协议见 [LICENSE](LICENSE)。

版本与 `requirements.lock` 一致，许可信息取自各组件发布包的元数据。

## 安装包里包含的运行时组件

| 组件 | 版本 | 许可协议 | 主页 |
| :--- | :--- | :--- | :--- |
| Python | 3.11 | PSF License | https://www.python.org |
| PyQt5 | 5.15.11 | GPL v3 | https://www.riverbankcomputing.com/software/pyqt/ |
| PyQt5-Qt5（Qt 5.15.2） | 5.15.2 | LGPL v3 | https://www.qt.io |
| PyQt5_sip | 12.16.1 | SIP License | https://github.com/Python-SIP/sip |
| DuckDB | 1.4.3 | MIT | https://duckdb.org |
| pandas | 2.3.1 | BSD 3-Clause | https://pandas.pydata.org |
| NumPy | 2.2.6 | BSD 3-Clause | https://numpy.org |
| Matplotlib | 3.10.0 | Matplotlib License（PSF 类） | https://matplotlib.org |
| mplcursors | 0.6 | zlib | https://github.com/anntzer/mplcursors |
| contourpy | 1.3.1 | BSD 3-Clause | https://github.com/contourpy/contourpy |
| cycler | 0.12.1 | BSD | https://matplotlib.org/cycler/ |
| kiwisolver | 1.4.8 | BSD | https://github.com/nucleic/kiwi |
| fonttools | 4.55.3 | MIT | https://github.com/fonttools/fonttools |
| pyparsing | 3.2.0 | MIT | https://github.com/pyparsing/pyparsing |
| Pillow | 11.3.0 | MIT-CMU | https://python-pillow.org |
| BaoStock | 0.9.1 | BSD | http://www.baostock.com |
| Tushare | 1.4.25 | BSD | https://tushare.pro |
| holidays | 0.69 | MIT | https://github.com/vacanza/holidays |
| psutil | 6.1.1 | BSD 3-Clause | https://github.com/giampaolo/psutil |
| requests | 2.32.4 | Apache-2.0 | https://requests.readthedocs.io |
| urllib3 | 2.5.0 | MIT | https://github.com/urllib3/urllib3 |
| certifi | 2025.7.9 | MPL-2.0 | https://github.com/certifi/python-certifi |
| charset-normalizer | 3.4.1 | MIT | https://github.com/jawah/charset_normalizer |
| idna | 2.10 | BSD | https://github.com/kjd/idna |
| websocket-client | 1.8.0 | Apache-2.0 | https://github.com/websocket-client/websocket-client |
| simplejson | 3.20.1 | MIT / AFL | https://github.com/simplejson/simplejson |
| beautifulsoup4 / bs4 | 4.13.3 / 0.0.2 | MIT | https://www.crummy.com/software/BeautifulSoup/ |
| soupsieve | 2.6 | MIT | https://github.com/facelessuser/soupsieve |
| lxml | 5.3.2 | BSD 3-Clause | https://lxml.de |
| openpyxl | 3.1.5 | MIT | https://openpyxl.readthedocs.io |
| et_xmlfile | 2.0.0 | MIT | https://foss.heptapod.net/openpyxl/et_xmlfile |
| xlrd | 2.0.1 | BSD | http://www.python-excel.org |
| tabulate | 0.9.0 | MIT | https://github.com/astanin/python-tabulate |
| tqdm | 4.67.1 | MPL-2.0 AND MIT | https://tqdm.github.io |
| python-dateutil | 2.9.0.post0 | BSD / Apache-2.0 | https://github.com/dateutil/dateutil |
| pytz | 2024.2 | MIT | https://pythonhosted.org/pytz |
| tzdata | 2024.2 | Apache-2.0 | https://github.com/python/tzdata |
| six | 1.17.0 | MIT | https://github.com/benjaminp/six |
| packaging | 24.2 | Apache-2.0 / BSD | https://packaging.pypa.io |
| typing_extensions | 4.14.1 | PSF-2.0 | https://github.com/python/typing_extensions |
| colorama | 0.4.6 | BSD | https://github.com/tartley/colorama |

## 只在打包时使用的工具（不作为库分发）

| 工具 | 版本 | 许可协议 | 说明 |
| :--- | :--- | :--- | :--- |
| PyInstaller | 6.14.2 | GPLv2+，附带允许分发打包产物的例外条款 | 生成 exe；安装包里只有它的启动器（适用该例外） |
| pyinstaller-hooks-contrib | 2025.5 | Apache-2.0 / GPLv2 | 打包钩子 |
| altgraph、pefile、pywin32-ctypes、setuptools | 见 requirements-build.lock | MIT / BSD | PyInstaller 的依赖 |
| Inno Setup | 6 | Inno Setup License | 生成安装程序 |
| packaging/ChineseSimplified.isl | Inno Setup 6.1.0+ | 随 Inno Setup 分发的简体中文翻译，维护者 Zhenghan Yang | https://github.com/kira-96/Inno-Setup-Chinese-Simplified-Translation |

## 数据服务

软件本身不带行情数据。BaoStock、Tushare 的数据由用户自己下载，使用时请遵守各数据服务的条款；
Tushare 需要注册账号，分钟线等接口需要相应的积分或权限。
