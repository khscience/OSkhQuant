# 看海量化回测平台（开源版）2.2

看海量化回测平台（KHQuant）的开源版：Windows 桌面程序，读取本地 DuckDB 行情数据做 A 股策略回测，
支持 T+0 / T+1 规则、Tick / 1 分钟 / 5 分钟 / 日线触发、成交量限制与滑点、完整的回测报告。

- **回测内核与看海量化 CS 版一致**：同一份数据、同一个策略，两边结果逐笔相同（发版前用冻结数据逐笔比对）。
- **不需要 QMT / miniQMT**：数据用 BaoStock（免费、不用注册）或 Tushare 下载到本地 DuckDB。
- **独立安装**：可以和 V2.1、CS 版装在同一台电脑上同时运行，设置、数据、日志互不干扰。

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
2. **下载数据**：主界面「数据管理 → BaoStock导入」，选股票池和日期下载日线。
   开始日期要早于回测开始日期，给均线等指标留出预热期。
3. **运行示例**：「加载配置」选一个示例策略（`文档\KhQuant_OS\strategies`），把回测区间改到已下载的数据范围内，点「开始运行」。

## 数据来源与能力边界

| 周期 | BaoStock | Tushare | 说明 |
| :--- | :--- | :--- | :--- |
| 日线 | ✅ 免费，不用账号 | ✅ 需要 Token | 复权价需 Tushare 约 2000 积分 |
| 5 分钟 | ✅ | ✅ 需要分钟权限 | |
| 1 分钟 | ❌ | ✅ 需要 `stk_mins` 权限 | 界面标注「需自备数据」 |
| Tick | ❌ | ❌ | 需自行导入 DuckDB |
| 指数 | ✅ 日线 | 只有日线 | 基准默认 000300.SH |
| ETF / LOF 等场内基金 | ❌ | ✅ 需要 `fund_daily` 权限 | BaoStock 下载时会自动跳过基金代码 |

- BaoStock 软件内每天最多用 3 万次请求，「数据管理」状态栏显示当天已用次数。增量下载前复权数据会把本地整段历史重新拉一遍，
  全市场只更新日线大约需要 1.5 万次。
- DuckDB 里的成交量统一是「手」，各数据源在导入时换算。

## 和 CS 版、V2.1 同时使用

| 项目 | 开源版 2.2 |
| :--- | :--- |
| 程序 | `khQuantOS.exe`，默认装在 `C:\Program Files\khQuantOS`，开始菜单组 `khQuantOS` |
| 设置 | `~\.khquant_os\settings.json`、注册表 `HKCU\Software\KHQuant\StockAnalyzerOS` |
| 数据 | 默认 `%LOCALAPPDATA%\KhQuantOS\khData` |
| 日志 | `%LOCALAPPDATA%\KhQuantOS\logs` |
| 回测结果 | `%LOCALAPPDATA%\KhQuantOS\backtest_results`（源码运行时在项目目录下） |
| 策略 | `文档\KhQuant_OS\strategies` |

**不要让开源版和 CS 版共用同一个数据目录。** DuckDB 同一个库文件同一时刻只允许一个进程写，
有进程在写时别的进程连只读也打不开，两边会互相挡住补数和回测。需要 CS 的数据时，
在「数据管理 → 复制CS数据」里复制一份给开源版（只读访问 CS 的目录，每个库复制完立即断开）。
如果坚持直接指向 CS 的目录，导入、核验索引、WAL 修复每次都要确认，策略里的 `khDuckWrite` 会拒绝写入。

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
