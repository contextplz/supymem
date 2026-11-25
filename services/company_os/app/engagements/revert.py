"""Undo for an engagement (36 §7).

Every engagement records a revert hint before any work happens, so "this cannot
be undone" is something the founder hears *before* the work, not after. The hint
is stored as JSON on the row and rendered to one plain sentence for the founder.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NO_CHANGES = "no changes were made"

KINDS = ("none", "files", "config", "branch")


@dataclass(frozen=True)
class RevertHint:
    kind: str = "none"
    paths: list[str] = field(default_factory=list)
    backup_dir: str | None = None
    branch: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "kind": self.kind,
                "paths": list(self.paths),
                "backup_dir": self.backup_dir,
                "branch": self.branch,
            }
        )

    def human(self) -> str:
        if self.kind == "none":
            return NO_CHANGES
        if self.kind == "branch":
            return f"the branch {self.branch}"
        if self.kind == "config":
            return "the saved copy of your settings from before the change"
        return f"{len(self.paths)} file(s) this engagement created"

    @property
    def revertible(self) -> bool:
        if self.kind == "none":
            return False
        if self.kind == "config":
            return bool(self.backup_dir) and bool(self.paths)
        if self.kind == "files":
            return bool(self.paths)
        return bool(self.branch)


def load_hint(raw: str | None) -> RevertHint:
    if not raw:
        return RevertHint()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # A hint written as prose is still a hint; treat it as un-machine-actionable.
        return RevertHint(kind="none")
    if not isinstance(data, dict):
        return RevertHint()
    kind = str(data.get("kind") or "none")
    return RevertHint(
        kind=kind if kind in KINDS else "none",
        paths=[str(p) for p in (data.get("paths") or [])],
        backup_dir=data.get("backup_dir"),
        branch=data.get("branch"),
    )


def snapshot(paths: list[Path], backup_dir: Path) -> RevertHint:
    """Copy the files we are about to touch, before touching them."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    kept: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        shutil.copy2(path, backup_dir / path.name)
        kept.append(str(path))
    if not kept:
        return RevertHint()
    return RevertHint(kind="config", paths=kept, backup_dir=str(backup_dir))


@dataclass(frozen=True)
class RevertResult:
    ok: bool
    detail: str
    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "restored": list(self.restored),
            "removed": list(self.removed),
        }


def apply(hint: RevertHint) -> RevertResult:
    if hint.kind == "none":
        return RevertResult(True, NO_CHANGES)
    if not hint.revertible:
        return RevertResult(False, "there is nothing recorded to undo")

    if hint.kind == "config":
        backup = Path(hint.backup_dir or "")
        restored: list[str] = []
        missing: list[str] = []
        for raw in hint.paths:
            target = Path(raw)
            source = backup / target.name
            if not source.is_file():
                missing.append(target.name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            restored.append(str(target))
        if missing:
            return RevertResult(
                False,
                f"the saved copy is gone for: {', '.join(missing)}",
                restored=restored,
            )
        return RevertResult(True, "put your settings back", restored=restored)

    if hint.kind == "files":
        removed: list[str] = []
        for raw in hint.paths:
            path = Path(raw)
            if path.is_file():
                path.unlink()
                removed.append(str(path))
            elif path.is_dir():
                shutil.rmtree(path)
                removed.append(str(path))
        return RevertResult(True, "removed what this engagement created", removed=removed)

    # A branch is the founder's to delete; we never touch their git history.
    return RevertResult(
        True,
        f"nothing of ours to undo, delete the branch {hint.branch} when you're ready",
    )
