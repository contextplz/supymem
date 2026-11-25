"""Private local failure origins, without messages, inputs or credentials."""
import json
import logging
import os
import sys
from datetime import datetime, timezone

_LOCAL_CODES = frozenset({"local_job_failed", "coding_preview_failed", "native_product_preview_unavailable", "coding_preview_stop_unconfirmed",
    "coding_checks_unavailable", "coding_source_changed", "chat_action_failed", "chat_reply_timeout",
    "chat_reply_cancelled", "chat_reply_empty", "chat_reply_lease_lost"})
_CONNECTION_ERROR_TYPES = frozenset({"APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout"})


def write_error(error: BaseException | None, *, code: str = "chat_action_failed") -> None:
    try:
        from app.core.local_mode import is_local
    except ImportError:
        return
    if not is_local():
        return
    try:
        from app.config import data_root
        from app.core.local_process import check_private, file_lock, open_private
        from desktop_runtime.cu_public_errors import PUBLIC_EXCEPTION_TYPES, public_failure_code

        frames, seen = [], set()
        current = error
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            trace = current.__traceback__
            while trace is not None:
                frames.append(f"{trace.tb_frame.f_globals.get('__name__', '<unknown>')}:{trace.tb_lineno}")
                trace = trace.tb_next
            current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        if not frames:
            caller = sys._getframe(1)
            frames.append(f"{caller.f_globals.get('__name__', '<unknown>')}:{caller.f_lineno}")
        error_type = type(error).__name__ if error is not None else "RuntimeError"
        entry = {"time": datetime.now(timezone.utc).isoformat(),
                 "error_type": error_type if error_type in PUBLIC_EXCEPTION_TYPES or error_type in _CONNECTION_ERROR_TYPES else "Exception",
                 "frames": frames,
                 "code": code if isinstance(code, str) and code in _LOCAL_CODES else public_failure_code(code, "chat_action_failed")}
        directory = data_root() / "logs"
        if directory.is_symlink():
            raise OSError()
        directory.mkdir(mode=0o700, exist_ok=True)
        descriptor = open_private(directory / "errors.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "a", encoding="utf-8") as target:
            check_private(target)
            with file_lock(target):
                target.write(json.dumps(entry, separators=(",", ":")) + "\n")
                target.flush()
    except Exception:
        logging.getLogger(__name__).warning("local_diagnostics_unavailable")
