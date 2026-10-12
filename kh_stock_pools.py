from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class StockPoolDefinition:
    id: str
    label: str
    aliases: tuple[str, ...]
    cli: bool = True
    desktop_codes: tuple[str, ...] = ()
    web: bool = False


# 单一注册表明确各端能力差异：CLI 额外提供深证A股/沪深转债，桌面与
# Web 保持 12 个常用池 + 自选清单。aliases 首项必须是当前随包文件名。
STOCK_POOL_DEFINITIONS = (
    StockPoolDefinition("sz50", "上证50", ("上证50成分股_股票列表.csv",), desktop_codes=("sh.000016",), web=True),
    StockPoolDefinition("hs300", "沪深300", ("沪深300成分股_股票列表.csv",), desktop_codes=("sh.000300",), web=True),
    StockPoolDefinition("zz500", "中证500", ("中证500成分股_股票列表.csv",), desktop_codes=("sh.000905",), web=True),
    StockPoolDefinition("gem", "创业板", ("创业板_股票列表.csv",), desktop_codes=("sz.399006",), web=True),
    StockPoolDefinition("star", "科创板", ("科创板_股票列表.csv",), desktop_codes=("sci_tech", "sh.000688"), web=True),
    StockPoolDefinition("a", "沪深A股", ("沪深A股_股票列表.csv",), desktop_codes=("all_a",), web=True),
    StockPoolDefinition("sha", "上证A股", ("上证A股_股票列表.csv",), desktop_codes=("sh_a",), web=True),
    StockPoolDefinition("sza", "深证A股", ("深证A股_股票列表.csv",)),
    StockPoolDefinition("etf", "沪深ETF", ("沪深ETF_成分股列表.csv", "沪深ETF_股票列表.csv"), desktop_codes=("hs_etf",), web=True),
    StockPoolDefinition("fund", "沪深场内基金（含ETF/LOF）", ("沪深基金_列表.csv", "沪深基金_股票列表.csv"), desktop_codes=("hs_fund",), web=True),
    StockPoolDefinition("t0etf", "T0型ETF", ("T0型ETF.csv", "T0型ETF_股票列表.csv"), desktop_codes=("t0_etf",), web=True),
    StockPoolDefinition("t0stock", "T0股票型ETF", ("T0股票型ETF列表.csv", "T0股票型ETF_股票列表.csv"), desktop_codes=("t0_stock_etf",), web=True),
    StockPoolDefinition("bond", "沪深转债", ("沪深转债_列表.csv", "沪深转债_股票列表.csv")),
    StockPoolDefinition("index", "常用指数", ("指数_股票列表.csv", "常用指数_股票列表.csv"), desktop_codes=("common_index",), web=True),
    StockPoolDefinition("custom", "自选清单", ("otheridx.csv",), cli=False, desktop_codes=("custom",), web=True),
)

POOL_INDEX = {item.id: item for item in STOCK_POOL_DEFINITIONS}
DESKTOP_CODE_INDEX = {
    code: item for item in STOCK_POOL_DEFINITIONS for code in item.desktop_codes
}


def cli_pool_definitions() -> tuple[StockPoolDefinition, ...]:
    return tuple(item for item in STOCK_POOL_DEFINITIONS if item.cli)


def desktop_pool_definitions() -> tuple[StockPoolDefinition, ...]:
    return tuple(item for item in STOCK_POOL_DEFINITIONS if item.desktop_codes)


def web_pool_definitions() -> tuple[StockPoolDefinition, ...]:
    return tuple(item for item in STOCK_POOL_DEFINITIONS if item.web)


def find_pool_file(
    definition: StockPoolDefinition,
    data_dirs: Iterable[Path],
) -> Path | None:
    for data_dir in data_dirs:
        root = Path(data_dir)
        for base in (root, root / "stock_lists"):
            for filename in definition.aliases:
                path = base / filename
                if path.is_file():
                    return path
    return None
