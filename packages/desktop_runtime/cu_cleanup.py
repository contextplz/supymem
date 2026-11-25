"""Content-free cleanup attribution. Never copy exception messages or payloads."""
import math
import errno

_PUBLIC_ERROR_TYPES = frozenset({"OSError", "PermissionError", "ProcessLookupError", "FileNotFoundError",
    "TimeoutError", "TimeoutExpired", "RuntimeError", "TypeError", "ValueError", "NameError", "AttributeError"})
_PUBLIC_ERRNOS = frozenset({errno.EPERM, errno.ESRCH, errno.EACCES, errno.EINVAL, errno.ENOENT,
    errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EIO, errno.ENOTEMPTY, errno.EBUSY})

def public_cleanup_error(error):
    kind = type(error).__name__
    result = {"error_type": kind if kind in _PUBLIC_ERROR_TYPES else "other_exception"}
    number = getattr(error, "errno", None)
    if type(number) is int and number in _PUBLIC_ERRNOS:
        result["error_errno"] = errno.errorcode[number]
    return result


PUBLIC_CLEANUP_CODES = frozenset({
    "qa_http_client_close_unconfirmed", "qa_proxy_close_unconfirmed", "qa_daemon_close_unconfirmed",
    "browser_cdp_close_unconfirmed", "browser_process_close_unconfirmed", "native_backend_close_unconfirmed",
    "qa_trial_cleanup_unconfirmed", "qa_form_fixture_cleanup_unconfirmed",
})


def public_cleanup_codes(values):
    if not isinstance(values, (tuple, list)):
        return []
    return sorted({code for code in values[:16] if isinstance(code, str) and code in PUBLIC_CLEANUP_CODES})


def cleanup_codes(error, fallback):
    return public_cleanup_codes(getattr(error, "cleanup_codes", None)) or public_cleanup_codes([fallback])


def public_cleanup_trace(values):
    if not isinstance(values, (tuple, list)):
        return []
    clean = []
    for row in values[-32:]:
        if (not isinstance(row, dict) or not isinstance(row.get("stage"), str)
                or row["stage"] not in {"group_signal", "group_wait", "parent_wait", "listener_probe", "dir_cleanup"}
                or not isinstance(row.get("outcome"), str) or row["outcome"] not in {"completed", "confirmed", "unconfirmed", "error"}):
            continue
        at, duration = row.get("at_monotonic"), row.get("duration_ms")
        if (type(at) not in {int, float} or not math.isfinite(at) or at < 0
                or type(duration) not in {int, float} or not math.isfinite(duration) or not 0 <= duration <= 60000):
            continue
        item = {"stage": row["stage"], "outcome": row["outcome"], "at_monotonic": at, "duration_ms": duration}
        if type(row.get("owned_group")) is int and row["owned_group"] > 0:
            item["owned_group"] = row["owned_group"]
        if type(row.get("group_retired")) is bool:
            item["group_retired"] = row["group_retired"]
        if row.get("outcome") == "error":
            if isinstance(row.get("error_type"), str) and row["error_type"] in _PUBLIC_ERROR_TYPES | {"other_exception"}:
                item["error_type"] = row["error_type"]
            if isinstance(row.get("error_errno"), str) and row["error_errno"] in {errno.errorcode[n] for n in _PUBLIC_ERRNOS}:
                item["error_errno"] = row["error_errno"]
        if isinstance(row.get("group_probe_reason"), str) and row["group_probe_reason"] in {"probe_permission_unavailable", "ownership_unconfirmed",
                "parent_not_exited", "kernel_inventory_unavailable", "kernel_group_nonempty", "kernel_group_empty", "listener_present", "listener_unknown"}:
            item["group_probe_reason"] = row["group_probe_reason"]
            count = row.get("group_member_count")
            if type(count) is int and 0 <= count <= 4096:
                item["group_member_count"] = count
        clean.append(item)
    return clean


def cleanup_trace(error):
    return public_cleanup_trace(getattr(error, "cleanup_trace", None))


class CleanupError(RuntimeError):
    def __init__(self, public_reason, codes):
        # The reason is supplied as a fixed literal by owned cleanup routes.
        super().__init__(public_reason)
        self.cleanup_codes = tuple(public_cleanup_codes(codes))


def darwin_group_members(group):
    """Complete bounded kernel membership; unknown never means empty."""
    import ctypes
    import sys
    if sys.platform != 'darwin' or type(group) is not int or group <= 1:
        raise RuntimeError('group_inventory_unavailable')
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    query = library.proc_listpids
    query.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    query.restype = ctypes.c_int
    members = (ctypes.c_int * 4096)()
    size = ctypes.sizeof(members)
    ctypes.set_errno(0)
    length = query(2, group, members, size)  # Apple PROC_PGRP_ONLY, byte count.
    if ctypes.get_errno() or length < 0 or length >= size or length % ctypes.sizeof(ctypes.c_int):
        raise RuntimeError('group_inventory_unavailable')
    pids = tuple(members[:length // ctypes.sizeof(ctypes.c_int)])
    if any(pid <= 0 for pid in pids):
        raise RuntimeError('group_inventory_unavailable')
    return pids
