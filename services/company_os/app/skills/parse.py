"""Tolerant parsing for Agent Skills ``SKILL.md`` files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class ParsedSkill:
    name: str
    description: str
    metadata: dict[str, Any]
    body: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


def parse_skill_text(text: str) -> ParsedSkill:
    """Parse YAML frontmatter, retaining a usable body on malformed metadata."""
    warnings: list[str] = []
    metadata: dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end < 0:
            warnings.append("unterminated_frontmatter")
        else:
            raw = text[3:end].strip()
            body = text[end + 4 :].lstrip("\n")
            try:
                parsed = yaml.safe_load(raw) or {}
                if isinstance(parsed, dict):
                    metadata = parsed
                else:
                    warnings.append("frontmatter_not_mapping")
            except yaml.YAMLError as exc:
                warnings.append(f"invalid_frontmatter:{exc.__class__.__name__}")
    else:
        warnings.append("missing_frontmatter")

    name = str(metadata.get("name") or "").strip()
    description = str(metadata.get("description") or "").strip()
    if not name:
        warnings.append("missing_name")
    if not description:
        warnings.append("missing_description")
    return ParsedSkill(name=name, description=description, metadata=metadata, body=body, warnings=tuple(warnings))


def parse_skill_file(path: Path) -> ParsedSkill:
    return parse_skill_text(path.read_text(encoding="utf-8"))
