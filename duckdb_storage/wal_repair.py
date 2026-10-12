import os
import argparse
import duckdb
import shutil
from datetime import datetime


def resolve_data_root(arg_path):
    if arg_path:
        return os.path.abspath(arg_path)
    env_path = os.environ.get("DUCKDB_DATA_ROOT", "")
    if env_path:
        return os.path.abspath(env_path)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "stock_data"))


QUARANTINE_PREFIX = "_wal_repair_quarantine_"

# 这些错误说明数据库只是被别的连接占着，文件本身没坏：不能删 WAL、更不能隔离换空库
_IN_USE_MARKERS = (
    "being used by another process",
    "另一个程序正在使用",
    "could not set lock",
    "conflicting lock",
    "different configuration",
    "database is locked",
)


def is_in_use_error(error):
    text = str(error).lower()
    return any(marker.lower() in text for marker in _IN_USE_MARKERS)


# 这些错误是环境问题（没权限/只读/磁盘满/内存不够），库文件本身没坏：
# 删 WAL 会把已提交还没写回主库的数据永久丢掉，所以一律跳过、不做任何改动
_ENV_ERROR_MARKERS = (
    ("权限不足或文件只读", (
        "拒绝访问",
        "access is denied",
        "permission denied",
        "winerror 5]",
        "errno 13",
        "read-only",
        "read only",
        "readonly",
        "只读",
    )),
    ("磁盘空间不足", (
        "no space left",
        "disk is full",
        "not enough space",
        "磁盘空间不足",
        "空间不足",
        "winerror 112]",
        "errno 28",
    )),
    ("内存不足", (
        "out of memory",
        "memory limit",
        "could not allocate",
        "failed to allocate",
        "内存不足",
    )),
    ("数据库版本不兼容", (
        # 新版 DuckDB 写的库旧版读不了，换成空库等于丢数据
        "version number",
        "storage version",
    )),
)

# WAL 里的长度字段损坏时，回放会按一个离谱的长度申请内存：Linux/macOS 报
# "Out of Memory Error: Failure while replaying WAL file ...: Allocation failure"，
# Windows 报 "... replaying WAL file ...: bad array new length"。这是文件损坏，
# 不是机器内存不够，要先于环境类判断（修复前仍会完整备份主库与 WAL）
_CORRUPT_WAL_ALLOCATION_MARKERS = (
    "allocation failure",
    "bad array new length",
)

# 只有明确是 WAL 回放失败/文件损坏的错误才进入修复流程（白名单）
_REPAIRABLE_MARKERS = (
    "replaying wal",
    "corrupt",
    "checksum",
    "not a valid duckdb database",
    "could not read enough bytes",
    "serialization error",
)


def classify_repair_error(error):
    """返回 (是否可以进修复流程, 原因)。环境类/占用类/没认出来的错误都不修复。"""
    if is_in_use_error(error):
        return False, "被占用"
    text = str(error).lower()
    if "replaying wal" in text and any(marker in text for marker in _CORRUPT_WAL_ALLOCATION_MARKERS):
        return True, "WAL回放失败或文件损坏"
    for reason, markers in _ENV_ERROR_MARKERS:
        if any(marker.lower() in text for marker in markers):
            return False, reason
    if any(marker in text for marker in _REPAIRABLE_MARKERS):
        return True, "WAL回放失败或文件损坏"
    return False, "未识别的错误类型，为防误删数据未做修复"


def is_repairable_error(error):
    return classify_repair_error(error)[0]


def _skip_message(reason, error):
    # viewer 按“被占用”前缀统计占用数量，这个前缀不能改
    if reason == "被占用":
        return f"被占用，已跳过：{error}"
    return f"已跳过（{reason}），未做任何改动：{error}"


def iter_db_files(root_path):
    for dirpath, dirnames, filenames in os.walk(root_path):
        # 以前修复留下的隔离目录不再检查
        dirnames[:] = [d for d in dirnames if not d.startswith(QUARANTINE_PREFIX)]
        for name in filenames:
            if name.endswith(".db"):
                yield os.path.join(dirpath, name)


def checkpoint_db(db_path, memory_limit):
    conn = duckdb.connect(db_path)
    try:
        if memory_limit:
            conn.execute(f"SET memory_limit='{memory_limit}'")
        conn.execute("CHECKPOINT")
    finally:
        conn.close()


def remove_sidecars(db_path):
    removed = 0
    for ext in [".wal", ".shm"]:
        path = db_path + ext
        if os.path.exists(path):
            os.remove(path)
            removed += 1
    return removed


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def move_if_exists(src, dst):
    if os.path.exists(src):
        ensure_dir(os.path.dirname(dst))
        shutil.move(src, dst)
        return True
    return False


def backup_db_files(db_path, backup_db):
    """动手前把主库和 .wal/.shm 原样复制一份，并核对大小；任何一步不对就抛异常。"""
    copied = []
    try:
        ensure_dir(os.path.dirname(backup_db))
        for ext in ["", ".wal", ".shm"]:
            src = db_path + ext
            if not os.path.exists(src):
                continue
            dst = backup_db + ext
            shutil.copy2(src, dst)
            copied.append(dst)
            src_size = os.path.getsize(src)
            dst_size = os.path.getsize(dst)
            if src_size != dst_size:
                raise IOError(f"备份大小不一致：{src}({src_size}) -> {dst}({dst_size})")
        if not os.path.exists(backup_db):
            raise IOError(f"主库备份不存在：{backup_db}")
    except Exception:
        # 备份不完整就不留半份，免得被误当成可用备份
        for path in copied:
            try:
                os.remove(path)
            except Exception:
                pass
        raise
    return copied


def restore_sidecars(db_path, backup_db):
    """把备份里的 .wal/.shm 放回原位（重试仍因环境问题失败时，恢复成修复前的样子）。"""
    for ext in [".wal", ".shm"]:
        src = backup_db + ext
        if os.path.exists(src) and not os.path.exists(db_path + ext):
            shutil.copy2(src, db_path + ext)


def rebuild_empty_db(db_path, memory_limit):
    conn = duckdb.connect(db_path)
    try:
        if memory_limit:
            conn.execute(f"SET memory_limit='{memory_limit}'")
        conn.execute("CHECKPOINT")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duckdb_path", default=None)
    parser.add_argument("--memory_limit", default="256MB")
    args = parser.parse_args()

    data_root = resolve_data_root(args.duckdb_path)
    result = repair_data_root(data_root, args.memory_limit)
    if result is None:
        return

    print(f"总计: {result['total']}")
    print(f"正常: {result['ok']}")
    print(f"修复: {result['fixed']}")
    print(f"重建: {result['rebuilt']}")
    print(f"失败: {len(result['failed'])}")
    print(f"删除WAL/SHM: {result['removed_sidecars']}")
    if result['rebuilt'] > 0:
        print(f"隔离目录: {result['quarantine_root']}")
    if result.get('backups'):
        print(f"修复前备份目录: {result['backup_root']}")
    if result['failed']:
        print("失败明细:")
        for db_path, msg in result['failed'][:50]:
            print(f"{db_path} -> {msg}")
        if len(result['failed']) > 50:
            print(f"... 还有 {len(result['failed']) - 50} 条失败记录")


def repair_data_root(data_root, memory_limit="256MB"):
    if not os.path.exists(data_root):
        print(f"路径不存在: {data_root}")
        return None

    db_files = list(iter_db_files(data_root))
    if not db_files:
        print("未找到任何 .db 文件")
        return None

    ok = 0
    fixed = 0
    failed = []
    removed_sidecars = 0
    rebuilt = 0
    skipped = 0
    backups = []
    quarantine_root = os.path.join(data_root, QUARANTINE_PREFIX + datetime.now().strftime("%Y%m%d_%H%M%S"))
    # 修复前的原样备份放在隔离目录下单独一层，隔离目录本身不会被再次扫描
    backup_root = os.path.join(quarantine_root, "_backup_before_repair")

    for db_path in db_files:
        try:
            checkpoint_db(db_path, memory_limit)
            ok += 1
            continue
        except Exception as e:
            repairable, reason = classify_repair_error(e)
            if not repairable:
                # 白名单之外的错误一律不碰文件，只报告原因
                failed.append((db_path, _skip_message(reason, e)))
                skipped += 1
                continue
            relative_path = os.path.relpath(db_path, data_root)
            backup_db = os.path.join(backup_root, relative_path)
            # 删 WAL/SHM、移主库、建空库之前，先完整备份主库和 WAL；备份不成功就不动这个库
            try:
                backup_db_files(db_path, backup_db)
            except Exception as e_bk:
                failed.append((db_path, f"{e} | 修复前备份失败，已中止、未做改动：{e_bk}"))
                continue
            backups.append((db_path, backup_db))
            try:
                removed_sidecars += remove_sidecars(db_path)
            except Exception as e_rm:
                failed.append((db_path, f"{e} | 删除残留临时文件失败：{e_rm} | 备份：{backup_db}"))
                continue
            try:
                checkpoint_db(db_path, memory_limit)
                fixed += 1
                continue
            except Exception as e2:
                repairable2, reason2 = classify_repair_error(e2)
                if not repairable2:
                    # 去掉 WAL 后打不开是环境问题，把 WAL 放回去，保持修复前原样
                    try:
                        restore_sidecars(db_path, backup_db)
                        msg = _skip_message(reason2, e2)
                    except Exception as e_rs:
                        msg = f"{reason2}，且放回WAL失败（请从备份手动放回）：{e2} | {e_rs}"
                    failed.append((db_path, f"{msg} | 备份：{backup_db}"))
                    skipped += 1
                    continue
                target_db = os.path.join(quarantine_root, relative_path)
                try:
                    move_if_exists(db_path, target_db)
                    move_if_exists(db_path + ".wal", target_db + ".wal")
                    move_if_exists(db_path + ".shm", target_db + ".shm")
                except Exception as e_mv:
                    failed.append((db_path, f"{e} | {e2} | 移到隔离目录失败：{e_mv} | 备份：{backup_db}"))
                    continue
                try:
                    rebuild_empty_db(db_path, memory_limit)
                    rebuilt += 1
                except Exception as e3:
                    failed.append((db_path, f"{e} | {e2} | {e3} | 备份：{backup_db}"))

    return {
        "total": len(db_files),
        "ok": ok,
        "fixed": fixed,
        "rebuilt": rebuilt,
        "failed": failed,
        "removed_sidecars": removed_sidecars,
        "quarantine_root": quarantine_root,
        # 以下为新增字段：跳过数、修复前备份 [(原库路径, 备份主库路径)] 与备份根目录
        "skipped": skipped,
        "backups": backups,
        "backup_root": backup_root,
    }


if __name__ == "__main__":
    main()
