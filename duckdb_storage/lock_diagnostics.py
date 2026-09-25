# -*- coding: utf-8 -*-
"""DuckDB 数据目录占用诊断与进程归属识别。

诊断基于操作系统当前打开的文件句柄，不会主动打开任何 DuckDB 文件，
因此不会为了“检查占用”反过来制造新的数据库锁。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import os
import re
import shlex
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class ProcessClassification:
    """看海量化进程归属及停止策略。"""

    module: str
    protected: bool
    can_bulk_stop: bool
    reason: str = ""


@dataclass
class DatabaseOccupancyRecord:
    """一个进程在指定数据目录内持有的 DuckDB 文件。"""

    pid: int
    module: str
    process_name: str
    executable: str
    command_line: str
    create_time: Optional[float]
    database_files: List[str] = field(default_factory=list)
    protected: bool = True
    can_bulk_stop: bool = False
    protection_reason: str = ""

    @property
    def started_at(self) -> str:
        if not self.create_time:
            return "未知"
        try:
            return datetime.fromtimestamp(self.create_time).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return "未知"


@dataclass
class OccupancyScanResult:
    """一次占用扫描的结果和可见性边界。"""

    data_root: str
    records: List[DatabaseOccupancyRecord] = field(default_factory=list)
    total_processes: int = 0
    skipped_processes: int = 0
    scanned_processes: int = 0
    inaccessible_processes: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def successfully_checked_processes(self) -> int:
        """真正完成句柄读取的进程数；无法访问/超时项是已纳入数的子集。"""
        return max(int(self.scanned_processes) - int(self.inaccessible_processes), 0)


def _normalized_command(command_line: Sequence[str] | str, executable: str = "") -> str:
    if isinstance(command_line, str):
        command_text = command_line
    else:
        command_text = " ".join(str(part) for part in command_line or [])
    return f"{executable} {command_text}".replace("\\", "/").lower()


def classify_database_process(
    command_line: Sequence[str] | str,
    executable: str = "",
    *,
    pid: Optional[int] = None,
    current_pid: Optional[int] = None,
) -> ProcessClassification:
    """根据命令行把数据库持有者归属到用户可理解的模块。"""
    text = f" {_normalized_command(command_line, executable)} "
    if pid is not None and current_pid is not None and int(pid) == int(current_pid):
        return ProcessClassification(
            "当前看海量化进程",
            protected=True,
            can_bulk_stop=False,
            reason="可能包含数据管理或桌面回测线程；不能结束自身，请先正常停止活动任务",
        )

    # 与 CS 版共用数据目录时，CS 主程序可能正在补数或运行策略，不能批量结束。
    if "看海量化回测平台.exe" in text:
        return ProcessClassification(
            "看海量化（CS 版）",
            protected=True,
            can_bulk_stop=False,
            reason="另一个看海量化程序正在使用这个数据目录，请在该程序里正常停止任务",
        )

    if any(marker in text for marker in ("baostock_import", "tushare_import")):
        return ProcessClassification("数据导入/补充", False, True, "已识别的数据写入任务")

    if any(marker in text for marker in ("run_duckdb_viewer.py",)):
        return ProcessClassification(
            "另一数据管理窗口",
            True,
            False,
            "另一窗口可能有未保存或正在查看的内容",
        )

    if any(marker in text for marker in ("guikhquant.py", "khquant.exe")):
        return ProcessClassification(
            "另一看海量化主程序",
            True,
            False,
            "结束主程序可能中断用户正在执行的任务",
        )

    if "python" in text:
        return ProcessClassification(
            "其他 Python 程序",
            True,
            False,
            "无法确认用途，禁止批量结束",
        )
    return ProcessClassification("未知外部程序", True, False, "无法确认用途，禁止批量结束")


def _is_within_data_root(path: str, data_root: str) -> bool:
    try:
        normalized_path = os.path.normcase(os.path.abspath(path))
        normalized_root = os.path.normcase(os.path.abspath(data_root))
        return os.path.commonpath((normalized_path, normalized_root)) == normalized_root
    except (OSError, ValueError, TypeError):
        return False


def _is_duckdb_handle(path: str, data_root: str) -> bool:
    if not path or not _is_within_data_root(path, data_root):
        return False
    lowered = os.path.normcase(path).lower()
    return lowered.endswith((".db", ".duckdb", ".db.wal", ".duckdb.wal"))


_SENSITIVE_COMMAND_KEYS = frozenset(
    {
        "api-key",
        "apikey",
        "access-token",
        "auth-token",
        "refresh-token",
        "token",
        "tushare-token",
        "password",
        "passwd",
        "client-secret",
        "secret",
        "credential",
        "credentials",
        "authorization",
    }
)


def _normalized_secret_key(value: str) -> str:
    return str(value or "").strip().lstrip("-/").lower().replace("_", "-")


def _redact_inline_secrets(value: str) -> str:
    text = str(value or "")
    text = re.sub(
        r"(?i)(\bBearer\s+)([^\s\"']+)",
        r"\1***",
        text,
    )
    text = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@",
        r"\1***:***@",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:api[-_]?key|access[-_]?token|auth[-_]?token|token|"
        r"password|passwd|client[-_]?secret|secret)=)([^&\s]+)",
        r"\1***",
        text,
    )
    return text


def _redact_command_text(command_line: str) -> str:
    sensitive_names = "|".join(
        sorted((re.escape(item) for item in _SENSITIVE_COMMAND_KEYS), key=len, reverse=True)
    ).replace(r"\-", "[-_]")
    option_pattern = re.compile(
        rf"(?i)(?P<prefix>(?<!\S)(?:--?|/)?(?:{sensitive_names})"
        rf"(?:\s*=\s*|\s+))(?P<value>\"[^\"]*\"|'[^']*'|\S+)"
    )
    redacted = option_pattern.sub(lambda match: match.group("prefix") + "***", command_line)
    return _redact_inline_secrets(redacted)


def _redact_command_parts(parts: Sequence[str]) -> List[str]:
    redacted = []
    redact_next = False
    for raw_part in parts or []:
        part = str(raw_part)
        if redact_next:
            redacted.append("***")
            redact_next = False
            continue

        key, separator, _value = part.partition("=")
        normalized_key = _normalized_secret_key(key)
        if separator and normalized_key in _SENSITIVE_COMMAND_KEYS:
            redacted.append(f"{key}=***")
            continue
        if not separator and _normalized_secret_key(part) in _SENSITIVE_COMMAND_KEYS:
            redacted.append(part)
            redact_next = True
            continue
        redacted.append(_redact_inline_secrets(part))
    return redacted


def _safe_command_line(parts: Sequence[str] | str) -> str:
    if isinstance(parts, str):
        return _redact_command_text(parts)
    safe_parts = _redact_command_parts(parts)
    try:
        return subprocess_list2cmdline(safe_parts)
    except Exception:
        return " ".join(safe_parts)


def subprocess_list2cmdline(parts: Sequence[str]) -> str:
    """使用 Windows 可读形式展示命令行，同时兼容非 Windows 测试。"""
    if os.name == "nt":
        import subprocess

        return subprocess.list2cmdline([str(part) for part in parts or []])
    return " ".join(shlex.quote(str(part)) for part in parts or [])


def _process_info_value(process: Any, name: str, default: Any = None) -> Any:
    info = getattr(process, "info", None) or {}
    if name in info and info.get(name) is not None:
        return info.get(name)
    value = getattr(process, name, None)
    if callable(value):
        return value()
    return default if value is None else value


def scan_database_occupancy(
    data_root: str,
    *,
    psutil_module: Any = None,
    processes: Optional[Iterable[Any]] = None,
    current_pid: Optional[int] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> OccupancyScanResult:
    """扫描当前持有 ``data_root`` 内 DuckDB 文件句柄的进程。

    ``processes`` 和 ``psutil_module`` 可注入，便于离线测试。扫描只读取系统
    进程句柄，不会连接或写入数据库。
    """
    root = os.path.abspath(str(data_root or ""))
    result = OccupancyScanResult(data_root=root)
    if not data_root or not os.path.isdir(root):
        result.errors.append("数据目录不存在或不可访问")
        return result

    if psutil_module is None:
        try:
            import psutil as psutil_module  # type: ignore[no-redef]
        except ImportError:
            result.errors.append("缺少 psutil，无法读取系统文件句柄")
            return result

    if current_pid is None:
        current_pid = os.getpid()
    if processes is None:
        processes = psutil_module.process_iter(
            ["pid", "name", "exe", "cmdline", "create_time"]
        )

    access_errors = tuple(
        error_type
        for error_type in (
            getattr(psutil_module, "NoSuchProcess", None),
            getattr(psutil_module, "AccessDenied", None),
            getattr(psutil_module, "ZombieProcess", None),
            getattr(psutil_module, "TimeoutExpired", None),
        )
        if isinstance(error_type, type)
    ) or (Exception,)

    for process in processes:
        if should_stop is not None and should_stop():
            break
        result.scanned_processes += 1
        try:
            open_files = process.open_files() or []
            database_files = sorted(
                {
                    os.path.normpath(str(getattr(item, "path", item)))
                    for item in open_files
                    if _is_duckdb_handle(str(getattr(item, "path", item)), root)
                },
                key=str.lower,
            )
            if not database_files:
                continue

            def process_value(name: str, default: Any = None) -> Any:
                try:
                    return _process_info_value(process, name, default)
                except access_errors:
                    return default

            pid = int(process_value("pid", getattr(process, "pid", 0)) or 0)
            process_name = str(process_value("name", "") or "")
            executable = str(process_value("exe", "") or "")
            command_parts = process_value("cmdline", []) or []
            create_time_value = process_value("create_time", None)
            try:
                create_time = float(create_time_value) if create_time_value else None
            except (TypeError, ValueError):
                create_time = None
            classification = classify_database_process(
                command_parts,
                executable,
                pid=pid,
                current_pid=current_pid,
            )
            result.records.append(
                DatabaseOccupancyRecord(
                    pid=pid,
                    module=classification.module,
                    process_name=process_name or os.path.basename(executable) or "未知",
                    executable=executable,
                    command_line=_safe_command_line(command_parts),
                    create_time=create_time,
                    database_files=database_files,
                    protected=classification.protected,
                    can_bulk_stop=classification.can_bulk_stop,
                    protection_reason=classification.reason,
                )
            )
        except access_errors:
            result.inaccessible_processes += 1
        except (OSError, RuntimeError, ValueError) as exc:
            result.inaccessible_processes += 1
            if len(result.errors) < 5:
                result.errors.append(f"PID {getattr(process, 'pid', '?')}: {exc}")

    result.records.sort(
        key=lambda item: (
            item.pid != current_pid,
            item.protected,
            item.module,
            item.pid,
        )
    )
    return result


def inspect_process_identity(
    pid: int,
    *,
    psutil_module: Any = None,
    current_pid: Optional[int] = None,
) -> Tuple[Optional[DatabaseOccupancyRecord], str]:
    """结束进程前重新读取身份，防止扫描后 PID 被系统复用。"""
    if psutil_module is None:
        try:
            import psutil as psutil_module  # type: ignore[no-redef]
        except ImportError:
            return None, "缺少 psutil，无法复核进程身份"
    try:
        process = psutil_module.Process(int(pid))
        executable = str(process.exe() or "")
        command_parts = process.cmdline() or []
        process_name = str(process.name() or "")
        create_time = float(process.create_time())
        classification = classify_database_process(
            command_parts,
            executable,
            pid=int(pid),
            current_pid=os.getpid() if current_pid is None else current_pid,
        )
        return DatabaseOccupancyRecord(
            pid=int(pid),
            module=classification.module,
            process_name=process_name or os.path.basename(executable) or "未知",
            executable=executable,
            command_line=_safe_command_line(command_parts),
            create_time=create_time,
            protected=classification.protected,
            can_bulk_stop=classification.can_bulk_stop,
            protection_reason=classification.reason,
        ), ""
    except Exception as exc:
        return None, str(exc)
