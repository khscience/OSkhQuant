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


def iter_db_files(root_path):
    for dirpath, _, filenames in os.walk(root_path):
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
    quarantine_root = os.path.join(data_root, "_wal_repair_quarantine_" + datetime.now().strftime("%Y%m%d_%H%M%S"))

    for db_path in db_files:
        try:
            checkpoint_db(db_path, memory_limit)
            ok += 1
            continue
        except Exception as e:
            removed_sidecars += remove_sidecars(db_path)
            try:
                checkpoint_db(db_path, memory_limit)
                fixed += 1
            except Exception as e2:
                relative_path = os.path.relpath(db_path, data_root)
                target_db = os.path.join(quarantine_root, relative_path)
                target_wal = target_db + ".wal"
                target_shm = target_db + ".shm"
                move_if_exists(db_path, target_db)
                move_if_exists(db_path + ".wal", target_wal)
                move_if_exists(db_path + ".shm", target_shm)
                try:
                    rebuild_empty_db(db_path, memory_limit)
                    rebuilt += 1
                except Exception as e3:
                    failed.append((db_path, f"{e} | {e2} | {e3}"))

    return {
        "total": len(db_files),
        "ok": ok,
        "fixed": fixed,
        "rebuilt": rebuilt,
        "failed": failed,
        "removed_sidecars": removed_sidecars,
        "quarantine_root": quarantine_root
    }


if __name__ == "__main__":
    main()
