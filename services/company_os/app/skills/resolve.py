"""Capability-filtered skill resolution using BM25 and reciprocal-rank fusion."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.memory.fuse import bm25_rank, rrf_order
from app.skills.library import SkillLibrary, SkillRecord


def _categories(capability_id: str, path: Path | None = None) -> set[str]:
    config_path = path or Path(__file__).resolve().parents[4] / "config/capabilities.yaml"
    if not config_path.exists():
        return set()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    for capability in config.get("capabilities", []):
        if capability.get("id") == capability_id:
            return set(map(str, capability.get("skill_categories", [])))
    return set()


def resolve_skills(
    *, capability_id: str, query: str, k: int = 4, library: SkillLibrary | None = None
) -> list[SkillRecord]:
    """Return at most ``k`` usable skills, ranked by keyword relevance and quality."""
    records = (library or SkillLibrary()).all()
    categories = _categories(capability_id)
    allowed = [
        record
        for record in records
        if record.tier in {"core", "extended"}
        and record.license
        and (not categories or record.category in categories)
    ]
    by_slug = {record.slug: record for record in allowed}
    keyword = bm25_rank(
        query,
        ((record.slug, " ".join((record.name, record.description, *record.triggers))) for record in allowed),
        limit=20,
    )
    quality = [
        record.slug
        for record in sorted(allowed, key=lambda record: record.quality_score, reverse=True)[:20]
    ]
    ordered = rrf_order([keyword, quality], limit=max(k * 3, k))
    return [by_slug[slug] for slug in ordered[:k]]
