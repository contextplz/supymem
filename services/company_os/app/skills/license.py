"""License decisions for harvested skill packs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_ANTHROPIC_DOCUMENT_SKILLS = frozenset({"docx", "pdf", "pptx", "xlsx"})


@dataclass(frozen=True, slots=True)
class LicenseDecision:
    license: str | None
    redistribute: bool
    attribution: str | None
    allowed: bool
    reason: str | None = None


def decision_for(pack: dict[str, Any], skill_name: str) -> LicenseDecision:
    """Resolve pack and per-skill licensing from the committed manifest."""
    license_name = pack.get("license")
    if not license_name:
        return LicenseDecision(None, False, None, False, "missing_pack_license")

    redistribute = bool(pack.get("redistribute", True))
    for override in pack.get("license_overrides", []) or []:
        skills = override.get("skills", [])
        if "*" in skills or skill_name in skills:
            license_name = override.get("license", license_name)
            redistribute = bool(override.get("redistribute", redistribute))
            break
    if pack.get("id") == "anthropic-skills" and skill_name in _ANTHROPIC_DOCUMENT_SKILLS:
        redistribute = False
    holder = pack.get("license_holder")
    attribution = f"{license_name}{holder}" if holder else str(license_name)
    return LicenseDecision(str(license_name), redistribute, attribution, True)


def can_materialize(pack: dict[str, Any], skill_name: str, *, exportable: bool = False) -> bool:
    """Exportable bundles may never contain a non-redistributable skill."""
    decision = decision_for(pack, skill_name)
    return decision.allowed and (not exportable or decision.redistribute)
