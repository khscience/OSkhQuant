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
    """Validate incoming units and record legacy compatibility atomically.

Called inside the data-write transaction, before any row is changed. Legacy
tables accept incoming values without inferring or changing their old units;
never rescale existing rows or mark uncertain history as confirmed lots.
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
    unverified_key = f"{table}.legacy_units_unverified"
    unverified_marker = conn.execute(
        "SELECT value FROM stock_info WHERE key=?", [unverified_key]
    ).fetchone()
    # 用户选择旧库直接续写：不再通过量额、同时间点比值或旧标记阻断。
    # 保留旧标记作为历史证据；未核验标记优先，不能宣称整表已统一。
    legacy_units_unverified = bool(unverified_marker)
    legacy_units_unverified |= bool(marker and marker[0] != VOLUME_UNIT)
    legacy_units_unverified |= bool(amount_marker and amount_marker[0] != AMOUNT_UNIT)
    if not marker or not amount_marker:
        legacy_units_unverified |= conn.execute(
            f"SELECT 1 FROM {table} LIMIT 1"
        ).fetchone() is not None
    data_type = conn.execute("SELECT data_type FROM information_schema.columns WHERE table_name=? AND column_name='volume'", [table]).fetchone()
    if data_type and data_type[0] != "DOUBLE":
        conn.execute(f"ALTER TABLE {table} ALTER COLUMN volume TYPE DOUBLE")
    if legacy_units_unverified:
        if not unverified_marker:
            conn.execute(
                "INSERT INTO stock_info (key, value, update_time) VALUES (?, ?, CURRENT_TIMESTAMP)",
                [unverified_key, "1"],
            )
        return
    # 输入声明和类型检查每次都执行；已正确标记的表无需反复改写
    # 相同元数据。否则即便行情完全没变化，关闭连接也会触发持久化写入。
    if not marker:
        conn.execute("INSERT INTO stock_info (key, value, update_time) VALUES (?, ?, CURRENT_TIMESTAMP)", [key, VOLUME_UNIT])
    if not amount_marker:
        conn.execute("INSERT INTO stock_info (key, value, update_time) VALUES (?, ?, CURRENT_TIMESTAMP)", [f"{table}.amount_unit", AMOUNT_UNIT])
