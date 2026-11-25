"""Stop an owned sidecar if its desktop parent disappears."""
from __future__ import annotations

import os
import sys
import threading
import time


def parent_alive(parent: int) -> bool:
    if sys.platform != "win32":
        os.kill(parent, 0)
        return True
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, parent)
    if not handle:
        return ctypes.get_last_error() != 87
    try:
        code = wintypes.DWORD()
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel.CloseHandle(handle)


def exit_when_parent_dies(poll_s: float = 2.0, exit=os._exit) -> None:
    """Do not leave an orphan holding a port after your desktop app stops."""
    try:
        parent = int(os.environ.get("SUPYMEM_PARENT_PID", ""))
    except ValueError:
        return

    def watch() -> None:
        while True:
            time.sleep(poll_s)
            try:
                if not parent_alive(parent):
                    exit(0)
                    return
            except ProcessLookupError:
                exit(0)
                return
            except OSError:
                pass

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()
