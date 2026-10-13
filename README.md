# 看海量化回测平台（开源版）2.2

看海量化回测平台（KHQuant）的开源版：Windows 桌面程序，读取本地 DuckDB 行情数据做 A 股策略回测，
支持 T+0 / T+1 规则、Tick / 1 分钟 / 5 分钟 / 日线触发、成交量限制与滑点、完整的回测报告。

- **回测内核逐笔核对**：本次使用冻结数据、固定策略参数和相同回测配置的六组案例，逐笔核对成交、费用和收益；数据、配置、随机参数或内核版本变化都可能影响结果。
- **不需要 QMT / miniQMT**：数据用 BaoStock（免费、不用注册）或 Tushare 下载到本地 DuckDB。
- **独立安装**：可以和 V2.1 装在同一台电脑上同时运行，设置、数据、日志互不干扰。

> **需要更多功能？看看 V3**：Windows / macOS / Linux 三端通用、CLI 命令行模式、Web 回测工作台、更多数据源（持续集成中）、
> 定时自动补数据、Windows 集成 VS Code 启动与断点调试（需自行安装）、Python 包管理、13 个策略案例（另有持续更新的专栏策略案例）。V3 和 2.2 用同一套回测内核，策略可以直接搬过去。
> 对比见下方「开源版与 V3」一节，详情见官网：https://khsci.com/khQuant/#version-comparison

> 从 V2.1 升级？先看 [docs/从V2.1迁移.md](docs/从V2.1迁移.md)。

## 下载安装

- GitHub：https://github.com/khscience/OSkhQuant/releases/latest
- Gitee：https://gitee.com/mrkanhai/oskhquant/releases

下载 `khQuantOS_Setup_V2.x.x.exe`，可以用同页的 `SHA256SUMS.txt` 核对文件。系统要求 Windows 10/11 64 位。

> 首次运行可能出现「Windows 已保护你的电脑」：安装包没有付费数字签名（开源项目普遍如此），
> 点「更多信息」→「仍要运行」即可。

## 快速上手

1. **首次启动引导**：选一个数据目录（默认 `%LOCALAPPDATA%\KhQuantOS\khData`，建议换到非系统盘），
   勾选「下载沪深300作为基准」。装过 V2.1 的话，可以顺便只读导入它的设置。
2. **加载示例并填写股票池**：「加载配置」选一个示例（`文档\KhQuant_OS\strategies`）。随包 `.kh` 的 `data.stock_list` 留空；请在左侧「股票池设置」自行添加研究标的，单股票案例填一只，多股票案例可填多只，然后保存配置。
3. **下载对应数据**：「数据管理 → BaoStock导入」，填写自己选择的同一批股票并下载日线。开始日期要早于回测开始日期，给指标留出预热期；ETF 请使用 Tushare。
4. **运行示例**：检查股票池非空，把回测区间改到已下载的数据范围内，再点「开始运行」。案例参数包含随机数；比较两次结果前要先固定参数。

## 数据来源与能力边界

| 周期 | BaoStock | Tushare | 说明 |
| :--- | :--- | :--- | :--- |
| 日线 | ✅ 免费，不用账号 | ✅ 需要 Token | 复权价需 Tushare 约 2000 积分 |
| 5 分钟 | ✅ | ✅ 需要分钟权限 | |
| 1 分钟 | ❌ | ✅ 股票用 `stk_mins`，ETF 用 `etf_mins` | 需对应分钟权限 |
| Tick | ❌ | ❌ | 需自行导入 DuckDB |
| 指数 | ✅ 日线 | 只有日线 | 基准默认 000300.SH |
| ETF / LOF 等场内基金 | ❌ | 日线用 `fund_daily`；ETF 分钟用 `etf_mins` | LOF 等其他基金仅支持日线 |
| 可转债 | ❌ | `cb_daily` 不复权日线 | 取消前/后复权；不支持分钟线 |

- BaoStock 软件内每天最多用 3 万次请求，「数据管理」状态栏显示当天已用次数。增量下载前复权数据会把本地整段历史重新拉一遍，
  全市场只更新日线大约需要 1.5 万次。
- 新导入 K 线的成交量标准化为「手」、成交额为「元」，保留小数手。旧表兼容续写保留历史原值并记录待核验状态，不表示历史单位已经统一，也不会自动缩放旧数据。
- Tushare 默认使用官方 HTTPS 接口。API 地址留空即可；自定义地址应由服务提供方确认，`http://` 地址不提供传输加密。权限、限流和服务端错误会报错，只有成功响应的空数据才按 0 条处理。

## 开源版与 V3

2.2.0 对齐 V3.4.1.10 的回测内核；本次六组冻结案例已在相同数据、固定策略参数和相同配置下逐笔核对。不能据此保证不同版本、不同配置或随机参数的任意策略结果始终相同。

| 项目 | 开源版 2.2 | V3 |
| :--- | :--- | :--- |
| 系统 | Windows | Windows 与 Apple Silicon macOS 桌面版；Linux 提供命令行和完整的 Web 回测工作台 |
| 使用方式 | 图形界面 | 另有 CLI 命令行：不打开界面也能下载数据、回测、查看结果，适合脚本批量跑、部署到远程服务器 |
| 数据来源 | BaoStock（免费）、Tushare | 另加同花顺（扶摇开放平台）、miniQMT，并会持续集成更多数据源 |
| Web 回测 | — | Web 回测工作台：在浏览器里跑回测、看结果，手机也能远程访问 |
| 策略编写 | 用自己的编辑器写 .py 文件 | Windows 集成 VS Code 启动与断点调试，需自行安装；Mac/Linux 使用外部编辑器 |
| 数据维护 | 手动下载、增量补充 | 另有定时自动补充数据 |
| 包管理 | — | Python 包管理页：在软件里安装策略要用的第三方库 |
| 策略案例 ※ | 4 个基础案例：双均线 3 个、RSI 1 个 | 共 13 个：另有 T+0 ETF 网格 4 个、数据库指标读写 4 个、日内多次触发 1 个；另有持续更新的专栏策略案例 |
| 回测内核 | 成交、费用、滑点、T+0 / T+1、复权 | 2.2.0 对齐 V3.4.1.10；相同条件下的六组冻结案例已逐笔核对 |

※ 策略案例说明：随包配置的股票池为空，需用户自行填写；案例参数中加入了随机数，仅作为算法写法演示，不构成任何策略或投资建议。

了解 V3：https://khsci.com/khQuant/#version-comparison

## 和 V2.1 同时使用

| 项目 | 开源版 2.2 |
| :--- | :--- |
| 程序 | `khQuantOS.exe`，默认装在 `C:\Program Files\khQuantOS`，开始菜单组 `khQuantOS` |
| 设置 | `~\.khquant_os\settings.json`、注册表 `HKCU\Software\KHQuant\StockAnalyzerOS` |
| 数据 | 默认 `%LOCALAPPDATA%\KhQuantOS\khData` |
| 日志 | `%LOCALAPPDATA%\KhQuantOS\logs` |
| 回测结果 | `%LOCALAPPDATA%\KhQuantOS\backtest_results`（源码运行时在项目目录下） |
| 策略 | `文档\KhQuant_OS\strategies` |

DuckDB 同一个库文件同一时刻只允许一个进程写，有进程在写时别的进程连只读也打不开。
下载或回测提示数据库被占用时，可以在「数据管理 → 占用诊断」里查看是哪个程序占着。

## 策略开发

策略是一个 Python 文件，实现 `init`、`khHandlebar`，可选 `khPreMarket`、`khPostMarket`：

```python
from khQuantImport import *

def init(stock_list, data):
    pass

def khHandlebar(data):
    signals = []
    price = khPrice(data, "000001.SZ", "close")
    if not khHas(data, "000001.SZ") and price > 0:
        signals.extend(generate_signal(data, "000001.SZ", price, 0.5, "buy", "示例买入"))
    return signals
```

常用接口：`khHistory`、`khKline`、`khMA`、`khPrice`、`khGet`、`khHas`、`khDuckDB`、`khDuckWrite`、MyTT 指标、缠论工具。
完整教程见 https://khsci.com/khQuant/tutorial/ 。V2.1 的 `xtdata` 接口在开源版里不可用，调用时会提示改用 `khHistory` 等本地接口。

## 源码运行与打包

```bash
python -m pip install -r requirements.txt
python GUIkhQuant.py
```

- 需要 Python 3.11（Windows 10/11 64 位）。
- 测试：`python -m pip install pytest`，然后 `python -m pytest tests`。
- 打包：再安装 `requirements-build.lock` 和 Inno Setup 6，运行 `build_inno.bat`，安装包在 `Output\`。
- 本仓库由看海量化主库的构建脚本生成，`PORT_BASE` 记录对应的主库提交。

## 反馈

问题和建议请提交 Issue：[GitHub](https://github.com/khscience/OSkhQuant/issues) · [Gitee](https://gitee.com/mrkanhai/oskhquant/issues)。
请附上软件版本、操作步骤和日志（`%LOCALAPPDATA%\KhQuantOS\logs\app.log`）。

作者：Mr.看海 · 微信公众号「看海的城堡」 · [知乎](https://www.zhihu.com/people/feng-zhu-38) · [B站](https://space.bilibili.com/3546667687086777)

## 许可与免责

- 源代码遵循 [CC BY-NC 4.0](LICENSE)（署名-非商业性使用），禁止商业用途。第三方组件见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
- 本软件只用于量化编程技术交流和策略研究，不构成任何投资建议。历史回测表现不代表未来收益，投资有风险，入市需谨慎。
