# coding: utf-8
r"""Shared path helpers for strategy/config files.

开源版的用户目录全部取自 kh_app_identity（%LOCALAPPDATA%\KhQuantOS 等），
与 CS 版的 KhQuant 目录互不影响。
"""

from __future__ import annotations

import ntpath
import os
import sys
import tempfile
import uuid
from typing import Iterable

from kh_app_identity import local_appdata_dir, LOCAL_APPDATA_DIR_NAME


def _clean_path(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(str(path).strip()))


def _is_windows_absolute(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return (
        (len(normalized) >= 3 and normalized[1:3] == ":/" and normalized[0].isalpha())
        or normalized.startswith("//")
    )


def _is_absolute_like(path: str) -> bool:
    return os.path.isabs(path) or _is_windows_absolute(path)


def _config_dir_from(config_path: str | None, config_dir: str | None = None) -> str:
    if config_dir:
        return os.path.abspath(_clean_path(config_dir))
    if config_path:
        return os.path.dirname(os.path.abspath(_clean_path(config_path)))
    return ""


def _path_basename(path: str) -> str:
    return ntpath.basename(path.replace("/", "\\"))


def _add_candidate(candidates: list[str], seen: set[str], path: str | None) -> None:
    if not path:
        return
    candidate = os.path.normpath(_clean_path(path))
    key = os.path.normcase(os.path.abspath(candidate) if not _is_absolute_like(candidate) else candidate)
    if key in seen:
        return
    seen.add(key)
    candidates.append(candidate)


def _existing_path(path: str) -> bool:
    try:
        return os.path.exists(path)
    except (OSError, ValueError):
        return False


def _absolute_existing(path: str) -> str:
    if _is_absolute_like(path):
        return os.path.normpath(path)
    return os.path.abspath(path)


def is_frozen_runtime() -> bool:
    """可靠判断当前是否运行在打包程序中（PyInstaller 标记或环境标记）。"""
    if bool(getattr(sys, "frozen", False)) or bool(getattr(sys, "_MEIPASS", None)):
        return True
    return os.environ.get("KHQUANT_PACKAGED") == "1"


def get_app_root_dir() -> str:
    """Return the install/code root used for user-visible local artifacts."""
    if is_frozen_runtime():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_user_stock_pool_dir(create: bool = False) -> str:
    """Return the per-user directory for downloaded stock-pool CSV files."""
    path = os.path.normpath(os.path.abspath(local_appdata_dir("data")))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _temp_fallback_dir(*parts: str) -> str:
    return os.path.join(tempfile.gettempdir(), LOCAL_APPDATA_DIR_NAME, *parts)


def get_bundled_stock_pool_dirs() -> list[str]:
    """Return bundled/source stock-pool directories in lookup order."""
    candidates: list[str] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(os.path.join(str(bundle_root), "data"))

    # Source mode and PyInstaller's imported module location.
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))

    if is_frozen_runtime():
        app_root = get_app_root_dir()
        candidates.extend(
            (
                os.path.join(app_root, "_internal", "data"),
                os.path.join(app_root, "data"),
            )
        )
    return _dedupe_paths(candidates)


def get_stock_pool_write_dir(create: bool = True) -> str:
    """Return a writable directory for generated stock-pool files.

    Packaged applications never write into ``Program Files`` or PyInstaller's
    ``_internal`` directory. Source mode keeps the historical project ``data``
    directory when it is writable.
    """
    bundled_dirs = get_bundled_stock_pool_dirs()
    if not is_frozen_runtime() and bundled_dirs:
        primary = bundled_dirs[0]
    else:
        primary = get_user_stock_pool_dir(create=False)

    if not create:
        return primary
    if _is_writable_dir(primary):
        return primary

    user_dir = get_user_stock_pool_dir(create=False)
    if _is_writable_dir(user_dir):
        return user_dir

    fallback = _temp_fallback_dir("data")
    if _is_writable_dir(fallback):
        return os.path.normpath(os.path.abspath(fallback))
    return os.path.normpath(os.path.abspath(primary))


def get_stock_pool_read_dirs(include_missing: bool = True) -> list[str]:
    """Return stock-pool lookup directories, user updates before bundled data."""
    candidates: list[str] = []
    fallback_dir = _temp_fallback_dir("data")
    if is_frozen_runtime():
        candidates.append(get_user_stock_pool_dir(create=False))
        # LocalAppData 不可写时更新器会落到临时目录；它仍属于本次用户更新，
        # 必须排在随包默认数据之前，避免刚写入就被旧文件遮住。
        candidates.append(fallback_dir)
    candidates.extend(get_bundled_stock_pool_dirs())
    if not is_frozen_runtime():
        # Covers the uncommon source deployment whose code directory is read-only.
        candidates.append(get_user_stock_pool_dir(create=False))
        candidates.append(fallback_dir)
    dirs = _dedupe_paths(candidates)
    if include_missing:
        return dirs
    return [path for path in dirs if os.path.isdir(path)]


def get_stock_pool_path(filename: str, *, for_write: bool = False) -> str:
    """Resolve one managed stock-pool file with user-over-bundle precedence."""
    clean_name = os.path.basename(str(filename or "").strip())
    if not clean_name or clean_name != str(filename or "").strip():
        raise ValueError("股票池文件名必须是不含目录的文件名")
    if for_write:
        return os.path.join(get_stock_pool_write_dir(create=True), clean_name)
    for data_dir in get_stock_pool_read_dirs(include_missing=True):
        candidate = os.path.join(data_dir, clean_name)
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(get_stock_pool_write_dir(create=False), clean_name)


def get_custom_stock_pool_path(*, for_write: bool = False) -> str:
    """Resolve ``otheridx.csv`` without ever editing a bundled read-only copy.

    打包版写到 LocalAppData 的股票池目录。
    """
    filename = "otheridx.csv"
    if not for_write:
        return get_stock_pool_path(filename)
    if not is_frozen_runtime():
        return get_stock_pool_path(filename, for_write=True)
    return os.path.join(get_user_stock_pool_dir(create=True), filename)


def _is_writable_dir(path: str) -> bool:
    """Quickly verify that *path* accepts a newly created file.

    ``tempfile.NamedTemporaryFile`` may retry a very large number of candidate
    names after ``PermissionError`` on Windows.  That turns a normal
    ``Program Files`` permission denial into a CPU-bound hang, so use one
    explicit exclusive create instead.
    """
    probe_path = None
    probe_fd = None
    try:
        os.makedirs(path, exist_ok=True)
        probe_path = os.path.join(
            path,
            f".khquant_write_test_{os.getpid()}_{uuid.uuid4().hex}",
        )
        probe_fd = os.open(
            probe_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.write(probe_fd, b"ok")
        os.close(probe_fd)
        probe_fd = None
        os.remove(probe_path)
        probe_path = None
        return True
    except OSError:
        if probe_fd is not None:
            try:
                os.close(probe_fd)
            except OSError:
                pass
        if probe_path:
            try:
                os.remove(probe_path)
            except OSError:
                pass
        return False


def _dedupe_paths(paths: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path:
            continue
        normalized = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
        key = os.path.normcase(normalized)
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _local_appdata_backtest_dir() -> str:
    return local_appdata_dir("backtest_results")


def get_backtest_results_dir(create: bool = True) -> str:
    r"""Return the primary backtest result directory.

    打包版写到 %LOCALAPPDATA%\KhQuantOS\backtest_results，普通用户无需写入
    Program Files；源码模式沿用项目目录下的 backtest_results。
    """
    if is_frozen_runtime():
        primary = _local_appdata_backtest_dir()
    else:
        primary = os.path.join(get_app_root_dir(), "backtest_results")

    if not create:
        return os.path.normpath(os.path.abspath(os.path.expanduser(primary)))

    if _is_writable_dir(primary):
        return os.path.normpath(os.path.abspath(os.path.expanduser(primary)))

    fallback = _local_appdata_backtest_dir()
    if _is_writable_dir(fallback):
        return os.path.normpath(os.path.abspath(os.path.expanduser(fallback)))
    return os.path.normpath(os.path.abspath(os.path.expanduser(primary)))


def get_backtest_results_dirs(include_legacy: bool = True, include_missing: bool = False) -> list[str]:
    """Return directories that may contain backtest results, primary first."""
    candidates = [get_backtest_results_dir(create=False)]

    if include_legacy:
        candidates.append(_local_appdata_backtest_dir())
        # 源码模式或早期把结果写在程序目录的情况，保留只读扫描。
        candidates.append(os.path.join(get_app_root_dir(), "backtest_results"))

    dirs = _dedupe_paths(candidates)
    if include_missing:
        return dirs
    return [path for path in dirs if os.path.isdir(path)]


def resolve_strategy_file(
    raw_path: str,
    config_path: str | None = None,
    config_dir: str | None = None,
    strategy_dir: str | None = None,
    cwd: str | None = None,
    extra_base_dirs: Iterable[str] | None = None,
) -> str:
    """Resolve a ``strategy_file`` value from a .kh config.

    Relative strategy paths are resolved relative to the .kh file directory
    first. Existing cwd/project-root style configs still work through later
    fallback candidates.
    """
    if not raw_path:
        return raw_path

    raw_path = _clean_path(raw_path)
    cfg_dir = _config_dir_from(config_path, config_dir)
    current_dir = os.path.abspath(_clean_path(cwd)) if cwd else os.getcwd()
    basename = _path_basename(raw_path)
    candidates: list[str] = []
    seen: set[str] = set()
    is_abs = _is_absolute_like(raw_path)

    if is_abs:
        _add_candidate(candidates, seen, raw_path)
    else:
        if cfg_dir:
            _add_candidate(candidates, seen, os.path.join(cfg_dir, raw_path))
        if current_dir:
            _add_candidate(candidates, seen, os.path.join(current_dir, raw_path))
        for base_dir in extra_base_dirs or []:
            if base_dir:
                _add_candidate(candidates, seen, os.path.join(base_dir, raw_path))

    if cfg_dir and basename:
        _add_candidate(candidates, seen, os.path.join(cfg_dir, basename))

    if cfg_dir and config_path:
        kh_base = os.path.splitext(os.path.basename(config_path))[0]
        if kh_base:
            _add_candidate(candidates, seen, os.path.join(cfg_dir, kh_base + ".py"))

    if strategy_dir and basename:
        if not is_abs:
            _add_candidate(candidates, seen, os.path.join(strategy_dir, raw_path))
        _add_candidate(candidates, seen, os.path.join(strategy_dir, basename))

    for candidate in candidates:
        if _existing_path(candidate):
            return _absolute_existing(candidate)

    return _absolute_existing(candidates[0]) if candidates else raw_path


def strategy_file_for_config(strategy_file: str, config_path: str) -> str:
    """Return a stable value to save in a .kh config.

    Absolute paths in the same directory as the target config are saved as a
    filename-only relative path. User-entered relative paths and external
    absolute paths are kept.
    """
    if not strategy_file:
        return strategy_file

    strategy_file = _clean_path(strategy_file)
    if not _is_absolute_like(strategy_file):
        return strategy_file

    config_dir = _config_dir_from(config_path)
    if not config_dir:
        return strategy_file

    strategy_abs = os.path.normpath(strategy_file)
    strategy_dir = os.path.dirname(strategy_abs)
    if os.path.normcase(strategy_dir) != os.path.normcase(config_dir):
        return strategy_file

    return _path_basename(strategy_abs)
