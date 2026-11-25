"""Deterministic, local skill-pack ingestion."""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.skills.library import SkillLibrary, SkillRecord
from app.skills.license import decision_for
from app.skills.parse import parse_skill_file

_UNSAFE_PATTERNS = (
    (r"curl\s+[^|\n]+\|\s*(?:ba)?sh", "unsafe_shell_pipe"),
    (r"\beval\s*\(", "unsafe_eval"),
    (r"credential harvesting|harvest credentials", "credential_harvesting"),
    (r"evad(?:e|ing).{0,30}(?:rate limits|terms of service|tos)", "platform_evasion"),
)


@dataclass(frozen=True, slots=True)
class IngestReport:
    packs: int
    discovered: int
    parsed: int
    quarantined: int
    rejected_safety: int
    wall_ms: int


def _triggers(description: str) -> tuple[str, ...]:
    quoted = re.findall(r"""["']([^"']{3,120})["']""", description)
    use_when = re.findall(r"(?i)(?:use|activates?)\s+when\s+([^.;]+)", description)
    return tuple(dict.fromkeys(item.strip() for item in [*quoted, *use_when] if item.strip()))


def _included(path: Path, root: Path, include: set[str]) -> bool:
    if not include:
        return True
    resolved = path.resolve()
    for rel in include:
        base = (root / rel).resolve()
        if resolved == base or resolved.is_relative_to(base):
            return True
    return False


def _unsafe_reason(text: str) -> str | None:
    for pattern, reason in _UNSAFE_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL):
            return reason
    return None


def _category(path: Path, root: Path, metadata: dict[str, Any]) -> str:
    declared = metadata.get("category") or metadata.get("categories")
    if isinstance(declared, list) and declared:
        return str(declared[0])
    if declared:
        return str(declared)
    relative = path.relative_to(root)
    return relative.parts[-3] if len(relative.parts) > 2 else relative.parts[0]


def ingest_all(*, manifest_path: Path, rebuild: bool = False, library: SkillLibrary | None = None) -> IngestReport:
    """Ingest present packs. Missing packs are skipped so the command is repeatable offline."""
    started = time.monotonic()
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    vendor_root = manifest_path.parent
    store = library or SkillLibrary()
    limit = int(os.environ.get("SKILLS_CURATE_LIMIT", "0") or "0")
    discovered = parsed = quarantined = rejected_safety = 0
    packs = 0

    for pack in manifest.get("packs", []):
        skills_root = pack.get("skills_root")
        if not skills_root:
            continue
        root = vendor_root / str(pack.get("dir", pack["id"])) / str(skills_root)
        if not root.exists():
            continue
        packs += 1
        include = {str(item) for item in (pack.get("include_dirs") or [])}
        paths = sorted(
            path
            for path in root.rglob("SKILL.md")
            if path.is_file() and not path.is_symlink() and _included(path, root, include)
        )
        for path in paths:
            if limit and parsed >= limit:
                break
            discovered += 1
            skill = parse_skill_file(path)
            if not skill.name or not skill.description:
                quarantined += 1
                continue
            parsed += 1
            text = path.read_text(encoding="utf-8")
            decision = decision_for(pack, skill.name)
            reason = _unsafe_reason(text)
            tier = "core" if parsed <= (limit or 60) else "extended"
            if not decision.allowed:
                reason = decision.reason
            if reason:
                tier = "quarantined"
                quarantined += 1
                rejected_safety += 1
            relative = path.relative_to(vendor_root / str(pack.get("dir", pack["id"])))
            metadata = skill.metadata.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            frameworks = metadata.get("frameworks", [])
            if not isinstance(frameworks, list):
                frameworks = [frameworks]
            body_lines = text.splitlines()
            score = min(1.0, 0.2 + 0.2 * bool(skill.description) + 0.15 * (len(body_lines) >= 80))
            record = SkillRecord(
                id=f"skl_{hashlib.sha256(str(relative).encode()).hexdigest()[:16]}",
                pack_id=str(pack["id"]),
                pack_ref=str(pack["ref"]),
                name=skill.name,
                slug=f"{pack['id']}/{relative.parent.as_posix()}",
                category=_category(path, root, skill.metadata),
                description=skill.description,
                triggers=_triggers(skill.description),
                frameworks=tuple(map(str, frameworks)),
                tags=tuple(map(str, metadata.get("tags", []))),
                body_path=relative.as_posix(),
                body_sha256=hashlib.sha256(text.encode()).hexdigest(),
                body_tokens=max(1, len(text.split()) * 4 // 3),
                has_scripts=(path.parent / "scripts").is_dir(),
                has_templates=(path.parent / "templates").is_dir(),
                has_references=(path.parent / "references").is_dir(),
                license=decision.license or "",
                redistribute=decision.redistribute,
                tier=tier,
                quality_score=score,
                reject_reason=reason,
            )
            store.upsert(record)
        if limit and parsed >= limit:
            break
    return IngestReport(packs, discovered, parsed, quarantined, rejected_safety, int((time.monotonic() - started) * 1000))


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[4]
    print(ingest_all(manifest_path=root / "vendor/skills/MANIFEST.yaml"))
