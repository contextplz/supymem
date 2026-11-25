"""Forced desktop updates halt hosted default locally (no website round-trip).

The Tauri updater writes ``update_status.json`` after checking CloudFront
``/downloads/latest.json``. If that snapshot says a **forced** update is
available and this build is still older, Company OS default is denied until
they install. Optional updates never halt hosted use. BYOK skips this gate.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from llm_gateway.hosted import HostedUpdateRequired

STATUS_FILENAME = "update_status.json"


def _conventional_data_dir() -> Path:
    """Same fallback as the Tauri shell when COMPANY_OS_DATA_DIR is unset."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "CompanyOS"
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return Path(base) / "CompanyOS"
    xdg = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return Path(xdg) / "companyos"


def _parse_dotted_version(raw: str) -> tuple[int, int, int]:
    s = (raw or "").strip().lstrip("vV")
    parts = s.split(".")

    def _num(idx: int) -> int:
        if idx >= len(parts):
            return 0
        digits = "".join(ch for ch in parts[idx] if ch.isdigit())
        return int(digits) if digits else 0

    return _num(0), _num(1), _num(2)


def _version_lt(current: str, other: str) -> bool:
    return _parse_dotted_version(current) < _parse_dotted_version(other)


def status_path() -> Path:
    override = (os.environ.get("COMPANY_OS_UPDATE_STATUS_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    data = (os.environ.get("COMPANY_OS_DATA_DIR") or "").strip()
    if data:
        return Path(data).expanduser() / STATUS_FILENAME
    return _conventional_data_dir() / STATUS_FILENAME


def persist_update_status(result: Mapping[str, Any], path: Path | None = None) -> Path:
    dest = path or status_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "available": bool(result.get("available")),
        "current_version": str(result.get("current_version") or ""),
        "version": result.get("version"),
        "force": bool(result.get("force")),
        "mandatory": bool(result.get("mandatory")),
        "can_defer": bool(result.get("can_defer", not result.get("force"))),
        "endpoint": str(result.get("endpoint") or ""),
    }
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return dest


def load_update_status(path: Path | None = None) -> dict[str, Any] | None:
    dest = path or status_path()
    if not dest.is_file():
        return None
    try:
        raw = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _app_version(explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    return (os.environ.get("COMPANY_OS_APP_VERSION") or "").strip()


def enforce_forced_update(
    *,
    byok: bool = False,
    current_version: str | None = None,
    now_status: Mapping[str, Any] | None = None,
) -> None:
    """Deny hosted default when a forced update is pending. Never performs HTTP."""
    if byok:
        return
    status = dict(now_status) if now_status is not None else load_update_status()
    if not status:
        return
    if not status.get("available") or not status.get("force"):
        return
    current = _app_version(current_version) or str(status.get("current_version") or "")
    advertised = str(status.get("version") or "").strip()
    if current and advertised and not _version_lt(current, advertised):
        return
    raise HostedUpdateRequired()


__all__ = [
    "STATUS_FILENAME",
    "enforce_forced_update",
    "load_update_status",
    "persist_update_status",
    "status_path",
]
