"""Intake: normalize → classify → research → complexity (deterministic fallbacks)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


def normalize(raw_idea: str) -> dict[str, str]:
    verbatim = raw_idea
    text = re.sub(r"\s+", " ", raw_idea).strip()
    return {"verbatim": verbatim, "normalized": text}


@dataclass
class ComplexityResult:
    score: float
    uncertainty: float
    build_feasibility: float
    factors: list[dict[str, Any]] = field(default_factory=list)


def score_complexity(idea: str, classification: dict[str, Any]) -> ComplexityResult:
    factors: list[dict[str, Any]] = [
        {"factor": "base", "weight": 0.25, "why": "every idea starts with baseline complexity"}
    ]
    score = 0.25
    text = idea.lower()

    def add(name: str, w: float, why: str) -> None:
        nonlocal score
        score += w
        factors.append({"factor": name, "weight": w, "why": why})

    if classification.get("business_model") == "marketplace":
        add("two_sided", 0.2, "marketplace needs both sides")
    if classification.get("regulatory_surface") not in {None, "none", ""}:
        add("regulatory", 0.2, f"regulatory surface: {classification.get('regulatory_surface')}")
    if any(k in text for k in ("payment", "stripe", "checkout", "billing")):
        add("payments", 0.15, "payments mentioned")
    if any(k in text for k in ("whatsapp", "instagram", "sms", "slack")):
        add("messaging_integration", 0.1, "messaging channel integration")
    if len(idea.split()) > 40:
        add("long_brief", 0.05, "longer brief implies more open variables")
    if classification.get("build_archetype") == "unsupported":
        add("unsupported_archetype", 0.25, "build archetype unsupported")

    score = max(0.0, min(1.0, score))
    uncertainty = min(1.0, 0.3 + score * 0.5)
    feasibility = max(0.1, 1.0 - score * 0.6)
    return ComplexityResult(score, uncertainty, feasibility, factors)


def classify_deterministic(idea: str) -> dict[str, Any]:
    text = idea.lower()
    domain = "general"
    if any(k in text for k in ("baker", "cake", "food", "restaurant")):
        domain = "food_service"
    elif any(k in text for k in ("finance", "bank", "invoice")):
        domain = "fintech"

    business_model = "saas"
    if "marketplace" in text or "two-sided" in text:
        business_model = "marketplace"
    elif "shop" in text or "ecommerce" in text or "store" in text:
        business_model = "ecommerce"

    build_archetype = "crud_web"
    if "bot" in text or "whatsapp" in text or "telegram" in text:
        build_archetype = "integration_bot"
    if "marketplace" in text:
        build_archetype = "marketplace"

    regulatory = "none"
    if any(k in text for k in ("payment", "stripe", "checkout", "billing", "escrow", "kyc")):
        regulatory = "payments"
    if any(
        k in text
        for k in (
            "health",
            "hipaa",
            "patient",
            "medical",
            "physiotherapist",
            "insurance",
            "clinic",
            "minors",
        )
    ):
        regulatory = "health"

    return {
        "domain": domain,
        "business_model": business_model,
        "build_archetype": build_archetype,
        "audience": "founders" if "founder" in text else "consumers",
        "monetization": "subscription",
        "regulatory_surface": regulatory,
        "novelty": "differentiated",
        "classifier_version": "deterministic-v1",
    }


async def research_recon(
    idea: str,
    *,
    budget_s: float = 20.0,
    extra_queries: list[str] | None = None,
) -> dict[str, Any]:
    """Delegate recon to the provider boundary and preserve its provenance."""
    from app.signals.research import research_recon as provider_recon

    return await provider_recon(idea, budget_s=budget_s, extra_queries=extra_queries)


def question_budget(complexity: float, uncertainty: float, regulatory: str) -> tuple[int, dict[str, Any]]:
    """Return N in [3,9] with machine-readable reason."""
    n = 3
    reasons: list[str] = ["base_minimum_3"]
    if complexity >= 0.35:
        n += 2
        reasons.append("complexity_mid")
    if complexity >= 0.55:
        n += 1
        reasons.append("complexity_elevated")
    if complexity >= 0.7:
        n += 2
        reasons.append("complexity_high")
    if uncertainty >= 0.55:
        n += 1
        reasons.append("uncertainty")
    if regulatory not in {"none", ""}:
        n += 1
        reasons.append("regulatory")
    n = max(3, min(9, n))
    return n, {"n": n, "final": n, "reasons": reasons}
