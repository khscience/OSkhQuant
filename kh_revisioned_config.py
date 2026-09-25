# coding: utf-8
"""通用配置文件的跨进程锁、revision 与 compare-and-swap。

该模块属于主程序通用能力，不能依赖仅在私有 Windows 发行链路中安装的
``kh_bigqmt_bridge``。大QMT桥接内部保留自己的 Python 3.6 兼容实现。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping


class ConfigRevisionConflict(RuntimeError):
    code = "config_revision_conflict"
    retryable = True

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = int(expected)
        self.actual = int(actual)
        super().__init__(
            f"配置已被其他进程更新（期望 revision={self.expected}，实际={self.actual}）"
        )


class RevisionedConfigStore:
    def __init__(self, path: str | Path, *, lock_timeout_seconds: float = 10.0) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.lock_timeout_seconds = max(0.1, float(lock_timeout_seconds))

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.lock_timeout_seconds
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            except FileExistsError:
                try:
                    # 进程异常退出后不让孤儿锁永久阻塞设置保存。
                    if time.time() - self.lock_path.stat().st_mtime > 120:
                        self.lock_path.unlink()
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待配置锁超时: {self.lock_path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                os.close(descriptor)
            finally:
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass

    def _read_unlocked(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def read(self) -> dict[str, object]:
        with self.lock():
            return self._read_unlocked()

    def merge(
        self,
        updates: Mapping[str, object],
        *,
        expected_revision: int | None = None,
        allowed_fields: frozenset[str] | None = None,
    ) -> dict[str, object]:
        with self.lock():
            current = self._read_unlocked()
            actual = int(current.get("config_revision") or 0)
            if expected_revision is not None and int(expected_revision) != actual:
                raise ConfigRevisionConflict(int(expected_revision), actual)
            for key, value in updates.items():
                if allowed_fields is None or key in allowed_fields:
                    current[str(key)] = value
            current["config_revision"] = actual + 1
            self._write_unlocked(current)
            return current

    def _write_unlocked(self, value: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".config-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = handle.name
                json.dump(dict(value), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = ""
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


__all__ = ["ConfigRevisionConflict", "RevisionedConfigStore"]
