# coding: utf-8
"""Optional Parquet cache packs for framework raw backtest loads.

This module is deliberately opt-in. It never replaces the normal DuckDB source
tree, and callers must explicitly enable read-only or build-then-read behavior
through performance config.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import shutil
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from performance_config import normalize_performance_config
from .dividend_adjustment import select_dividend_fields


MANIFEST_VERSION = 4
PACK_FORMAT = "khquant_framework_raw_v1"
_FRAMEWORK_RAW_PREFIX = "__kh_raw_"
_PRICE_FIELDS = ("open", "high", "low", "close")
_ADJUSTED_DIVIDEND_TYPES = ("front", "back", "front_ratio", "back_ratio")


def default_pack_root(repo_root: Optional[Path] = None) -> Path:
    if repo_root is None and sys.platform.startswith("linux"):
        cache_home = os.environ.get("XDG_CACHE_HOME")
        base = Path(cache_home).expanduser() if cache_home else Path.home() / ".cache"
        return base / "khquant" / "parquet_cache_pack"
    if repo_root is None and getattr(sys, "frozen", False):
        # 打包版安装目录对普通用户不可写，缓存放到 %LOCALAPPDATA%\\KhQuantOS\\temp
        from kh_app_identity import local_appdata_dir

        return Path(local_appdata_dir("temp", "parquet_cache_pack"))
    root = repo_root or Path(__file__).resolve().parents[1]
    return Path(root) / "temp" / "parquet_cache_pack"


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _read_manifest(path: Path) -> Optional[dict]:
    try:
        return json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def _pack_info(path: Path) -> dict:
    manifest = _read_manifest(path)
    spec = manifest.get("spec", {}) if isinstance(manifest, dict) else {}
    parquet_files = list(path.glob("batch_*.parquet"))
    try:
        manifest_version = int(spec.get("manifest_version", 0) or 0)
    except (TypeError, ValueError):
        manifest_version = 0
    valid = bool(
        isinstance(manifest, dict)
        and spec.get("format") == PACK_FORMAT
        and manifest_version == MANIFEST_VERSION
    )
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return {
        "path": str(path),
        "name": path.name,
        "valid": valid,
        "status": "valid" if valid else ("invalid" if manifest is not None else "no_manifest"),
        "bytes": _dir_size(path),
        "rows": int(manifest.get("rows", 0) or 0) if isinstance(manifest, dict) else 0,
        "files": int(manifest.get("files", len(parquet_files)) or 0) if isinstance(manifest, dict) else len(parquet_files),
        "compression": manifest.get("compression", "") if isinstance(manifest, dict) else "",
        "created_at": manifest.get("created_at", "") if isinstance(manifest, dict) else "",
        "mtime": mtime,
        "format": spec.get("format", ""),
        "manifest_version": spec.get("manifest_version"),
        "period": spec.get("period", ""),
        "start_time": spec.get("start_time", ""),
        "end_time": spec.get("end_time", ""),
        "stock_count": int(spec.get("stock_count", 0) or 0),
        "fields": list(spec.get("fields", []) or []),
        "dividend_type": spec.get("dividend_type", "none"),
        "data_root": spec.get("data_root", ""),
        "fingerprint": _fingerprint(spec) if spec else "",
    }


def inspect_cache_pack_root(pack_root: Optional[str | Path] = None) -> dict:
    root = _normal_path(pack_root, default_pack_root())
    packs = []
    if root.exists():
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if child.is_dir():
                packs.append(_pack_info(child))
    total_bytes = sum(int(p.get("bytes", 0) or 0) for p in packs)
    valid_count = sum(1 for p in packs if p.get("valid"))
    return {
        "root": str(root),
        "exists": root.exists(),
        "total_bytes": total_bytes,
        "pack_count": len(packs),
        "valid_count": valid_count,
        "invalid_count": len(packs) - valid_count,
        "packs": packs,
    }


def clear_cache_pack_root(
    pack_root: Optional[str | Path] = None,
    *,
    older_than_days: Optional[float] = None,
    dry_run: bool = True,
) -> dict:
    root = _normal_path(pack_root, default_pack_root())
    stats = inspect_cache_pack_root(root)
    root_resolved = root.resolve()
    cutoff_ts = None
    if older_than_days is not None:
        cutoff_ts = _dt.datetime.now().timestamp() - float(older_than_days) * 86400.0

    deleted = []
    skipped = []
    for pack in stats["packs"]:
        path = Path(pack["path"])
        try:
            path_resolved = path.resolve()
        except OSError:
            skipped.append({**pack, "reason": "resolve_failed"})
            continue
        if path_resolved.parent != root_resolved:
            skipped.append({**pack, "reason": "outside_root"})
            continue
        if not pack.get("valid"):
            skipped.append({**pack, "reason": "not_khquant_pack"})
            continue
        if cutoff_ts is not None and float(pack.get("mtime", 0) or 0) >= cutoff_ts:
            skipped.append({**pack, "reason": "newer_than_cutoff"})
            continue

        item = {**pack, "dry_run": bool(dry_run)}
        if not dry_run:
            shutil.rmtree(path)
        deleted.append(item)

    return {
        "root": str(root),
        "dry_run": bool(dry_run),
        "deleted_count": len(deleted),
        "deleted_bytes": sum(int(p.get("bytes", 0) or 0) for p in deleted),
        "deleted": deleted,
        "skipped_count": len(skipped),
        "skipped": skipped,
    }


def _chunks(items: List[str], size: int) -> Iterable[List[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _normal_mode(value) -> str:
    text = str(value or "off").strip().lower()
    if text in ("0", "false", "off", "none", "disabled"):
        return "off"
    if text in ("1", "true", "on", "read", "readonly", "read_only"):
        return "read_only"
    if text in ("build", "build_then_read", "build-read", "read_write", "readwrite"):
        return "build_then_read"
    return "off"


def _normal_path(value, fallback: Path) -> Path:
    if value:
        return Path(str(value)).expanduser()
    return fallback


def _stocks_sha256(stock_list: List[str]) -> str:
    return hashlib.sha256("\n".join(stock_list).encode("utf-8")).hexdigest()


def _stock_db_path(data_root: str, stock_code: str) -> Path:
    market = "SH" if str(stock_code).upper().endswith(".SH") else "SZ"
    code = str(stock_code).split(".")[0]
    return Path(data_root) / market / f"{code}.db"


def _source_signature(data_root: str, stock_list: List[str]) -> str:
    """Fingerprint source DB file presence, size, and mtime for stale-pack checks."""
    h = hashlib.sha256()
    root = str(Path(data_root).resolve())
    h.update(root.encode("utf-8", errors="ignore"))
    for code in stock_list:
        path = _stock_db_path(data_root, code)
        try:
            stat = path.stat()
            payload = f"{code}|1|{stat.st_size}|{stat.st_mtime_ns}\n"
        except OSError:
            payload = f"{code}|0|0|0\n"
        h.update(payload.encode("utf-8", errors="ignore"))
    return h.hexdigest()


def _query_fields(
    field_list: List[str],
    dividend_type: str,
    *,
    legacy_ratio_only: bool = False,
) -> List[str]:
    fields = list(dict.fromkeys(["time"] + [f for f in (field_list or []) if f != "time"]))
    dt = (dividend_type or "none").lower()
    if dt in ("front", "back", "front_ratio", "back_ratio"):
        base_dt = dt.replace("_ratio", "")
        if legacy_ratio_only and dt in ("front_ratio", "back_ratio"):
            ratio_columns = {
                f"{price_field}_{dt}" for price_field in ("open", "high", "low", "close")
            }
            fields = [field for field in fields if field not in ratio_columns]
            suffixes = (base_dt,)
        elif dt in ("front_ratio", "back_ratio"):
            suffixes = (dt, base_dt)
        else:
            suffixes = (dt,)
        for suffix in suffixes:
            for price_field in ("open", "high", "low", "close"):
                field = f"{price_field}_{suffix}"
                if field not in fields:
                    fields.append(field)
    return fields


def _coerce_stock_list(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        import re
        raw = [part for part in re.split(r"[\s,]+", value) if part]
    else:
        raw = list(value)
    return [str(code).strip() for code in raw if str(code).strip()]


def _read_stock_list_file(path: Path) -> List[str]:
    if not path.exists():
        return []
    import csv

    codes = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return []
        code_col = 0
        for i, name in enumerate(header):
            if str(name).strip().lower() in ("code", "stock_code", "ts_code", "symbol", "代码", "证券代码"):
                code_col = i
                break
        else:
            first = str(header[0]).strip()
            if first:
                codes.append(first)
        for row in reader:
            if len(row) > code_col and str(row[code_col]).strip():
                codes.append(str(row[code_col]).strip())
    return codes


def _config_stock_list(data_cfg: dict, config_dir: Path) -> List[str]:
    stocks = _coerce_stock_list(data_cfg.get("stock_list") or data_cfg.get("stock_pool"))
    if stocks:
        return stocks
    stock_file = data_cfg.get("stock_list_file")
    if not stock_file:
        return []
    path = Path(str(stock_file)).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return _read_stock_list_file(path)


def _parse_auto_int(value, default: int) -> int:
    try:
        text = str(value).strip().lower()
    except Exception:
        text = ""
    if value is None or text in ("", "auto", "default"):
        return int(default)
    try:
        return int(value)
    except Exception:
        return int(default)


def _pack_request_from_config(
    config_path: str | Path,
    *,
    data_root: str,
    pack_root: Optional[str | Path] = None,
    performance_overrides: Optional[dict] = None,
) -> dict:
    config_path = Path(config_path)
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    config_dir = config_path.resolve().parent
    performance = normalize_performance_config(cfg.get("performance") or {})
    if performance_overrides:
        performance.update(performance_overrides)
    if pack_root is not None:
        performance["parquet_cache_pack_root"] = str(pack_root)

    if not data_root:
        raise ValueError("data_root is required for Parquet cache pack operations")

    data_cfg = cfg.get("data", {}) or {}
    backtest_cfg = cfg.get("backtest", {}) or {}
    field_list = list(data_cfg.get("fields") or [])
    if "time" not in field_list:
        field_list = ["time"] + field_list
    if "close" not in field_list:
        field_list.append("close")

    stock_list = _config_stock_list(data_cfg, config_dir)
    if not stock_list:
        raise ValueError("config data.stock_list/data.stock_pool is empty")

    start_time = str(backtest_cfg.get("start_time", "20240101") or "20240101")
    end_time = str(backtest_cfg.get("end_time", "20241231") or "20241231")
    try:
        preload_days = int(performance.get("framework_history_preload_days", 0) or 0)
    except Exception:
        preload_days = 0
    load_start_time = start_time
    if preload_days > 0:
        try:
            load_start_time = (
                pd.to_datetime(start_time, format="%Y%m%d") - pd.Timedelta(days=preload_days)
            ).strftime("%Y%m%d")
        except Exception:
            load_start_time = start_time

    period = str(data_cfg.get("kline_period", "1d") or "1d")
    dividend_type = str(data_cfg.get("dividend_type", "none") or "none").lower()
    spec = _spec(
        data_root=data_root,
        field_list=field_list,
        stock_list=stock_list,
        period=period,
        start_time=load_start_time,
        end_time=end_time,
        dividend_type=dividend_type,
    )
    resolved_pack_root = _normal_path(performance.get("parquet_cache_pack_root"), default_pack_root())
    pack_dir = pack_dir_for_spec(resolved_pack_root, spec)
    return {
        "config_path": str(config_path),
        "data_root": str(Path(data_root).resolve()),
        "pack_root": str(resolved_pack_root),
        "pack_dir": str(pack_dir),
        "performance": performance,
        "field_list": field_list,
        "stock_list": stock_list,
        "period": period,
        "start_time": load_start_time,
        "backtest_start_time": start_time,
        "end_time": end_time,
        "dividend_type": dividend_type,
        "spec": spec,
        "fingerprint": _fingerprint(spec),
    }


def check_pack_for_config(
    config_path: str | Path,
    *,
    data_root: str,
    pack_root: Optional[str | Path] = None,
    performance_overrides: Optional[dict] = None,
) -> dict:
    request = _pack_request_from_config(
        config_path,
        data_root=data_root,
        pack_root=pack_root,
        performance_overrides=performance_overrides,
    )
    pack_dir = Path(request["pack_dir"])
    matches, manifest = _manifest_matches(pack_dir, request["spec"])
    info = {
        "status": "hit" if matches else "miss",
        "pack_dir": str(pack_dir),
        "pack_root": request["pack_root"],
        "fingerprint": request["fingerprint"],
        "stock_count": len(request["stock_list"]),
        "period": request["period"],
        "start_time": request["start_time"],
        "end_time": request["end_time"],
        "fields": list(request["spec"]["fields"]),
        "dividend_type": request["dividend_type"],
    }
    if manifest:
        info.update(
            {
                "rows": int(manifest.get("rows", 0) or 0),
                "files": int(manifest.get("files", 0) or 0),
                "bytes": int(manifest.get("bytes", 0) or 0),
                "created_at": manifest.get("created_at", ""),
                "compression": manifest.get("compression", ""),
            }
        )
    return info


def build_pack_for_config(
    config_path: str | Path,
    *,
    data_root: str,
    pack_root: Optional[str | Path] = None,
    batch_size: Optional[int] = None,
    workers: Optional[int] = None,
    compression: Optional[str] = None,
    force: bool = False,
    performance_overrides: Optional[dict] = None,
) -> dict:
    overrides = dict(performance_overrides or {})
    if batch_size is not None:
        overrides["parquet_cache_pack_batch_size"] = batch_size
    if workers is not None:
        overrides["parquet_cache_pack_workers"] = workers
    if compression is not None:
        overrides["parquet_cache_pack_compression"] = compression
    request = _pack_request_from_config(
        config_path,
        data_root=data_root,
        pack_root=pack_root,
        performance_overrides=overrides,
    )
    pack_dir = Path(request["pack_dir"])
    matches, manifest = _manifest_matches(pack_dir, request["spec"])
    if matches and not force:
        return {
            "status": "exists",
            "pack_dir": str(pack_dir),
            "pack_root": request["pack_root"],
            "fingerprint": request["fingerprint"],
            "stock_count": len(request["stock_list"]),
            "rows": int(manifest.get("rows", 0) or 0) if manifest else 0,
            "files": int(manifest.get("files", 0) or 0) if manifest else 0,
            "bytes": int(manifest.get("bytes", 0) or 0) if manifest else 0,
            "created_at": manifest.get("created_at", "") if manifest else "",
        }

    perf = request["performance"]
    batch = _parse_auto_int(
        perf.get("parquet_cache_pack_batch_size", perf.get("duckdb_load_batch_size", 250)),
        250,
    )
    worker_count = _parse_auto_int(
        perf.get("parquet_cache_pack_workers", perf.get("duckdb_parallel_read_workers", 16)),
        16,
    )
    comp = str(perf.get("parquet_cache_pack_compression", "SNAPPY") or "SNAPPY").upper()
    query_fields = _query_fields(request["field_list"], request["dividend_type"])
    manifest = _build_pack(
        pack_dir=pack_dir,
        spec=request["spec"],
        stock_list=list(request["stock_list"]),
        query_fields=query_fields,
        field_list=list(request["spec"]["fields"]),
        batch_size=max(1, batch),
        workers=max(1, worker_count),
        compression=comp,
    )
    return {
        "status": "built",
        "pack_dir": str(pack_dir),
        "pack_root": request["pack_root"],
        "fingerprint": request["fingerprint"],
        "stock_count": len(request["stock_list"]),
        "rows": int(manifest.get("rows", 0) or 0),
        "files": int(manifest.get("files", 0) or 0),
        "bytes": int(manifest.get("bytes", 0) or 0),
        "build_s": manifest.get("build_s"),
        "created_at": manifest.get("created_at", ""),
        "compression": manifest.get("compression", comp),
    }


def write_read_only_config(
    config_path: str | Path,
    *,
    output_path: Optional[str | Path] = None,
    pack_root: Optional[str | Path] = None,
    in_place: bool = False,
    lazy_current_data: bool = False,
) -> dict:
    src = Path(config_path)
    cfg = json.loads(src.read_text(encoding="utf-8"))
    perf = normalize_performance_config(cfg.get("performance") or {})
    perf["parquet_cache_pack"] = "read_only"
    if pack_root is not None:
        perf["parquet_cache_pack_root"] = str(pack_root)
    if lazy_current_data:
        perf["current_data_container_mode"] = "lazy"
    cfg["performance"] = perf

    if in_place:
        target = src
    elif output_path is not None:
        target = Path(output_path)
    else:
        suffix = src.suffix or ".kh"
        target = src.with_name(f"{src.stem}_parquet_readonly{suffix}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")
    return {
        "status": "written",
        "path": str(target),
        "in_place": bool(in_place),
        "parquet_cache_pack": "read_only",
        "parquet_cache_pack_root": perf.get("parquet_cache_pack_root", ""),
    }


def _spec(
    *,
    data_root: str,
    field_list: List[str],
    stock_list: List[str],
    period: str,
    start_time: str,
    end_time: str,
    dividend_type: str,
) -> dict:
    fields = list(dict.fromkeys(["time"] + [f for f in (field_list or []) if f != "time"]))
    stocks = list(stock_list or [])
    dividend_type = str(dividend_type or "none").lower()
    raw_sidecar_fields = []
    if dividend_type in _ADJUSTED_DIVIDEND_TYPES:
        raw_sidecar_fields = [
            f"{_FRAMEWORK_RAW_PREFIX}{field}"
            for field in _PRICE_FIELDS
            if field in fields
        ]
    return {
        "format": PACK_FORMAT,
        "manifest_version": MANIFEST_VERSION,
        "data_root": str(Path(data_root).resolve()),
        "period": str(period or ""),
        "fields": fields,
        "stock_count": len(stocks),
        "stocks_sha256": _stocks_sha256(stocks),
        "source_signature": _source_signature(data_root, stocks),
        "start_time": str(start_time or ""),
        "end_time": str(end_time or ""),
        "dividend_type": dividend_type,
        "raw_sidecar_fields": raw_sidecar_fields,
    }


def _fingerprint(spec: dict) -> str:
    raw = json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def pack_dir_for_spec(pack_root: Path, spec: dict) -> Path:
    safe_period = str(spec["period"]).replace("/", "_")
    return pack_root / (
        f"{safe_period}_{spec['start_time']}_{spec['end_time']}_"
        f"{spec['stock_count']}_{_fingerprint(spec)}"
    )


def _manifest_matches(path: Path, spec: dict) -> Tuple[bool, Optional[dict]]:
    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        return False, None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return False, None
    has_files = bool(list(path.glob("batch_*.parquet")))
    is_empty_pack = int(manifest.get("rows", -1) or 0) == 0
    return manifest.get("spec") == spec and (has_files or is_empty_pack), manifest


def _select_dividend_fields(
    df: pd.DataFrame,
    dividend_type: str,
    *,
    context: Optional[str] = None,
) -> pd.DataFrame:
    return select_dividend_fields(df, dividend_type, context=context)


def _read_pack(pack_dir: Path, spec: dict, stock_list: List[str]) -> Dict[str, pd.DataFrame]:
    import duckdb

    fields = list(dict.fromkeys(
        list(spec["fields"]) + list(spec.get("raw_sidecar_fields", []) or [])
    ))
    result: Dict[str, pd.DataFrame] = {}
    files = list(pack_dir.glob("batch_*.parquet"))
    if not files:
        empty = pd.DataFrame(columns=fields)
        return {code: empty.copy() for code in stock_list}

    glob_path = (pack_dir / "batch_*.parquet").as_posix().replace("'", "''")
    con = duckdb.connect()
    try:
        df = con.execute(f"SELECT * FROM read_parquet('{glob_path}')").fetchdf()
    finally:
        con.close()

    if not df.empty and "stock_code" in df.columns:
        for code, part in df.groupby("stock_code", sort=False):
            out = part.drop(columns=["stock_code"]).reset_index(drop=True)
            if "time" in out.columns and not out["time"].is_monotonic_increasing:
                out = out.sort_values("time").reset_index(drop=True)
            result[str(code)] = out[[col for col in fields if col in out.columns]].copy()
    empty = pd.DataFrame(columns=fields)
    ordered: Dict[str, pd.DataFrame] = {}
    for code in stock_list:
        ordered[code] = result.get(code, empty.copy())
    return ordered


def _build_pack(
    *,
    pack_dir: Path,
    spec: dict,
    stock_list: List[str],
    query_fields: List[str],
    field_list: List[str],
    batch_size: int,
    workers: int,
    compression: str,
) -> dict:
    import duckdb

    from .manager import DuckDBManager
    from .xtdata_adapter import _LOCAL_TZ_OFFSET_SECONDS

    pack_dir.mkdir(parents=True, exist_ok=True)
    for old in pack_dir.glob("batch_*.parquet"):
        old.unlink()

    manager = DuckDBManager(data_root=spec["data_root"], max_connections=600, read_only=True)
    writer = duckdb.connect()
    started = _dt.datetime.now()
    rows = 0
    files = 0
    raw_sidecar_fields = list(spec.get("raw_sidecar_fields", []) or [])
    stored_fields = list(dict.fromkeys(list(field_list) + raw_sidecar_fields))
    columns = ["stock_code"] + stored_fields
    try:
        for batch_no, batch_codes in enumerate(_chunks(stock_list, batch_size), start=1):
            data = manager.get_kline_data_batch_epoch_seconds(
                batch_codes,
                spec["period"],
                spec["start_time"],
                spec["end_time"],
                dividend_type=None,
                fields=query_fields,
                workers=workers,
                tz_offset_seconds=_LOCAL_TZ_OFFSET_SECONDS,
            )
            if spec["dividend_type"] in ("front_ratio", "back_ratio"):
                retry_codes = [
                    code for code in batch_codes
                    if data.get(code) is None or data[code].empty
                ]
                if retry_codes:
                    legacy_data = manager.get_kline_data_batch_epoch_seconds(
                        retry_codes,
                        spec["period"],
                        spec["start_time"],
                        spec["end_time"],
                        dividend_type=None,
                        fields=_query_fields(
                            field_list,
                            spec["dividend_type"],
                            legacy_ratio_only=True,
                        ),
                        workers=workers,
                        tz_offset_seconds=_LOCAL_TZ_OFFSET_SECONDS,
                    )
                    for code in retry_codes:
                        legacy_df = legacy_data.get(code)
                        if legacy_df is not None and not legacy_df.empty:
                            data[code] = legacy_df
            frames = []
            for code in batch_codes:
                df = data.get(code)
                if df is None or df.empty:
                    continue
                raw_values = {}
                for raw_column in raw_sidecar_fields:
                    field = raw_column[len(_FRAMEWORK_RAW_PREFIX):]
                    if field in df.columns:
                        raw_values[raw_column] = df[field].to_numpy(copy=False)
                out = _select_dividend_fields(
                    df,
                    spec["dividend_type"],
                    context=f"Parquet {code}/{spec['period']}",
                )
                for raw_column, values in raw_values.items():
                    out[raw_column] = values
                available = [col for col in stored_fields if col in out.columns]
                out = out[available].copy()
                out.insert(0, "stock_code", code)
                frames.append(out)
            if not frames:
                continue
            batch_df = pd.concat(frames, ignore_index=True, copy=False)
            batch_df = batch_df[[col for col in columns if col in batch_df.columns]]
            file_path = pack_dir / f"batch_{batch_no:04d}.parquet"
            writer.register("batch_df", batch_df)
            try:
                writer.execute(
                    f"COPY batch_df TO '{file_path.as_posix()}' "
                    f"(FORMAT PARQUET, COMPRESSION {compression})"
                )
            finally:
                writer.unregister("batch_df")
            rows += len(batch_df)
            files += 1
    finally:
        writer.close()
        try:
            manager.close_all()
        except Exception:
            pass

    elapsed = (_dt.datetime.now() - started).total_seconds()
    manifest = {
        "spec": spec,
        "rows": rows,
        "files": files,
        "compression": compression,
        "build_s": round(elapsed, 3),
        "bytes": sum(p.stat().st_size for p in pack_dir.glob("batch_*.parquet")),
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    tmp_manifest_path = pack_dir / "manifest.json.tmp"
    manifest_path = pack_dir / "manifest.json"
    tmp_manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_manifest_path.replace(manifest_path)
    return manifest


def load_or_build_framework_raw_pack(
    *,
    field_list: List[str],
    stock_list: List[str],
    period: str,
    start_time: str,
    end_time: str,
    dividend_type: str,
    data_root: str,
    performance_cfg: dict,
) -> Tuple[Optional[Dict[str, pd.DataFrame]], dict]:
    """Load an optional framework raw cache pack, building only when requested."""
    perf = normalize_performance_config(performance_cfg or {})
    mode = _normal_mode(perf.get("parquet_cache_pack", "off"))
    info = {"mode": mode, "status": "off"}
    if mode == "off":
        return None, info

    repo_root = Path(__file__).resolve().parents[1]
    pack_root = _normal_path(
        perf.get("parquet_cache_pack_root"),
        repo_root / "temp" / "parquet_cache_pack",
    )
    spec = _spec(
        data_root=data_root,
        field_list=field_list,
        stock_list=stock_list,
        period=period,
        start_time=start_time,
        end_time=end_time,
        dividend_type=dividend_type,
    )
    pack_dir = pack_dir_for_spec(pack_root, spec)
    info.update({"pack_dir": str(pack_dir), "fingerprint": _fingerprint(spec)})

    matches, manifest = _manifest_matches(pack_dir, spec)
    if not matches:
        if mode != "build_then_read":
            info["status"] = "miss"
            return None, info
        try:
            batch_size = int(perf.get("parquet_cache_pack_batch_size", perf.get("duckdb_load_batch_size", 250)) or 250)
        except Exception:
            batch_size = 250
        try:
            workers = int(perf.get("parquet_cache_pack_workers", perf.get("duckdb_parallel_read_workers", 16)) or 16)
        except Exception:
            workers = 16
        compression = str(perf.get("parquet_cache_pack_compression", "SNAPPY") or "SNAPPY").upper()
        query_fields = _query_fields(field_list, dividend_type)
        try:
            manifest = _build_pack(
                pack_dir=pack_dir,
                spec=spec,
                stock_list=list(stock_list or []),
                query_fields=query_fields,
                field_list=list(spec["fields"]),
                batch_size=max(1, batch_size),
                workers=max(1, workers),
                compression=compression,
            )
            info["status"] = "built"
            info["build_s"] = manifest.get("build_s")
            info["bytes"] = manifest.get("bytes")
        except Exception as exc:
            logging.warning("Parquet cache pack build failed; falling back to DuckDB: %s", exc)
            info["status"] = "build_failed"
            info["error"] = str(exc)
            return None, info

    try:
        data = _read_pack(pack_dir, spec, list(stock_list or []))
    except Exception as exc:
        logging.warning("Parquet cache pack read failed; falling back to DuckDB: %s", exc)
        info["status"] = "read_failed"
        info["error"] = str(exc)
        return None, info
    info["status"] = "hit" if info.get("status") != "built" else "built"
    info["rows"] = int(sum(len(df) for df in data.values() if df is not None))
    info["stock_count"] = len(data)
    if manifest:
        info["bytes"] = manifest.get("bytes")
    return data, info
