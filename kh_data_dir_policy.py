# coding: utf-8
"""DuckDB 数据目录的归属判断，以及把 CS 的数据复制成 OS 自己的副本。

DuckDB 同一个库文件同一时刻只允许一个进程写；有进程在写时，别的进程连只读
也打不开。所以 OS 和 CS 共用一个数据目录时，OS 的回测和数据管理会挡住 CS
的补数和实盘加载，反过来也一样。开源版的规则：

- 默认使用 OS 自己的目录（%LOCALAPPDATA%\\KhQuantOS\\khData）。
- 推荐把 CS 的数据复制一份给 OS 用：每个库用内存连接以只读方式短暂挂上，
  复制完立即断开，不往 CS 的目录写任何东西。
- 用户坚持直接指向 CS 的目录时，数据管理里的导入、核验索引、WAL 修复每次都
  要确认，策略里的 khDuckWrite 拒绝写入。

CS 不在数据目录里留下任何标记，OS 也绝不读取 CS 的设置文件，所以这里按目录
本身判断：OS 建立或认领过的目录里放一个标记文件；没有标记、却已经有行情库的
目录，以及 CS 的默认目录 D:\\khData，都当作「可能和 CS 共用」。用户确认某个
目录不是 CS 在用的以后，给它补上标记，以后不再询问。
"""
import json
import os
from datetime import datetime

from kh_app_identity import APP_NAME, default_duckdb_dir

OS_MARKER_NAME = ".khquant_os_data.json"
CS_DEFAULT_DATA_DIRS = (r"D:\khData",)
MARKET_DIRS = ("SH", "SZ", "BJ")

SHARED_WRITE_WARNING = "OS 回测和数据管理期间，CS 的补数和实盘策略可能读写失败。"


class SharedDataDirWriteRefused(PermissionError):
    """策略向可能和 CS 共用的数据目录写入时抛出。"""


def normalize_dir(path) -> str:
    """把目录规范成可比较的形式（绝对路径、统一大小写和分隔符）。"""
    text = os.path.expandvars(os.path.expanduser(str(path or "").strip()))
    return os.path.normcase(os.path.normpath(os.path.abspath(text)))


def _same_dir(left, right) -> bool:
    try:
        return normalize_dir(left) == normalize_dir(right)
    except (TypeError, ValueError):
        return False


def is_cs_default_dir(path) -> bool:
    return any(_same_dir(path, candidate) for candidate in CS_DEFAULT_DATA_DIRS)


def _marker_path(path) -> str:
    return os.path.join(str(path), OS_MARKER_NAME)


def is_os_owned(path) -> bool:
    """OS 的默认目录，或 OS 建立 / 认领过（有标记文件）的目录。"""
    if not path:
        return False
    if _same_dir(path, default_duckdb_dir()):
        return True
    return os.path.isfile(_marker_path(path))


def mark_os_owned(path, reason: str = "") -> str:
    """给目录补上 OS 标记文件，返回标记文件路径。已有标记时不改动。"""
    os.makedirs(path, exist_ok=True)
    marker = _marker_path(path)
    if os.path.isfile(marker):
        return marker
    payload = {
        "app": APP_NAME,
        "marked_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
        "note": "看海量化开源版用这个文件识别自己的数据目录，删除后会被当作可能和 CS 共用的目录。",
    }
    temp = marker + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp, marker)
    return marker


def _market_dir_entries(path):
    """返回 [(市场名, 市场目录)]；Windows 上目录名不区分大小写。"""
    try:
        names = os.listdir(path)
    except OSError:
        return []
    wanted = {market.lower(): market for market in MARKET_DIRS}
    found = []
    for name in names:
        full = os.path.join(path, name)
        if name.lower() in wanted and os.path.isdir(full):
            found.append((wanted[name.lower()], full))
    return found


def has_market_data(path) -> bool:
    """目录里已经有 metadata.db 或任何行情库文件。"""
    if not path or not os.path.isdir(path):
        return False
    if os.path.isfile(os.path.join(path, "metadata.db")):
        return True
    for _market, market_dir in _market_dir_entries(path):
        try:
            if any(name.lower().endswith(".db") for name in os.listdir(market_dir)):
                return True
        except OSError:
            continue
    return False


def may_be_shared_with_cs(path) -> bool:
    """True 表示这个目录可能也被 CS 版在用，写入前要让用户确认。"""
    if not path:
        return False
    if is_os_owned(path):
        return False
    if is_cs_default_dir(path):
        return True
    return has_market_data(path)


def claim_if_new(path, reason: str = "OS 首次写入") -> bool:
    """目录还没有行情数据时登记为 OS 自己的目录。

    返回 True 表示这是 OS 的目录（原来就是或刚登记）；返回 False 表示它可能和
    CS 共用，没有做任何改动。
    """
    if not path or may_be_shared_with_cs(path):
        return False
    if not is_os_owned(path):
        mark_os_owned(path, reason)
    return True


def shared_dir_write_message(path, operation: str) -> str:
    return (
        f"当前数据目录 {path} 可能也在被看海量化 CS 版使用。\n\n"
        f"{SHARED_WRITE_WARNING}\n"
        f"「{operation}」会以写方式打开这个目录里的数据库，期间 CS 无法读写这些库。"
    )


def refuse_strategy_write_if_shared(path) -> None:
    """khDuckWrite 写入前调用：目录可能和 CS 共用时拒绝写入。"""
    if not path:
        return
    if may_be_shared_with_cs(path):
        raise SharedDataDirWriteRefused(
            f"khDuckWrite 已拒绝写入：数据目录 {path} 可能也在被看海量化 CS 版使用，"
            "写入会挡住 CS 的补数和实盘加载。请把 duckdb_path 改成开源版自己的目录"
            "（可在「数据管理」里把 CS 的数据复制一份过去）。如果确认这个目录不是 CS "
            "在用的，在「数据管理」里对它执行一次导入并选择「这不是 CS 的目录」即可。"
        )
    claim_if_new(path, "khDuckWrite 首次写入")


# ── 复制 CS 数据为 OS 自己的副本 ──────────────────────────────────────────


def _duckdb_literal(path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def list_database_files(src):
    """列出要复制的库文件（相对路径）：metadata.db 和 SH/SZ/BJ 下的 *.db。"""
    files = []
    if os.path.isfile(os.path.join(src, "metadata.db")):
        files.append("metadata.db")
    for market, market_dir in sorted(_market_dir_entries(src)):
        try:
            names = sorted(os.listdir(market_dir))
        except OSError:
            continue
        for name in names:
            full = os.path.join(market_dir, name)
            if name.lower().endswith(".db") and os.path.isfile(full):
                files.append(f"{market}/{name}")
    return files


def validate_copy_dirs(src, dst):
    """检查源目录和目标目录，出问题时抛 ValueError（中文说明）。"""
    if not src or not os.path.isdir(src):
        raise ValueError("请选择存在的源数据目录（CS 的 DuckDB 数据目录）。")
    if not dst:
        raise ValueError("请选择目标目录。")
    src_norm, dst_norm = normalize_dir(src), normalize_dir(dst)
    if src_norm == dst_norm:
        raise ValueError("源目录和目标目录不能是同一个。")
    if dst_norm.startswith(src_norm + os.sep) or src_norm.startswith(dst_norm + os.sep):
        raise ValueError("源目录和目标目录不能互相包含。")
    if not has_market_data(src):
        raise ValueError("源目录里没有找到行情库（metadata.db 或 SH/SZ/BJ 下的 .db 文件）。")
    if os.path.isdir(dst) and has_market_data(dst) and not is_os_owned(dst):
        raise ValueError("目标目录里已经有别的行情数据，请换一个空目录或开源版自己的目录。")


def _copy_one_database(src_file, dst_file):
    """用内存连接只读挂上源库，整库复制到新文件，完成后立即断开。"""
    import duckdb

    partial = dst_file + ".partial"
    for leftover in (partial, partial + ".wal"):
        if os.path.exists(leftover):
            os.remove(leftover)
    os.makedirs(os.path.dirname(dst_file), exist_ok=True)
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH {_duckdb_literal(src_file)} AS kh_src (READ_ONLY)")
        try:
            con.execute(f"ATTACH {_duckdb_literal(partial)} AS kh_dst")
            try:
                con.execute("COPY FROM DATABASE kh_src TO kh_dst")
                con.execute("CHECKPOINT kh_dst")
            finally:
                con.execute("DETACH kh_dst")
        finally:
            con.execute("DETACH kh_src")
    finally:
        con.close()
    if os.path.exists(partial + ".wal"):
        os.remove(partial + ".wal")
    os.replace(partial, dst_file)


def _copy_root_config(src, dst):
    """复制 config.json，并把其中的 data_root 改成目标目录。"""
    source = os.path.join(src, "config.json")
    if not os.path.isfile(source):
        return
    try:
        with open(source, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError):
        return
    if isinstance(config, dict):
        config["data_root"] = os.path.abspath(dst)
    target = os.path.join(dst, "config.json")
    with open(target + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=4)
    os.replace(target + ".tmp", target)


def copy_data_dir_snapshot(src, dst, progress=None, should_stop=None, log=None, overwrite=False):
    """把 src 的 DuckDB 数据复制成 dst（OS 自己的目录）。

    - 对 src 只做只读访问；每个库复制完立即断开。
    - 目标里已经存在的同名库视为上次已复制，直接跳过，所以中断后可以续做；
      overwrite=True 时重新复制并覆盖（副本里后来写入的内容会丢失）。
    - 源库正被 CS 写入而打不开时记入 failed，其余库继续；稍后可再运行一次补齐。

    progress(done, total, rel)、should_stop() 和 log(text) 都可以省略。
    返回 {"total", "copied", "skipped", "failed": [(rel, error)], "stopped", "bytes"}。
    """
    validate_copy_dirs(src, dst)
    os.makedirs(dst, exist_ok=True)
    mark_os_owned(dst, f"复制自 {os.path.abspath(src)}")
    files = list_database_files(src)
    result = {"total": len(files), "copied": 0, "skipped": 0, "failed": [], "stopped": False, "bytes": 0}
    for index, rel in enumerate(files, start=1):
        if should_stop is not None and should_stop():
            result["stopped"] = True
            break
        src_file = os.path.join(src, *rel.split("/"))
        dst_file = os.path.join(dst, *rel.split("/"))
        if not overwrite and os.path.isfile(dst_file) and os.path.getsize(dst_file) > 0:
            result["skipped"] += 1
        else:
            try:
                _copy_one_database(src_file, dst_file)
                result["copied"] += 1
                result["bytes"] += os.path.getsize(dst_file)
            except Exception as exc:  # noqa: BLE001 - 单个库失败不影响其余库
                result["failed"].append((rel, str(exc)))
                if log is not None:
                    log(f"✗ {rel} 复制失败：{exc}")
        if progress is not None:
            progress(index, len(files), rel)
    if not result["stopped"]:
        _copy_root_config(src, dst)
    return result


__all__ = [
    "OS_MARKER_NAME", "CS_DEFAULT_DATA_DIRS", "SHARED_WRITE_WARNING", "SharedDataDirWriteRefused",
    "normalize_dir", "is_cs_default_dir", "is_os_owned", "mark_os_owned", "has_market_data",
    "may_be_shared_with_cs", "claim_if_new", "shared_dir_write_message",
    "refuse_strategy_write_if_shared", "list_database_files", "validate_copy_dirs",
    "copy_data_dir_snapshot",
]
