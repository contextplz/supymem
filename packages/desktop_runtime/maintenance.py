"""Bounded pre-start maintenance and a desktop data-folder ownership lock."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

MAINTENANCE_TIMEOUT_SECONDS = 120


@contextmanager
def instance_lock(root: Path):
    from app.cli_local import LocalMaintenanceError
    from app.core.local_process import check_private, file_lock, open_private

    root = root.resolve()
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = open_private(root.with_name(f".{root.name}-desktop.lock"), os.O_CREAT | os.O_RDWR)
    with os.fdopen(descriptor, "rb") as lock:
        check_private(lock)
        try:
            with file_lock(lock, blocking=False):
                yield
        except BlockingIOError:
            raise LocalMaintenanceError("Your data folder is already open. Close your other app window, then reopen this app.") from None


def run() -> None:
    from desktop_runtime.__main__ import REPO_ROOT
    from app.cli_local import LocalMaintenanceError
    from app.core.local_process import LocalProcessBusy, process_lock

    try:
        with process_lock(exclusive=True):
            pass
    except LocalProcessBusy:
        raise LocalMaintenanceError("Your data folder is already open. Close your other app window, then reopen this app.") from None
    print("Checking your saved data before opening your app.", file=sys.stderr, flush=True)
    runtime = Path(sys.executable).parent
    site = runtime / "Lib/site-packages" if sys.platform == "win32" else runtime.parent / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    try:
        result = subprocess.run([sys.executable, "-I", "-c",
                                 "import sys; from pathlib import Path; root=Path(sys.argv[1]); "
                                 "sys.path[:0]=[str(root/'packages'),str(root/'services/company_os'),sys.argv[2]]; "
                                 "from desktop_runtime.maintenance import _worker; _worker()", str(REPO_ROOT), str(site)],
                                timeout=MAINTENANCE_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        from app.db.local_sqlite import recover_sqlite
        from app.db.session import resolve_database_url

        with process_lock(exclusive=True):
            restored = recover_sqlite(resolve_database_url())
        message = ("Your automatic backup was restored. " if restored else "Your saved work has not been changed. ")
        raise LocalMaintenanceError(
            "Preparing your saved data took too long. " + message +
            "Check free disk space and your data-folder permissions. Reopen your app to retry."
        ) from None
    if result.returncode:
        raise LocalMaintenanceError("Your saved data could not be prepared. Follow the restore or update step shown above, then reopen your app.")


def _worker() -> None:
    from desktop_runtime.__main__ import _ensure_service_on_path

    _ensure_service_on_path("core")
    from app.cli_local import LocalMaintenanceError
    from app.core.local_data import apply_pending
    from app.core.local_process import LocalProcessBusy, process_lock
    from app.db.local_sqlite import migrate_sqlite
    from app.db.session import resolve_database_url

    try:
        apply_pending()
        with process_lock(exclusive=True):
            migrate_sqlite(resolve_database_url(), progress=lambda message: print(message, file=sys.stderr, flush=True))
        print("Your saved data is ready. Opening your app.", file=sys.stderr, flush=True)
    except LocalProcessBusy:
        raise SystemExit("Your data folder is already open. Close your other app window, then reopen this app.") from None
    except LocalMaintenanceError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit("Your saved data could not be prepared. Check free disk space and your data-folder permissions, then reopen your app.") from None


if __name__ == "__main__":
    _worker()
