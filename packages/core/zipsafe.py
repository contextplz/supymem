"""Zip extraction that refuses path traversal (zip-slip)."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path


class ZipSlipError(ValueError):
    """Archive member would write outside the destination directory."""


def is_safe_member(name: str) -> bool:
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    parts = Path(name.replace("\\", "/")).parts
    if any(p == ".." for p in parts):
        return False
    return True


def safe_extract(archive: zipfile.ZipFile, dest: Path) -> list[Path]:
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for info in archive.infolist():
        name = info.filename
        if name.endswith("/"):
            if not is_safe_member(name):
                raise ZipSlipError(name)
            continue
        if not is_safe_member(name):
            raise ZipSlipError(name)
        target = (dest / name.replace("\\", "/")).resolve()
        if target != dest and not str(target).startswith(str(dest) + os.sep):
            raise ZipSlipError(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as src, open(target, "wb") as out:
            out.write(src.read())
        written.append(target)
    return written
