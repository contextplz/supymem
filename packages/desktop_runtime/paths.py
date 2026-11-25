"""Resolve COMPANY_OS_DATA_DIR with OS-conventional fallbacks for packaged apps."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def data_root(*, create: bool = False) -> Path:
    """Return the writable data directory.

    Priority:
    1. COMPANY_OS_DATA_DIR (set by Tauri supervisor or operator)
    2. Repo-relative `data/` (dev / make company-os-dev)
    """
    override = os.environ.get("COMPANY_OS_DATA_DIR", "").strip()
    if override:
        path = Path(override).expanduser().resolve()
    else:
        path = repo_root() / "data"
    if create:
        path.mkdir(parents=True, exist_ok=True)
        (path / "ventures").mkdir(exist_ok=True)
        (path / "backups").mkdir(exist_ok=True)
    return path


def venture_workspace_root(workspace_slug: str, data_directory: Path) -> Path:
    from desktop_runtime.hosted import _is_local

    root = Path.home() / "Documents" / "Supymem" / "ventures"
    selected = root / workspace_slug
    if _is_local() and not (selected.exists() or selected.is_symlink()):
        return data_directory / "workspaces"
    return root


def os_conventional_app_data_dir() -> Path:
    """Platform app-data path used by the desktop shell (doc 90 §6).

    Prefers ``Supymem``. If only a leftover ``CompanyOS`` folder exists (older
    installs), keep using it so data is not orphaned. The Tauri supervisor
    migrates the folder on first launch of the renamed app.
    """
    home = Path.home()
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        new, legacy = support / "Supymem", support / "CompanyOS"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or str(home / "AppData" / "Roaming"))
        new, legacy = base / "Supymem", base / "CompanyOS"
    else:
        xdg = Path(os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share"))
        new, legacy = xdg / "supymem", xdg / "companyos"
    if new.exists() or not legacy.exists():
        return new
    return legacy
