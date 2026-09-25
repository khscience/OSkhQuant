# -*- coding: utf-8 -*-
"""行情导入共用的增量区间规划。

本模块不依赖 Qt，也不调用任何行情接口。它只把交易日历与 DuckDB 中的
日级覆盖情况合成为下载计划，供 BaoStock、Tushare 以及后续 CLI 入口复用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as datetime_date
from datetime import datetime, timedelta, time as datetime_time
import logging
import math
from typing import Iterable, Mapping, Optional, Sequence, Tuple

from .time_utils import coerce_market_time


# XtData/QMT tick 行数会随证券、交易日和行情源版本变化，不能像分钟线
# 一样要求一个固定的“恰好 N 根”。历史 GUI 曾以 4700 根的 95%（4465）
# 作为保守完整阈值；把策略集中在这里，扫描器、覆写校验和测试共用同一
# 份协议。调用方可以通过 ``tick_min_rows`` 覆盖阈值，但默认值保持兼容。
TICK_EXPECTED_ROWS_PER_DAY = 4700
TICK_MIN_ROWS_PER_DAY = int(TICK_EXPECTED_ROWS_PER_DAY * 0.95)
TICK_MAX_AGE_DAYS = 31
TICK_MAX_SPAN_DAYS = 31
TICK_REQUIRED_COLUMNS = ("lastPrice", "volume", "amount")

# 公开的可读别名（不同历史调用点使用过 rows/bars/threshold 命名）。
EXPECTED_TICKS_PER_DAY = TICK_EXPECTED_ROWS_PER_DAY
TICK_COMPLETE_THRESHOLD = TICK_MIN_ROWS_PER_DAY
TICK_MIN_ROWS = TICK_MIN_ROWS_PER_DAY

EXPECTED_BARS_PER_DAY = {
    "1d": 1,
    "1m": 241,
    "5m": 48,
    # 对 tick 而言这是“最低有效行数”，不是精确行数；旧代码读取此
    # 映射做展示/测试，因此保留在同一映射中并在判断处单独处理。
    "tick": TICK_MIN_ROWS_PER_DAY,
}

RAW_PRICE_COLUMNS = ("open", "high", "low", "close")
RAW_COMPLETENESS_COLUMNS = RAW_PRICE_COLUMNS + ("volume", "amount")
ADJUSTMENT_SUFFIXES = ("front", "back", "front_ratio", "back_ratio")
ADJUSTMENT_COLUMNS = tuple(
    f"{field}_{suffix}"
    for suffix in ADJUSTMENT_SUFFIXES
    for field in RAW_PRICE_COLUMNS
)
FRONT_ADJUSTMENT_COLUMNS = tuple(
    f"{field}_{suffix}"
    for suffix in ("front", "front_ratio")
    for field in RAW_PRICE_COLUMNS
)
BACK_ADJUSTMENT_COLUMNS = tuple(
    f"{field}_{suffix}"
    for suffix in ("back", "back_ratio")
    for field in RAW_PRICE_COLUMNS
)


def adjustment_columns_to_invalidate(
    *,
    front_valid: bool = False,
    back_valid: bool = False,
) -> Tuple[str, ...]:
    """返回 raw 写入时必须同步失效的旧复权派生列。"""

    valid_suffixes = set()
    if front_valid:
        valid_suffixes.add("front")
    if back_valid:
        valid_suffixes.add("back")
    return tuple(
        f"{field}_{suffix}"
        for suffix in ADJUSTMENT_SUFFIXES
        if suffix not in valid_suffixes
        for field in RAW_PRICE_COLUMNS
    )


@dataclass(frozen=True)
class IncrementalPlan:
    """单只证券、单周期在一个用户区间内的增量计划。"""

    download_ranges: Tuple[Tuple[str, str], ...]
    expected_dates: Tuple[str, ...]
    complete_dates: Tuple[str, ...]
    missing_dates: Tuple[str, ...]
    partial_dates: Tuple[str, ...]
    ignored_open_dates: Tuple[str, ...]
    anomalous_dates: Tuple[str, ...]
    # Tick 在 QMT 端只保留近一个月；超出窗口的交易日不是“下载失败”，
    # 而是明确排除，避免每次扫描都生成一批必然失败的任务。默认值让旧
    # 的七字段手工构造保持完全兼容。
    ignored_retention_dates: Tuple[str, ...] = ()

    @property
    def needs_download(self) -> bool:
        return bool(self.download_ranges)

    @property
    def unresolved_dates(self) -> Tuple[str, ...]:
        return tuple(sorted(set(self.missing_dates) | set(self.partial_dates)))

    @property
    def out_of_retention_dates(self) -> Tuple[str, ...]:
        """``ignored_retention_dates`` 的语义别名。"""

        return self.ignored_retention_dates

    @property
    def retention_excluded_dates(self) -> Tuple[str, ...]:
        """兼容扫描器使用的“保留窗外”命名。"""

        return self.ignored_retention_dates


def _period_text(period: object) -> str:
    """把字符串/Enum 周期转成稳定的小写 wire value。"""

    value = getattr(period, "value", period)
    return str(value or "").strip().lower()


def _tick_threshold(value: object = TICK_MIN_ROWS_PER_DAY) -> int:
    """校验 tick 最低行数配置，避免 0/负数让空表被判完整。"""

    try:
        threshold = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("tick 最低完整行数必须是正整数") from exc
    if threshold <= 0:
        raise ValueError("tick 最低完整行数必须大于 0")
    return threshold


def required_columns_for_period(
    period: object,
    required_columns: Optional[Sequence[str]] = None,
) -> Tuple[str, ...]:
    """返回完整性校验应传给存储层的真实字段。

    GUI 会把 K 线复权字段列表复用于所有周期；tick 表没有 ``close``/
    ``open_front`` 等列。对 tick 遇到空列表或 K 线字段时，统一回落到
    ``lastPrice/volume/amount``，防止“字段不存在→valid_rows=0”造成无意义
    的重复任务。若调用方明确传入 tick 字段，则保留其自定义要求。
    """

    requested = tuple(dict.fromkeys(
        str(column).strip()
        for column in (required_columns or ())
        if str(column).strip()
    ))
    if _period_text(period) != "tick":
        return requested
    tick_schema = {
        "time", "lastPrice", "open", "high", "low", "lastClose",
        "amount", "volume", "pvolume", "stockStatus", "openInt",
        "lastSettlementPrice", "transactionNum",
    }
    # 复权/普通 K 线字段不是 tick 的质量字段；即使只传了其中一部分，
    # 也必须使用完整的 tick 三字段门禁。
    if not requested or not set(requested).issubset(tick_schema):
        return TICK_REQUIRED_COLUMNS
    return requested


def normalize_date8(value: str) -> str:
    """把 GUI/CLI 使用的日期或时间字符串归一化为 YYYYMMDD。"""

    text = str(value or "").strip()
    if not text:
        raise ValueError("日期不能为空")
    compact = text[:10].replace("-", "").replace("/", "")
    compact = compact[:8]
    datetime.strptime(compact, "%Y%m%d")
    return compact


def required_price_columns(
    *,
    front: bool = False,
    back: bool = False,
) -> Tuple[str, ...]:
    """返回指定行情口径需要非空的价格列。"""

    columns = list(RAW_COMPLETENESS_COLUMNS)
    if front:
        columns.extend(f"{field}_front" for field in RAW_PRICE_COLUMNS)
    if back:
        columns.extend(f"{field}_back" for field in RAW_PRICE_COLUMNS)
    return tuple(columns)


def _is_unclosed_today(date8: str, now: datetime, close_grace_time: datetime_time) -> bool:
    return date8 == now.strftime("%Y%m%d") and now.time() < close_grace_time


def _format_range(period: str, start8: str, end8: str) -> Tuple[str, str]:
    if _period_text(period) == "1d":
        return start8, end8
    return (
        f"{start8[:4]}-{start8[4:6]}-{start8[6:]} 09:00:00",
        f"{end8[:4]}-{end8[4:6]}-{end8[6:]} 15:00:00",
    )


def _group_missing_trade_dates_with_counts(
    missing_dates: Iterable[str],
    expected_trade_dates: Sequence[str],
    *,
    max_span_days: Optional[int] = None,
) -> Tuple[Tuple[str, str, int], ...]:
    """按交易日序列分组；周五与下周一属于连续交易日。

    ``max_span_days`` 是自然日闭区间上限（例如 QMT tick 的 31 天）。
    它只会把一个原本连续的缺口拆成更小的请求，不改变缺口日期集合；
    默认 ``None`` 保持旧版分组行为。
    """

    missing = set(missing_dates)
    try:
        max_span = int(max_span_days) if max_span_days is not None else None
    except (TypeError, ValueError, OverflowError):
        max_span = None
    if max_span is not None and max_span <= 0:
        max_span = None
    groups = []
    group_start = None
    group_end = None
    group_count = 0
    for date8 in expected_trade_dates:
        if date8 in missing:
            if group_start is None:
                group_start = date8
                group_count = 0
            elif max_span is not None:
                # 闭区间 span = (end - start) + 1。拆分前先保存上一组，
                # 保证送给 native bridge 的请求永远不超过其校验上限。
                try:
                    span = (
                        datetime.strptime(date8, "%Y%m%d").date()
                        - datetime.strptime(group_start, "%Y%m%d").date()
                    ).days + 1
                except (TypeError, ValueError):
                    span = 0
                if span > max_span:
                    groups.append((group_start, group_end, group_count))
                    group_start = date8
                    group_count = 0
            group_end = date8
            group_count += 1
        elif group_start is not None:
            groups.append((group_start, group_end, group_count))
            group_start = None
            group_end = None
            group_count = 0
    if group_start is not None:
        groups.append((group_start, group_end, group_count))

    return tuple(groups)


def group_missing_trade_dates(
    missing_dates: Iterable[str],
    expected_trade_dates: Sequence[str],
    *,
    max_span_days: Optional[int] = None,
) -> Tuple[Tuple[str, str], ...]:
    """按交易日序列分组并返回二元日期区间。

    无论是否设置 ``max_span_days``，本公开函数始终保持历史的二元返回
    形状；需要同时取得每组缺口天数的内部扫描器使用
    :func:`group_missing_trade_dates_with_counts`。
    """

    groups = _group_missing_trade_dates_with_counts(
        missing_dates,
        expected_trade_dates,
        max_span_days=max_span_days,
    )
    return tuple((start, end) for start, end, _count in groups)


def group_missing_trade_dates_with_counts(
    missing_dates: Iterable[str],
    expected_trade_dates: Sequence[str],
    *,
    max_span_days: Optional[int] = None,
) -> Tuple[Tuple[str, str, int], ...]:
    """返回 ``(start, end, missing_count)`` 的缺口分组。"""

    return _group_missing_trade_dates_with_counts(
        missing_dates,
        expected_trade_dates,
        max_span_days=max_span_days,
    )


def split_trade_date_ranges(
    missing_dates: Iterable[str],
    expected_trade_dates: Sequence[str],
    *,
    max_span_days: Optional[int] = None,
) -> Tuple[Tuple[str, str], ...]:
    """返回二元日期区间，并可按自然日跨度拆分。

    这是给规划器/扫描器使用的稳定包装；``group_missing_trade_dates``
    保持历史二元返回值，而本函数始终返回 ``(start, end)``。
    """

    if max_span_days is None:
        return group_missing_trade_dates(missing_dates, expected_trade_dates)
    grouped = group_missing_trade_dates(
        missing_dates,
        expected_trade_dates,
        max_span_days=max_span_days,
    )
    return tuple(grouped)


def tick_retained_trade_dates(
    dates: Iterable[str],
    *,
    now: Optional[datetime] = None,
    max_age_days: Optional[int] = None,
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """按 QMT tick 保留窗口拆分日期。

    返回 ``(retained, ignored)``。当 max_age_days 为 None 时不按天数裁剪，
    仅排除未来日期；指定 max_age_days 时排除超出保留窗的日期。
    """

    if max_age_days is not None:
        try:
            age = int(max_age_days)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("tick 保留天数必须是正整数") from exc
        if age < 0:
            raise ValueError("tick 保留天数不能为负数")
    else:
        age = None
    current = now or datetime.now()
    # ``datetime.date`` 本身没有 ``.date()`` 方法；测试/调用方常直接注入
    # date 对象。两种类型都要尊重注入值，不能悄悄退回系统当天。
    if isinstance(current, datetime):
        today = current.date()
    elif isinstance(current, datetime_date):
        today = current
    else:  # pragma: no cover - 仅防御不规范的第三方 now 对象
        today = datetime.now().date()
    cutoff = (today - timedelta(days=age)) if age is not None else None
    retained = []
    ignored = []
    for raw in sorted(set(str(item) for item in (dates or ()))):
        date8 = normalize_date8(raw)
        date_value = datetime.strptime(date8, "%Y%m%d").date()
        if date_value > today:
            ignored.append(date8)
        elif cutoff is not None and date_value < cutoff:
            ignored.append(date8)
        else:
            retained.append(date8)
    return tuple(retained), tuple(ignored)


def validate_tick_frame_retention(
    frame,
    *,
    now: Optional[datetime] = None,
    max_age_days: Optional[int] = None,
) -> None:
    """在写库前拒绝未来/超出指定保留窗的 Tick 行。

    这是所有导入入口共用的最后一道时间边界校验。部分 miniQMT 版本会
    返回带 ``DatetimeIndex`` 的表，故在没有显式 ``time`` 列时兼容索引；
    普通 ``RangeIndex`` 则视为缺少时间键。异常带稳定 ``code`` 属性，便于
    GUI/CLI 将质量拒绝与网络失败区分处理。
    """

    if frame is None or len(frame) == 0:
        return

    columns = getattr(frame, "columns", ())
    values = frame["time"] if "time" in columns else None
    if values is None:
        index = getattr(frame, "index", None)
        if index is not None and index.__class__.__name__ != "RangeIndex":
            values = index
    if values is None:
        error = RuntimeError("tick 返回缺少 time，拒绝写入")
        setattr(error, "code", "DATA_QUALITY")
        raise error

    parsed = coerce_market_time(values)
    if parsed.isna().any():
        error = RuntimeError("tick 返回含无效 time，拒绝写入")
        setattr(error, "code", "DATA_QUALITY")
        raise error

    current = now or datetime.now()
    if isinstance(current, datetime):
        today = current.date()
    elif isinstance(current, datetime_date):
        today = current
    else:  # pragma: no cover - 防御不规范的第三方时钟对象
        today = datetime.now().date()

    dates = parsed.dt.date
    if bool((dates > today).any()):
        error = RuntimeError("tick 返回含未来日期，拒绝写入")
        setattr(error, "code", "OUT_OF_RETENTION")
        raise error

    if max_age_days is not None:
        try:
            age = max(0, int(max_age_days))
        except (TypeError, ValueError, OverflowError):
            age = None
        if age is not None:
            cutoff = today - timedelta(days=age)
            if bool((dates < cutoff).any()):
                error = RuntimeError("tick 返回超出近一个月保留窗口，拒绝写入")
                setattr(error, "code", "OUT_OF_RETENTION")
                raise error


def validate_tick_frame_for_write(
    frame,
    *,
    now: Optional[datetime] = None,
    max_age_days: Optional[int] = None,
) -> None:
    """校验 Tick 写入帧的时间窗口及输入时间戳唯一性。

    数据库层会再次执行唯一键约束；这里提前检查可避免多周期批事务先
    打开写锁后才回滚，并让独立 miniQMT/定时导入入口得到稳定错误码。
    """

    validate_tick_frame_retention(
        frame,
        now=now,
        max_age_days=max_age_days,
    )
    if frame is None or len(frame) == 0:
        return
    columns = getattr(frame, "columns", ())
    values = frame["time"] if "time" in columns else None
    if values is None:
        index = getattr(frame, "index", None)
        if index is not None and index.__class__.__name__ != "RangeIndex":
            values = index
    if values is None:
        # validate_tick_frame_retention already raises this; defensive branch
        # keeps static type checkers and third-party frame objects predictable.
        return
    parsed = coerce_market_time(values)
    duplicate_mask = parsed.duplicated(keep=False)
    if bool(duplicate_mask.any()):
        duplicate_count = int(duplicate_mask.sum())
        duplicate_values = parsed.loc[duplicate_mask].drop_duplicates().head(8).tolist()
        error = RuntimeError(
            f"tick 输入包含 {duplicate_count} 个重复时间戳，拒绝写入"
        )
        setattr(error, "code", "DUPLICATE_TIMESTAMP")
        setattr(error, "duplicate_count", duplicate_count)
        setattr(error, "duplicate_values", duplicate_values)
        raise error


def expected_trade_dates(
    start_date: str,
    end_date: str,
    *,
    trade_days: Optional[Iterable[str]] = None,
    now: Optional[datetime] = None,
    include_open_session: bool = False,
    close_grace_time: datetime_time = datetime_time(15, 15),
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """返回可核验交易日与被忽略的盘中/未来交易日。"""

    start8 = normalize_date8(start_date)
    end8 = normalize_date8(end_date)
    if start8 > end8:
        raise ValueError(f"开始日期 {start8} 晚于结束日期 {end8}")
    if trade_days is None:
        from khQTTools import get_trade_days_set, get_trade_days_set_checked

        trade_days = get_trade_days_set(start8, end8)
        if not trade_days:
            trade_days = get_trade_days_set_checked(start8, end8)

    current = now or datetime.now()
    today8 = current.strftime("%Y%m%d")
    expected = []
    ignored_open = []
    for raw_date in sorted(set(str(item) for item in (trade_days or ()))):
        date8 = normalize_date8(raw_date)
        if date8 < start8 or date8 > end8:
            continue
        if date8 > today8:
            ignored_open.append(date8)
            continue
        if not include_open_session and _is_unclosed_today(
            date8, current, close_grace_time,
        ):
            ignored_open.append(date8)
            continue
        expected.append(date8)
    return tuple(expected), tuple(ignored_open)


def validate_overwrite_frame(
    frame,
    period: str,
    expected_dates: Sequence[str],
    *,
    required_columns: Sequence[str] = RAW_COMPLETENESS_COLUMNS,
    tick_min_rows: int = TICK_MIN_ROWS_PER_DAY,
    verified_short_trade_dates: Optional[Mapping[str, int]] = None,
    verified_absent_trade_dates: Optional[Sequence[str]] = None,
) -> None:
    """在整日覆写前证明远端帧完整，失败时保证数据库零写入。

    ``verified_short_trade_dates`` 列出经日线成交量核对过的合法缺根交易日
    （停牌半天等），这些日期按证据根数精确核对；``verified_absent_trade_dates``
    列出经日线证实的全天停牌日，允许在帧中缺席。证据只放宽列出的日期。
    """

    import pandas as pd

    period_value = _period_text(period)
    if period_value not in EXPECTED_BARS_PER_DAY:
        raise ValueError(f"不支持整日覆写校验的周期: {period_value}")
    expected = {normalize_date8(value) for value in (expected_dates or ())}
    if frame is None or frame.empty:
        raise ValueError("数据源返回空数据，拒绝整日覆写，原数据未改动")
    if "time" not in frame.columns:
        raise ValueError("数据源返回缺少 time，拒绝整日覆写，原数据未改动")
    required = required_columns_for_period(period_value, required_columns)
    missing_columns = [column for column in required if column not in frame.columns]
    if missing_columns:
        raise ValueError(
            "数据源返回缺少必需字段，拒绝整日覆写: "
            + ", ".join(missing_columns)
        )

    # XtData 直接返回 UTC 口径的毫秒时间戳；统一使用写库层相同的市场时间
    # 解析器，避免 pandas 把整数误当纳秒而落到 1970 年。
    parsed_time = coerce_market_time(frame["time"])
    if parsed_time.isna().any():
        raise ValueError("数据源返回含无效 time，拒绝整日覆写，原数据未改动")
    checked = frame.copy()
    checked["_trade_date"] = parsed_time.dt.strftime("%Y%m%d")
    valid_mask = checked[list(required)].notna().all(axis=1)
    for column in required:
        numeric = pd.to_numeric(checked[column], errors="coerce")
        valid_mask &= numeric.notna() & numeric.map(math.isfinite)
    checked["_valid"] = valid_mask
    returned = set(checked["_trade_date"].tolist())
    absent_allowed = {
        normalize_date8(value) for value in (verified_absent_trade_dates or ())
    }
    short_allowed = {
        normalize_date8(key): int(value)
        for key, value in dict(verified_short_trade_dates or {}).items()
    }
    missing_dates = sorted(expected - returned - absent_allowed)
    unexpected_dates = sorted(returned - expected)
    expected_rows = (
        _tick_threshold(tick_min_rows)
        if period_value == "tick"
        else EXPECTED_BARS_PER_DAY[period_value]
    )
    bad_days = []
    for date8, group in checked.groupby("_trade_date", sort=True):
        total_rows = int(group["time"].nunique())
        # 用唯一时间键统计有效行，避免重复输入把缺失的 tick 时间点
        # “冲”成达到阈值；StockDB 后续也会以 DUPLICATE_TIMESTAMP 拒绝。
        valid_rows = int(group.loc[group["_valid"], "time"].nunique())
        if period_value == "tick":
            incomplete = total_rows < expected_rows or valid_rows < expected_rows
        else:
            allowed_rows = expected_rows
            evidence_rows = short_allowed.get(str(date8))
            if (
                period_value != "1d"
                and evidence_rows is not None
                and 0 < evidence_rows < expected_rows
            ):
                allowed_rows = evidence_rows
            incomplete = total_rows != allowed_rows or valid_rows != allowed_rows
        if incomplete:
            bad_days.append((date8, total_rows, valid_rows))
    if missing_dates or unexpected_dates or bad_days:
        details = []
        if missing_dates:
            details.append(
                f"缺少 {len(missing_dates)} 个交易日（{'、'.join(missing_dates[:5])}）"
            )
        if unexpected_dates:
            details.append(
                f"包含 {len(unexpected_dates)} 个区间外日期"
            )
        if bad_days:
            preview = "、".join(
                (
                    f"{date8}:{total}/{'至少' if period_value == 'tick' else ''}"
                    f"{expected_rows}根(有效{valid})"
                )
                for date8, total, valid in bad_days[:5]
            )
            details.append("根数/字段不完整 " + preview)
        raise ValueError(
            "拒绝整日覆写，原数据未改动：" + "；".join(details)
        )


INTRADAY_EVIDENCE_PERIODS = frozenset({"1m", "5m"})


def daily_volume_overwrite_evidence(
    frame,
    daily_frame,
    period: str,
    expected_dates: Sequence[str],
    *,
    tolerance_ratio: float = 1e-4,
) -> tuple[dict[str, int], set[str]]:
    """用同区间日线为分钟整日覆写寻找合法缺根/缺日证据。

    返回 ``(verified_short, verified_absent)``：
    - 分钟根数不足、但分钟成交量之和与日线成交量一致：停牌半天等合法缺根；
    - 分钟整日缺失、而日线该日成交量为 0 或日线同样没有该日：全天停牌。
    日线为空时不给出任何证据，避免日线请求失败被当成“全部停牌”。
    """

    import pandas as pd

    period_value = _period_text(period)
    expected_rows = EXPECTED_BARS_PER_DAY.get(period_value)
    if period_value not in INTRADAY_EVIDENCE_PERIODS or not expected_rows:
        return {}, set()
    if (
        daily_frame is None
        or getattr(daily_frame, "empty", True)
        or "time" not in daily_frame.columns
        or "volume" not in daily_frame.columns
    ):
        return {}, set()
    daily_volume: dict[str, float] = {}
    daily_time = coerce_market_time(daily_frame["time"]).reset_index(drop=True)
    daily_values = pd.to_numeric(daily_frame["volume"], errors="coerce").reset_index(drop=True)
    for when, volume in zip(daily_time, daily_values):
        if pd.isna(when) or pd.isna(volume):
            continue
        daily_volume[when.strftime("%Y%m%d")] = float(volume)
    if not daily_volume:
        return {}, set()

    minute_stats: dict[str, tuple[int, Optional[float]]] = {}
    if (
        frame is not None
        and not getattr(frame, "empty", True)
        and "time" in frame.columns
    ):
        parsed = coerce_market_time(frame["time"]).reset_index(drop=True)
        volumes = (
            pd.to_numeric(frame["volume"], errors="coerce").reset_index(drop=True)
            if "volume" in frame.columns
            else pd.Series([float("nan")] * len(parsed))
        )
        checked = pd.DataFrame({"time": parsed, "volume": volumes})
        checked = checked.loc[checked["time"].notna()]
        for date8, group in checked.groupby(checked["time"].dt.strftime("%Y%m%d")):
            volume = None if group["volume"].isna().any() else float(group["volume"].sum())
            minute_stats[str(date8)] = (int(group["time"].nunique()), volume)

    verified_short: dict[str, int] = {}
    verified_absent: set[str] = set()
    for date8 in sorted({normalize_date8(value) for value in (expected_dates or ())}):
        day_volume = daily_volume.get(date8)
        stat = minute_stats.get(date8)
        if stat is None:
            if day_volume is None or day_volume == 0:
                verified_absent.add(date8)
            continue
        rows, minute_volume = stat
        if rows >= expected_rows or day_volume is None or minute_volume is None:
            continue
        tolerance = max(1.0, abs(day_volume) * float(tolerance_ratio))
        if abs(minute_volume - day_volume) <= tolerance:
            verified_short[date8] = rows
    return verified_short, verified_absent


def validate_overwrite_frame_with_daily_evidence(
    frame,
    period: str,
    expected_dates: Sequence[str],
    *,
    fetch_daily=None,
    **kwargs,
) -> dict[str, int]:
    """整日覆写校验；分钟帧不完整时拉同区间日线找停牌证据后再核对一次。

    返回应传给 ``save_kline_data(verified_short_trade_dates=...)`` 的证据。
    证据仍对不上时抛出与 ``validate_overwrite_frame`` 相同的错误。
    """

    try:
        validate_overwrite_frame(frame, period, expected_dates, **kwargs)
        return {}
    except ValueError as exc:
        if fetch_daily is None or _period_text(period) not in INTRADAY_EVIDENCE_PERIODS:
            raise
        if frame is None or getattr(frame, "empty", True):
            raise
        original_error = exc
    try:
        daily_frame = fetch_daily()
    except Exception:
        raise original_error
    verified_short, verified_absent = daily_volume_overwrite_evidence(
        frame, daily_frame, period, expected_dates,
    )
    if not verified_short and not verified_absent:
        raise original_error
    validate_overwrite_frame(
        frame,
        period,
        expected_dates,
        verified_short_trade_dates=verified_short,
        verified_absent_trade_dates=sorted(verified_absent),
        **kwargs,
    )
    return verified_short


def validate_adjustment_coverage(
    raw_frame,
    adjusted_frame,
    suffixes: Sequence[str],
    *,
    period: Optional[str] = None,
) -> int:
    """证明所选复权列与 raw 时间轴一一对应且全部为有效数值。

    返回应更新的唯一时间键数量。该函数只校验、不修改输入，可同时用于
    Tushare 的独立复权结果与 BaoStock 已合并到 raw 帧中的复权列。
    """

    import pandas as pd

    selected_suffixes = tuple(dict.fromkeys(
        str(value).strip().lower() for value in (suffixes or ())
        if str(value).strip()
    ))
    invalid_suffixes = [
        suffix for suffix in selected_suffixes
        if suffix not in ("front", "back")
    ]
    if invalid_suffixes:
        raise ValueError("不支持的复权口径: " + ", ".join(invalid_suffixes))
    if not selected_suffixes:
        return 0
    if raw_frame is None or raw_frame.empty:
        raise ValueError("原始行情为空，无法核验复权覆盖")
    if adjusted_frame is None or adjusted_frame.empty:
        raise ValueError("复权结果为空")
    if "time" not in raw_frame.columns or "time" not in adjusted_frame.columns:
        raise ValueError("原始行情或复权结果缺少 time")

    raw_times = coerce_market_time(raw_frame["time"])
    adjusted_times = coerce_market_time(adjusted_frame["time"])
    if raw_times.isna().any() or adjusted_times.isna().any():
        raise ValueError("原始行情或复权结果包含无效 time")
    if period == "1d":
        raw_keys = raw_times.dt.strftime("%Y%m%d")
        adjusted_keys = adjusted_times.dt.strftime("%Y%m%d")
    else:
        raw_keys = raw_times.astype("int64")
        adjusted_keys = adjusted_times.astype("int64")
    if raw_keys.duplicated().any():
        raise ValueError("原始行情包含重复时间，无法安全应用复权")
    if adjusted_keys.duplicated().any():
        raise ValueError("复权结果包含重复时间")

    raw_key_set = set(raw_keys.tolist())
    adjusted_key_set = set(adjusted_keys.tolist())
    if raw_key_set != adjusted_key_set:
        missing = len(raw_key_set - adjusted_key_set)
        unexpected = len(adjusted_key_set - raw_key_set)
        raise ValueError(
            f"复权时间轴与 raw 不一致（缺少 {missing}，额外 {unexpected}）"
        )

    required = [
        f"{field}_{suffix}"
        for suffix in selected_suffixes
        for field in RAW_PRICE_COLUMNS
    ]
    missing_columns = [
        column for column in required if column not in adjusted_frame.columns
    ]
    if missing_columns:
        raise ValueError("复权结果缺少字段: " + ", ".join(missing_columns))
    invalid_columns = []
    for column in required:
        numeric = pd.to_numeric(adjusted_frame[column], errors="coerce")
        if numeric.isna().any() or not numeric.map(math.isfinite).all():
            invalid_columns.append(column)
    if invalid_columns:
        raise ValueError(
            "复权结果存在空值或无效数值: " + ", ".join(invalid_columns)
        )
    return len(raw_key_set)


def build_incremental_plan(
    manager,
    stock_code: str,
    period: str,
    start_date: str,
    end_date: str,
    *,
    required_columns: Sequence[str] = RAW_COMPLETENESS_COLUMNS,
    trade_days: Optional[Iterable[str]] = None,
    now: Optional[datetime] = None,
    include_open_session: bool = False,
    close_grace_time: datetime_time = datetime_time(15, 15),
    tick_min_rows: int = TICK_MIN_ROWS_PER_DAY,
    tick_max_age_days: Optional[int] = None,
    tick_max_span_days: int = TICK_MAX_SPAN_DAYS,
) -> IncrementalPlan:
    """读取本地覆盖情况并生成真正的缺口下载区间。

    历史日线必须恰有 1 个时间戳，1m/5m 必须分别恰有 241/48 个不同
    时间戳；所需价格列为空也视为部分日。tick 使用最低有效行数阈值，
    若指定 tick_max_age_days 则只规划保留窗口内的交易日，默认不限制。
    默认不判断尚未收盘的当天，也永不把未来交易日加入下载计划。
    """

    period_value = _period_text(period)
    if period_value not in EXPECTED_BARS_PER_DAY:
        raise ValueError(f"不支持增量规划的周期: {period_value}")

    tick_threshold = _tick_threshold(tick_min_rows)
    try:
        tick_age = int(tick_max_age_days) if tick_max_age_days is not None else None
        tick_span = int(tick_max_span_days)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("tick 保留天数/请求跨度必须是整数") from exc
    if tick_age is not None and tick_age < 0:
        raise ValueError("tick 保留天数不能为负数")
    if tick_span <= 0:
        raise ValueError("tick 请求跨度必须大于 0")

    start8 = normalize_date8(start_date)
    end8 = normalize_date8(end_date)
    if start8 > end8:
        raise ValueError(f"开始日期 {start8} 晚于结束日期 {end8}")

    current = now or datetime.now()
    expected_all, ignored_open = expected_trade_dates(
        start8,
        end8,
        trade_days=trade_days,
        now=current,
        include_open_session=include_open_session,
        close_grace_time=close_grace_time,
    )

    if period_value == "tick":
        expected, ignored_retention = tick_retained_trade_dates(
            expected_all,
            now=current,
            max_age_days=tick_age,
        )
    else:
        expected = expected_all
        ignored_retention = ()

    # 旧版 BaoStock 日线使用 00:00，而 miniQMT/Tushare 使用 09:30。
    # 在可写导入 manager 上先做一次幂等迁移，避免规划器按日期判完整、实际
    # 回测时间轴却仍跨来源错位。只读诊断 manager 不执行任何写入。
    normalize_daily = getattr(manager, "normalize_daily_timestamps", None)
    if (
        period_value == "1d"
        and callable(normalize_daily)
        and not bool(getattr(manager, "read_only", False))
    ):
        migrated_rows = int(normalize_daily(stock_code) or 0)
        if migrated_rows:
            logging.info(
                "%s 旧日线时间已统一到 09:30，共保全并规范化 %d 个交易日",
                stock_code,
                migrated_rows,
            )

    coverage: Mapping[str, Mapping[str, int]] = manager.get_existing_date_completeness(
        stock_code,
        period_value,
        # Tick 的实际字段与 K 线不同；把 GUI 复权字段列表映射为真实
        # tick 质量字段，避免 manager 因 schema 不存在而返回全 0。
        required_columns=list(
            required_columns_for_period(period_value, required_columns)
        ),
        raise_on_error=True,
        start_date=start8,
        end_date=end8,
    )

    expected_bars = (
        tick_threshold if period_value == "tick"
        else EXPECTED_BARS_PER_DAY[period_value]
    )
    complete = []
    missing = []
    partial = []
    anomalous = []
    for date8 in expected:
        stats = coverage.get(date8) or {}
        total_rows = int(stats.get("total_rows", 0) or 0)
        valid_rows = int(stats.get("valid_rows", 0) or 0)
        if period_value == "tick":
            is_complete = (
                total_rows >= expected_bars and valid_rows >= expected_bars
            )
        else:
            is_complete = (
                total_rows == expected_bars and valid_rows == expected_bars
            )
        if is_complete:
            complete.append(date8)
        elif total_rows <= 0:
            missing.append(date8)
        else:
            partial.append(date8)
            # Tick 行数超过经验值是正常的（不同证券/行情源可能有更多
            # 快照），不能把它标成异常并在每次扫描中重复下载。
            if period_value != "tick" and (
                total_rows > expected_bars or valid_rows > expected_bars
            ):
                anomalous.append(date8)

    unresolved = set(missing) | set(partial)
    if period_value == "tick":
        date_groups = split_trade_date_ranges(
            unresolved,
            expected,
            max_span_days=tick_span,
        )
    else:
        date_groups = group_missing_trade_dates(unresolved, expected)
    ranges = tuple(
        _format_range(period_value, start, end)
        for start, end in date_groups
    )
    return IncrementalPlan(
        download_ranges=ranges,
        expected_dates=expected,
        complete_dates=tuple(complete),
        missing_dates=tuple(missing),
        partial_dates=tuple(partial),
        ignored_open_dates=ignored_open,
        anomalous_dates=tuple(anomalous),
        ignored_retention_dates=tuple(ignored_retention),
    )


def full_range(period: str, start_date: str, end_date: str) -> Tuple[str, str]:
    """返回强制覆写模式使用的规范化完整区间。"""

    return _format_range(
        _period_text(period), normalize_date8(start_date), normalize_date8(end_date)
    )
