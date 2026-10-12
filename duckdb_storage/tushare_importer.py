# -*- coding: utf-8 -*-
"""
Tushare 数据导入核心模块

功能：
- 封装 tushare API 调用，API 地址可由外部配置（默认官方地址）
- 支持可选代理（代理开关）
  不使用代理时自动设置 NO_PROXY 绕过系统代理，直连 tushare 域名
- 乘法前复权：前复权价 = 原始价 × (当日 adj_factor / 最新 adj_factor)
  注意：end_date 必须传最新实际交易日，否则前复权基准日不是今天，结果偏高

使用示例：
    importer = TushareImporter(token="your_token", use_proxy=False)
    ok, msg = importer.test_connection()
    if ok:
        df = importer.mul_qfq("000001.SZ", "20240101", "20241231", freq="D")
"""

from __future__ import annotations

import os
import datetime
import logging
import math
import time
from urllib.parse import urlparse

import pandas as pd
import requests

from tushare_config import DEFAULT_TUSHARE_API_URL, normalize_tushare_api_url
from security_type_utils import is_listed_fund_code

logger = logging.getLogger(__name__)


class TushareAPIError(RuntimeError):
    """Tushare 请求本身失败；与接口正常返回空行情明确区分。"""


class AdjustmentFactorCoverageError(RuntimeError):
    """复权因子未完整覆盖待计算行情，禁止生成可能错误的复权价格。"""


def _safe_error_text(error: object, token: str = "") -> str:
    """生成可记录的脱敏异常文本，避免日志意外暴露 Token。"""
    message = str(error)
    if token:
        message = message.replace(token, "***")
    return message

# 空字符串表示使用 Tushare SDK 自带的数据接口；仅自定义服务才覆盖实例地址。
TUSHARE_DEFAULT_URL = DEFAULT_TUSHARE_API_URL

# 日线时间偏移（09:30:00，与 miniQMT 保持一致）
_DAILY_TIME_OFFSET = pd.Timedelta(hours=9, minutes=30)

# 各分钟频率每交易日 bar 数。1 分钟源包含 09:30 共 241 根；
# 5 分钟完整交易日按本地 Tushare/miniQMT 实际落库结果为 48 根。
_BARS_PER_DAY: dict = {
    "1min": 241, "5min": 48, "15min": 17, "30min": 9, "60min": 5,
}

# 官方 fund_adj 单次最多 2000 行，并明确支持 offset/limit 分页。
# 股票 adj_factor 官方说明单只股票可一次提取全部历史，因此不额外放大请求量；
# 无论来源如何，后续复权计算仍会逐交易日校验因子覆盖，截断结果不会被静默使用。
_FUND_ADJ_PAGE_SIZE = 2000
_FUND_ADJ_MAX_PAGES = 100

_TRANSIENT_API_ERROR_MARKERS = (
    "ssl", "eof", "connection", "max retries", "remote disconnected",
    "connection reset", "connection aborted", "timed out", "timeout",
    "broken pipe", "temporarily unavailable", "too many requests", "429",
    "502", "503", "504", "频率", "限流", "每分钟", "请求过于频繁", "服务繁忙",
    "系统繁忙", "网关", "连接中断", "连接重置", "网络异常",
)

_PERMANENT_API_ERROR_MARKERS = (
    "token不对", "token 无效", "token失效", "没有访问该接口的权限",
    "无权限", "权限不足", "积分不足", "参数错误", "invalid parameter",
    "api_name不存在", "接口不存在", "不在积分档范围", "单独开通",
)


def _is_retryable_api_error(error: Exception) -> bool:
    """仅重试网络瞬断、限流和服务端临时异常，避免放大永久性错误。"""
    message = str(error or "").strip().lower()
    if any(marker in message for marker in _PERMANENT_API_ERROR_MARKERS):
        return False
    return any(marker in message for marker in _TRANSIENT_API_ERROR_MARKERS)


def _api_retry_settings() -> tuple[int, float]:
    """读取可测试的重试参数；默认共尝试 3 次，指数退避 0.8/1.6 秒。"""
    try:
        attempts = int(os.environ.get("KH_TUSHARE_API_ATTEMPTS", "3") or 3)
    except (TypeError, ValueError):
        attempts = 3
    try:
        base_delay = float(os.environ.get("KH_TUSHARE_API_RETRY_DELAY", "0.8") or 0.8)
    except (TypeError, ValueError):
        base_delay = 0.8
    return max(1, min(attempts, 8)), max(0.0, min(base_delay, 30.0))


def _normalize_url_text(url: str) -> str:
    """将 URL 中误填的中文冒号规范化为英文冒号。"""
    if not isinstance(url, str):
        return ""
    return url.replace("：", ":").strip()


def _normalize_api_url(url: str) -> str:
    """规范化自定义 Tushare API 根地址；留空时保留 SDK 默认行为。"""
    raw_url = normalize_tushare_api_url(_normalize_url_text(url)).rstrip("/")
    if not raw_url:
        return ""
    # 兼容误把接口名一并粘贴到设置中的历史输入。
    if raw_url.lower().endswith("/daily"):
        raw_url = raw_url[:-6].rstrip("/")
    if not raw_url.lower().startswith(("http://", "https://")):
        raw_url = "https://" + raw_url
    return raw_url


def is_tushare_index_code(ts_code: str) -> bool:
    """识别当前 KH 股票代码体系中应走 ``index_daily`` 的常见指数。"""
    code, _, market = str(ts_code or "").strip().upper().partition(".")
    return (
        (market == "SH" and code.startswith("000"))
        or (market == "SZ" and code.startswith("399"))
        or (market == "BJ" and code.startswith("899"))
    )


def _setup_proxy(use_proxy: bool, proxy_url: str = "", api_url: str = "") -> None:
    """配置或清除代理环境变量。"""
    if use_proxy and proxy_url:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ[key] = proxy_url
        # 不绕过 tushare 域名
        os.environ.pop("NO_PROXY",  None)
        os.environ.pop("no_proxy",  None)
    else:
        # 不使用代理时清除之前可能设置的代理变量，防止残留
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
            "ALL_PROXY", "all_proxy",
        ):
            os.environ.pop(key, None)
        # 确保 tushare 域名不走系统代理
        existing_domains = set(
            d.strip() for d in os.environ.get("NO_PROXY", "").split(",") if d.strip()
        )
        tushare_domains = {"api.tushare.pro", "tushare.pro", "tushare.xyz"}
        custom_hostname = urlparse(_normalize_api_url(api_url)).hostname
        if custom_hostname:
            tushare_domains.add(custom_hostname)
        missing = tushare_domains - existing_domains
        if missing:
            all_domains = sorted(existing_domains | tushare_domains)
            os.environ["NO_PROXY"] = ",".join(all_domains)
            os.environ["no_proxy"] = os.environ["NO_PROXY"]


def _prepare_adj_factor_frame(df_factor: pd.DataFrame) -> pd.DataFrame:
    """Normalize factor dates/types before matching them to market data."""
    required = {"adj_factor", "trade_date"}
    if df_factor is None or df_factor.empty or not required.issubset(df_factor.columns):
        return pd.DataFrame(columns=["trade_date", "adj_factor"])

    factors = df_factor.loc[:, ["trade_date", "adj_factor"]].copy()
    date_text = factors["trade_date"].astype("string").str.strip()
    date_text = date_text.str.replace(r"\.0$", "", regex=True)
    parsed = pd.to_datetime(date_text, format="%Y%m%d", errors="coerce")
    missing = parsed.isna()
    if missing.any():
        parsed.loc[missing] = pd.to_datetime(date_text.loc[missing], errors="coerce")

    factors["trade_date"] = parsed.dt.strftime("%Y%m%d")
    factors["adj_factor"] = pd.to_numeric(factors["adj_factor"], errors="coerce")
    factors = factors.dropna(subset=["trade_date", "adj_factor"])
    factors = factors[
        factors["adj_factor"].map(math.isfinite)
        & factors["adj_factor"].gt(0)
    ]
    if factors.empty:
        return pd.DataFrame(columns=["trade_date", "adj_factor"])

    return (
        factors.sort_values("trade_date", ascending=False)
        .drop_duplicates(subset=["trade_date"], keep="first")
        .reset_index(drop=True)
    )


def _match_adjustment_factors(
    df: pd.DataFrame,
    df_factor: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.Series]:
    """规范因子并严格映射行情日期；任何日期缺因子都明确失败。"""
    factors = _prepare_adj_factor_frame(df_factor)
    if factors.empty:
        raise AdjustmentFactorCoverageError(
            "复权因子与行情日期不匹配：因子为空或字段无效，已停止复权计算"
        )

    parsed_time = pd.to_datetime(df["time"], errors="coerce")
    invalid_time_count = int(parsed_time.isna().sum())
    if invalid_time_count:
        raise AdjustmentFactorCoverageError(
            f"复权因子与行情日期不匹配：行情中有 {invalid_time_count} 条 time 无法解析，"
            "已停止复权计算"
        )

    market_dates = parsed_time.dt.strftime("%Y%m%d")
    factor_map = factors.set_index("trade_date")["adj_factor"]
    required_dates = set(market_dates.tolist())
    missing_dates = sorted(required_dates - set(factor_map.index.tolist()))
    if missing_dates:
        preview = "、".join(missing_dates[:8])
        suffix = "..." if len(missing_dates) > 8 else ""
        raise AdjustmentFactorCoverageError(
            f"复权因子与行情日期不匹配：缺少 {len(missing_dates)} 个交易日因子"
            f"（{preview}{suffix}），已停止复权计算"
        )

    mapped = market_dates.map(factor_map)
    if mapped.isna().any():
        # 理论上集合校验后不会到达；保留防御，禁止未来类型变化造成静默填充。
        raise AdjustmentFactorCoverageError(
            "复权因子与行情日期不匹配：存在无法映射的交易日，已停止复权计算"
        )
    return factors, pd.to_numeric(mapped, errors="coerce")


class TushareImporter:
    """
    Tushare 数据导入器

    支持：
    - 日线 / 分钟线原始数据下载
    - 乘法前复权（adj_factor 方式，与 tushare/Wind 一致）
    - 可选代理
    """

    def __init__(
        self,
        token:     str,
        use_proxy: bool = False,
        proxy_url: str  = "",
        api_url:   str  = "",
    ):
        """
        Args:
            token     : tushare token（在 https://tushare.pro 注册获取）
            use_proxy : 是否使用代理
            proxy_url : 代理地址，如 http://127.0.0.1:7890
            api_url   : 自定义 API 数据接口根地址；留空使用 Tushare SDK 默认值
        """
        self.token     = token
        self.use_proxy = use_proxy
        self.proxy_url = _normalize_url_text(proxy_url)
        
        # 处理可能的误填，去掉多余的接口名并补齐协议。
        self.api_url   = _normalize_api_url(api_url)
        self._pro      = None  # 延迟初始化
        self.request_count = 0
        self.request_times = []  # 记录最近的请求时间戳
        # 部分自定义服务在权限不足时返回 HTTP 4xx。Tushare SDK 使用
        # ``if response`` 判断，会把这类响应静默变成空 DataFrame。这里缓存
        # 已确认的权限错误和合法空响应，避免对每只股票重复做协议诊断。
        self._custom_api_denied: dict[str, str] = {}
        self._custom_empty_checked: set[str] = set()

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _get_pro(self):
        """初始化并返回 tushare pro_api 实例（延迟创建，只创建一次）。"""
        if self._pro is not None:
            return self._pro

        try:
            import tushare as ts
        except ImportError as e:
            raise ImportError(f"未安装 tushare，请执行: pip install tushare  ({e})")

        _setup_proxy(self.use_proxy, self.proxy_url, self.api_url)

        self._pro = ts.pro_api(self.token)

        # tushare 1.x 把服务地址保存在 DataApi 的私有属性中。必须写到
        # 当前实例，不能修改 DataApi 类变量：类变量会让并行下载中的不同
        # importer 互相覆盖 API 地址，也会污染进程内其他 Tushare 调用。
        if self.api_url:
            setattr(self._pro, "_DataApi__http_url", self.api_url)
            logger.info(f"[TushareImporter] 使用自定义 API 地址: {self.api_url}")
        else:
            logger.info("[TushareImporter] 使用 Tushare SDK 默认 API 地址")
        return self._pro

    def _record_request(self) -> None:
        """记录一次真实 API 请求，供频率显示和限流统计使用。"""
        self.request_count += 1
        now = time.time()
        self.request_times.append(now)
        self.request_times = [value for value in self.request_times if now - value <= 60]

    def _diagnose_custom_empty(self, method_name: str, kwargs: dict) -> pd.DataFrame | None:
        """识别自定义端点把 HTTP 4xx 吞成空表的情况。

        官方端点和已经确认过的合法空响应不额外请求。自定义端点第一次出现
        空表时，按照 Tushare 通用 HTTP 协议直连根地址检查一次：权限错误会
        明确抛出；若根地址实际返回了数据，则直接采用该数据。
        """
        if not self.api_url:
            return None
        if method_name in self._custom_api_denied:
            raise TushareAPIError(self._custom_api_denied[method_name])
        if method_name in self._custom_empty_checked:
            return None

        self._record_request()
        try:
            response = requests.post(
                self.api_url,
                json={
                    "api_name": method_name,
                    "token": self.token,
                    "params": kwargs,
                    "fields": "",
                },
                headers={"Accept-Encoding": "gzip"},
                timeout=30,
            )
            payload = response.json()
        except Exception as exc:
            message = _safe_error_text(exc, self.token)
            raise TushareAPIError(
                f"{method_name} 接口返回空，且自定义端点诊断失败: "
                f"{type(exc).__name__}: {message}"
            ) from exc

        code = payload.get("code")
        if code not in (0, "0"):
            message = _safe_error_text(
                str(payload.get("msg") or f"服务端返回错误 code={code}").strip(),
                self.token,
            )
            self._custom_api_denied[method_name] = message
            raise TushareAPIError(message)

        self._custom_empty_checked.add(method_name)
        data = payload.get("data") or {}
        fields = data.get("fields") or []
        items = data.get("items") or []
        if fields and items:
            logger.warning(
                "tushare [%s] SDK 路径返回空，但根地址返回 %d 条，已自动采用根地址结果",
                method_name,
                len(items),
            )
            return pd.DataFrame(items, columns=fields)
        return None

    def _call(self, method_name: str, **kwargs) -> pd.DataFrame:
        """统一调用 pro 接口。

        接口正常返回空数据时仍返回空 DataFrame；网络、权限、限流和服务端异常
        则抛出 TushareAPIError，让上层重试并在任务汇总中如实标记失败。
        """
        attempts, base_delay = _api_retry_settings()
        last_error = None
        for attempt in range(1, attempts + 1):
            if method_name in self._custom_api_denied:
                raise TushareAPIError(self._custom_api_denied[method_name])
            self._record_request()

            try:
                pro = self._get_pro()
                func = getattr(pro, method_name)
                df = func(**kwargs)
                # Tushare SDK/代理端点在空结果时可能返回 None、[]，部分镜像也
                # 可能返回 records/dict；统一成 DataFrame，避免上层访问 .empty
                # 时把正常空结果误判成线程异常。
                if df is None:
                    df = pd.DataFrame()
                elif isinstance(df, (list, tuple)):
                    df = pd.DataFrame(df)
                elif isinstance(df, dict):
                    try:
                        df = pd.DataFrame(df)
                    except ValueError:
                        df = pd.DataFrame([df])
                if not isinstance(df, pd.DataFrame):
                    raise TushareAPIError(
                        f"Tushare {method_name} 返回了不支持的数据类型: "
                        f"{type(df).__name__}"
                    )
                if df.empty:
                    direct = self._diagnose_custom_empty(method_name, kwargs)
                    if direct is not None:
                        return direct
                return df
            except Exception as e:
                last_error = e
                retryable = _is_retryable_api_error(e)
                safe_error = _safe_error_text(e, self.token)
                if not retryable or attempt >= attempts:
                    err_str = safe_error
                    if "权限" in err_str or "抱歉，您没有访问该接口的权限" in err_str:
                        logger.warning(
                            f"tushare [{method_name}] 调用失败: {safe_error} (您的账号积分可能不足)"
                        )
                    else:
                        logger.warning(f"tushare [{method_name}] 调用失败: {safe_error}")
                    break

                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"tushare [{method_name}] 临时失败，第 {attempt}/{attempts} 次: {safe_error}; "
                    f"{delay:.1f} 秒后重试"
                )
                # 镜像站 SSL/连接异常后重建 SDK 实例，避免复用异常连接状态。
                self._pro = None
                if delay > 0:
                    time.sleep(delay)

        safe_last_error = _safe_error_text(last_error, self.token)
        raise TushareAPIError(
            f"Tushare {method_name} 接口调用失败（已尝试 {attempt} 次）: {safe_last_error}"
        ) from None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        """
        验证 token 是否有效。每次调用都重新建立连接（确保测试最新配置）。

        Returns:
            (ok, message)
        """
        try:
            from tushare_config import validate_tushare_token

            token_ok, token_message = validate_tushare_token(self.token)
            if not token_ok:
                return False, f"连接失败: {token_message}"

            self._pro = None  # 强制重新初始化，确保使用最新 api_url
            checks = (
                ("daily", {"ts_code": "000001.SZ", "start_date": "20240102", "end_date": "20240102"}),
                ("index_daily", {"ts_code": "000300.SH", "start_date": "20240102", "end_date": "20240102"}),
            )
            for api_name, params in checks:
                frame = self._call(api_name, **params)
                if not isinstance(frame, pd.DataFrame) or frame.empty:
                    return False, f"连接失败: {api_name} 接口返回空数据"

            endpoint = self.api_url or "Tushare SDK 默认地址"
            return True, f"连接成功，daily/index_daily 均可用（{endpoint}）"
            
        except Exception as e:
            err_msg = _safe_error_text(e, self.token)
            lowered = err_msg.lower()
            if "token不对" in err_msg or "token无效" in err_msg or "invalid token" in lowered:
                return False, f"连接失败: 您的 Token 不正确，请确认。({err_msg})"
            if "权限" in err_msg or "积分" in err_msg:
                return False, f"连接失败: 必需接口无权限或积分不足: {err_msg}"
            return False, f"连接失败: {err_msg}"

    def get_latest_trade_date(self, ts_code: str = "000001.SZ") -> str:
        """
        查询最近30天内最新实际交易日（tushare 不接受未来日期作为 end_date）。

        Returns:
            'YYYYMMDD' 格式字符串
        """
        today     = datetime.date.today().strftime("%Y%m%d")
        month_ago = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y%m%d")
        
        method = "index_daily" if is_tushare_index_code(ts_code) else "daily"
        if method == "daily" and is_listed_fund_code(ts_code):
            method = "fund_daily"
                
        df = self._call(method, ts_code=ts_code, start_date=month_ago, end_date=today)
        if not df.empty and "trade_date" in df.columns:
            return df.sort_values("trade_date", ascending=False)["trade_date"].iloc[0]
        return today

    def download_adj_factor(self, ts_code: str) -> pd.DataFrame:
        """
        获取全量复权因子（不限日期，确保能取到最新一条）。

        股票 ``adj_factor`` 官方支持按单只股票一次提取全部历史，直接调用一次。
        ETF ``fund_adj`` 官方单次最多 2000 行，使用 offset/limit 循环分页；若服务
        忽略 offset 并重复返回同一页，则明确失败，不能把截断数据冒充全量。

        Returns:
            DataFrame 含 trade_date, adj_factor，按日期降序且日期唯一
        """
        method = "adj_factor"
        if is_listed_fund_code(ts_code):
            method = "fund_adj"

        if method != "fund_adj":
            return _prepare_adj_factor_frame(self._call(method, ts_code=ts_code))

        pages = []
        seen_dates: set[str] = set()
        offset = 0
        for _page_number in range(1, _FUND_ADJ_MAX_PAGES + 1):
            page = self._call(
                method,
                ts_code=ts_code,
                offset=offset,
                limit=_FUND_ADJ_PAGE_SIZE,
            )
            raw_count = len(page.index)
            if raw_count == 0:
                break

            normalized = _prepare_adj_factor_frame(page)
            if normalized.empty:
                raise TushareAPIError(
                    f"Tushare fund_adj 第 {_page_number} 页返回 {raw_count} 条，"
                    "但没有有效 trade_date/adj_factor，无法确认全量复权因子"
                )

            page_dates = set(normalized["trade_date"].tolist())
            new_dates = page_dates - seen_dates
            if not new_dates:
                raise TushareAPIError(
                    "Tushare fund_adj 分页未前进：服务端可能忽略 offset，"
                    "已停止导入，避免把截断复权因子误当成全量"
                )
            seen_dates.update(new_dates)
            pages.append(normalized)

            if raw_count < _FUND_ADJ_PAGE_SIZE:
                break
            offset += _FUND_ADJ_PAGE_SIZE
        else:
            raise TushareAPIError(
                f"Tushare fund_adj 分页超过安全上限 {_FUND_ADJ_MAX_PAGES} 页，"
                "无法确认复权因子已完整下载"
            )

        if not pages:
            return pd.DataFrame(columns=["trade_date", "adj_factor"])
        return _prepare_adj_factor_frame(pd.concat(pages, ignore_index=True))

    # ------------------------------------------------------------------
    # 分批辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _batch_by_trade_days(
        d_start: "datetime.date",
        d_end:   "datetime.date",
        target_trade_days: int,
    ):
        """
        按"目标交易日数量"将 [d_start, d_end] 切成若干子区间，以自然日估算。

        A股每年约 245 个交易日（扣除周末+约15个法定假日），
        转换系数：1 交易日 ≈ 365/245 ≈ 1.49 自然日。
        保守取 1.55，确保每批自然日数能覆盖目标交易日数且留有余量。

        每批区间 [batch_start, batch_end] 前后不重叠：
          batch_start  = 上一批 batch_end + 1天
          batch_end    = min(batch_start + delta - 1天, d_end)
        这样即使 API 返回了 batch_end 当天的数据，下一批 start 是 batch_end+1，
        不会产生重复记录。
        """
        # 1 交易日 → 自然日，保守系数 1.55
        delta_days = max(1, int(target_trade_days * 1.55))
        delta      = datetime.timedelta(days=delta_days)
        one_day    = datetime.timedelta(days=1)

        batch_start = d_start
        while batch_start <= d_end:
            batch_end = min(batch_start + delta - one_day, d_end)
            yield batch_start, batch_end
            batch_start = batch_end + one_day

    @staticmethod
    def _batch_by_minutes(
        t_start: "datetime.datetime",
        t_end:   "datetime.datetime",
        target_trade_days: int,
    ):
        """
        按"目标交易日数量"将 [t_start, t_end] 切成子区间（分钟级）。

        以 t_start 为基准，每批整倍 delta 对齐，不累积秒数漂移：
          batch N : [t_start + N*delta,  t_start + (N+1)*delta]
        首尾重叠的那一秒由 _merge_chunks 去重处理，不会产生数据丢失或重复。
        """
        delta_days = max(1, int(target_trade_days * 1.55))
        delta      = datetime.timedelta(days=delta_days)

        n = 0
        while True:
            batch_start = t_start + n * delta
            if batch_start > t_end:
                break
            batch_end = min(t_start + (n + 1) * delta, t_end)
            yield batch_start, batch_end
            if batch_end >= t_end:
                break
            n += 1

    # ------------------------------------------------------------------
    # 下载日线
    # ------------------------------------------------------------------

    def download_daily(
        self,
        ts_code:    str,
        start_date: str,
        end_date:   str,
        adj:        str = "none",
    ) -> pd.DataFrame:
        """
        下载日线数据，自动分批（tushare 单次上限约 6000 条）。

        分批策略：以 4000 个交易日为目标，换算成自然日（× 1.55）≈ 6200 天 ≈ 17 年/批，
        远低于 6000 条上限，留有充足余量。

        Args:
            ts_code    : 如 '000001.SZ'
            start_date : 'YYYYMMDD'
            end_date   : 'YYYYMMDD'
            adj        : 'none'=不复权

        Returns:
            DataFrame 含 time(datetime64), open, high, low, close, volume, amount, preClose
            按 time 升序，重复行已去除
        """
        TARGET_TRADE_DAYS = 4000  # 安全上限，4000 交易日 ≈ 16.3 年

        method = "daily"
        if is_listed_fund_code(ts_code):
            method = "fund_daily"

        try:
            d_start = datetime.datetime.strptime(start_date, "%Y%m%d").date()
            d_end   = datetime.datetime.strptime(end_date,   "%Y%m%d").date()
        except ValueError:
            logger.warning(f"[download_daily] 日期格式错误: {start_date} ~ {end_date}，单次请求")
            return self._normalize_daily(
                self._call(method, ts_code=ts_code, start_date=start_date, end_date=end_date)
            )

        if d_start > d_end:
            logger.warning(f"[download_daily] {ts_code}: start_date ({start_date}) > end_date ({end_date})，返回空")
            return pd.DataFrame()

        chunks = []
        for b_start, b_end in self._batch_by_trade_days(d_start, d_end, TARGET_TRADE_DAYS):
            s = b_start.strftime("%Y%m%d")
            e = b_end.strftime("%Y%m%d")
            logger.debug(f"[download_daily] {ts_code} {s} ~ {e}")

            df_chunk = self._call(method, ts_code=ts_code, start_date=s, end_date=e)
            if df_chunk is not None and not df_chunk.empty:
                chunks.append(self._normalize_daily(df_chunk))

        return self._merge_chunks(chunks, time_col="time")

    def download_index_daily(
        self,
        ts_code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """
        下载指数日线数据（如 000300.SH），自动分批并标准化为 DuckDB 格式。

        Returns:
            DataFrame 含 time(datetime64), open, high, low, close, volume, amount, preClose
            按 time 升序，重复行已去除
        """
        TARGET_TRADE_DAYS = 4000

        try:
            d_start = datetime.datetime.strptime(start_date, "%Y%m%d").date()
            d_end   = datetime.datetime.strptime(end_date,   "%Y%m%d").date()
        except ValueError:
            logger.warning(f"[download_index_daily] 日期格式错误: {start_date} ~ {end_date}，单次请求")
            return self._normalize_daily(
                self._call("index_daily", ts_code=ts_code, start_date=start_date, end_date=end_date)
            )

        if d_start > d_end:
            logger.warning(f"[download_index_daily] {ts_code}: start_date ({start_date}) > end_date ({end_date})，返回空")
            return pd.DataFrame()

        chunks = []
        for b_start, b_end in self._batch_by_trade_days(d_start, d_end, TARGET_TRADE_DAYS):
            s = b_start.strftime("%Y%m%d")
            e = b_end.strftime("%Y%m%d")
            logger.debug(f"[download_index_daily] {ts_code} {s} ~ {e}")

            df_chunk = self._call("index_daily", ts_code=ts_code, start_date=s, end_date=e)
            if df_chunk is not None and not df_chunk.empty:
                chunks.append(self._normalize_daily(df_chunk))

        return self._merge_chunks(chunks, time_col="time")

    # ------------------------------------------------------------------
    # 下载分钟线
    # ------------------------------------------------------------------

    def download_minutes(
        self,
        ts_code:    str,
        freq:       str,
        start_date: str,
        end_date:   str,
    ) -> pd.DataFrame:
        """
        下载分钟线原始数据，自动分批（tushare 单次上限 8000 条）。

        分批策略（以交易日为单位计算，再换算自然日）：
          A股每交易日 K 线根数见模块常量 _BARS_PER_DAY（1min=241, 5min=49, ...）
          目标交易日数 = floor(8000 / bars_per_day / 1.2 安全系数)
          换算自然日    = 目标交易日 × 1.55

        Args:
            ts_code    : 如 '000001.SZ'
            freq       : '1min' / '5min' / '15min' / '30min' / '60min'
            start_date : 'YYYY-MM-DD HH:MM:SS'
            end_date   : 'YYYY-MM-DD HH:MM:SS'

        Returns:
            DataFrame 含 time(datetime64), open, high, low, close, volume, amount
            按 time 升序，重复行已去除
        """
        bars_per_day = _BARS_PER_DAY.get(freq, 241)
        # 8000 条上限，× 1.2 安全余量，向下取整得目标交易日数
        target_trade_days = max(1, int(8000 / bars_per_day / 1.2))

        # tushare 对股票和 ETF 分钟线都使用 stk_mins 接口
        method = "stk_mins"

        try:
            t_start = datetime.datetime.strptime(start_date[:19], "%Y-%m-%d %H:%M:%S")
            t_end   = datetime.datetime.strptime(end_date[:19],   "%Y-%m-%d %H:%M:%S")
        except ValueError:
            logger.warning(f"[download_minutes] 日期格式错误: {start_date} ~ {end_date}，单次请求")
            # tushare 对股票和 ETF 分钟线都使用 stk_mins 接口
            method = "stk_mins"
            return self._normalize_minutes(
                self._call(method, ts_code=ts_code, freq=freq,
                           start_date=start_date, end_date=end_date)
            )

        if t_start > t_end:
            logger.warning(f"[download_minutes] {ts_code}: start > end，返回空")
            return pd.DataFrame()

        chunks = []
        # tushare 对股票和 ETF 分钟线都使用 stk_mins 接口
        method = "stk_mins"
                
        method = "stk_mins"
        for b_start, b_end in self._batch_by_minutes(t_start, t_end, target_trade_days):
            s = b_start.strftime("%Y-%m-%d %H:%M:%S")
            e = b_end.strftime("%Y-%m-%d %H:%M:%S")
            logger.debug(f"[download_minutes] {ts_code} {freq} {s} ~ {e}")
            df_chunk = self._call(method, ts_code=ts_code, freq=freq,
                                  start_date=s, end_date=e)
            if df_chunk is not None and not df_chunk.empty:
                chunks.append(self._normalize_minutes(df_chunk))

        return self._merge_chunks(chunks, time_col="time")

    @staticmethod
    def _merge_chunks(chunks: list, time_col: str = "time") -> pd.DataFrame:
        """合并多批 DataFrame，按 time_col 去重并升序排序。"""
        if not chunks:
            return pd.DataFrame()
        if len(chunks) == 1:
            # 单批无重复，_normalize_* 已保证升序，直接返回
            return chunks[0]
        df = pd.concat(chunks, ignore_index=True)
        if time_col in df.columns:
            df = df.drop_duplicates(subset=[time_col])
            df = df.sort_values(time_col).reset_index(drop=True)
        return df

    @staticmethod
    def _prepare_adj_factors(df_factor: pd.DataFrame) -> pd.DataFrame:
        return _prepare_adj_factor_frame(df_factor)

    @staticmethod
    def _apply_both_adj(
        df: pd.DataFrame,
        df_factor: pd.DataFrame,
    ) -> "tuple[pd.DataFrame, pd.DataFrame]":
        """
        同时计算前复权和后复权，共享全部预处理步骤（排序、factor_map、strftime、map）。
        供 worker 在前/后复权都需要时调用，避免 _apply_adj 被调用两次的重复开销。

        Returns:
            (df_front, df_back)；行情为空或价格字段无效时返回空 DataFrame

        Raises:
            AdjustmentFactorCoverageError: 任一行情交易日缺少有效复权因子
        """
        if df is None or df.empty or "time" not in df.columns:
            return pd.DataFrame(), pd.DataFrame()
        df_factor, matched_factors = _match_adjustment_factors(df, df_factor)

        try:
            latest_factor   = float(df_factor["adj_factor"].iloc[0])
            earliest_factor = float(df_factor["adj_factor"].iloc[-1])
        except (TypeError, ValueError):
            return pd.DataFrame(), pd.DataFrame()
        if (not latest_factor   or latest_factor   != latest_factor or
                not earliest_factor or earliest_factor != earliest_factor):
            return pd.DataFrame(), pd.DataFrame()

        # 只 copy 一次，共享 _d / _adj 列
        base = df.copy()
        base["_adj"] = matched_factors.to_numpy()

        price_cols = [c for c in ["open", "high", "low", "close"] if c in base.columns]
        if not price_cols:
            return pd.DataFrame(), pd.DataFrame()
        for col in price_cols:
            price = pd.to_numeric(base[col], errors="coerce")
            base[f"{col}_front"] = (price * base["_adj"] / latest_factor).round(3)
            base[f"{col}_back"]  = (price * base["_adj"] / earliest_factor).round(3)
            if base[f"{col}_front"].isna().all() or base[f"{col}_back"].isna().all():
                return pd.DataFrame(), pd.DataFrame()
        base.drop(columns=["_adj"], inplace=True)

        # 拆分成两个独立 DataFrame（drop 是 O(列数)，不复制行数据）
        back_cols  = [f"{c}_back"  for c in price_cols]
        front_cols = [f"{c}_front" for c in price_cols]
        df_front = base.drop(columns=back_cols)
        df_back  = base.drop(columns=front_cols)
        return df_front, df_back

    @staticmethod
    def _apply_adj(df: pd.DataFrame, df_factor: pd.DataFrame, mode: str = "qfq") -> pd.DataFrame:
        """
        基于已有 df 和 df_factor 计算复权价（不重复下载）。

        供 worker 复用：下载一次 raw + 一次 adj_factor 后，可分别计算前/后复权。

        Args:
            df       : 含 time/open/high/low/close 的原始行情（会被 copy，不修改入参）
            df_factor: 含 trade_date/adj_factor 的复权因子
            mode     : 'qfq'=前复权  'hfq'=后复权

        Returns:
            新 DataFrame，含原始列 + open_front/high_front/... 或 open_back/...

        Raises:
            AdjustmentFactorCoverageError: 任一行情交易日缺少有效复权因子
        """
        if df is None or df.empty or "time" not in df.columns:
            return pd.DataFrame()
        df_factor, matched_factors = _match_adjustment_factors(df, df_factor)

        df = df.copy()

        base_factor = df_factor["adj_factor"].iloc[0] if mode == "qfq" else df_factor["adj_factor"].iloc[-1]
        try:
            base_factor = float(base_factor)
        except (TypeError, ValueError):
            base_factor = 0.0
        if not base_factor or base_factor != base_factor:
            return pd.DataFrame()

        df["_adj"] = matched_factors.to_numpy()

        suffix = "front" if mode == "qfq" else "back"
        price_cols = [col for col in ["open", "high", "low", "close"] if col in df.columns]
        if not price_cols:
            return pd.DataFrame()
        for col in price_cols:
            price = pd.to_numeric(df[col], errors="coerce")
            adjusted_col = f"{col}_{suffix}"
            df[adjusted_col] = (price * df["_adj"] / base_factor).round(3)
            if df[adjusted_col].isna().all():
                return pd.DataFrame()
        df.drop(columns=["_adj"], inplace=True)
        return df

    def mul_adj(
        self,
        ts_code:    str,
        start_date: str,
        end_date:   str,
        freq:       str  = "D",
        mode:       str  = "qfq",
    ) -> pd.DataFrame:
        """
        乘法复权（adj_factor 方式），同时支持前复权和后复权。

        公式：
          前复权(qfq)：复权价 = 原始价 × (当日 adj_factor / 最新 adj_factor)
            → 最近日期价格 ≈ 原始价，历史价格向下调整
          后复权(hfq)：复权价 = 原始价 × (当日 adj_factor / 最早 adj_factor)
            → 最早日期价格 ≈ 原始价，近期价格向上调整

        注意：adj_factor 接口需要约 2000 积分权限；结果保留 3 位小数。

        Args:
            ts_code    : 如 '000001.SZ'
            start_date : 日线传 'YYYYMMDD'，分钟线传 'YYYY-MM-DD HH:MM:SS'
            end_date   : 日线传 'YYYYMMDD'，分钟线传 'YYYY-MM-DD HH:MM:SS'
            freq       : 'D' / '1min' / '5min' / '15min' / '30min' / '60min'
            mode       : 'qfq'=前复权  'hfq'=后复权

        Returns:
            原始 DataFrame + 新增 open_front/high_front/low_front/close_front（前复权）
                               或   open_back/high_back/low_back/close_back（后复权）

        Raises:
            AdjustmentFactorCoverageError: 任一行情交易日缺少有效复权因子；调用方应
                保留已取得的原始行情，并把复权列标记为待补
        """
        # 1. 获取原始行情
        if freq == "D":
            df = self.download_daily(ts_code, start_date, end_date, adj="none")
        else:
            df = self.download_minutes(ts_code, freq, start_date, end_date)

        if df.empty:
            return df

        if "time" not in df.columns:
            logger.warning(f"[mul_adj] {ts_code} 下载数据缺少 time 列，无法计算复权")
            return df

        # 2. 获取全量复权因子
        df_factor = self.download_adj_factor(ts_code)
        required_cols = {"adj_factor", "trade_date"}
        if df_factor.empty or not required_cols.issubset(df_factor.columns):
            logger.warning(
                f"[mul_adj] {ts_code} adj_factor 接口无数据或缺少必要列"
                "（需要约 2000 积分，且返回需含 trade_date/adj_factor）"
            )
            return df

        # 3. 计算复权价（复用 _apply_adj，避免重复代码）
        return self._apply_adj(df, df_factor, mode=mode)

    def mul_qfq(
        self,
        ts_code:    str,
        start_date: str,
        end_date:   str,
        freq:       str = "D",
    ) -> pd.DataFrame:
        """乘法前复权（adj_factor 方式）的快捷调用，等价于 mul_adj(..., mode='qfq')。"""
        return self.mul_adj(ts_code, start_date, end_date, freq=freq, mode="qfq")

    def mul_hfq(
        self,
        ts_code:    str,
        start_date: str,
        end_date:   str,
        freq:       str = "D",
    ) -> pd.DataFrame:
        """乘法后复权（adj_factor 方式）的快捷调用，等价于 mul_adj(..., mode='hfq')。"""
        return self.mul_adj(ts_code, start_date, end_date, freq=freq, mode="hfq")

    # ------------------------------------------------------------------
    # 数据标准化（统一输出 time/open/high/low/close/volume/amount/preClose）
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_daily(df: pd.DataFrame) -> pd.DataFrame:
        """将 tushare daily 返回格式标准化为 DuckDB 存储格式。"""
        if df is None or df.empty:
            return pd.DataFrame()

        from duckdb_storage.units import normalize_kline_units, UNIT_CONTRACT_VERSION
        if df.attrs.get("khquant_unit_contract") == UNIT_CONTRACT_VERSION:
            return normalize_kline_units(df, source_volume_unit="lots")

        df = df.copy()

        # trade_date → time（datetime64，日线取 09:30:00 以与 miniQMT 一致）
        if "trade_date" in df.columns:
            df["time"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
            df["time"] = df["time"] + _DAILY_TIME_OFFSET

        # pre_close → preClose
        if "pre_close" in df.columns and "preClose" not in df.columns:
            df["preClose"] = df["pre_close"]

        # vol → volume：日线原值已经是手，与历史 QMT/DuckDB 保持一致。
        if "vol" in df.columns and "volume" not in df.columns:
            df["volume"] = pd.to_numeric(df["vol"], errors="coerce")

        # 确保数值列
        for col in ["open", "high", "low", "close", "volume", "preClose"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # amount：tushare daily 单位为千元，转换为元（与分钟线及 miniQMT 保持一致）
        if "amount" in df.columns:
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce") * 1000

        # 按时间升序（tushare 返回降序）
        if "time" not in df.columns:
            logger.warning("[_normalize_daily] 返回数据缺少 trade_date 列，无法生成 time")
            return pd.DataFrame()
        df = df.dropna(subset=["time"])
        if df.empty:
            return pd.DataFrame()
        df = df.sort_values("time").reset_index(drop=True)
        from duckdb_storage.units import normalize_kline_units
        return normalize_kline_units(df, source_volume_unit="lots")

    @staticmethod
    def _normalize_minutes(df: pd.DataFrame) -> pd.DataFrame:
        """将 tushare stk_mins 返回格式标准化为 DuckDB 存储格式。"""
        if df is None or df.empty:
            return pd.DataFrame()

        df = df.copy()

        # trade_time → time
        if "trade_time" in df.columns and "time" not in df.columns:
            df["time"] = pd.to_datetime(df["trade_time"], errors="coerce")

        if "time" not in df.columns:
            logger.warning("[_normalize_minutes] 返回数据缺少 trade_time 列，无法生成 time")
            return pd.DataFrame()

        # 分钟线原值为股/份，统一在本入口换算为手。
        if "vol" in df.columns and "volume" not in df.columns:
            df["volume"] = pd.to_numeric(df["vol"], errors="coerce")

        for col in ["open", "high", "low", "close", "amount", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["time"])
        df = df.sort_values("time").reset_index(drop=True)
        from duckdb_storage.units import normalize_kline_units
        return normalize_kline_units(df, source_volume_unit="shares")
