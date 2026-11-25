"""Keep local processes and maintenance mutually exclusive across data replacement."""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack, contextmanager

from app.core.local_mode import is_local

if os.name != "nt":
    import fcntl


def open_private(path, flags, mode=0o600):
    if os.name == "nt":
        from app.core.local_windows import private_open

        return private_open(path, flags)
    return os.open(path, flags | os.O_NOFOLLOW, mode)


def check_private(stream):
    metadata = os.fstat(stream.fileno())
    if not stat.S_ISREG(metadata.st_mode) or (os.name != "nt" and metadata.st_mode & 0o077):
        raise ValueError("Your local file must be private and regular. Check your data-folder permissions, then retry.")


@contextmanager
def file_lock(stream, *, exclusive=True, blocking=True):
    if os.name == "nt":
        from app.core.local_windows import lock_file

        lock_file(stream, exclusive=exclusive, blocking=blocking)
    else:
        fcntl.flock(stream.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if blocking else fcntl.LOCK_NB))
    try:
        yield
    finally:
        if os.name == "nt":
            lock_file(stream, unlock=True)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class LocalProcessBusy(ValueError):
    pass


@contextmanager
def process_lock(*, exclusive: bool = False):
    if not is_local():
        yield
        return
    from app.config import data_root

    root = data_root().resolve()
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = open_private(root.with_name(f".{root.name}-process.lock"), os.O_CREAT | os.O_RDWR)
    with os.fdopen(descriptor, "rb") as lock, ExitStack() as locks:
        check_private(lock)
        try:
            locks.enter_context(file_lock(lock, exclusive=exclusive, blocking=False))
        except BlockingIOError:
            message = ("Stop your app and worker, then retry your restore or upgrade." if exclusive else
                       "Your data is being restored or upgraded. Wait for maintenance to finish, then restart your app and worker.")
            raise LocalProcessBusy(message) from None
        yield
