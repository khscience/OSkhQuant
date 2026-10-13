"""Run an isolated packaged GUI and verify cleanup without touching other instances."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kh_app_identity import LEGACY_V21_QT_APP, LEGACY_V21_QT_ORG, QT_APP, QT_ORG, SETTINGS_FILE


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
                ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
                ("x", wintypes.DWORD), ("y", wintypes.DWORD),
                ("xsize", wintypes.DWORD), ("ysize", wintypes.DWORD),
                ("xchars", wintypes.DWORD), ("ychars", wintypes.DWORD),
                ("fill", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("show", wintypes.WORD), ("reserved_size", wintypes.WORD),
                ("reserved_ptr", ctypes.c_void_p), ("stdin", wintypes.HANDLE),
                ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]


class _BasicLimit(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD)]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [("basic", _BasicLimit), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [("times", ctypes.c_int64 * 4), ("faults", wintypes.DWORD),
                ("total", wintypes.DWORD), ("active", wintypes.DWORD),
                ("terminated", wintypes.DWORD)]


class ProcessJob:
    """Assign a suspended process to a kill-on-close job before it can spawn."""
    def __init__(self, command, env, cwd):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "CreateProcessW": ([wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                ctypes.POINTER(_StartupInfo), ctypes.POINTER(_ProcessInfo)], wintypes.BOOL),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
            "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (argtypes, restype) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = argtypes, restype
        self.job = self.api.CreateJobObjectW(None, None)
        self.process = None
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _ProcessInfo()
        try:
            limits = _ExtendedLimit()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            self._check(self.api.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            startup = _StartupInfo()
            startup.cb = ctypes.sizeof(startup)
            startup.flags = 1  # STARTF_USESHOWWINDOW; SW_HIDE is zero
            environment = ctypes.create_unicode_buffer("\0".join(
                f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].upper())
            ) + "\0")
            flags = 0x4 | 0x400 | 0x08000000  # SUSPENDED | UNICODE_ENVIRONMENT | NO_WINDOW
            self._check(self.api.CreateProcessW(None, ctypes.create_unicode_buffer(subprocess.list2cmdline(command)),
                        None, None, False, flags, environment, str(cwd), ctypes.byref(startup), ctypes.byref(info)))
            self.process, self.pid = info.process, int(info.pid)
            self._check(self.api.AssignProcessToJobObject(self.job, self.process))
            if self.api.ResumeThread(info.thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            if info.process:
                self.api.TerminateProcess(info.process, 1)
                self.api.CloseHandle(info.process)
                self.process = None
            self.api.CloseHandle(self.job)
            self.job = None
            raise
        finally:
            if info.thread:
                self.api.CloseHandle(info.thread)

    @staticmethod
    def _check(ok):
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def exit_code(self):
        code = wintypes.DWORD()
        self._check(self.api.GetExitCodeProcess(self.process, ctypes.byref(code)))
        return None if code.value == 259 else code.value

    def active_count(self):
        accounting = _Accounting()
        self._check(self.api.QueryInformationJobObject(self.job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None))
        return int(accounting.active)

    def close(self):
        if not self.job:
            return 0
        try:
            self._check(self.api.TerminateJobObject(self.job, 0))
            deadline = time.monotonic() + 5
            while self.active_count() and time.monotonic() < deadline:
                time.sleep(0.05)
            remaining = self.active_count()
            if remaining:
                raise RuntimeError(f"Smoke process cleanup incomplete: {remaining} processes")
            return remaining
        finally:
            self.api.CloseHandle(self.job)
            self.job = None
            if self.process:
                self.api.CloseHandle(self.process)
                self.process = None


def registry_fingerprint():
    """Only fingerprints leave memory; registry values may contain user tokens."""
    import winreg
    digest = hashlib.sha256()

    def visit(path):
        digest.update(path.encode("utf-8"))
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ)
        except FileNotFoundError:
            digest.update(b"<absent>")
            return
        with key:
            subkeys, values, _ = winreg.QueryInfoKey(key)
            for value in sorted((winreg.EnumValue(key, i) for i in range(values)), key=lambda entry: entry[0]):
                digest.update(repr(value).encode("utf-8"))
            for name in sorted(winreg.EnumKey(key, i) for i in range(subkeys)):
                visit(path + "\\" + name)

    for organization, application in ((QT_ORG, QT_APP), (LEGACY_V21_QT_ORG, LEGACY_V21_QT_APP)):
        visit("Software\\" + organization + "\\" + application)
    return digest.hexdigest()


def profile_fingerprints():
    path = Path(SETTINGS_FILE)
    return {"os_settings": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}


def run_smoke(exe, seconds):
    from PyInstaller.archive.readers import CArchiveReader
    exe = Path(exe).resolve(strict=True)
    if "smoke_runtime" not in CArchiveReader(str(exe)).toc:
        raise RuntimeError("安装包缺少隔离运行钩子，请使用更新后的 spec 重新构建")
    run_id = uuid.uuid4().hex
    root = Path(tempfile.mkdtemp(prefix=f"khos_smoke_{run_id}_"))
    (root / "run-id.txt").write_text(run_id, encoding="utf-8")
    env = dict(os.environ)
    env.update(KHQUANT_SMOKE_ROOT=str(root), KHQUANT_SMOKE_RUN_ID=run_id,
               PYTHONHASHSEED="0", QT_QPA_PLATFORM="offscreen",
               KHQUANT_GUI_TEST_SKIP_DISCLAIMER="1")
    before_registry, before_profile = registry_fingerprint(), profile_fingerprints()
    result = {"exe": str(exe), "run_id": run_id, "passed": False, "root": str(root)}
    job = None
    started = time.monotonic()
    try:
        job = ProcessJob([str(exe)], env, exe.parent)
        result["pid"] = job.pid
        log = root / "local/KhQuantOS/logs/app.log"
        deadline = started + seconds
        while time.monotonic() < deadline:
            code = job.exit_code()
            if code is not None:
                raise RuntimeError(f"主窗口就绪前进程退出，退出码 {code}")
            if log.is_file() and "主窗口创建成功" in log.read_text(encoding="utf-8", errors="replace"):
                break
            time.sleep(0.25)
        else:
            raise RuntimeError(f"{seconds} 秒内未生成主窗口就绪日志")
        content = log.read_text(encoding="utf-8", errors="replace")
        if any(word in content for word in ("Traceback", "ModuleNotFoundError", "No module named")):
            raise RuntimeError("打包版启动日志包含异常")
        runtime = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
        if runtime["run_id"] != run_id or runtime["settings_format"] != "INI":
            raise RuntimeError("隔离运行钩子回执不匹配")
        if not list((root / "qsettings").rglob("*.ini")):
            raise RuntimeError("未生成隔离的界面设置 INI")
        time.sleep(1)
        if job.exit_code() is not None:
            raise RuntimeError("主窗口创建后进程意外退出")
        result["startup_seconds"] = round(time.monotonic() - started, 3)
        result["active_before_cleanup"] = job.active_count()
        result["passed"] = True
    except Exception as error:
        result["error"] = str(error)
    finally:
        if job is not None:
            try:
                result["active_after_cleanup"] = job.close()
            except Exception as error:
                result["passed"], result["cleanup_error"] = False, str(error)
        result["registry_unchanged"] = registry_fingerprint() == before_registry
        result["profile_settings_unchanged"] = profile_fingerprints() == before_profile
        if not result["registry_unchanged"] or not result["profile_settings_unchanged"]:
            result["passed"] = False
        (root / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("冒烟报告：", root / "report.json")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True)
    parser.add_argument("--seconds", type=int, default=40)
    args = parser.parse_args()
    if os.name != "nt" or not 1 <= args.seconds <= 300:
        parser.error("Windows only; seconds must be 1..300")
    raise SystemExit(run_smoke(args.exe, args.seconds))
