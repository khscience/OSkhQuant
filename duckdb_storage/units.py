"""DuckDB K-line contract: volume in lots, amount in CNY.

Keep the original QMT storage convention. A-stock/ETF/stock-index adapters
convert 100 shares/units to one lot before persistence; other asset classes
must supply their own documented contract size. Tick pvolume is a separate
source field and must never be converted as if it were K-line volume.
"""
from __future__ import annotations

import pandas as pd

VOLUME_UNIT = "lots"
AMOUNT_UNIT = "CNY"
UNIT_CONTRACT_VERSION = 1


class VolumeUnitError(ValueError):
    code = "VOLUME_UNIT_CONFLICT"
    retryable = False


def normalize_kline_units(frame, *, source_volume_unit, amount_multiplier=1):
    """Normalize once, preserving fractional lots and missing values."""
    if source_volume_unit not in ("shares", "lots"):
        raise VolumeUnitError(f"未声明支持的成交量单位: {source_volume_unit}")
    result = frame.copy()
    if result.attrs.get("khquant_unit_contract") == UNIT_CONTRACT_VERSION:
        if result.attrs.get("volume_unit") != VOLUME_UNIT or result.attrs.get("amount_unit") != AMOUNT_UNIT:
            raise VolumeUnitError("标准化标记与成交量/成交额单位不一致")
        return result
    if "volume" in result:
        result["volume"] = pd.to_numeric(result["volume"], errors="raise")
        if source_volume_unit == "shares":
            result["volume"] = result["volume"] / 100.0
    if "amount" in result:
        result["amount"] = pd.to_numeric(result["amount"], errors="raise") * amount_multiplier
    result.attrs.update(volume_unit=VOLUME_UNIT, amount_unit=AMOUNT_UNIT,
                        khquant_unit_contract=UNIT_CONTRACT_VERSION)
    return result


def ensure_kline_storage_units(conn, table, frame, stock_code):
    """Guard legacy tables, widen integer lots and record units atomically.

Called inside the data-write transaction, before any row is changed. Unknown
legacy equity units require positive evidence; never rescale existing rows.
"""
    if "volume" not in frame or frame["volume"].isna().all():
        return
    declared = frame.attrs.get("volume_unit", VOLUME_UNIT)
    if declared != VOLUME_UNIT:
        raise VolumeUnitError(f"DuckDB 成交量统一为手，输入声明为 {declared}；请先在数据源入口换算")
    if frame.attrs.get("amount_unit", AMOUNT_UNIT) != AMOUNT_UNIT:
        raise VolumeUnitError("DuckDB 成交额统一为元；请先在数据源入口换算")
    key = f"{table}.volume_unit"
    conn.execute("CREATE TABLE IF NOT EXISTS stock_info (key VARCHAR PRIMARY KEY, value VARCHAR, update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    marker = conn.execute("SELECT value FROM stock_info WHERE key=?", [key]).fetchone()
    amount_marker = conn.execute("SELECT value FROM stock_info WHERE key=?", [f"{table}.amount_unit"]).fetchone()
    if amount_marker and amount_marker[0] != AMOUNT_UNIT:
        raise VolumeUnitError(f"旧表 {table} 成交额标记为 {amount_marker[0]}，目标为元，拒绝混写")
    if marker and marker[0] != VOLUME_UNIT:
        raise VolumeUnitError(f"旧表 {table} 标记为 {marker[0]}，目标单位为手；请先核验并迁移旧库，拒绝混写")
    if not marker:
        # Equity VWAP must be between the raw low and high. A share-denominated
        # legacy table instead matches amount / volume without the lot factor.
        sample = conn.execute(f"SELECT time, low, high, volume, amount FROM {table} WHERE volume > 0 ORDER BY time DESC LIMIT 32").fetchdf()
        if not sample.empty:
            code = str(stock_code).upper()
            index = (code.endswith(".SH") and code.startswith("000")) or (code.endswith(".SZ") and code.startswith("399"))
            equity = ((code.endswith(".SH") and code.startswith(("5", "6")))
                      or (code.endswith(".SZ") and code.startswith(("0", "30", "15", "16", "18")))
                      or code.endswith(".BJ")) and not index
            compatible = False
            if equity:
                # Audit the whole unmarked table once: looking only at the
                # newest rows could miss an earlier share-denominated import.
                shares, lots = conn.execute(f"""SELECT
                    count(*) FILTER (WHERE amount / volume BETWEEN low * .98 AND high * 1.02),
                    count(*) FILTER (WHERE amount / volume / 100 BETWEEN low * .98 AND high * 1.02)
                    FROM {table} WHERE volume > 0 AND amount > 0 AND low > 0 AND high >= low""").fetchone()
                if shares:
                    raise VolumeUnitError(f"旧表 {table} 存在按股保存的成交量证据；目标为手，拒绝自动缩放或混写，请先核验旧库")
                compatible = bool(lots)
            # Exact timestamps also work for index data and minute sources
            # without amount. Integer QMT lots may truncate a fractional lot.
            incoming = frame[["time", "volume"]].copy()
            pairs = sample.merge(incoming, on="time", suffixes=("_old", "_new"))
            pairs = pairs[(pairs.volume_old > 0) & (pairs.volume_new > 0)]
            if not pairs.empty:
                ratio = pairs.volume_new / pairs.volume_old
                if ratio.between(95, 105).any() or ratio.between(.0095, .0105).any():
                    raise VolumeUnitError(f"旧表 {table} 与待写入的手单位成交量相差约100倍，拒绝混写")
                compatible |= bool(((pairs.volume_new - pairs.volume_old).abs() <= pairs.volume_old * .02 + 1).all())
            if (equity or index) and not compatible:
                raise VolumeUnitError(f"旧表 {table} 缺少可确认成交量单位的证据；请先核验，不能直接追加")
            # Legacy non-equity QMT tables keep their native lot/contract unit;
            # a stock-specific 100 multiplier must not be guessed for them.
    data_type = conn.execute("SELECT data_type FROM information_schema.columns WHERE table_name=? AND column_name='volume'", [table]).fetchone()
    if data_type and data_type[0] != "DOUBLE":
        conn.execute(f"ALTER TABLE {table} ALTER COLUMN volume TYPE DOUBLE")
    # 上面的声明、冲突和类型检查每次都执行；已正确标记的表无需反复改写
    # 相同元数据。否则即便行情完全没变化，关闭连接也会触发持久化写入。
    if not marker:
        conn.execute("INSERT INTO stock_info (key, value, update_time) VALUES (?, ?, CURRENT_TIMESTAMP)", [key, VOLUME_UNIT])
    if not amount_marker:
        conn.execute("INSERT INTO stock_info (key, value, update_time) VALUES (?, ?, CURRENT_TIMESTAMP)", [f"{table}.amount_unit", AMOUNT_UNIT])
