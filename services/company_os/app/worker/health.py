"""Worker container health: live process and recent successful database poll."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from threading import get_ident


def path() -> Path:
    return Path(os.environ.get("COS_WORKER_HEALTH_PATH") or Path(tempfile.gettempdir()) / "supymem-worker-health.json")


def _windows_process_started_at(pid: int) -> int | None:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        return None
    try:
        if kernel.WaitForSingleObject(handle, 0) != 258:
            return None
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        return created.dwHighDateTime << 32 | created.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def write(state: str) -> None:
    target = path()
    temporary = target.with_suffix(f".{os.getpid()}.{get_ident()}.tmp")
    data = {"state": state, "pid": os.getpid(), "at": time.time()}
    if sys.platform == "win32":
        data["process_started_at"] = _windows_process_started_at(os.getpid())
    temporary.write_text(json.dumps(data))
    temporary.replace(target)


def healthy() -> bool:
    from app.core.local_mode import is_local

    if is_local() and os.environ.get("SUPYMEM_SINGLE_PROCESS", "").strip() == "1":
        from app.core.local_worker import is_running

        return is_running()
    try:
        data = json.loads(path().read_text())
        age = time.time() - float(data["at"])
        ttl = max(10, min(int(os.environ.get("COS_WORKER_HEALTH_MAX_AGE_S", "60")), 300))
        if data["state"] != "running" or not 0 <= age <= ttl or int(data["pid"]) <= 0:
            return False
        if sys.platform == "win32":
            started = _windows_process_started_at(int(data["pid"]))
            return started is not None and started == data.get("process_started_at")
        os.kill(int(data["pid"]), 0)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


if __name__ == "__main__":
    sys.exit(0 if healthy() else 1)
