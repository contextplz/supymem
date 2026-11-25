"""Heuristic requirement-tree extraction for an idea.

Extraction identifies what a venture needs; it deliberately does not assess
whether Company OS can supply it.
"""
from __future__ import annotations

import re

from app.feasibility.model import Domain, RequirementNode

_SIGNALS: tuple[tuple[Domain, tuple[str, ...], str], ...] = (
    (Domain.HARDWARE, ("device", "sensor", "wearable", "robot", "drone", "pcb", "hardware"), "Physical product"),
    (Domain.REGULATED, ("medical", "clinical", "prescribe", "hipaa", "fda", "insurance", "financial advice"), "Regulatory approval"),
    (Domain.FINANCIAL, ("payment", "stripe", "checkout", "payout", "subscription", "billing", "kyc"), "Payment collection and payouts"),
    (Domain.IDENTITY, ("account", "oauth", "login", "sign up", "social", "instagram", "youtube", "whatsapp"), "Social account connection"),
    (Domain.GTM, ("market", "marketing", "seo", "sales", "ads", "outreach", "customer acquisition"), "Go-to-market"),
    (Domain.CONTENT, ("content", "copy", "video", "design", "brand"), "Content and brand"),
)


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def extract_requirements(idea: str, *, structure: dict[str, object] | None = None) -> RequirementNode:
    """Produce a bounded requirement tree from text and optional supplied structure."""
    text = " ".join(idea.split())
    lower = text.lower()
    children: list[RequirementNode] = [
        RequirementNode(id="software-core", label="Core software experience", domain=Domain.SOFTWARE)
    ]
    seen = {Domain.SOFTWARE}
    for domain, terms, label in _SIGNALS:
        if domain not in seen and any(term in lower for term in terms):
            children.append(RequirementNode(id=_slug(label), label=label, domain=domain))
            seen.add(domain)

    supplied = (structure or {}).get("requirements")
    if isinstance(supplied, list):
        for index, value in enumerate(supplied[:55]):
            if not isinstance(value, dict) or not isinstance(value.get("label"), str):
                continue
            try:
                domain = Domain(str(value.get("domain", "software")))
            except ValueError:
                domain = Domain.SOFTWARE
            label = value["label"].strip()
            if label:
                children.append(RequirementNode(id=f"provided-{index}-{_slug(label)}", label=label, domain=domain))
    return RequirementNode(id="venture", label=text or "Venture", domain=Domain.SOFTWARE, children=children[:60])


extract = extract_requirements
